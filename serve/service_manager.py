"""Fail-closed Linux llama.cpp supervision and byte-transparent call recorder.

The example asset file is intentionally incomplete.  No executable or model
service is started until a separately verified Linux/HIP asset manifest is
provided.  This module is not run by the static-only source correction phase.
"""

from __future__ import annotations

import argparse
import contextlib
import math
import hashlib
try:
    import fcntl
except ImportError:  # pragma: no cover - runtime manager requires Linux
    fcntl = None  # type: ignore[assignment]
import http.client
import json
import os
import platform
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Mapping

from serve.package_identity import validate_preinstalled_package

from official_entry.process_supervision import (
    enable_child_subreaper, live_process_group_members, stop_owned_group,
    stop_owned_process_group,
)


PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ASSETS = PACKAGE_ROOT / "serve" / "runtime_assets.json"
_proxy_forward_lock = threading.Lock()
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime asset manifest must be a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_file(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError("{} must be an explicit absolute path".format(label))
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError("{} is unavailable".format(label))
    return path


def _verify_digest(path: Path, expected: Any, label: str) -> None:
    if not isinstance(expected, str) or len(expected) != 64 or _sha256_file(path) != expected.lower():
        raise ValueError("{} SHA-256 is missing or mismatched".format(label))


def validate_runtime_assets(assets: Mapping[str, Any], *, verify_hashes: bool = True) -> dict[str, Any]:
    """Validate the supplied model, build, backend and loopback identities."""
    cpp = assets.get("llama_cpp")
    proxy = assets.get("proxy")
    if not isinstance(cpp, dict) or not isinstance(proxy, dict):
        raise ValueError("llama_cpp and proxy sections are required")
    binary = _absolute_file(cpp.get("binary_path"), "Linux llama-server binary")
    binary_sha = cpp.get("binary_sha256")
    model_sha = cpp.get("model_sha256")
    build_sha = cpp.get("build_record_sha256")
    for value, label in ((binary_sha, "llama-server binary"), (model_sha, "GGUF model"), (build_sha, "build record")):
        if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdefABCDEF" for character in value):
            raise ValueError("{} SHA-256 must be a 64-digit hexadecimal value".format(label))
    if verify_hashes:
        _verify_digest(binary, binary_sha, "llama-server binary")
    model = _absolute_file(cpp.get("model_path"), "GGUF model")
    if verify_hashes:
        _verify_digest(model, model_sha, "GGUF model")
    build_record = _absolute_file(cpp.get("build_record_path"), "llama.cpp build record")
    if verify_hashes:
        _verify_digest(build_record, build_sha, "build record")
    commit = cpp.get("source_commit")
    version = cpp.get("version")
    hip_version = cpp.get("hip_rocm_version")
    target_gfx = cpp.get("target_gfx")
    cpu_arch = cpp.get("cpu_architecture")
    argv = cpp.get("argv")
    alias = cpp.get("alias")
    context = cpp.get("context_tokens")
    identity_kind = cpp.get("identity_kind", "source_build")
    if identity_kind not in ("source_build", "preinstalled_package"):
        raise ValueError("unknown engine identity kind")
    if identity_kind == "source_build" and (not isinstance(commit, str) or len(commit) != 40):
        raise ValueError("llama.cpp full source commit is required")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("llama.cpp version/build identity is required")
    if identity_kind == "source_build" and (not isinstance(hip_version, str) or not hip_version.strip()):
        raise ValueError("HIP/ROCm build identity is required")
    if cpp.get("backend") != "HIP":
        raise ValueError("production backend must be explicitly HIP")
    if target_gfx not in ("gfx1100", "gfx1201"):
        raise ValueError("a supported target gfx identity must be explicitly selected")
    if cpu_arch not in ("x86_64", "aarch64") or platform.machine() != cpu_arch:
        raise ValueError("Linux CPU architecture must be explicitly declared and match this runtime")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ValueError("version-specific llama-server argv is not configured")
    if not isinstance(alias, str) or not alias.strip() or isinstance(context, bool) or not isinstance(context, int) or context <= 0:
        raise ValueError("model alias and positive context size are required")
    required_values = (str(model), alias, str(context), "127.0.0.1", "18081")
    if any(value not in argv for value in required_values):
        raise ValueError("llama-server argv must explicitly bind the configured model, alias, context and loopback port")
    try:
        build = _load_json(build_record)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("llama.cpp build record must be a readable JSON identity record") from error
    expected_build = {
        "source_commit": commit,
        "version": version,
        "backend": "HIP",
        "hip_rocm_version": hip_version,
        "cpu_architecture": cpu_arch,
        "target_gfx": target_gfx,
    }
    if identity_kind == "preinstalled_package":
        validate_preinstalled_package(cpp, build, verify_hashes=verify_hashes)
    elif any(build.get(key) != value for key, value in expected_build.items()):
        raise ValueError("llama.cpp build record fields do not match the asset declaration")
    proxy_host = proxy.get("listen_host")
    proxy_port = proxy.get("listen_port")
    upstream = proxy.get("upstream_base_url")
    if proxy_host != "127.0.0.1" or proxy_port != 18080:
        raise ValueError("public model recorder must bind only to 127.0.0.1:18080")
    if upstream != "http://127.0.0.1:18081/v1":
        raise ValueError("internal llama.cpp endpoint must be 127.0.0.1:18081/v1")
    return {"binary": binary, "model": model, "build_record": build_record, "alias": alias, "context_tokens": context, "cpu_architecture": cpu_arch, "target_gfx": target_gfx}


def _process_owner_identity(pid: int) -> dict[str, Any] | None:
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text(encoding="ascii")
        tail = stat[stat.rfind(")") + 2 :].split()
        return {"pid": pid, "start_ticks": int(tail[19])}
    except (OSError, ValueError, IndexError):
        return None


def _read_context(path: Path) -> dict[str, Any]:
    lock_path = path.with_name(path.name + ".lock")
    fd = None
    held_by_writer = False
    try:
        fd = os.open(str(lock_path), os.O_RDWR)
        if fcntl is None:
            return {"association_status": "unverified", "record_id": None, "lifecycle": "unverified"}
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return {"association_status": "unverified", "record_id": None, "lifecycle": "unverified"}
        except BlockingIOError:
            held_by_writer = True
        value = _load_json(path)
        record_id = value.get("record_id")
        owner_pid = value.get("owner_pid")
        owner_start_ticks = value.get("owner_start_ticks")
        if (
            value.get("active") is not True
            or value.get("lifecycle") != "active"
            or not isinstance(record_id, str)
            or not record_id
            or isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or isinstance(owner_start_ticks, bool)
            or not isinstance(owner_start_ticks, int)
        ):
            return {"association_status": "unverified", "record_id": None, "lifecycle": "unverified"}
        current = _process_owner_identity(owner_pid)
        if not current or current.get("start_ticks") != owner_start_ticks:
            return {"association_status": "unverified", "record_id": None, "lifecycle": "unverified"}
        # Close the race where the lease exits after the first LOCK_SH attempt.
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            held_by_writer = False
        except BlockingIOError:
            held_by_writer = True
        if not held_by_writer:
            return {"association_status": "unverified", "record_id": None, "lifecycle": "unverified"}
        return value
    except (OSError, ValueError, json.JSONDecodeError):
        return {"association_status": "unverified", "record_id": None, "lifecycle": "unverified"}
    finally:
        if fd is not None:
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".{}.{}.{}.tmp".format(path.name, os.getpid(), secrets.token_hex(6)))
    fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
        _fsync_directory(path.parent)
    except BaseException:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _acquire_flock(path: Path, deadline: float) -> int:
    if fcntl is None:
        raise RuntimeError("kernel-held flock requires Linux")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(0.02, remaining))
        raise TimeoutError("kernel-held association/control lock exceeded its deadline")
    except BaseException:
        os.close(fd)
        raise


