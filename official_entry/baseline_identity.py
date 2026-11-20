"""Pinned byte identities for the current public RTL reference snapshot.

This pins the retrieved reference commit for development.  It does not claim
that the event has designated this commit as its final baseline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


UPSTREAM_REPOSITORY = "https://gitee.com/Vickyiii/rtlagent2026"
UPSTREAM_COMMIT = "f7a8bd465c2a820e006c7a00ecc7df468974635b"
BASELINE_PY_SHA256 = "17aaa609d4dbd36a94d1390e39f769394448f8588f3ccdd2bd37db9e34ed05e2"
RUN_BASELINE_SH_SHA256 = "5c46c40d32c0cf4c4e1dc52ae12e60f8396a0deccafaa4e6f69d7316a3482e75"
FINAL_EVENT_DESIGNATION = "pending"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_copies(package_root: Path) -> dict[str, object]:
    """Check the root and compatibility copies against the pinned raw bytes."""
    paths = {
        "baseline.py": BASELINE_PY_SHA256,
        "run_baseline.sh": RUN_BASELINE_SH_SHA256,
        "official_entry/baseline.py": BASELINE_PY_SHA256,
        "official_entry/run_baseline.sh": RUN_BASELINE_SH_SHA256,
    }
    actual: dict[str, str | None] = {}
    for relative, expected in paths.items():
        path = Path(package_root) / relative
        try:
            actual[relative] = sha256_file(path)
        except OSError:
            actual[relative] = None
    return {
        "upstream_commit": UPSTREAM_COMMIT,
        "final_event_designation": FINAL_EVENT_DESIGNATION,
        "expected": paths,
        "actual": actual,
        "all_match": all(actual[name] == expected for name, expected in paths.items()),
    }
