"""Select an existing checker before binding its roles; never invent an oracle.

This bounded recognizer records positive public obligations. A component name
alone is not an output relationship: FSM storage must not imply output<=input.
Unknown behavior stays unverified, without asking for an irrelevant clock.
"""
import re


def select_checker(prompt):
    flat = re.sub(r'\s+', ' ', prompt)
    patterns = {
        'state_behavior': r'\b(?:finite\s+state\s+machine|state\s+machine|FSM|Moore|Mealy|state\s+(?:transition|table|diagram)|next[- ]state)\b',
        'priority': r'\bpriority\s+encoder\b',
        'serial_start': r'\bstart\s+bit\b',
        'serial_stop': r'\bstop\s+bit\b',
        'rotation': r'\brotat(?:or|e|es|ing|ion)\b',
        'decimal': r'\bBCD\b|binary[- ]coded\s+decimal',
        'counter': r'\bcounter\b|\bcount(?:s|ing)?\s+(?:up|from|to)\b',
        # D describes a data-storage element; "state flip-flops" does not.
        'data_storage': r'\bD\s+flip[- ]flops?\b|\bDFFs?\b',
        'storage_bank_obligation': r'\b(?:include[sd]?|contain[sd]?|comprise[sd]?)\s+(?:a\s+bank\s+of\s+)?(?:\d+\s+)?(?:D\s+flip[- ]flops?|DFFs?)\b',
        'explicit_combinational': r'\bcombinational\b',
        'explicit_sequential': r'\bsequential\b|\b(?:rising|falling|positive|negative)\s+edge\b',
    }
    evidence = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, flat, re.I)
        if match:
            evidence[name] = match.group(0)
    # Transition notation can establish state dependence without the word FSM.
    if re.search(r'\b[A-Z][A-Za-z0-9_]*\s*(?:\([^)]*\))?\s*--[^>]{0,60}-->\s*[A-Z]', flat):
        evidence['state_behavior'] = 'explicit named-state transition relation'
    checker = None
    domain = 'unknown'
    if 'serial_start' in evidence and 'serial_stop' in evidence:
        checker, domain = 'serial', 'sequential_protocol'
    elif 'state_behavior' in evidence:
        # An explicit state relation outranks mentions of its implementation
        # components. Specific event contracts are handled before this selector.
        domain = 'stateful_relation'
    elif 'explicit_combinational' in evidence and any(k in evidence for k in ('data_storage', 'counter')):
        domain = 'mixed_or_ambiguous'
    elif 'priority' in evidence:
        checker, domain = 'priority', 'combinational'
    elif 'decimal' in evidence and 'counter' in evidence:
        checker, domain = 'bcd', 'sequential_counter'
    elif 'rotation' in evidence:
        checker, domain = 'rotate', 'sequential_rotation'
    elif 'data_storage' in evidence and 'storage_bank_obligation' in evidence:
        checker, domain = 'capture', 'sequential_storage'
    elif 'explicit_combinational' in evidence:
        domain = 'combinational'
    elif 'explicit_sequential' in evidence:
        domain = 'sequential_unspecified'
    return {
        'checker': checker,
        'domain': domain,
        'evidence': evidence,
        'requires_clock': checker in ('serial', 'bcd', 'rotate', 'capture'),
        'reason_code': 'CHECKER_SELECTED' if checker else 'PUBLIC_BEHAVIOR_MODEL_UNAVAILABLE',
    }
