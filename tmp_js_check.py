import re
from pathlib import Path
import subprocess
import sys

paths = [
    Path('templates/apna.html'),
    Path('templates/hirist.html'),
    Path('templates/shine.html'),
]
errors = []
for path in paths:
    text = path.read_text(encoding='utf-8')
    scripts = re.findall(r'<script[^>]*>([\s\S]*?)</script>', text, re.I)
    for i, script in enumerate(scripts, 1):
        tmp = Path(f'tmp_check_{path.stem}_{i}.js')
        tmp.write_text(script, encoding='utf-8')
        try:
            subprocess.run(['node', '--check', str(tmp)], check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            errors.append((path, i, e.stdout + e.stderr))
        finally:
            tmp.unlink()

if errors:
    for path, i, msg in errors:
        print('ERROR', path, 'script', i)
        print(msg)
    sys.exit(1)

print('ALL CLEAN')
