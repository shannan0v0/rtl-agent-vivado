"""Bounded candidate-source dependency leads and public simulation windows.

No golden RTL, fixture or task-ID lookup. Static leads are not causal proof.
Unsupported declarations are omitted rather than instrumented speculatively.
"""
import re


def dependency_leads(code, output):
    clean=re.sub(r'/\*.*?\*/|//[^\n]*', '', code, flags=re.S)
    # One module only: never construct hierarchical probes into ambiguous scopes.
    if (len(re.findall(r'\bmodule\b',clean))!=1 or re.search(r'\b(function|task|generate)\b',clean)
            or re.search(r'`(?:ifdef|ifndef|elsif|include|define)\b',clean)):
        return {'status':'unsupported_source','signals':[], 'drivers':[]}
    assignments=[]
    for match in re.finditer(r'(?<![\w$])([A-Za-z_]\w*)\s*(<=|=(?!=))\s*([^;]+);',clean):
        assignments.append((match[1],match[2],match[3].strip()))
    frontier={output};drivers=[];seen=set()
    for depth in range(3):
        following=set()
        for target,op,rhs in assignments:
            if target in frontier and (target,op,rhs) not in seen:
                seen.add((target,op,rhs));drivers.append({'target':target,'op':op,'rhs':rhs[:160]})
                following.update(re.findall(r'\b[A-Za-z_]\w*\b',rhs))
        frontier=following
    declared={}
    module_declarations=re.split(r'\b(?:always|always_ff|always_comb|always_latch|initial)\b',clean,maxsplit=1)[0]
    for m in re.finditer(r'\b(?:reg|logic|wire)\s*(?:\[\s*(\d+)\s*:\s*(\d+)\s*\])?\s+([^;()]+);',module_declarations):
        width=abs(int(m[1])-int(m[2]))+1 if m[1] else 1
        for n in m[3].split(','):
            if re.fullmatch(r'\s*[A-Za-z_]\w*\s*',n) and width<=64:declared[n.strip()]=width
    targets={d['target'] for d in drivers}
    deps={n for d in drivers for n in re.findall(r'\b[A-Za-z_]\w*\b',d['rhs'])}
    signals=[{'name':n,'width':declared[n]} for n in sorted((targets|deps)&declared.keys())][:8]
    return {'status':'bounded_static_leads','signals':signals,'drivers':drivers[:10],
            'limitation':'Assignment dependencies only; branches and reachability are not proven.'}


def attach_window(result, vectors, observed, internals, leads):
    if result.get('status')!='fail' or not result.get('counterexamples'):return
    index=result['counterexamples'][0]['sample']
    result['short_trace']={'source_leads':leads,'window':[
        {'sample':i,'event':vectors[i]['tag'],'inputs':vectors[i]['inputs'],
         'expected':vectors[i]['expected'],'observed':observed[i],
         **({'candidate_before':internals[i]['before'],'candidate_after':internals[i]['after']}
            if internals else {})}
        for i in range(max(0,index-3),min(len(vectors),index+2))],
        'limitation':'Candidate values observed around public stimulus; leads are hypotheses, not reference evidence.'}


def trace_feedback(result):
    trace=result.get('short_trace')
    if not trace:return ''
    import json
    # Keep the actual request consumer's 6000-character tail intact.
    compact=json.dumps(trace,separators=(',',':'),sort_keys=True)
    if len(compact)>3400:
        trace=dict(trace,window=trace['window'][-3:])
        compact=json.dumps(trace,separators=(',',':'),sort_keys=True)
    if len(compact)>3400:
        trace=dict(trace,source_leads={'status':'omitted_for_budget'},window=trace['window'][-2:])
        compact=json.dumps(trace,separators=(',',':'),sort_keys=True)
    if len(compact)>3400:
        compact=json.dumps({'limitation':'Trace exceeds request budget; full evidence retained on disk.','window':[]})
    return '\nPUBLIC_SHORT_TRACE: '+compact+'\nLocate the first violated public edge obligation in this candidate. '+\
        'Use before/after values and assignment dependencies to identify the storage or driver involved. '+\
        'Repair its cause while preserving correct reset, widths, other outputs and terminal holds. '+\
        'Return complete RTL; these finite checks do not prove unchecked behavior.'
