"""Narrow candidate-only FSM diagnostics. Never rewrites RTL or invents states."""
import re

IDENT = r'[A-Za-z_$][A-Za-z0-9_$]*'
LITERAL = re.compile(r"(?:(\d+)\s*'\s*([sS]?)\s*([bBoOdDhH])\s*([0-9a-fA-F_]+)|(\d+))\Z")


def _integer(text):
    m = LITERAL.fullmatch(text.strip())
    if not m:
        return None
    bits, signed, base, digits, decimal = m.groups()
    try:
        value = int(decimal) if decimal else int(digits.replace('_', ''), {'b':2,'o':8,'d':10,'h':16}[base.lower()])
    except ValueError:
        return None
    width = int(bits) if bits else None
    if width is not None and not 1 <= width <= 4096:
        return None
    # Signed extension/expression evaluation is outside this narrow diagnostic.
    if signed:
        return None
    return value, width


def check_state_encoding(code):
    """Report concrete truncation for named constants used as case labels.

    Symbolic widths, enums, expressions, x/z, signed literals and intentional
    aliases without truncation stay outside scope. Absence of findings is not
    a proof of correct transitions or behavior.
    """
    text = re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"', ' ', code, flags=re.S)
    constants = {}
    for body in re.findall(r'\b(?:localparam|parameter)\b([^;]+);', text):
        width = re.match(r'\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]', body)
        packed = abs(int(width[1])-int(width[2]))+1 if width else None
        if width:
            body = body[width.end():]
        elif re.match(r'\s*(?:integer|int|logic|reg|bit|signed|unsigned)\b', body):
            continue
        for item in body.split(','):
            m = re.fullmatch(r'\s*('+IDENT+r')\s*=\s*(.*?)\s*', item, re.S)
            if m and (number := _integer(m[2])) is not None:
                if m[1] in constants:
                    constants[m[1]] = None
                else:
                    constants[m[1]] = (*number, packed)
    storage = {}
    for m in re.finditer(r'\b(?:reg|logic|bit)\s+(?:\[\s*(\d+)\s*:\s*(\d+)\s*\]\s*)?([^;]+);', text):
        width = abs(int(m[1])-int(m[2]))+1 if m[1] is not None else 1
        for item in m[3].split(','):
            name = re.fullmatch(r'\s*('+IDENT+r')\s*', item)
            if name:
                storage[name[1]] = width
    issues = []
    seen = set()
    for case in re.finditer(r'\bcase\s*\(\s*('+IDENT+r')\s*\)(.*?)\bendcase\b', text, re.S):
        state = case[1]
        # Nested cases require a parser; do not conflate inner and outer labels.
        if re.search(r'\bcase(?:x|z)?\b', case[2]):
            continue
        labels = re.findall(r'(?:^|[;\n])\s*('+IDENT+r')\s*:', case[2])
        linked = []
        for source in re.findall(r'\b'+re.escape(state)+r'\s*<=\s*('+IDENT+r')\s*;', text):
            if source != state and source in storage and re.search(
                    r'\b'+re.escape(source)+r'\s*=\s*(?:'+'|'.join(map(re.escape, labels))+r')\s*;', case[2]) if labels else False:
                linked.append(source)
        for name in labels:
            definition = constants.get(name)
            if definition is None:
                continue
            value, literal_width, declared_width = definition
            steps = []
            effective = value
            widths = [('literal',literal_width), ('constant declaration',declared_width), ('state register',storage.get(state))]
            widths.extend(('next-state register '+signal,storage[signal]) for signal in sorted(set(linked)))
            for kind, width in widths:
                if width is None or not 1 <= width <= 4096:
                    continue
                if effective >= (1 << width):
                    after = effective % (1 << width)
                    steps.append(f'{kind} width {width} truncates {effective} to {after}')
                    effective = after
            key = (state,name)
            if steps and key not in seen:
                seen.add(key)
                issues.append(f'STATE_ENCODING: case({state}) label {name}: '+ '; '.join(steps) +
                              '. This is candidate-source evidence, not a reference-state assignment. '
                              'Reconcile ALL state constants, literal widths and state/next-state storage together; '
                              'check uniqueness, transitions, self/terminal holds and reset consistency against the public task. '
                              'Preserve correct counters, data storage and output timing; return one complete module.')
    return issues[:6]
