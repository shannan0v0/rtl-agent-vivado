"""Finite edge obligations extracted only from explicit public text, never DUTs."""
import random
import re


def parse_cycle_events(prompt, interface, reset_resolver):
    flat = re.sub(r'\s+', ' ', prompt)
    ports = {n:{'direction':p.direction,'width':int(p.width)} for n,p in interface.ports.items()}
    ins = [n for n,p in ports.items() if p['direction']=='input']
    outs = [n for n,p in ports.items() if p['direction']=='output']
    pulse = re.search(
        r'reset (?:signal|input) is de[ -]?asserted.{0,80}?after the next clock edge.{0,90}?'
        r'(?:set|assert) (?:the )?output\s+([A-Za-z_$][\w$]*)\s+to\s+1\s+for (?:one|1) clock cycle', flat, re.I)
    anyedge = bool(re.search(r'detect any edge',flat,re.I))
    rising = bool(re.search(r'(?:positive|rising) edge detection',flat,re.I))
    falling = bool(re.search(r'(?:negative|falling) edge detection',flat,re.I))
    sampled = (anyedge or rising or falling) and bool(re.search(r'(?:the )?cycle after',flat,re.I))
    if not pulse and not sampled:
        return None
    def unverified(reason):
        return {'status':'unverified','reason':'cycle-event contract: '+reason}
    edge_names = re.findall(r'(positive|rising|negative|falling) edge of(?: the)?\s+([A-Za-z_$][\w$]*)',flat,re.I)
    named_clocks = [n for n in ins if ports[n]['width']==1 and
                    (re.fullmatch('clk|clock',n,re.I) or re.search(r'clock input called\s+'+re.escape(n)+r'\b',flat,re.I))]
    clocks = set()
    for direction,name in edge_names:
        if name.lower()=='clock' and name not in ins and len(named_clocks)==1:
            name=named_clocks[0]
        if name in ins and ports[name]['width']==1:
            clocks.add((name,'posedge' if direction.lower() in ('positive','rising') else 'negedge'))
    if len(clocks)!=1:
        return unverified('one explicit clock and sampling edge are required')
    clock,edge = next(iter(clocks))
    c={'status':'supported','kind':'cycle_events','module':interface.module,'ports':ports,'clock':clock,'edge':edge,
       'concerns':[],'scope':'finite explicit public edge obligations only; not complete FSM behavior'}
    if pulse:
        result=pulse[1]
        if result not in outs or ports[result]['width']!=1:
            return unverified('release pulse output must be an explicit one-bit output')
        try:reset=reset_resolver()
        except ValueError as e:return unverified(str(e))
        if not reset['synchronous'] or reset['name']==clock:
            return unverified('only explicit synchronous reset-release pulses are supported')
        terminal_zero=bool(re.search(r'(?:remains?|stays?|maintains?)\s+(?:at\s+)?(?:zero|0)\s+until\s+(?:another|the next|a new)\s+reset',flat,re.I))
        reset_zero=bool(re.search(r'\bwhile\s+'+re.escape(reset['name'])+r'\s+is\s+asserted\s*,?\s*'+re.escape(result)+r'\s+is\s+(?:zero|0)\b',flat,re.I))
        c.update(event='reset_release_pulse',result=result,reset=reset,terminal_zero=terminal_zero,reset_zero=reset_zero)
        c['concerns'].append('Only the specified release pulse is checked; subsequent state transitions and other outputs remain unverified.')
    else:
        data=[n for n in ins if n!=clock]
        if len(data)!=1 or len(outs)!=1 or ports[data[0]]['width']!=ports[outs[0]]['width'] or not 1<=ports[data[0]]['width']<=64:
            return unverified('sampled-edge detector needs one data vector and matching output, width1..64')
        if not anyedge and rising==falling:
            return unverified('transition direction is ambiguous')
        c.update(event='sampled_transition',data=data[0],result=outs[0],transition='any' if anyedge else 'rising' if rising else 'falling')
    c['checked_outputs']=[c['result']]
    c['unchecked_outputs']=[n for n in outs if n!=c['result']]
    return c


def event_vectors(c):
    values={n:0 for n,p in c['ports'].items() if p['direction']=='input'}
    active=1 if c['edge']=='posedge' else 0
    inactive=1-active
    rows=[]
    def row(tag,clock,expected=None,changes=None):
        if changes:values.update(changes)
        values[c['clock']]=clock
        rows.append({'inputs':dict(values),'tag':tag,'expected':{} if expected is None else {c['result']:expected}})
    if c['event']=='reset_release_pulse':
        rst=c['reset'];aux=[n for n in values if n not in (c['clock'],rst['name'])]
        for trial in range(8):
            changes={n:((trial+i)&1)*((1<<c['ports'][n]['width'])-1) for i,n in enumerate(aux)}
            changes[rst['name']]=rst['active']
            zero=0 if c.get('reset_zero') else None
            row('reset_setup',inactive,changes=changes);row('reset_edge',active,zero)
            row('reset_hold',inactive,zero);row('reset_again',active,zero)
            row('release_setup',inactive,changes={rst['name']:1-rst['active']})
            row('release_first_edge',active,1)
            # Change unrelated inputs between edges; the pulse must persist.
            row('release_pulse_hold',inactive,1,{n:values[n]^((1<<c['ports'][n]['width'])-1) for n in aux})
            row('release_second_edge',active,0)
            row('release_pulse_end_hold',inactive,0)
            # A single startup pulse is not a free-running toggle. Assert the
            # long hold only when explicitly stated in the public specification.
            if c.get('terminal_zero'):
                for step in range(3+trial):
                    row('terminal_zero_edge',active,0)
                    row('terminal_zero_hold',inactive,0,{n:values[n]^((1<<c['ports'][n]['width'])-1) for n in aux})
    else:
        n=c['ports'][c['data']]['width'];mask=(1<<n)-1;rng=random.Random(2719)
        previous=None;held=None
        patterns=[0,0,1,1,mask,mask,0]+[1<<i for i in range(n)]+[rng.getrandbits(n) for _ in range(12)]
        for value in patterns:
            row('between_edge_hold',inactive,held,{c['data']:value})
            want=None if previous is None else ((value^previous) if c['transition']=='any' else
                (value & ~previous & mask) if c['transition']=='rising' else (previous & ~value & mask))
            row('sampled_transition_edge',active,want)
            previous=value;held=want
        row('final_output_hold',inactive,held)
    return rows
