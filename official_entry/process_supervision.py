"""Linux process ownership helpers for HTTP, directory, and resident services.

Signals are limited to an isolated process group created with a new session or
an inherited task group whose descendants were discovered from /proc. No
process-name matching or waitpid(-1) is used.
"""

from __future__ import annotations

import ctypes
import errno
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

from .lifecycle_types import (
    ProcEntry as StrictProcEntry,
    ProcSnapshot,
    ProcessIdentity,
    StopOutcome,
)


class CleanupDeadlineExceeded(TimeoutError):
    """A strict HTTP lifecycle action was not started after its absolute deadline."""


def _require_cleanup_time(cleanup_deadline_at: float, stage: str) -> None:
    if not math.isfinite(cleanup_deadline_at) or time.monotonic() >= cleanup_deadline_at:
        raise CleanupDeadlineExceeded("cleanup deadline exhausted before {}".format(stage[:96]))


# (ppid, process_group, session_id, state, start_ticks)
ProcEntry = tuple[int, int, int, str, int]


def enable_child_subreaper() -> bool:
    """Ask Linux to reparent orphaned descendants to this process."""
    if not sys_is_linux():
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None or prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "prctl(PR_SET_CHILD_SUBREAPER)")
    return True


def sys_is_linux() -> bool:
    return os.name == "posix" and Path("/proc/self/stat").is_file()


def _proc_snapshot() -> dict[int, ProcEntry]:
    result: dict[int, ProcEntry] = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return result
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="ascii")
            tail = raw[raw.rfind(")") + 2 :].split()
            # tail begins at field 3 (state): ppid=1, pgrp=2, session=3,
            # and starttime=19 (the kernel's field 22).
            result[int(entry.name)] = (
                int(tail[1]), int(tail[2]), int(tail[3]), tail[0], int(tail[19])
            )
        except (OSError, ValueError, IndexError):
            continue
    return result


def _descendants(root_pid: int, snapshot: dict[int, ProcEntry]) -> set[int]:
    found = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _group, _session, _state, _start) in snapshot.items():
            if parent in found and pid not in found:
                found.add(pid)
                changed = True
    return found


def _signal_pids(pids: Iterable[int], sig: int, *, allowed_group: int) -> None:
    snapshot = _proc_snapshot()
    for pid in sorted(set(pids), reverse=True):
        entry = snapshot.get(pid)
        if entry is None or entry[1] != allowed_group:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            continue


def live_process_group_members(process_group: int, session_id: int) -> list[int]:
    """List live processes only when both recorded group and session match."""
    if (
        isinstance(process_group, bool)
        or not isinstance(process_group, int)
        or process_group <= 1
        or isinstance(session_id, bool)
        or not isinstance(session_id, int)
        or session_id != process_group
    ):
        raise ValueError("owned service group must be a positive isolated PGID/SID pair")
    return [
        pid
        for pid, (_parent, group, session, state, _start) in _proc_snapshot().items()
        if group == process_group and session == session_id and state != "Z"
    ]


def _reap_adopted_group_children(
    process_group: int, session_id: int, *, timeout_s: float
) -> None:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        snapshot = _proc_snapshot()
        adopted = [
            pid
            for pid, (parent, group, session, state, _start) in snapshot.items()
            if parent == os.getpid()
            and group == process_group
            and session == session_id
            and state == "Z"
        ]
        for pid in adopted:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        live = [
            pid
            for pid, (parent, group, session, state, _start) in snapshot.items()
            if parent == os.getpid()
            and group == process_group
            and session == session_id
            and state != "Z"
        ]
        if not live:
            return
        time.sleep(0.02)


def reap_adopted_children(
    process_group: int, *, timeout_s: float = 1.0, session_id: int | None = None
) -> None:
    """Reap only this process's adopted children in the supplied task group."""
    snapshot = _proc_snapshot()
    if session_id is None:
        sessions = {
            session for _pid, (_parent, group, session, _state, _start) in snapshot.items()
            if group == process_group
        }
        if len(sessions) != 1:
            return
        session_id = next(iter(sessions))
    if isinstance(session_id, bool) or not isinstance(session_id, int):
        return
    _reap_adopted_group_children(process_group, session_id, timeout_s=timeout_s)


