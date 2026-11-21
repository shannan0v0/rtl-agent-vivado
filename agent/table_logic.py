"""Combinational RTL from explicitly bound scalar truth tables and Karnaugh maps."""

import re

from agent.indexed_logic import SCALAR_INTERFACE, _INTRO, _cover, _expression, _matches
from agent.public_tables import parse_public_table
from official_entry.interface_check import parse_prompt_interface


_TRUTH = re.compile(
    r"(?:The module should implement a combinational circuit for the following truth table"
    r"|(?:Combinational )?truth table)\s*[:.]?", re.I,
)
_MAP = re.compile(
    r"(?:The module should implement (?:the circuit described by )?the Karnaugh map below"
    r"|Karnaugh map)\s*[:.]?", re.I,
)
_FREE = re.compile(
    r"\s+d is don['’]t-care, which means you may choose to output whatever value is convenient\.$",
    re.I,
)


def solve_table_logic(prompt, public_input, interface_text=""):
    """Produce (source, exhaustive cell checks) only for a complete standalone specification."""
    if not re.search(r"truth table|Karnaugh map", prompt, re.I):
        return None
    if interface_text.strip() and not SCALAR_INTERFACE.fullmatch(interface_text):
        return None
    intro = _INTRO.match(prompt)
    if not intro and not interface_text.strip():
        return None
    interface = parse_prompt_interface(public_input)
    if not interface or any(p.width != "1" or p.direction not in ("input", "output")
                            for p in interface.ports.values()):
        return None
    inputs = [n for n, p in interface.ports.items() if p.direction == "input"]
    outputs = [n for n, p in interface.ports.items() if p.direction == "output"]
    if not 1 <= len(inputs) <= 4 or not outputs:
        return None

    body = prompt[intro.end():] if intro else prompt
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    first_bar = next((i for i, line in enumerate(lines) if "|" in line), None)
    if first_bar is None:
        return None
    is_map = bool(re.search(r"Karnaugh map", body, re.I))
    first_table_line = first_bar - 2 if is_map else first_bar
    if first_table_line < 1:
        return None
    heading = " ".join(" ".join(lines[:first_table_line]).split())
    heading, permits_d = _FREE.subn("", heading)
    if not (_MAP if is_map else _TRUTH).fullmatch(heading):
        return None
    # parse_public_table can ignore unrelated lines for its diagnostic use.
    # The direct solver requires every row to consist exclusively of table cells.
    cell = "[01dD]" if permits_d else "[01]"
    left = r"[01]+\s*\|\s*" if is_map else r"\|?\s*"
    row = left + rf"{cell}(?:\s*\|\s*{cell})+\s*\|?"
    data = lines[first_bar:] if is_map else lines[first_bar + 1:]
    if not data or any(not re.fullmatch(row, line) for line in data):
        return None
    table = parse_public_table(body, interface)
    if not table or table["status"] != "supported" or table["unchecked_outputs"]:
        return None
    expected_rows = (1 << len(table["mapping"]["row_bits"])) if is_map else (1 << len(inputs))
    if len(data) != expected_rows:
        return None

    cells = {n: (set(), set()) for n in outputs}
    for row_data in table["table_rows"]:
        value = 0
        for name in inputs:
            value = (value << 1) | row_data["inputs"][name]
        for name, (expected, _) in row_data["expected"].items():
            cells[name][expected].add(value)
    ports = ", ".join(f"{p.direction} wire {n}" for n, p in interface.ports.items())
    source = [f"module {interface.module}({ports});"]
    checked_cells = 0
    for name, (zeros, ones) in cells.items():
        terms = _cover(len(inputs), ones, zeros)
        for value in ones | zeros:
            if any(_matches(term, value) for term in terms) != (value in ones):
                return None
        checked_cells += len(ones) + len(zeros)
        source.append(f"    assign {name} = {_expression(terms, inputs, False)};")
    source.append("endmodule")
    proof = {"kind": "scalar_truth_table", "input_order": inputs, "table_format": table["table_format"],
             "checked_output_cells": checked_cells, "input_rows": len(table["table_rows"])}
    return "\n".join(source) + "\n", proof
