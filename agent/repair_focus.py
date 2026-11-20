"""Candidate-relative public repair guidance; no RTL synthesis or judge access.

Static source leads are hypotheses, not a complete control/dataflow proof.
Only complete, identity-bound finite one-hot checks can change repair routing.
"""
import hashlib
import json
from agent.comb_fsm import comb_vectors
from agent.short_trace import dependency_leads


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def checked_cells(result, contract, vectors):
    total = sum(len(v['expected']) for v in vectors)
    if (result.get('status') not in ('pass', 'fail') or result.get('checks') != total
            or result.get('contract_sha256') != digest(contract)
            or result.get('vectors_sha256') != digest(vectors)):
        return None
    raw = result.get('bad_public_cells')
    if not isinstance(raw, list) or len(raw) != result.get('mismatches'):
        return None
    cells = set()
    for cell in raw:
        if (not isinstance(cell, list) or len(cell) != 2
                or type(cell[0]) is not int or not isinstance(cell[1], str)
                or not 0 <= cell[0] < len(vectors)
                or cell[1] not in vectors[cell[0]]['expected']):
            return None
        cells.add(tuple(cell))
    if len(cells) != len(raw) or bool(cells) != (result['status'] == 'fail'):
        return None
    return cells


def repair_plan(code, contract, current, previous=None):
    legacy = {'action': 'legacy', 'reason': 'no complete bound localization'}
    if (contract.get('kind') != 'comb_fsm' or contract.get('status') != 'supported'
            or contract.get('multi_hot')):
        return legacy
    vectors = comb_vectors(contract)
    bad = checked_cells(current, contract, vectors)
    if (not bad or current.get('candidate_sha256') != hashlib.sha256(code.encode()).hexdigest()):
        return legacy
    prior = checked_cells(previous, contract, vectors) if previous else None
    if previous is not None and prior is None:
        return legacy
    fixed = prior - bad if prior is not None else set()
    introduced = bad - prior if prior is not None else set()
    if prior is not None and not fixed:
        return dict(action='legacy', reason='no previously failing public cell fixed')
    outputs = sorted({n for _, n in bad})
    leads = {n: dependency_leads(code, n) for n in outputs}
    if any(not x.get('drivers') for x in leads.values()):
        return legacy
    # Summarize the full public obligation for each failing output, not only
    # the three displayed counterexamples. Controls remain explicitly bound.
    obligations = {}
    control_names = list(contract['control_inputs'][0])
    for output in outputs:
        groups = {}
        for row in vectors:
            state = row['inputs'][contract['state_input']]
            key = str(state) + ':' + str(row['expected'][output])
            controls = {n:row['inputs'][n] for n in control_names}
            groups.setdefault(key, []).append(contract['control_inputs'].index(controls))
        obligations[output] = groups
    def examples(cells):
        return [{'inputs': vectors[i]['inputs'], 'output': n,
                 'expected': vectors[i]['expected'][n]} for i,n in sorted(cells)[:3]]
    # Existing dependency helper exposes assignment dependencies only. Include
    # this limitation instead of calling it exact faulty-line identification.
    compact_leads = {n: {'drivers':x['drivers'][:4], 'limitation':x['limitation']}
                     for n,x in leads.items()}
    counts = {n: sum(x == n for _,x in bad) for n in contract['checked_outputs']}
    data = dict(state_input=contract['state_input'], state_codes=contract['codes'],
                control_inputs=contract['control_inputs'],
                obligation_key='state-code:expected-output-value; list indexes control_inputs',
                full_obligations_for_failed_outputs=obligations,
                current_source_leads=compact_leads, current_failed_examples=examples(bad),
                fixed_examples=examples(fixed), new_failure_examples=examples(introduced),
                per_output_failed_checks=counts,
                fixed_count=len(fixed), new_failure_count=len(introduced),
                preserved_output_checks=[n for n,c in counts.items() if c == 0])
    text = ('PUBLIC_LOCAL_REPAIR: Edit the supplied current candidate. Repair the failed output obligations '
            'and their dependencies together; preserve the previously corrected public cells and other '
            'checked outputs. A source lead is a hypothesis, not permission to ignore branch controls. '
            'Return one complete RTL module, not a patch.\n' +
            json.dumps(data, separators=(',',':')))
    if len(text) > 4000:
        # Avoid a hidden tail truncation or incomplete public relation.
        return dict(action='legacy', reason='complete local plan exceeds existing consumer budget')
    return dict(action='edit_current', reason='first located failure' if prior is None else 'verified cell repairs with remaining failures',
                fixed_count=len(fixed), new_failure_count=len(introduced),
                feedback=text, source_localization='bounded assignment leads; controls not proven')
