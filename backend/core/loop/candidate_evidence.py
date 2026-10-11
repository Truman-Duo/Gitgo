"""Source-bound delivery references, without model-generated excerpt copies."""
from hashlib import sha256


def candidate_segments(candidate):
    """Preserve the entire candidate with bounded source segments.

    References are local to this candidate, not authority or success receipts.
    Completion still binds the full input and candidate digests independently.
    """
    prefix = sha256(candidate.encode('utf-8')).hexdigest()[:16]
    # Fixed-sized source chunks also bound framing overhead for many tiny lines.
    # Their boundaries convey no semantic classification or completion verdict.
    return [{'id': f'{prefix}:{start // 1000 + 1}', 'text': candidate[start:start + 1000]}
            for start in range(0, len(candidate), 1000)]


def resolve_candidate_references(candidate, references, artifacts=()):
    if not isinstance(references, list) or len(references) > 256:
        raise ValueError('delivery_check evidence_refs must be a bounded list')
    source = {part['id']: part['text'] for part in candidate_segments(candidate)}
    source.update({part['id']: part['text'] for artifact in artifacts for part in artifact['segments']})
    quotes = []
    for reference in references:
        if not isinstance(reference, str) or reference not in source or not source[reference].strip():
            raise ValueError('delivery_check evidence_ref must identify actual nonblank candidate content')
        quotes.append(source[reference])
    return quotes
