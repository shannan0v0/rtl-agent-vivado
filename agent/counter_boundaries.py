"""Bounded counters from explicit public prose; no task IDs, answers or DUT writer."""
import json
import re


def parse_counter_events(prompt, interface, reset_resolver):
    flat=re.sub(r'\s+',' ',prompt)
    pulse=re.search(r'assert\s+(\w+)\s+for\s+(\d+)\s+cycles,?\s+then\s+0\s+forever\s*\(until reset\)',flat,re.I)
    falling=re.search(r'falls? for more than\s+(\d+)\s+clock cycles then hits the \w+',flat,re.I)
    if not pulse and not falling:return None
    ports={n:{'direction':p.direction,'width':int(p.width)} for n,p in interface.ports.items()}
    ins={n for n,p in ports.items() if p['direction']=='input'}
    outs={n for n,p in ports.items() if p['direction']=='output'}
    def fail(reason):return dict(status='unverified',reason='public counter contract: '+reason)
    clocks=re.findall(r'(positive|rising|negative|falling) edge of (?:the )?(\w+)',flat,re.I)
    conventional=[n for n in ins if n.lower() in ('clk','clock')]
    clocks=[(side,conventional[0] if n=='clock' and n not in ins and len(conventional)==1 else n) for side,n in clocks]
    clocks={(n,'negedge' if side.lower() in ('negative','falling') else 'posedge') for side,n in clocks if n in ins}
    if len(clocks)!=1:return fail('unique explicit clock edge required')
    clock,edge=next(iter(clocks))
    try:reset=reset_resolver()
    except ValueError:
        match=re.search(r'(\w+) is positive edge triggered asynchronous reset',flat,re.I)
        if not match or match[1] not in ins:return fail('reset binding not explicit')
        reset=dict(name=match[1],active=1,synchronous=False,value=None)
    c=dict(status='supported',kind='counter_events',module=interface.module,ports=ports,clock=clock,edge=edge,reset=reset,concerns=[],scope='finite public counter boundary scenarios; not complete FSM proof')
    if pulse:
        result=pulse[1];length=int(pulse[2])
        if not 1<=length<=64 or outs!={result} or ins!={clock,reset['name']} or ports[result]['width']!=1 or not reset['synchronous']:return fail('unsupported finite reset window')
        c.update(event='reset_count_window',result=result,length=length)
    else:
        threshold=int(falling[1])
        if not 1<=threshold<=64 or not re.search(r'no upper limit',flat,re.I) or not re.search(r'do not splatter in mid-air',flat,re.I):return fail('threshold lifetime not explicit')
        def role(pattern):
            m=re.search(pattern,flat,re.I)
            if not m:raise ValueError('unbound public role: '+pattern)
            return m[1]
        try:
            left=role(r'walking left\s*\((\w+) is 1\)');right=role(r'walking right\s*\((\w+) is 1\)')
            bump_left=role(r'bumped on the left\s*\(by receiving a 1 on (\w+)\)')
            bump_right=role(r'bumped on the right\s*\(by receiving a 1 on (\w+)\)')
            ground=role(r'when (\w+)=0,? the \w+ will fall')
            dig=role(r'it starts \w+ when (\w+)=1')
            fall=role(r'will fall and say ["\s]*(\w+)!')
            remain=outs-{left,right,fall}
            if len(remain)!=1:raise ValueError('digging output not unique')
            digging=next(iter(remain))
            if ins!={clock,reset['name'],bump_left,bump_right,ground,dig} or outs!={left,right,fall,digging} or any(p['width']!=1 for p in ports.values()):raise ValueError('public scalar roles differ')
            if not re.search(r'fall has higher precedence than (?:dig|'+re.escape(dig)+r'),? which has higher precedence than switching directions',flat,re.I):raise ValueError('priority not explicit')
            obligations=[r'same direction as before the fall',r'being bumped while falling does not affect',r'being bumped while \w+ has no effect',r'all 4 outputs become 0\).*?forever',r'reset\w* .*? to walk left']
            if not all(re.search(rule,flat,re.I) for rule in obligations):raise ValueError('mode lifetime, direction memory or reset obligations incomplete')
        except ValueError as e:return fail(str(e))
        c.update(event='bounded_fall_lifetime',result=fall,threshold=threshold,left=left,right=right,fall=fall,digging=digging,ground=ground,dig=dig,bump_left=bump_left,bump_right=bump_right)
    c['checked_outputs']=sorted(outs);c['unchecked_outputs']=[]
    return c


