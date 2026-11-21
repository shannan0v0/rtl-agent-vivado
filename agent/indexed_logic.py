"""Generate minimum two-level logic from a fully specified, indexed Boolean function."""

from functools import lru_cache
from itertools import product
import re

from official_entry.interface_check import parse_prompt_interface


_ID = r"[a-zA-Z_][a-zA-Z0-9_$]*"
_NUMBER = r"(?:\d{1,2})"
_NUMBERS = rf"(?:none|{_NUMBER}(?:(?:\s*,\s*(?:(?:and|or)\s+)?|\s+(?:and|or)\s+){_NUMBER})*)"
_NAMES = rf"{_ID}(?:\s*,\s*{_ID})*"
_PORT = rf"(?:input|output)\s+(?:wire\s+)?{_ID}"
SCALAR_INTERFACE = re.compile(
    rf"\s*module\s+{_ID}\s*\(\s*{_PORT}(?:\s*,\s*{_PORT})*\s*\)\s*;\s*(?:endmodule\s*)?"
)
_INTRO = re.compile(
    (rf"\s*I would like you to implement a module named {_ID} with the following interface\. "
     r"All input and output ports are (?:one|1) bit unless otherwise specified\. ").replace(" ", r"\s+")
    + rf"(?:[ \t]*[-*][ \t]*(?:input|output)[ \t]+{_ID}"
      r"(?:[ \t]*\([ \t]*[1-4][ \t]+bits?[ \t]*\))?[ \t]*\r?\n)+", re.I,
)
_SPEC = re.compile(
    rf"The module should implement a digital system with (?:(?P<count>one|two|three|four|\d+) )?inputs? "
    rf"\(\s*(?P<bits>{_NAMES})\s*\) that generates (?:a )?logic-1 when (?P<one>{_NUMBERS}) "
    rf"appears? on the inputs?,? and (?:a )?logic-0 when (?P<zero>{_NUMBERS}) appears?\. "
    rf"(?:The input conditions for the numbers? (?P<free>{_NUMBERS}) never occur in this system\. )?"
    rf"For example, (?P<example>{_NUMBER}) corresponds to (?P<order>{_NAMES}) being set to "
    rf"(?P<values>[01](?:\s*,\s*[01])*), respectively\. "
    rf"Determine the output (?P<sop>{_ID}) in minimum sum-of-products form,? and "
    rf"(?:the output )?(?P<pos>{_ID}) in minimum product-of-sums form\.?", re.I,
)
# Short-form engineering specification with an explicit bit order and both output roles.
_SHORT_SPEC = re.compile(
    rf"Inputs? \(MSB first\):\s*(?P<bits>{_NAMES})\.\s*"
    rf"Ones:\s*(?P<one>{_NUMBERS})\.\s*Zeros:\s*(?P<zero>{_NUMBERS})\.\s*"
    rf"Don['’]t[- ]cares:\s*(?P<free>{_NUMBERS})\.\s*"
    rf"Minimum SOP:\s*(?P<sop>{_ID})\.\s*Minimum POS:\s*(?P<pos>{_ID})\.?", re.I,
)


def _cover(width, required, forbidden):
    """Find an exact cover, minimizing term count then total literals; '-' means free."""
    if not required:
        return ()
    cubes = {}
    for pattern in product("01-", repeat=width):
        pattern = "".join(pattern)
        hits = {i for i in range(1 << width) if _matches(pattern, i)}
        if hits & required and not hits & forbidden:
            cubes[pattern] = sum(1 << i for i in hits & required)
    # A non-prime cube can always be replaced by a larger one with fewer literals.
    primes = {p: hits for p, hits in cubes.items() if not any(
        p[:i] + "-" + p[i + 1:] in cubes for i, bit in enumerate(p) if bit != "-")}

    @lru_cache(maxsize=None)
    def best(uncovered):
        if not uncovered:
            return (0, 0, ())
        pivot = uncovered & -uncovered
        options = []
        for pattern, hits in primes.items():
            if hits & pivot:
                terms, literals, rest = best(uncovered & ~hits)
                options.append((1 + terms, width - pattern.count("-") + literals, (pattern,) + rest))
        return min(options)

    return best(sum(1 << i for i in required))[2]