def _release_flock(fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _held_flock(path: Path, deadline: float):
    fd = _acquire_flock(path, deadline)
    try:
        yield
    finally:
        _release_flock(fd)


class RecordContextLease:
    """Kernel-held side-channel lease; nonce is never added to model JSON."""

    def __init__(self, assets_path: Path, context: Mapping[str, Any], deadline: float) -> None:
        self.assets_path = Path(assets_path)
        self.context = dict(context)
        self.deadline = deadline
        self.context_path: Path | None = None
        self.lock_path: Path | None = None
        self.lock_fd: int | None = None
        self.record_id = secrets.token_hex(16)
        self.owner_identity: dict[str, Any] | None = None
        self.acquired = False

    def __enter__(self) -> "RecordContextLease":
        assets = _load_json(self.assets_path)
        proxy = assets.get("proxy", {})
        context_value = proxy.get("context_path")
        if not isinstance(context_value, str) or not Path(context_value).is_absolute():
            raise ValueError("proxy context_path must be an absolute runtime path")
        self.context_path = Path(context_value)
        self.lock_path = self.context_path.with_name(self.context_path.name + ".lock")
        self.context_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_fd = _acquire_flock(self.lock_path, self.deadline)
        self.acquired = True
        try:
            # A stale context is made unavailable before the replacement is
            # written; the persistent lock inode itself is never unlinked.
            try:
                self.context_path.unlink()
                _fsync_directory(self.context_path.parent)
            except FileNotFoundError:
                pass
            self.owner_identity = _process_owner_identity(os.getpid())
            if self.owner_identity is None:
                raise RuntimeError("request owner PID/start identity could not be read")
            payload = {
                **self.context,
                "active": True,
                "lifecycle": "active",
                "record_id": self.record_id,
                "owner_pid": self.owner_identity["pid"],
                "owner_start_ticks": self.owner_identity["start_ticks"],
                "association_status": self.context.get("association_status", "verified_side_channel"),
            }
            _atomic_json(self.context_path, payload)
        except BaseException:
            self._release()
            raise
        return self

    def _release(self) -> None:
        if self.lock_fd is not None:
            fd, self.lock_fd = self.lock_fd, None
            _release_flock(fd)
        self.acquired = False

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if self.acquired and self.context_path is not None and self.owner_identity is not None:
                try:
                    current = _load_json(self.context_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    current = {}
                if (
                    current.get("record_id") == self.record_id
                    and current.get("owner_pid") == self.owner_identity["pid"]
                    and current.get("owner_start_ticks") == self.owner_identity["start_ticks"]
                ):
                    try:
                        self.context_path.unlink()
                        _fsync_directory(self.context_path.parent)
                    except FileNotFoundError:
                        pass
        finally:
            self._release()


def _normalize_upstream_target(upstream_base_url: str, target: str) -> str:
    endpoint = urlsplit(upstream_base_url)
    incoming = urlsplit(target)
    if endpoint.path.rstrip("/") != "/v1" or endpoint.query or endpoint.fragment:
        raise ValueError("upstream_base_url must end in the single /v1 API prefix")
    if incoming.fragment or not incoming.path.startswith("/v1/") or incoming.path.startswith("/v1/v1/"):
        raise ValueError("request target must contain exactly one absolute /v1 API prefix")
    suffix = incoming.path[len("/v1"):]
    normalized = endpoint.path.rstrip("/") + suffix
    if incoming.query:
        normalized += "?" + incoming.query
    return normalized


def _record_call(
    assets: Mapping[str, Any], method: str, target: str,
    request_headers: Mapping[str, str], request_body: bytes,
    context: Mapping[str, Any],
) -> tuple[int, str, list[tuple[str, str]], bytes, dict[str, Any]]:
    proxy = assets["proxy"]
    upstream = str(proxy["upstream_base_url"]).rstrip("/")
    endpoint = urlsplit(upstream)
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.port != 18081:
        raise ValueError("only the configured loopback model upstream is allowed")
    path = _normalize_upstream_target(upstream, target)
    call_id = secrets.token_hex(16)
    evidence_root = Path(str(proxy["evidence_dir"]))
    if not evidence_root.is_absolute():
        raise ValueError("proxy evidence_dir must be an absolute runtime path")
    call_dir = evidence_root / call_id
    call_dir.mkdir(parents=True, exist_ok=False)
    _fsync_directory(evidence_root)
    request_sha = hashlib.sha256(request_body).hexdigest()
    request_path = call_dir / "request_body.bin"
    record_path = call_dir / "record.json"
    response_path = call_dir / "response_body.bin"
    _atomic_write_bytes(request_path, request_body)
    event: dict[str, Any] = {
        "record_id": context.get("record_id"), "call_id": call_id,
        "association_status": context.get("association_status", "unverified"),
        "task_id": context.get("task_id"), "nonce": context.get("nonce"),
        "mode": context.get("mode"), "method": method, "path": target,
        "upstream_path": path,
        "kind": "generation" if method == "POST" and urlsplit(target).path.rstrip("/").endswith("/chat/completions") else "metadata",
        "phase": "prepared", "send_attempted": False, "send_status": "prepared",
        "request_body_sha256": request_sha, "response_body_sha256": None,
        "http_status": None, "started_at_epoch_s": time.time(),
        "authorization_saved": False, "error": None,
    }
    _atomic_json(record_path, event)
    headers = {key: value for key, value in request_headers.items() if key.lower() not in _HOP_BY_HOP and key.lower() != "host"}
    headers["Content-Length"] = str(len(request_body))
    response_headers: list[tuple[str, str]] = [("Content-Type", "application/json")]
    deadline = context.get("request_deadline_monotonic")
    timeout_s = float(proxy.get("socket_timeout_s", 300))
    if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
        timeout_s = min(timeout_s, max(0.0, float(deadline) - time.monotonic()))
    if timeout_s <= 0:
        event.update({"phase": "complete", "send_status": "not_sent", "error": "request deadline expired before upstream send", "proxy_status": 504})
        _atomic_json(record_path, event)
        return 504, "Task deadline expired before upstream send", response_headers, b"", event
    conn = http.client.HTTPConnection("127.0.0.1", 18081, timeout=timeout_s)
    try:
        with _proxy_forward_lock:
            remaining = timeout_s
            if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
                remaining = min(remaining, max(0.0, float(deadline) - time.monotonic()))
            if remaining <= 0:
                event.update({"phase": "complete", "send_status": "not_sent", "error": "request deadline expired while queued", "proxy_status": 504})
                _atomic_json(record_path, event)
                return 504, "Task deadline expired before upstream send", response_headers, b"", event
            conn.timeout = remaining
            # Persist may-have-been-sent immediately before the only network call.
            event.update({"phase": "attempted", "send_attempted": True, "send_status": "attempted_uncertain", "send_attempted_at_epoch_s": time.time()})
            _atomic_json(record_path, event)
            remaining = timeout_s
            if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
                remaining = min(remaining, max(0.0, float(deadline) - time.monotonic()))
            if remaining <= 0:
                event.update({"phase": "complete", "send_status": "not_sent", "send_attempted": False, "error": "deadline expired after attempted was persisted but before network call", "proxy_status": 504})
                _atomic_json(record_path, event)
                return 504, "Task deadline expired before upstream send", response_headers, b"", event
            conn.timeout = remaining
            response = None
            try:
                conn.request(method, path, body=request_body, headers=headers)
                response = conn.getresponse()
                response_body = response.read()
                status = response.status
                reason = response.reason
                response_headers = [(key, value) for (key, value) in response.getheaders() if key.lower() not in _HOP_BY_HOP and key.lower() != "content-length"]
                _atomic_write_bytes(response_path, response_body)
                event.update({"phase": "received", "send_status": "response_received", "response_body_sha256": hashlib.sha256(response_body).hexdigest(), "http_status": status, "response_received_at_epoch_s": time.time(), "error": None, "proxy_status": status})
                _atomic_json(record_path, event)
                return status, reason, response_headers, response_body, event
            except Exception as error:
                event.update({"phase": "complete", "send_status": "attempted_uncertain", "error": "{}: {}".format(type(error).__name__, str(error)[:240]), "proxy_status": 502})
                try:
                    _atomic_json(record_path, event)
                except OSError as evidence_error:
                    raise OSError("upstream outcome uncertain and durable event update failed: {}".format(type(evidence_error).__name__)) from error
                return 502, "Upstream response unavailable; request was not retried", response_headers, b"", event
    finally:
        conn.close()

class RecorderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def do_HEAD(self) -> None:  # noqa: N802
        self._forward()

    def _forward(self) -> None:
        if self.path == "/_record/health":
            self._respond(200, "OK", [("Content-Type", "application/json")], b'{"ready":true}')
            return
        if not self.path.startswith("/v1/"):
            self._respond(404, "Not Found", [("Content-Type", "application/json")], b'{"error":"not found"}')
            return
        limit = int(self.server.assets["proxy"].get("max_body_bytes", 64 * 1024 * 1024))
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._respond(400, "Bad Request", [], b"")
            return
        if length < 0 or length > limit:
            self._respond(413, "Payload Too Large", [], b"")
            return
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._respond(400, "Bad Request", [], b"")
            return
        context_path = Path(str(self.server.assets["proxy"]["context_path"]))
        context = _read_context(context_path)
        try:
            status, reason, headers, response_body, _event = _record_call(
                self.server.assets,
                self.command,
                self.path,
                {key: value for key, value in self.headers.items()},
                body,
                context,
            )
        except Exception as error:
            # A failure before an upstream send is a local recorder failure;
            # never repair it by issuing a second model request.
            detail = "{}: {}".format(type(error).__name__, str(error)[:200]).encode("utf-8")
            self._respond(507, "Model call evidence unavailable", [("Content-Type", "text/plain; charset=utf-8")], detail)
            return
        self._respond(status, reason, headers, response_body, suppress_body=self.command == "HEAD")

    def _respond(self, status: int, reason: str, headers: list[tuple[str, str]], body: bytes, suppress_body: bool = False) -> None:
        self.send_response(status, reason)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if not suppress_body:
            try:
                self.wfile.write(body)
            except OSError:
                pass

    def log_message(self, format: str, *args: object) -> None:
        return None


class RecorderServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], assets: dict[str, Any]) -> None:
        super().__init__(address, RecorderHandler)
        self.assets = assets