def _terminate_verified_group(
    process_group: int,
    session_id: int,
    *,
    leader_pid: int,
    leader_start_ticks: int | None,
    grace_s: float,
    kill_grace_s: float,
) -> None:
    if process_group <= 1 or session_id != process_group or leader_pid != process_group:
        raise ValueError("refusing to signal a group without its isolated owner identity")
    snapshot = _proc_snapshot()
    leader = snapshot.get(leader_pid)
    if leader is not None:
        if (
            leader[1] != process_group
            or leader[2] != session_id
            or leader_start_ticks is None
            or leader[4] != leader_start_ticks
        ):
            raise RuntimeError("owned group leader identity changed; refusing to signal")
    members = [
        pid
        for pid, (_parent, group, session, state, _start) in snapshot.items()
        if group == process_group and session == session_id and state != "Z"
    ]
    if not members:
        return
    # When the leader has already exited, the persisted PGID/SID still names
    # its isolated session while members remain. No other group can reuse it.
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    grace_deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < grace_deadline:
        _reap_adopted_group_children(process_group, session_id, timeout_s=0.02)
        if not live_process_group_members(process_group, session_id):
            return
        time.sleep(0.02)
    if live_process_group_members(process_group, session_id):
        os.killpg(process_group, signal.SIGKILL)
    kill_deadline = time.monotonic() + max(0.0, kill_grace_s)
    while time.monotonic() < kill_deadline:
        _reap_adopted_group_children(process_group, session_id, timeout_s=0.02)
        if not live_process_group_members(process_group, session_id):
            return
        time.sleep(0.02)
    remaining = live_process_group_members(process_group, session_id)
    if remaining:
        raise RuntimeError("owned process group remains live after TERM/KILL: {}".format(len(remaining)))


def stop_owned_process_group(
    process_group: int,
    session_id: int,
    *,
    leader_pid: int,
    leader_start_ticks: int | None,
    grace_s: float = 1.0,
    kill_grace_s: float = 2.0,
) -> None:
    """Stop a group from a persisted, previously verified owner record.

    Callers must first verify the owner's executable, command digest, PID
    start ticks (when the leader remains), and PGID/SID. A vanished leader is
    allowed only so surviving members of that same isolated session can be
    terminated and waited for.
    """
    if not sys_is_linux():
        raise RuntimeError("owned Linux process-group stopping requires /proc")
    _terminate_verified_group(
        process_group,
        session_id,
        leader_pid=leader_pid,
        leader_start_ticks=leader_start_ticks,
        grace_s=grace_s,
        kill_grace_s=kill_grace_s,
    )


def stop_owned_group(
    process: subprocess.Popen[bytes],
    *,
    process_group: int,
    grace_s: float = 0.75,
) -> None:
    """TERM, wait, KILL if needed, and reap a child launched in a new session."""
    if os.name == "nt":
        _stop_windows_process(process, grace_s)
        return
    if process_group != process.pid:
        raise RuntimeError("owned worker group must equal its new-session leader PID")
    leader = _proc_snapshot().get(process.pid)
    if leader is not None and (leader[1] != process_group or leader[2] != process_group):
        raise RuntimeError("owned worker process group/session identity changed")
    start_ticks = leader[4] if leader is not None else None
    _terminate_verified_group(
        process_group,
        process_group,
        leader_pid=process.pid,
        leader_start_ticks=start_ticks,
        grace_s=grace_s,
        kill_grace_s=2.0,
    )
    try:
        process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise RuntimeError("owned worker pipes remained open after group termination") from error
    process.wait()
    _reap_adopted_group_children(process_group, process_group, timeout_s=1.0)
    if live_process_group_members(process_group, process_group):
        raise RuntimeError("owned worker process group still has live processes")