def counter_vectors(c):
    values={n:0 for n,p in c['ports'].items() if p['direction']=='input'}
    rows=[];active=int(c['edge']=='posedge');inactive=1-active;rst=c['reset']
    state=None;remaining=None;direction=0;elapsed=0;held={}
    def step(reset=False,**changes):
        nonlocal state,remaining,direction,elapsed,held
        values.update(changes);values[rst['name']]=rst['active'] if reset else 1-rst['active']
        def outputs():
            if c['event']=='reset_count_window':return {c['result']:int(remaining is not None and remaining>0)}
            return {c['left']:int(state=='walk' and direction==0),c['right']:int(state=='walk' and direction==1),c['fall']:int(state=='fall'),c['digging']:int(state=='dig')}
        if reset and not rst['synchronous']:
            state='walk';direction=0;elapsed=0;held=outputs()
        values[c['clock']]=inactive
        rows.append(dict(inputs=dict(values),tag='counter_hold',expected=dict(held)))
        if c['event']=='reset_count_window':
            if reset:remaining=c['length']
            elif remaining is not None:remaining=max(0,remaining-1)
        elif reset:state='walk';direction=0;elapsed=0
        elif state=='fall':
            if values[c['ground']]:state='dead' if elapsed>c['threshold'] else 'walk'
            else:elapsed=min(elapsed+1,c['threshold']+1)
        elif state=='walk':
            if not values[c['ground']]:state='fall';elapsed=1
            elif values[c['dig']]:state='dig'
            elif values[c['bump_left'] if direction==0 else c['bump_right']]:direction=1-direction
        elif state=='dig' and not values[c['ground']]:state='fall';elapsed=1
        held=outputs();values[c['clock']]=active
        tag='counter_reset' if reset else 'counter_boundary'
        rows.append(dict(inputs=dict(values),tag=tag,expected=dict(held)))
    if c['event']=='reset_count_window':
        for interrupted in (0,1,max(1,c['length']-1)):
            step(True);step(True)
            for _ in range(interrupted):step()
            step(True)
            for _ in range(c['length']*3+5):step()
    else:
        t=c['threshold'];wrap=1<<(t+1).bit_length()
        lengths=sorted({1,t-1,t,t+1,wrap-1,wrap,wrap+1,2*wrap+3})
        for trial,length in enumerate(lengths):
            step(True,**{c['ground']:1,c['dig']:0,c['bump_left']:0,c['bump_right']:0})
            if trial%2:step(**{c['bump_left']:1});step(**{c['bump_left']:0})
            if trial%3==0:step(**{c['dig']:1});step(**{c['dig']:0})
            for n in range(length):step(**{c['ground']:0,c['dig']:n%2,c['bump_left']:n%2,c['bump_right']:(n+1)%2})
            step(**{c['ground']:1,c['dig']:1,c['bump_left']:1,c['bump_right']:1})
            for _ in range(8):step(**{c['dig']:0,c['bump_left']:0,c['bump_right']:0})
        step(True,**{c['ground']:1})
    return rows


def counter_feedback(result,c):
    if c['kind']=='bcd':
        q=c['result']
        rules={'carry_into_digit':{str(k):q+f'[{4*k-1}:0] == {4*k}\'h'+('9'*k) for k in range(1,c['digits'])},'enable_packed_bit_to_digit':{str(k-1):k for k in range(1,c['digits'])},'digit_update':'Evaluate all carries from OLD q. For every digit including the highest: reset->0; if enabled and old_digit==9 ->0; if enabled ->old_digit+1; else hold. Enable output describes CURRENT q, not the already-incremented next q.','enable_output':c.get('carry_enable',c.get('enable'))}
    elif c['event']=='reset_count_window':
        rules={'reset_edge_output':1,'remaining_at_reset':c['length'],'after_each_nonreset_edge':'remaining_next = max(remaining_old-1,0); output = remaining_next>0','required_storage_bits':c['length'].bit_length(),'terminal':'zero forever until reset; repeated reset restarts the full window'}
    else:
        rules={'count_convention':'The first transition into falling counts as air edge1. Count only while airborne, including entry. Landing uses the count of preceding air edges.','saturate_at':c['threshold']+1,'required_storage_bits':(c['threshold']+1).bit_length(),'landing_test':f'air_edges_old > {c["threshold"]}','lifetime':'Above threshold stays above threshold for arbitrarily long falls. Do not die in mid-air; on landing transition to permanent terminal state. Reset restores direction and clears lifetime.','preserve':'direction across fall/dig; ignore bump/dig while falling; fall > dig > bump priority; all public outputs decoded from the same current mode'}
    return 'PUBLIC_COUNTER_REPAIR: Public-derived boundary decisions, not a full RTL implementation. Preserve unrelated passing behavior; model must write complete RTL.\n'+json.dumps({'binding':{k:c[k] for k in ('event','clock','edge','reset','result','digits','length','threshold','carry_enable') if k in c},'decisions':rules,'first_counterexample':result.get('counterexamples',[None])[0],'checked_outputs':c['checked_outputs'],'unchecked_outputs':c['unchecked_outputs']},separators=(',',':'))
