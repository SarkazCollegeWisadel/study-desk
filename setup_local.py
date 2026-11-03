"""Create ignored local configuration without overwriting existing settings."""
import argparse
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--root', type=Path, default=BASE / 'data')
args = parser.parse_args()
root = args.root.resolve()
root.mkdir(parents=True, exist_ok=True)
for name in ('config', 'providers'):
    target = BASE / 'kb' / (name + '.json')
    if target.exists():
        print('Keeping existing ' + target.name)
        continue
    config = json.loads((BASE / 'kb' / (name + '.example.json')).read_text(encoding='utf-8'))
    if name == 'config':
        config['root'] = str(root)
    target.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Created ' + target.name)
