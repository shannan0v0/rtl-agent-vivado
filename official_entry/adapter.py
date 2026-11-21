"""In-process HTTP contract, request accounting, and evidence archive layer.

The worker backend is injected by ``server.py``. This adapter itself does not
contact the model or contain design-generation rules.
"""

from __future__ import annotations

import json
import hashlib
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from .lifecycle_types import LifecycleEvidence, LifecycleExecutionError, exception_chain
from .job_manager import DEFAULT_PER_RESULT_BYTES, JobExecutionResult, JobManager, JobRecord


class BackendTimeout(Exception):
    """A task-dir backend stopped at its own deadline."""


class RequestDeadlineExceeded(TimeoutError):
    """A new HTTP-path operation was refused after the shared deadline."""


@dataclass(frozen=True)
class TaskRequest:
    task_id: str
    nonce: str
    mode: str
    prompt: str
    interface: str
    deadline_s: int
    deadline_at: float = 0.0
    received_monotonic: float | None = None
    total_deadline_at: float | None = None
    work_deadline_at: float | None = None
    job_id: str = ""
    request_fingerprint: str = ""
    archive_id: str = ""


@dataclass(frozen=True)
class EntryResponse:
    status: int
    body: dict[str, Any]


class TaskBackend(Protocol):
    def run(
        self, request: TaskRequest, task_dir: Path, out_dir: Path, *, timeout_s: float
    ) -> LifecycleEvidence | None:
        """Read task_dir and atomically write solution.v/trace.jsonl to out_dir."""


HealthProvider = Callable[[], Mapping[str, Any]]
AuditSink = Callable[[Mapping[str, Any]], None]


