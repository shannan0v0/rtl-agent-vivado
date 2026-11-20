"""Public explicit Moore tables only; no task IDs, DUT code or external fixtures."""
import collections
import json
import re

ID = r'[A-Za-z_][A-Za-z0-9_]*'
ROW = re.compile(r'^\s*('+ID+r')(?:\s*\(([^()]*)\))?\s*-{2,}\s*(.*?)\s*-{2,}>\s*('+ID+r')\s*$')


def _number(token):
    token = token.strip().replace('_', '')
    m = re.fullmatch(r"(\d+)'([bBoOdDhH])([0-9a-fA-F]+)", token)
    if m:
        value = int(m[3], {'b':2,'o':8,'d':10,'h':16}[m[2].lower()])
        if value >= 1 << int(m[1]):raise ValueError('public literal truncates')
        return value
    if re.fullmatch(r'0[xX][0-9a-fA-F]+|\d+', token):return int(token,16 if token.lower().startswith('0x') else 10)
    raise ValueError('unsupported public numeric notation')


def parse_public_fsm(prompt, interface, reset_resolver):
    lines = [(i+1,line.strip()) for i,line in enumerate(prompt.splitlines()) if '-->' in line]
    if not lines:return None
    def unavailable(reason, **extra):
        return dict(status='unverified',reason='public FSM: '+reason,
                    reason_code='PUBLIC_FSM_INCOMPLETE_OR_CONFLICTING',**extra)
    try:
        if re.search(r'\bMealy\b',prompt,re.I):raise ValueError('Mealy outputs outside explicit Moore scope')
        parsed=[]
        for line_no,line in lines:
            m=ROW.fullmatch(line)
            if not m:raise ValueError('transition line not fully understood at line '+str(line_no))
            parsed.append((m[1],m[2],m[3],m[4],line_no))
        states=sorted({r[0] for r in parsed}|{r[3] for r in parsed})
        if not 1<=len(states)<=32:raise ValueError('requires1..32states')
        ports={n:dict(direction=p.direction,width=int(p.width)) for n,p in interface.ports.items()}
        inputs=[n for n,p in ports.items() if p['direction']=='input'];outputs=[n for n,p in ports.items() if p['direction']=='output']
        reset=reset_resolver();clock_edges=set()
        conventional=[n for n in inputs if n.lower() in ('clk','clock')]
        for side,name in re.findall(r'(positive|rising|negative|falling) edge of(?: the)?\s+('+ID+r')',prompt,re.I):
            if name.lower()=='clock' and name not in inputs and len(conventional)==1:name=conventional[0]
            if name in inputs and name!=reset['name']:clock_edges.add((name,'posedge' if side.lower() in ('positive','rising') else 'negedge'))
        if len(clock_edges)!=1:raise ValueError('unique explicit clock edge required')
        clock,edge=next(iter(clock_edges))
        reset_state=re.findall(r'\bresets?\s+(?:into|to)\s+state\s+('+ID+r')',prompt,re.I)
        if len(set(reset_state))!=1 or reset_state[0] not in states:raise ValueError('unique public reset state required')
        data=sorted(n for n in inputs if n not in (clock,reset['name']))
        bits=sum(ports[n]['width'] for n in data)
        if not 1<=bits<=8 or len(states)*(1<<bits)>256:raise ValueError('finite table exceeds256state/inputcells')
        if not outputs or sum(ports[n]['width'] for n in outputs)>64:raise ValueError('public outputs outside bounded scope')
        def unpack(value):
            result={}
            for n in reversed(data):result[n]=value&((1<<ports[n]['width'])-1);value>>=ports[n]['width']
            return result
        def condition(text,values):
            if re.fullmatch(r'0|1',text.strip()) and bits==1:return next(iter(values.values()))==int(text.strip())
            terms=text.split(',');matches=True
            for term in terms:
                m=re.fullmatch(r'\s*('+ID+r')(?:\[(\d+)\])?\s*=\s*(\d+)\s*',term)
                if not m:raise ValueError('condition must be explicit input equality conjunction')
                name,index=m[1],m[2]
                if name not in values and index is None:
                    aliases=[(n,name[len(n):]) for n in data if name.startswith(n) and name[len(n):].isdigit()]
                    if len(aliases)!=1:raise ValueError('input condition role is not unique')
                    name,index=aliases[0]
                if name not in values:raise ValueError('condition names non-input')
                want=int(m[3]);got=values[name]
                if index is not None:
                    index=int(index)
                    if not 0<=index<ports[name]['width'] or want not in (0,1):raise ValueError('input bit/value out of range')
                    got=(got>>index)&1
                elif not 0<=want<(1<<ports[name]['width']):raise ValueError('input value out of range')
                if got!=want:matches=False
            return matches
        next_state={s:[None]*(1<<bits) for s in states};origins={}
        for state,annotation,term,dest,line_no in parsed:
            for value in range(1<<bits):
                values=unpack(value)
                if not condition(term,values):continue
                prior=next_state[state][value]
                if prior is not None and prior!=dest:
                    return unavailable('same state/input has different next states',
                        conflict=dict(state=state,inputs=values,next_states=[prior,dest],public_lines=[origins[(state,value)],line_no]))
                next_state[state][value]=dest;origins[(state,value)]=line_no
        missing=[dict(state=s,input_code=i) for s,targets in next_state.items() for i,t in enumerate(targets) if t is None]
        if missing:return unavailable('transition table not complete',missing_cells=missing[:8])
        state_outputs={}
        for state,annotation,term,dest,line_no in parsed:
            if annotation is None:continue
            if len(outputs)==1 and '=' not in annotation:
                values={outputs[0]:_number(annotation)}
            else:
                values={}
                for assignment in annotation.split(','):
                    m=re.fullmatch(r'\s*('+ID+r')\s*=\s*(.*?)\s*',assignment)
                    if not m or m[1] not in outputs:raise ValueError('state output annotation not fully bound')
                    values[m[1]]=_number(m[2])
            if set(values)!=set(outputs):raise ValueError('every public output must be explicit for each state')
            if any(not 0<=v<(1<<ports[n]['width']) for n,v in values.items()):raise ValueError('state output width mismatch')
            if state in state_outputs and state_outputs[state]!=values:raise ValueError('conflicting state outputs')
            state_outputs[state]=values
        if set(state_outputs)!=set(states):raise ValueError('state outputs incomplete; no zero/default inference')
        contract = dict(status='supported',kind='public_fsm',module=interface.module,ports=ports,
            clock=clock,edge=edge,reset=reset,reset_state=reset_state[0],input_order=data,
            states=states,next_state=next_state,state_outputs=state_outputs,
            checked_outputs=sorted(outputs),unchecked_outputs=[],concerns=['Finite reset-reachable transition/hold scenarios, not a complete implementation proof.'],
            public_transition_lines=[r[4] for r in parsed],scope='explicit complete public Moore state table only')
        facts={k:contract[k] for k in ('clock','edge','reset','reset_state','input_order','next_state','state_outputs')}
        if len(json.dumps(facts,separators=(',',':')))>3600:
            raise ValueError('public table does not fit existing bounded repair consumer')
        return contract
    except (ValueError,IndexError) as e:return unavailable(str(e))


