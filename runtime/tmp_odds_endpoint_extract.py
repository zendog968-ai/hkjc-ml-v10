from pathlib import Path
p = Path('/tmp/_static_js_main.f7d1d890.js')
text = p.read_text(errors='ignore')
needles = ['opGetSingleQTTOdds', 'consvc.hkjc.com', '/api/graph', 'getJSON.aspx', 'odds']
for needle in needles:
    print(f'=== {needle} ===')
    start = 0
    found = 0
    while True:
        i = text.find(needle, start)
        if i < 0 or found >= 8:
            break
        print(text[max(0, i-350):min(len(text), i+650)].replace('\n', ' ')[:1100])
        start = i + len(needle)
        found += 1
