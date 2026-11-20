"""Public task-directory CLI; delegates each call to a supervised worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

from official_entry.adapter import BackendTimeout, TaskRequest
from official_entry.process_supervision import enable_child_subreaper
from official_entry.runtime_config import RuntimeConfig
from official_entry.server import RootScriptBackend, TaskDirBackend
from serve.service_manager import RecordContextLease


def run_from_task_dir(
    task_dir: Path,
    out_dir: Path,
    mode: str,
    config_path: Path,
    *,
    timeout_s: float | None = None,
    transport: object = None,
    baseline_urlopen: object = None,
) -> dict[str, object]:
    """In-process helper for unit tests; the public CLI uses a worker process."""
    from official_entry.task_runner import process_task

    config = RuntimeConfig.load(config_path)
    return process_task(
        task_dir,
        out_dir,
        mode,
        config,
        timeout_s=timeout_s,
        transport=transport,
        baseline_urlopen=baseline_urlopen,
    )


def _resolve_path(value: str, caller_cwd: Path, *, must_exist: bool = False) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = caller_cwd / path
    absolute = Path(os.path.abspath(os.fspath(path)))
    if must_exist and not absolute.exists():
        raise FileNotFoundError(str(absolute))
    return absolute


def _append_diagnostic(out_dir: Path, payload: dict[str, Any]) -> None:
    path = out_dir / "diagnostics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write((json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def _write_parent_failure(out_dir: Path, reason: str, status: str, elapsed_s: float) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    solution = out_dir / "solution.v"
    temporary = solution.with_name(solution.name + ".tmp")
    temporary.write_bytes(b"")
    temporary.replace(solution)
    if not (out_dir / "trace.jsonl").exists():
        (out_dir / "trace.jsonl").write_bytes(b"")
    _append_diagnostic(
        out_dir,
        {"ts": time.time(), "state": "directory_supervisor", "status": status, "reason": reason},
    )
    (out_dir / "failure.txt").write_text(reason + "\n", encoding="utf-8")
    result = {
        "status": status,
        "reason": reason,
        "elapsed_s": round(elapsed_s, 6),
        "solution_sha256": hashlib.sha256(b"").hexdigest(),
    }
    (out_dir / "output_status.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    invoked_at = time.monotonic()
    caller_cwd = Path.cwd()
    enable_child_subreaper()
    parser = argparse.ArgumentParser(description="Supervise one RTL task-directory request")
    parser.add_argument("--mode", required=True, choices=("agent", "baseline"))
    parser.add_argument("--task-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--timeout-s", type=float)
    args = parser.parse_args()

    task_dir = _resolve_path(args.task_dir, caller_cwd, must_exist=True)
    out_dir = _resolve_path(args.out_dir, caller_cwd)
    config_path = _resolve_path(args.config, caller_cwd, must_exist=True)
    config = RuntimeConfig.load(config_path)
    limit_s = float(config.request_budget_s)
    official_deadline = os.environ.get("AGENT_DEADLINE_S")
    if official_deadline is not None:
        try:
            official_limit = int(official_deadline)
        except ValueError as error:
            raise ValueError("AGENT_DEADLINE_S must be a positive integer") from error
        if official_limit <= 0:
            raise ValueError("AGENT_DEADLINE_S must be a positive integer")
        limit_s = min(limit_s, float(official_limit))
    if args.timeout_s is not None:
        limit_s = min(limit_s, args.timeout_s)
    if limit_s <= 0:
        raise ValueError("effective directory request budget must be positive")
    elapsed_before_worker = time.monotonic() - invoked_at
    reserve_s = min(max(float(config.archive_reserve_s), 0.0), max(0.0, limit_s * 0.25))
    worker_timeout_s = limit_s - elapsed_before_worker - reserve_s
    if worker_timeout_s <= 0:
        result = _write_parent_failure(
            out_dir,
            "directory request budget exhausted before worker start",
            "empty_budget_exceeded",
            time.monotonic() - invoked_at,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 124

    task_id = os.environ.get("RTL_TASK_ID") or "taskdir-" + secrets.token_hex(8)
    nonce = os.environ.get("RTL_TASK_NONCE", "")
    managed_context = os.environ.get("RTL_RECORD_CONTEXT_MANAGED") == "1"
    context_lease = None
    try:
        if args.mode == "baseline":
            # Baseline mode follows the evaluator-facing root shell and never
            # imports baseline.py or monkey-patches its HTTP client.
            supervisor = RootScriptBackend("baseline", config, config_path)
            request = TaskRequest(
                task_id, nonce, "baseline", "", "", int(limit_s), invoked_at + limit_s,
                received_monotonic=invoked_at, total_deadline_at=invoked_at + limit_s,
                work_deadline_at=invoked_at + limit_s - reserve_s,
            )
            supervisor.run(request, task_dir, out_dir, timeout_s=worker_timeout_s)
            solution_path = out_dir / "solution.v"
            trace_path = out_dir / "trace.jsonl"
            solution = solution_path.read_bytes() if solution_path.is_file() else b""
            trace = trace_path.read_bytes() if trace_path.is_file() else b""
            result = {
                "status": "baseline_output_written" if solution_path.is_file() and trace_path.is_file() else "baseline_output_missing",
                "solution_sha256": hashlib.sha256(solution).hexdigest(),
                "trace_sha256": hashlib.sha256(trace).hexdigest(),
                "elapsed_s": round(time.monotonic() - invoked_at, 6),
            }
            (out_dir / "output_status.json").write_text(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result["status"] == "baseline_output_written" else 1

        supervisor = TaskDirBackend("agent", config_path)
        if not managed_context:
            assets_path = Path(os.environ.get("RTL_MODEL_LED_ASSETS", str(Path(__file__).resolve().parent / "runtime_assets.json")))
            context_lease = RecordContextLease(
                assets_path,
                {
                    "association_status": "task_scope_only",
                    "task_id": task_id,
                    "nonce": nonce,
                    "mode": "agent",
                    "request_deadline_monotonic": invoked_at + limit_s,
                },
                invoked_at + limit_s,
            )
            context_lease.__enter__()
        os.environ["RTL_RECORD_CONTEXT_MANAGED"] = "1"
        # TaskDirBackend launches run_task_worker, which calls process_task
        # directly and stays in this outer task's process group.
        worker_timeout_s = invoked_at + limit_s - time.monotonic() - reserve_s
        if worker_timeout_s <= 0:
            result = _write_parent_failure(
                out_dir,
                "directory request budget exhausted before worker start",
                "empty_budget_exceeded",
                time.monotonic() - invoked_at,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 124
        supervisor.run(None, task_dir, out_dir, timeout_s=worker_timeout_s)
    except BackendTimeout as error:
        result = _write_parent_failure(
            out_dir,
            "directory worker tree timed out: {}".format(error),
            "empty_worker_timeout",
            time.monotonic() - invoked_at,
        )
        result["worker_pid"] = supervisor.last_pid
        (out_dir / "output_status.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 124
    except Exception as error:
        result = _write_parent_failure(
            out_dir,
            "directory worker error:{}: {}".format(type(error).__name__, str(error)[:300]),
            "empty_worker_error",
            time.monotonic() - invoked_at,
        )
        result["worker_pid"] = supervisor.last_pid
        (out_dir / "output_status.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        if context_lease is not None:
            context_lease.__exit__(None, None, None)

    elapsed = time.monotonic() - invoked_at
    try:
        result = json.loads((out_dir / "output_status.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise ValueError("worker output_status is not an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        result = _write_parent_failure(
            out_dir,
            "directory worker returned without readable output status: {}".format(error),
            "empty_worker_result_error",
            elapsed,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1

    if elapsed >= limit_s:
        result = _write_parent_failure(
            out_dir,
            "directory request exceeded total wall-clock budget during output preparation",
            "empty_budget_exceeded",
            elapsed,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 124

    result["directory_elapsed_s"] = round(elapsed, 6)
    result["directory_worker_pid"] = supervisor.last_pid
    (out_dir / "output_status.json").write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
