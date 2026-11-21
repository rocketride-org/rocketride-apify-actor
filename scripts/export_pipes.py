"""Write example .pipe files for the ready-made pipelines into pipelines/.

    python scripts/export_pipes.py          # regenerate
    python scripts/export_pipes.py --check  # exit 1 if out of date

Open them in the RocketRide VS Code extension, or send them to the Actor's /run endpoint.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
import pipelines  # noqa: E402

ENV = {'ROCKETRIDE_OPENAI_KEY': 'set'}
EXAMPLES = {
    'doc-to-text': {'ocr': True},
    'redact-pii': {'ocr': True},
    'summarize': {},
    'extract-data': {'fields': ['invoice_number', {'name': 'total', 'type': 'decimal'}, {'name': 'due_date', 'type': 'date'}]},
    'ask': {'question': 'What is the total amount due, and when?'},
    'agent': {},
}


def export(check: bool = False) -> list[str]:
    stale = []
    for name, params in EXAMPLES.items():
        text = json.dumps(pipelines.build(name, params, ENV), indent=2) + '\n'
        path = ROOT / 'pipelines' / f'{name}.pipe'
        if not path.exists() or path.read_text() != text:
            stale.append(name)
            if not check:
                path.write_text(text)
    return stale


if __name__ == '__main__':
    changed = export(check='--check' in sys.argv)
    print('out of date:' if '--check' in sys.argv else 'written:', ', '.join(changed) or 'nothing')
    sys.exit(1 if changed and '--check' in sys.argv else 0)