def _matches(pattern, index):
    return all(bit == "-" or int(bit) == ((index >> i) & 1)
               for i, bit in enumerate(reversed(pattern)))


def _expression(cover, names, pos):
    # A POS clause is the complement of a cube in the cover of the zero set.
    terms = []
    for pattern in cover:
        negate_if = "1" if pos else "0"
        literals = [(("~" if bit == negate_if else "") + name)
                    for bit, name in zip(pattern, names) if bit != "-"]
        if not literals:
            return "1'b0" if pos else "1'b1"
        terms.append("(" + (" | " if pos else " & ").join(literals) + ")")
    return (" & " if pos else " | ").join(terms) or ("1'b1" if pos else "1'b0")


def solve_indexed_logic(prompt, public_input, interface_text=""):
    """Return (RTL, proof details) or None when the public specification is ambiguous."""
    if "product-of-sums" not in prompt.lower() and "minimum pos" not in prompt.lower():
        return None
    if interface_text.strip() and not SCALAR_INTERFACE.fullmatch(interface_text):
        return None
    body = " ".join(_INTRO.sub("", prompt, count=1).split())
    match = _SPEC.fullmatch(body) or _SHORT_SPEC.fullmatch(body)
    if not match:
        return None
    fields = match.groupdict()
    interface = parse_prompt_interface(public_input)
    bits = [s.strip() for s in fields["bits"].split(",")]
    if not interface or not 1 <= len(bits) <= 4 or len(set(bits)) != len(bits):
        return None
    count = fields.get("count")
    if count and count.lower() not in (str(len(bits)), ("one", "two", "three", "four")[len(bits) - 1]):
        return None
    inputs = {n for n, p in interface.ports.items() if p.direction == "input"}
    outputs = {n for n, p in interface.ports.items() if p.direction == "output"}
    sop, pos = fields["sop"], fields["pos"]
    if (set(bits) != inputs or sop == pos or outputs != {sop, pos} or
            len(inputs) + 2 != len(interface.ports) or any(p.width != "1" for p in interface.ports.values())):
        return None
    if fields.get("example") is not None:
        order = [n.strip() for n in fields["order"].split(",")]
        example = re.sub(r"[,\s]", "", fields["values"])
        if order != bits or len(example) != len(bits) or int(example, 2) != int(fields["example"]):
            return None
    rows = [list(map(int, re.findall(r"\d+", fields[key] or ""))) for key in ("one", "zero", "free")]
    cells = [value for group in rows for value in group]
    universe = set(range(1 << len(bits)))
    if len(cells) != len(universe) or set(cells) != universe:
        return None
    ones, zeros, _ = map(set, rows)
    if not ones and not zeros:
        return None
    sop_cover = _cover(len(bits), ones, zeros)
    pos_cover = _cover(len(bits), zeros, ones)
    for i in ones | zeros:
        positive = any(_matches(p, i) for p in sop_cover)
        negative = any(_matches(p, i) for p in pos_cover)
        if positive != (i in ones) or negative != (i in zeros):
            return None
    ports = ", ".join(f"{port.direction} wire {name}" for name, port in interface.ports.items())
    rtl = (f"module {interface.module}({ports});\n"
           f"    assign {sop} = {_expression(sop_cover, bits, False)};\n"
           f"    assign {pos} = {_expression(pos_cover, bits, True)};\nendmodule\n")
    proof = {"kind": "indexed_boolean", "input_order": bits, "checked_input_values": len(ones) + len(zeros),
             "sop_output": sop, "pos_output": pos, "one_cover": sop_cover, "zero_cover": pos_cover}
    return rtl, proof
