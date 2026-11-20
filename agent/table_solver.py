"""Public-only finite-table calculation. Never emits or patches a DUT."""
import json
import os
import sys


def binary_table(contract):
    ports = contract['ports']
    names = sorted(n for n, p in ports.items() if p['direction'] == 'input')
    return {
        'input_bits_left_to_right': [n if ports[n]['width'] == 1 else f'{n}[{b}]'
                                    for n in names for b in reversed(range(ports[n]['width']))],
        'rows': sorted([
            [''.join(format(row['inputs'][n], f"0{ports[n]['width']}b") for n in names),
             {n: [format(value, f"0{ports[n]['width']}b"), format(mask, f"0{ports[n]['width']}b")]
              for n, (value, mask) in row['expected'].items()}]
            for row in contract['table_rows']]),
        'output_cell_format': '[value,mask]; mask=0 is unconstrained',
    }


def progress_route(previous, current, same_hash=False):
    old = {json.dumps(x, sort_keys=True) for x in previous}
    new = {json.dumps(x, sort_keys=True) for x in current}
    if not new:
        return 'public_pass'
    if same_hash or old == new:
        return 'clean_reread_stagnant'
    if new < old:
        return 'continue_current_remaining_cells'
    return 'rebuild_from_full_public_table'


def solve_public_table(contract):
    table = binary_table(contract)
    # Projected mux outputs must retain the public mux-only implementation rule.
    # The table itself is the deterministic lookup result; do not suggest gates.
    if contract.get('mapping', {}).get('output_bit_rule') == 'numeric selector value':
        return {'status': 'verified_public_lookup', 'method': 'mux_lookup',
                'instruction': 'Use only the public allowed mux structure and constants. Each output bit has the complete lookup rows above; do not replace it with AND/OR gates.'}
    dependency_path = os.environ.get('RTL_TABLE_SOLVER_DEPENDENCIES')
    if dependency_path and dependency_path not in sys.path:
        sys.path.insert(0, dependency_path)
    from sympy import Symbol, SOPform, __version__
    if __version__ != '1.14.0':
        raise RuntimeError('table solver requires fixed SymPy 1.14.0')
    names = table['input_bits_left_to_right']
    if not 1 <= len(names) <= 6:
        raise ValueError('public input bound exceeded')
    symbols = [Symbol('v' + str(i)) for i in range(len(names))]
    by_input = {int(row[0], 2): row[1] for row in table['rows']}
    if len(by_input) != len(table['rows']):
        raise ValueError('duplicate public input row')
    expressions = {}
    verified = 0
    for output in sorted(contract['checked_outputs']):
        for bit in range(contract['ports'][output]['width']):
            ones, free = [], []
            for index in range(1 << len(names)):
                value, mask = by_input.get(index, {}).get(output, ['0', '0'])
                if not (int(mask, 2) >> bit & 1):
                    free.append(index)
                elif int(value, 2) >> bit & 1:
                    ones.append(index)
            expression = SOPform(symbols, ones, free)
            # Independent evaluation of every constrained public cell, including
            # zeros. A minimizer result alone is not accepted as validation.
            for key, outputs in by_input.items():
                value, mask = outputs.get(output, ['0', '0'])
                if int(mask, 2) >> bit & 1:
                    actual = bool(expression.subs({s: bool(key >> (len(names)-1-i) & 1)
                                                   for i, s in enumerate(symbols)}))
                    if actual != bool(int(value, 2) >> bit & 1):
                        raise ValueError('computed expression violates public cell')
                    verified += 1
            label = output if contract['ports'][output]['width'] == 1 else f'{output}[{bit}]'
            expressions[label] = str(expression)
    return {'status': 'verified_public_expression', 'method': 'SymPy 1.14.0 SOPform',
            'variables': {str(s): n for s, n in zip(symbols, names)},
            'expressions': expressions, 'verified_constrained_cells': verified,
            'notation': '~ NOT; & AND; | OR; True=1; False=0. This is an expression, not RTL. Preserve public structural restrictions.'}


def table_feedback(result, contract):
    table = binary_table(contract)
    try:
        calculation = solve_public_table(contract)
    except (ImportError, RuntimeError, ValueError) as error:
        calculation = {'status': 'unavailable', 'reason': type(error).__name__ + ': ' + str(error)[:160]}
    return ('PUBLIC_TABLE_REPAIR: Implement the complete logic from this PUBLIC canonical table. '
            'Input bit order is explicit. Preserve interface, all constrained cells, public implementation restrictions and other correct behavior. '
            'Leave mask0 cells free. Return complete model-written RTL, using the verified expression or direct table/mux structure.\n' +
            json.dumps({'public_table': table, 'calculation': calculation,
                        'remaining_public_error_cells': result.get('bad_public_cells', []),
                        'unchecked_outputs': contract['unchecked_outputs'],
                        'public_mapping': contract['mapping'],
                        'counterexamples': [{k: e[k] for k in ('inputs', 'output', 'expected', 'mask', 'actual')}
                                            for e in result.get('counterexamples', [])[:3]]}, separators=(',', ':')))
