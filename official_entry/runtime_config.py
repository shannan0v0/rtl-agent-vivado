"""One source of runtime settings for HTTP and task-directory entry points."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeConfig:
    model_base_url: str
    model_name: str
    ctx_size: int = 16384
    max_tokens: int = 8192
    temperature: float = 0.0
    top_p: float = 1.0
    request_budget_s: int = 360
    archive_reserve_s: float = 5.0
    entry_host: str = "127.0.0.1"
    entry_port: int = 7860
    evidence_root: str = "./evidence"
    prompt_path: str = "./skill/prompt_templates/rtl_generation.md"
    model_lock_path: str = "./runtime/model_service.lock"
    job_total_result_bytes: int = 8 * 1024 * 1024

    @classmethod
    def load(cls, path: str | Path | None = None) -> "RuntimeConfig":
        selected = path or os.environ.get("RTL_MODEL_LED_CONFIG")
        if not selected:
            selected = Path(__file__).absolute().parent.parent / "runtime_config.json"
        config_path = Path(selected)
        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("runtime config must be a JSON object")
        fields = {name: raw[name] for name in cls.__dataclass_fields__ if name in raw}
        config = cls(**fields)
        config.validate()
        return config

    def validate(self) -> None:
        if not isinstance(self.model_base_url, str) or not isinstance(self.model_name, str):
            raise ValueError("model_base_url and model_name must be strings")
        if not self.model_base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
            raise ValueError("model_base_url must use loopback HTTP")
        if not self.model_name.strip():
            raise ValueError("model_name must be explicit")
        if self.entry_host != "127.0.0.1":
            raise ValueError("entry_host must be 127.0.0.1")
        if isinstance(self.entry_port, bool) or not isinstance(self.entry_port, int) or not 1 <= self.entry_port <= 65535:
            raise ValueError("entry_port must be a TCP port")
        if (
            isinstance(self.job_total_result_bytes, bool)
            or not isinstance(self.job_total_result_bytes, int)
            or not 1024 * 1024 <= self.job_total_result_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError("job_total_result_bytes must be an integer from 1048576 to 536870912")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.ctx_size, self.max_tokens)):
            raise ValueError("ctx_size and max_tokens must be integers")
        if self.ctx_size <= 0 or self.max_tokens <= 0:
            raise ValueError("ctx_size and max_tokens must be positive")
        if (
            isinstance(self.temperature, bool)
            or isinstance(self.top_p, bool)
            or not isinstance(self.temperature, (int, float))
            or not isinstance(self.top_p, (int, float))
        ):
            raise ValueError("sampling values must be numeric")
        if self.max_tokens != 8192 or self.temperature != 0.0 or self.top_p != 1.0:
            raise ValueError("agent must retain the team limits: max_tokens=8192, temperature=0, top_p=1")
        if isinstance(self.request_budget_s, bool) or not isinstance(self.request_budget_s, int):
            raise ValueError("request_budget_s must be an integer")
        if isinstance(self.archive_reserve_s, bool) or not isinstance(self.archive_reserve_s, (int, float)):
            raise ValueError("archive_reserve_s must be numeric")
        if self.request_budget_s <= 0 or self.archive_reserve_s < 0:
            raise ValueError("invalid request budget")
        if self.archive_reserve_s >= self.request_budget_s:
            raise ValueError("archive reserve must be below the request budget")

    def public_dict(self) -> dict[str, Any]:
        return {
            "model_base_url": self.model_base_url,
            "model_name": self.model_name,
            "ctx_size": self.ctx_size,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "request_budget_s": self.request_budget_s,
            "archive_reserve_s": self.archive_reserve_s,
            "model_lock_path": self.model_lock_path,
            "entry_host": self.entry_host,
            "entry_port": self.entry_port,
            "job_total_result_bytes": self.job_total_result_bytes,
        }


def resolve_package_path(value: str, *, package_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(os.path.abspath(str(package_root / path)))


class ModelCallLock:
    """Cross-process serialization for agent and baseline access to one server."""

    def __init__(self, path: str | Path, deadline: float) -> None:
        self.path = Path(path)
        self.deadline = deadline
        self._stream: Any = None

    def __enter__(self) -> "ModelCallLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        if os.name == "nt":
            import msvcrt

            self._stream.seek(0)
            if self.path.stat().st_size == 0:
                self._stream.write(b"0")
                self._stream.flush()
                self._stream.seek(0)
            while time.monotonic() < self.deadline:
                try:
                    msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                    return self
                except OSError:
                    time.sleep(min(0.05, max(0.0, self.deadline - time.monotonic())))
        else:
            import fcntl

            while time.monotonic() < self.deadline:
                try:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    time.sleep(min(0.05, max(0.0, self.deadline - time.monotonic())))
        self._stream.close()
        self._stream = None
        raise TimeoutError("model service queue wait exceeded request budget")

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._stream is None:
            return
        if os.name == "nt":
            import msvcrt

            self._stream.seek(0)
            msvcrt.locking(self._stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None
