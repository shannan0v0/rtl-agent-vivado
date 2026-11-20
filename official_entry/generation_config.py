"""Root-manifest generation declaration and official-compatible effective fields."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def _reject_constant(value: str) -> None:
    raise ValueError("manifest contains a non-finite JSON constant: " + value)


def _require_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("manifest numbers must be finite")
    if isinstance(value, dict):
        for item in value.values():
            _require_finite(item)
    elif isinstance(value, list):
        for item in value:
            _require_finite(item)


def load_generation() -> tuple[dict[str, Any], dict[str, Any]]:
    """Fail explicitly on invalid declarations; never infer a thinking default.

    Missing generation and null mean no request override. Otherwise generation
    must be an object. Valid fields use the pinned official baseline's filter;
    ignored fields never change sampling, output limits or transport settings.
    """
    path = Path(__file__).resolve().parent.parent / "manifest.json"
    raw = path.read_bytes()
    manifest = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    _require_finite(manifest)
    if not isinstance(manifest, dict):
        raise ValueError("root manifest must be a JSON object")
    gen = manifest.get("generation")
    if gen is None:
        gen = {}
    if not isinstance(gen, dict):
        raise ValueError("manifest.generation must be an object or null")
    out = {}
    kw = gen.get("chat_template_kwargs")
    if isinstance(kw, dict):
        kw = {k: v for k, v in kw.items()
              if isinstance(k, str) and isinstance(v, (bool, int, float, str))}
        if kw:
            out["chat_template_kwargs"] = kw
    effort = gen.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        out["reasoning_effort"] = effort
    summary = {
        "manifest_path": str(path),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "declaration_state": "missing" if "generation" not in manifest else
                             "null" if manifest["generation"] is None else "object",
        "effective_generation": out,
        "policy": "official_f7a8_two_field_filter_no_inferred_defaults",
    }
    return out, summary
