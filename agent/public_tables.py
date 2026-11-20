"""Public table bindings only. No task identifiers, reference RTL or DUT generation."""
import re


def _axis(label, available):
    text=re.sub(r'\s+','',label)
    if len(text)>128:raise ValueError('axis label exceeds bounded format')
    solutions=[]
    def visit(rest,parts):
        if len(solutions)>1:return
        if not rest:
            solutions.append(parts);return
        for bit in available:
            if rest.startswith(bit):visit(rest[len(bit):],parts+[bit])
    visit(text,[])
    if len(solutions)!=1 or len(set(solutions[0]))!=len(solutions[0]):
        raise ValueError('ambiguous or unknown axis bit labels')
    return solutions[0]


def _input_values(bits, values, ports):
    packed={n:0 for n,p in ports.items() if p['direction']=='input'}
    for bit,value in zip(bits,values):
        m=re.fullmatch(r'([A-Za-z_]\w*)(?:\[(\d+)\])?',bit)
        name,index=m[1],int(m[2] or 0)
        packed[name]|=int(value)<<index
    return packed


def parse_public_table(prompt, interface):
    kmap=bool(re.search(r'Karnaugh map',prompt,re.I))
    truth=bool(re.search(r'truth table',prompt,re.I))
    if not kmap and not truth:return None
    def bad(reason):return {'status':'unverified','reason':'public table: '+reason}
    ports={n:{'direction':p.direction,'width':int(p.width)} for n,p in interface.ports.items()}
    ins=[n for n,p in ports.items() if p['direction']=='input'];outs=[n for n,p in ports.items() if p['direction']=='output']
    bits=[n if ports[n]['width']==1 else n+'['+str(i)+']' for n in ins for i in range(ports[n]['width'])]
    if not 1<=len(bits)<=6 or not outs:return bad('requires 1..6 fully bound combinational input bits')
    if re.search(r'\b(clock|posedge|negedge|sequential)\b',prompt,re.I):return bad('sequential table is outside combinational scope')
    dontcare=bool(re.search(r"don.t[ -]?care",prompt,re.I))
    def cell(token):
        if token in ('0','1'):return int(token),1
        if token.lower() in ('d','x','-') and dontcare:return 0,0
        raise ValueError('unsupported cell or unspecified dont-care semantics')
    lines=prompt.splitlines();rows=[];mapping={};mux=False
    try:
        if kmap:
            headers=[]
            for i,line in enumerate(lines):
                m=re.fullmatch(r'\s*(.*?)\s+((?:[01]+\s+)+[01]+)\s*',line)
                if m and m[1].strip():headers.append((i,m[1].strip(),m[2].split()))
            if len(headers)!=1:raise ValueError('one explicit map header is required')
            i,row_label,cols=headers[0]
            preceding=[s.strip() for s in lines[:i] if s.strip()]
            if not preceding:raise ValueError('missing column-axis label')
            col_label=preceding[-1]
            selector=re.search(r'takes as input\s*\{\s*([^}]+)\}',prompt,re.I)
            extra=selector[1].replace(' ','').split(',') if selector else []
            if any(not re.fullmatch(r'[A-Za-z_]\w*',n) for n in extra):raise ValueError('unsupported selector labels')
            col_bits=_axis(col_label,bits+extra);row_bits=_axis(row_label,bits)
            if len(col_bits)+len(row_bits)>6:raise ValueError('map bit budget exceeded')
            if set(col_bits)&set(row_bits):raise ValueError('axes overlap')
            if len(cols)!=2**len(col_bits) or len(set(cols))!=len(cols) or any(len(c)!=len(col_bits) for c in cols):raise ValueError('incomplete or duplicate column labels')
            mux=bool(set(col_bits)-set(bits))
            if mux:
                if col_bits!=extra or set(row_bits)!=set(bits) or len(outs)!=1 or ports[outs[0]]['width']!=len(cols):raise ValueError('selector/output binding is incomplete')
                # Accept only the public numeric selector-to-vector-bit rule,
                # demonstrated for both first values with an explicit continuation.
                examples=re.findall(r'\b'+re.escape(re.sub(r'\s+','',col_label))+r'\s*=\s*([01]+)\s+is\s+connected\s+to\s+'+re.escape(outs[0])+r'\[(\d+)\]',prompt,re.I)
                if len(examples)<2 or any(int(c,2)!=int(b) for c,b in examples) or not {'0'*len(col_bits),format(1,'0'+str(len(col_bits))+'b')}<=set(c for c,_ in examples) or not re.search(r'and so (?:on|in)\b',prompt,re.I):raise ValueError('numeric selector-to-output-bit rule not explicit')
            elif set(col_bits+row_bits)!=set(bits) or len(outs)!=1 or ports[outs[0]]['width']!=1:
                raise ValueError('map does not bind all input bits and one output')
            table=[]
            for line in lines[i+1:]:
                m=re.fullmatch(r'\s*([01]+)\s*\|\s*(.*?)\s*\|?\s*',line)
                if m:
                    tokens=[s.strip() for s in m[2].strip().strip('|').split('|')]
                    if len(tokens)!=len(cols):raise ValueError('map row length differs from header')
                    table.append((m[1],tokens))
            labels=[r for r,_ in table]
            if len(table)!=2**len(row_bits) or len(set(labels))!=len(table) or any(len(r)!=len(row_bits) for r in labels):raise ValueError('incomplete or duplicate row labels')
            for r,tokens in table:
                if mux:
                    value=mask=0
                    for c,token in zip(cols,tokens):
                        v,m=cell(token);value|=v<<int(c,2);mask|=m<<int(c,2)
                    rows.append({'inputs':_input_values(row_bits,r,ports),'expected':{outs[0]:[value,mask]} if mask else {},'label':{'row':r}})
                else:
                    for c,token in zip(cols,tokens):
                        v,m=cell(token)
                        rows.append({'inputs':_input_values(col_bits+row_bits,c+r,ports),'expected':{outs[0]:[v,m]} if m else {},'label':{'column':c,'row':r}})
            mapping={'column_bits':col_bits,'row_bits':row_bits,'column_order':cols,'output_bit_rule':'numeric selector value' if mux else 'single output'}
        else:
            output_bits=[n for n in outs if ports[n]['width']==1]
            headers=[]
            for i,line in enumerate(lines):
                names=[x.strip() for x in line.strip().strip('|').split('|')]
                if len(names)>1 and set(names)==set(bits+output_bits) and len(names)==len(set(names)) and len(output_bits)==len(outs):headers.append((i,names))
            if len(headers)!=1:raise ValueError('one complete named truth-table header is required')
            i,names=headers[0]
            for line in lines[i+1:]:
                tokens=[x.strip() for x in line.strip().strip('|').split('|')]
                if len(tokens)!=len(names) or not all(re.fullmatch(r'[01dxX-]',t) for t in tokens):continue
                values=dict(zip(names,tokens))
                if any(values[b] not in ('0','1') for b in bits):raise ValueError('wildcard input rows not supported')
                expected={}
                for n in outs:
                    v,m=cell(values[n])
                    if m:expected[n]=[v,m]
                rows.append({'inputs':_input_values(bits,[values[b] for b in bits],ports),'expected':expected,'label':{'header':names,'cells':tokens}})
            mapping={'header_order':names}
        keys=[tuple(r['inputs'][n] for n in ins) for r in rows]
        if len(rows)!=2**len(bits) or len(set(keys))!=len(rows):raise ValueError('table input coverage is incomplete or duplicated')
        checked=[n for n in outs if any(n in r['expected'] for r in rows)]
        if not checked:raise ValueError('no constrained output cells')
        return {'status':'supported','kind':'public_table','module':interface.module,'ports':ports,'result':outs[0],
                'checked_outputs':checked,'unchecked_outputs':[n for n in outs if n not in checked],
                'concerns':['Only explicit public table cells are checked; dont-care cells remain unconstrained.']+
                    (['The mux-only structural implementation restriction is not checked by this functional table oracle.'] if mux else []),
                'scope':'exhaustive finite public combinational table cells; not hidden-judge or synthesis proof',
                'table_format':'kmap' if kmap else 'truth','mapping':mapping,'table_rows':rows}
    except (ValueError,KeyError,IndexError) as error:return bad(str(error))


def table_vectors(contract):
    rows=contract['table_rows'];vectors=[]
    # Multiple input orders expose stale/latching outputs without inventing clocks.
    order=list(range(len(rows)))+list(reversed(range(len(rows))))
    for i,index in enumerate(order):
        row=rows[index]
        vectors.append({'inputs':dict(row['inputs']),'expected':dict(row['expected']),'tag':'table'+str(index),'source_cell':row['label']})
    return vectors
