"""Development-only legacy observer; formal routes execute the root scripts."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .model_agent import sha256_bytes
from .baseline_identity import BASELINE_PY_SHA256, RUN_BASELINE_SH_SHA256
from .runtime_config import RuntimeConfig

EXPECTED_BASELINE_SHA256 = BASELINE_PY_SHA256
EXPECTED_BASELINE_SCRIPT_SHA256 = RUN_BASELINE_SH_SHA256


class ReplayResponse:
    def __init__(self, raw: bytes, status: int, headers: Any) -> None:
        self._raw = raw
        self.status = status
        self.code = status
        self.headers = headers
        self._offset = 0

    def __enter__(self) -> "ReplayResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            amount = len(self._raw) - self._offset
        result = self._raw[self._offset:self._offset + amount]
        self._offset += len(result)
        return result


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
    temporary.replace(path)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_write(path, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def run_baseline_observed(
    task_dir: Path,
    out_dir: Path,
    config: RuntimeConfig,
    *,
    original_urlopen: Callable[..., Any] | None = None,
    deadline_at: float | None = None,
) -> dict[str, Any]:
    baseline_path = Path(__file__).resolve().parent.parent / "baseline.py"
    baseline_sha = sha256_bytes(baseline_path.read_bytes())
    script_sha = sha256_bytes(baseline_path.with_name("run_baseline.sh").read_bytes())
    if baseline_sha != EXPECTED_BASELINE_SHA256 or script_sha != EXPECTED_BASELINE_SCRIPT_SHA256:
        raise RuntimeError("copied baseline files failed their local byte-identity checks")
    calls_dir = out_dir / "baseline_calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    calls: list[dict[str, Any]] = []
    original = original_urlopen or urllib.request.urlopen
    old_env = {key: os.environ.get(key) for key in ("LLM_BASE_URL", "MODEL_NAME", "TRACK")}
    original_argv = sys.argv
    baseline_error: str | None = None

    def observed(request: Any, *args: Any, **kwargs: Any) -> ReplayResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        method = request.get_method() if hasattr(request, "get_method") else "GET"
        request_body = getattr(request, "data", None) or b""
        kind = "metadata" if url.rstrip("/").endswith("/models") else "generation"
        if kind == "generation" and sum(call["kind"] == "generation" for call in calls) >= 1:
            raise RuntimeError("baseline generation request limit exceeded")
        index = sum(call["kind"] == kind for call in calls)
        call_dir = calls_dir / "call_{:02d}_{}".format(index, kind)
        call_dir.mkdir(parents=True, exist_ok=False)
        request_sha = sha256_bytes(request_body)
        _atomic_write(call_dir / "request_body.bin", request_body)
        prepared = time.time()
        try:
            send_at = time.time()
            observed_kwargs = dict(kwargs)
            if deadline_at is not None:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("baseline transport budget exhausted before send")
                existing_timeout = observed_kwargs.get("timeout")
                observed_kwargs["timeout"] = min(float(existing_timeout), remaining) if existing_timeout is not None else remaining
            with original(request, *args, **observed_kwargs) as response:
                response_body = response.read()
                response_status = int(getattr(response, "status", getattr(response, "code", 200)))
                response_headers = getattr(response, "headers", {})
            received = time.time()
            response_sha = sha256_bytes(response_body)
            _atomic_write(call_dir / "response_body.bin", response_body)
            call = {
                "kind": kind,
                "url": url,
                "method": method,
                "request_sha256": request_sha,
                "response_sha256": response_sha,
                "http_status": response_status,
                "prepared_at": prepared,
                "send_attempted_at": send_at,
                "response_received_at": received,
                "observer_transformations": 0,
            }
            _atomic_json(call_dir / "record.json", call)
            if kind == "generation":
                try:
                    envelope = json.loads(response_body.decode("utf-8"))
                    choice = envelope["choices"][0]
                    content = choice["message"].get("content") or ""
                    _atomic_write(call_dir / "assistant_content.txt", str(content).encode("utf-8"))
                    _atomic_json(
                        call_dir / "response_summary.json",
                        {
                            "response_model": envelope.get("model"),
                            "finish_reason": choice.get("finish_reason"),
                            "usage": envelope.get("usage") or {},
                        },
                    )
                except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
                    call["parse_error"] = "{}: {}".format(type(error).__name__, str(error)[:300])
            calls.append(call)
            return ReplayResponse(response_body, response_status, response_headers)
        except urllib.error.HTTPError as error:
            response_body = error.read()
            response_sha = sha256_bytes(response_body)
            _atomic_write(call_dir / "response_body.bin", response_body)
            call = {
                "kind": kind,
                "url": url,
                "method": method,
                "prepared_at": prepared,
                "send_attempted_at": locals().get("send_at"),
                "response_received_at": time.time(),
                "request_sha256": request_sha,
                "response_sha256": response_sha,
                "http_status": int(error.code),
                "http_error": str(error)[:300],
                "observer_transformations": 0,
            }
            _atomic_json(call_dir / "record.json", call)
            calls.append(call)
            raise
        except Exception as error:
            detail = "{}: {}".format(type(error).__name__, str(error)[:300])
            _atomic_json(
                call_dir / "record.json",
                {
                    "kind": kind,
                    "url": url,
                    "method": method,
                    "prepared_at": prepared,
                    "send_attempted_at": locals().get("send_at"),
                    "response_received_at": None,
                    "request_sha256": request_sha,
                    "response_sha256": None,
                    "error": detail,
                    "observer_transformations": 0,
                },
            )
            raise

    try:
        os.environ["LLM_BASE_URL"] = config.model_base_url.rstrip("/")
        os.environ["MODEL_NAME"] = config.model_name
        os.environ["TRACK"] = "rtl"
        spec = importlib.util.spec_from_file_location("rtl_model_led_fixed_baseline", baseline_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load copied fixed baseline")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.argv = [str(baseline_path), str(task_dir), str(out_dir), "rtl"]
        with_urlopen = urllib.request.urlopen
        urllib.request.urlopen = observed  # type: ignore[assignment]
        try:
            module.main()
        finally:
            urllib.request.urlopen = with_urlopen  # type: ignore[assignment]
    except Exception as error:
        baseline_error = "{}: {}".format(type(error).__name__, str(error)[:300])
    finally:
        sys.argv = original_argv
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    generation_calls = [call for call in calls if call["kind"] == "generation"]
    solution_path = out_dir / "solution.v"
    solution_bytes = solution_path.read_bytes() if solution_path.is_file() else b""
    candidate_sha = sha256_bytes(solution_bytes)
    if generation_calls:
        generation = generation_calls[0]
        generation_dir = calls_dir / "call_00_generation"
        content_path = generation_dir / "assistant_content.txt"
        generation["assistant_sha256"] = sha256_bytes(content_path.read_bytes()) if content_path.is_file() else None
        generation["candidate_sha256"] = candidate_sha
        generation["solution_sha256"] = candidate_sha
        _atomic_json(generation_dir / "record.json", generation)
    summary = {
        "calls": calls,
        "metadata_calls": sum(call["kind"] == "metadata" for call in calls),
        "generation_calls": len(generation_calls),
        "candidate_sha256": candidate_sha,
        "solution_sha256": candidate_sha,
        "baseline_sha256": baseline_sha,
        "baseline_error": baseline_error,
        "response_transparent": all(call.get("observer_transformations") == 0 for call in calls),
        "request_count_rule": "one generation POST; metadata GET excluded",
    }
    _atomic_json(out_dir / "observer_summary.json", summary)
    return summary
