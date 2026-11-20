"""Conservative public-interface parsing and exact RTL candidate comparison.

This module only parses and compares declarations. It never edits or completes
the candidate RTL. Unsupported specification syntax returns ``unverified``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Port:
    direction: str
    width: str


@dataclass(frozen=True)
class Interface:
    module: str
    ports: dict[str, Port]


@dataclass(frozen=True)
class CheckResult:
    status: str
    reason: str
    expected: Interface | None = None
    actual: Interface | None = None


_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
_MODULE_START = re.compile(r"\bmodule\s+(%s)\b" % _IDENT)
_DIRECTION = re.compile(r"\b(input|output|inout)\b")
_MODIFIERS = {"wire", "logic", "reg", "signed", "unsigned", "var", "tri", "integer", "time"}


def _matching(text: str, start: int, opening: str = "(", closing: str = ")") -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_top_level(text: str, delimiter: str = ",") -> list[str]:
    result: list[str] = []
    square = paren = brace = 0
    start = 0
    for index, char in enumerate(text):
        if char == "[":
            square += 1
        elif char == "]":
            square = max(0, square - 1)
        elif char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == delimiter and square == 0 and paren == 0 and brace == 0:
            result.append(text[start:index].strip())
            start = index + 1
    result.append(text[start:].strip())
    return [part for part in result if part]


def _width(range_text: str | None) -> str:
    if not range_text:
        return "1"
    match = re.fullmatch(r"\s*([^:]+)\s*:\s*([^:]+)\s*", range_text)
    if not match:
        return "?"
    left, right = (part.strip().replace(" ", "") for part in match.groups())
    if re.fullmatch(r"\d+", left) and re.fullmatch(r"\d+", right):
        return str(abs(int(left) - int(right)) + 1)
    return "[{}:{}]".format(left, right)


def _range(segment: str) -> tuple[str, str]:
    match = re.search(r"\[\s*([^\]]+)\s*\]", segment)
    if not match:
        return segment, "1"
    return segment[:match.start()] + segment[match.end():], _width(match.group(1))


def _port_name(segment: str) -> str | None:
    segment = re.sub(r"\s*=.*$", "", segment)
    clean = re.sub(r"\b(?:wire|logic|reg|signed|unsigned|var|tri|integer|time|const)\b", " ", segment)
    clean = re.sub(r"\[[^\]]*\]", " ", clean)
    names = re.findall(_IDENT, clean)
    return names[-1] if names else None


def _parse_ansi_ports(header: str) -> dict[str, Port] | None:
    ports: dict[str, Port] = {}
    direction: str | None = None
    width = "1"
    for segment in _split_top_level(header):
        directive = _DIRECTION.search(segment)
        if directive:
            direction = directive.group(1)
            _, width = _range(segment[directive.end():])
            name = _port_name(segment[directive.end():])
        else:
            name = _port_name(segment)
        if direction is None or name is None or width == "?":
            return None
        if name in ports:
            return None
        ports[name] = Port(direction, width)
    return ports


def _parse_declaration_body(body: str, names: Iterable[str]) -> dict[str, Port] | None:
    found: dict[str, Port] = {}
    for declaration in re.findall(r"\b(input|output|inout)\b([^;]*);", body, flags=re.DOTALL):
        direction, remainder = declaration
        clean, width = _range(remainder)
        if width == "?":
            return None
        clean = re.sub(r"\b(?:wire|logic|reg|signed|unsigned|var|tri|integer|time)\b", " ", clean)
        declared = [re.sub(r"\s*=.*$", "", part).strip() for part in _split_top_level(clean)]
        for item in declared:
            name = _port_name(item)
            if name:
                found[name] = Port(direction, width)
    if set(found) != set(names):
        return None
    return found


def parse_module(text: str) -> Interface | None:
    """Parse one module header and ANSI or non-ANSI port declarations."""
    text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.DOTALL)
    match = _MODULE_START.search(text)
    if not match:
        return None
    module_name = match.group(1)
    cursor = match.end()
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if text.startswith("#", cursor):
        open_paren = text.find("(", cursor + 1)
        if open_paren < 0:
            return None
        close_paren = _matching(text, open_paren)
        if close_paren is None:
            return None
        cursor = close_paren + 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
    if cursor >= len(text) or text[cursor] != "(":
        return None
    close = _matching(text, cursor)
    if close is None:
        return None
    header = text[cursor + 1:close]
    body_end = re.search(r"\bendmodule\b", text[close + 1:])
    body = text[close + 1:close + 1 + body_end.start()] if body_end else text[close + 1:]
    if re.search(r"\b(input|output|inout)\b", header):
        ports = _parse_ansi_ports(header)
    else:
        names = [part.strip() for part in _split_top_level(header)]
        if any(not re.fullmatch(_IDENT, name) for name in names):
            return None
        ports = _parse_declaration_body(body, names)
    if ports is None:
        return None
    return Interface(module_name, ports)


def parse_prompt_interface(prompt: str) -> Interface | None:
    """Extract only declarations that are explicitly present in public prompt text."""
    uncommented = re.sub(r"/\*.*?\*/|//[^\n]*", " ", prompt, flags=re.DOTALL)
    # Count syntactic HDL declarations, not English phrases such as
    # "module named ..." or "the module should ...".
    declarations = list(re.finditer(
        r"\bmodule\s+(%s)\s*(?=\(|#\s*\(|;)" % _IDENT,
        uncommented,
    ))
    module_names = re.findall(
        r"\bmodule\s+(?:(?:named|called)\s+|name\s*(?:is\s+|:\s*))(%s)\b" % _IDENT,
        uncommented,
        flags=re.IGNORECASE,
    )
    # A named target and an explicit bullet interface may coexist with an
    # unrelated example module. Only declarations of the target can constrain
    # that interface; never infer its ports from the example implementation.
    if len(module_names) == 1:
        bullets = _parse_bullet_interface(prompt, module_names[0])
        if bullets is not None:
            target_declarations = [decl for decl in declarations if decl.group(1) == module_names[0]]
            if len(target_declarations) > 1:
                return None
            for decl in target_declarations:
                if parse_module(uncommented[decl.start():]) != bullets:
                    return None
            return bullets
        if re.search(r"^\s*[-*].*\b(?:input|output|inout)\b", prompt, re.MULTILINE | re.IGNORECASE):
            return None
    if len(declarations) > 1 or len(module_names) > 1:
        return None
    if len(re.findall(r"\bendmodule\b", uncommented)) > 1:
        return None
    if declarations:
        parsed = parse_module(uncommented[declarations[0].start():])
        if parsed is None or (module_names and module_names[0] != parsed.module):
            return None
        return parsed
    if not module_names or re.search(r"\bendmodule\b", uncommented):
        return None
    module_name = module_names[0]
    ports: dict[str, Port] = {}
    recognized = False
    for role, section in re.findall(r"\b(inputs?|outputs?)\s*:\s*([^\n.;]+)", prompt, flags=re.IGNORECASE):
        direction = "input" if role.lower().startswith("input") else "output"
        for item in _split_top_level(section):
            match = re.fullmatch(r"\s*(%s)\s*(?:\[\s*([^\]]+)\s*\])?\s*(?:\((\d+)\s*bits?\))?\s*" % _IDENT, item, flags=re.IGNORECASE)
            if not match:
                return None
            name, packed, bits = match.groups()
            width = str(int(bits)) if bits else _width(packed)
            if width == "?" or name in ports:
                return None
            ports[name] = Port(direction, width)
        recognized = True
    if recognized and ports:
        return Interface(module_name, ports)

    return _parse_bullet_interface(prompt, module_name)


def _parse_bullet_interface(prompt: str, module_name: str) -> Interface | None:
    default_one_bit = re.search(
        r"\ball\s+input\s+and\s+output\s+ports?\s+are\s+(?:one|1)\s+bit\s+unless\s+otherwise\s+specified\b",
        prompt,
        flags=re.IGNORECASE,
    ) is not None
    bullet_lines = [
        line
        for line in prompt.splitlines()
        if re.match(r"^\s*[-*]\s*(?:input|output|inout)\b", line, flags=re.IGNORECASE)
    ]
    if not bullet_lines:
        return None

    bullet_ports: dict[str, Port] = {}
    bullet_re = re.compile(
        r"^\s*[-*]\s*(input|output|inout)\s+(%s)"
        r"(?:\s*\(\s*(\d+)\s+bits?\s*\)|\s*\[\s*(\d+)\s+bits?\s*\])?\s*,?\s*$" % _IDENT,
        flags=re.IGNORECASE,
    )
    for line in bullet_lines:
        bullet = bullet_re.fullmatch(line)
        if not bullet:
            return None
        direction, name, paren_width, bracket_width = bullet.groups()
        width = paren_width or bracket_width
        if width is not None and int(width) <= 0:
            return None
        if width is None and not default_one_bit:
            return None
        if name in bullet_ports:
            return None
        bullet_ports[name] = Port(direction.lower(), str(int(width)) if width else "1")
    return Interface(module_name, bullet_ports) if bullet_ports else None


def compare_interfaces(expected: Interface | None, actual: Interface | None) -> CheckResult:
    if expected is None:
        return CheckResult("unverified", "prompt interface could not be parsed", expected, actual)
    if actual is None:
        return CheckResult("fail", "candidate module interface could not be parsed", expected, actual)
    if expected.module != actual.module:
        return CheckResult("fail", "module name mismatch", expected, actual)
    missing = sorted(set(expected.ports) - set(actual.ports))
    extra = sorted(set(actual.ports) - set(expected.ports))
    if missing or extra:
        return CheckResult("fail", "port set mismatch; missing={}, extra={}".format(missing, extra), expected, actual)
    mismatches = []
    for name in sorted(expected.ports):
        want, got = expected.ports[name], actual.ports[name]
        if want.direction != got.direction or want.width != got.width:
            mismatches.append("{} expected {}/{} got {}/{}".format(
                name, want.direction, want.width, got.direction, got.width
            ))
    if mismatches:
        return CheckResult("fail", "; ".join(mismatches), expected, actual)
    return CheckResult("pass", "module and all ports match", expected, actual)


def check_prompt_candidate(prompt: str, candidate: str) -> CheckResult:
    return compare_interfaces(parse_prompt_interface(prompt), parse_module(candidate))
