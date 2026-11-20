import json

path = r'F:\Users\47305\Desktop\127.0.0.1_2026_11_20_16_44_47.har'
d = json.load(open(path, encoding='utf-8'))
es = d['log']['entries']


def sv(obj):
    """serialize short-ish"""
    return json.dumps(obj, ensure_ascii=False)


def headers(e):
    return {h['name']: h['value'] for h in e['request']['headers']}


print("=" * 30, "POST /api/start", "=" * 30)
for i, e in enumerate(es, 1):
    if e['request']['method'] == 'POST':
        print('REQ headers:', sv(headers(e)))
        print('REQ body  :', e['request'].get('postData', {}).get('text'))
        print('RES status:', e['response']['status'])
        print('RES headers:', sv({h['name']: h['value'] for h in e['response']['headers']}))
        print('RES body  :', e['response']['content'].get('text'))

print("=" * 30, "/api/events", "=" * 30)
for i, e in enumerate(es, 1):
    if '/api/events' in e['request']['url']:
        print('REQ headers:', sv(headers(e)))
        print('RES status:', e['response']['status'])
        print('RES headers:', sv({h['name']: h['value'] for h in e['response']['headers']}))
        print('RES content:', sv(e['response']['content']))

print("=" * 30, "status bodies (changed ones only)", "=" * 30)
prev = None
for i, e in enumerate(es, 1):
    if '/api/status' not in e['request']['url']:
        continue
    b = e['response']['content'].get('text') or ''
    if b != prev:
        print(f"--- entry {i} ({e['startedDateTime']}, {e['time']:.0f}ms, {len(b)}c) ---")
        print(b)
        prev = b

print("=" * 30, "timings entry34 / 36 / 37", "=" * 30)
for idx in (34, 36, 37):
    e = es[idx - 1]
    print(idx, e['request']['url'].replace('http://127.0.0.1:8787', ''), 'time=', e['time'],
          'timings=', sv(e.get('timings')))