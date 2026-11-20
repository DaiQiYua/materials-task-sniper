import json
import sys

path = r'F:\Users\47305\Desktop\127.0.0.1_2026_11_20_16_44_47.har'
d = json.load(open(path, encoding='utf-8'))
es = d['log']['entries']
for i, e in enumerate(es, 1):
    r = e['request']
    s = e['response']
    body = s.get('content', {}).get('text') or ''
    print(f"{i:>3} {e['startedDateTime']} {r['method']:4} "
          f"{r['url'].replace('http://127.0.0.1:8787', '')} -> {s['status']} "
          f"{e['time']:.0f}ms body={len(body)}c")