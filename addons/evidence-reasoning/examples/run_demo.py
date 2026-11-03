"""Run from the candidate root: python -m examples.run_demo."""
from pathlib import Path
import copy,json
from evidence_reasoning.answer_contract import validate_answer
fixture=json.loads(Path(__file__).with_name('synthetic_case.json').read_text())
good=validate_answer(fixture['answer'],fixture['packet'])
bad=copy.deepcopy(fixture['answer']);bad['claims'][0]['hit_ids']=['invented']
rejected=validate_answer(bad,fixture['packet'])
assert good['structural_valid'] and not rejected['structural_valid']
print(json.dumps({'synthetic_valid':good,'synthetic_fabricated_reference':rejected},indent=2))
