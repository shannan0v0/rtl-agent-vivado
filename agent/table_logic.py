"""Combinational RTL from complete truth tables, labeled Karnaugh maps and waveforms."""

import re

from agent.indexed_logic import _ID, _INTRO, _cover, _expression, _matches
from agent.public_tables import parse_public_table
from official_entry.interface_check import parse_prompt_interface


_TRUTH = re.compile(
    r"(?:The module should implement a combinational circuit for the following truth table"
    r"|(?:Combinational )?truth table)\s*[:.]?", re.I,
)
_MAP = re.compile(
    rf"(?:The module should implement (?:the circuit described by |the function (?P<output>{_ID}) shown in )?"
    r"the Karnaugh map below|Karnaugh map)\s*[:.]?", re.I,
)
_WAVE = re.compile(
    r"(?:The module (?:should implement|implements) a combinational circuit\. "
    r"Read the simulation waveforms? to determine what the circuit does, then implement it\."
    r"|Combinational (?:simulation )?waveforms?\s*[:.]?)", re.I,
)
_PORT = rf"(?:input\s+(?:wire\s+)?(?:\[\s*[1-3]\s*:\s*0\s*\]\s*)?|output\s+(?:wire\s+)?){_ID}"
_TABLE_INTERFACE = re.compile(
    rf"\s*module\s+{_ID}\s*\(\s*{_PORT}(?:\s*,\s*{_PORT})*\s*\)\s*;\s*(?:endmodule\s*)?"
)
_FREE = re.compile(
    r"\s+d is don['’]t-care, which means you may choose to output whatever value is convenient\.$",
    re.I,
)


def _waveform_as_table(lines, inputs, outputs):
    """Fold repeated observations into rows only for a stated combinational function."""
    header = next((i for i, line in enumerate(lines) if line.split()[0].lower() == "time"), None)
    if header is None or not _WAVE.fullmatch(" ".join(" ".join(lines[:header]).split())):
        return None
    columns = lines[header].split()[1:]
    if len(columns) != len(inputs) + len(outputs) or set(columns) != set(inputs + outputs):
        return None
    indices = [columns.index(n) for n in inputs + outputs]
    rows = {}
    previous_time, unit = -1, None
    for line in lines[header + 1:]:
        stamp, *values = line.split()
        time = re.fullmatch(r"(\d{1,18})(fs|ps|ns|us|ms|s)", stamp)
        if not time or len(values) != len(columns) or any(v not in ("0", "1") for v in values):
            return None
        if (unit is not None and time[2] != unit) or int(time[1]) <= previous_time:
            return None
        previous_time, unit = int(time[1]), time[2]
        ordered = tuple(values[j] for j in indices)
        key, value = ordered[:len(inputs)], ordered[len(inputs):]
        if rows.setdefault(key, value) != value:
            return None
    if len(rows) != 1 << len(inputs):
        return None
    table = ["Combinational truth table:", " | ".join(inputs + outputs)]
    table.extend(" | ".join(key + value) for key, value in rows.items())
    return table, len(lines) - header - 1


def solve_table_logic(prompt, public_input, interface_text=""):
    """Produce (source, exhaustive cell checks) only for a complete standalone specification."""
    if not re.search(r"truth\s+table|Karnaugh\s+map|waveforms?", prompt, re.I):
        return None
    if interface_text.strip() and not _TABLE_INTERFACE.fullmatch(interface_text):
        return None
    intro = _INTRO.match(prompt)
    if not intro and not interface_text.strip():
        return None
    interface = parse_prompt_interface(public_input)
    if not interface:
        return None
    for port in interface.ports.values():
        if port.direction == "input" and port.width in ("1", "2", "3", "4"):
            continue
        if port.direction != "output" or port.width != "1":
            return None
    inputs = [n for n, p in interface.ports.items() if p.direction == "input"]
    outputs = [n for n, p in interface.ports.items() if p.direction == "output"]
    widths = {name: int(interface.ports[name].width) for name in inputs}
    input_bits = [name if width == 1 else f"{name}[{bit}]"
                  for name, width in widths.items() for bit in reversed(range(width))]
    if not 1 <= len(input_bits) <= 4 or not outputs:
        return None

    body = prompt[intro.end():] if intro else prompt
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    observed_samples = None
    if re.search(r"\bwaveforms?\b", body, re.I):
        if input_bits != inputs:
            return None
        normalized = _waveform_as_table(lines, inputs, outputs)
        if normalized is None:
            return None
        lines, observed_samples = normalized
        body = "\n".join(lines)
    first_bar = next((i for i, line in enumerate(lines) if "|" in line), None)
    if first_bar is None:
        return None
    is_map = bool(re.search(r"Karnaugh map", body, re.I))
    first_table_line = first_bar - 2 if is_map else first_bar
    if first_table_line < 1:
        return None
    heading = " ".join(" ".join(lines[:first_table_line]).split())
    heading, permits_d = _FREE.subn("", heading)
    recognized = (_MAP if is_map else _TRUTH).fullmatch(heading)
    if not recognized or (is_map and recognized["output"] and outputs != [recognized["output"]]):
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
    expected_rows = 1 << (len(table["mapping"]["row_bits"]) if is_map else len(input_bits))
    if len(data) != expected_rows:
        return None

    cells = {n: (set(), set()) for n in outputs}
    for row_data in table["table_rows"]:
        value = 0
        for name, width in widths.items():
            value = (value << width) | row_data["inputs"][name]
        for name, (expected, _) in row_data["expected"].items():
            cells[name][expected].add(value)
    ports = []
    for name, port in interface.ports.items():
        bit_range = f"[{int(port.width) - 1}:0] " if port.width != "1" else ""
        ports.append(f"{port.direction} wire {bit_range}{name}")
    source = [f"module {interface.module}({', '.join(ports)});"]
    checked_cells = 0
    for name, (zeros, ones) in cells.items():
        terms = _cover(len(input_bits), ones, zeros)
        for value in ones | zeros:
            if any(_matches(term, value) for term in terms) != (value in ones):
                return None
        checked_cells += len(ones) + len(zeros)
        source.append(f"    assign {name} = {_expression(terms, input_bits, False)};")
    source.append("endmodule")
    kind = "packed_truth_table" if len(input_bits) != len(inputs) else "scalar_truth_table"
    proof = {"kind": kind, "input_order": input_bits, "table_format": table["table_format"],
             "checked_output_cells": checked_cells, "input_rows": len(table["table_rows"])}
    if observed_samples is not None:
        proof.update(kind="combinational_waveform", table_format="waveform", observed_samples=observed_samples)
    return "\n".join(source) + "\n", proof
