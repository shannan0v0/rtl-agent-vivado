"""Public-specification checks and a generic model review; never produce RTL."""

import re

from official_entry.interface_check import parse_prompt_interface
from agent.public_contract import reset_signal
from agent.state_encoding import check_state_encoding


def check_reset_timing(prompt: str, code: str) -> list[str]:
    """Retain reset checks and add concrete candidate FSM encoding diagnostics.

    This is a narrow textual lint, not a functional proof. Mixed/unspecified
    reset timing and unfamiliar reset names are left for model review.
    """
    issues = check_state_encoding(code)
    if not re.search(r"\bsynchronous\b", prompt, re.I) or re.search(r"\basynchronous\b", prompt, re.I):
        return issues
    interface = parse_prompt_interface(prompt)
    if interface is None:
        return issues
    reset = reset_signal(prompt, interface)
    resets = [reset] if reset else []
    syntax = re.sub(r"/\*.*?\*/|//[^\n]*", " ", code, flags=re.S)
    for sensitivity in re.findall(r"\balways(?:_ff)?\s*@\s*\(([^)]*)\)", syntax):
        for name in resets:
            if re.search(r"\b(?:posedge|negedge)\s+" + re.escape(name) + r"\b", sensitivity):
                issues.append("The public specification requires synchronous reset, but " + name +
                              " is an edge event in a sensitivity list. Reset must affect registers "
                              "only at the specified clock edge; preserve its requested polarity and value.")
    return issues


def review_feedback(issues: list[str], prompt: str = "") -> str:
    if re.search(r"\bBCD\b|binary-coded decimal", prompt, re.I):
        return (
            "Review questions against the ORIGINAL PUBLIC SPECIFICATION only. "
            "Check reset timing and polarity, decimal rollover and the highest digit. "
            "For each upper-digit enable, trace ALL lower digits BEFORE the clock edge. "
            "Distinguish a current carry permission from a stored indication of a past update: "
            "registering an enable adds a clock of latency. Do not add that latency unless requested. "
            "Derive enable bit mapping and unused bits only from the public text; preserve the interface. "
            "The finite counter self-check may not cover enable outputs; a pass is not evidence "
            "that unchecked outputs are correct. Return one complete module.\n" + "\n".join(issues)
        )
    checks = []
    if re.search(r"\bpriority encoder\b", prompt, re.I):
        checks.append(
            "Priority encoder synthesis: reserve $clog2 for compile-time constants, not runtime "
            "input signals. A simulation pass does not establish synthesizability. Use fixed-bound "
            "combinational position-selection logic; preserve priority and zero-input behavior.")
    if re.search(r"\brotat(?:or|e|es|ing|ion)\b", prompt, re.I):
        checks.append(
            "Rotation: trace LOAD then two consecutive rotations, followed by HOLD. "
            "The output must show the loaded value at the specified edge, and each rotation "
            "must operate on the CURRENT contents, not a stale load-only shadow register. "
            "Check both directions and simultaneous controls against the task's priority. "
            "Each shifted-out bit must return at the opposite end.")
    if re.search(r"\bstart bit\b", prompt, re.I) and re.search(r"\bstop bit\b", prompt, re.I):
        checks.append(
            "Serial receiver: trace one edge per start bit, each payload bit, and stop bit. "
            "The edge detecting start already consumes the start bit; do not require a second "
            "start or accidentally consume the first data bit as start. Derive payload length "
            "and bit order from the task; capture the LAST data bit before publishing the byte. "
            "Counters and saved payload need clocked storage, not combinational self-updates. "
            "Check back-to-back frames, exactly one completion pulse, malformed stop recovery "
            "and reset timing against the stated protocol.")
    if re.search(r"state transition table", prompt, re.I):
        checks.append(
            "State table: compare EVERY next-state entry separately for every input value, "
            "including transitions out of the output-active state. A Moore output depends "
            "on the current state. Reset synchrony is determined by executable sensitivity "
            "lists, not comments. Do not alter table entries while fixing declarations.")
    focused = "\nTask-relevant review questions (not a functional test result):\n" + "\n".join(checks) if checks else ""
    return (
        "Perform an independent behavioral review against the ORIGINAL PUBLIC SPECIFICATION. "
        "A compiler pass checks HDL legality, not correctness. Correct any behavioral mistakes "
        "and return one complete module, even if the existing module compiles. "
        "Check each of these against the stated requirements:\n"
        "1. Combinational versus sequential behavior: do not invent clocks, resets or data-edge triggers.\n"
        "2. Reset synchrony, polarity, value and priority; a comment does not establish the actual behavior.\n"
        "3. Parallel data capture versus serial shifting; all required input bits must reach the outputs. "
        "Avoid extra pipeline cycles introduced by nonblocking assignments reading old register values.\n"
        "4. Bit slices, enables and hold behavior; check which lane each enable controls.\n"
        "5. Arithmetic widths, boundaries, carry propagation and wraparound; enable means a real update condition.\n"
        "6. Every state-table row and output; trace input transitions at successive clock edges.\n"
        "7. Protocol timing: start, each payload bit and stop occupy distinct edges; check bit order, "
        "last-bit capture, one-cycle completion and recovery after malformed traffic.\n"
        "Use these as review questions, never as permission to invent behavior absent from the specification. "
        "Use only the public specification and this candidate. No reference design or official test is available.\n"
        + focused + "\n" + ("Public-spec lint findings:\n" + "\n".join(issues) if issues else
           "No reset-timing conflict was detected by the narrow textual lint; all other behavior remains unverified.")
    )
