"""Bounded public one-hot combinational relations; never infer clock or RTL."""
import itertools,json,re
from agent.public_fsm import ID,ROW,_number

def parse_comb_fsm(prompt,interface):
    flat=re.sub(r'\s+',' ',prompt)
    if not re.search(r'one[- ]hot',flat,re.I) or not re.search(r'(?:combinational|not the state flip-flops|input of state flip-flop)',flat,re.I):return None
    def reject(reason):return dict(status='unverified',reason='public combinational FSM: '+reason)
    try:
        if re.search(r'\bMealy\b',flat,re.I):raise ValueError('Mealy outputs outside scope')
        ports={n:dict(direction=p.direction,width=int(p.width)) for n,p in interface.ports.items()}
        ins=[n for n,p in ports.items() if p['direction']=='input'];outs=[n for n,p in ports.items() if p['direction']=='output']
        banks=[n for n in ins if ports[n]['width']>1]
        if len(banks)!=1:raise ValueError('one explicit state bank required')
        bank=banks[0];controls=sorted(n for n in ins if n!=bank);width=ports[bank]['width']
        if not 2<=width<=16 or not 1<=len(controls)<=3 or any(ports[n]['width']!=1 for n in controls):raise ValueError('bounded scalar controls required')
        if any(re.fullmatch(r'clk|clock|reset\w*|rst\w*',n,re.I) for n in controls):raise ValueError('sequential ports outside combinational scope')
        rows=[];annotations={}
        def annotation(s,a):
            if a is None:return
            if s in annotations and annotations[s]!=a:raise ValueError('conflicting output annotations')
            annotations[s]=a
        for line in prompt.splitlines():
            if '-->' not in line:continue
            if re.match(r'\s*state\s+\(output\)',line,re.I):continue
            m=ROW.fullmatch(line)
            if not m:raise ValueError('transition line not fully understood')
            rows.append((m[1],m[3],m[4]));annotation(m[1],m[2])
        if not rows:
            header=re.search(r'State\s*\|\s*Next state\s+('+ID+r')=0,\s*Next state\s+\1=1\s*\|\s*Output',flat,re.I)
            if not header or controls!=[header[1]]:raise ValueError('explicit two-column transition table required')
            for line in prompt.splitlines():
                m=re.fullmatch(r'\s*('+ID+r')\s*\|\s*('+ID+r'),\s*('+ID+r')\s*\|\s*([01])\s*',line)
                if m:rows.extend([(m[1],header[1]+'=0',m[2]),(m[1],header[1]+'=1',m[3])]);annotation(m[1],m[4])
        if not rows:raise ValueError('no public transition rows')
        states=sorted({s for s,_,t in rows}|{t for s,_,t in rows})
        if len(states)!=width:raise ValueError('state bank width does not match full state set')
        codes={}
        def bind(s,v):
            if s not in states or v<=0 or v&(v-1) or v>=(1<<width):raise ValueError('state encoding is not a bound one-hot code')
            if s in codes and codes[s]!=v:raise ValueError('conflicting state encoding')
            codes[s]=v
        for s,w,bits in re.findall(r'\b('+ID+r')\s*=\s*(\d+)\s*\x27b([01]+)',flat):
            if int(w)!=width:raise ValueError('encoding width conflict')
            bind(s,int(bits,2))
        for bits,s in re.findall(r'\b([01]{2,})\s*\(('+ID+r')\)',flat):
            if len(bits)!=width:raise ValueError('encoding width conflict')
            bind(s,int(bits,2))
        if not codes:
            numbered=re.search(re.escape(bank)+r'\[0\]\s+through\s+'+re.escape(bank)+r'\[(\d+)\]\s+correspond to the states\s+('+ID+r')\s+(?:through|though)\s+('+ID+r')',flat,re.I)
            ordered=re.search(r'encoding is used:\s*\(([^)]+)\)\s*=\s*\(([^)]+)\)',flat,re.I)
            if numbered:
                first=re.fullmatch(r'([A-Za-z_]+)(\d+)',numbered[2]);last=re.fullmatch(r'([A-Za-z_]+)(\d+)',numbered[3])
                if not first or not last or first[1]!=last[1] or int(first[2])!=0 or int(last[2])!=width-1 or int(numbered[1])!=width-1:raise ValueError('ordinal state mapping incomplete')
                for i in range(width):bind(first[1]+str(i),1<<i)
            elif ordered:
                names=[s.strip() for s in ordered[1].split(',')];tokens=[s.strip() for s in ordered[2].split(',')]
                if len(names)!=width:raise ValueError('ordered encoding state count mismatch')
                if len(tokens)==width and '...' not in tokens:
                    for s,t in zip(names,tokens):bind(s,_number(t))
                elif (tokens.count('...')==1 and tokens.index('...')>=2 and tokens.index('...')==len(tokens)-2
                      and [_number(t) for t in tokens[:-2]]==[1<<i for i in range(len(tokens)-2)]
                      and _number(tokens[-1])==1<<(width-1)):
                    for i,s in enumerate(names):bind(s,1<<i)
                else:raise ValueError('ordered encoding expansion ambiguous')
        if set(codes)!=set(states) or len(set(codes.values()))!=width:raise ValueError('complete unique public state encoding required')
        inputs=[dict(zip(controls,bits)) for bits in itertools.product((0,1),repeat=len(controls))]
        def condition(term,values):
            if term.strip()=='(always go to next cycle)':return True
            if term.strip() in ('0','1') and len(controls)==1:return values[controls[0]]==int(term)
            matched=True
            for part in term.split(','):
                m=re.fullmatch(r'\s*('+ID+r')\s*=\s*([01])\s*',part)
                if not m or m[1] not in values:raise ValueError('condition not completely bound to public inputs')
                matched=matched and values[m[1]]==int(m[2])
            return matched
        transitions={s:[None]*len(inputs) for s in states}
        for s,term,t in rows:
            for i,x in enumerate(inputs):
                if condition(term,x):
                    old=transitions[s][i]
                    if old is not None and old!=t:raise ValueError('conflicting state/input transition')
                    transitions[s][i]=t
        if any(t is None for row in transitions.values() for t in row):raise ValueError('incomplete transition table')
        output_bindings={};vectors=[n for n in outs if ports[n]['width']==width]
        if len(vectors)>1:raise ValueError('next-state vector role ambiguous')
        if vectors:
            name=vectors[0]
            if name!='next_state' and not re.search(r'\b'+re.escape(name)+r'\b\s+is the next[- ]state vector',flat,re.I):
                raise ValueError('next-state vector output role not explicit')
            output_bindings[name]=dict(kind='next_vector')
        for name in outs:
            if name in output_bindings:continue
            m=re.search(r'\b'+re.escape(name)+r'\s*--\s*Assert when next-state is\s+('+ID+r')\s+state',flat,re.I)
            bit=re.search(r'output signal\s+'+re.escape(name)+r'\s+should be the input of state flip-flop\s+'+re.escape(bank)+r'\[(\d+)\]',flat,re.I)
            if m:
                if m[1] not in states:raise ValueError('output names unknown next state')
                output_bindings[name]=dict(kind='next_bit',mask=codes[m[1]])
            elif bit:
                if int(bit[1])>=width:raise ValueError('next-state output bit out of range')
                output_bindings[name]=dict(kind='next_bit',mask=1<<int(bit[1]))
            else:output_bindings[name]=dict(kind='current_output')
        value_names=[n for n in outs if output_bindings[n]['kind']=='current_output']
        if any(ports[n]['width']!=1 for n in value_names):raise ValueError('scalar state outputs required')
        order=value_names
        explicit_order=re.search(r'outputs are given as\s*"\(([^)]+)\)"',flat,re.I)
        if explicit_order:
            order=[n.strip() for n in explicit_order[1].split(',')]
            if set(order)!=set(value_names):raise ValueError('tuple output order not fully bound')
        default_zero=bool(re.search(r'(?:assume outputs are 0|outputs are zero unless otherwise specified)',flat,re.I))
        state_outputs={}
        for s in states:
            a=annotations.get(s);values={n:0 for n in value_names} if default_zero else {}
            if a is not None and a.strip():
                if '=' in a:
                    seen=set()
                    for part in a.split(','):
                        m=re.fullmatch(r'\s*('+ID+r')\s*=\s*([01])\s*',part)
                        if not m or m[1] not in value_names:raise ValueError('output annotation not fully bound')
                        if m[1] in seen:raise ValueError('duplicate output annotation')
                        seen.add(m[1])
                        values[m[1]]=int(m[2])
                elif value_names:
                    tokens=[t.strip() for t in a.split(',')]
                    if len(tokens)!=len(order) or any(t not in ('0','1') for t in tokens):raise ValueError('output tuple incomplete')
                    values.update(zip(order,map(int,tokens)))
            if set(values)!=set(value_names):raise ValueError('public current-state output values incomplete')
            state_outputs[s]=values
        multi=bool(re.search(r'can be a combinational? of multiple states',flat,re.I))
        c=dict(status='supported',kind='comb_fsm',module=interface.module,ports=ports,state_input=bank,codes=codes,control_inputs=inputs,transitions=transitions,state_outputs=state_outputs,output_bindings=output_bindings,multi_hot=multi,checked_outputs=sorted(outs),unchecked_outputs=[],concerns=['Only declared valid one-hot states checked.' if not multi else 'Explicit multiple-state semantics checked by OR of inspection equations.'],scope='explicit complete public one-hot combinational state relation')
        if len(json.dumps({k:c[k] for k in ('codes','transitions','state_outputs','output_bindings')},separators=(',',':')))>3600:raise ValueError('public relation exceeds repair consumer budget')
        return c
    except (ValueError,IndexError) as e:return reject(str(e))

