"""Apply exact, model-authored edits atomically; never infer replacement logic."""
import hashlib
import json
import re

class EditError(ValueError):
    pass

def digest(code):
    return hashlib.sha256(code.encode('utf-8')).hexdigest()

def tokens(code):
    # Preserve strings, escaped identifiers and compound operators; ignore only
    # ordinary whitespace/comments. This is a no-op filter, not equivalence proof.
    parts=re.findall(r'"(?:\\.|[^"\\])*"|\\[^\s]+|//[^\r\n]*|/\*.*?\*/|\s+|(?:===|!==|<<<|>>>|<<=|>>=|\*\*|&&|\|\||==|!=|<=|>=|<<|>>|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|~\^|\^~|~&|~\||->|=>|\+:|-:)|[A-Za-z_$][\w$]*|\d+|\S',code,re.S)
    return tuple(p for p in parts if not p.isspace() and not p.startswith(('//','/*')))

def apply_edits(base, response, expected_sha):
    if digest(base)!=expected_sha:
        raise EditError('base_identity_mismatch')
    # Only unwrap a complete single JSON envelope. Never extract JSON from
    # surrounding prose or silently repair malformed/ambiguous content.
    wrapper = 'none'
    if isinstance(response, str):
        wrapped = re.fullmatch(r'```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*', response.strip(), re.I)
        if wrapped:
            response = wrapped[1]
            wrapper = 'json_fence'
    def unique_object(pairs):
        d={}
        for k,v in pairs:
            if k in d:raise EditError('duplicate_json_key')
            d[k]=v
        return d
    try:
        obj=json.loads(response,object_pairs_hook=unique_object)
    except (ValueError,TypeError) as error:
        raise EditError('invalid_edit_json') from error
    if not isinstance(obj,dict) or set(obj)!={'edits'} or not isinstance(obj['edits'],list) or not obj['edits']:
        raise EditError('invalid_edit_schema')
    spans=[]
    for e in obj['edits']:
        if (not isinstance(e,dict) or set(e)!={'search','replace'}
                or not isinstance(e['search'],str) or not isinstance(e['replace'],str) or not e['search']):
            raise EditError('invalid_edit_block')
        # Detect even overlapping duplicate occurrences, not just str.count().
        found=[m.start() for m in re.finditer('(?='+re.escape(e['search'])+')',base)]
        if len(found)!=1:raise EditError('search_not_unique')
        start=found[0];spans.append((start,start+len(e['search']),e['replace']))
    spans.sort()
    if any(a[1]>b[0] for a,b in zip(spans,spans[1:])):raise EditError('overlapping_edits')
    # Resolve every search against the SAME snapshot before applying anything.
    chunks=[];cursor=0;records=[]
    for start,end,replacement in spans:
        chunks.extend((base[cursor:start],replacement));cursor=end
        records.append({'start':start,'end':end,'replacement_sha256':digest(replacement)})
    chunks.append(base[cursor:]);candidate=''.join(chunks)
    if tokens(candidate)==tokens(base):raise EditError('no_token_change')
    return candidate,dict(base_sha256=expected_sha,candidate_sha256=digest(candidate),edits=records,response_wrapper=wrapper,
                         provenance='unchanged spans from base model RTL; replacements verbatim from model response')