class OfficialEntry:
    """Dispatch authenticated requests through one injected task-dir backend."""

    def __init__(
        self,
        *,
        token: str | None,
        model_name: str | None = None,
        scratch: str | Path | None = None,
        agent_backend: TaskBackend | None = None,
        baseline_backend: TaskBackend | None = None,
        health_provider: HealthProvider | None = None,
        audit_sink: AuditSink | None = None,
        evidence_root: str | Path | None = None,
        max_request_budget_s: int = 360,
        archive_reserve_s: float = 5.0,
        job_manager: JobManager | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = token or ""
        self._model_name = model_name or "unknown"
        self._scratch = Path(scratch) if scratch is not None else None
        self._backends = {"agent": agent_backend, "baseline": baseline_backend}
        self._health_provider = health_provider
        self._audit_sink = audit_sink
        self._evidence_root = Path(evidence_root) if evidence_root is not None else None
        self._max_request_budget_s = max_request_budget_s
        self._archive_reserve_s = archive_reserve_s
        self._job_manager = job_manager
        self._execution_lock: Any = None
        self._clock = monotonic_clock
        self.audit_records: list[dict[str, Any]] = []

    def configure_job_manager(self, manager: JobManager) -> None:
        self._job_manager = manager

    def set_execution_lock(self, lock: Any) -> None:
        self._execution_lock = lock

    def start_job_manager(self) -> bool:
        return bool(self._job_manager and self._job_manager.start())

    def close_job_manager(self) -> dict[str, Any]:
        if self._job_manager is None:
            return {"state": "not_configured", "close_incomplete": False}
        return self._job_manager.close()

    def dispatch(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None,
        body: bytes = b"",
        request_started_at: float | None = None,
        request_received_wall_time: str | None = None,
    ) -> EntryResponse:
        method = method.upper()
        route = urlsplit(path).path
        if route == "/v1/health":
            if method != "GET":
                return self._error(405, "method not allowed")
            auth_error = self._check_auth(headers)
            return auth_error or self._health()
        if route == "/v1/solve":
            if method != "POST":
                return self._error(405, "method not allowed")
            auth_error = self._check_auth(headers)
            if auth_error is not None:
                return auth_error
            if self._job_manager is None:
                return self._solve(headers, body, request_started_at=request_started_at)
            return self._submit_async(
                headers,
                body,
                request_started_at=request_started_at,
                request_received_wall_time=request_received_wall_time,
            )
        if route.startswith("/v1/jobs/"):
            if method != "GET":
                return self._error(405, "method not allowed")
            auth_error = self._check_auth(headers)
            if auth_error is not None:
                return auth_error
            job_id = route[len("/v1/jobs/"):]
            if not job_id or "/" in job_id or self._job_manager is None:
                return self._error(404, "not found")
            snapshot = self._job_manager.get_snapshot(job_id)
            if snapshot is None:
                return self._error(404, "not found")
            status_code = 503 if snapshot.get("status") in ("failed", "expired") else 200
            return EntryResponse(status_code, snapshot)
        return self._error(404, "not found")

    def _check_auth(self, headers: Mapping[str, str] | None) -> EntryResponse | None:
        if not self._token:
            return self._error(500, "service token is not configured")
        normalized = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        if normalized.get("authorization") != f"Bearer {self._token}":
            return self._error(401, "bad token")
        return None

    def _health(self) -> EntryResponse:
        if self._health_provider is None:
            state: Mapping[str, Any] = {
                "ready": False,
                "model": None,
                "vram_gb": None,
            }
        else:
            try:
                state = self._health_provider()
            except Exception:
                return self._error(503, "health state unavailable")
        manager_ready = self._job_manager is None or self._job_manager.is_healthy()
        return EntryResponse(
            200,
            {
                "ready": bool(state.get("ready", False)) and manager_ready,
                "track": "rtl",
                "model": state.get("model"),
                "vram_gb": state.get("vram_gb"),
            },
        )

    def _submit_async(
        self,
        headers: Mapping[str, str] | None,
        body: bytes,
        *,
        request_started_at: float | None = None,
        request_received_wall_time: str | None = None,
    ) -> EntryResponse:
        if len(body) > 8 * 1024 * 1024:
            return self._error(413, "request too large")
        content_type = next(
            (str(value) for key, value in (headers or {}).items() if str(key).lower() == "content-type"),
            "",
        )
        if content_type and content_type.split(";", 1)[0].strip().lower() != "application/json":
            return self._error(415, "content type must be application/json")
        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(400, "invalid JSON")
        if not isinstance(raw, dict):
            return self._error(400, "request must be a JSON object")
        try:
            parsed = self._parse_request(raw)
        except ValueError as error:
            return self._error(400, str(error))
        if not parsed.nonce:
            return self._error(400, "nonce must be a non-empty string")
        if len(parsed.nonce) > 256 or len(parsed.task_id) > 256:
            return self._error(400, "nonce and task_id must be at most 256 characters")
        received = request_started_at if request_started_at is not None else self._clock()
        effective_s = min(parsed.deadline_s, self._max_request_budget_s)
        total_deadline = received + effective_s
        work_deadline = total_deadline - max(0.0, self._archive_reserve_s)
        principal = hashlib.sha256(self._token.encode("utf-8")).hexdigest()
        fingerprint_payload = {
            "task_id": parsed.task_id,
            "mode": parsed.mode,
            "prompt": parsed.prompt,
            "interface": parsed.interface,
            "deadline_s": parsed.deadline_s,
        }
        fingerprint_raw = json.dumps(
            fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fingerprint = hashlib.sha256(fingerprint_raw).hexdigest()
        queued_request = replace(
            parsed,
            deadline_s=effective_s,
            deadline_at=total_deadline,
            received_monotonic=received,
            total_deadline_at=total_deadline,
            work_deadline_at=work_deadline,
            request_fingerprint=fingerprint,
        )
        submission = self._job_manager.submit(
            principal_fingerprint=principal,
            nonce=parsed.nonce,
            request_fingerprint=fingerprint,
            request=queued_request,
            request_body=body,
            task_id=parsed.task_id,
            mode=parsed.mode,
            received_monotonic=received,
            received_wall_time=request_received_wall_time or datetime.now(timezone.utc).isoformat(),
            requested_deadline_s=parsed.deadline_s,
            max_deadline_s=self._max_request_budget_s,
            archive_reserve_s=self._archive_reserve_s,
        )
        return EntryResponse(submission.http_status, dict(submission.body))

    def _unclosed_lifecycle_result(
        self,
        record: JobRecord,
        work: Path | None,
        lifecycle: LifecycleEvidence,
        *,
        error_code: str,
        result_category: str = "backend_failure",
        archive_complete: bool = False,
    ) -> JobExecutionResult:
        """Write only a bounded control receipt and leave uncertain work in place."""
        admission_before_release = record.result_admission_state
        release_attempted_before = record.result_release_attempted
        release_returned = None
        release_outcome = "NO_RESERVATION"
        if self._job_manager is not None:
            try:
                release_returned = self._job_manager.release_result_admission(record)
                if record.result_release_outcome == "UNKNOWN":
                    release_outcome = "UNKNOWN"
                elif admission_before_release != "RESERVED":
                    release_outcome = "NO_RESERVATION"
                elif release_returned and record.result_admission_state == "RELEASED":
                    release_outcome = "RELEASED"
                else:
                    release_outcome = "UNKNOWN"
                    record.result_release_outcome = "UNKNOWN"
                    record.result_release_error = "release False/completion unconfirmed for pre-call RESERVED"
                    record.result_release_failure_kind = "RETURNED_FALSE" if not release_returned else "STATE_UNCONFIRMED"
            except Exception as release_error:
                try:
                    message = str(release_error)[:256]
                except Exception:
                    message = "<unprintable>"
                release_outcome = "UNKNOWN"
                record.result_release_outcome = "UNKNOWN"
                record.result_release_state_before = admission_before_release
                record.result_release_state_after = record.result_admission_state
                record.result_release_error = "{}:{}".format(type(release_error).__name__[:64], message)
                record.result_release_failure_kind = "EXCEPTION"
        elif admission_before_release == "RESERVED":
            release_outcome = "UNKNOWN"
            record.result_release_outcome = "UNKNOWN"
            record.result_release_error = "RESERVED record without its Manager; no release attempted"
            record.result_release_failure_kind = "MANAGER_UNAVAILABLE"
        release_evidence = {
            "outcome": release_outcome,
            "state_before": admission_before_release[:64],
            "state_after": record.result_admission_state[:64],
            "raw_attempted_ever": record.result_release_attempted,
            "raw_attempted_now": not release_attempted_before and record.result_release_attempted,
            "release_api_returned": release_returned,
            "owner_confirmed": record.result_release_owner_confirmed,
            "responsibility_job_id": record.job_id[:128],
            "reserved_bytes": record.result_admission_bytes,
            "error": record.result_release_error,
            "failure_kind": record.result_release_failure_kind,
        }
        if release_outcome == "UNKNOWN":
            cleanup = list(lifecycle.cleanup_errors[:8])
            if len(cleanup) < 8:
                cleanup.append("result_reservation_release:{}".format((record.result_release_error or "UNKNOWN")[:321]))
            else:
                release_evidence["cleanup_error_list_full"] = True
            lifecycle = replace(lifecycle, cleanup_errors=tuple(cleanup))
        pending = list(lifecycle.pending_paths[:3])
        if work is not None:
            pending.append(str(work))
        workspace_state = lifecycle.workspace_removed
        if workspace_state in ("UNKNOWN", "NOT_YET_REMOVED"):
            workspace_state = "NOT_ATTEMPTED_UNCLOSED"
        lifecycle = replace(
            lifecycle,
            stage=(lifecycle.stage or "UNKNOWN")[:96],
            evidence_sealed="CONTROL_RECEIPT_PENDING",
            workspace_removed=workspace_state,
            pending_paths=tuple(pending[:4]),
        )
        receipt_path = self._evidence_root / "_lifecycle_unclosed" / (record.archive_id + ".json")
        receipt = {
            "job_id": record.job_id[:128],
            "task_id": record.task_id[:128],
            "archive_id": record.archive_id[:128],
            "status": "lifecycle_unclosed",
            "logical_result_status": result_category[:64],
            "public_status": "expired" if result_category == "expired" else "failed",
            "error_code": error_code[:128],
            "archive_complete": bool(archive_complete),
            "manager_result_admission_state": record.result_admission_state,
            "manager_result_admission_error_code": record.result_admission_error_code,
            "manager_result_admission_bytes": record.result_admission_bytes,
            "reservation_release": release_evidence,
            "receipt_truncated": False,
            "lifecycle": lifecycle.to_record(),
        }
        receipt_truncated = False
        try:
            raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(raw) + 1 > 8192:
                receipt_truncated = True
                receipt["receipt_truncated"] = True
                receipt["lifecycle"]["exception_chain"] = []
                receipt["lifecycle"]["cleanup_errors"] = []
                receipt["lifecycle"]["pending_paths"] = [item[:256] for item in receipt["lifecycle"]["pending_paths"]]
                receipt["lifecycle"]["primary_error"] = (receipt["lifecycle"].get("primary_error") or "")[:256]
                receipt["lifecycle"]["stop_outcome_error"] = (receipt["lifecycle"].get("stop_outcome_error") or "")[:256]
                raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(raw) + 1 > 8192:
                minimal = {
                    "job_id": record.job_id[:64],
                    "task_id": record.task_id[:64],
                    "archive_id": record.archive_id[:64],
                    "status": "lifecycle_unclosed",
                    "logical_result_status": result_category[:32],
                    "public_status": "expired" if result_category == "expired" else "failed",
                    "error_code": error_code[:96],
                    "archive_complete": bool(archive_complete),
                    "manager_result_admission_state": record.result_admission_state[:32],
                    "manager_result_admission_error_code": (record.result_admission_error_code or "")[:96],
                    "manager_result_admission_bytes": record.result_admission_bytes,
                    "reservation_release": {
                        "outcome": release_outcome,
                        "state_before": admission_before_release[:32],
                        "state_after": record.result_admission_state[:32],
                        "raw_attempted_ever": record.result_release_attempted,
                        "raw_attempted_now": not release_attempted_before and record.result_release_attempted,
                        "owner_confirmed": record.result_release_owner_confirmed,
                        "error": (record.result_release_error or "")[:128],
                        "failure_kind": (record.result_release_failure_kind or "")[:32],
                    },
                    "receipt_truncated": True,
                    "lifecycle": {
                        "stage": lifecycle.stage[:48],
                        "primary_error": (lifecycle.primary_error or "")[:96],
                        "cleanup_errors": [item[:96] for item in lifecycle.cleanup_errors[:1]],
                        "workspace_removed": lifecycle.workspace_removed[:32],
                        "pending_paths": [item[:96] for item in lifecycle.pending_paths[:1]],
                        "evidence_sealed": "CONTROL_RECEIPT_ONLY",
                    },
                }
                receipt = minimal
                raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            final_bytes = raw + b"\n"
            if len(final_bytes) > 8192:
                raise ValueError("bounded lifecycle control receipt exceeds 8192 bytes including newline")
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write(receipt_path, final_bytes)
            lifecycle = replace(lifecycle, evidence_sealed="CONTROL_RECEIPT_ONLY")
        except Exception as error:
            cleanup = list(lifecycle.cleanup_errors[:7])
            cleanup.append("control_receipt_write:{}:{}".format(type(error).__name__[:64], str(error)[:256]))
            lifecycle = replace(
                lifecycle,
                evidence_sealed="UNKNOWN",
                cleanup_errors=tuple(cleanup[:8]),
            )
        self.audit_records.append(
            {
                "job_id": record.job_id,
                "task_id": record.task_id,
                "archive_id": record.archive_id,
                "status": result_category,
                "error_code": error_code,
                "archive_complete": bool(archive_complete),
                "receipt_truncated": receipt_truncated,
                "manager_result_admission_state": record.result_admission_state,
                "manager_result_admission_error_code": record.result_admission_error_code,
                "manager_result_admission_bytes": record.result_admission_bytes,
                "reservation_release": release_evidence,
                "lifecycle": lifecycle.to_record(),
                "pending_paths": list(lifecycle.pending_paths),
            }
        )
        return JobExecutionResult(
            result_category,
            record.archive_id,
            error_code=error_code,
            archive_complete=bool(archive_complete),
            lifecycle=lifecycle,
        )

    def _execute_job_record(self, record: JobRecord) -> JobExecutionResult:
        """Run one accepted job, archive its own evidence, and classify outcomes."""
        started = record.received_monotonic
        request = replace(
            record.request,
            deadline_s=record.requested_deadline_s,
            deadline_at=record.total_deadline_at,
            received_monotonic=record.received_monotonic,
            total_deadline_at=record.total_deadline_at,
            work_deadline_at=record.work_deadline_at,
            job_id=record.job_id,
            request_fingerprint=record.request_fingerprint,
            archive_id=record.archive_id,
        )
        backend = self._backends.get(record.mode)
        if self._evidence_root is None:
            return JobExecutionResult("archive_failure", record.archive_id, error_code="archive_unconfigured")
        work: Path | None = None
        task_dir: Path | None = None
        out_dir: Path | None = None
        lock_acquired = False
        archive_started = False
        backend_called = False
        category = "backend_failure"
        error_code = "backend_failure"
        failure_stage: str | None = None
        failure_type: str | None = None
        solution = ""
        trace = ""
        trace_valid = False
        output_status = "missing_output"
        lifecycle = LifecycleEvidence(
            stage="before_backend",
            spawn_state="NOT_ATTEMPTED",
            identity_state="NOT_APPLICABLE",
            stop_requested="NOT_NEEDED",
            exit_confirmed="NOT_APPLICABLE",
            process_snapshot_complete="NOT_APPLICABLE",
            owned_group_quiescent="CONFIRMED_NO_POPEN_ATTEMPT",
            reap_status="NOT_APPLICABLE",
            pipe_eof="NOT_APPLICABLE",
            process_resources_closed="CONFIRMED",
            external_writer_scope="NOT_APPLICABLE",
            cleanup_deadline_status="NOT_APPLICABLE",
            evidence_sealed="NOT_YET_SEALED",
            workspace_removed="NOT_YET_REMOVED",
        )
        try:
            self._require_before_deadline(request.total_deadline_at, "scratch directory creation")
            if self._scratch is not None:
                self._scratch.mkdir(parents=True, exist_ok=True)
            self._require_before_deadline(request.total_deadline_at, "job workspace creation")
            work = Path(tempfile.mkdtemp(dir=self._scratch, prefix="official-entry-job-"))
            task_dir = work / "task"
            out_dir = work / "out"
            self._require_before_deadline(request.total_deadline_at, "task directory creation")
            task_dir.mkdir()
            self._require_before_deadline(request.total_deadline_at, "output directory creation")
            out_dir.mkdir()
            self._require_before_deadline(request.total_deadline_at, "prompt input write")
            self._write_exact(
                task_dir / "prompt.txt", request.prompt,
                deadline_at=request.total_deadline_at, clock=self._clock,
            )
            if request.interface:
                self._require_before_deadline(request.total_deadline_at, "interface input write")
                self._write_exact(
                    task_dir / "interface.txt", request.interface,
                    deadline_at=request.total_deadline_at, clock=self._clock,
                )
            remaining = request.work_deadline_at - self._clock()
            if backend is None:
                category, error_code = "backend_failure", "backend_unconfigured"
            elif remaining <= 0:
                category, error_code = "expired", "deadline_expired_before_dispatch"
            else:
                if self._execution_lock is not None:
                    lock_acquired = self._execution_lock.acquire(timeout=remaining)
                    if not lock_acquired:
                        category, error_code = "expired", "deadline_expired_waiting_for_worker_slot"
                if category != "expired":
                    remaining = request.work_deadline_at - self._clock()
                    if remaining <= 0:
                        category, error_code = "expired", "deadline_expired_before_backend"
                    else:
                        backend_called = True
                        failure_stage = "backend"
                        try:
                            backend_result = backend.run(request, task_dir, out_dir, timeout_s=remaining)
                            if isinstance(backend_result, LifecycleEvidence):
                                lifecycle = backend_result
                            else:
                                lifecycle = LifecycleEvidence(
                                    stage="backend_result_missing_lifecycle",
                                    spawn_state="UNKNOWN",
                                    identity_state="UNKNOWN",
                                    primary_error="backend returned no explicit lifecycle evidence",
                                )
                                return self._unclosed_lifecycle_result(
                                    record, work, lifecycle, error_code="lifecycle_result_missing"
                                )
                            if not lifecycle.safe_to_read_outputs:
                                return self._unclosed_lifecycle_result(
                                    record, work, lifecycle, error_code="lifecycle_unconfirmed"
                                )
                            if self._clock() >= request.total_deadline_at:
                                lifecycle = replace(
                                    lifecycle,
                                    stage="deadline_before_output_read",
                                    cleanup_deadline_status="EXCEEDED",
                                )
                                return self._unclosed_lifecycle_result(
                                    record, work, lifecycle, error_code="total_deadline_before_output_read"
                                )
                            failure_stage = "output_validation"
                            solution, trace, output_status, trace_valid = self._read_outputs(
                                out_dir, mode=record.mode, max_result_bytes=DEFAULT_PER_RESULT_BYTES,
                                deadline_at=request.total_deadline_at, clock=self._clock,
                            )
                            if output_status in ("success", "empty_solution") and trace_valid:
                                category, error_code = "normal", ""
                            elif output_status == "budget_output" and trace_valid:
                                category, error_code = "budget_stopped", "work_deadline_reached"
                            elif output_status == "result_too_large":
                                category, error_code = "invalid_output", "result_too_large"
                            elif output_status.startswith("producer_failure_"):
                                category, error_code = "backend_failure", output_status
                            else:
                                category, error_code = "invalid_output", output_status
                        except LifecycleExecutionError as backend_error:
                            lifecycle = backend_error.evidence
                            failure_type = type(backend_error).__name__[:96]
                            if not lifecycle.safe_to_read_outputs:
                                return self._unclosed_lifecycle_result(
                                    record, work, lifecycle, error_code="lifecycle_unconfirmed"
                                )
                            if self._clock() >= request.total_deadline_at:
                                lifecycle = replace(
                                    lifecycle,
                                    stage="deadline_before_output_read",
                                    cleanup_deadline_status="EXCEEDED",
                                )
                                return self._unclosed_lifecycle_result(
                                    record, work, lifecycle, error_code="total_deadline_before_output_read"
                                )
                            solution, trace, output_status, trace_valid = self._read_outputs(
                                out_dir, mode=record.mode, max_result_bytes=DEFAULT_PER_RESULT_BYTES,
                                deadline_at=request.total_deadline_at, clock=self._clock,
                            )
                            if backend_error.is_timeout:
                                if output_status in ("success", "empty_solution", "budget_output") and trace_valid:
                                    category, error_code = "budget_stopped", "work_deadline_reached"
                                elif output_status == "result_too_large":
                                    category, error_code = "invalid_output", "result_too_large"
                                elif output_status.startswith("producer_failure_"):
                                    category, error_code = "backend_failure", output_status
                                else:
                                    category, error_code = "invalid_output", "timeout_" + output_status
                            else:
                                category, error_code = "backend_failure", backend_error.failure_kind
                        except RequestDeadlineExceeded as deadline_error:
                            lifecycle = replace(
                                lifecycle,
                                stage="deadline_during_output_read",
                                cleanup_deadline_status="EXCEEDED",
                                primary_error=lifecycle.primary_error or "RequestDeadlineExceeded: {}".format(
                                    str(deadline_error)[:256]
                                ),
                                exception_chain=lifecycle.exception_chain or exception_chain(deadline_error),
                            )
                            return self._unclosed_lifecycle_result(
                                record, work, lifecycle,
                                error_code="total_deadline_during_output_read",
                                result_category=category,
                            )
                        except (BackendTimeout, TimeoutError) as backend_error:
                            lifecycle = LifecycleEvidence(
                                stage="legacy_backend_timeout_without_lifecycle",
                                spawn_state="UNKNOWN",
                                identity_state="UNKNOWN",
                                primary_error="{}: {}".format(type(backend_error).__name__, str(backend_error)[:320]),
                                exception_chain=exception_chain(backend_error),
                            )
                            return self._unclosed_lifecycle_result(
                                record, work, lifecycle, error_code="legacy_backend_timeout_lifecycle_unknown"
                            )
                        except Exception as backend_error:
                            failure_type = type(backend_error).__name__[:96]
                            lifecycle = LifecycleEvidence(
                                stage="legacy_backend_exception_without_lifecycle",
                                spawn_state="UNKNOWN",
                                identity_state="UNKNOWN",
                                primary_error="{}: {}".format(type(backend_error).__name__[:96], str(backend_error)[:320]),
                                exception_chain=exception_chain(backend_error),
                            )
                            return self._unclosed_lifecycle_result(
                                record, work, lifecycle, error_code="legacy_backend_lifecycle_unknown"
                            )
            frozen_elapsed = round(max(0.0, self._clock() - started), 6)
            frozen_result_valid = category in ("normal", "budget_stopped")
            if frozen_result_valid:
                final_success_response: dict[str, Any] = {
                    "job_id": record.job_id,
                    "task_id": record.task_id,
                    "status": "done",
                    "solution": solution,
                    "trace": trace,
                    "elapsed_s": frozen_elapsed,
                }
                # Fail closed until the resource cleanup decision is persisted.
                fallback_response: dict[str, Any] = {
                    "job_id": record.job_id,
                    "task_id": record.task_id,
                    "status": "failed",
                    "error_code": "cleanup_finalization_unconfirmed",
                }
            else:
                final_success_response = {
                    "job_id": record.job_id,
                    "task_id": record.task_id,
                    "status": "expired" if category == "expired" else "failed",
                    "error_code": error_code,
                }
                fallback_response = dict(final_success_response)
            archive_record = {
                "job_id": record.job_id,
                "task_id": record.task_id,
                "nonce": record.nonce,
                "mode": record.mode,
                "request_fingerprint": record.request_fingerprint,
                "received_monotonic": record.received_monotonic,
                "received_wall_time": record.received_wall_time,
                "total_deadline_at": record.total_deadline_at,
                "work_deadline_at": record.work_deadline_at,
                "archive_id": record.archive_id,
                "logical_result_status": category,
                "logical_error_code": error_code or None,
                "public_status": fallback_response["status"],
                "public_error_code": fallback_response.get("error_code"),
                "resource_cleanup_status": "NOT_CONFIRMED_AT_ARCHIVE_WRITE",
                "workspace_removed": "NOT_CONFIRMED_AT_ARCHIVE_WRITE",
                "backend_called": backend_called,
                "solution_nonempty": bool(solution),
                "trace_valid": trace_valid,
                "output_status": output_status,
                "execution_observed_elapsed_s": frozen_elapsed,
                "archive_complete": False,
                "lifecycle": lifecycle.to_record(),
            }
            if failure_stage is not None and category not in ("normal", "budget_stopped"):
                archive_record["failure_stage"] = failure_stage
            if failure_type is not None:
                archive_record["failure_type"] = failure_type
            if frozen_result_valid:
                archive_record["logical_solution_sha256"] = hashlib.sha256(solution.encode("utf-8")).hexdigest()
                archive_record["logical_trace_sha256"] = hashlib.sha256(trace.encode("utf-8")).hexdigest()
            if self._clock() >= request.total_deadline_at:
                lifecycle = replace(
                    lifecycle,
                    stage="deadline_before_archive",
                    cleanup_deadline_status="EXCEEDED",
                    workspace_removed="NOT_ATTEMPTED_DEADLINE_EXHAUSTED",
                )
                return self._unclosed_lifecycle_result(
                    record, work, lifecycle,
                    error_code="cleanup_finalization_unconfirmed" if frozen_result_valid else (error_code or "total_deadline_before_archive"),
                    result_category=category,
                )
            archive_record["lifecycle"] = lifecycle.to_record()
            archive_started = True
            try:
                self._archive(
                    record.archive_id, record.request_body, task_dir, out_dir,
                    archive_record, fallback_response, deadline_at=request.total_deadline_at,
                )
            except BaseException as archive_error:
                if self._clock() >= request.total_deadline_at:
                    cleanup = list(lifecycle.cleanup_errors[:7])
                    cleanup.append("archive_deadline:{}:{}".format(type(archive_error).__name__[:64], str(archive_error)[:256]))
                    lifecycle = replace(
                        lifecycle, stage="archive_deadline", cleanup_deadline_status="EXCEEDED",
                        cleanup_errors=tuple(cleanup[:8]), workspace_removed="NOT_ATTEMPTED_DEADLINE_EXHAUSTED",
                    )
                    return self._unclosed_lifecycle_result(
                        record, work, lifecycle,
                        error_code="cleanup_finalization_unconfirmed" if frozen_result_valid else (error_code or "archive_failure"),
                        result_category=category,
                    )
                raise
            archive_record["archive_complete"] = True
            pipe_evidence_failed = any(
                item.startswith("pipe_evidence_write:") for item in lifecycle.cleanup_errors
            )
            lifecycle = replace(
                lifecycle,
                evidence_sealed="PIPE_EVIDENCE_WRITE_FAILED" if pipe_evidence_failed else "CONFIRMED",
                workspace_removed="UNKNOWN",
            )
            archive_record["lifecycle"] = lifecycle.to_record()
            if self._clock() >= request.total_deadline_at:
                lifecycle = replace(
                    lifecycle, stage="deadline_after_archive", cleanup_deadline_status="EXCEEDED",
                    workspace_removed="NOT_ATTEMPTED_DEADLINE_EXHAUSTED",
                )
                return self._unclosed_lifecycle_result(
                    record, work, lifecycle,
                    error_code="cleanup_finalization_unconfirmed" if frozen_result_valid else (error_code or "total_deadline_after_archive"),
                    result_category=category, archive_complete=True,
                )

            cleanup_error_type: str | None = None
            workspace_removed = "NOT_ATTEMPTED"
            try:
                self._require_before_deadline(request.total_deadline_at, "workspace existence check")
                if work.exists():
                    self._require_before_deadline(request.total_deadline_at, "workspace recursive removal")
                    shutil.rmtree(work)
                    workspace_removed = "CONFIRMED"
                else:
                    workspace_removed = "CONFIRMED_ALREADY_ABSENT"
            except BaseException as cleanup_error:
                cleanup_error_type = type(cleanup_error).__name__[:96]
                workspace_removed = (
                    "NOT_ATTEMPTED_DEADLINE_EXHAUSTED"
                    if self._clock() >= request.total_deadline_at and isinstance(cleanup_error, TimeoutError)
                    else "FAILED"
                )
                cleanup = list(lifecycle.cleanup_errors[:7])
                cleanup.append("workspace_cleanup:{}:{}".format(type(cleanup_error).__name__[:64], str(cleanup_error)[:256]))
                lifecycle = replace(
                    lifecycle, stage="workspace_cleanup", cleanup_errors=tuple(cleanup[:8]),
                )
            cleanup_deadline_exceeded = self._clock() >= request.total_deadline_at
            lifecycle = replace(
                lifecycle,
                workspace_removed=workspace_removed,
                cleanup_deadline_status="EXCEEDED" if cleanup_deadline_exceeded else lifecycle.cleanup_deadline_status,
            )
            workspace_cleanup_confirmed = workspace_removed in ("CONFIRMED", "CONFIRMED_ALREADY_ABSENT")
            pipe_evidence_failed = any(
                item.startswith("pipe_evidence_write:") for item in lifecycle.cleanup_errors
            )
            result_publishable = bool(
                frozen_result_valid
                and workspace_cleanup_confirmed
                and not cleanup_deadline_exceeded
                and not pipe_evidence_failed
                and lifecycle.eligible_for_done
            )
            manager_admission = None
            if result_publishable and self._job_manager is not None:
                self._require_before_deadline(
                    request.total_deadline_at, "manager result capacity admission"
                )
                manager_admission = self._job_manager.prepare_result_admission(
                    record,
                    solution=solution,
                    trace=trace,
                    elapsed_s=frozen_elapsed,
                    deadline_at=request.total_deadline_at,
                    clock=self._clock,
                )
                if not manager_admission.accepted:
                    result_publishable = False
            if result_publishable:
                archive_record["elapsed_s"] = frozen_elapsed
            else:
                archive_record.pop("elapsed_s", None)
            if frozen_result_valid and not result_publishable:
                if cleanup_deadline_exceeded:
                    outcome_error_code = "cleanup_finalization_unconfirmed"
                elif pipe_evidence_failed:
                    outcome_error_code = "worker_evidence_unconfirmed"
                elif not workspace_cleanup_confirmed:
                    outcome_error_code = "workspace_cleanup_unconfirmed"
                elif manager_admission is not None and not manager_admission.accepted:
                    outcome_error_code = manager_admission.error_code or "result_admission_unavailable"
                else:
                    outcome_error_code = "lifecycle_unconfirmed"
                final_response = {
                    "job_id": record.job_id,
                    "task_id": record.task_id,
                    "status": "failed",
                    "error_code": outcome_error_code,
                }
            else:
                outcome_error_code = error_code
                final_response = final_success_response
            archive_record.update(
                {
                    "logical_result_status": category,
                    "logical_error_code": error_code or None,
                    "public_status": final_response["status"],
                    "public_error_code": final_response.get("error_code"),
                    "resource_cleanup_status": workspace_removed,
                    "workspace_removed": workspace_removed,
                    "lifecycle": lifecycle.to_record(),
                    "manager_result_admission": (
                        "NOT_APPLICABLE" if self._job_manager is None
                        else "NOT_REQUESTED" if manager_admission is None
                        else "ACCEPTED" if manager_admission.accepted
                        else "REJECTED"
                    ),
                    "manager_result_admission_error_code": (
                        manager_admission.error_code if manager_admission is not None else None
                    ),
                    "manager_admission_serialized_bytes": (
                        manager_admission.serialized_bytes if manager_admission is not None else None
                    ),
                    "manager_admission_payload_sha256": (
                        manager_admission.payload_sha256 if manager_admission is not None else None
                    ),
                }
            )
            if cleanup_deadline_exceeded:
                # Only the independent bounded control receipt is allowed after expiry.
                return self._unclosed_lifecycle_result(
                    record, work, lifecycle,
                    error_code=outcome_error_code or "cleanup_finalization_unconfirmed",
                    result_category=category, archive_complete=True,
                )
            try:
                self._rewrite_archive_response(
                    record.archive_id, final_response, archive_record,
                    deadline_at=request.total_deadline_at,
                )
            except BaseException as finalization_error:
                cleanup = list(lifecycle.cleanup_errors[:7])
                cleanup.append("archive_finalization:{}:{}".format(type(finalization_error).__name__[:64], str(finalization_error)[:256]))
                lifecycle = replace(
                    lifecycle, stage="archive_finalization", evidence_sealed="UNKNOWN",
                    cleanup_errors=tuple(cleanup[:8]),
                )
                return self._unclosed_lifecycle_result(
                    record, work, lifecycle,
                    error_code=outcome_error_code or "cleanup_finalization_unconfirmed",
                    result_category=category, archive_complete=True,
                )
            if self._clock() >= request.total_deadline_at:
                lifecycle = replace(
                    lifecycle, stage="deadline_after_final_archive_write",
                    cleanup_deadline_status="EXCEEDED", evidence_sealed="UNKNOWN",
                )
                return self._unclosed_lifecycle_result(
                    record, work, lifecycle,
                    error_code=outcome_error_code or "cleanup_finalization_unconfirmed",
                    result_category=category, archive_complete=True,
                )
            audit_record = dict(archive_record)
            audit_record["archive_complete"] = True
            audit_record["post_archive_elapsed_s"] = round(max(0.0, self._clock() - started), 6)
            audit_record["cleanup_error_type"] = cleanup_error_type
            audit_record["workspace_removed"] = workspace_removed
            audit_record["lifecycle"] = lifecycle.to_record()
            self.audit_records.append(audit_record)
            if self._audit_sink is not None and self._clock() < request.total_deadline_at:
                try:
                    self._audit_sink(dict(audit_record))
                except Exception as audit_error:
                    audit_record["audit_sink_error_type"] = type(audit_error).__name__[:96]
            result_solution = solution if result_publishable else ""
            result_trace = trace if result_publishable else ""
            result_elapsed = frozen_elapsed if result_publishable else 0.0
            return JobExecutionResult(
                category,
                record.archive_id,
                result_solution,
                result_trace,
                result_elapsed,
                outcome_error_code or None,
                archive_complete=True,
                lifecycle=lifecycle,
            )
        except Exception as error:
            if self._clock() >= request.total_deadline_at:
                cleanup = list(lifecycle.cleanup_errors[:7])
                cleanup.append(
                    "deadline_exception_unwind:{}:{}".format(
                        type(error).__name__[:64], str(error)[:256]
                    )
                )
                lifecycle = replace(
                    lifecycle,
                    stage="deadline_exception_unwind",
                    cleanup_deadline_status="EXCEEDED",
                    evidence_sealed="UNKNOWN",
                    primary_error=lifecycle.primary_error or "{}: {}".format(
                        type(error).__name__[:96], str(error)[:320]
                    ),
                    exception_chain=lifecycle.exception_chain or exception_chain(error),
                    cleanup_errors=tuple(cleanup[:8]),
                )
                return self._unclosed_lifecycle_result(
                    record,
                    work,
                    lifecycle,
                    error_code="cleanup_deadline_exceeded",
                    result_category=category,
                    archive_complete=False,
                )
            try:
                self._require_before_deadline(request.total_deadline_at, "failure workspace existence check")
                work_exists = work is not None and work.exists()
                self._require_before_deadline(request.total_deadline_at, "failure workspace existence result")
                if work_exists and lifecycle.safe_to_read_outputs:
                    self._require_before_deadline(request.total_deadline_at, "failure workspace retention")
                    retained = self._retain_failed_work(
                        work, record.archive_id,
                        deadline_at=request.total_deadline_at, clock=self._clock,
                    )
                    self._require_before_deadline(request.total_deadline_at, "failure request body write")
                    failure_stage = "archive" if archive_started else (failure_stage or "workspace")
                    self._atomic_write(
                        retained / "request_body.bin",
                        record.request_body,
                        deadline_at=request.total_deadline_at,
                        clock=self._clock,
                    )
                    self._require_before_deadline(request.total_deadline_at, "failure summary write")
                    self._atomic_write_json(
                        retained / ("archive_failure.json" if archive_started else "task_failure.json"),
                        {"job_id": record.job_id, "task_id": record.task_id, "archive_id": record.archive_id,
                         "status": "archive_failure" if archive_started else "backend_failure",
                         "error_code": "archive_failure" if archive_started else "task_workspace_failure",
                          "failure_stage": failure_stage,
                          "error_type": type(error).__name__[:96]},
                        deadline_at=request.total_deadline_at,
                        clock=self._clock,
                    )
                elif work_exists:
                    return self._unclosed_lifecycle_result(
                        record, work, lifecycle, error_code="exception_with_lifecycle_unconfirmed",
                        result_category=category, archive_complete=False,
                    )
            except BaseException as evidence_error:
                cleanup = list(lifecycle.cleanup_errors[:7])
                cleanup.append("failure_evidence_write:{}:{}".format(type(evidence_error).__name__[:64], str(evidence_error)[:256]))
                lifecycle = replace(
                    lifecycle,
                    stage="failure_evidence_write",
                    cleanup_deadline_status=(
                        "EXCEEDED" if self._clock() >= request.total_deadline_at else lifecycle.cleanup_deadline_status
                    ),
                    primary_error=lifecycle.primary_error or "{}: {}".format(
                        type(error).__name__[:96], str(error)[:320]
                    ),
                    exception_chain=lifecycle.exception_chain or exception_chain(error),
                    cleanup_errors=tuple(cleanup[:8]),
                )
                return self._unclosed_lifecycle_result(
                    record, work, lifecycle,
                    error_code="cleanup_deadline_exceeded" if self._clock() >= request.total_deadline_at else "failure_evidence_unconfirmed",
                    result_category=category,
                    archive_complete=False,
                )
            return JobExecutionResult(
                "archive_failure" if archive_started else "backend_failure",
                record.archive_id,
                error_code="archive_failure" if archive_started else "task_workspace_failure",
                archive_complete=False,
                lifecycle=lifecycle,
            )
        finally:
            if lock_acquired and self._execution_lock is not None:
                self._execution_lock.release()

    def _solve(
        self,
        headers: Mapping[str, str] | None,
        body: bytes,
        *,
        request_started_at: float | None = None,
    ) -> EntryResponse:
        content_type = next(
            (str(v) for k, v in (headers or {}).items() if str(k).lower() == "content-type"),
            "",
        )
        if content_type and content_type.split(";", 1)[0].strip().lower() != "application/json":
            return self._error(415, "content type must be application/json")
        try:
            raw = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._error(400, "invalid JSON")
        if not isinstance(raw, dict):
            return self._error(400, "request must be a JSON object")
        try:
            request = self._parse_request(raw)
        except ValueError as exc:
            return self._error(400, str(exc))

        started = request_started_at if request_started_at is not None else time.monotonic()
        effective_budget = min(request.deadline_s, self._max_request_budget_s)
        deadline_at = started + effective_budget
        request = TaskRequest(
            request.task_id, request.nonce, request.mode, request.prompt,
            request.interface, effective_budget, deadline_at,
        )
        archive_id = secrets.token_hex(12)
        backend = self._backends[request.mode]
        backend_called = False
        status = "backend_unconfigured"
        solution = ""
        trace = ""
        trace_valid = False
        if self._scratch is not None:
            self._scratch.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(dir=self._scratch, prefix="official-entry-"))
        task_dir = work / "task"
        out_dir = work / "out"
        task_dir.mkdir()
        out_dir.mkdir()
        archive_error: str | None = None
        retained_work: Path | None = None
        try:
            self._write_exact(task_dir / "prompt.txt", request.prompt)
            if request.interface:
                self._write_exact(task_dir / "interface.txt", request.interface)

            if backend is not None:
                backend_called = True
                try:
                    remaining = deadline_at - time.monotonic() - self._archive_reserve_s
                    if remaining <= 0:
                        raise BackendTimeout("no worker budget remains after archive reserve")
                    backend.run(request, task_dir, out_dir, timeout_s=remaining)
                    if time.monotonic() > deadline_at:
                        status = "timeout"
                    else:
                        solution, trace, status, trace_valid = self._read_outputs(out_dir, mode=request.mode)
                except BackendTimeout:
                    status = "timeout"
                    _, trace, _, trace_valid = self._read_outputs(out_dir, mode=request.mode)
                except TimeoutError:
                    status = "timeout"
                    _, trace, _, trace_valid = self._read_outputs(out_dir, mode=request.mode)
                except Exception as exc:  # Backend errors become normal empty solutions.
                    status = f"backend_error:{type(exc).__name__}"
                    _, trace, _, trace_valid = self._read_outputs(out_dir, mode=request.mode)

            elapsed = round(max(0.0, time.monotonic() - started), 6)
            record = {
                "task_id": request.task_id,
                "nonce": request.nonce,
                "mode": request.mode,
                "interface_source": "prompt+interface" if request.interface else "prompt",
                "interface_field_present": bool(request.interface),
                "archive_id": archive_id,
                "status": status,
                "backend_called": backend_called,
                "solution_nonempty": bool(solution),
                "trace_valid": trace_valid,
                "elapsed_s": elapsed,
                "archive_complete": self._evidence_root is None,
            }
            response_body = {
                "task_id": request.task_id,
                "solution": solution,
                "trace": trace,
                "elapsed_s": elapsed,
            }
            if self._evidence_root is not None:
                try:
                    self._archive(archive_id, body, task_dir, out_dir, record, response_body)
                    record["archive_complete"] = True
                except Exception as exc:
                    archive_error = f"{type(exc).__name__}: {exc}"[:500]
                    retained_work = self._retain_failed_work(work, archive_id)
                    failed_record = dict(record)
                    failed_record.update(
                        {
                            "status": f"archive_error:{type(exc).__name__}",
                            "archive_complete": False,
                            "archive_error": archive_error,
                            "retained_work_dir": str(retained_work),
                        }
                    )
                    self._atomic_write_json(
                        retained_work / "archive_failure.json",
                        {
                            "error": archive_error,
                            "record": failed_record,
                            "response_body_before_failure": response_body,
                        },
                    )
                    record = failed_record
                    response_body = {
                        "task_id": request.task_id,
                        "solution": "",
                        "trace": "",
                        "elapsed_s": elapsed,
                    }
            # Archive and response preparation are part of the same request budget.
            elapsed = round(max(0.0, time.monotonic() - started), 6)
            if time.monotonic() > deadline_at:
                status = "archive_error_after_budget" if archive_error else "timeout_after_archive"
                response_body = {
                    "task_id": request.task_id,
                    "solution": "",
                    "trace": trace,
                    "elapsed_s": elapsed,
                }
                record.update({"status": status, "solution_nonempty": False, "elapsed_s": elapsed})
                if self._evidence_root is not None and archive_error is None:
                    try:
                        self._rewrite_archive_response(archive_id, response_body, record)
                    except Exception as exc:
                        record.update(
                            {
                                "status": "archive_timeout_rewrite_error:{}".format(type(exc).__name__),
                                "archive_complete": False,
                                "archive_timeout_rewrite_error": str(exc)[:300],
                            }
                        )
        finally:
            if archive_error is None and work.exists():
                shutil.rmtree(work)

        self.audit_records.append(record)
        if self._audit_sink is not None:
            self._audit_sink(dict(record))

        return EntryResponse(
            200,
            response_body,
        )

    @staticmethod
    def _parse_request(raw: dict[str, Any]) -> TaskRequest:
        task_id = raw.get("task_id")
        prompt = raw.get("prompt")
        if not isinstance(task_id, str):
            raise ValueError("task_id must be a string")
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        nonce = raw.get("nonce", "")
        mode = raw.get("mode", "agent")
        interface = raw.get("interface", "")
        deadline_s = raw.get("deadline_s", 360)
        if not isinstance(nonce, str) or not isinstance(mode, str) or not isinstance(interface, str):
            raise ValueError("nonce, mode, and interface must be strings")
        if mode not in ("agent", "baseline"):
            raise ValueError("unknown mode")
        if isinstance(deadline_s, bool) or not isinstance(deadline_s, int) or deadline_s <= 0:
            raise ValueError("deadline_s must be a positive integer")
        return TaskRequest(task_id, nonce, mode, prompt, interface, deadline_s)

    @staticmethod
    def _write_exact(
        path: Path,
        text: str,
        *,
        deadline_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        def require_time(stage: str) -> None:
            if deadline_at is not None and clock() >= deadline_at:
                raise RequestDeadlineExceeded("absolute request deadline reached before {}".format(stage[:80]))

        require_time("exact text open")
        with path.open("w", encoding="utf-8", newline="") as fh:
            require_time("exact text write")
            fh.write(text)
        require_time("exact text close")

    @staticmethod
    def _read_outputs(
        out_dir: Path,
        *,
        mode: str = "agent",
        max_result_bytes: int | None = None,
        deadline_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[str, str, str, bool]:
        def require_time(stage: str) -> None:
            if deadline_at is not None and clock() >= deadline_at:
                raise RequestDeadlineExceeded("absolute request deadline reached before {}".format(stage[:80]))

        if mode not in ("agent", "baseline"):
            return "", "", "invalid_output_mode", False
        solution_path = out_dir / "solution.v"
        trace_path = out_dir / "trace.jsonl"
        status_path = out_dir / "output_status.json"
        if max_result_bytes is not None:
            try:
                require_time("solution output open")
                with solution_path.open("rb") as stream:
                    require_time("solution output read")
                    solution_bytes = stream.read(max_result_bytes + 1)
                require_time("solution output decode")
            except RequestDeadlineExceeded:
                raise
            except FileNotFoundError:
                require_time("solution output missing classification")
                solution_missing = True
                solution_state = "missing_solution"
                solution = ""
            except OSError:
                require_time("solution output error classification")
                return "", "", "unreadable_solution", False
            else:
                solution_missing = False
                solution_state = "readable"
                if len(solution_bytes) > max_result_bytes:
                    require_time("solution output size classification")
                    return "", "", "result_too_large", False
                try:
                    require_time("solution output decode")
                    solution = solution_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    require_time("solution output decode error classification")
                    return "", "", "invalid_solution_encoding", False
            require_time("trace output open")
            trace_budget = max_result_bytes - len(solution_bytes) if not solution_missing else max_result_bytes
            try:
                require_time("trace output open")
                with trace_path.open("rb") as stream:
                    require_time("trace output read")
                    trace_bytes = stream.read(trace_budget + 1)
                require_time("trace output decode")
            except RequestDeadlineExceeded:
                raise
            except FileNotFoundError:
                require_time("trace output missing classification")
                return "", "", "missing_trace", False
            except OSError:
                require_time("trace output error classification")
                return "", "", "unreadable_trace", False
            if len(trace_bytes) > trace_budget:
                require_time("trace output size classification")
                return "", "", "result_too_large", False
            try:
                require_time("trace output decode")
                trace = trace_bytes.decode("utf-8")
            except UnicodeDecodeError:
                require_time("trace output decode error classification")
                return solution, "", "invalid_trace_encoding", False
        else:
            solution_missing = False
            try:
                require_time("solution output read")
                solution = solution_path.read_text(encoding="utf-8")
                require_time("solution output decode")
            except RequestDeadlineExceeded:
                raise
            except FileNotFoundError:
                require_time("solution output missing classification")
                solution = ""
                solution_state = "missing_solution"
            except UnicodeDecodeError:
                require_time("solution output decode error classification")
                return "", "", "invalid_solution_encoding", False
            except OSError:
                require_time("solution output error classification")
                return "", "", "unreadable_solution", False
            else:
                solution_state = "readable"
            try:
                require_time("trace output read")
                trace = trace_path.read_text(encoding="utf-8")
                require_time("trace output decode")
            except RequestDeadlineExceeded:
                raise
            except FileNotFoundError:
                require_time("trace output missing classification")
                return solution, "", "missing_trace", False
            except UnicodeDecodeError:
                require_time("trace output decode error classification")
                return solution, "", "invalid_trace_encoding", False
            except OSError:
                require_time("trace output error classification")
                return solution, "", "unreadable_trace", False
        trace_events: list[dict[str, Any]] = []
        trace_has_interface_rejection = False
        trace_has_checked_candidate = False
        spec_candidate_digest = None
        try:
            for line in trace.splitlines():
                require_time("trace event parsing")
                if line.strip():
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("trace event is not an object")
                    if mode == "baseline":
                        trace_events.append(event)
                    elif event.get("tool") == "public_spec_solver":
                        spec_candidate_digest = event.get("candidate_sha256")
                    if (
                        event.get("tool") == "llm"
                        and event.get("output_status") == "empty_interface_or_format_failure"
                        and event.get("check_status") == "fail"
                    ):
                        trace_has_interface_rejection = True
                    if (
                        event.get("tool") == "llm"
                        and event.get("output_status") == "candidate_checked"
                        and event.get("check_status") == "pass"
                    ):
                        trace_has_checked_candidate = True
            require_time("trace parsing completion")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            require_time("trace parse error classification")
            return "", trace, "malformed_trace", False

        if mode == "baseline":
            # The fixed root baseline writes only solution.v and trace.jsonl.
            # Metadata is not a generation call; served_model may legitimately be null.
            if solution_missing or solution_state != "readable":
                return "", trace, "missing_solution", True
            meta_fields = {"script_sha256", "served_model", "temperature", "top_p", "max_tokens", "track"}
            if (
                len(trace_events) not in (2, 3)
                or trace_events[0].get("tool") != "baseline_meta"
                or not meta_fields.issubset(trace_events[0])
                or trace_events[0].get("track") != "rtl"
            ):
                return solution, trace, "invalid_baseline_trace", True
            llm_events = trace_events[1:]
            for index, event in enumerate(llm_events):
                if (
                    event.get("tool") != "llm"
                    or type(event.get("round")) is not int
                    or event["round"] != 0
                    or not {"ts", "sec"}.issubset(event)
                ):
                    return solution, trace, "invalid_baseline_trace", True
                if "error" in event:
                    if index != len(llm_events) - 1 or not isinstance(event["error"], str) or not event["error"]:
                        return solution, trace, "invalid_baseline_trace", True
                elif (
                    not {"tokens_in", "tokens_out", "finish", "empty_content"}.issubset(event)
                    or type(event["empty_content"]) is not bool
                ):
                    return solution, trace, "invalid_baseline_trace", True
            # extract() can raise after the success event was appended, recording
            # a second event for the same round-0 attempt, followed by empty output.
            if len(llm_events) == 2 and "error" not in llm_events[-1]:
                return solution, trace, "invalid_baseline_trace", True
            if "error" in llm_events[-1]:
                return solution, trace, "producer_failure_baseline_llm_error", True
            # Empty content is normal, and extract("") writes a newline. Preserve
            # the producer's solution verbatim, including that newline.
            status = "empty_solution" if llm_events[0]["empty_content"] or not solution else "success"
            return solution, trace, status, True

        output_status: dict[str, Any] = {}
        try:
            require_time("output status open")
            if max_result_bytes is None:
                status_text = status_path.read_text(encoding="utf-8")
                require_time("output status decode")
            else:
                with status_path.open("rb") as stream:
                    require_time("output status read")
                    status_bytes = stream.read(64 * 1024 + 1)
                require_time("output status decode")
                if len(status_bytes) > 64 * 1024:
                    require_time("output status size classification")
                    return solution, trace, "invalid_output_status", True
                require_time("output status decode")
                status_text = status_bytes.decode("utf-8")
            require_time("output status parse")
            loaded_status = json.loads(status_text)
            require_time("output status parse completion")
        except RequestDeadlineExceeded:
            raise
        except FileNotFoundError:
            require_time("output status missing classification")
            loaded_status = None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            require_time("output status error classification")
            return solution, trace, "invalid_output_status", True
        if loaded_status is not None:
            if not isinstance(loaded_status, dict):
                return solution, trace, "invalid_output_status", True
            output_status = loaded_status
        else:
            return solution, trace, "missing_output_status", True
        interface_value = output_status.get("interface_status")
        if interface_value == "unverified" or output_status.get("output_status") == "candidate_interface_unverified":
            return solution, trace, "unverified_interface", True
        try:
            status_value = str(output_status.get("status", ""))
            detail_value = str(output_status.get("output_status", ""))
        except Exception:
            return solution, trace, "invalid_output_status", True
        budget_statuses = {"empty_budget_exceeded", "empty_worker_timeout", "empty_budget_exhausted"}
        if status_value in budget_statuses or detail_value in budget_statuses:
            if (
                status_value not in ("", *budget_statuses)
                or detail_value not in ("", *budget_statuses)
                or (status_value and detail_value and status_value != detail_value)
            ):
                return solution, trace, "invalid_output_status", True
            if solution_missing or solution_state == "missing_solution":
                return "", trace, "budget_output_missing_solution", False
            return solution, trace, "budget_output", True

        if detail_value == "empty_interface_or_format_failure":
            if (
                status_value in ("", "empty_interface_or_format_failure")
                and interface_value == "fail"
                and not solution_missing
                and solution_state == "readable"
                and not solution
                and trace_has_interface_rejection
            ):
                return "", trace, "empty_solution", True
            return solution, trace, "invalid_output_status", True

        if detail_value == "candidate_spec_checked":
            if (status_value in ("", "candidate_spec_checked") and interface_value == "pass"
                    and solution_state == "readable" and solution and spec_candidate_digest
                    and output_status.get("request_count") == 0
                    and spec_candidate_digest == output_status.get("solution_sha256")
                    and spec_candidate_digest == hashlib.sha256(solution.encode("utf-8")).hexdigest()):
                return solution, trace, "success", True
            return solution, trace, "invalid_output_status", True

        if detail_value == "candidate_checked":
            if (
                status_value in ("", "candidate_checked")
                and interface_value == "pass"
                and not solution_missing
                and solution_state == "readable"
                and bool(solution)
                and trace_has_checked_candidate
            ):
                return solution, trace, "success", True
            return solution, trace, "invalid_output_status", True

        if status_value == "baseline_candidate":
            if (
                detail_value in ("", "baseline_candidate")
                and not solution_missing
                and solution_state == "readable"
                and bool(solution)
            ):
                return solution, trace, "success", True
            return solution, trace, "invalid_output_status", True

        if status_value in ("empty_baseline_output", "baseline_output_written"):
            if (
                detail_value in ("", "empty_baseline_output", "baseline_output_written")
                and not solution_missing
                and solution_state == "readable"
                and not solution
            ):
                return "", trace, "empty_solution", True
            return solution, trace, "invalid_output_status", True

        producer_failures = {
            "empty_http_error",
            "empty_parse_error",
            "empty_transport_error",
            "empty_post_response_error",
            "empty_queue_or_worker_error",
            "empty_input_error",
            "empty_baseline_error",
        }
        if status_value in producer_failures or detail_value in producer_failures:
            return solution, trace, "producer_failure_" + (detail_value if detail_value in producer_failures else status_value), True
        if detail_value.startswith("prepare_error:"):
            return solution, trace, "producer_failure_prepare_error", True
        if solution_missing or solution_state == "missing_solution":
            return "", trace, "missing_solution", True
        return solution, trace, "unrecognized_output_status", True

    def _archive(
        self,
        archive_id: str,
        request_body: bytes,
        task_dir: Path,
        out_dir: Path,
        record: Mapping[str, Any],
        response_body: Mapping[str, Any],
        *,
        deadline_at: float | None = None,
    ) -> None:
        if self._evidence_root is None:
            return
        self._require_before_deadline(deadline_at, "archive root creation")
        self._evidence_root.mkdir(parents=True, exist_ok=True)
        destination = self._evidence_root / archive_id
        self._require_before_deadline(deadline_at, "archive destination creation")
        destination.mkdir(parents=True, exist_ok=False)
        self._require_before_deadline(deadline_at, "archive task directory creation")
        (destination / "task").mkdir()
        self._require_before_deadline(deadline_at, "archive output directory creation")
        (destination / "out").mkdir()
        for source_dir, target_dir in ((task_dir, destination / "task"), (out_dir, destination / "out")):
            members = iter(source_dir.rglob("*"))
            while True:
                self._require_before_deadline(deadline_at, "archive member enumeration")
                try:
                    source = next(members)
                except StopIteration:
                    break
                self._require_before_deadline(deadline_at, "archive member type check")
                if source.is_file():
                    relative = source.relative_to(source_dir)
                    target = target_dir / relative
                    self._require_before_deadline(deadline_at, "archive parent directory creation")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._require_before_deadline(deadline_at, "archive payload copy")
                    shutil.copy2(source, target)
        self._atomic_write(destination / "request_body.bin", request_body, deadline_at=deadline_at, clock=self._clock)
        self._atomic_write_json(destination / "response.json", response_body, deadline_at=deadline_at, clock=self._clock)
        metadata = dict(record)
        metadata.update(
            {
                "archive_complete": False,
                "request_sha256": hashlib.sha256(request_body).hexdigest(),
                "response_sha256": hashlib.sha256(
                    json.dumps(response_body, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "solution_sha256": hashlib.sha256(
                    str(response_body.get("solution", "")).encode("utf-8")
                ).hexdigest(),
                "trace_sha256": hashlib.sha256(
                    str(response_body.get("trace", "")).encode("utf-8")
                ).hexdigest(),
            }
        )
        self._atomic_write_json(destination / "metadata.json", metadata, deadline_at=deadline_at, clock=self._clock)
        self._verify_archive(destination, task_dir, out_dir, deadline_at=deadline_at)
        metadata["archive_complete"] = True
        lifecycle = metadata.get("lifecycle")
        if isinstance(lifecycle, dict):
            cleanup_errors = lifecycle.get("cleanup_errors", [])
            pipe_evidence_failed = isinstance(cleanup_errors, list) and any(
                isinstance(item, str) and item.startswith("pipe_evidence_write:")
                for item in cleanup_errors
            )
            lifecycle["evidence_sealed"] = (
                "PIPE_EVIDENCE_WRITE_FAILED" if pipe_evidence_failed else "CONFIRMED"
            )
        self._atomic_write_json(destination / "metadata.json", metadata, deadline_at=deadline_at, clock=self._clock)

    def _rewrite_archive_response(
        self,
        archive_id: str,
        response_body: Mapping[str, Any],
        record: Mapping[str, Any],
        *,
        deadline_at: float | None = None,
    ) -> None:
        if self._evidence_root is None:
            return
        destination = self._evidence_root / archive_id
        metadata_path = destination / "metadata.json"
        self._require_before_deadline(deadline_at, "final archive metadata read")
        metadata_text = metadata_path.read_text(encoding="utf-8")
        self._require_before_deadline(deadline_at, "final archive metadata decode")
        metadata = json.loads(metadata_text)
        self._require_before_deadline(deadline_at, "final archive metadata update")
        metadata.update(dict(record))
        metadata.update(
            {
                "archive_complete": True,
                "response_sha256": hashlib.sha256(
                    json.dumps(response_body, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "solution_sha256": hashlib.sha256(str(response_body.get("solution", "")).encode("utf-8")).hexdigest(),
                "trace_sha256": hashlib.sha256(str(response_body.get("trace", "")).encode("utf-8")).hexdigest(),
            }
        )
        self._atomic_write_json(destination / "response.json", response_body, deadline_at=deadline_at, clock=self._clock)
        self._require_before_deadline(deadline_at, "final archive metadata write")
        self._atomic_write_json(metadata_path, metadata, deadline_at=deadline_at, clock=self._clock)

    def _verify_archive(
        self, destination: Path, task_dir: Path, out_dir: Path, *, deadline_at: float | None = None
    ) -> None:
        for source_dir, target_dir in ((task_dir, destination / "task"), (out_dir, destination / "out")):
            members = iter(source_dir.rglob("*"))
            while True:
                self._require_before_deadline(deadline_at, "archive verification member enumeration")
                try:
                    source = next(members)
                except StopIteration:
                    break
                self._require_before_deadline(deadline_at, "archive verification member type check")
                if source.is_file():
                    target = target_dir / source.relative_to(source_dir)
                    self._require_before_deadline(deadline_at, "archive source evidence read")
                    source_bytes = source.read_bytes()
                    self._require_before_deadline(deadline_at, "archive copy evidence read")
                    self._require_before_deadline(deadline_at, "archive copy file type check")
                    target_is_file = target.is_file()
                    self._require_before_deadline(deadline_at, "archive copy file type result")
                    if not target_is_file:
                        raise IOError(f"archived evidence mismatch: {target}")
                    self._require_before_deadline(deadline_at, "archive copy evidence bytes read")
                    if target.read_bytes() != source_bytes:
                        raise IOError(f"archived evidence mismatch: {target}")
        for required in ("request_body.bin", "response.json", "metadata.json"):
            self._require_before_deadline(deadline_at, "archive required member check")
            member_exists = (destination / required).is_file()
            self._require_before_deadline(deadline_at, "archive required member result")
            if not member_exists:
                raise IOError(f"archive evidence missing: {destination / required}")

    def _require_before_deadline(self, deadline_at: float | None, stage: str) -> None:
        if deadline_at is not None and self._clock() >= deadline_at:
            raise RequestDeadlineExceeded("absolute request deadline reached before {}".format(stage[:96]))
    def _retain_failed_work(
        self,
        work: Path,
        archive_id: str,
        *,
        deadline_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Path:
        if self._evidence_root is None:
            return work
        def require_time(stage: str) -> None:
            if deadline_at is not None and clock() >= deadline_at:
                raise RequestDeadlineExceeded("absolute request deadline reached before {}".format(stage[:96]))

        require_time("failed workspace retention directory creation")
        retained_root = self._evidence_root.parent / "retained_work"
        retained_root.mkdir(parents=True, exist_ok=True)
        require_time("failed workspace move")
        destination = retained_root / archive_id
        try:
            require_time("failed workspace move")
            shutil.move(str(work), str(destination))
            return destination
        except RequestDeadlineExceeded:
            raise
        except Exception:
            return work

    @staticmethod
    def _atomic_write(
        path: Path,
        data: bytes,
        *,
        deadline_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        def require_time(stage: str) -> None:
            if deadline_at is not None and clock() >= deadline_at:
                raise RequestDeadlineExceeded("absolute deadline reached before atomic write {}".format(stage[:80]))

        temporary = path.with_name(path.name + ".tmp")
        require_time("temp open")
        with temporary.open("wb") as fh:
            require_time("file write")
            fh.write(data)
            require_time("file flush")
            fh.flush()
            require_time("file close")
        require_time("replace")
        temporary.replace(path)

    @classmethod
    def _atomic_write_json(
        cls,
        path: Path,
        value: Mapping[str, Any],
        *,
        deadline_at: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cls._atomic_write(
            path,
            (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
            deadline_at=deadline_at,
            clock=clock,
        )
    @staticmethod
    def _error(status: int, detail: str) -> EntryResponse:
        return EntryResponse(status, {"detail": detail})
