Repair current RTL against the public task and diagnostics. Return only JSON:
{"edits":[{"search":"exact old code","replace":"new code"}]}.
Each search must occur once; use the same base snapshot, with no overlapping edits.
Include all related changes. Write replacements yourself; caller applies them verbatim.
Preserve correct declarations, interface and unaffected logic. Check literal radix,
width, sign and encoding; packed BCD differs from the same decimal integer.
No full module or formatting-only edits. Diagnostics are evidence, not format rules.