def _proc_identity(pid: int) -> dict[str, Any] | None:
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text(encoding="ascii")
        tail = stat[stat.rfind(")") + 2 :].split()
        identity: dict[str, Any] = {
            "pid": pid, "state": tail[0], "process_group": int(tail[2]),
            "session_id": int(tail[3]), "start_ticks": int(tail[19]),
            "executable": None, "cmdline_sha256": None,
        }
    except (OSError, ValueError, IndexError):
        return None
    try:
        identity["executable"] = os.path.realpath(str(proc / "exe"))
        identity["cmdline_sha256"] = hashlib.sha256((proc / "cmdline").read_bytes()).hexdigest()
    except OSError:
        # Zombie leaders still have an authoritative stat identity and can
        # anchor cleanup of descendants in their recorded PGID/SID.
        pass
    return identity


def _owned_state_or_zombie_matches(state: Mapping[str, Any], expected_executable: Path) -> bool:
    pid = state.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    current = _proc_identity(pid)
    if current is None:
        return False
    if current.get("state") == "Z":
        return bool(
            current.get("start_ticks") == state.get("start_ticks")
            and current.get("process_group") == state.get("process_group") == pid
            and current.get("session_id") == state.get("session_id") == pid
        )
    return _owned_state_matches(state, expected_executable)


def _owned_state_matches(state: Mapping[str, Any], expected_executable: Path) -> bool:
    pid = state.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    current = _proc_identity(pid)
    return bool(
        current
        and current["executable"] == os.path.realpath(str(expected_executable))
        and current["start_ticks"] == state.get("start_ticks")
        and current["cmdline_sha256"] == state.get("cmdline_sha256")
        and current["process_group"] == state.get("process_group") == pid
        and current["session_id"] == state.get("session_id") == pid
        and state.get("command_sha256")
    )

def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _model_probe(url: str, timeout: float) -> tuple[str | None, str]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/models", timeout=max(0.001, timeout)) as response:
            if response.status != 200:
                return None, "probe_error"
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict) and isinstance(rows[0].get("id"), str):
            return rows[0]["id"], "ok"
        return None, "probe_error"
    except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError, TimeoutError):
        return None, "not_ready"


def _models(url: str, timeout: float = 1.0) -> str | None:
    alias, _status = _model_probe(url, timeout)
    return alias


