"""Drive the admin wizard end-to-end through the real HTTP API on the server (trial book).

Picks option 0 at each step; at the scale step prefers a lv0_1 option (complete book) when offered.
The admin token is read from the server env and never printed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
TOKEN = os.environ["XIAOSHUO_ADMIN_TOKEN"]
IDEA = sys.argv[1]


def call(path, body):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} on {path}: {e.read().decode('utf-8')[:600]}")
        raise SystemExit(1)
    print(f"  ({time.time() - t:.1f}s)")
    return data


def show(view):
    if view["status"] != "IN_PROGRESS":
        return
    print(f"[第{view['step_index'] + 1}步 {view['step']['title']}]")
    for i, o in enumerate(view["options"]):
        extra = {k: v for k, v in o.items() if k != "label"}
        print(f"  {i}. {o['label']} | {json.dumps(extra, ensure_ascii=False)[:160]}")


view = call("/api/wizard/sessions", {"idea": IDEA})
sid = view["session_id"]
show(view)
while view["status"] == "IN_PROGRESS":
    choice = 0
    if view["step"]["key"] == "scale":
        lv = [i for i, o in enumerate(view["options"]) if o["author_level"] == "lv0_1"]
        choice = lv[0] if lv else 0
    print(f"  -> 选 {choice}")
    view = call(f"/api/wizard/sessions/{sid}/answer", {"choice": choice})
    show(view)
print("READY:", view["status"])
view = call(f"/api/wizard/sessions/{sid}/confirm", {"pen_name": "试跑"})
print("CONFIRMED:", view["status"], "book_id=", view["book_id"])
