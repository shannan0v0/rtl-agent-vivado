"""Explicit, bounded lifecycle evidence for the strict HTTP child path.

The states describe only the directly owned process/session and captured pipes.
They do not claim that escaped or external writers do not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


MAX_LIFECYCLE_TEXT = 512
MAX_LIFECYCLE_ERRORS = 8


def _bounded_text(value: object, limit: int = MAX_LIFECYCLE_TEXT) -> str:
    try:
        text = str(value)
    except Exception:
        text = "<unprintable>"
    return text[:limit]


def exception_chain(error: BaseException | None, *, limit: int = MAX_LIFECYCLE_ERRORS) -> tuple[str, ...]:
    """Return a bounded cause/context chain without replacing its first error."""
    result: list[str] = []
    seen: set[int] = set()
    current = error
    while current is not None and len(result) < limit and id(current) not in seen:
        seen.add(id(current))
        result.append(
            "{}: {}".format(type(current).__name__[:96], _bounded_text(current))
        )
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    if current is not None and len(result) == limit:
        result.append("exception_chain_truncated")
    return tuple(result)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int | None = None
    start_ticks: int | None = None
    process_group: int | None = None
    session_id: int | None = None
    acquisition_state: str = "UNKNOWN"
    pidfd: int | None = field(default=None, repr=False, compare=False)
    acquisition_errors: tuple[str, ...] = ()

    @property
    def bound(self) -> bool:
        return (
            self.acquisition_state == "BOUND"
            and self.pid is not None
            and self.start_ticks is not None
            and self.process_group == self.pid
            and self.session_id == self.pid
            and self.pidfd is not None
        )


@dataclass(frozen=True)
class ProcEntry:
    parent_pid: int
    process_group: int
    session_id: int
    state: str
    start_ticks: int


@dataclass(frozen=True)
class ProcSnapshot:
    entries: Mapping[int, ProcEntry]
    complete: bool
    vanished_count: int = 0
    error_count: int = 0
    errors: tuple[str, ...] = ()
    scope: str = "visible_numeric_proc_entries_at_scan_time"


@dataclass(frozen=True)
class StopOutcome:
    identity_state: str = "UNKNOWN"
    stop_requested: str = "UNKNOWN"
    exit_confirmed: str = "UNKNOWN"
    snapshot_complete: str = "UNKNOWN"
    owned_group_quiescent: str = "UNKNOWN"
    reap_status: str = "UNKNOWN"
    term_signals_sent: int = 0
    kill_signals_sent: int = 0
    vanished_during_scan: int = 0
    last_live_member_count: int | None = None
    cleanup_errors: tuple[str, ...] = ()
    primary_error: str | None = None


@dataclass(frozen=True)
class LifecycleEvidence:
    stage: str = "UNKNOWN"
    spawn_state: str = "UNKNOWN"
    identity_state: str = "UNKNOWN"
    pid: int | None = None
    process_returncode: int | None = None
    process_group: int | None = None
    session_id: int | None = None
    start_ticks: int | None = None
    stop_requested: str = "UNKNOWN"
    term_signals_sent: int = 0
    kill_signals_sent: int = 0
    vanished_during_scan: int = 0
    last_live_member_count: int | None = None
    stop_outcome_error: str | None = None
    exit_confirmed: str = "UNKNOWN"
    process_snapshot_complete: str = "UNKNOWN"
    owned_group_quiescent: str = "UNKNOWN"
    reap_status: str = "UNKNOWN"
    pipe_eof: str = "UNKNOWN"
    process_resources_closed: str = "UNKNOWN"
    external_writer_scope: str = "UNKNOWN"
    cleanup_deadline_status: str = "UNKNOWN"
    evidence_sealed: str = "UNKNOWN"
    workspace_removed: str = "UNKNOWN"
    primary_error: str | None = None
    exception_chain: tuple[str, ...] = ()
    cleanup_errors: tuple[str, ...] = ()
    pending_paths: tuple[str, ...] = ()

    @property
    def safe_to_read_outputs(self) -> bool:
        if self.spawn_state == "NOT_ATTEMPTED":
            return True
        return (
            self.spawn_state == "CREATED"
            and self.identity_state == "BOUND"
            and self.exit_confirmed == "CONFIRMED"
            and self.process_snapshot_complete == "CONFIRMED"
            and self.owned_group_quiescent == "CONFIRMED_FOR_BOUND_GROUP"
            and self.reap_status == "CONFIRMED"
            and self.pipe_eof == "CONFIRMED"
            and self.process_resources_closed == "CONFIRMED"
            and self.cleanup_deadline_status == "WITHIN_DEADLINE"
        )

    @property
    def eligible_for_done(self) -> bool:
        return (
            self.safe_to_read_outputs
            and self.spawn_state == "CREATED"
            and self.evidence_sealed == "CONFIRMED"
            and self.workspace_removed in ("CONFIRMED", "CONFIRMED_ALREADY_ABSENT")
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "stage": _bounded_text(self.stage, 96),
            "spawn_state": _bounded_text(self.spawn_state, 64),
            "identity_state": _bounded_text(self.identity_state, 64),
            "pid": self.pid,
            "process_returncode": self.process_returncode,
            "process_group": self.process_group,
            "session_id": self.session_id,
            "start_ticks": self.start_ticks,
            "stop_requested": _bounded_text(self.stop_requested, 64),
            "term_signals_sent": self.term_signals_sent,
            "kill_signals_sent": self.kill_signals_sent,
            "vanished_during_scan": self.vanished_during_scan,
            "last_live_member_count": self.last_live_member_count,
            "stop_outcome_error": _bounded_text(self.stop_outcome_error) if self.stop_outcome_error else None,
            "exit_confirmed": _bounded_text(self.exit_confirmed, 64),
            "process_snapshot_complete": _bounded_text(self.process_snapshot_complete, 64),
            "owned_group_quiescent": _bounded_text(self.owned_group_quiescent, 96),
            "reap_status": _bounded_text(self.reap_status, 64),
            "pipe_eof": _bounded_text(self.pipe_eof, 64),
            "process_resources_closed": _bounded_text(self.process_resources_closed, 64),
            "external_writer_scope": _bounded_text(self.external_writer_scope, 96),
            "cleanup_deadline_status": _bounded_text(self.cleanup_deadline_status, 64),
            "evidence_sealed": _bounded_text(self.evidence_sealed, 64),
            "workspace_removed": _bounded_text(self.workspace_removed, 64),
            "primary_error": _bounded_text(self.primary_error) if self.primary_error else None,
            "exception_chain": [_bounded_text(item) for item in self.exception_chain[:MAX_LIFECYCLE_ERRORS + 1]],
            "cleanup_errors": [_bounded_text(item) for item in self.cleanup_errors[:MAX_LIFECYCLE_ERRORS]],
            "pending_paths": [_bounded_text(item, 1024) for item in self.pending_paths[:4]],
        }


class LifecycleExecutionError(RuntimeError):
    """Backend error carrying explicit lifecycle state for the HTTP adapter."""

    def __init__(
        self,
        message: str,
        evidence: LifecycleEvidence,
        *,
        is_timeout: bool = False,
        failure_kind: str = "backend_failure",
    ) -> None:
        super().__init__(_bounded_text(message))
        self.evidence = evidence
        self.is_timeout = bool(is_timeout)
        self.failure_kind = _bounded_text(failure_kind, 96)