def _read_actual_gfx_probe() -> dict[str, Any]:
    """Read the upstream KFD split-file layout; never infer gfx from ASIC name."""
    root = Path("/sys/class/kfd/kfd/topology/nodes")
    if not root.is_dir():
        return {"status": "probe_error", "targets": [], "reason": "KFD topology nodes are unavailable"}
    gpu_nodes = 0
    targets: set[str] = set()
    try:
        nodes = sorted((entry for entry in root.iterdir() if entry.is_dir()), key=lambda path: path.name)
    except OSError as error:
        return {"status": "probe_error", "targets": [], "reason": "KFD nodes could not be enumerated: {}".format(type(error).__name__)}
    if not nodes:
        return {"status": "probe_error", "targets": [], "reason": "KFD topology has no node entries"}
    for node in nodes:
        try:
            gpu_text = (node / "gpu_id").read_text(encoding="ascii").strip()
            gpu_id = int(gpu_text, 10)
        except PermissionError:
            # Container device isolation exposes inaccessible host GPU nodes.
            # Validate all accessible nodes; no accessible GPU still fails below.
            continue
        except (OSError, ValueError):
            return {"status": "probe_error", "targets": sorted(targets), "reason": "KFD gpu_id file is missing or malformed"}
        if gpu_id == 0:
            continue
        if gpu_id < 0:
            return {"status": "probe_error", "targets": sorted(targets), "reason": "KFD gpu_id must be nonnegative"}
        gpu_nodes += 1
        try:
            lines = (node / "properties").read_text(encoding="ascii").splitlines()
        except OSError:
            return {"status": "probe_error", "targets": sorted(targets), "reason": "KFD properties file is unavailable"}
        property_values: dict[str, str] = {}
        for line in lines:
            key, separator, value = line.partition(" ")
            if not separator or not key:
                continue
            if key in property_values:
                return {"status": "probe_error", "targets": sorted(targets), "reason": "KFD property key is duplicated"}
            property_values[key] = value.strip()
        raw_version = property_values.get("gfx_target_version")
        if raw_version is None or not raw_version.isdecimal():
            return {"status": "probe_error", "targets": sorted(targets), "reason": "KFD gfx_target_version is missing or malformed"}
        target = {110000: "gfx1100", 120001: "gfx1201"}.get(int(raw_version, 10))
        if target is None:
            return {"status": "unsupported_target", "targets": sorted(targets), "reason": "KFD gfx_target_version is not an explicitly supported value"}
        targets.add(target)
    if gpu_nodes == 0:
        return {"status": "identity_mismatch", "targets": [], "reason": "KFD topology exposes no GPU node"}
    return {"status": "ok", "targets": sorted(targets), "gpu_nodes": gpu_nodes}


