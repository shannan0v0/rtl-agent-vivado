"""Bounded, single-worker in-memory registry for asynchronous HTTP jobs.

Importing this module has no runtime side effects.  The owning server starts
the worker explicitly after construction.
"""

from __future__ import annotations

import json
import hashlib
import queue
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .lifecycle_types import LifecycleEvidence

DEFAULT_PER_RESULT_BYTES = 1024 * 1024
DEFAULT_TOTAL_RESULT_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class JobExecutionResult:
    category: str
    archive_id: str
    solution: str = ""
    trace: str = ""
    elapsed_s: float = 0.0
    error_code: str | None = None
    archive_complete: bool = False
    lifecycle: LifecycleEvidence | None = None


@dataclass(frozen=True)
class ResultAdmission:
    accepted: bool
    error_code: str | None
    serialized_bytes: int
    payload_sha256: str


@dataclass(frozen=True)
class JobSubmission:
    http_status: int
    body: dict[str, Any]
    accepted: bool = False
    duplicate: bool = False


@dataclass
class JobRecord:
    job_id: str
    task_id: str
    nonce: str
    mode: str
    principal_fingerprint: str
    request_fingerprint: str
    archive_id: str
    received_monotonic: float
    received_wall_time: str
    requested_deadline_s: int
    total_deadline_at: float
    work_deadline_at: float
    request: Any
    request_body: bytes
    status: str = "queued"
    error_code: str | None = None
    result: JobExecutionResult | None = None
    body_bytes: int = 0
    result_bytes: int = 0
    result_admission_state: str = "UNDECIDED"
    result_admission_bytes: int = 0
    result_admission_sha256: str | None = None
    result_admission_error_code: str | None = None
    result_release_outcome: str = "NOT_ATTEMPTED"
    result_release_state_before: str | None = None
    result_release_state_after: str | None = None
    result_release_attempted: bool = False
    result_release_owner_confirmed: bool | None = None
    result_release_error: str | None = None
    result_release_failure_kind: str | None = None


