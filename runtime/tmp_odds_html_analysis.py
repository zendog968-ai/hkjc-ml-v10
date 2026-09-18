from pathlib import Path
import re
from bs4 import BeautifulSoup
p = Path('/tmp/hkjc_wp_probe_20260906.html')
text = p.read_text(errors='ignore')
s = BeautifulSoup(text, 'html.parser')
print('title=', s.title.get_text(' ', strip=True) if s.title else '')
print('scripts:')
for x in s.find_all('script'):
    src = x.get('src')
    if src:
        print(src)
print('links/assets:')
for x in s.find_all(['link', 'a']):
    u = x.get('href') or x.get('src')
    if u and any(k in u.lower() for k in ('json', 'api', 'racing', 'odds', 'wp')):
        print(u)
print('body text=', s.get_text(' ', strip=True)[:1200])
print('endpoint-like tokens:')
for m in sorted(set(re.findall(r"https?://[^\"'\\s<>]+|/[A-Za-z0-9_./-]*(?:json|api|odds)[A-Za-z0-9_./?=&-]*", text, re.I))):
    print(m[:300])

auto = sorted(set(re.findall(r"(?:fetch|axios|ajax|url|endpoint|api|json|odds)[^\n]{0,160}", text, re.I)))
print('keyword lines:')
for line in auto[:80]:
    print(line[:300])
