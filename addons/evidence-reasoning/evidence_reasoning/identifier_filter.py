"""Preserve long explicit identifiers when judging candidate relevance."""
import re

def identifier_evidence_matches(query, body):
    # Preserve code/gene/document identifiers as evidence requirements. Segmenting
    # an absent identifier must not turn a generic fragment such as "KB" into proof.
    candidates = re.findall(r'(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9_-]{7,}(?![A-Za-z0-9_-])', query)
    identifiers = [x for x in candidates if '_' in x or re.search(r'\d', x)]
    return not identifiers or any(re.search(r'(?<![A-Za-z0-9_-])' + re.escape(x) + r'(?![A-Za-z0-9_-])', body, re.I) for x in identifiers)
