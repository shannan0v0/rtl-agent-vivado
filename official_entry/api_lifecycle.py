"""Official API lifecycle: transparent auth, non-cancelling stop, idle restart proof.

Only the module's CLI reads a completed child receipt. Import has no effects.
The existing run_api.sh is the actual detached parent and automatic restarter.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

TERMINAL = frozenset(('done', 'failed', 'expired'))
KNOWN = TERMINAL | {'queued', 'running'}


def process_start_ticks(pid: int) -> str:
    raw = (Path('/proc') / str(pid) / 'stat').read_text()
    return raw[raw.rfind(')') + 2:].split()[19]


def write_state(path: Path, value: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def restart_allowed(receipt: dict, pid: int, start_ticks: str) -> bool:
    return bool(start_ticks and start_ticks.isdecimal()
                and receipt.get('pid') == pid and receipt.get('start_ticks') == start_ticks
                and receipt.get('phase') in ('RUNNING', 'CLOSED')
                and receipt.get('restart_safe') is True
                and receipt.get('jobs_known') is True
                and receipt.get('pending_jobs') == 0 and receipt.get('connections') == 0)


class LifecycleGate:
    """Reuse the exercised admission/actual-registry/connection drain semantics."""
    def __init__(self, entry, publish):
        self.entry = entry
        self.publish = publish
        self.lock = threading.RLock()
        self.closed = False
        self.connections = 0
        self.phase = 'STARTING'

    def __getattr__(self, name):
        return getattr(self.entry, name)

    def snapshot(self):
        with self.lock:
            try:
                manager = self.entry._job_manager
                with manager._lock:
                    states = [r.status for r in manager._records.values()]
                known = all(s in KNOWN for s in states)
                pending = sum(s not in TERMINAL for s in states) if known else None
            except BaseException:
                known, pending = False, None
            if not known:
                self.closed = True
            drained = self.closed and known and pending == 0 and self.connections == 0
            value = dict(phase=self.phase, admission_closed=self.closed, jobs_known=known,
                         pending_jobs=pending, connections=self.connections, drained=drained,
                         restart_safe=known and pending == 0 and self.connections == 0
                         and self.phase in ('RUNNING', 'CLOSED'))
            self.publish(value)
            return value

    def open_connection(self):
        with self.lock:
            self.connections += 1
            return self.snapshot()

    def close_connection(self):
        with self.lock:
            self.connections -= 1
            if self.connections < 0:
                raise RuntimeError('API_CONNECTION_ACCOUNTING_ERROR')
            return self.snapshot()

    def close_admission(self):
        with self.lock:
            self.closed = True
            self.phase = 'DRAINING'
            return self.snapshot()

    def dispatch(self, method, path, headers, body, **kwargs):
        if method.upper() == 'POST' and urlsplit(path).path == '/v1/solve':
            with self.lock:
                auth = self.entry._check_auth(headers)
                if auth is not None:
                    return auth
                self.snapshot()  # Before acceptance; unknown registry closes admission.
                if self.closed:
                    return self.entry._error(503, 'operator maintenance: admission closed')
                result = self.entry.dispatch(method, path, headers, body, **kwargs)
                self.snapshot()  # The unchanged product owns nonce/queue/interface rules.
                return result
        return self.entry.dispatch(method, path, headers, body, **kwargs)


def run(entry, config, server_class, handler_class, request_budget_s):
    pid = os.getpid()
    ticks = process_start_ticks(pid)
    state_root = Path(os.environ.get('RTL_API_STATE_DIR', '/workspace/v4_submission/runtime/run_api'))
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / ('api.' + str(pid) + '.json')
    def publish(value):
        write_state(path, dict(value, pid=pid, start_ticks=ticks))
    gate = LifecycleGate(entry, publish)
    gate.snapshot()  # STARTING is never eligible for an automatic restart.
    stop = threading.Event()
    monitor_failed = threading.Event()

    class Handler(handler_class):
        def setup(self):
            self._tracked = False
            super().setup()
            gate.open_connection()  # Persist non-idle before accepting any request body.
            self._tracked = True

        def finish(self):
            try:
                super().finish()
            finally:
                if self._tracked:
                    gate.close_connection()
                    self._tracked = False

        def do_POST(self):
            with gate.lock:
                if gate.closed:
                    auth = entry._check_auth(dict(self.headers.items()))
                    self._write(auth.status, auth.body) if auth is not None else self._write(503, {'detail': 'admission closed'})
                    return
            super().do_POST()

    server = server_class((config.entry_host, config.entry_port), gate, request_budget_s)
    server.RequestHandlerClass = Handler
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    gate.phase = 'RUNNING'
    gate.snapshot()

    def monitor():
        try:
            while True:
                if stop.is_set():
                    gate.close_admission()
                    if gate.snapshot()['drained']:
                        server.shutdown()
                        return
                else:
                    gate.snapshot()
                time.sleep(.2)
        except BaseException:
            monitor_failed.set()
            with gate.lock:
                gate.closed = True
                gate.phase = 'UNKNOWN_KEEP_SCENE'
            try:
                gate.snapshot()
            except BaseException:
                pass
            # No cancellation, guessed drain, server shutdown, or blind restart.

    watcher = threading.Thread(target=monitor, name='official-api-drain-owner', daemon=True)
    watcher.start()
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        gate.close_admission()
        if monitor_failed.is_set() or not gate.snapshot()['drained']:
            raise RuntimeError('API_DRAIN_UNCONFIRMED_KEEP_SCENE')
        watcher.join(timeout=5)
        if watcher.is_alive():
            raise RuntimeError('API_MONITOR_NOT_CLOSED')
        server.server_close()
        closed = entry.close_job_manager()
        if closed.get('state') != 'closed' or closed.get('worker_alive') is not False:
            raise RuntimeError('API_WORKER_CLOSE_UNCONFIRMED')
        gate.phase = 'CLOSED'
        gate.snapshot()


def main():
    if len(sys.argv) != 4:
        raise SystemExit(2)
    pid, ticks, root = int(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
    try:
        receipt = json.loads((root / ('api.' + str(pid) + '.json')).read_text(encoding='utf-8'))
        allowed = restart_allowed(receipt, pid, ticks)
    except (OSError, ValueError, TypeError):
        allowed = False
    raise SystemExit(0 if allowed else 78)


if __name__ == '__main__':
    main()
