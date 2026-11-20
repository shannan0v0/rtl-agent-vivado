"""Diagnostic guidance only: no generated RTL, task IDs or private test input."""

import json
import re


_HINTS = {
    "VRFC 10-4982": (
        "Check the reported token and enclosing expression for HDL syntax. "
        "When replication is one operand of a larger concatenation, close the "
        "replication before the next comma: {{N{sign_bit}}, value}. "
        "Do not change widths, signedness, shift direction or cycle behavior "
        "to silence a syntax error. Return the complete module, not a patch."
    ),
    "VRFC 10-1280": (
        "A destination assigned in always/initial must be a variable, not a wire/default net. "
        "Use reg (Verilog) or logic (SystemVerilog) for a procedural output, or drive a net "
        "from one internal variable through a continuous assignment. Changing the storage "
        "qualifier does not change the public interface; preserve all port names, directions "
        "and widths and avoid multiple drivers. For ANSI ports declared in the module header, "
        "edit that ORIGINAL header declaration in place. Do not add a second declaration "
        "of the same port in the module body."
        " Generic syntax example: change the header fragment 'output [W-1:0] result' "
        "to 'output reg [W-1:0] result' when it is procedurally assigned. This example "
        "illustrates a declaration only; derive actual names and widths from the task."
    ),
    "VRFC 10-395": (
        "Packed and unpacked dimensions are not interchangeable. A dimension before the "
        "identifier describes packed bits; a dimension after it describes array elements. "
        "Check the declaration and EVERY assignment/access together. If the specification "
        "needs a bit vector, use a consistent packed vector; if it needs an array, index "
        "its elements before assigning packed bits. Preserve widths and cycle behavior."
    ),
    "VRFC 10-800": (
        "An unpacked array or array slice is not a packed concatenation operand. Check "
        "the declaration, select individual packed elements where an array is intended, "
        "or use a consistent packed declaration where a vector is intended. Fix all "
        "related accesses; do not merely cast an incompatible aggregate."
    ),
    "VRFC 10-2989": (
        "Resolve the undeclared identifier against the public specification and local "
        "declarations. Do NOT add public ports, clocks or resets to silence this error. "
        "Declare legitimate internal signals, or remove behavior that depends on inputs "
        "the specification does not supply."
    ),
    "VRFC 10-9336": (
        "An ANSI port declared in the module header cannot be redeclared in the body. "
        "Consolidate its storage qualifier into the ORIGINAL header declaration and remove "
        "the duplicate body declaration. If procedural assignment requires reg/logic, keep "
        "that qualifier in the header; simply deleting all variable qualifiers restores the "
        "previous non-register assignment error. Preserve port names, widths and directions."
    ),
}


def normalize_diagnostics(reason: str, work_dir: str | None = None) -> str:
    if work_dir:
        reason = reason.replace(str(work_dir), "<compile_work>")
    return re.sub(r'(?:[A-Za-z]:)?[\\/][^\s"\]\n]*[\\/]candidate\.sv', 'candidate.sv', reason)


def compiler_feedback(reason: str, *, work_dir: str | None = None, candidate: str = "") -> dict:
    errors = "\n".join(line for line in reason.splitlines() if re.match(r"\s*(?:ERROR|FATAL):", line))
    codes = sorted({family.upper() + " " + number for family, number in
                    re.findall(r"\[([A-Za-z][A-Za-z0-9_]*)\s+(\d+-\d+)\]", errors)})
    hints = [code + ": " + _HINTS[code] for code in codes if code in _HINTS]
    targets = []
    if "VRFC 10-1280" in codes or "VRFC 10-9336" in codes:
        names = set(re.findall(r"non-register\s+['\"]?([A-Za-z_][A-Za-z0-9_$]*)", errors))
        names.update(re.findall(r"redeclaration of ANSI port\s+['\"]([A-Za-z_][A-Za-z0-9_$]*)", errors))
        for name in sorted(names):
            declaration = re.search(r"\boutput\s+(?:(?:wire|reg|logic|signed|unsigned)\s+)*"
                                    r"(?:\[[^\]]+\]\s*)?" + re.escape(name) + r"\b", candidate)
            if declaration:
                targets.append({"identifier": name, "candidate_output_declaration": declaration.group(0)})
    normalized = normalize_diagnostics(reason, work_dir)
    feedback = "Vivado compile/elaboration failed. Compiler diagnostics (irrelevant temporary paths normalized):\n" + normalized
    if hints:
        feedback += "\nError-type repair guidance:\n" + "\n".join(hints)
    if targets:
        feedback += ("\nExact declarations located in THIS candidate (diagnostic context, not generated RTL):\n" +
                     json.dumps(targets, sort_keys=True) +
                     "\nFix the storage qualifier at the original declaration above; do not redeclare "
                     "that output in the body. Preserve the public width and procedural logic.")
    return {"trigger": "compile_failure", "error_codes": codes, "hints": hints,
            "feedback": feedback, "raw_log": reason, "diagnostic_targets": targets,
            "diagnostics_normalized": normalized != reason}


