"""Loopback-only HTTP shell for the shared task-directory entry."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .adapter import BackendTimeout, OfficialEntry, RequestDeadlineExceeded, TaskRequest
from .job_manager import JobManager
from .baseline_identity import verify_copies
from .lifecycle_types import (
    LifecycleEvidence,
    LifecycleExecutionError,
    ProcessIdentity,
    StopOutcome,
    exception_chain,
)
from .process_supervision import (
    bind_spawn_identity,
    close_spawn_identity,
    enable_child_subreaper,
    require_http_linux_supervision,
    strict_proc_snapshot,
    stop_http_owned_group,
    stop_owned_tree,
)
from .runtime_config import RuntimeConfig, resolve_package_path
from serve.service_manager import RecordContextLease, inspect_runtime_attestation


class _RecordContextExit:
    """Capture the body exception at the call site before a lease releases it."""

    def __init__(self, lease: Any, capture: Any) -> None:
        self._lease = lease
        self._capture = capture

    def __enter__(self) -> Any:
        return self._lease.__enter__()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        if exc is not None:
            self._capture(exc)
        return self._lease.__exit__(exc_type, exc, traceback)


PACKAGE_ROOT = Path(__file__).absolute().parent.parent
_TASK_ENV_ALLOWLIST = (
    "PATH",
    "LD_LIBRARY_PATH",
    "VIVADO_BIN",
    "XILINX_VIVADO",
    "XILINXD_LICENSE_FILE",
    "LM_LICENSE_FILE",
    "FLEXLM_TIMEOUT",
    "EDA_TMP",
    "ROCM_PATH",
    "HIP_PATH",
    "HSA_OVERRIDE_GFX_VERSION",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "RTL_MODEL_LED_PYTHON",
    "AGENT_DEADLINE_S",
)


def _task_environment(config_path: Path, assets_path: Path | None = None) -> dict[str, str]:
    """Build a task environment without forwarding the HTTP bearer token."""
    env = {key: os.environ[key] for key in _TASK_ENV_ALLOWLIST if key in os.environ}
    env.update(
        {
            "RTL_MODEL_LED_CONFIG": str(config_path),
            "TRACK": "rtl",
        }
    )
    if assets_path is not None:
        env["RTL_MODEL_LED_ASSETS"] = str(assets_path)
    return env


def _atomic_text(
    path: Path,
    text: str,
    *,
    deadline_at: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    def require_time(stage: str) -> None:
        if deadline_at is not None and clock() >= deadline_at:
            raise RequestDeadlineExceeded("absolute request deadline reached before {}".format(stage[:80]))

    require_time("HTTP evidence parent mkdir")
    path.parent.mkdir(parents=True, exist_ok=True)
    require_time("HTTP evidence temp open")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        require_time("HTTP evidence stream write")
        stream.write(text)
    require_time("HTTP evidence replace")
    temporary.replace(path)


def _append_timeout_diagnostic(out_dir: Path, timeout_s: float) -> None:
    event = {
        "ts": time.time(),
        "state": "output",
        "status": "worker_tree_terminated",
        "reason": "remaining worker budget expired",
        "worker_timeout_s": round(timeout_s, 6),
    }
    path = out_dir / "diagnostics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write((json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))


def _stop_worker_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.communicate()
        return
    if os.name == "nt":
        observed_tree = _windows_descendants(process.pid)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        # taskkill can be denied in restricted hosts. Kill only PIDs observed as
        # descendants of this newly spawned worker, checking their image again.
        for pid, parent_pid, image in observed_tree:
            if pid == process.pid or not _pid_image_matches(pid, image):
                continue
            _terminate_windows_pid(pid)
        _terminate_windows_pid(process.pid)
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
        try:
            process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("worker exited but an inherited output pipe remained open") from error
        return
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        process.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()


def _windows_descendants(root_pid: int) -> list[tuple[int, int, str]]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32FirstW.restype = wintypes.BOOL
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.Process32NextW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    entries: dict[int, tuple[int, str]] = {}
    try:
        item = ProcessEntry()
        item.dwSize = ctypes.sizeof(ProcessEntry)
        if kernel.Process32FirstW(snapshot, ctypes.byref(item)):
            while True:
                entries[int(item.th32ProcessID)] = (int(item.th32ParentProcessID), str(item.szExeFile))
                if not kernel.Process32NextW(snapshot, ctypes.byref(item)):
                    break
    finally:
        kernel.CloseHandle(snapshot)
    depth: dict[int, int] = {root_pid: 0}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _) in entries.items():
            if parent in depth and pid not in depth:
                depth[pid] = depth[parent] + 1
                changed = True
    result: list[tuple[int, int, str]] = []
    for pid, level in depth.items():
        if pid == root_pid:
            result.append((pid, 0, entries.get(pid, (0, ""))[1]))
        else:
            parent, image = entries[pid]
            result.append((pid, parent, image))
    return sorted(result, key=lambda entry: depth[entry[0]], reverse=True)


def _pid_image_matches(pid: int, expected_name: str) -> bool:
    if not expected_name:
        return False
    current = _windows_process_image(pid)
    if not current:
        return False
    return Path(current).name.casefold() == expected_name.casefold()


def _windows_process_image(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return None
    finally:
        kernel.CloseHandle(handle)


def _terminate_windows_pid(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0001 | 0x00100000, False, pid)
    if not handle:
        return
    try:
        kernel.TerminateProcess(handle, 1)
        kernel.WaitForSingleObject(handle, 5000)
    finally:
        kernel.CloseHandle(handle)


class TaskDirBackend:
    """A bounded, disposable worker; its model service is never its child."""

    def __init__(
        self,
        mode: str,
        config_path: Path,
        *,
        command_prefix: list[str] | None = None,
    ) -> None:
        self.mode = mode
        self.config_path = config_path
        self.command_prefix = command_prefix or [sys.executable, "-m", "serve.run_task_worker"]
        self.last_returncode: int | None = None
        self.last_pid: int | None = None

    def run(self, request: TaskRequest | None, task_dir: Path, out_dir: Path, *, timeout_s: float) -> None:
        command = self.command_prefix + [
            "--mode",
            self.mode,
            "--task-dir",
            str(task_dir),
            "--out-dir",
            str(out_dir),
            "--config",
            str(self.config_path),
            "--timeout-s",
            str(max(0.0, timeout_s)),
        ]
        env = _task_environment(self.config_path)
        process = subprocess.Popen(
            command,
            cwd=str(PACKAGE_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Keep the worker in the task process group established by the
            # outer HTTP dispatcher/evaluator so its deadline signal reaches it.
            start_new_session=False,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        self.last_pid = process.pid
        try:
            stdout, stderr = process.communicate(timeout=max(0.001, timeout_s))
        except subprocess.TimeoutExpired as error:
            stop_owned_tree(process, grace_s=min(0.75, max(0.0, timeout_s * 0.1)))
            self.last_returncode = process.returncode
            _atomic_text(out_dir / "worker_timeout.txt", "worker tree exceeded remaining request budget\n")
            _atomic_text(out_dir / "worker_stdout.txt", _decode(error.stdout or b""))
            _atomic_text(out_dir / "worker_stderr.txt", _decode(error.stderr or b""))
            _append_timeout_diagnostic(out_dir, timeout_s)
            raise BackendTimeout("worker tree stopped at the remaining request deadline") from error
        self.last_returncode = process.returncode
        stop_owned_tree(process, grace_s=min(0.75, max(0.0, timeout_s * 0.1)))
        _atomic_text(out_dir / "worker_stdout.txt", _decode(stdout))
        _atomic_text(out_dir / "worker_stderr.txt", _decode(stderr))
        if process.returncode != 0:
            raise RuntimeError("{} task-dir worker exited {}".format(self.mode, process.returncode))


class RootScriptBackend:
    """Formal HTTP backend that executes the package's evaluator-facing script."""

    def __init__(self, mode: str, config: RuntimeConfig, config_path: Path) -> None:
        self.mode = mode
        self.config = config
        self.config_path = config_path
        self.last_pid: int | None = None
        self.last_returncode: int | None = None
        self.last_lifecycle: LifecycleEvidence | None = None
        self._unclosed_process: subprocess.Popen[bytes] | None = None
        self._unclosed_identity: ProcessIdentity | None = None
        self.assets_path = Path(os.environ.get("RTL_MODEL_LED_ASSETS", str(PACKAGE_ROOT / "serve" / "runtime_assets.json")))

    def run(
        self, request: TaskRequest, task_dir: Path, out_dir: Path, *, timeout_s: float
    ) -> LifecycleEvidence:
        del timeout_s  # The HTTP record's absolute deadlines are authoritative.
        process: subprocess.Popen[bytes] | None = None
        identity: ProcessIdentity | None = None
        current_lifecycle: LifecycleEvidence | None = None
        current_primary_exception: BaseException | None = None
        current_failure_kind: str | None = None
        current_is_timeout = False
        spawn_attempted = False
        stage = "linux_preflight"

        def remember_failure(
            primary_error: BaseException | None,
            evidence: LifecycleEvidence,
            *,
            is_timeout: bool,
            failure_kind: str,
        ) -> None:
            nonlocal current_lifecycle, current_primary_exception, current_failure_kind, current_is_timeout
            current_lifecycle = evidence
            current_primary_exception = current_primary_exception or primary_error
            current_failure_kind = current_failure_kind or failure_kind[:96]
            current_is_timeout = current_is_timeout or bool(is_timeout)
            self.last_lifecycle = evidence

        def pre_spawn_evidence(error: BaseException, *, attempted: bool) -> LifecycleEvidence:
            state = "HANDLE_NOT_RETURNED_UNKNOWN" if attempted else "NOT_ATTEMPTED"
            return LifecycleEvidence(
                stage=stage,
                spawn_state=state,
                identity_state="UNKNOWN" if attempted else "NOT_APPLICABLE",
                stop_requested="UNKNOWN" if attempted else "NOT_NEEDED",
                exit_confirmed="UNKNOWN" if attempted else "NOT_APPLICABLE",
                process_snapshot_complete="UNKNOWN" if attempted else "NOT_APPLICABLE",
                owned_group_quiescent="UNKNOWN" if attempted else "CONFIRMED_NO_POPEN_ATTEMPT",
                reap_status="UNKNOWN" if attempted else "NOT_APPLICABLE",
                pipe_eof="UNKNOWN" if attempted else "NOT_APPLICABLE",
                process_resources_closed="UNKNOWN" if attempted else "CONFIRMED",
                external_writer_scope="UNKNOWN" if attempted else "NOT_APPLICABLE",
                cleanup_deadline_status="UNKNOWN" if attempted else "NOT_APPLICABLE",
                primary_error="{}: {}".format(type(error).__name__[:96], str(error)[:320]),
                exception_chain=exception_chain(error),
            )

        try:
            total_deadline = request.total_deadline_at
            work_deadline = request.work_deadline_at
            if (
                isinstance(total_deadline, bool)
                or not isinstance(total_deadline, (int, float))
                or not math.isfinite(float(total_deadline))
                or isinstance(work_deadline, bool)
                or not isinstance(work_deadline, (int, float))
                or not math.isfinite(float(work_deadline))
                or float(work_deadline) > float(total_deadline)
            ):
                raise ValueError("HTTP request must carry finite absolute work and total deadlines")
            total_deadline = float(total_deadline)
            work_deadline = float(work_deadline)
            require_http_linux_supervision(cleanup_deadline_at=total_deadline)
            if self._unclosed_process is not None:
                raise RuntimeError("previous HTTP process lifecycle remains unclosed; refusing another spawn")

            def remaining(deadline_at: float, phase: str) -> float:
                value = deadline_at - time.monotonic()
                if value <= 0:
                    raise BackendTimeout("absolute request deadline reached {}".format(phase))
                return value

            stage = "runtime_asset_read"
            remaining(work_deadline, "before runtime asset read")
            try:
                runtime_assets = json.loads(self.assets_path.read_text(encoding="utf-8"))
                model_assets = runtime_assets.get("llama_cpp", {})
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError("verified runtime asset manifest is unavailable") from error
            remaining(work_deadline, "after runtime asset read")
            if (
                self.config.model_base_url.rstrip("/") != "http://127.0.0.1:18080/v1"
                or self.config.model_name != model_assets.get("alias")
            ):
                raise RuntimeError("agent and baseline must share configured 127.0.0.1:18080/v1 and the asset alias")
            script = PACKAGE_ROOT / ("run.sh" if self.mode == "agent" else "run_baseline.sh")
            stage = "script_preflight"
            if not script.is_file():
                raise FileNotFoundError("formal entry script is missing: {}".format(script.name))
            remaining(work_deadline, "after script check")
            context = RecordContextLease(
                self.assets_path,
                {
                    "task_id": request.task_id,
                    "nonce": request.nonce,
                    "mode": self.mode,
                    "request_deadline_monotonic": request.deadline_at,
                    "job_id": request.job_id,
                    "archive_id": request.archive_id,
                },
                work_deadline,
            )
            env = _task_environment(self.config_path, self.assets_path)
            env["LLM_BASE_URL"] = self.config.model_base_url.rstrip("/")
            env["MODEL_NAME"] = self.config.model_name
            env["RTL_RECORD_CONTEXT_MANAGED"] = "1"

            def capture_context_body_error(error: BaseException) -> None:
                nonlocal current_lifecycle, current_primary_exception, current_failure_kind, current_is_timeout
                current_primary_exception = current_primary_exception or error
                if current_lifecycle is None and isinstance(error, LifecycleExecutionError):
                    current_lifecycle = error.evidence
                if isinstance(error, LifecycleExecutionError):
                    current_failure_kind = current_failure_kind or error.failure_kind
                    current_is_timeout = current_is_timeout or error.is_timeout
                else:
                    current_failure_kind = current_failure_kind or (
                        "spawn_handle_unknown" if spawn_attempted and process is None
                        else "backend_pre_spawn_failure" if process is None
                        else "backend_exception"
                    )
                    current_is_timeout = current_is_timeout or isinstance(error, (BackendTimeout, TimeoutError))
                if current_lifecycle is not None:
                    self.last_lifecycle = current_lifecycle

            with _RecordContextExit(context, capture_context_body_error):
                remaining(work_deadline, "while acquiring recorder context")
                lock = None
                if self.mode == "baseline":
                    from .runtime_config import ModelCallLock

                    lock_path = resolve_package_path(self.config.model_lock_path, package_root=PACKAGE_ROOT)
                    lock = ModelCallLock(lock_path, work_deadline)
                if lock is not None:
                    lock.__enter__()
                try:
                    remaining(work_deadline, "after model lock wait")
                    reserve_s = max(0.0, self.config.archive_reserve_s)
                    remaining_s = remaining(work_deadline, "immediately before Popen")
                    child_budget_s = int(remaining_s + reserve_s)
                    if child_budget_s <= 0:
                        raise BackendTimeout("remaining work budget cannot be represented")
                    env["AGENT_DEADLINE_S"] = str(child_budget_s)
                    stage = "popen_start"
                    spawn_attempted = True
                    try:
                        process = subprocess.Popen(
                            [str(script), str(task_dir), str(out_dir)],
                            cwd=str(PACKAGE_ROOT),
                            env=env,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            start_new_session=True,
                            close_fds=True,
                        )
                    except BaseException as error:
                        evidence = pre_spawn_evidence(error, attempted=True)
                        timeout = isinstance(error, (BackendTimeout, TimeoutError))
                        remember_failure(error, evidence, is_timeout=timeout, failure_kind="spawn_handle_unknown")
                        raise LifecycleExecutionError(
                            "Popen was attempted but returned no owned process handle",
                            evidence,
                            is_timeout=timeout,
                            failure_kind="spawn_handle_unknown",
                        ) from error
                    self.last_pid = process.pid
                    evidence, stdout, stderr, primary, is_timeout = self._supervise_http_process(
                        process, work_deadline=work_deadline, total_deadline=total_deadline
                    )
                    self.last_lifecycle = evidence
                    current_lifecycle = evidence
                    frozen_primary = primary
                    frozen_is_timeout = bool(is_timeout) if primary is not None else False
                    frozen_failure_kind = (
                        "backend_timeout" if frozen_is_timeout else "backend_exception"
                    ) if primary is not None else None
                    if frozen_primary is not None:
                        remember_failure(
                            frozen_primary,
                            evidence,
                            is_timeout=frozen_is_timeout,
                            failure_kind=frozen_failure_kind,
                        )
                    if not evidence.safe_to_read_outputs:
                        self._unclosed_process = process
                        self._unclosed_identity = self.last_lifecycle_identity
                        remember_failure(
                            frozen_primary,
                            evidence,
                            is_timeout=frozen_is_timeout,
                            failure_kind=frozen_failure_kind or "lifecycle_unconfirmed",
                        )
                        raise LifecycleExecutionError(
                            "HTTP process writers or resources are not confirmed closed",
                            evidence,
                            is_timeout=frozen_is_timeout,
                            failure_kind=frozen_failure_kind or "lifecycle_unconfirmed",
                        ) from primary
                    def stop_before_evidence_write(path_label: str) -> None:
                        if time.monotonic() < total_deadline:
                            return
                        expired = LifecycleEvidence(
                            **{
                                **evidence.__dict__,
                                "stage": "deadline_before_{}".format(path_label)[:96],
                                "cleanup_deadline_status": "EXCEEDED",
                            }
                        )
                        timeout_error = BackendTimeout("total deadline reached before {}".format(path_label))
                        remember_failure(
                            timeout_error, expired, is_timeout=True, failure_kind="cleanup_deadline_exceeded"
                        )
                        raise LifecycleExecutionError(
                            "total deadline reached before bounded process evidence write",
                            expired,
                            is_timeout=True,
                            failure_kind="cleanup_deadline_exceeded",
                        ) from timeout_error

                    try:
                        stop_before_evidence_write("stdout_evidence_write")
                        _atomic_text(
                            out_dir / "worker_stdout.txt", _decode(stdout or b""),
                            deadline_at=total_deadline, clock=time.monotonic,
                        )
                        stop_before_evidence_write("stderr_evidence_write")
                        _atomic_text(
                            out_dir / "worker_stderr.txt", _decode(stderr or b""),
                            deadline_at=total_deadline, clock=time.monotonic,
                        )
                    except BaseException as error:
                        write_error = "pipe_evidence_write:{}:{}".format(
                            type(error).__name__[:64], str(error)[:256]
                        )
                        prior_cleanup_errors = list(evidence.cleanup_errors[:7])
                        prior_cleanup_errors.append(write_error)
                        selected_primary = frozen_primary if frozen_primary is not None else error
                        selected_is_timeout = (
                            frozen_is_timeout
                            if frozen_primary is not None
                            else isinstance(error, RequestDeadlineExceeded)
                        )
                        selected_failure_kind = (
                            frozen_failure_kind
                            if frozen_primary is not None
                            else (
                                "cleanup_deadline_exceeded"
                                if selected_is_timeout
                                else "pipe_evidence_write_failed"
                            )
                        )
                        selected_exception_chain = evidence.exception_chain or exception_chain(selected_primary)
                        evidence = LifecycleEvidence(
                            **{
                                **evidence.__dict__,
                                "stage": "pipe_evidence_write",
                                "primary_error": evidence.primary_error or "{}: {}".format(
                                    type(selected_primary).__name__[:96], str(selected_primary)[:320]
                                ),
                                "exception_chain": selected_exception_chain,
                                "cleanup_errors": tuple(prior_cleanup_errors[:8]),
                            }
                        )
                        remember_failure(
                            selected_primary, evidence, is_timeout=selected_is_timeout,
                            failure_kind=selected_failure_kind,
                        )
                        raise LifecycleExecutionError(
                            "bounded process output evidence could not be written",
                            evidence,
                            is_timeout=selected_is_timeout,
                            failure_kind=selected_failure_kind,
                        ) from selected_primary
                    if primary is not None:
                        remember_failure(
                            frozen_primary, evidence, is_timeout=frozen_is_timeout,
                            failure_kind=frozen_failure_kind,
                        )
                        raise LifecycleExecutionError(
                            "{} HTTP root process did not complete within its work stage".format(self.mode),
                            evidence,
                            is_timeout=frozen_is_timeout,
                            failure_kind=frozen_failure_kind,
                        ) from frozen_primary
                    if process.returncode != 0:
                        error = RuntimeError(
                            "{} official root script exited {}".format(self.mode, process.returncode)
                        )
                        evidence = LifecycleEvidence(
                            **{
                                **evidence.__dict__,
                                "stage": "root_exit_status",
                                "primary_error": "{}: {}".format(type(error).__name__, str(error)[:320]),
                                "exception_chain": exception_chain(error),
                            }
                        )
                        remember_failure(error, evidence, is_timeout=False, failure_kind="root_script_nonzero")
                        raise LifecycleExecutionError(
                            str(error), evidence, failure_kind="root_script_nonzero"
                        ) from error
                    return evidence
                finally:
                    if lock is not None:
                        active_error = sys.exc_info()[1]
                        try:
                            lock.__exit__(
                                type(active_error) if active_error is not None else None,
                                active_error,
                                active_error.__traceback__ if active_error is not None else None,
                            )
                        except BaseException as cleanup_error:
                            prior = current_lifecycle or pre_spawn_evidence(
                                active_error or cleanup_error, attempted=spawn_attempted
                            )
                            cleanup = list(prior.cleanup_errors[:7])
                            cleanup.append(
                                "model_lock_exit:{}:{}".format(
                                    type(cleanup_error).__name__[:64], str(cleanup_error)[:256]
                                )
                            )
                            current_lifecycle = LifecycleEvidence(
                                **{
                                    **prior.__dict__,
                                    "stage": "model_lock_exit",
                                    "cleanup_errors": tuple(cleanup[:8]),
                                }
                            )
                            self.last_lifecycle = current_lifecycle
                            if active_error is not None:
                                current_primary_exception = current_primary_exception or active_error
                                current_is_timeout = current_is_timeout or isinstance(
                                    active_error, (BackendTimeout, TimeoutError)
                                ) or (
                                    isinstance(active_error, LifecycleExecutionError) and active_error.is_timeout
                                )
                                if current_failure_kind is None:
                                    current_failure_kind = (
                                        active_error.failure_kind
                                        if isinstance(active_error, LifecycleExecutionError)
                                        else "backend_pre_spawn_failure"
                                    )
                            if isinstance(active_error, LifecycleExecutionError):
                                raise LifecycleExecutionError(
                                    str(active_error),
                                    current_lifecycle,
                                    is_timeout=current_is_timeout,
                                    failure_kind=current_failure_kind or active_error.failure_kind,
                                ) from active_error
                            if active_error is None:
                                raise
        except LifecycleExecutionError:
            raise
        except BaseException as error:
            if process is None:
                original = current_primary_exception or error.__context__ or error.__cause__ or error
                prior = current_lifecycle
                evidence = prior or pre_spawn_evidence(original, attempted=spawn_attempted)
                cleanup = list(evidence.cleanup_errors[:7])
                if original is not error:
                    cleanup.append(
                        "context_exit:{}:{}".format(type(error).__name__[:64], str(error)[:256])
                    )
                    evidence = LifecycleEvidence(
                        **{
                            **evidence.__dict__,
                            "stage": "pre_spawn_context_exit",
                            "primary_error": evidence.primary_error or "{}: {}".format(
                                type(original).__name__[:96], str(original)[:320]
                            ),
                            "exception_chain": evidence.exception_chain or exception_chain(original),
                            "cleanup_errors": tuple(cleanup[:8]),
                        }
                    )
                self.last_lifecycle = evidence
                raise LifecycleExecutionError(
                    "HTTP backend failed before a returned process handle or while unwinding context",
                    evidence,
                    is_timeout=current_is_timeout or isinstance(original, (BackendTimeout, TimeoutError)),
                    failure_kind=current_failure_kind or (
                        "backend_pre_spawn_failure" if not spawn_attempted else "spawn_handle_unknown"
                    ),
                ) from error
            prior_error = current_primary_exception or error.__context__ or error.__cause__
            evidence = current_lifecycle or (
                prior_error.evidence
                if isinstance(prior_error, LifecycleExecutionError)
                else None
            )
            if evidence is None:
                evidence = LifecycleEvidence(
                    stage="backend_outer_failure",
                    spawn_state="CREATED",
                    identity_state="UNKNOWN",
                    pid=process.pid,
                    stop_requested="UNKNOWN",
                    exit_confirmed="UNKNOWN",
                    primary_error=(
                        "{}: {}".format(type(prior_error).__name__[:96], str(prior_error)[:320])
                        if prior_error is not None
                        else "{}: {}".format(type(error).__name__[:96], str(error)[:320])
                    ),
                    exception_chain=exception_chain(prior_error or error),
                )
            cleanup = list(evidence.cleanup_errors[:7])
            cleanup.append(
                "backend_outer_failure:{}:{}".format(type(error).__name__[:64], str(error)[:256])
            )
            evidence = LifecycleEvidence(
                **{
                    **evidence.__dict__,
                    "stage": "backend_outer_failure",
                    "primary_error": evidence.primary_error or "{}: {}".format(
                        type(prior_error).__name__[:96] if prior_error is not None else type(error).__name__[:96],
                        str(prior_error if prior_error is not None else error)[:320],
                    ),
                    "exception_chain": evidence.exception_chain or exception_chain(prior_error or error),
                    "cleanup_errors": tuple(cleanup[:8]),
                }
            )
            self.last_lifecycle = evidence
            if not evidence.safe_to_read_outputs:
                self._unclosed_process = process
                self._unclosed_identity = self.last_lifecycle_identity
            raise LifecycleExecutionError(
                "HTTP backend failed while unwinding an owned process lifecycle",
                evidence,
                is_timeout=current_is_timeout or (
                    prior_error.is_timeout if isinstance(prior_error, LifecycleExecutionError)
                    else isinstance(prior_error or error, (BackendTimeout, TimeoutError))
                ),
                failure_kind=current_failure_kind or (
                    prior_error.failure_kind if isinstance(prior_error, LifecycleExecutionError)
                    else "backend_outer_failure"
                ),
            ) from error

    @property
    def last_lifecycle_identity(self) -> ProcessIdentity | None:
        return getattr(self, "_last_lifecycle_identity", None)

    def _supervise_http_process(
        self,
        process: subprocess.Popen[bytes],
        *,
        work_deadline: float,
        total_deadline: float,
    ) -> tuple[LifecycleEvidence, bytes | str | None, bytes | str | None, BaseException | None, bool]:
        primary: BaseException | None = None
        is_timeout = False
        stdout: bytes | str | None = None
        stderr: bytes | str | None = None
        cleanup_errors: list[str] = []
        identity: ProcessIdentity
        try:
            identity = bind_spawn_identity(process, cleanup_deadline_at=total_deadline)
        except BaseException as error:
            identity = ProcessIdentity(pid=process.pid, acquisition_state="UNKNOWN")
            cleanup_errors.append("identity_capture:{}:{}".format(type(error).__name__[:64], str(error)[:256]))
            primary = error
        self._last_lifecycle_identity = identity
        pipe_eof = "UNKNOWN"
        stop_outcome = StopOutcome(
            identity_state=identity.acquisition_state,
            stop_requested="UNKNOWN",
            exit_confirmed="UNKNOWN",
            snapshot_complete="UNKNOWN",
            owned_group_quiescent="UNKNOWN",
            primary_error="stop_not_attempted",
        )

        remaining_work = work_deadline - time.monotonic()
        if remaining_work > 0:
            try:
                stdout, stderr = process.communicate(timeout=remaining_work)
                pipe_eof = "CONFIRMED"
                if time.monotonic() >= work_deadline:
                    primary = primary or BackendTimeout("work deadline passed while communicate returned")
                    is_timeout = True
            except subprocess.TimeoutExpired as error:
                primary = primary or error
                is_timeout = True
            except BaseException as error:
                primary = primary or error
        else:
            primary = BackendTimeout("work deadline reached immediately after Popen")
            is_timeout = True

        if identity.bound:
            try:
                stop_outcome = stop_http_owned_group(
                    process,
                    identity,
                    cleanup_deadline_at=total_deadline,
                    term_grace_s=0.75,
                )
            except BaseException as error:
                cleanup_errors.append("stop_http_owned_group:{}:{}".format(type(error).__name__[:64], str(error)[:256]))
                stop_outcome = StopOutcome(
                    identity_state="BOUND",
                    stop_requested="UNKNOWN",
                    exit_confirmed="UNKNOWN",
                    snapshot_complete="UNKNOWN",
                    owned_group_quiescent="UNKNOWN",
                    primary_error="stop_helper_exception",
                )
        else:
            cleanup_errors.extend(identity.acquisition_errors)

        if stop_outcome.owned_group_quiescent == "CONFIRMED_FOR_BOUND_GROUP":
            remaining_total = total_deadline - time.monotonic()
            if process.returncode is None and remaining_total > 0:
                try:
                    process.wait(timeout=remaining_total)
                except subprocess.TimeoutExpired as error:
                    primary = primary or error
                    cleanup_errors.append("wait_deadline_expired")
                except BaseException as error:
                    primary = primary or error
                    cleanup_errors.append("wait:{}:{}".format(type(error).__name__[:64], str(error)[:256]))
            if process.returncode is not None:
                self.last_returncode = process.returncode
                exit_confirmed = "CONFIRMED"
            else:
                exit_confirmed = "UNKNOWN"
                cleanup_errors.append("popen_exit_not_confirmed")
            reap_status = stop_outcome.reap_status
            if reap_status == "PENDING_POPEN_REAP" and exit_confirmed == "CONFIRMED":
                reap_status = "CONFIRMED"
            if time.monotonic() < total_deadline:
                final_snapshot = strict_proc_snapshot(cleanup_deadline_at=total_deadline)
                if not final_snapshot.complete:
                    cleanup_errors.extend(final_snapshot.errors)
                    stop_outcome = StopOutcome(
                        **{
                            **stop_outcome.__dict__,
                            "snapshot_complete": "UNKNOWN",
                            "owned_group_quiescent": "UNKNOWN",
                            "reap_status": "UNKNOWN",
                        }
                    )
                    reap_status = "UNKNOWN"
                elif any(
                    entry.process_group == identity.process_group
                    and entry.session_id == identity.session_id
                    for entry in final_snapshot.entries.values()
                ):
                    cleanup_errors.append("bound_group_has_residual_proc_entries_after_popen_wait")
                    stop_outcome = StopOutcome(
                        **{
                            **stop_outcome.__dict__,
                            "snapshot_complete": "CONFIRMED",
                            "owned_group_quiescent": "UNKNOWN",
                            "reap_status": "UNKNOWN",
                        }
                    )
                    reap_status = "UNKNOWN"
            else:
                cleanup_errors.append("final_proc_snapshot_not_started_deadline_exhausted")
                reap_status = "UNKNOWN"
            if pipe_eof != "CONFIRMED":
                remaining_total = total_deadline - time.monotonic()
                if remaining_total > 0:
                    try:
                        stdout, stderr = process.communicate(timeout=remaining_total)
                        pipe_eof = "CONFIRMED"
                    except subprocess.TimeoutExpired as error:
                        primary = primary or error
                        cleanup_errors.append("pipe_drain_deadline_expired")
                    except BaseException as error:
                        primary = primary or error
                        cleanup_errors.append("pipe_drain:{}:{}".format(type(error).__name__[:64], str(error)[:256]))
                else:
                    cleanup_errors.append("pipe_drain_not_started_deadline_exhausted")
        else:
            exit_confirmed = "CONFIRMED" if process.returncode is not None else "UNKNOWN"
            reap_status = "UNKNOWN"
            if pipe_eof == "CONFIRMED":
                cleanup_errors.append("pipe_eof_confirmed_but_owned_group_not_confirmed")

        pidfd_errors = close_spawn_identity(identity)
        cleanup_errors.extend(pidfd_errors)
        cleanup_errors.extend(stop_outcome.cleanup_errors)
        if stop_outcome.primary_error not in (None, "stop_not_attempted"):
            cleanup_errors.append("stop_outcome:{}".format(stop_outcome.primary_error[:320]))
        process_streams_closed = all(
            stream is None or bool(getattr(stream, "closed", False))
            for stream in (process.stdout, process.stderr)
        )
        if pidfd_errors or any("pidfd_close" in error for error in cleanup_errors):
            resources_closed = "FAILED"
        elif pipe_eof == "CONFIRMED" and process_streams_closed:
            resources_closed = "CONFIRMED"
        else:
            resources_closed = "UNKNOWN"
        group_quiet = stop_outcome.owned_group_quiescent
        writer_quiet = (
            "CONFIRMED_FOR_BOUND_GROUP"
            if (
                identity.bound
                and exit_confirmed == "CONFIRMED"
                and stop_outcome.snapshot_complete == "CONFIRMED"
                and group_quiet == "CONFIRMED_FOR_BOUND_GROUP"
                and pipe_eof == "CONFIRMED"
                and resources_closed == "CONFIRMED"
            )
            else "UNKNOWN"
        )
        evidence = LifecycleEvidence(
            stage="process_supervision",
            spawn_state="CREATED",
            identity_state=identity.acquisition_state,
            pid=identity.pid,
            process_returncode=process.returncode,
            process_group=identity.process_group,
            session_id=identity.session_id,
            start_ticks=identity.start_ticks,
            stop_requested=stop_outcome.stop_requested,
            term_signals_sent=stop_outcome.term_signals_sent,
            kill_signals_sent=stop_outcome.kill_signals_sent,
            vanished_during_scan=stop_outcome.vanished_during_scan,
            last_live_member_count=stop_outcome.last_live_member_count,
            stop_outcome_error=stop_outcome.primary_error,
            exit_confirmed=exit_confirmed,
            process_snapshot_complete=stop_outcome.snapshot_complete,
            owned_group_quiescent=group_quiet,
            reap_status=reap_status,
            pipe_eof=pipe_eof,
            process_resources_closed=resources_closed,
            external_writer_scope="UNKNOWN_ESCAPED_OR_EXTERNAL_UNVERIFIED",
            cleanup_deadline_status=(
                "WITHIN_DEADLINE" if time.monotonic() <= total_deadline else "EXCEEDED"
            ),
            evidence_sealed="NOT_YET_SEALED",
            workspace_removed="NOT_YET_REMOVED",
            primary_error=("{}: {}".format(type(primary).__name__[:96], str(primary)[:320]) if primary else None),
            exception_chain=exception_chain(primary),
            cleanup_errors=tuple(cleanup_errors[:8]),
        )
        if not evidence.safe_to_read_outputs:
            self._unclosed_process = process
            self._unclosed_identity = identity
        return evidence, stdout, stderr, primary, is_timeout