class JobManager:
    """Own one non-daemon worker and a bounded, non-evicting job registry."""

    def __init__(
        self,
        executor: Callable[[JobRecord], JobExecutionResult],
        *,
        queue_capacity: int = 8,
        record_capacity: int = 4096,
        pending_request_bytes: int = 16 * 1024 * 1024,
        per_result_bytes: int = DEFAULT_PER_RESULT_BYTES,
        total_result_bytes: int = DEFAULT_TOTAL_RESULT_BYTES,
        close_wait_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(queue_capacity, record_capacity, pending_request_bytes, per_result_bytes, total_result_bytes) <= 0:
            raise ValueError("job manager capacities must be positive")
        if close_wait_s < 0:
            raise ValueError("close wait must be non-negative")
        self._executor = executor
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=queue_capacity)
        self._record_capacity = record_capacity
        self._pending_request_bytes_limit = pending_request_bytes
        self._per_result_bytes = per_result_bytes
        self._total_result_bytes_limit = total_result_bytes
        self._close_wait_s = close_wait_s
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, JobRecord] = {}
        self._nonce_index: dict[tuple[str, str], str] = {}
        self._pending_request_bytes = 0
        self._total_result_bytes = 0
        self._reserved_result_bytes = 0
        self._result_reservations: dict[str, tuple[JobRecord, int]] = {}
        self._result_accounting_uncertain = False
        self._worker: threading.Thread | None = None
        self._accepting = False
        self._closed = False
        self._state = "not_started"

    def start(self) -> bool:
        """Start exactly one worker; repeated starts never create another."""
        with self._lock:
            if self._worker is not None or self._closed:
                return False
            worker = threading.Thread(target=self._worker_loop, name="official-entry-job-worker", daemon=False)
            self._worker = worker
            self._state = "starting"
            try:
                worker.start()
            except RuntimeError:
                self._worker = None
                self._state = "start_failed"
                self._accepting = False
                return False
            self._state = "running"
            self._accepting = True
            return True

    def is_healthy(self) -> bool:
        with self._lock:
            return self._state == "running" and self._accepting and self._worker is not None and self._worker.is_alive()

    def state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "accepting": self._accepting,
                "worker_alive": bool(self._worker and self._worker.is_alive()),
                "close_incomplete": self._state == "close_incomplete",
                "jobs": len(self._records),
            }

    @staticmethod
    def _serialize_public_result(
        record: JobRecord, solution: str, trace: str, elapsed_s: float
    ) -> tuple[dict[str, Any], bytes]:
        payload = {
            "job_id": record.job_id,
            "task_id": record.task_id,
            "status": "done",
            "solution": solution,
            "trace": trace,
            "elapsed_s": elapsed_s,
        }
        # Match EntryRequestHandler._write exactly, including JSON whitespace.
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return payload, raw

    def prepare_result_admission(
        self,
        record: JobRecord,
        *,
        solution: str,
        trace: str,
        elapsed_s: float,
        deadline_at: float,
        clock: Callable[[], float],
    ) -> ResultAdmission:
        """Reserve exact public JSON bytes before the adapter finalizes a done archive."""
        if clock() >= deadline_at:
            raise TimeoutError("result admission deadline exceeded before serialization")
        _, raw = self._serialize_public_result(record, solution, trace, elapsed_s)
        serialized_bytes = len(raw)
        payload_sha256 = hashlib.sha256(raw).hexdigest()
        if clock() >= deadline_at:
            raise TimeoutError("result admission deadline exceeded after serialization")

        with self._lock:
            if clock() >= deadline_at:
                raise TimeoutError("result admission deadline exceeded before reservation")
            if record.result_release_outcome == "UNKNOWN":
                self._result_accounting_uncertain = True
                return ResultAdmission(False, "result_reservation_release_unconfirmed", serialized_bytes, payload_sha256)
            if self._result_accounting_uncertain:
                return ResultAdmission(False, "result_cache_accounting_unconfirmed", serialized_bytes, payload_sha256)
            current = self._records.get(record.job_id)
            if current is not record or current.status != "running":
                if self._try_release_result_admission_locked(record) == "UNKNOWN":
                    return ResultAdmission(False, "result_admission_unavailable", serialized_bytes, payload_sha256)
                record.result_admission_state = "REJECTED"
                record.result_admission_error_code = "result_admission_unavailable"
                record.result_admission_bytes = serialized_bytes
                record.result_admission_sha256 = payload_sha256
                return ResultAdmission(False, record.result_admission_error_code, serialized_bytes, payload_sha256)

            if record.result_admission_state != "UNDECIDED":
                same_payload = (
                    record.result_admission_sha256 == payload_sha256
                    and record.result_admission_bytes == serialized_bytes
                )
                if same_payload and record.result_admission_state in ("RESERVED", "REJECTED"):
                    return ResultAdmission(
                        record.result_admission_state == "RESERVED",
                        record.result_admission_error_code,
                        serialized_bytes,
                        payload_sha256,
                    )
                if self._try_release_result_admission_locked(record) == "UNKNOWN":
                    return ResultAdmission(False, "result_admission_conflict", serialized_bytes, payload_sha256)
                record.result_admission_state = "REJECTED"
                record.result_admission_error_code = "result_admission_conflict"
                record.result_admission_bytes = serialized_bytes
                record.result_admission_sha256 = payload_sha256
                return ResultAdmission(False, record.result_admission_error_code, serialized_bytes, payload_sha256)

            record.result_admission_bytes = serialized_bytes
            record.result_admission_sha256 = payload_sha256
            if serialized_bytes > self._per_result_bytes:
                record.result_admission_state = "REJECTED"
                record.result_admission_error_code = "result_too_large"
            elif serialized_bytes > self._total_result_bytes_limit - self._total_result_bytes - self._reserved_result_bytes:
                record.result_admission_state = "REJECTED"
                record.result_admission_error_code = "result_cache_full"
            else:
                self._reserved_result_bytes += serialized_bytes
                record.result_admission_bytes = serialized_bytes
                record.result_admission_state = "RESERVED"
                record.result_admission_error_code = None
                self._result_reservations[record.job_id] = (record, serialized_bytes)
            return ResultAdmission(
                record.result_admission_state == "RESERVED",
                record.result_admission_error_code,
                serialized_bytes,
                payload_sha256,
            )

    def _release_result_admission_locked(self, record: JobRecord) -> bool:
        reservation = self._result_reservations.get(record.job_id)
        if (
            record.result_admission_state != "RESERVED"
            or reservation is None
            or reservation[0] is not record
        ):
            return False
        reserved = record.result_admission_bytes
        if reserved <= 0 or reservation[1] != reserved or reserved > self._reserved_result_bytes:
            raise RuntimeError("result admission reservation accounting is inconsistent")
        self._reserved_result_bytes -= reserved
        del self._result_reservations[record.job_id]
        record.result_admission_state = "RELEASED"
        return True

    def release_result_admission(self, record: JobRecord) -> bool:
        """Idempotently confirm release; UNKNOWN never retries the raw mutation."""
        with self._lock:
            return self._try_release_result_admission_locked(record) == "RELEASED"

    def _try_release_result_admission_locked(self, record: JobRecord) -> str:
        """Keep release failure secondary and retain this record's quota responsibility."""
        if record.result_release_outcome == "UNKNOWN":
            self._result_accounting_uncertain = True
            return "UNKNOWN"
        reservation = self._result_reservations.get(record.job_id)
        owned = reservation is not None and reservation[0] is record
        if record.result_release_outcome == "RELEASED":
            if record.result_admission_state == "RELEASED" and not owned:
                return "RELEASED"
            record.result_release_outcome = "UNKNOWN"
            record.result_release_error = "released reservation state/map changed; no retry"
            record.result_release_failure_kind = "STATE_CHANGED"
            record.result_release_state_after = record.result_admission_state
            record.result_release_owner_confirmed = owned
            self._result_accounting_uncertain = True
            return "UNKNOWN"
        before = record.result_admission_state
        record.result_release_state_before = before
        record.result_release_state_after = before
        record.result_release_owner_confirmed = owned
        if before != "RESERVED":
            record.result_release_outcome = "UNKNOWN" if owned else "NO_RESERVATION"
            if owned:
                record.result_release_error = "non-RESERVED state still owns reservation; no mutation"
                record.result_release_failure_kind = "STATE_MAP_MISMATCH"
                self._result_accounting_uncertain = True
            return record.result_release_outcome
        # Latch UNKNOWN before the one raw attempt, including a partially completed unwind.
        record.result_release_outcome = "UNKNOWN"
        record.result_release_attempted = True
        try:
            released = self._release_result_admission_locked(record)
            remaining = self._result_reservations.get(record.job_id)
            if released and record.result_admission_state == "RELEASED" and (
                remaining is None or remaining[0] is not record
            ):
                record.result_release_outcome = "RELEASED"
                record.result_release_error = None
                record.result_release_failure_kind = None
            else:
                record.result_release_error = "release returned False or completion state unconfirmed"
                record.result_release_failure_kind = "RETURNED_FALSE" if not released else "STATE_UNCONFIRMED"
        except Exception as error:
            # The explicit raw invariant error has a primitive text argument.
            # Do not invoke an arbitrary exception formatter while holding the lock.
            message = "<message not evaluated under Manager lock>"
            if type(error) is RuntimeError and error.args and type(error.args[0]) is str:
                message = error.args[0][:256]
            record.result_release_error = "{}:{}".format(type(error).__name__[:64], message)
            record.result_release_failure_kind = "EXCEPTION"
        finally:
            record.result_release_state_after = record.result_admission_state
        if record.result_release_outcome == "UNKNOWN":
            self._result_accounting_uncertain = True
        return record.result_release_outcome

    def _fail_publication_locked(
        self, record: JobRecord, result: JobExecutionResult, fallback: str | None = None
    ) -> None:
        """Terminalize only a current running job; cleanup evidence is not the primary."""
        if self._records.get(record.job_id) is not record or record.status != "running":
            return
        primary = result.error_code
        if (
            result.category == "budget_stopped" and primary == "work_deadline_reached"
            and result.archive_complete and result.lifecycle is not None
            and result.lifecycle.eligible_for_done
        ):
            primary = None  # Logical budget stop is not a public done error.
        if fallback is None:
            if result.category == "expired":
                fallback = "deadline_expired"
            elif result.category in ("normal", "budget_stopped"):
                if not result.archive_complete:
                    fallback = "archive_incomplete"
                elif result.lifecycle is not None and result.lifecycle.workspace_removed not in (
                    "CONFIRMED", "CONFIRMED_ALREADY_ABSENT"
                ):
                    fallback = "workspace_cleanup_unconfirmed"
                else:
                    fallback = "lifecycle_unconfirmed"
            else:
                fallback = result.category
        record.status = "expired" if result.category == "expired" else "failed"
        record.error_code = record.error_code or primary or fallback
        record.result = None
        record.result_bytes = 0
        self._release_request_locked(record)

    def submit(
        self,
        *,
        principal_fingerprint: str,
        nonce: str,
        request_fingerprint: str,
        request: Any,
        request_body: bytes,
        task_id: str,
        mode: str,
        received_monotonic: float,
        received_wall_time: str,
        requested_deadline_s: int,
        max_deadline_s: int,
        archive_reserve_s: float,
    ) -> JobSubmission:
        key = (principal_fingerprint, nonce)
        with self._lock:
            if not self._accepting or self._state != "running":
                return JobSubmission(503, {"detail": "service_unavailable"})
            # Idempotency lookup deliberately precedes all capacity checks.
            existing_id = self._nonce_index.get(key)
            if existing_id is not None:
                existing = self._records[existing_id]
                if existing.request_fingerprint != request_fingerprint:
                    return JobSubmission(409, {
                        "detail": "nonce_conflict",
                        "conflict_fields": ["task_id", "mode", "prompt", "interface", "deadline_s"],
                    })
                return self._public_submission(existing, duplicate=True)
            if len(self._records) >= self._record_capacity:
                return JobSubmission(503, {"detail": "job_registry_full"})
            # Budget both the retained raw JSON and the parsed prompt copy.
            body_size = len(request_body) * 2
            if body_size > self._pending_request_bytes_limit - self._pending_request_bytes:
                return JobSubmission(503, {"detail": "request_capacity_full"})

            effective_s = min(requested_deadline_s, max_deadline_s)
            total_deadline = received_monotonic + effective_s
            work_deadline = total_deadline - max(0.0, archive_reserve_s)
            job_id = secrets.token_hex(16)
            archive_id = secrets.token_hex(12)
            record = JobRecord(
                job_id=job_id,
                task_id=task_id,
                nonce=nonce,
                mode=mode,
                principal_fingerprint=principal_fingerprint,
                request_fingerprint=request_fingerprint,
                archive_id=archive_id,
                received_monotonic=received_monotonic,
                received_wall_time=received_wall_time,
                requested_deadline_s=effective_s,
                total_deadline_at=total_deadline,
                work_deadline_at=work_deadline,
                request=request,
                request_body=request_body,
                body_bytes=body_size,
            )
            # Construct the first ACK before the worker can mutate this record.
            ack = {"job_id": job_id, "task_id": task_id, "status": "queued"}
            self._records[job_id] = record
            self._nonce_index[key] = job_id
            self._pending_request_bytes += body_size
            try:
                self._queue.put_nowait(job_id)
            except queue.Full:
                self._pending_request_bytes -= body_size
                self._nonce_index.pop(key, None)
                self._records.pop(job_id, None)
                return JobSubmission(503, {"detail": "queue_full"})
            return JobSubmission(202, ack, accepted=True)

    def _public_submission(self, record: JobRecord, *, duplicate: bool) -> JobSubmission:
        if record.status == "done" and record.result is not None:
            result = record.result
            return JobSubmission(200, {
                "job_id": record.job_id,
                "task_id": record.task_id,
                "status": "done",
                "solution": result.solution,
                "trace": result.trace,
                "elapsed_s": result.elapsed_s,
            }, duplicate=duplicate)
        if record.status in ("failed", "expired"):
            return JobSubmission(503, {
                "job_id": record.job_id,
                "task_id": record.task_id,
                "status": record.status,
                "error_code": record.error_code or "job_failed",
            }, duplicate=duplicate)
        return JobSubmission(200 if duplicate else 202, {
            "job_id": record.job_id,
            "task_id": record.task_id,
            "status": record.status,
        }, duplicate=duplicate)

    def get_snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return None
            if record.status == "done" and record.result is not None:
                result = record.result
                return {
                    "job_id": record.job_id,
                    "task_id": record.task_id,
                    "status": "done",
                    "solution": result.solution,
                    "trace": result.trace,
                    "elapsed_s": result.elapsed_s,
                }
            if record.status in ("failed", "expired"):
                return {
                    "job_id": record.job_id,
                    "task_id": record.task_id,
                    "status": record.status,
                    "error_code": record.error_code or "job_failed",
                }
            return {"job_id": record.job_id, "task_id": record.task_id, "status": record.status}

    def close(self) -> dict[str, Any]:
        """Stop admission, terminalize queued work, then finitely join the worker."""
        with self._lock:
            if self._closed:
                if self._worker is not None and not self._worker.is_alive() and self._state == "close_incomplete":
                    self._state = "closed"
                return self._state_snapshot_locked()
            self._accepting = False
            self._closed = True
            worker = self._worker
            if worker is None:
                self._state = "closed"
        # Queue operations and the join stay outside the registry lock.
        drained: list[str] = []
        if worker is not None:
            while True:
                try:
                    queued = self._queue.get_nowait()
                except queue.Empty:
                    break
                if queued is not None:
                    drained.append(queued)
        with self._lock:
            for job_id in drained:
                record = self._records.get(job_id)
                if record is not None and record.status == "queued":
                    record.status = "failed"
                    record.error_code = "service_closing"
                    self._release_request_locked(record)
            if worker is not None:
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    # A full queue means the worker is consuming or a race left
                    # an item. It will observe admission closed after that item.
                    pass
        if worker is not None:
            worker.join(timeout=self._close_wait_s)
            with self._lock:
                if worker.is_alive():
                    self._state = "close_incomplete"
                elif self._state != "worker_failed":
                    self._state = "closed"
        return self.state_snapshot()

    def _state_snapshot_locked(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "accepting": self._accepting,
            "worker_alive": bool(self._worker and self._worker.is_alive()),
            "close_incomplete": self._state == "close_incomplete",
            "jobs": len(self._records),
        }

    def _release_request_locked(self, record: JobRecord) -> None:
        self._pending_request_bytes = max(0, self._pending_request_bytes - record.body_bytes)
        record.body_bytes = 0
        record.request_body = b""
        record.request = None

    def _worker_loop(self) -> None:
        try:
            while True:
                job_id = self._queue.get()
                if job_id is None:
                    break
                with self._lock:
                    record = self._records.get(job_id)
                    if record is None or record.status != "queued":
                        continue
                    if not self._accepting or self._closed:
                        record.status = "failed"
                        record.error_code = "service_closing"
                        self._release_request_locked(record)
                        continue
                    if self._clock() >= record.work_deadline_at:
                        record.status = "expired"
                        record.error_code = "deadline_expired_in_queue"
                        self._release_request_locked(record)
                        continue
                    record.status = "running"
                try:
                    result = self._executor(record)
                    if not isinstance(result, JobExecutionResult):
                        result = JobExecutionResult("backend_failure", record.archive_id, error_code="invalid_executor_result")
                except BaseException:
                    result = JobExecutionResult("backend_failure", record.archive_id, error_code="backend_exception")
                self._publish_result(record, result)
        except BaseException:
            with self._lock:
                self._state = "worker_failed"
                self._accepting = False
                for record in self._records.values():
                    if record.status in ("queued", "running"):
                        record.status = "failed"
                        record.error_code = record.error_code or "worker_failed"
                        self._try_release_result_admission_locked(record)
                        self._release_request_locked(record)
    def _publish_result(self, record: JobRecord, result: JobExecutionResult) -> None:
        category = result.category
        with self._lock:
            current = self._records.get(record.job_id)
            if current is not record or current.status != "running":
                if record.result_admission_state == "RESERVED":
                    self._try_release_result_admission_locked(record)
                return
            if current.result_release_outcome == "UNKNOWN":
                self._result_accounting_uncertain = True
            if self._result_accounting_uncertain:
                if current.result_admission_state == "RESERVED":
                    self._try_release_result_admission_locked(current)
                self._fail_publication_locked(
                    current, result,
                    "result_reservation_release_unconfirmed" if current.result_release_outcome == "UNKNOWN"
                    else "result_cache_accounting_unconfirmed",
                )
                return
            if current.result_admission_state in (
                "RELEASED", "COMMITTED"
            ):
                self._fail_publication_locked(current, result, "result_reservation_release_unconfirmed")
                return
        if (
            record.result_admission_state == "RESERVED"
            and self._clock() >= record.total_deadline_at
        ):
            with self._lock:
                current = self._records.get(record.job_id)
                if current is record and current.status == "running":
                    self._try_release_result_admission_locked(current)
                    self._fail_publication_locked(current, result, "cleanup_finalization_unconfirmed")
                else:
                    if record.result_admission_state == "RESERVED":
                        self._try_release_result_admission_locked(record)
            return
        _, serialized = self._serialize_public_result(
            record, result.solution, result.trace, result.elapsed_s
        )
        serialized_size = len(serialized)
        payload_sha256 = hashlib.sha256(serialized).hexdigest()
        with self._lock:
            current = self._records.get(record.job_id)
            if current is not record or current.status != "running":
                if record.result_admission_state == "RESERVED":
                    self._try_release_result_admission_locked(record)
                return
            if current.result_release_outcome == "UNKNOWN":
                self._result_accounting_uncertain = True
            if self._result_accounting_uncertain:
                if current.result_admission_state == "RESERVED":
                    self._try_release_result_admission_locked(current)
                self._fail_publication_locked(
                    current, result,
                    "result_reservation_release_unconfirmed" if current.result_release_outcome == "UNKNOWN"
                    else "result_cache_accounting_unconfirmed",
                )
                return
            if current.result_admission_state in (
                "RELEASED", "COMMITTED"
            ):
                self._fail_publication_locked(current, result, "result_reservation_release_unconfirmed")
                return
            self._release_request_locked(current)
            lifecycle_ready = result.lifecycle is not None and result.lifecycle.eligible_for_done

            if current.result_admission_state == "REJECTED":
                if (
                    category in ("normal", "budget_stopped")
                    and result.archive_complete
                    and lifecycle_ready
                    and result.error_code == current.result_admission_error_code
                ):
                    current.error_code = current.result_admission_error_code
                else:
                    current.error_code = result.error_code or "cleanup_finalization_unconfirmed"
                current.status = "failed"
                current.result = None
                current.result_bytes = 0
                return

            if current.result_admission_state == "RESERVED":
                if category in ("normal", "budget_stopped") and result.archive_complete and lifecycle_ready:
                    if self._clock() >= current.total_deadline_at:
                        self._try_release_result_admission_locked(current)
                        self._fail_publication_locked(current, result, "cleanup_finalization_unconfirmed")
                        return
                    if (
                        serialized_size != current.result_admission_bytes
                        or payload_sha256 != current.result_admission_sha256
                    ):
                        self._try_release_result_admission_locked(current)
                        self._fail_publication_locked(current, result, "result_admission_mismatch")
                        return
                    reserved = current.result_admission_bytes
                    reservation = self._result_reservations.get(current.job_id)
                    if (
                        reserved <= 0
                        or reserved > self._reserved_result_bytes
                        or reservation is None
                        or reservation[0] is not current
                        or reservation[1] != reserved
                    ):
                        self._try_release_result_admission_locked(current)
                        self._fail_publication_locked(current, result, "result_admission_accounting_unconfirmed")
                        return
                    self._reserved_result_bytes -= reserved
                    del self._result_reservations[current.job_id]
                    current.result_admission_state = "COMMITTED"
                    current.status = "done"
                    current.error_code = None
                    current.result = result
                    current.result_bytes = serialized_size
                    self._total_result_bytes += serialized_size
                    return
                self._try_release_result_admission_locked(current)
                self._fail_publication_locked(current, result)
                return

            if category in ("normal", "budget_stopped") and result.archive_complete and lifecycle_ready:
                if serialized_size > self._per_result_bytes:
                    current.status = "failed"
                    current.error_code = "result_too_large"
                    return
                if serialized_size > self._total_result_bytes_limit - self._total_result_bytes - self._reserved_result_bytes:
                    current.status = "failed"
                    current.error_code = "result_cache_full"
                    return
                current.status = "done"
                current.error_code = None
                current.result = result
                current.result_bytes = serialized_size
                self._total_result_bytes += serialized_size
            elif category in ("normal", "budget_stopped"):
                current.status = "failed"
                if not result.archive_complete:
                    current.error_code = result.error_code or "archive_incomplete"
                elif result.lifecycle is not None and result.lifecycle.workspace_removed not in (
                    "CONFIRMED",
                    "CONFIRMED_ALREADY_ABSENT",
                ):
                    current.error_code = result.error_code or "workspace_cleanup_unconfirmed"
                else:
                    current.error_code = result.error_code or "lifecycle_unconfirmed"
            elif category == "expired":
                current.status = "expired"
                current.error_code = result.error_code or "deadline_expired"
            else:
                current.status = "failed"
                current.error_code = result.error_code or category

