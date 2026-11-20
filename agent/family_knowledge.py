"""Public-rule knowledge loaded only for checked repair, never task answers."""
import json
from pathlib import Path

def family_knowledge(contract):
    library=json.loads((Path(__file__).resolve().parents[1]/'skill/knowledge/families.json').read_text(encoding='utf-8'))
    card=library.get(contract.get('kind'))
    if not card:
        return ''
    # Keep the card short enough for the existing 6000-character repair consumer.
    return 'Family invariants from public semantics (apply only when specified): '+ '; '.join(card['repair_invariants'])