def fsm_step(c,state,previous_clock,inputs):
    now=inputs[c['clock']];active=(previous_clock==0 and now==1) if c['edge']=='posedge' else (previous_clock==1 and now==0)
    reset=inputs[c['reset']['name']]==c['reset']['active']
    if reset and (active or not c['reset']['synchronous']):state=c['reset_state']
    elif active and state is not None:
        value=0
        for n in c['input_order']:value=(value<<c['ports'][n]['width'])|inputs[n]
        state=c['next_state'][state][value]
    return state,dict(c['state_outputs'][state]) if state is not None else {}


def fsm_vectors(c):
    inactive=0 if c['edge']=='posedge' else 1;active=1-inactive
    values={n:0 for n,p in c['ports'].items() if p['direction']=='input'};values[c['clock']]=inactive
    state=None;previous_clock=inactive;rows=[]
    def sample(tag,**updates):
        nonlocal state,previous_clock
        values.update(updates);before=state;state,expected=fsm_step(c,state,previous_clock,values);previous_clock=values[c['clock']]
        rows.append(dict(inputs=dict(values),tag=tag,expected=expected,state_before=before,state_after=state))
    def cycle(value,tag):
        changes={};rest=value
        for n in reversed(c['input_order']):changes[n]=rest&((1<<c['ports'][n]['width'])-1);rest>>=c['ports'][n]['width']
        sample(tag+'_hold',**{**changes,c['clock']:inactive});sample(tag+'_edge',**{c['clock']:active})
    def reset():
        sample('reset_setup',**{c['clock']:inactive,c['reset']['name']:c['reset']['active']})
        sample('reset_edge',**{c['clock']:active})
        sample('reset_release_hold',**{c['clock']:inactive,c['reset']['name']:1-c['reset']['active']})
    paths={c['reset_state']:[]};queue=collections.deque(paths)
    while queue:
        s=queue.popleft()
        for value,t in enumerate(c['next_state'][s]):
            if t not in paths:paths[t]=paths[s]+[value];queue.append(t)
    for s,path in paths.items():
        for value in range(len(c['next_state'][s])):
            reset()
            for v in path:cycle(v,'access')
            cycle(value,'transition_'+s)
    reset()
    # A deterministic continuous walk supplements isolated transition coverage.
    for i in range(64):cycle((i*13+i//7)%len(c['next_state'][c['reset_state']]),'continuous')
    reset()
    if len(rows)>50000:raise ValueError('public FSM stimulus budget exceeded')
    return rows


def check_fsm_samples(c,vectors,observed):
    if not vectors or len(vectors)!=len(observed):return dict(status='unverified',reason='incomplete public observations',checks=0)
    checks=0;examples=[];mismatches=0
    for i,(row,actual) in enumerate(zip(vectors,observed)):
        for name,want in row['expected'].items():
            checks+=1;bits=str(actual.get(name,'')) if not isinstance(actual.get(name),int) else format(actual[name],'b')
            valid=bool(re.fullmatch(r'[01]+',bits))
            if not valid or int(bits,2)!=want:
                mismatches+=1
                if len(examples)<3:examples.append(dict(sample=i,operation=row['tag'],inputs=row['inputs'],state_before=row['state_before'],state_after=row['state_after'],output=name,expected=want,actual=actual.get(name),recent_inputs=[r['inputs'] for r in vectors[max(0,i-4):i+1]]))
    transitions={(r['state_before'],tuple(r['inputs'][n] for n in c['input_order'])) for r in vectors if r['tag'].startswith('transition_') and r['tag'].endswith('_edge')}
    return dict(status='fail' if mismatches else 'pass' if checks else 'unverified',checks=checks,samples=len(vectors),mismatches=mismatches,counterexamples=examples,
        checked_outputs=c['checked_outputs'],unchecked_outputs=[],public_transition_cells_exercised=len(transitions))


def fsm_feedback(result,c):
    return 'PUBLIC_FSM_REPAIR: Use only this public Moore table. Update state on the specified edge, derive outputs from CURRENT state, preserve reset and unrelated verified behavior. Model must return complete RTL.\n'+json.dumps(dict(clock=c['clock'],edge=c['edge'],reset=c['reset'],reset_state=c['reset_state'],input_order=c['input_order'],next_state=c['next_state'],state_outputs=c['state_outputs'],counterexamples=result.get('counterexamples',[])[:3]),separators=(',',':'))
