"""Limited public-specification contracts and test oracles; never generate DUT RTL."""

import random
import re

from official_entry.interface_check import parse_prompt_interface
from agent.cycle_events import parse_cycle_events, event_vectors
from agent.public_tables import parse_public_table, table_vectors
from agent.counter_boundaries import parse_counter_events, counter_vectors
from agent.behavior_intent import select_checker
from agent.public_fsm import parse_public_fsm, fsm_vectors, check_fsm_samples
from agent.comb_fsm import parse_comb_fsm, comb_vectors, comb_check


def reset_signal(prompt, interface):
    inputs = [name for name, port in interface.ports.items() if port.direction == 'input' and port.width == '1']
    explicit = [name for name in inputs if re.search(
        r'\b' + re.escape(name) + r'\b\s+(?:is|acts as|provides)\s+[^.\n]{0,100}\breset\b', prompt, re.I)]
    if len(explicit) == 1:
        return explicit[0]
    conventional = [name for name in inputs if re.search(r'reset|rst', name, re.I)]
    return conventional[0] if len(conventional) == 1 else None


def _one(items):
    if len(items) != 1:
        raise ValueError('public port roles are not unique')
    return items[0]


def _reset(prompt, interface):
    name = reset_signal(prompt, interface)
    sync = bool(re.search(r'\bsynchronous\b', prompt, re.I))
    asynchronous = bool(re.search(r'\basynchronous\b', prompt, re.I))
    high = bool(re.search(r'active[ -]high', prompt, re.I))
    low = bool(re.search(r'active[ -]low', prompt, re.I))
    if not name or sync == asynchronous or high == low:
        raise ValueError('reset role, timing or polarity is not explicit')
    value, packed_literal = None, False
    match = re.search(r'\breset\b[^.\n]{0,70}?\bto\s+(\d+\s*\x27\s*[hdb][0-9a-fA-F_]+|\d+)\b', prompt, re.I)
    if match:
        token = match.group(1).replace(' ', '').replace('_', '')
        if "'" in token:
            packed_literal = True
            _, number = token.split("'")
            value = int(number[1:], {'h': 16, 'd': 10, 'b': 2}[number[0].lower()])
        else:
            value = int(token)
    return {'name': name, 'active': int(high), 'synchronous': sync, 'value': value, 'packed_literal': packed_literal}


