"""Bounded Vivado simulation of public-derived scenarios, never official fixtures."""

import hashlib
import json
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from agent.compile_check import _find_tool
from agent.public_contract import build_vectors, check_samples
from agent.family_knowledge import family_knowledge
from agent.short_trace import dependency_leads, attach_window, trace_feedback
from official_entry.process_supervision import stop_owned_tree


def _harness(contract, vectors, nonce, probes=()):
    ports = contract['ports']
    prefix = '__rtl_public_'
    while any(name.startswith(prefix) for name in [*ports,contract['module']]):
        prefix += '_'
    top, instance, memory_name, index = [prefix+name for name in ('tb','dut','vectors','i')]
    inputs = [n for n,p in ports.items() if p['direction']=='input']
    outputs = [n for n,p in ports.items() if p['direction']=='output']
    input_width = sum(ports[n]['width'] for n in inputs)
    declarations = ['{} [{}:0] {};'.format('reg' if p['direction']=='input' else 'wire', p['width']-1,n)
                    for n,p in ports.items()]
    source = ('`timescale 1ns/1ps\nmodule '+top+';\n'+'\n'.join(declarations)+
              '\n'+contract['module']+' '+instance+'('+','.join('.'+n+'('+n+')' for n in ports)+');\n'+
              'reg [{}:0] {} [0:{}]; integer {};\n'.format(input_width-1,memory_name,len(vectors)-1,index)+
              'initial begin\n'+''.join(n+'=0;\n' for n in inputs)+
              '$readmemh("vectors.mem",'+memory_name+');\n'+
              'for({0}=0;{0}<{1};{0}={0}+1) begin\n'.format(index,len(vectors))+
              '{'+','.join(inputs)+'}='+memory_name+'['+index+']; #5;\n'+
              '$display("PUBLIC_SAMPLE_'+nonce+' %0d %b",'+index+',{'+','.join(outputs)+'});\nend\n'+
              '$display("PUBLIC_DONE_'+nonce+' {}"); $finish;\nend\nendmodule\n'.format(len(vectors)))
    lines = []
    if probes:
        names=','.join(instance+'.'+p['name'] for p in probes)
        source=source.replace('{'+','.join(inputs)+'}='+memory_name,
            '$display("PUBLIC_BEFORE_'+nonce+' %0d '+ ' '.join('%b' for _ in probes)+'",'+index+','+names+');\n{'+','.join(inputs)+'}='+memory_name)
        source=source.replace('$display("PUBLIC_SAMPLE_',
            '$display("PUBLIC_AFTER_'+nonce+' %0d '+' '.join('%b' for _ in probes)+'",'+index+','+names+');\n$display("PUBLIC_SAMPLE_')
    for row in vectors:
        value = 0
        for name in inputs:
            value = (value<<ports[name]['width'])|row['inputs'][name]
        lines.append(format(value,'0{}x'.format((input_width+3)//4)))
    return source, '\n'.join(lines)+'\n', outputs, top


def functional_check(code, contract, out_dir, *, timeout_s):
    started = time.monotonic()
    deadline = started+max(0,timeout_s)
    if contract.get('status') != 'supported':
        return {'status': 'unverified', 'reason': contract.get('reason','unsupported public contract')}
    tools = {name: _find_tool(name) for name in ('xvlog','xelab','xsim')}
    if not all(tools.values()):
        return {'status': 'unavailable', 'reason': 'missing Vivado functional-check tools'}
    vectors = build_vectors(contract)
    if not vectors or len(vectors)>50000:
        return {'status': 'unverified', 'reason': 'public stimulus budget not supported'}
    out_dir.mkdir(parents=True,exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='public-check-',dir=out_dir)).resolve()
    nonce = uuid.uuid4().hex
    leads=dependency_leads(code,contract['result']) if contract.get('kind')=='cycle_events' else {}
    probes=[p for p in leads.get('signals',[]) if p['name'] not in contract['ports']]
    source, memory, outputs, top = _harness(contract,vectors,nonce,probes)
    (work/'candidate.sv').write_text(code,encoding='utf-8')
    (work/'public_tb.sv').write_text(source,encoding='utf-8')
    (work/'vectors.mem').write_text(memory,encoding='utf-8')
    (work/'vectors.json').write_text(json.dumps(vectors,sort_keys=True),encoding='utf-8')
    result = {'status': 'unavailable', 'work_dir': str(work), 'scope': contract['scope'],
              'checked_outputs': contract['checked_outputs'], 'unchecked_outputs': contract['unchecked_outputs'],
              'contract_sha256': hashlib.sha256(json.dumps(contract,sort_keys=True).encode()).hexdigest(),
              'candidate_sha256': hashlib.sha256(code.encode()).hexdigest(),
              'testbench_sha256': hashlib.sha256(source.encode()).hexdigest(),
              'vectors_sha256': hashlib.sha256(json.dumps(vectors,sort_keys=True).encode()).hexdigest(), 'stages': []}
    commands = [('xvlog',[tools['xvlog'],'-sv','candidate.sv','public_tb.sv']),
                ('xelab',[tools['xelab'],top,'-s','public_check','-timescale','1ns/1ps','-mt','off']),
                ('xsim',[tools['xsim'],'public_check','-R','--nolog'])]
    for name,command in commands:
        remaining = deadline-time.monotonic()-3
        if remaining<=0:
            result.update(status='timeout',reason='public-check budget exhausted')
            break
        path = work/(name+'.stdout.log')
        stage = {'tool':name,'command':command,'log':str(path)}
        result['stages'].append(stage)
        try:
            with path.open('wb') as log:
                process = subprocess.Popen(command,cwd=work,stdout=log,stderr=subprocess.STDOUT)
                try:
                    stage['rc'] = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    stop_owned_tree(process,grace_s=0.25)
                    result.update(status='timeout',reason='public-check '+name+' timed out')
                    break
        except OSError as error:
            result.update(status='unavailable',reason=type(error).__name__+': '+str(error)[:500])
            break
        if path.stat().st_size>8*1024*1024:
            result.update(status='unavailable',reason='public-check log exceeded evidence budget')
            break
        text = path.read_text(encoding='utf-8',errors='replace')
        if stage['rc'] != 0 or re.search(r'^\s*(?:ERROR|FATAL):|LICENSE_ERROR|licen[sc]e.*(?:fail|not found)|failed.*licen[sc]e',text,re.M|re.I):
            result.update(status='unavailable',reason='public-check tool/harness failed; not a confirmed DUT counterexample',excerpt=text[-2000:])
            break
        if name=='xelab' and 'Built simulation snapshot public_check' not in text:
            result.update(status='unavailable',reason='public-check elaboration did not produce a snapshot')
            break
        if name=='xsim':
            matches = re.findall(r'^PUBLIC_SAMPLE_'+nonce+r' (\d+) ([01xz]+)\s*$',text,re.M|re.I)
            if (re.findall(r'^PUBLIC_DONE_'+nonce+r' (\d+)\s*$',text,re.M) != [str(len(vectors))]
                    or [int(n) for n,_ in matches] != list(range(len(vectors)))):
                result.update(status='unavailable',reason='public-check trace incomplete or malformed')
                break
            observed = []
            total_width = sum(contract['ports'][n]['width'] for n in outputs)
            malformed = False
            for _,bits in matches:
                if len(bits) != total_width:
                    malformed = True
                    break
                values, offset = {},0
                for port in outputs:
                    width = contract['ports'][port]['width']
                    values[port] = bits[offset:offset+width]
                    offset += width
                observed.append(values)
            if malformed:
                result.update(status='unavailable',reason='public-check output bit widths differ from contract')
                break
            result.update(check_samples(contract,vectors,observed))
            if contract.get('kind')=='cycle_events':
                internals=[]
                if probes:
                    sides={side:re.findall(r'^PUBLIC_'+side+'_'+nonce+r' (\d+) ([01xz ]+)\s*$',text,re.M|re.I) for side in ('BEFORE','AFTER')}
                    valid=all([int(i) for i,_ in sides[side]]==list(range(len(vectors))) and
                              all(len(bits.split())==len(probes) for _,bits in sides[side]) for side in sides)
                    if valid:
                        internals=[{side.lower():dict(zip([p['name'] for p in probes],sides[side][i][1].split())) for side in sides} for i in range(len(vectors))]
                attach_window(result,vectors,observed,internals,leads)
                (work/'short_trace.json').write_text(json.dumps(result.get('short_trace',{}),sort_keys=True))
    result['elapsed_s'] = round(time.monotonic()-started,6)
    return result


def functional_feedback(result, contract, candidate=''):
    if contract['kind']=='comb_fsm':
        from agent.comb_fsm import comb_feedback
        return comb_feedback(result,contract)
    if contract['kind']=='public_fsm':
        from agent.public_fsm import fsm_feedback
        return fsm_feedback(result,contract)
    if contract['kind']=='counter_events' or contract.get('carry_enable'):
        from agent.counter_boundaries import counter_feedback
        return counter_feedback(result,contract)
    if contract['kind']=='public_table':
        from agent.table_solver import table_feedback
        return table_feedback(result, contract)
    hints = {
        'cycle_events': 'Build a pre-edge/post-edge cycle plan for state, sampled input, next-state, '
                        'stored data and each public output together. A nonblocking right-hand side '
                        'reads the OLD register value. A second register decoding the old FSM state '
                        'can add a cycle, while a combinational input comparison can lose a required '
                        'full-cycle pulse. Use the public event and counterexample to decide which '
                        'storage is required; do not blindly remove output registers or replace all '
                        'state references with next_state. Preserve state-encoding fixes, reset polarity, '
                        'legal interface, terminal holds and other correctly timed outputs.',
        'bcd': 'Every decimal digit, including the MOST significant, must wrap from 9 to 0. '
               'A hexadecimal A in any digit is invalid BCD. Carry into an upper digit depends '
               'on ALL lower digits being 9, not a bit of the upper digit. Rebuild a uniform '
               'digit-update process covering ALL public digits instead of an exceptional final increment. '
               'Evaluate carries from pre-edge contents and apply the SAME 9-to-0 rule to the highest digit.',
        'rotate': 'LOAD must update the visible output at that clock edge. All subsequent rotations '
                  'must use and update the CURRENT stored contents; a load-only shadow is stale. '
                  'Use one visible stored vector, updated by every load/rotation operation. '
                  'Preserve the public direction codes below: LEFT moves each bit to the next HIGHER index '
                  'and wraps the old highest bit to index zero; RIGHT does the inverse. Rotate exactly ONE bit.',
        'serial': 'The edge detecting start already consumes start. Capture exactly the public number '
                  'of data bits on distinct following clock edges. Store bit count and payload in CLOCKED '
                  'registers. Use a cycle plan: edge 0 consumes start, edges 1 through N capture data, '
                  'edge N+1 consumes stop. LSB-first capture starts at payload index zero. '
                  'Do not add a state that waits for a second start. Default done low each clock. '
                  'Publish the byte and one done pulse on the valid stop edge, never on a data edge.',
        'priority': 'Return the selected bit POSITION, not the logarithm of the numeric input. '
                    'Non-power-of-two and multiple-asserted-bit inputs must obey the public priority.',
        'capture': 'Capture all enabled lanes in parallel at the specified edge, without a pipeline stage. '
                   'Disabled lanes hold. Preserve the public reset timing, polarity, value and priority.'}

    if contract['kind'] == 'serial' and not contract.get('result'):
        hints['serial'] = (
            'Separate receive position from output projection. The edge detecting start consumes start; '
            'consume exactly N following data edges and check stop on edge N+1. Store position in '
            'clocked state. Assert the public completion output once at valid stop, never on a data '
            'edge. No payload output exists here; do not invent one. Reset abandons the partial frame.')

    def literal(name, value):
        width=contract['ports'][name]['width']
        if isinstance(value,str) and re.fullmatch('[01]+',value): value=int(value,2)
        if isinstance(value,int): return str(width)+"'h"+format(value,'0{}x'.format((width+3)//4))
        if isinstance(value,str) and set(value.lower()) <= {'x','z'}: return 'unknown ('+str(width)+' bits)'
        return str(width)+"'b"+str(value)

    def example(item):
        name=item['output']
        value = {'operation':item['operation'],'sample':item['sample'],'output':name,
                'expected':literal(name,item['expected']) if isinstance(item['expected'],int) else item['expected'],
                'actual':literal(name,item['actual']),
                'compare_mask':literal(name,item.get('mask',(1<<contract['ports'][name]['width'])-1)),
                'inputs':{n:literal(n,v) for n,v in item['inputs'].items()},
                'previous_outputs':{n:literal(n,v) for n,v in item.get('previous_outputs',{}).items()}}
        if contract['kind'] == 'serial':
            # Full half-cycle evidence stays in functional_result.json. The
            # unchanged request producer retains only the last6000 characters,
            # so send a compact active-edge window instead of duplicating it.
            active = 0 if contract.get('edge') == 'negedge' else 1
            recent = [s for s in item.get('recent_samples', [])
                      if s['inputs'].get(contract['clock']) == active][-4:]
            value['public_cycle_trace'] = [
                {'sample': s['sample'], 'stage': s['operation'],
                 'in': literal(contract['data'],s['inputs'][contract['data']]),
                 'done': literal(contract['done'],s['outputs'][contract['done']]),
                 **({'payload': literal(contract['result'],s['outputs'][contract['result']])} if contract['result'] else {})}
                for s in recent]
        return value

    binding={key:contract[key] for key in ('kind','event','transition','data','result','control','load','clock','reset','done','digits') if key in contract}
    if contract.get('result'): binding['result_width']=contract['ports'][contract['result']]['width']
    if contract.get('edge') != 'either' and contract.get('edge'): binding['clock_edge']=contract['edge']
    if 'directions' in contract:
        binding['direction_codes']={format(code,'02b'):side for code,side in contract['directions'].items()}
    if contract['kind']=='serial': binding['payload_bits']=contract['payload_bits']
    if contract.get('recovery_policy'):
        binding['recovery_policy'] = contract['recovery_policy']
        hints['serial'] += (' If the expected stop is missing, discard that frame. '
                            'A later high bit is resynchronization, not a valid-frame completion. '
                            'Accept the next start only after recovery, preserving normal-frame '
                            'last-bit capture, bit order and one-cycle completion.')
    declarations=[]
    for name,port in contract['ports'].items():
        if port['direction']!='output': continue
        found=re.search(r'\boutput\s+(?:(?:wire|reg|logic|signed|unsigned)\s+)*(?:\[[^\]]+\]\s*)?'+re.escape(name)+r'\b',candidate)
        if found: declarations.append(found[0])
    feedback = ('The candidate compiled, but a self-check derived ONLY from the public specification failed. '
            'These are public-scenario counterexamples, not official reference/test results. '
            'Correct the root behavior, not a single input. Compare only compare_mask bits; '
            'other bits are unspecified. Keep legal register/output declarations.\n'+
            hints[contract['kind']]+'\n'+
            family_knowledge(contract)+'\n'+
            json.dumps({'public_binding':binding,'counterexamples':[example(e) for e in result.get('counterexamples',[])[:3]],
                        'previous_compilable_output_declarations':declarations,
                        'both_clock_interpretations_failed':bool(result.get('other_clock_interpretations')),
                        **({'public_scenario_coverage':result.get('public_scenario_coverage')}
                           if contract['kind']=='serial' else {}),
                        'unverified_outputs':result['unchecked_outputs'],'public_concerns':contract['concerns']},sort_keys=True))
    if contract['kind']=='cycle_events' and result.get('short_trace'):
        # Put actionable evidence last so the existing bounded consumer retains it.
        feedback=('PUBLIC_CYCLE_REPAIR: The candidate failed a finite public-only self-check. '
                  'A nonblocking RHS reads the pre-edge register. Preserve interface, reset, '
                  'state widths and unrelated behavior. The candidate code accompanies this request.\n'+
                  json.dumps({'binding':binding,'first_counterexample':example(result['counterexamples'][0]),
                              'unchecked_outputs':result['unchecked_outputs']},separators=(',',':'))+
                  trace_feedback(result))
    return feedback
