"""Shared task-dir processing used by HTTP workers and the public run.sh path."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .baseline_observer import run_baseline_observed
from .model_agent import run_agent, sha256_bytes
from .runtime_config import ModelCallLock, RuntimeConfig, resolve_package_path


def _atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("wb") as stream:
        stream.write(raw)
        stream.flush()
    temp.replace(path)


def _diagnostic(out_dir: Path, event: dict[str, Any]) -> None:
    path = out_dir / "diagnostics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def process_task(
    task_dir: Path,
    out_dir: Path,
    mode: str,
    config: RuntimeConfig,
    *,
    timeout_s: float | None = None,
    transport: Any = None,
    baseline_urlopen: Any = None,
) -> dict[str, Any]:
    if mode not in {"agent", "baseline"}:
        raise ValueError("mode must be agent or baseline")
    started = time.monotonic()
    limit = float(config.request_budget_s if timeout_s is None else timeout_s)
    out_dir.mkdir(parents=True, exist_ok=True)
    solution_path = out_dir / "solution.v"
    trace_path = out_dir / "trace.jsonl"
    prompt_path = task_dir / "prompt.txt"
    if not prompt_path.is_file():
        _atomic(solution_path, b"")
        if not trace_path.exists():
            _atomic(trace_path, b"")
        error = "missing prompt.txt"
        _diagnostic(out_dir, {"ts": time.time(), "state": "output", "status": "empty", "reason": error})
        return {"status": "empty_input_error", "reason": error, "solution_sha256": sha256_bytes(b"")}
    prompt = prompt_path.read_bytes()
    interface_path = task_dir / "interface.txt"
    interface = interface_path.read_bytes() if interface_path.is_file() else b""
    input_summary = {
        "mode": mode,
        "prompt_sha256": sha256_bytes(prompt),
        "interface_sha256": sha256_bytes(interface) if interface else None,
        "interface_present": bool(interface),
        "config_sha256": hashlib.sha256(json.dumps(config.public_dict(), sort_keys=True).encode("utf-8")).hexdigest(),
    }
    _atomic(out_dir / "input_manifest.json", (json.dumps(input_summary, sort_keys=True) + "\n").encode("utf-8"))

    package_root = Path(__file__).absolute().parent.parent
    lock_path = resolve_package_path(config.model_lock_path, package_root=package_root)
    deadline_at = started + limit
    queue_wait_started = time.monotonic()
    queue_wait_s = 0.0
    try:
        with ModelCallLock(lock_path, deadline_at):
            queue_wait_s = time.monotonic() - queue_wait_started
            remaining = max(0.0, deadline_at - time.monotonic())
            if mode == "agent":
                result = run_agent(
                    task_dir,
                    out_dir,
                    config,
                    timeout_s=remaining,
                    transport=transport,
                )
            else:
                result = _run_baseline(task_dir, out_dir, config, started, limit, input_summary, baseline_urlopen)
    except Exception as error:
        reason = "model_queue_or_worker_error:{}: {}".format(type(error).__name__, str(error)[:300])
        _atomic(solution_path, b"")
        if not trace_path.exists():
            _atomic(trace_path, b"")
        _diagnostic(out_dir, {"ts": time.time(), "state": "worker_error", "reason": reason})
        _diagnostic(out_dir, {"ts": time.time(), "state": "output", "status": "empty", "reason": reason, "solution_sha256": sha256_bytes(b"")})
        result = {"status": "empty_queue_or_worker_error", "reason": reason, "solution_sha256": sha256_bytes(b"")}
    if time.monotonic() - started > limit:
        reason = "task-directory request budget exceeded before output completion"
        _atomic(solution_path, b"")
        result.update(
            {
                "status": "empty_budget_exceeded",
                "reason": reason,
                "output_status": "empty_budget_exceeded",
                "solution_sha256": sha256_bytes(b""),
            }
        )
        _diagnostic(out_dir, {"ts": time.time(), "state": "output", "status": "empty_budget_exceeded", "reason": reason, "solution_sha256": sha256_bytes(b"")})
    result["queue_wait_s"] = round(queue_wait_s, 6)
    result["elapsed_s"] = round(time.monotonic() - started, 6)
    result.setdefault("solution_sha256", sha256_bytes(solution_path.read_bytes() if solution_path.is_file() else b""))
    status_path = out_dir / "output_status.json"
    _atomic(status_path, (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    if time.monotonic() - started > limit and result.get("status") != "empty_budget_exceeded":
        reason = "task-directory request budget exceeded during output preparation"
        _atomic(solution_path, b"")
        result.update(
            {
                "status": "empty_budget_exceeded",
                "reason": reason,
                "output_status": "empty_budget_exceeded",
                "solution_sha256": sha256_bytes(b""),
                "elapsed_s": round(time.monotonic() - started, 6),
            }
        )
        _diagnostic(out_dir, {"ts": time.time(), "state": "output", "status": "empty_budget_exceeded", "reason": reason, "solution_sha256": sha256_bytes(b"")})
        _atomic(status_path, (json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    return result


def _run_baseline(
    task_dir: Path,
    out_dir: Path,
    config: RuntimeConfig,
    started: float,
    limit: float,
    input_summary: dict[str, Any],
    baseline_urlopen: Any = None,
) -> dict[str, Any]:
    solution_path = out_dir / "solution.v"
    _diagnostic(out_dir, {"ts": time.time(), "state": "prepared", **input_summary})
    try:
        summary = run_baseline_observed(
            task_dir,
            out_dir,
            config,
            original_urlopen=baseline_urlopen,
            deadline_at=started + limit,
        )
        _diagnostic(out_dir, {"ts": time.time(), "state": "send", "status": "observed", "calls": summary["calls"]})
        _diagnostic(out_dir, {"ts": time.time(), "state": "response", "status": "received_or_failed", "metadata_calls": summary["metadata_calls"], "generation_calls": summary["generation_calls"]})
        _diagnostic(out_dir, {"ts": time.time(), "state": "parse", "status": "baseline_script_completed", "baseline_sha256": summary["baseline_sha256"], "baseline_error": summary["baseline_error"]})
        _diagnostic(out_dir, {"ts": time.time(), "state": "check", "status": "not_applicable_fixed_baseline"})
        if summary["baseline_error"]:
            _atomic(solution_path, b"")
            status = "empty_baseline_error"
            reason = summary["baseline_error"]
        else:
            status = "baseline_candidate" if solution_path.is_file() and solution_path.stat().st_size else "empty_baseline_output"
            reason = "" if status == "baseline_candidate" else "fixed baseline produced an empty solution"
        if time.monotonic() - started > limit:
            _atomic(solution_path, b"")
            status = "empty_budget_exceeded"
            reason = "task-directory request budget exceeded"
        output_sha = sha256_bytes(solution_path.read_bytes() if solution_path.is_file() else b"")
        _diagnostic(out_dir, {"ts": time.time(), "state": "output", "status": status, "reason": reason, "solution_sha256": output_sha})
        result = {"status": status, "reason": reason, "solution_sha256": output_sha, **summary}
    except Exception as error:
        reason = "baseline_runner_error:{}: {}".format(type(error).__name__, str(error)[:300])
        _atomic(solution_path, b"")
        _diagnostic(out_dir, {"ts": time.time(), "state": "baseline_error", "reason": reason})
        _diagnostic(out_dir, {"ts": time.time(), "state": "output", "status": "empty_baseline_error", "reason": reason, "solution_sha256": sha256_bytes(b"")})
        result = {"status": "empty_baseline_error", "reason": reason, "solution_sha256": sha256_bytes(b"")}
    return result