def stop_owned_tree(
    process: subprocess.Popen[bytes],
    *,
    grace_s: float = 0.75,
) -> None:
    """Stop one inherited-group child and descendants discovered below its PID."""
    if os.name == "nt":
        _stop_windows_process(process, grace_s)
        return
    try:
        allowed_group = os.getpgrp()
    except OSError:
        process.terminate()
        process.wait()
        return

    def current_owned() -> set[int]:
        snapshot = _proc_snapshot()
        current = _descendants(process.pid, snapshot)
        current = {pid for pid in current if snapshot.get(pid, (0, -1, -1, "", 0))[1] == allowed_group}
        if process.poll() is not None:
            current.update(
                pid
                for pid, (parent, group, _session, state, _start) in snapshot.items()
                if parent == os.getpid() and group == allowed_group and state != "Z"
            )
        return current

    _signal_pids(current_owned(), signal.SIGTERM, allowed_group=allowed_group)
    grace_deadline = time.monotonic() + max(0.0, grace_s)
    try:
        process.communicate(timeout=max(0.0, grace_s))
    except subprocess.TimeoutExpired:
        pass
    while time.monotonic() < grace_deadline:
        snapshot = _proc_snapshot()
        for pid, (parent, group, _session, state, _start) in snapshot.items():
            if parent == os.getpid() and group == allowed_group and state == "Z":
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
        if not current_owned():
            break
        time.sleep(0.02)
    _signal_pids(current_owned(), signal.SIGKILL, allowed_group=allowed_group)
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline and current_owned():
        time.sleep(0.02)
    try:
        process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired as error:
        process.kill()
        process.communicate()
        raise RuntimeError("owned worker pipes remained open after tree termination") from error
    process.wait()
    reap_adopted_children(allowed_group, timeout_s=1.0)
    if current_owned():
        raise RuntimeError("owned worker descendants remain in the inherited task group")


def _stop_windows_process(process: subprocess.Popen[bytes], grace_s: float) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.communicate(timeout=max(0.0, grace_s))
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
    process.wait()


# The strict HTTP path below is separate from the legacy helpers above. Other
# consumers (directory workers and service_manager) retain their existing API
# and behavior until they receive their own reviewed lifecycle changes.
_STRICT_PROC_STAT_MAX_BYTES = 4096
_STRICT_PROC_ENTRY_MAX_COUNT = 131072
_STRICT_PROC_ERROR_SAMPLE_MAX = 16
_STRICT_PIDFD_INFO_MAX_BYTES = 4096


def _bounded_lifecycle_error(error: BaseException, *, stage: str) -> str:
    try:
        detail = str(error)
    except Exception:
        detail = "<unprintable>"
    return "{}:{}:{}".format(stage[:64], type(error).__name__[:64], detail[:320])


def require_http_linux_supervision(*, cleanup_deadline_at: float) -> None:
    """Fail before spawn unless Linux procfs and pidfd signal APIs are present."""
    _require_cleanup_time(cleanup_deadline_at, "Linux supervision preflight")
    if not sys.platform.startswith("linux") or not sys_is_linux():
        raise RuntimeError("strict HTTP process supervision requires Linux /proc")
    _require_cleanup_time(cleanup_deadline_at, "pidfd capability checks")
    if not callable(getattr(os, "pidfd_open", None)):
        raise RuntimeError("strict HTTP process supervision requires os.pidfd_open")
    if not callable(getattr(signal, "pidfd_send_signal", None)):
        raise RuntimeError("strict HTTP process supervision requires signal.pidfd_send_signal")
    _require_cleanup_time(cleanup_deadline_at, "subreaper capability check")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    subreaper_state = ctypes.c_int(0)
    _require_cleanup_time(cleanup_deadline_at, "PR_GET_CHILD_SUBREAPER call")
    if prctl is None or prctl(37, ctypes.byref(subreaper_state), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), "prctl(PR_GET_CHILD_SUBREAPER)")
    if subreaper_state.value != 1:
        raise RuntimeError("strict HTTP process supervision requires the controller subreaper capability")
    _require_cleanup_time(cleanup_deadline_at, "self pidfd open")
    descriptor = os.pidfd_open(os.getpid(), 0)
    try:
        _require_cleanup_time(cleanup_deadline_at, "self pidfd stat")
        os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _require_cleanup_time(cleanup_deadline_at, "preflight proc snapshot")
    snapshot = strict_proc_snapshot(cleanup_deadline_at=cleanup_deadline_at)
    if not snapshot.complete or os.getpid() not in snapshot.entries:
        raise RuntimeError("strict HTTP process supervision could not obtain a complete procfs preflight")