def confirmed_interface_mismatch(check: dict) -> bool:
    expected, actual = check.get("expected"), check.get("actual")
    if check.get("status") != "fail" or expected is None or actual is None:
        return False
    if expected["module"] != actual["module"] or set(expected["ports"]) != set(actual["ports"]):
        return True
    for name, want in expected["ports"].items():
        got = actual["ports"][name]
        if want["direction"] != got["direction"]:
            return True
        if want["width"].isdigit() and got["width"].isdigit() and int(want["width"]) != int(got["width"]):
            return True
    # Symbolic widths are not evaluated by the existing interface parser.
    return False


def interface_feedback(check: dict) -> dict:
    expected, actual = check["expected"], check["actual"]
    missing = sorted(set(expected["ports"]) - set(actual["ports"]))
    extra = sorted(set(actual["ports"]) - set(expected["ports"]))
    ranges = {name: ("scalar" if port["width"] == "1" else
                     "[{}:0]".format(int(port["width"]) - 1))
              for name, port in expected["ports"].items() if port["width"].isdigit()}
    feedback = (
        "The parsed public interface explicitly rejects this candidate: " + check["reason"] +
        "\nMissing ports: " + json.dumps(missing) + "; extra ports: " + json.dumps(extra) +
        "\nRequired and actual interfaces (including names, directions and widths):\n" +
        json.dumps({"expected": expected, "actual": actual}, sort_keys=True) +
        "\nPacked range widths count BOTH endpoints: abs(high-low)+1. Conventional ranges "
        "for the requested widths (preserve any explicitly stated indexing): " + json.dumps(ranges, sort_keys=True) +
        "\nReturn the complete corrected module. Do not invent ports or change the public "
        "interface to make an internal compiler error disappear."
    )
    return {"trigger": "interface_mismatch", "error_codes": [], "hints": [], "feedback": feedback}


def error_history_feedback(history: list[dict]) -> str:
    failed = [item for item in history if item.get("compile_status") == "fail"
              or item.get("interface_status") == "fail" or item.get("public_spec_issues")
              or item.get("functional_status") == "fail"]
    if not failed:
        return ""
    repeated = len({item["candidate_sha256"] for item in failed}) < len(failed)
    retained_guidance = ''
    if any(item.get('functional_status') == 'fail' for item in failed):
        codes = sorted({code for item in failed for code in item.get('error_codes',[]) if code in _HINTS})
        if codes:
            retained_guidance = '\nRetain previous compiler fixes: ' + ' '.join(_HINTS[code] for code in codes)
        # Full counterexamples stay in functional_result/trace, not duplicated in every prompt.
        failed = [{key:item[key] for key in ('round','candidate_sha256','compile_status','error_codes',
                                           'interface_status','public_spec_issues','functional_status') if key in item}
                  for item in failed]
    return (
        "\nCumulative failed-attempt history (diagnostics only):\n" + json.dumps(failed, sort_keys=True) +
        "\nDo not return an unchanged candidate whose hash is listed as failed. Retain fixes "
        "for earlier errors while correcting the new error; undoing the last edit can restore "
        "the first error. If local edits alternate between errors, reconsider the complete "
        "declaration/driver structure against the public specification. Return the complete module."
        + (" A repeated failed candidate was detected; use a different repair approach." if repeated else "")
        + retained_guidance
    )