def parse_contract(prompt):
    """Abstain unless a supported operation and its public binding are clear."""
    interface = parse_prompt_interface(prompt)
    if interface is None:
        return {'status': 'unverified', 'reason': 'public interface cannot be parsed'}
    if any(p.direction == 'inout' or not p.width.isdigit() or not 1 <= int(p.width) <= 4096
           for p in interface.ports.values()):
        return {'status': 'unverified', 'reason': 'unsupported public port widths or inout'}
    table = parse_public_table(prompt, interface)
    if table is not None:
        return table
    temporal = parse_cycle_events(prompt, interface, lambda: _reset(prompt, interface))
    if temporal is not None:
        return temporal
    counter = parse_counter_events(prompt, interface, lambda: _reset(prompt, interface))
    if counter is not None:
        return counter
    comb = parse_comb_fsm(prompt, interface)
    if comb is not None:
        return comb
    fsm = parse_public_fsm(prompt, interface, lambda: _reset(prompt, interface))
    if fsm is not None:
        return fsm
    ports = {n: {'direction': p.direction, 'width': int(p.width)} for n,p in sorted(interface.ports.items())}
    ins = [n for n,p in ports.items() if p['direction'] == 'input']
    outs = [n for n,p in ports.items() if p['direction'] == 'output']
    width = lambda n: ports[n]['width']
    flat = re.sub(r'\s+', ' ', prompt)
    intent = select_checker(prompt)
    if intent['checker'] is None:
        return {'status': 'unverified',
                'reason': 'no executable public behavior model for ' + intent['domain'],
                'reason_code': intent['reason_code'], 'behavior_intent': intent,
                'binding_stage': 'behavior_selection', 'clock_binding_attempted': False}
    contract = {'status': 'supported', 'module': interface.module, 'ports': ports,
                'checked_outputs': [], 'unchecked_outputs': [], 'concerns': []}
    try:
        if intent['checker'] == 'priority':
            direction = [name for name in ('least', 'most') if re.search(name+r' significant', flat, re.I)]
            if len(direction) != 1 or not re.search(r'(?:report|output) zero', flat, re.I):
                raise ValueError('priority or zero-input behavior is not explicit')
            data, result = _one(ins), _one(outs)
            if width(result) < (width(data)-1).bit_length():
                raise ValueError('encoder result cannot represent all public input indices')
            contract.update(kind='priority', data=data, result=result, priority=direction[0])
        else:
            clocks = re.findall(r'(?:positive|rising|negative|falling) edge of\s+([A-Za-z_][A-Za-z0-9_$]*)', flat, re.I)
            clocks = [n for n in clocks if n in ins and width(n) == 1]
            clock = _one(sorted(set(clocks))) if clocks else _one([n for n in ins if re.fullmatch(r'clk|clock', n, re.I)])
            positive = bool(re.search(r'(?:positive|rising) edge', flat, re.I))
            negative = bool(re.search(r'(?:negative|falling) edge', flat, re.I))
            if positive and negative:
                raise ValueError('mixed public clock edges')
            contract.update(clock=clock, edge='posedge' if positive else 'negedge' if negative else 'either')
            if intent['checker'] == 'rotate':
                if len(ins) != 4 or len(outs) != 1:
                    raise ValueError('unsupported rotator ports')
                if re.search(r'active[ -]low|load[^.]{0,50}when low', flat, re.I):
                    raise ValueError('low-active load is not supported by this checker')
                result = outs[0]
                data = _one([n for n in ins if width(n) == width(result) and n != clock])
                control = _one([n for n in ins if width(n) == 2 and n not in (data, clock)])
                load = _one([n for n in ins if n not in (data, control, clock) and width(n) == 1])
                directions = re.findall(r"\b(?:2\s*'\s*b\s*)?([01]{2})\b\s*[:=]?\s*rotates?\s+(left|right)", flat, re.I)
                mapping = {int(code,2): side.lower() for code,side in directions}
                if len(mapping) != 2 or set(mapping.values()) != {'left', 'right'}:
                    raise ValueError('rotation control encodings are not explicit')
                hold_clause = re.search(r'[^.,;]*?(?:do not rotate|retain the current contents|hold)[^.,;]*', flat, re.I)
                hold_codes = set(int(code,2) for code in re.findall(r"\b(?:2\s*'\s*b\s*)?([01]{2})\b", hold_clause[0])) if hold_clause else set()
                if hold_codes != set(range(4))-set(mapping):
                    raise ValueError('disabled control behavior is not explicit')
                if not re.search(r'\b'+re.escape(load)+r'\b[^.]{0,100}\bloads?\b', flat, re.I):
                    raise ValueError('load role is not explicit')
                contract.update(kind='rotate', data=data, result=result, control=control, load=load,
                                directions=mapping, load_priority=bool(re.search(r'instead of rotating|takes priority|priority over', flat, re.I)))
            elif intent['checker'] == 'bcd':
                reset = _reset(prompt, interface)
                if set(ins) != {clock, reset['name']} or not reset['synchronous']:
                    raise ValueError('unsupported counter controls')
                if reset['value'] and not reset['packed_literal']:
                    raise ValueError('nonzero bare BCD reset value has no supported packed interpretation')
                result = _one([n for n in outs if width(n) == max(width(o) for o in outs)])
                digits = width(result)//4
                if width(result) % 4 or not 1 <= digits <= 16:
                    raise ValueError('BCD packed digit layout is not supported')
                digit_layout = (r'\b'+re.escape(result)+r'\[3\s*:\s*0\][^.]{0,35}\b(?:ones|units)\b'
                                r'|\b(?:ones|units) digit[^.]{0,35}\b'+re.escape(result)+r'\[3\s*:\s*0\]')
                if not re.search(digit_layout, flat, re.I):
                    raise ValueError('least-significant decimal digit layout is not explicit')
                if re.search(r'\bdown\b|decrement|saturat|stop(?:s)? (?:at|counting)', flat, re.I):
                    raise ValueError('only increasing wrapping BCD counters are supported')
                contract.update(kind='bcd', result=result, digits=digits, reset=reset)
                extra = [n for n in outs if n != result]
                if digits == 2 and len(extra) == 1 and width(extra[0]) == 1 and re.search(r'combinational', flat, re.I) and re.search(r'current units digit is 9', flat, re.I):
                    contract['enable'] = extra[0]
                elif (len(extra)==1 and width(extra[0])==digits-1 and
                      re.search(r'For digits\s*\[\s*'+str(digits-1)+r'\s*:\s*1\s*\].{0,70}output an enable', flat, re.I)):
                    contract['carry_enable']=extra[0]
                elif extra:
                    contract['concerns'].append('Upper-digit enables must express carry from all lower decimal digits. '
                        'Their public bit mapping or sampling convention is incomplete, so these outputs are not hard-checked. '
                        'Review them independently; a high bit of a digit is not a decimal carry condition.')
            elif intent['checker'] == 'serial':
                reset = _reset(prompt, interface)
                payload_outputs = [n for n in outs if width(n) > 1]
                if len(payload_outputs)>1:
                    raise ValueError('ambiguous serial payload outputs')
                result = payload_outputs[0] if payload_outputs else None
                done = _one([n for n in outs if width(n) == 1])
                data = _one([n for n in ins if n not in (clock, reset['name']) and width(n) == 1])
                count = re.search(r'\b(\d+)\s+(?:data|payload) bits', flat, re.I)
                if len(ins) != 3 or len(outs) != 1+len(payload_outputs) or not count or not 1 <= int(count[1]) <= 64 or (result and int(count[1]) != width(result)):
                    raise ValueError('serial frame width or roles are not explicit')
                if not re.search(r'start bit\s*\(0\)', flat, re.I) or not re.search(r'stop bit\s*\(1\)', flat, re.I):
                    raise ValueError('unsupported or unspecified frame delimiters')
                if not re.search(r'least significant bit first', flat, re.I) or 'idle' not in flat.lower():
                    raise ValueError('serial bit order or idle behavior is not explicit')
                contract.update(kind='serial', data=data, result=result, payload_bits=int(count[1]), done=done, reset=reset,
                                concerns=['Only valid frames are hard-checked. Malformed-stop completion behavior is not inferred.'])
                recovery = re.search(
                    r'if the stop bit does not appear when expected.{0,100}'
                    r'wait until it finds a stop bit before attempting to receive the next byte',
                    flat, re.I)
                if recovery and re.search(r'correctly(?:[- ]received| received|.{0,35}received)', flat, re.I):
                    contract['recovery_policy'] = 'discard_bad_frame_then_resynchronize'
                    contract['concerns'] = [
                        'A missing stop bit invalidates that frame; the later recovery delimiter '
                        'permits a new start, not successful completion of the invalid frame. '
                        'Only the explicitly stated public recovery policy is checked.']
            elif intent['checker'] == 'capture' and not re.search(r'\bshift', flat, re.I):
                reset = _reset(prompt, interface)
                result = _one(outs)
                data = _one([n for n in ins if n not in (clock, reset['name']) and width(n) == width(result)])
                enables = [n for n in ins if n not in (clock, reset['name'], data)]
                lanes = []
                if enables:
                    enable = _one(enables)
                    for index in range(width(enable)):
                        match = re.search(r'\b'+re.escape(enable)+r'\['+str(index)+r'\][^.]{0,90}?\[(\d+)\s*:\s*(\d+)\]', flat, re.I)
                        if not match:
                            raise ValueError('enable-to-data lane mapping is not explicit')
                        high, low = map(int, match.groups())
                        if not 0 <= low <= high < width(result):
                            raise ValueError('invalid public lane slice')
                        lanes.append([index, high, low])
                    bits = [bit for _,hi,lo in lanes for bit in range(lo,hi+1)]
                    if sorted(bits) != list(range(width(result))):
                        raise ValueError('overlapping or incomplete lane mapping')
                if len(ins) != 3+len(enables):
                    raise ValueError('additional unmodeled register inputs')
                contract.update(kind='capture', data=data, result=result, reset=reset,
                                enable=enables[0] if enables else None, lanes=lanes)
            else:
                raise ValueError('public behavior family is not supported')
        contract['checked_outputs'] = ([contract['result']] if contract['result'] else []) + ([contract['done']] if contract['kind']=='serial' else [])
        if contract.get('enable') and contract['kind'] == 'bcd':
            contract['checked_outputs'].append(contract['enable'])
        if contract.get('carry_enable'):
            contract['checked_outputs'].append(contract['carry_enable'])
        contract['unchecked_outputs'] = [n for n in outs if n not in contract['checked_outputs']]
        contract['scope'] = 'finite public-derived scenarios; not a complete functional proof'
        return contract
    except (ValueError, IndexError) as error:
        return {'status': 'unverified', 'reason': str(error)}


