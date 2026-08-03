from pathlib import Path
import subprocess

root = Path(__file__).parent
files = [root / 'templates' / 'glassdoor.html', root / 'templates' / 'linkedin.html', root / 'templates' / 'apna.html']
errors = []
for f in files:
    text = f.read_text(encoding='utf-8')
    start = text.find('<script')
    end = text.find('</script>', start)
    if start == -1 or end == -1:
        errors.append(f.name + ': missing <script> block')
        continue
    script = text[text.find('>', start) + 1:end]
    tmp = root / (f.name + '.tmp.js')
    tmp.write_text(script, encoding='utf-8')
    result = subprocess.run(['node', '-c', str(tmp)], capture_output=True, text=True)
    tmp.unlink()
    if result.returncode != 0:
        errors.append(f.name + ': ' + result.stderr.strip())

if errors:
    print('\n'.join(errors))
else:
    print('OK')
