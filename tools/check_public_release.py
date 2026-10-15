"""Check public file scope and common accidental personal-data disclosures."""
from pathlib import Path
import argparse
import json
import re
import subprocess
import tomllib

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--anonymous-history', action='store_true',
                    help='Also require anonymous author/committer identities in all local Git history')
args = parser.parse_args()
expected = set(json.loads((root/'PUBLIC_FILES.json').read_text(encoding='utf-8'))['files'])
tracked = set(subprocess.check_output(['git','-C',str(root),'ls-files'],text=True).splitlines())
if tracked != expected:
    raise SystemExit('Public file allowlist mismatch: '+', '.join(sorted(tracked ^ expected)))

# Patterns, not a particular developer's name or machine path, belong in public code.
personal_path = re.compile(r'(?:[A-Za-z]:[\\/]+(?:Users|Documents and Settings)[\\/]+|/(?:Users|home)/)[^\s\x22\x27]+', re.I)
email = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')
for relative in sorted(tracked):
    path = root/relative
    if not path.is_file() or path.is_symlink():
        raise SystemExit('Missing or linked public file: '+relative)
    text = path.read_text(encoding='utf-8')
    if personal_path.search(text):
        raise SystemExit('Personal machine path found in '+relative)
    # Security tests can contain URL user-info; that is not an email address.
    without_urls = re.sub(r'\b(?:https?|wss?)://[^\s\x22\x27<>]+', '', text)
    for match in email.finditer(without_urls):
        domain = match.group(1).lower()
        if domain not in {'example.com','example.org','example.net'} and not domain.endswith(('.invalid','.example')):
            raise SystemExit('Review possible personal email in '+relative)

template = tomllib.loads((root/'alpaca-credentials.example.toml').read_text(encoding='utf-8'))
if any(template.get('alpaca',{}).get(key) for key in ('api_key','secret_key')):
    raise SystemExit('The public credential template must contain empty values')
for private in ('alpaca-credentials.toml','.env','data/quotes.json','runs/report.json',
                'training/fit.py','src/orbquant/private_idea.py','notebooks/idea.ipynb','best-model.pt',
                '.venv/pyvenv.cfg','.pytest_cache/README.md','tests/__pycache__/test.pyc'):
    if subprocess.run(['git','-C',str(root),'check-ignore','--no-index','-q',private]).returncode:
        raise SystemExit('Private path is not ignored: '+private)
if args.anonymous_history:
    identities = subprocess.check_output(['git','-C',str(root),'log','--all',
        '--format=%an%n%ae%n%cn%n%ce'],text=True).splitlines()
    expected_identity = ['OrbQuant Contributors','maintainers@example.invalid']*2
    if not identities or any(value != expected_identity[i % 4] for i,value in enumerate(identities)):
        raise SystemExit('Nonanonymous author/committer metadata found; review history before publishing')
print('Public allowlist, empty credential template and personal-path/email checks passed.')
