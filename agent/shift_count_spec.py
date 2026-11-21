"""Lower a fully specified serial shift register with an enabled unit counter."""

import re

from official_entry.interface_check import parse_prompt_interface


_ID = r"[a-zA-Z_][a-zA-Z0-9_$]*"
_NUMBERS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen".split()
_WIDTH = r"(?:[0-9]{1,4}|" + "|".join(_NUMBERS) + ")"


def _width(token):
    return int(token) if token.isdecimal() else _NUMBERS.index(token.lower())


def _behavior_text(prompt, interface_text, interface):
    # Accept width-only bullet declarations. Explicit HDL ranges could denote a
    # different bit order and an example DUT could contain other constraints.
    if interface_text.strip() or not re.search(r"(?m)^\s*[-*]\s+input\s", prompt):
        return None
    body = re.sub(r"(?mi)^\s*[-*]\s+(?:input|output)\s+[^\n]+$", " ", prompt)
    body = " ".join(body.split())
    intro = (rf"I would like you to implement a module named {re.escape(interface.module)} "
             r"with the following interface\.\s*")
    body = re.sub("^" + intro, "", body, count=1, flags=re.I)
    body = re.sub(r"^All input and output ports are (?:one|1) bit unless otherwise specified\.\s*",
                  "", body, count=1, flags=re.I)
    return body


def solve_shift_count(prompt, public_input, interface_text=""):
    if not re.search(r"shift\s+register", prompt, re.I) or not re.search(r"\bcounter\b", prompt, re.I):
        return None
    interface = parse_prompt_interface(public_input)
    if interface is None or len(interface.ports) != 5:
        return None
    behavior = _behavior_text(prompt, interface_text, interface)
    if not behavior:
        return None
    ports = interface.ports
    inputs = {name for name, port in ports.items() if port.direction == "input" and port.width == "1"}
    outputs = [name for name, port in ports.items() if port.direction == "output"]
    if len(inputs) != 4 or len(outputs) != 1:
        return None
    out = outputs[0]
    # Each statement must be consumed in full. Extra implementation rules,
    # contradictory directions and unspecified enable priority fall through.
    statements = [part.strip() for part in behavior.split(".") if part.strip()]
    spec = {}
    for s in statements:
        m = re.fullmatch(rf"(?:The module should implement|Implement|Build) (?:a |an )?({_WIDTH})[ -]bit "
                         r"shift register (?:that also acts as|with) (?:a |an )?(up|down) counter", s, re.I)
        if m:
            key, value = "unit", (_width(m[1]), m[2].lower())
        elif m := re.fullmatch(rf"({_ID}) is shifted in (most|least)[ -]significant[ -]bit first "
                               rf"when ({_ID}) is (?:1|high)", s, re.I):
            key, value = "shift", (m[1], m[2].lower(), m[3])
        elif m := re.fullmatch(rf"When ({_ID}) is (?:1|high), shift in ({_ID}) (MSB|LSB)[ -]first", s, re.I):
            key, value = "shift", (m[2], "most" if m[3].lower() == "msb" else "least", m[1])
        elif m := re.fullmatch(rf"The number currently in the shift register is (incremented|decremented) "
                               rf"when ({_ID}) is (?:1|high)", s, re.I):
            key, value = "count", (m[1].lower(), m[2])
        elif m := re.fullmatch(rf"When ({_ID}) is (?:1|high), (increment|decrement) "
                               rf"(?:the register|{re.escape(out)}) by one", s, re.I):
            key, value = "count", (m[2].lower() + "ed", m[1])
        elif m := re.fullmatch(rf"Since the full system doesn't ever use ({_ID}) and ({_ID}) together, "
                               r"it does not matter what your circuit does if both control inputs are 1"
                               r"(?: \(this mainly means that it doesn't matter which case gets higher priority\))?", s, re.I):
            key, value = "priority", ("exclusive", m[1], m[2])
        elif m := re.fullmatch(rf"({_ID}) and ({_ID}) are mutually exclusive", s, re.I):
            key, value = "priority", ("exclusive", m[1], m[2])
        elif m := re.fullmatch(rf"({_ID}) (?:has|takes) priority over ({_ID})", s, re.I):
            key, value = "priority", ("explicit", m[1], m[2])
        elif m := re.fullmatch(rf"(?:Assume all sequential logic is triggered|Update (?:the register|{re.escape(out)})) "
                               r"on the (positive|negative|rising|falling) "
                               rf"edge of (?:the )?({_ID})", s, re.I):
            key, value = "edge", (m[1].lower(), m[2])
        elif m := re.fullmatch(rf"Otherwise (?:the register|{re.escape(out)}) holds its (?:current )?value", s, re.I):
            key, value = "hold", True
        else:
            return None
        if key in spec:
            return None
        spec[key] = value
    if not {"unit", "shift", "count", "priority", "edge"} <= spec.keys():
        return None
    bits, direction = spec["unit"]
    serial, order, shift_enable = spec["shift"]
    operation, count_enable = spec["count"]
    priority, first, second = spec["priority"]
    transition, clock = spec["edge"]
    if (str(bits) != ports[out].width or not 1 <= bits <= 4096 or
            (direction == "up") != (operation == "incremented") or
            {shift_enable, count_enable} != {first, second} or
            shift_enable == count_enable or not {shift_enable, count_enable} <= inputs):
        return None
    # "Data" and "the clock" may be prose roles. Infer at most one by
    # elimination, once the other is unambiguously identified.
    free = inputs - {shift_enable, count_enable}
    if serial not in free and serial.lower() == "data":
        matches = [name for name in free if name.lower() == "data"]
        if len(matches) == 1:
            serial = matches[0]
    if clock not in free and clock.lower() == "clock" and serial in free:
        clock, = free - {serial}
    if serial not in free and serial.lower() == "data" and clock in free:
        serial, = free - {clock}
    if {serial, clock} != free:
        return None
    edge = "posedge" if transition in ("positive", "rising") else "negedge"
    if bits == 1:
        shifted = serial
    elif order == "most":
        shifted = f"{{{out}[{bits - 2}:0], {serial}}}"
    else:
        shifted = f"{{{serial}, {out}[{bits - 1}:1]}}"
    counted = f"{out} {'+' if direction == 'up' else '-'} 1'b1"
    actions = {shift_enable: shifted, count_enable: counted}
    preferred = first if priority == "explicit" else shift_enable
    other = count_enable if preferred == shift_enable else shift_enable
    width = f"[{bits - 1}:0] " if bits > 1 else ""
    declarations = [f"output reg {width}{name}" if name == out else f"input wire {name}" for name in ports]
    rtl = (f"module {interface.module}(\n    " + ",\n    ".join(declarations) + "\n);\n"
           f"    always @({edge} {clock}) begin\n"
           f"        if ({preferred}) {out} <= {actions[preferred]};\n"
           f"        else if ({other}) {out} <= {actions[other]};\n"
           "    end\nendmodule\n")
    return rtl, {"kind": "serial_shift_count", "word_width": bits, "edge": edge, "serial_order": order,
                 "count_direction": direction, "priority": priority if priority == "exclusive" else preferred,
                 "roles": {"data": serial, "clock": clock, "shift_enable": shift_enable,
                           "count_enable": count_enable, "output": out}}