def _parse_strict_proc_stat(raw: bytes) -> StrictProcEntry:
    if not raw or len(raw) > _STRICT_PROC_STAT_MAX_BYTES:
        raise ValueError("proc stat is empty or exceeds the bounded record size")
    text = raw.decode("ascii", errors="strict")
    close = text.rfind(")")
    if close < 0 or close + 2 >= len(text):
        raise ValueError("proc stat has no command/state separator")
    tail = text[close + 2 :].split()
    if len(tail) < 20:
        raise ValueError("proc stat has fewer than 22 fields")
    state = tail[0]
    if len(state) != 1:
        raise ValueError("proc stat state field is malformed")
    return StrictProcEntry(
        parent_pid=int(tail[1]),
        process_group=int(tail[2]),
        session_id=int(tail[3]),
        state=state,
        start_ticks=int(tail[19]),
    )


def _read_strict_proc_entry(pid: int, *, cleanup_deadline_at: float) -> StrictProcEntry | None:
    """Read one bounded /proc stat row; None means the PID vanished (ENOENT)."""
    path = "/proc/{}/stat".format(pid)
    try:
        _require_cleanup_time(cleanup_deadline_at, "open {}".format(path))
        with open(path, "rb", buffering=0) as stream:
            _require_cleanup_time(cleanup_deadline_at, "read {}".format(path))
            raw = stream.read(_STRICT_PROC_STAT_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    return _parse_strict_proc_stat(raw)


def _read_pidfd_target_pid(descriptor: int, *, cleanup_deadline_at: float) -> int | None:
    """Read the kernel-reported pidfd target; None means it has exited."""
    path = "/proc/self/fdinfo/{}".format(descriptor)
    _require_cleanup_time(cleanup_deadline_at, "open {}".format(path))
    with open(path, "rb", buffering=0) as stream:
        _require_cleanup_time(cleanup_deadline_at, "read {}".format(path))
        raw = stream.read(_STRICT_PIDFD_INFO_MAX_BYTES + 1)
    if not raw or len(raw) > _STRICT_PIDFD_INFO_MAX_BYTES:
        raise ValueError("pidfd fdinfo is empty or exceeds the bounded record size")
    text = raw.decode("ascii", errors="strict")
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "Pid":
            target = int(value.strip())
            return target if target > 0 else None
    raise ValueError("pidfd fdinfo does not expose a Pid field")


def strict_proc_snapshot(*, cleanup_deadline_at: float) -> ProcSnapshot:
    """Read visible numeric proc entries and distinguish races from unreadable rows.

    ENOENT for a PID listed by /proc is recorded as a normal disappearance race.
    Any other I/O/parse failure, a missing /proc directory, or the bounded entry
    cap makes the snapshot incomplete. Even a complete snapshot covers only the
    visible /proc namespace at scan time; it cannot establish absence of escaped
    sessions or external writers.
    """
    entries: dict[int, StrictProcEntry] = {}
    errors: list[str] = []
    vanished = 0
    error_count = 0
    complete = True
    numeric_count = 0

    def record_error(text: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < _STRICT_PROC_ERROR_SAMPLE_MAX:
            errors.append(text[:384])

    try:
        _require_cleanup_time(cleanup_deadline_at, "snapshot self probe")
        self_entry = _read_strict_proc_entry(os.getpid(), cleanup_deadline_at=cleanup_deadline_at)
        if self_entry is None:
            complete = False
            record_error("proc_self_probe_missing_before_scan")
        _require_cleanup_time(cleanup_deadline_at, "open /proc scanner")
        with os.scandir("/proc") as scan:
            while True:
                _require_cleanup_time(cleanup_deadline_at, "read next /proc entry")
                try:
                    directory = next(scan)
                except StopIteration:
                    break
                name = directory.name
                if not (name.isascii() and name.isdecimal()):
                    continue
                numeric_count += 1
                if numeric_count > _STRICT_PROC_ENTRY_MAX_COUNT:
                    complete = False
                    record_error("proc_entry_limit_exceeded")
                    break
                pid = int(name)
                try:
                    entry = _read_strict_proc_entry(pid, cleanup_deadline_at=cleanup_deadline_at)
                except CleanupDeadlineExceeded as error:
                    complete = False
                    record_error(_bounded_lifecycle_error(error, stage="proc_deadline"))
                    break
                except (OSError, UnicodeError, ValueError, IndexError) as error:
                    complete = False
                    record_error(_bounded_lifecycle_error(error, stage="proc_stat"))
                    continue
                if entry is None:
                    vanished += 1
                    continue
                entries[pid] = entry
        if complete:
            self_after = _read_strict_proc_entry(os.getpid(), cleanup_deadline_at=cleanup_deadline_at)
        else:
            self_after = None
        if self_after is None or os.getpid() not in entries:
            complete = False
            record_error("proc_self_probe_missing_after_scan")
    except OSError as error:
        complete = False
        record_error(_bounded_lifecycle_error(error, stage="proc_scan"))
    except CleanupDeadlineExceeded as error:
        complete = False
        record_error(_bounded_lifecycle_error(error, stage="proc_deadline"))
    except (UnicodeError, ValueError, IndexError) as error:
        complete = False
        record_error(_bounded_lifecycle_error(error, stage="proc_self_probe"))
    return ProcSnapshot(
        entries=entries,
        complete=complete,
        vanished_count=vanished,
        error_count=error_count,
        errors=tuple(errors),
    )


def bind_spawn_identity(
    process: subprocess.Popen[bytes], *, cleanup_deadline_at: float
) -> ProcessIdentity:
    """Bind the Popen child to its start-time identity without polling first."""
    pid = process.pid
    descriptor: int | None = None
    errors: list[str] = []
    try:
        _require_cleanup_time(cleanup_deadline_at, "spawn pidfd open")
        descriptor = os.pidfd_open(pid, 0)
    except OSError as error:
        errors.append(_bounded_lifecycle_error(error, stage="pidfd_open_spawn"))
        return ProcessIdentity(pid=pid, acquisition_state="UNKNOWN", acquisition_errors=tuple(errors))
    try:
        pidfd_target = _read_pidfd_target_pid(descriptor, cleanup_deadline_at=cleanup_deadline_at)
        if pidfd_target != pid:
            errors.append("pidfd_fdinfo_spawn:target_pid_mismatch_or_exited")
            return ProcessIdentity(
                pid=pid,
                acquisition_state="UNKNOWN",
                pidfd=descriptor,
                acquisition_errors=tuple(errors),
            )
        entry = _read_strict_proc_entry(pid, cleanup_deadline_at=cleanup_deadline_at)
    except (OSError, UnicodeError, ValueError, IndexError) as error:
        errors.append(_bounded_lifecycle_error(error, stage="proc_stat_spawn"))
        return ProcessIdentity(pid=pid, acquisition_state="UNKNOWN", pidfd=descriptor, acquisition_errors=tuple(errors))
    if entry is None:
        errors.append("proc_stat_spawn:PID_VANISHED_BEFORE_IDENTITY_BIND")
        return ProcessIdentity(pid=pid, acquisition_state="UNKNOWN", pidfd=descriptor, acquisition_errors=tuple(errors))
    try:
        target_after_stat = _read_pidfd_target_pid(descriptor, cleanup_deadline_at=cleanup_deadline_at)
    except (OSError, UnicodeError, ValueError, IndexError) as error:
        errors.append(_bounded_lifecycle_error(error, stage="pidfd_recheck_spawn"))
        return ProcessIdentity(
            pid=pid,
            start_ticks=entry.start_ticks,
            process_group=entry.process_group,
            session_id=entry.session_id,
            acquisition_state="UNKNOWN",
            pidfd=descriptor,
            acquisition_errors=tuple(errors),
        )
    if target_after_stat != pid:
        errors.append("pidfd_recheck_spawn:target_pid_mismatch_or_exited")
        return ProcessIdentity(
            pid=pid,
            start_ticks=entry.start_ticks,
            process_group=entry.process_group,
            session_id=entry.session_id,
            acquisition_state="UNKNOWN",
            pidfd=descriptor,
            acquisition_errors=tuple(errors),
        )
    if entry.process_group != pid or entry.session_id != pid:
        errors.append("proc_stat_spawn:NEW_SESSION_IDENTITY_MISMATCH")
        return ProcessIdentity(
            pid=pid,
            start_ticks=entry.start_ticks,
            process_group=entry.process_group,
            session_id=entry.session_id,
            acquisition_state="MISMATCH",
            pidfd=descriptor,
            acquisition_errors=tuple(errors),
        )
    return ProcessIdentity(
        pid=pid,
        start_ticks=entry.start_ticks,
        process_group=entry.process_group,
        session_id=entry.session_id,
        acquisition_state="BOUND",
        pidfd=descriptor,
    )


def close_spawn_identity(identity: ProcessIdentity) -> tuple[str, ...]:
    """Close only the pidfd returned for this Popen identity; never retry close."""
    if identity.pidfd is None:
        return ()
    try:
        os.close(identity.pidfd)
    except OSError as error:
        return (_bounded_lifecycle_error(error, stage="pidfd_close_owner"),)
    return ()


def _identity_mismatch(snapshot: ProcSnapshot, identity: ProcessIdentity) -> str | None:
    leader = snapshot.entries.get(identity.pid or -1)
    if leader is None:
        return None
    if (
        leader.start_ticks != identity.start_ticks
        or leader.process_group != identity.process_group
        or leader.session_id != identity.session_id
    ):
        return "BOUND_LEADER_IDENTITY_CHANGED"
    return None


def _signal_snapshot_members(
    snapshot: ProcSnapshot,
    identity: ProcessIdentity,
    sig: int,
    *,
    cleanup_deadline_at: float,
) -> tuple[int, int, tuple[str, ...], bool]:
    sent = 0
    vanished = 0
    errors: list[str] = []
    deadline_exhausted = False

    def record_error(message: str) -> None:
        if len(errors) < _STRICT_PROC_ERROR_SAMPLE_MAX:
            errors.append(message[:384])

    members = [
        (pid, entry)
        for pid, entry in snapshot.entries.items()
        if entry.process_group == identity.process_group
        and entry.session_id == identity.session_id
        and entry.state != "Z"
    ]
    for pid, observed in sorted(members, reverse=True):
        descriptor: int | None = None
        try:
            _require_cleanup_time(cleanup_deadline_at, "member pidfd open")
            descriptor = os.pidfd_open(pid, 0)
            pidfd_target = _read_pidfd_target_pid(descriptor, cleanup_deadline_at=cleanup_deadline_at)
            if pidfd_target != pid:
                record_error("member_pidfd_target_mismatch_or_exited:pid={}".format(pid))
                continue
            current = _read_strict_proc_entry(pid, cleanup_deadline_at=cleanup_deadline_at)
            if current is None:
                vanished += 1
                continue
            if (
                current.start_ticks != observed.start_ticks
                or current.process_group != identity.process_group
                or current.session_id != identity.session_id
            ):
                record_error("member_identity_changed:pid={}".format(pid))
                continue
            if current.state == "Z":
                continue
            _require_cleanup_time(cleanup_deadline_at, "pidfd signal")
            signal.pidfd_send_signal(descriptor, sig, None, 0)
            sent += 1
        except CleanupDeadlineExceeded as error:
            deadline_exhausted = True
            record_error(_bounded_lifecycle_error(error, stage="member_signal_deadline"))
            break
        except ProcessLookupError:
            vanished += 1
        except FileNotFoundError as error:
            # A missing fdinfo entry is not the normal /proc/<pid>/stat vanish race.
            record_error(_bounded_lifecycle_error(error, stage="pidfd_fdinfo_or_signal"))
        except OSError as error:
            if error.errno == errno.ESRCH:
                vanished += 1
            else:
                record_error(_bounded_lifecycle_error(error, stage="pidfd_signal"))
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError as error:
                    record_error(_bounded_lifecycle_error(error, stage="pidfd_close_member"))
    return sent, vanished, tuple(errors), deadline_exhausted


def _reap_visible_adopted(
    snapshot: ProcSnapshot, identity: ProcessIdentity, *, cleanup_deadline_at: float
) -> tuple[bool, tuple[str, ...]]:
    errors: list[str] = []
    unresolved = False
    for pid, entry in snapshot.entries.items():
        try:
            _require_cleanup_time(cleanup_deadline_at, "reap candidate inspection")
        except CleanupDeadlineExceeded as error:
            errors.append(_bounded_lifecycle_error(error, stage="reap_deadline"))
            unresolved = True
            break
        if (
            pid == identity.pid
            or
            entry.parent_pid != os.getpid()
            or entry.process_group != identity.process_group
            or entry.session_id != identity.session_id
            or entry.state != "Z"
        ):
            continue
        try:
            _require_cleanup_time(cleanup_deadline_at, "waitpid WNOHANG")
            waited, _status = os.waitpid(pid, os.WNOHANG)
            if waited == 0:
                unresolved = True
        except ChildProcessError:
            unresolved = True
        except CleanupDeadlineExceeded as error:
            errors.append(_bounded_lifecycle_error(error, stage="reap_deadline"))
            unresolved = True
            break
        except OSError as error:
            errors.append(_bounded_lifecycle_error(error, stage="waitpid_wnohang"))
            unresolved = True
    for pid, entry in snapshot.entries.items():
        if time.monotonic() >= cleanup_deadline_at:
            errors.append("reap_deadline:exhausted_before_remaining_member_check")
            unresolved = True
            break
        if (
            pid != identity.pid
            and entry.process_group == identity.process_group
            and entry.session_id == identity.session_id
            and entry.state == "Z"
            and entry.parent_pid != os.getpid()
        ):
            unresolved = True
    return not unresolved, tuple(errors[:_STRICT_PROC_ERROR_SAMPLE_MAX])


def stop_http_owned_group(
    process: subprocess.Popen[bytes],
    identity: ProcessIdentity,
    *,
    cleanup_deadline_at: float,
    term_grace_s: float = 0.75,
) -> StopOutcome:
    """Stop only a previously bound HTTP session, within one absolute deadline.

    Signals use pidfds for individually revalidated members rather than PID
    guesses or process-name matching. Empty snapshots are accepted only when
    complete; the resulting quiescence is scoped to the bound visible group.
    """
    errors: list[str] = list(identity.acquisition_errors[:_STRICT_PROC_ERROR_SAMPLE_MAX])
    if not identity.bound or process.pid != identity.pid:
        return StopOutcome(
            identity_state=identity.acquisition_state,
            stop_requested="UNKNOWN",
            exit_confirmed="UNKNOWN",
            snapshot_complete="UNKNOWN",
            owned_group_quiescent="UNKNOWN",
            cleanup_errors=tuple(errors),
            primary_error="spawn_identity_not_bound",
        )
    if not math.isfinite(cleanup_deadline_at) or time.monotonic() >= cleanup_deadline_at:
        return StopOutcome(
            identity_state="BOUND",
            stop_requested="NOT_REQUESTED_DEADLINE_EXHAUSTED",
            exit_confirmed="UNKNOWN",
            snapshot_complete="UNKNOWN",
            owned_group_quiescent="UNKNOWN",
            cleanup_errors=tuple(errors),
            primary_error="cleanup_deadline_exhausted_before_stop",
        )

    term_sent = 0
    kill_sent = 0
    vanished_total = 0
    last_live: int | None = None
    snapshot_state = "CONFIRMED"
    stop_state = "NOT_NEEDED"
    quiet_streak = 0
    kill_requested = False
    last_reap_status = "UNKNOWN"
    term_deadline = min(cleanup_deadline_at, time.monotonic() + max(0.0, term_grace_s))

    while time.monotonic() < cleanup_deadline_at:
        try:
            snapshot = strict_proc_snapshot(cleanup_deadline_at=cleanup_deadline_at)
        except CleanupDeadlineExceeded as error:
            snapshot = ProcSnapshot(entries={}, complete=False, error_count=1,
                                    errors=(_bounded_lifecycle_error(error, stage="proc_deadline"),))
        vanished_total += snapshot.vanished_count
        if not snapshot.complete:
            snapshot_state = "UNKNOWN"
            errors.extend(snapshot.errors)
            break
        if time.monotonic() >= cleanup_deadline_at:
            snapshot_state = "UNKNOWN"
            errors.append("cleanup_deadline_exhausted_after_proc_snapshot")
            break
        mismatch = _identity_mismatch(snapshot, identity)
        if mismatch is not None:
            snapshot_state = "FAILED"
            errors.append(mismatch)
            break
        leader_present = identity.pid in snapshot.entries
        if not leader_present and process.returncode is None:
            # No new identity is adopted if the owned leader disappeared.
            if process.poll() is None:
                snapshot_state = "UNKNOWN"
                errors.append("bound_leader_absent_but_popen_exit_unconfirmed")
                break
        members = [
            (pid, entry)
            for pid, entry in snapshot.entries.items()
            if entry.process_group == identity.process_group
            and entry.session_id == identity.session_id
            and entry.state != "Z"
        ]
        last_live = len(members)
        if not members:
            if time.monotonic() >= cleanup_deadline_at:
                snapshot_state = "UNKNOWN"
                errors.append("cleanup_deadline_exhausted_before_reap")
                break
            reap_ok, reap_errors = _reap_visible_adopted(
                snapshot, identity, cleanup_deadline_at=cleanup_deadline_at
            )
            errors.extend(reap_errors)
            if time.monotonic() >= cleanup_deadline_at:
                snapshot_state = "UNKNOWN"
                errors.append("cleanup_deadline_exhausted_after_reap")
                break
            if reap_ok:
                quiet_streak += 1
                last_reap_status = (
                    "PENDING_POPEN_REAP"
                    if (identity.pid in snapshot.entries and snapshot.entries[identity.pid].state == "Z")
                    else "CONFIRMED"
                )
            else:
                quiet_streak = 0
                last_reap_status = "UNKNOWN"
                return StopOutcome(
                    identity_state="BOUND",
                    stop_requested=stop_state,
                    exit_confirmed="PENDING_POPEN_REAP",
                    snapshot_complete=snapshot_state,
                    owned_group_quiescent="CONFIRMED_FOR_BOUND_GROUP",
                    reap_status="UNKNOWN",
                    term_signals_sent=term_sent,
                    kill_signals_sent=kill_sent,
                    vanished_during_scan=vanished_total,
                    last_live_member_count=0,
                    cleanup_errors=tuple(errors[:_STRICT_PROC_ERROR_SAMPLE_MAX]),
                    primary_error="unreaped_nonleader_zombie_or_waitpid_error",
                )
            if quiet_streak >= 2:
                return StopOutcome(
                    identity_state="BOUND",
                    stop_requested=stop_state,
                    exit_confirmed="PENDING_POPEN_REAP",
                    snapshot_complete=snapshot_state,
                    owned_group_quiescent="CONFIRMED_FOR_BOUND_GROUP",
                    reap_status=last_reap_status,
                    term_signals_sent=term_sent,
                    kill_signals_sent=kill_sent,
                    vanished_during_scan=vanished_total,
                    last_live_member_count=0,
                    cleanup_errors=tuple(errors[:_STRICT_PROC_ERROR_SAMPLE_MAX]),
                )
            remaining = cleanup_deadline_at - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.01, remaining))
            continue

        quiet_streak = 0
        if stop_state == "NOT_NEEDED":
            sig = signal.SIGTERM
            stop_state = "TERM_REQUESTED"
        elif time.monotonic() >= term_deadline and not kill_requested:
            sig = signal.SIGKILL
            stop_state = "KILL_REQUESTED"
            kill_requested = True
        else:
            next_deadline = term_deadline if not kill_requested else cleanup_deadline_at
            remaining = min(next_deadline, cleanup_deadline_at) - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.02, remaining))
            continue
        sent, vanished, signal_errors, signal_deadline_exhausted = _signal_snapshot_members(
            snapshot, identity, sig, cleanup_deadline_at=cleanup_deadline_at
        )
        vanished_total += vanished
        if len(errors) < _STRICT_PROC_ERROR_SAMPLE_MAX:
            errors.extend(signal_errors[: _STRICT_PROC_ERROR_SAMPLE_MAX - len(errors)])
        if sig == signal.SIGTERM:
            term_sent += sent
        else:
            kill_sent += sent
        if signal_errors or signal_deadline_exhausted:
            snapshot_state = "UNKNOWN"
        remaining = cleanup_deadline_at - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.01, remaining))

    return StopOutcome(
        identity_state="BOUND",
        stop_requested=stop_state if stop_state != "NOT_NEEDED" else "NOT_REQUESTED",
        exit_confirmed="UNKNOWN",
        snapshot_complete=snapshot_state,
        owned_group_quiescent="UNKNOWN",
        reap_status=last_reap_status,
        term_signals_sent=term_sent,
        kill_signals_sent=kill_sent,
        vanished_during_scan=vanished_total,
        last_live_member_count=last_live,
        cleanup_errors=tuple(errors[:_STRICT_PROC_ERROR_SAMPLE_MAX]),
        primary_error="cleanup_deadline_or_snapshot_not_closed",
    )