def build_vectors(contract):
    """Deterministic inputs only. This function never receives candidate code."""
    c = contract
    if c.get('kind') == 'comb_fsm':
        return comb_vectors(c)
    if c.get('kind') == 'public_fsm':
        return fsm_vectors(c)
    if c.get('kind') == 'counter_events':
        return counter_vectors(c)
    if c.get('kind') == 'public_table':
        return table_vectors(c)
    if c.get('kind') == 'cycle_events':
        return event_vectors(c)
    ins = {n: 0 for n,p in c['ports'].items() if p['direction'] == 'input'}
    width = c['payload_bits'] if c['kind']=='serial' else c['ports'][c['result']]['width']
    mask = (1 << width)-1
    rng = random.Random(731)
    rows = []

    def sample(tag, expected=None, **values):
        ins.update(values)
        rows.append({'inputs': dict(ins), 'tag': tag, 'expected': expected or {}})

    def cycle(tag, expected=None, **values):
        sample(tag, **{**values, c['clock']: 0})
        sample(tag, expected, **{c['clock']: 1})

    if c['kind'] == 'priority':
        n = c['ports'][c['data']]['width']
        values = [0, (1<<n)-1] + [1<<i for i in range(n)]
        values += [3<<i for i in range(max(0,n-1))] + [rng.getrandbits(n) for _ in range(24)]
        for value in values:
            expected = 0 if not value else value.bit_length()-1 if c['priority']=='most' else (value & -value).bit_length()-1
            sample('priority', {c['result']: expected}, **{c['data']: value})
    elif c['kind'] == 'rotate':
        hold = sorted(set(range(4))-set(c['directions']))
        for value in [1, mask, mask//3, rng.getrandbits(width)]:
            for _ in range(2):
                cycle('load', **{c['load']: 1, c['control']: hold[0], c['data']: value})
            directions = list(c['directions'])
            for control in [directions[0]]*2 + [directions[1]]*2 + directions + hold:
                cycle('rotate_or_hold', **{c['load']: 0, c['control']: control, c['data']: rng.getrandbits(width)})
            if c['load_priority']:
                for control in c['directions']:
                    cycle('load_priority', **{c['load']: 1, c['control']: control, c['data']: rng.getrandbits(width)})
    elif c['kind'] == 'serial':
        reset = c['reset']
        for _ in range(2):
            cycle('reset', **{reset['name']: reset['active'], c['data']: 1})
        for _ in range(2):
            cycle('idle', {c['done']: 0}, **{reset['name']: 1-reset['active'], c['data']: 1})
        for value in [0, mask, 1, 1<<(width-1), mask//3, (mask//3)^mask]:
            cycle('start', {c['done']: 0}, **{c['data']: 0})
            for bit in range(width):
                cycle('payload_'+str(bit), {c['done']: 0}, **{c['data']: (value>>bit)&1})
            cycle('valid_stop', {c['done']: 1, **({c['result']: value} if c['result'] else {})}, **{c['data']: 1})
        cycle('idle', {c['done']: 0}, **{c['data']: 1})
        # Keep the original normal-frame prefix byte-for-byte in the vector list.
        # Additional scenarios have explicit expectations independent of DUT code.
        def frame(tag, value, stop=1):
            cycle(tag+'_start', {c['done']: 0}, **{c['data']: 0})
            for bit in range(width):
                cycle(tag+'_data_'+str(bit), {c['done']: 0},
                      **{c['data']: (value>>bit)&1})
            expected = {c['done']: int(bool(stop))}
            if stop and c['result']:
                expected[c['result']] = value
            cycle(tag+'_stop', expected, **{c['data']: stop})

        # The existing serial harness samples at the positive edge. Do not extend
        # these temporal claims to an unspecified or different clock interpretation.
        if c.get('edge') == 'posedge' and reset['synchronous']:
            if c.get('recovery_policy'):
                for low_cycles in (1, 2, width+1):
                    frame('badframe', mask//3, stop=0)
                    for _ in range(low_cycles):
                        cycle('recovery_wait', {c['done']: 0}, **{c['data']: 0})
                    cycle('recovery_delimiter', {c['done']: 0}, **{c['data']: 1})
                    frame('recovered', mask^(1<<(width-1)))
                    cycle('recovered_idle', {c['done']: 0}, **{c['data']: 1})
            # A reset abandons partial reception. Payload is deliberately not
            # compared during reset or outside a valid completion window.
            for consumed in sorted({0, width//2, width-1}):
                cycle('interrupted_start', {c['done']: 0}, **{c['data']: 0})
                for bit in range(consumed):
                    cycle('interrupted_data_'+str(bit), {c['done']: 0},
                          **{c['data']: (mask>>bit)&1})
                cycle('reset_in_frame', {c['done']: 0},
                      **{reset['name']: reset['active'], c['data']: 0})
                cycle('reset_release', {c['done']: 0},
                      **{reset['name']: 1-reset['active'], c['data']: 1})
                frame('afterreset', mask//3)
                cycle('afterreset_idle', {c['done']: 0}, **{c['data']: 1})
    else:
        reset = c['reset']
        for _ in range(2):
            cycle('reset', **{reset['name']: reset['active']})
        if c['kind'] == 'bcd':
            limit = min(10**c['digits']+2, 12000)
            for _ in range(limit):
                cycle('count', **{reset['name']: 1-reset['active']})
        else:
            enable = c['enable']
            for index in range(48):
                values = {reset['name']: reset['active'] if index in (17,35) else 1-reset['active'],
                          c['data']: rng.getrandbits(width)}
                if enable:
                    values[enable] = ((1<<c['ports'][enable]['width'])-1) if index==0 else index % (1<<c['ports'][enable]['width'])
                cycle('capture_or_reset', **values)
    return rows


def _observation(value, width):
    if isinstance(value, int):
        return value, (1<<width)-1
    if value is None:
        return 0, 0
    bits = str(value).lower().zfill(width)[-width:]
    return int(''.join(b if b in '01' else '0' for b in bits),2), int(''.join('1' if b in '01' else '0' for b in bits),2)


def check_samples(contract, vectors, observed):
    """Check observations against fixed public semantics, not against another DUT."""
    if contract.get('kind') == 'comb_fsm':
        return comb_check(contract,vectors,observed)
    if contract.get('kind') == 'public_fsm':
        return check_fsm_samples(contract, vectors, observed)
    if not vectors or len(vectors) != len(observed):
        return {'status': 'unverified', 'reason': 'incomplete public observations', 'checks': 0}
    c = contract
    width = c['payload_bits'] if c['kind']=='serial' else c['ports'][c['result']]['width']
    mask = (1<<width)-1
    edges = ['posedge', 'negedge'] if c.get('edge') == 'either' else [c.get('edge')]
    outcomes = []
    for edge in edges:
        state, known, checks, examples = 0, 0, 0, []
        bad_public_cells = set()
        previous_clock = 0
        example_tags = set()
        count = None
        for index,(row, actual) in enumerate(zip(vectors, observed)):
            inputs, tag = row['inputs'], row['tag']
            expected = dict(row['expected'])
            clock = inputs.get(c.get('clock'), 0)
            active_edge = (previous_clock==0 and clock==1) if edge=='posedge' else (previous_clock==1 and clock==0)
            previous_clock = clock
            active_reset = bool(c.get('reset')) and inputs[c['reset']['name']] == c['reset']['active']
            if c['kind'] in ('rotate', 'capture'):
                if c['kind']=='capture' and active_reset and (active_edge or not c['reset']['synchronous']):
                    reset_value = c['reset']['value']
                    state, known = (reset_value & mask, mask) if reset_value is not None else (0,0)
                elif active_edge and not active_reset:
                    if c['kind']=='rotate':
                        if inputs[c['load']]:
                            state, known = inputs[c['data']], mask
                        else:
                            direction = c['directions'].get(inputs[c['control']])
                            if direction == 'left':
                                state = ((state<<1)|(state>>(width-1))) & mask
                                known = ((known<<1)|(known>>(width-1))) & mask
                            elif direction == 'right':
                                state = (state>>1)|((state&1)<<(width-1))
                                known = (known>>1)|((known&1)<<(width-1))
                    else:
                        lanes = c['lanes'] if c['enable'] else [[0,width-1,0]]
                        for lane,hi,lo in lanes:
                            if not c['enable'] or (inputs[c['enable']]>>lane)&1:
                                bits = ((1<<(hi-lo+1))-1)<<lo
                                state = (state & ~bits)|(inputs[c['data']] & bits)
                                known |= bits
                if known:
                    expected[c['result']] = [state, known]
            elif c['kind'] == 'bcd':
                value, value_known = _observation(actual.get(c['result']), width)
                valid = value_known == mask and all(((value>>(4*i))&15)<10 for i in range(c['digits']))
                if active_edge and active_reset:
                    # Unspecified reset value is anchored only after a real reset edge.
                    count = sum(((value>>(4*i))&15)*10**i for i in range(c['digits'])) if valid else None
                    if c['reset']['value'] is not None:
                        count = sum(((c['reset']['value']>>(4*i))&15)*10**i for i in range(c['digits']))
                elif active_edge and count is not None:
                    count = (count+1) % (10**c['digits'])
                if count is not None:
                    expected[c['result']] = sum(((count//(10**i))%10)<<(4*i) for i in range(c['digits']))
                    if c.get('carry_enable'):
                        expected[c['carry_enable']] = sum(int(count % (10**k)==10**k-1) << (k-1) for k in range(1,c['digits']))
                    if c.get('enable'):
                        expected[c['enable']] = int(count%10==9)
                if active_edge and not valid and count is None:
                    expected[c['result']] = 'valid decimal digits after a clock edge'
            # Serial expected bytes are checked only on specified valid-stop edges.
            for name, value in expected.items():
                checks += 1
                want, bits = value if isinstance(value,list) else (value, (1<<c['ports'][name]['width'])-1)
                actual_width = c['ports'][name]['width']
                got, got_known = _observation(actual.get(name), actual_width)
                mismatch = (got_known & bits) != bits or not isinstance(want,int) or (got & bits) != (want & bits)
                if c['kind'] == 'public_table':
                    for bit in range(actual_width):
                        if bits >> bit & 1 and (not (got_known >> bit & 1) or ((got ^ want) >> bit & 1)):
                            bad_public_cells.add((tuple(sorted(inputs.items())), name, bit))
                key = (name, tag.split('_')[0])
                if mismatch and key not in example_tags and len(examples)<3:
                    example_tags.add(key)
                    examples.append({'sample': index, 'operation': tag, 'output': name,
                                     'expected': want, 'mask': bits, 'actual': got if got_known == (1<<actual_width)-1 else actual.get(name),
                                     'inputs': inputs, 'recent_inputs': [v['inputs'] for v in vectors[max(0,index-4):index]]})
                    examples[-1]['previous_outputs'] = observed[index-1] if index else {}
                    if c['kind'] == 'serial':
                        examples[-1]['recent_samples'] = [
                            {'sample': j, 'operation': vectors[j]['tag'],
                             'inputs': vectors[j]['inputs'], 'outputs': observed[j],
                             'expected': vectors[j]['expected']}
                            for j in range(max(0,index-12),index+1)]
        outcomes.append({'status': 'fail' if examples else 'pass' if checks else 'unverified',
                         'checks': checks, 'samples': len(observed), 'counterexamples': examples,
                         'clock_interpretation': edge})
        if c['kind'] == 'public_table':
            outcomes[-1]['bad_public_cells'] = [[dict(inputs), name, bit]
                                               for inputs, name, bit in sorted(bad_public_cells)]
    passing = next((result for result in outcomes if result['status']=='pass'), None)
    result = passing or outcomes[0]
    if not passing and len(outcomes)>1:
        result['other_clock_interpretations'] = outcomes[1:]
    if c['kind'] == 'serial':
        result['public_scenario_coverage'] = {
            'scope': 'finite exercised scenarios; not a complete functional proof',
            'recovery_policy': c.get('recovery_policy', 'not_inferred'),
            'checked_sample_counts': {
                group: sum(bool(row['expected']) and row['tag'].split('_')[0] == group
                           for row in vectors)
                for group in sorted({row['tag'].split('_')[0] for row in vectors
                                     if row['expected']})}}
    return result