def _actual_gfx_probe(timeout_s: float = 1.0) -> dict[str, Any]:
    """Run the synchronous sysfs reader in a timed, isolated helper process."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(float(timeout_s)) or timeout_s <= 0:
        return {"status": "probe_error", "targets": [], "reason": "KFD probe has no remaining time budget"}
    command = [sys.executable, "-m", "serve.service_manager", "probe-gfx"]
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, cwd=str(PACKAGE_ROOT),
            env=_runtime_child_environment(),
        )
    except OSError as error:
        return {"status": "probe_error", "targets": [], "reason": "KFD probe helper could not start: {}".format(type(error).__name__)}
    try:
        output, _ = process.communicate(timeout=float(timeout_s))
    except subprocess.TimeoutExpired:
        try:
            stop_owned_group(process, process_group=process.pid, grace_s=0.05)
        except Exception as error:
            return {"status": "probe_error", "targets": [], "reason": "KFD probe exceeded its budget and helper cleanup failed: {}".format(type(error).__name__)}
        return {"status": "probe_error", "targets": [], "reason": "KFD probe exceeded its remaining startup budget"}
    if process.returncode != 0:
        return {"status": "probe_error", "targets": [], "reason": "KFD probe helper exited unsuccessfully"}
    try:
        result = json.loads(output.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"status": "probe_error", "targets": [], "reason": "KFD probe helper returned invalid JSON"}
    if not isinstance(result, dict) or result.get("status") not in ("ok", "probe_error", "identity_mismatch", "unsupported_target"):
        return {"status": "probe_error", "targets": [], "reason": "KFD probe helper returned an invalid result envelope"}
    return result


def _vram_probe(timeout_s: float) -> tuple[bool, str]:
    executable = shutil_which("rocm-smi")
    if not executable:
        return False, "probe_error"
    try:
        result = subprocess.run(
            [executable, "--showmeminfo", "vram", "--json"], capture_output=True,
            text=True, timeout=max(0.001, timeout_s), check=False,
        )
        if result.returncode != 0:
            return False, "probe_error"
        payload = json.loads(result.stdout)
        cards = [row for row in payload.values() if isinstance(row, dict)] if isinstance(payload, dict) else []
        if len(cards) != 1:
            return False, "probe_error"
        used = cards[0].get("VRAM Total Used Memory (B)")
        total = cards[0].get("VRAM Total Memory (B)")
        if not all(str(value).strip().isdigit() for value in (used, total)):
            return False, "probe_error"
        return int(total) > 0 and int(used) <= int(total), "ok"
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
        return False, "probe_error"


def _vram_explicit_bytes(timeout_s: float = 1.0) -> bool:
    return _vram_probe(timeout_s)[0]


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _state_path(assets: Mapping[str, Any], service: str) -> Path:
    state_root = assets.get("state_dir")
    if not isinstance(state_root, str) or not Path(state_root).is_absolute():
        raise ValueError("state_dir must be an absolute runtime directory")
    return Path(state_root) / (service + ".json")


def _startup_config(assets: Mapping[str, Any]) -> dict[str, float]:
    config = assets.get("startup")
    if not isinstance(config, dict):
        raise ValueError("explicit finite startup budget configuration is required")
    values = {
        "total_timeout_s": config.get("total_timeout_s"),
        "probe_timeout_cap_s": config.get("probe_timeout_cap_s"),
        "poll_interval_s": config.get("poll_interval_s"),
        "shutdown_grace_s": config.get("shutdown_grace_s"),
    }
    parsed: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError("startup.{} must be a finite positive number".format(key))
        parsed[key] = float(value)
    if (parsed["total_timeout_s"] > 600 or parsed["probe_timeout_cap_s"] > parsed["total_timeout_s"]
            or parsed["poll_interval_s"] > parsed["total_timeout_s"]
            or parsed["shutdown_grace_s"] > min(60.0, parsed["total_timeout_s"])):
        raise ValueError("startup budgets exceed the supported finite range")
    return parsed


def _assets_fingerprint(assets: Mapping[str, Any]) -> str:
    canonical = json.dumps(assets, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_owned_state(
    path: Path, process: subprocess.Popen[bytes], command: list[str], executable: Path,
    extra_state: Mapping[str, Any] | None = None, *, deadline: float,
) -> dict[str, Any]:
    # Never publish an incomplete or unstable live identity as an owned state.
    if path.exists():
        raise RuntimeError("owned state already exists; refusing to overwrite it")
    expected_pid = process.pid
    if isinstance(expected_pid, bool) or not isinstance(expected_pid, int) or expected_pid <= 1:
        raise RuntimeError("new Popen PID is invalid")
    expected_executable = os.path.realpath(str(executable))
    stable_fields = ("pid", "start_ticks", "process_group", "session_id", "executable", "cmdline_sha256")
    extras = dict(extra_state or {})
    if any(key in extras for key in (*stable_fields, "state", "command_sha256")):
        raise ValueError("extra state must not override sampled process identity")
    empty_cmdline_sha256 = hashlib.sha256(b"").hexdigest()
    anchored_start_ticks = None
    anchored_identity = None
    previous_complete = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("started process exited before stable identity publication")
        identity = _proc_identity(expected_pid)
        if process.poll() is not None:
            raise RuntimeError("started process exited during identity sampling")
        complete = isinstance(identity, dict)
        if complete:
            identity = dict(identity)
            for key in ("pid", "start_ticks", "process_group", "session_id"):
                value = identity.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    complete = False
                elif key != "start_ticks" and value != expected_pid:
                    raise RuntimeError("sampled PID/group/session differs from the new Popen identity")
            ticks = identity.get("start_ticks")
            if isinstance(ticks, int) and not isinstance(ticks, bool) and ticks > 0:
                if anchored_start_ticks is not None and ticks != anchored_start_ticks:
                    raise RuntimeError("started process start ticks changed during identity sampling")
                anchored_start_ticks = ticks
            actual_executable = identity.get("executable")
            if not isinstance(actual_executable, str) or not Path(actual_executable).is_absolute():
                complete = False
            elif actual_executable != expected_executable:
                raise RuntimeError("observed process executable differs from the expected real path")
            cmdline_sha256 = identity.get("cmdline_sha256")
            if (not isinstance(cmdline_sha256, str) or len(cmdline_sha256) != 64
                    or any(ch not in "0123456789abcdef" for ch in cmdline_sha256)
                    or cmdline_sha256 == empty_cmdline_sha256):
                complete = False
            state = identity.get("state")
            if state in ("Z", "X", "x"):
                raise RuntimeError("started process is no longer live during identity sampling")
            if not isinstance(state, str) or len(state) != 1:
                complete = False
        if complete:
            sample_key = tuple(identity[key] for key in stable_fields)
            if anchored_identity is not None and sample_key != anchored_identity:
                raise RuntimeError("complete process identity changed during stable sampling")
            anchored_identity = sample_key
            if previous_complete == sample_key:
                if process.poll() is not None:
                    raise RuntimeError("started process exited before owned state publication")
                if time.monotonic() >= deadline:
                    break
                if path.exists():
                    raise RuntimeError("owned state appeared during sampling; refusing overwrite")
                state = {
                    **identity,
                    "command_sha256": hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest(),
                    **extras,
                }
                _atomic_json(path, state)
                return state
            previous_complete = sample_key
        else:
            # An unreadable sample breaks consecutive stability, never the original deadline.
            previous_complete = None
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.01, remaining))
    raise TimeoutError("complete stable process identity was not obtained before startup deadline")


def _complete_group_record(state: Mapping[str, Any], role: str) -> bool:
    pid = state.get("pid")
    if (
        state.get("role") != role
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or state.get("process_group") != pid
        or state.get("session_id") != pid
        or isinstance(state.get("start_ticks"), bool)
        or not isinstance(state.get("start_ticks"), int)
        or not isinstance(state.get("executable"), str)
        or not Path(state["executable"]).is_absolute()
    ):
        return False
    for key in ("cmdline_sha256", "command_sha256"):
        value = state.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
            return False
    return True


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _state_has_live_group(state: Mapping[str, Any]) -> bool:
    pid = state.get("pid")
    group = state.get("process_group")
    session = state.get("session_id")
    if not all(isinstance(value, int) and not isinstance(value, bool) for value in (pid, group, session)):
        return False
    if pid != group or group != session:
        return False
    return bool(live_process_group_members(group, session))


def _remove_state_after_exit(path: Path, state: Mapping[str, Any]) -> None:
    pid = state.get("pid")
    current = _proc_identity(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
    if current and current.get("start_ticks") != state.get("start_ticks"):
        raise RuntimeError("state PID was reused; ownership record retained")
    if current and current.get("state") != "Z":
        raise RuntimeError("state owner is still live; ownership record retained")
    if current is None and isinstance(pid, int) and not isinstance(pid, bool):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise RuntimeError("cannot verify state owner liveness; ownership record retained") from error
        else:
            raise RuntimeError("state owner is live but /proc identity is unreadable; ownership record retained")
    if _state_has_live_group(state):
        raise RuntimeError("owned process group remains live; ownership record retained")
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        pass


def _stop_saved_service_group(
    assets: Mapping[str, Any], name: str, *, grace_s: float,
    owner_guardian_pid: int | None = None,
) -> bool:
    path = _state_path(assets, name)
    if not path.is_file():
        return False
    state = _load_json(path)
    if not _complete_group_record(state, name):
        raise RuntimeError("{} ownership record is incomplete; refusing to signal".format(name))
    if owner_guardian_pid is not None and state.get("owner_guardian_pid") != owner_guardian_pid:
        return False
    pid = state.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise RuntimeError("{} ownership record has no PID".format(name))
    group = state.get("process_group")
    session = state.get("session_id")
    if group != pid or session != pid:
        raise RuntimeError("{} ownership record has no isolated PGID/SID identity".format(name))
    current = _proc_identity(pid)
    if current is not None and not _owned_state_or_zombie_matches(state, Path(str(state.get("executable", "")))):
        raise RuntimeError("{} process identity mismatch; refusing to signal".format(name))
    members = live_process_group_members(group, session)
    if members:
        stop_owned_process_group(
            group, session, leader_pid=pid,
            leader_start_ticks=state.get("start_ticks"),
            grace_s=grace_s, kill_grace_s=max(2.0, grace_s),
        )
    _remove_state_after_exit(path, state)
    return True


def _model_attestation(assets: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    cpp = assets["llama_cpp"]
    binary_stat = Path(identity["binary"]).stat()
    model_stat = Path(identity["model"]).stat()
    return {
        "source_commit": cpp["source_commit"], "version": cpp["version"],
        "binary_sha256": cpp["binary_sha256"].lower(), "binary_size": binary_stat.st_size,
        "binary_mtime_ns": binary_stat.st_mtime_ns,
        "build_record_sha256": cpp["build_record_sha256"].lower(),
        "model_sha256": cpp["model_sha256"].lower(), "model_size": model_stat.st_size,
        "model_mtime_ns": model_stat.st_mtime_ns,
        "target_gfx": identity["target_gfx"], "cpu_architecture": identity["cpu_architecture"],
        "alias": identity["alias"], "context_tokens": identity["context_tokens"],
    }


def _launch_owned_service(
    assets: dict[str, Any], name: str, executable: Path, command: list[str],
    host: str, port: int, ready: Any, *, deadline: float,
    extra_state: Mapping[str, Any] | None = None,
    child_env: Mapping[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    state_path = _state_path(assets, name)
    if state_path.is_file():
        state = _load_json(state_path)
        if not _complete_group_record(state, name):
            raise RuntimeError("{} ownership record is incomplete; refusing duplicate start".format(name))
        pid = state.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise RuntimeError("{} ownership record is unreadable; refusing duplicate start".format(name))
        current = _proc_identity(pid)
        if current is not None and current.get("state") != "Z":
            raise RuntimeError("{} already has a live recorded service; leaving it untouched".format(name))
        if current is None and _pid_exists(pid):
            raise RuntimeError("{} PID is live but its /proc identity is unreadable; refusing duplicate start".format(name))
        if current is not None and (
            current.get("start_ticks") != state.get("start_ticks")
            or current.get("process_group") != pid
            or current.get("session_id") != pid
        ):
            raise RuntimeError("{} zombie PID identity changed; refusing duplicate start".format(name))
        if _state_has_live_group(state):
            raise RuntimeError("{} leader exited but its recorded process group remains; refusing duplicate start".format(name))
        state_path.unlink()
        _fsync_directory(state_path.parent)
    if _port_open(host, port):
        raise RuntimeError("{} port is held by a process not started by this guardian; refusing to signal it".format(name))
    if time.monotonic() >= deadline:
        raise TimeoutError("startup budget expired before {} launch".format(name))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = state_path.with_suffix(".log")
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True, cwd=str(PACKAGE_ROOT),
            env=dict(child_env or _runtime_child_environment()),
        )
    created_state: dict[str, Any] | None = None
    try:
        created_state = _write_owned_state(
            state_path, process, command, executable,
            {"role": name, "owner_guardian_pid": os.getpid(), **dict(extra_state or {})},
            deadline=deadline,
        )
        ready_result = ready(deadline)
        if not ready_result.get("ready"):
            raise RuntimeError("{} startup failed: {}".format(ready_result.get("status", "not_ready"), ready_result.get("reason", "")))
        if time.monotonic() >= deadline:
            raise TimeoutError("{} became ready after the total startup deadline".format(name))
        return process
    except BaseException:
        try:
            stop_owned_group(process, process_group=process.pid, grace_s=0.75)
        finally:
            if created_state is not None:
                _remove_state_after_exit(state_path, created_state)
        raise


def _wait_for_models(
    url: str, alias: str, target_gfx: str, *, deadline: float,
    probe_timeout_cap_s: float, poll_interval_s: float,
) -> dict[str, Any]:
    saw_probe_error = False
    last_status = "slow_start_timeout"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        alias_value, model_status = _model_probe(url, min(probe_timeout_cap_s, remaining))
        if model_status == "probe_error":
            saw_probe_error = True
        if alias_value is not None and alias_value != alias:
            return {"ready": False, "status": "identity_mismatch", "reason": "model alias differs from configured identity"}
        if time.monotonic() >= deadline:
            break
        gfx = _actual_gfx_probe(min(probe_timeout_cap_s, max(0.0, deadline - time.monotonic())))
        if gfx["status"] in ("unsupported_target", "identity_mismatch"):
            return {"ready": False, "status": "identity_mismatch", "reason": gfx["reason"]}
        if gfx["status"] == "probe_error":
            saw_probe_error = True
        elif target_gfx not in gfx["targets"]:
            if gfx["targets"]:
                return {"ready": False, "status": "identity_mismatch", "reason": "visible KFD gfx target differs from configured target"}
            saw_probe_error = True
        if time.monotonic() >= deadline:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        vram_ok, vram_status = _vram_probe(min(probe_timeout_cap_s, remaining))
        if vram_status != "ok":
            saw_probe_error = True
        if (alias_value == alias and gfx["status"] == "ok" and target_gfx in gfx["targets"]
                and vram_ok and time.monotonic() <= deadline):
            return {"ready": True, "status": "ready", "alias": alias_value, "target_gfx": target_gfx, "vram_explicit_bytes": True}
        last_status = "probe_error" if saw_probe_error else "slow_start_timeout"
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval_s, remaining))
    return {"ready": False, "status": last_status, "reason": "startup total budget expired; no retry or restart was attempted"}


def _service_startup(assets_path: Path, startup_deadline_monotonic: float) -> int:
    if (
        isinstance(startup_deadline_monotonic, bool)
        or not isinstance(startup_deadline_monotonic, (int, float))
        or not math.isfinite(startup_deadline_monotonic)
    ):
        raise ValueError("guardian requires a finite absolute monotonic startup deadline")
    deadline = startup_deadline_monotonic
    if time.monotonic() >= deadline:
        raise TimeoutError("startup deadline expired before guardian asset loading")
    assets = _load_json(assets_path)
    startup = _startup_config(assets)
    if time.monotonic() >= deadline:
        raise TimeoutError("startup deadline expired while reading guardian assets")
    status_path = _state_path(assets, "guardian_status")
    service_processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    failure: dict[str, Any] | None = None
    stopping = threading.Event()
    prior_term = signal.getsignal(signal.SIGTERM)
    prior_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stopping.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stopping.set())
    enable_child_subreaper()
    try:
        identity = validate_runtime_assets(assets)
        if time.monotonic() >= deadline:
            raise TimeoutError("startup budget expired while validating configured assets")
        proxy = assets["proxy"]
        model_command = [str(identity["binary"])] + list(assets["llama_cpp"]["argv"])
        model_process = _launch_owned_service(
            assets, "model", identity["binary"], model_command, "127.0.0.1", 18081,
            lambda end: _wait_for_models(
                str(proxy["upstream_base_url"]), str(identity["alias"]), str(identity["target_gfx"]),
                deadline=end, probe_timeout_cap_s=startup["probe_timeout_cap_s"],
                poll_interval_s=startup["poll_interval_s"],
            ),
            deadline=deadline, extra_state=_model_attestation(assets, identity),
            child_env=_runtime_child_environment(),
        )
        service_processes.append(("model", model_process))
        recorder_command = [sys.executable, "-m", "serve.service_manager", "proxy", "--assets", str(assets_path)]
        recorder_process = _launch_owned_service(
            assets, "recorder", Path(sys.executable), recorder_command, "127.0.0.1", 18080,
            lambda end: _proxy_startup_result(end, startup),
            deadline=deadline, child_env=_runtime_child_environment(),
        )
        service_processes.append(("recorder", recorder_process))
        _atomic_json(status_path, {
            "status": "ready", "guardian_pid": os.getpid(),
            "guardian_start_ticks": (_proc_identity(os.getpid()) or {}).get("start_ticks"),
            "asset_fingerprint": _assets_fingerprint(assets),
            "model_pid": model_process.pid, "recorder_pid": recorder_process.pid,
            "ready_at_epoch_s": time.time(),
        })
        while not stopping.wait(0.1):
            exited = [(name, process.returncode) for name, process in service_processes if process.poll() is not None]
            if exited:
                failure = {"status": "failed", "reason": "resident child exited unexpectedly", "children": exited}
                break
        if stopping.is_set() and failure is None:
            failure = {"status": "stopped", "reason": "guardian received a stop signal"}
    except BaseException as error:
        failure = {"status": "failed", "reason": "{}: {}".format(type(error).__name__, str(error)[:300])}
    finally:
        for name, process in reversed(service_processes):
            try:
                stop_owned_group(process, process_group=process.pid, grace_s=startup["shutdown_grace_s"])
                state_path = _state_path(assets, name)
                if state_path.is_file():
                    _remove_state_after_exit(state_path, _load_json(state_path))
            except FileNotFoundError:
                pass
            except Exception as error:
                failure = {"status": "failed", "reason": "{} cleanup failed: {}".format(name, type(error).__name__)}
        if failure is not None:
            try:
                _atomic_json(status_path, {**failure, "guardian_pid": os.getpid(), "finished_at_epoch_s": time.time()})
            except OSError:
                pass
        signal.signal(signal.SIGTERM, prior_term)
        signal.signal(signal.SIGINT, prior_int)
    return 0 if failure and failure.get("status") == "stopped" else (0 if failure is None else 78)


def _guardian_healthy(
    assets: dict[str, Any], state: Mapping[str, Any], identity: Mapping[str, Any],
    *, deadline: float,
) -> bool:
    def probe_budget(cap: float) -> float:
        remaining = deadline - time.monotonic()
        return min(cap, remaining) if remaining > 0 else 0.0

    pid = state.get("pid")
    if not isinstance(pid, int) or not _owned_state_matches(state, Path(sys.executable)):
        return False
    fingerprint = _assets_fingerprint(assets)
    if state.get("asset_fingerprint") != fingerprint:
        return False
    try:
        status = _load_json(_state_path(assets, "guardian_status"))
        model_state = _load_json(_state_path(assets, "model"))
        recorder_state = _load_json(_state_path(assets, "recorder"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    probe_cap = _startup_config(assets)["probe_timeout_cap_s"]
    remaining = probe_budget(probe_cap)
    if remaining <= 0:
        return False
    gfx = _actual_gfx_probe(remaining)
    remaining = probe_budget(probe_cap)
    if remaining <= 0:
        return False
    vram_ok, _vram_state = _vram_probe(remaining)
    remaining = probe_budget(probe_cap)
    if remaining <= 0:
        return False
    alias = _models(str(assets["proxy"]["upstream_base_url"]), timeout=remaining)
    remaining = probe_budget(probe_cap)
    if remaining <= 0:
        return False
    proxy_ok = _proxy_healthy(timeout=remaining)
    return bool(
        status.get("status") == "ready"
        and status.get("guardian_pid") == pid
        and status.get("asset_fingerprint") == fingerprint
        and model_state.get("owner_guardian_pid") == pid
        and recorder_state.get("owner_guardian_pid") == pid
        and _owned_state_matches(model_state, Path(str(identity["binary"])))
        and _owned_state_matches(recorder_state, Path(sys.executable))
        and gfx.get("status") == "ok"
        and identity["target_gfx"] in gfx.get("targets", [])
        and vram_ok
        and proxy_ok
        and alias == identity["alias"]
        and time.monotonic() <= deadline
    )


def start_services(assets_path: Path) -> dict[str, Any]:
    started_at = time.monotonic()
    assets = _load_json(assets_path)
    startup = _startup_config(assets)
    deadline = started_at + startup["total_timeout_s"]
    identity = validate_runtime_assets(assets)
    if os.name != "posix" or not Path("/proc/self/stat").is_file():
        raise RuntimeError("production service manager requires Linux")
    fingerprint = _assets_fingerprint(assets)
    guardian_path = _state_path(assets, "guardian")
    control_lock = guardian_path.with_name(guardian_path.name + ".control.lock")
    with _held_flock(control_lock, deadline):
        if guardian_path.is_file():
            state = _load_json(guardian_path)
            if not _complete_group_record(state, "guardian"):
                raise RuntimeError("guardian ownership record is incomplete; refusing duplicate start")
            pid = state.get("pid")
            current = _proc_identity(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
            if current is not None and current.get("state") != "Z":
                if not _owned_state_matches(state, Path(sys.executable)):
                    raise RuntimeError("live guardian identity mismatch; no service was signalled")
                if state.get("asset_fingerprint") != fingerprint:
                    raise RuntimeError("live guardian uses a different asset declaration; no duplicate started")
                if _guardian_healthy(assets, state, identity, deadline=deadline):
                    return {"ready": True, "status": "existing_guardian", "model_alias": identity["alias"], "target_gfx": identity["target_gfx"], "runtime_ready_is_not_peak_memory_or_official_acceptance": True}
                raise RuntimeError("verified guardian exists but is not healthy; automatic restart is disabled")
            if _state_has_live_group(state):
                raise RuntimeError("guardian leader exited while its isolated process group remains; refusing duplicate start")
            if pid is not None:
                guardian_path.unlink()
                _fsync_directory(guardian_path.parent)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("startup budget expired before guardian launch")
        status_path = _state_path(assets, "guardian_status")
        if status_path.exists():
            status_path.unlink()
            _fsync_directory(status_path.parent)
        command = [sys.executable, "-m", "serve.service_manager", "guardian", "--assets", str(assets_path), "--startup-deadline-monotonic", repr(deadline)]
        state_root = Path(str(assets["state_dir"]))
        state_root.mkdir(parents=True, exist_ok=True)
        with guardian_path.with_suffix(".log").open("ab") as log:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("startup budget expired before guardian launch")
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True, cwd=str(PACKAGE_ROOT),
                env=_runtime_child_environment(),
            )
        owner_state: dict[str, Any] | None = None
        try:
            owner_state = _write_owned_state(
                guardian_path, process, command, Path(sys.executable),
                {"role": "guardian", "asset_fingerprint": fingerprint}, deadline=deadline,
            )
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    try:
                        result = _load_json(_state_path(assets, "guardian_status"))
                    except (OSError, ValueError, json.JSONDecodeError):
                        result = {"status": "failed", "reason": "guardian exited before publishing status"}
                    raise RuntimeError("guardian startup {}: {}".format(result.get("status"), result.get("reason", "")))
                try:
                    status = _load_json(_state_path(assets, "guardian_status"))
                except (OSError, ValueError, json.JSONDecodeError):
                    status = {}
                if status.get("status") == "failed":
                    raise RuntimeError("guardian startup failed: {}".format(status.get("reason", "unknown")))
                if (status.get("status") == "ready" and status.get("asset_fingerprint") == fingerprint
                        and _guardian_healthy(assets, owner_state, identity, deadline=deadline)
                        and time.monotonic() <= deadline):
                    return {"ready": True, "status": "new_guardian", "model_alias": identity["alias"], "target_gfx": identity["target_gfx"], "runtime_ready_is_not_peak_memory_or_official_acceptance": True}
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(startup["poll_interval_s"], remaining))
            raise TimeoutError("guardian did not reach ready before the total startup deadline")
        except BaseException:
            if process.poll() is None:
                stop_owned_group(process, process_group=process.pid, grace_s=startup["shutdown_grace_s"])
            for service in ("recorder", "model"):
                _stop_saved_service_group(
                    assets, service, grace_s=startup["shutdown_grace_s"],
                    owner_guardian_pid=process.pid,
                )
            if owner_state is not None:
                _remove_state_after_exit(guardian_path, owner_state)
            raise


def stop_services(assets_path: Path) -> dict[str, Any]:
    assets = _load_json(assets_path)
    startup = _startup_config(assets)
    deadline = time.monotonic() + max(5.0, startup["shutdown_grace_s"] * 4.0)
    guardian_path = _state_path(assets, "guardian")
    control_lock = guardian_path.with_name(guardian_path.name + ".control.lock")
    stopped: list[str] = []
    guardian_owner_pid: int | None = None
    retained_unmatched: list[str] = []
    with _held_flock(control_lock, deadline):
        if guardian_path.is_file():
            state = _load_json(guardian_path)
            if not _complete_group_record(state, "guardian"):
                raise RuntimeError("guardian ownership record is incomplete; refusing to signal")
            pid = state.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise RuntimeError("guardian ownership record has no PID")
            current = _proc_identity(pid)
            guardian_owner_pid = pid
            if current is not None and not _owned_state_or_zombie_matches(state, Path(sys.executable)):
                raise RuntimeError("guardian identity mismatch; refusing to signal")
            if _state_has_live_group(state):
                stop_owned_process_group(
                    state["process_group"], state["session_id"], leader_pid=pid,
                    leader_start_ticks=state.get("start_ticks"),
                    grace_s=startup["shutdown_grace_s"], kill_grace_s=max(2.0, startup["shutdown_grace_s"] * 2),
                )
            _remove_state_after_exit(guardian_path, state)
            stopped.append("guardian")
        for service in ("recorder", "model"):
            path = _state_path(assets, service)
            existed = path.exists()
            if guardian_owner_pid is None:
                if existed:
                    retained_unmatched.append(service)
                continue
            removed = _stop_saved_service_group(
                assets, service, grace_s=startup["shutdown_grace_s"],
                owner_guardian_pid=guardian_owner_pid,
            )
            if removed:
                stopped.append(service)
            elif existed:
                retained_unmatched.append(service)
        status_path = _state_path(assets, "guardian_status")
        if status_path.exists():
            try:
                status_path.unlink()
                _fsync_directory(status_path.parent)
            except FileNotFoundError:
                pass
    return {"stopped_owned_services": stopped, "retained_unmatched_service_states": retained_unmatched, "unknown_processes_signalled": 0}


def proxy_main(assets_path: Path) -> int:
    assets = _load_json(assets_path)
    proxy = assets.get("proxy", {})
    if proxy.get("listen_host") != "127.0.0.1" or proxy.get("listen_port") != 18080:
        raise ValueError("recording proxy must bind to loopback port 18080")
    server = RecorderServer(("127.0.0.1", 18080), assets)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


def _proxy_healthy(timeout: float) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:18080/_record/health", timeout=max(0.001, timeout)) as response:
            body = json.loads(response.read().decode("utf-8"))
        return response.status == 200 and body.get("ready") is True
    except (OSError, urllib.error.URLError, UnicodeError, json.JSONDecodeError, TimeoutError):
        return False


def _wait_for_proxy(deadline: float, probe_timeout_cap_s: float, poll_interval_s: float) -> bool:
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if _proxy_healthy(timeout=min(probe_timeout_cap_s, remaining)) and time.monotonic() <= deadline:
            return True
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(poll_interval_s, remaining))
    return False


def _proxy_startup_result(deadline: float, startup: Mapping[str, float]) -> dict[str, Any]:
    ready = _wait_for_proxy(deadline, startup["probe_timeout_cap_s"], startup["poll_interval_s"])
    return {"ready": ready, "status": "ready" if ready else "probe_error", "reason": None if ready else "recorder health probe did not succeed"}

def _runtime_child_environment() -> dict[str, str]:
    """Pass runtime loader paths but not HTTP tokens or unrelated credentials."""
    allowed = (
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
        "ROCBLAS_TENSILE_LIBPATH",
        "HSA_OVERRIDE_GFX_VERSION",
        "LANG",
        "LC_ALL",
        "TMPDIR",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def inspect_runtime_attestation(assets_path: Path) -> dict[str, Any]:
    """Read-only readiness check; never starts, restarts, or stops a process."""
    try:
        assets = _load_json(assets_path)
        cpp = assets.get("llama_cpp", {})
        identity = validate_runtime_assets(assets, verify_hashes=False)
        guardian_state = _load_json(_state_path(assets, "guardian"))
        guardian_matches = _owned_state_matches(guardian_state, Path(sys.executable))
        model_state = _load_json(_state_path(assets, "model"))
        recorder_state = _load_json(_state_path(assets, "recorder"))
        binary_stat = identity["binary"].stat()
        model_stat = identity["model"].stat()
        asset_identity_matches = bool(
            guardian_matches
            and guardian_state.get("asset_fingerprint") == _assets_fingerprint(assets)
            and model_state.get("source_commit") == cpp.get("source_commit")
            and model_state.get("binary_sha256") == str(cpp.get("binary_sha256", "")).lower()
            and model_state.get("binary_size") == binary_stat.st_size
            and model_state.get("binary_mtime_ns") == binary_stat.st_mtime_ns
            and model_state.get("build_record_sha256") == str(cpp.get("build_record_sha256", "")).lower()
            and model_state.get("model_sha256") == str(cpp.get("model_sha256", "")).lower()
            and model_state.get("model_size") == model_stat.st_size
            and model_state.get("model_mtime_ns") == model_stat.st_mtime_ns
            and model_state.get("target_gfx") == identity["target_gfx"]
            and model_state.get("cpu_architecture") == identity["cpu_architecture"]
            and model_state.get("alias") == identity["alias"]
            and model_state.get("context_tokens") == identity["context_tokens"]
            and _owned_state_matches(model_state, identity["binary"])
            and _owned_state_matches(recorder_state, Path(sys.executable))
        )
        gfx_probe = _actual_gfx_probe(1.0)
        actual_gfx = sorted(gfx_probe.get("targets", []))
        vram_verified, vram_status = _vram_probe(1.0)
        model_alias = _models("http://127.0.0.1:18080/v1", timeout=1.0)
        ready = bool(
            asset_identity_matches
            and model_alias == identity["alias"]
            and gfx_probe.get("status") == "ok"
            and identity["target_gfx"] in actual_gfx
            and vram_verified
            and _proxy_healthy(timeout=1.0)
        )
        return {
            "ready": ready,
            "status": "verified_local_runtime_identity" if ready else "not_ready",
            "source_commit": cpp.get("source_commit"), "backend": cpp.get("backend"),
            "target_gfx": identity["target_gfx"], "cpu_architecture": identity["cpu_architecture"],
            "visible_gfx": actual_gfx, "kfd_probe_status": gfx_probe.get("status"),
            "alias": model_alias, "asset_identity_matches": asset_identity_matches,
            "explicit_vram_bytes_available": vram_verified, "vram_probe_status": vram_status,
            "scope": "local runtime readiness only; not peak memory or official acceptance",
        }
    except Exception as error:
        return {"ready": False, "status": "unverified", "reason": "{}: {}".format(type(error).__name__, str(error)[:200])}

def main() -> int:
    parser = argparse.ArgumentParser(description="Fail-closed llama.cpp service manager and raw HTTP recorder")
    parser.add_argument("command", choices=("start", "check", "stop", "proxy", "guardian", "probe-gfx"))
    parser.add_argument("--assets", type=Path, default=Path(os.environ.get("RTL_MODEL_LED_ASSETS", str(DEFAULT_ASSETS))))
    parser.add_argument("--startup-deadline-monotonic", type=float)
    args = parser.parse_args()
    if args.command == "guardian" and args.startup_deadline_monotonic is None:
        parser.error("--startup-deadline-monotonic is required for guardian")
    if args.command == "probe-gfx":
        print(json.dumps(_read_actual_gfx_probe(), ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "proxy":
        return proxy_main(args.assets)
    if args.command == "guardian":
        try:
            return _service_startup(args.assets, args.startup_deadline_monotonic)
        except Exception as error:
            print(json.dumps({"ready": False, "reason": "{}: {}".format(type(error).__name__, str(error)[:300])}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
            return 78
    if args.command == "stop":
        try:
            print(json.dumps(stop_services(args.assets), sort_keys=True))
            return 0
        except Exception as error:
            print(json.dumps({"stopped_owned_services": [], "reason": "{}: {}".format(type(error).__name__, str(error)[:300])}, sort_keys=True), file=sys.stderr)
            return 78
    try:
        if args.command == "start":
            state = start_services(args.assets)
        else:
            state = inspect_runtime_attestation(args.assets)
        print(json.dumps(state, ensure_ascii=False, sort_keys=True))
        return 0 if state.get("ready") is True else 78
    except Exception as error:
        print(json.dumps({"ready": False, "reason": "{}: {}".format(type(error).__name__, str(error)[:300])}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 78

if __name__ == "__main__":
    raise SystemExit(main())