def _decode(value: bytes | str) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _effective_request_budget(config: RuntimeConfig) -> int:
    budget = config.request_budget_s
    official = os.environ.get("AGENT_DEADLINE_S")
    if official is not None:
        try:
            official_budget = int(official)
        except ValueError as error:
            raise ValueError("AGENT_DEADLINE_S must be a positive integer") from error
        if official_budget <= 0:
            raise ValueError("AGENT_DEADLINE_S must be a positive integer")
        budget = min(budget, official_budget)
    return budget


def _probe_model(base_url: str, timeout_s: float = 2.0) -> dict[str, Any]:
    request = urllib.request.Request(base_url.rstrip("/") + "/models", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read()
        payload = json.loads(raw.decode("utf-8"))
        models = payload.get("data") if isinstance(payload, dict) else None
        model_id = models[0].get("id") if isinstance(models, list) and models and isinstance(models[0], dict) else None
        return {"reachable": True, "model": model_id, "response_sha256": _sha(raw)}
    except Exception as error:
        return {"reachable": False, "model": None, "error": "{}: {}".format(type(error).__name__, str(error)[:250])}


def _sha(raw: bytes) -> str:
    import hashlib

    return hashlib.sha256(raw).hexdigest()


def _read_vram() -> tuple[int | None, int | None, str]:
    executable = shutil.which("rocm-smi")
    if not executable:
        return None, None, "rocm-smi-unavailable"
    try:
        result = subprocess.run(
            [executable, "--showmeminfo", "vram", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None, None, "rocm-smi-exit-{}".format(result.returncode)
        payload = json.loads(result.stdout)
        cards = [value for value in payload.values() if isinstance(value, dict)] if isinstance(payload, dict) else []
        if len(cards) != 1:
            return None, None, "vram-device-count-unknown-or-not-single-card"
        card = cards[0]
        used = card.get("VRAM Total Used Memory (B)")
        total = card.get("VRAM Total Memory (B)")
        if not isinstance(used, (str, int)) or not isinstance(total, (str, int)):
            return None, None, "vram-byte-fields-unavailable"
        used_text, total_text = str(used).strip(), str(total).strip()
        if not used_text.isdigit() or not total_text.isdigit():
            return None, None, "vram-byte-fields-invalid"
        used_bytes, total_bytes = int(used_text), int(total_text)
        if used_bytes < 0 or total_bytes <= 0 or used_bytes > total_bytes:
            return None, None, "vram-byte-fields-out-of-range"
        return used_bytes, total_bytes, "rocm-smi-json-explicit-bytes"
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError) as error:
        return None, None, "vram-read-error:{}".format(type(error).__name__)


def _health_state(config: RuntimeConfig, *, model_probe: dict[str, Any] | None = None, vram: tuple[int | None, int | None, str] | None = None) -> dict[str, Any]:
    probe = model_probe if model_probe is not None else _probe_model(config.model_base_url)
    used_bytes, total_bytes, vram_source = vram if vram is not None else _read_vram()
    actual_model = probe.get("model") if probe.get("reachable") else None
    model_verified = isinstance(actual_model, str) and bool(actual_model)
    vram_verified = used_bytes is not None and total_bytes is not None
    runtime = inspect_runtime_attestation(Path(os.environ.get("RTL_MODEL_LED_ASSETS", str(PACKAGE_ROOT / "serve" / "runtime_assets.json"))))
    ready = bool(
        probe.get("reachable")
        and model_verified
        and actual_model == config.model_name
        and vram_verified
        and runtime.get("ready") is True
    )
    return {
        "ready": ready,
        "model": actual_model,
        "model_identity_status": "verified_alias" if model_verified and actual_model == config.model_name else "unknown_or_mismatch",
        "vram_used_bytes": used_bytes,
        "vram_total_bytes": total_bytes,
        "vram_gb": round(used_bytes / (1024 ** 3), 3) if used_bytes is not None else None,
        "vram_source": vram_source,
        "memory_compliance": "not_established_by_current_health_sample",
        "runtime_identity": runtime,
    }


def build_entry(*, token: str | None, config: RuntimeConfig, config_path: Path) -> OfficialEntry:
    enable_child_subreaper()
    agent = RootScriptBackend("agent", config, config_path)
    baseline = RootScriptBackend("baseline", config, config_path)
    package_prompt = resolve_package_path(config.prompt_path, package_root=PACKAGE_ROOT)
    baseline_state = verify_copies(PACKAGE_ROOT)
    try:
        runtime_assets = json.loads(
            Path(os.environ.get("RTL_MODEL_LED_ASSETS", str(PACKAGE_ROOT / "serve" / "runtime_assets.json"))).read_text(encoding="utf-8")
        )
        route_config_ok = (
            isinstance(runtime_assets, dict)
            and config.model_base_url.rstrip("/") == "http://127.0.0.1:18080/v1"
            and config.model_name == runtime_assets.get("llama_cpp", {}).get("alias")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        route_config_ok = False
    config_ok = package_prompt.is_file() and baseline_state.get("all_match") is True and route_config_ok
    evidence_root = resolve_package_path(config.evidence_root, package_root=PACKAGE_ROOT)

    def health() -> dict[str, Any]:
        state = _health_state(config)
        state["ready"] = bool(state["ready"] and config_ok)
        state["config_ok"] = config_ok
        state["baseline_identity"] = baseline_state
        evidence_root.mkdir(parents=True, exist_ok=True)
        _atomic_text(evidence_root / "health_last.json", json.dumps({"ts": time.time(), **state}, ensure_ascii=False, sort_keys=True) + "\n")
        return state

    effective_budget = _effective_request_budget(config)
    entry = OfficialEntry(
        token=token,
        model_name=config.model_name,
        agent_backend=agent,
        baseline_backend=baseline,
        health_provider=health,
        evidence_root=evidence_root / "requests",
        max_request_budget_s=effective_budget,
        archive_reserve_s=config.archive_reserve_s,
    )
    entry.configure_job_manager(
        JobManager(
            entry._execute_job_record,
            clock=entry._clock,
            total_result_bytes=config.job_total_result_bytes,
        )
    )
    return entry


class EntryHTTPServer(ThreadingHTTPServer):
    request_budget_s: int

    def __init__(self, address: tuple[str, int], entry: OfficialEntry, request_budget_s: int) -> None:
        super().__init__(address, EntryRequestHandler)
        self.entry = entry
        self.request_budget_s = request_budget_s
        self.solve_lock = threading.Lock()
        self.entry.set_execution_lock(self.solve_lock)
        if not self.entry.start_job_manager():
            self.server_close()
            raise RuntimeError("asynchronous job worker could not be started")


class EntryRequestHandler(BaseHTTPRequestHandler):
    server: EntryHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        started = time.monotonic()
        received_wall_time = datetime.now(timezone.utc).isoformat()
        self._dispatch(b"", started, received_wall_time)

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        received_wall_time = datetime.now(timezone.utc).isoformat()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write(400, {"detail": "invalid Content-Length"})
            return
        if length < 0 or length > 8 * 1024 * 1024:
            self._write(413, {"detail": "request too large"})
            return
        remaining_to_read = max(0.001, self.server.request_budget_s - (time.monotonic() - started))
        self.connection.settimeout(float(remaining_to_read))
        try:
            body = self.rfile.read(length)
        except (TimeoutError, OSError):
            self._write(408, {"detail": "request body read exceeded budget"})
            return
        if len(body) != length:
            self._write(400, {"detail": "incomplete request body"})
            return
        self._dispatch(body, started, received_wall_time)

    def _dispatch(self, body: bytes, started: float, received_wall_time: str) -> None:
        headers = {key: value for key, value in self.headers.items()}
        response = self.server.entry.dispatch(
            self.command,
            self.path,
            headers,
            body,
            request_started_at=started,
            request_received_wall_time=received_wall_time,
        )
        self._write(response.status, response.body)

    def _write(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            return

    def log_message(self, format: str, *args: object) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = RuntimeConfig.load(args.config)
    token = os.environ.get("FPGACHINA_TOKEN")
    entry = build_entry(token=token, config=config, config_path=args.config.absolute())
    from .api_lifecycle import run
    run(entry, config, EntryHTTPServer, EntryRequestHandler, _effective_request_budget(config))


if __name__ == "__main__":
    main()
