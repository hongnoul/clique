#!/usr/bin/env python3
import json, time, urllib.request, concurrent.futures

URL = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = "nemotron-3-nano-fp8"

def one(max_tokens=256, i=0):
    prompt = f"Task {i}: Explain quantum computing in one paragraph. Then list three applications."
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "stream": False}).encode()
    t0 = time.time()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=600))
    return r["usage"]["completion_tokens"], time.time() - t0

one(8)
ct, dt = one(256)
print("single: tokens=%d t=%.2fs TPS=%.1f" % (ct, dt, ct/dt))
for n in (8, 16, 32):
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(n) as ex:
        rs = list(ex.map(lambda i: one(256, i), range(n)))
    wall = time.time() - t0
    tot = sum(c for c, _ in rs)
    print("concurrent_%d: total=%d wall=%.2fs aggTPS=%.1f" % (n, tot, wall, tot/wall))