def evaluate(c,state,i):
    next_code=0;output={n:0 for n,b in c['output_bindings'].items() if b['kind']=='current_output'}
    for s,code in c['codes'].items():
        if state&code:
            next_code|=c['codes'][c['transitions'][s][i]]
            for n,v in c['state_outputs'][s].items():output[n]|=v
    for n,b in c['output_bindings'].items():
        if b['kind']=='next_vector':output[n]=next_code
        elif b['kind']=='next_bit':output[n]=int(bool(next_code&b['mask']))
    return output

def comb_vectors(c):
    states=range(1<<c['ports'][c['state_input']]['width']) if c['multi_hot'] else sorted(c['codes'].values())
    if len(states)*len(c['control_inputs'])>65536:raise ValueError('comb FSM stimulus budget exceeded')
    return [dict(inputs={c['state_input']:s,**x},expected=evaluate(c,s,i),tag='public_comb_state') for s in states for i,x in enumerate(c['control_inputs'])]

def comb_check(c,vectors,observed):
    if not vectors or len(vectors)!=len(observed):return dict(status='unverified',reason='incomplete combinational observations',checks=0)
    errors=[];count=0;bad=0;cells=[]
    for i,(row,actual) in enumerate(zip(vectors,observed)):
        for n,want in row['expected'].items():
            count+=1;got=actual.get(n);bits=format(got,'b') if isinstance(got,int) else str(got)
            if not re.fullmatch('[01]+',bits) or int(bits,2)!=want:
                bad+=1;cells.append([i,n])
                if len(errors)<3:errors.append(dict(sample=i,inputs=row['inputs'],output=n,expected=want,actual=got))
    return dict(status='fail' if bad else 'pass',checks=count,samples=len(vectors),mismatches=bad,bad_public_cells=cells,counterexamples=errors,checked_outputs=c['checked_outputs'],unchecked_outputs=[])

def comb_feedback(r,c):
    return 'PUBLIC_COMB_FSM_REPAIR: This is combinational next-state/output logic; do not invent clock, reset or internal state storage. Use the complete public state relation and encoding; keep unrelated verified behavior. Return complete RTL.\n'+json.dumps({k:c[k] for k in ('state_input','codes','transitions','control_inputs','state_outputs','output_bindings','multi_hot')}|dict(counterexamples=r.get('counterexamples',[])[:3]),separators=(',',':'))
