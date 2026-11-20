"""Vivado-only compile/elaboration feedback; never generate or edit a design."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from official_entry.process_supervision import stop_owned_tree


def _find_tool(name: str) -> str | None:
    directory = os.environ.get("VIVADO_BIN")
    if not directory and os.environ.get("XILINX_VIVADO"):
        directory = str(Path(os.environ["XILINX_VIVADO"]) / "bin")
    if directory:
        for suffix in ((".bat", ".exe", "") if os.name == "nt" else ("",)):
            path = Path(directory) / (name + suffix)
            if path.is_file():
                return str(path.resolve())
        return None
    return shutil.which(name)


def compile_rtl(code: str, out_dir: Path, *, timeout_s: float) -> dict[str, Any]:
    """Share one bounded budget across xvlog and xelab, retaining raw logs."""
    started = time.monotonic()
    deadline = started + max(0.0, timeout_s)
    tools = {name: _find_tool(name) for name in ("xvlog", "xelab")}
    missing = [name for name, path in tools.items() if not path]
    if missing:
        return {"status": "unavailable", "reason": "missing Vivado tools: " + ", ".join(missing), "stages": []}
    syntax = re.sub(r"/\*.*?\*/|//[^\n]*", " ", code, flags=re.DOTALL)
    module = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)\b", syntax)
    if module is None:
        return {"status": "fail", "reason": "no module name for elaboration", "stages": []}
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="compile-", dir=out_dir)).resolve()
    (work / "candidate.sv").write_text(code, encoding="utf-8")
    commands = [
        ("xvlog", [tools["xvlog"], "-sv", "candidate.sv"]),
        ("xelab", [tools["xelab"], module.group(1), "-s", "compile_check", "-timescale", "1ns/1ps", "-mt", "off"]),
    ]
    stages: list[dict[str, Any]] = []
    result: dict[str, Any] = {"status": "pass", "reason": "", "work_dir": str(work), "stages": stages}
    for name, command in commands:
        # Leave time to terminate owned descendants and persist the result.
        remaining = deadline - time.monotonic() - 5.0
        if remaining <= 0:
            result.update(status="timeout", reason="compile budget exhausted before " + name)
            break
        log_path = work / (name + ".stdout.log")
        entry: dict[str, Any] = {"tool": name, "command": command, "log": str(log_path)}
        stages.append(entry)
        phase_started = time.monotonic()
        try:
            with log_path.open("wb") as log:
                process = subprocess.Popen(command, cwd=work, stdout=log, stderr=subprocess.STDOUT)
                try:
                    entry["rc"] = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    stop_owned_tree(process, grace_s=0.25)
                    entry.update(rc=process.returncode, timeout=True)
        except OSError as error:
            result.update(status="unavailable", reason="{}: {}".format(type(error).__name__, str(error)[:500]))
            break
        finally:
            entry["elapsed_s"] = round(time.monotonic() - phase_started, 6)
        with log_path.open("rb") as log:
            size = log.seek(0, os.SEEK_END)
            log.seek(max(0, size - 16384))
            excerpt = log.read().decode("utf-8", errors="replace")
        entry["excerpt"] = excerpt[-6000:]
        if entry.get("timeout"):
            result.update(status="timeout", reason=name + " timed out")
            break
        if entry["rc"] < 0 or re.search(
            r"error while loading shared libraries|symbol lookup error|cannot execute binary file|bad interpreter",
            excerpt, re.I,
        ):
            result.update(status="unavailable", reason=entry["excerpt"])
            break
        if re.search(
            r"LICENSE_ERROR|PART_NOT_LICENSED|failed to (?:check.?out|obtain).*licen[sc]e"
            r"|licen[sc]e checkout fail(?:ed|ure)|could not obtain the necessary license"
            r"|unable to get licensing information|valid license was not found",
            excerpt, re.I,
        ):
            result.update(status="unavailable", reason=entry["excerpt"])
            break
        diagnostic_error = re.search(r"^\s*(?:ERROR|FATAL):", excerpt, re.M)
        if entry["rc"] != 0 and not diagnostic_error:
            result.update(status="unavailable", reason=name + " exited without an HDL error diagnostic: " + entry["excerpt"])
            break
        if diagnostic_error:
            result.update(status="fail", reason=entry["excerpt"])
            break
        if name == "xelab" and "Built simulation snapshot compile_check" not in excerpt:
            result.update(status="unavailable", reason="xelab produced no completed simulation snapshot")
            break
    result["elapsed_s"] = round(time.monotonic() - started, 6)
    return result
