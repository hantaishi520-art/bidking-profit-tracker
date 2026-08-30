# ASCII-only: public-access gate tests (enforced pairing + browser_token + sensitive static).
import json, os, sys, threading, tempfile, shutil, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))   # src/tests/ -> src/
import serve  # noqa

TMP = tempfile.mkdtemp(prefix="bk_gate2_")
serve.init(root=TMP)

class FakeProc:
    def poll(self): return None
serve.NGROK_PROC = FakeProc()   # tunnel "running" so XFF -> public works

# seed a result.json so the static file exists
with open(os.path.join(TMP, "result.json"), "w", encoding="utf-8") as f:
    json.dump({"games": [], "meta": {"uid": "t"}}, f)

srv = serve.make_server(8799, TMP)
threading.Thread(target=srv.serve_forever, daemon=True).start()

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("  | " + str(detail)[:220] if not cond else ""), flush=True)

def req(path, method="GET", body=None, token=None, host=None, xff=None):
    url = "http://%s:8799%s" % (host or "127.0.0.1", path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token: r.add_header("X-Auth-Token", token)
    if xff: r.add_header("X-Forwarded-For", xff)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode("utf-8"))
        except Exception: return e.code, {}

# T1 public password login with pairing_required ON -> 403 + 尚未配对 (app-recognizable)
c, j = req("/api/auth", "POST", {"pwd": ""}, xff="8.8.8.8")
check("T1 public auth -> 403 pairing", c == 403 and "尚未配对" in (j.get("error") or ""), (c, j))

# T2 public sensitive static without token -> 401
c, j = req("/result.json", xff="8.8.8.8")
check("T2 public result.json -> 401", c == 401, (c, j))

# T3 lan sensitive static without anything -> 200 (lan trusted, unchanged)
c, j = req("/result.json", host=serve._detect_lan_ip() or "127.0.0.1")
check("T3 lan result.json -> 200", c == 200, (c, j))

# T4 full browser pairing flow: request -> approve -> status returns device_key + browser_token
c, j = req("/api/pair/request", "POST",
           {"device_id": "web-test-1", "name": "网页浏览器", "model": "UA", "pwd": ""},
           xff="8.8.8.8")
rid = j.get("rid", "")
check("T4a pair request ok", c == 200 and bool(rid), (c, j))
c, j = req("/api/pair/approve", "POST", {"rid": rid})
check("T4b approve", c == 200 and j.get("ok"), (c, j))
c, j = req("/api/pair/status?rid=%s&did=web-test-1" % rid, xff="8.8.8.8")
btok = j.get("browser_token", "")
check("T4c approved returns key+token", c == 200 and j.get("state") == "approved"
      and j.get("device_key") and btok, (c, j))
c, j = req("/api/pair/status?rid=%s&did=web-test-1" % rid, xff="8.8.8.8")
check("T4d one-time", j.get("state") != "approved", (c, j))

# T5 browser_token works on sensitive static + data API for public
c, j = req("/result.json", token=btok, xff="8.8.8.8")
check("T5a token reads result.json", c == 200, (c, j))
c, j = req("/api/career-db-status", token=btok, xff="8.8.8.8")
check("T5b token reads data api", c == 200, (c, j))
# device scope must NOT touch sensitive admin
c, j = req("/api/pass-set", "POST", {"pwd": "hax"}, token=btok, xff="8.8.8.8")
check("T5c device token cannot pass-set", c == 403, (c, j))

# T6 pairing_required OFF + legacy ON -> public password login gets legacy-readonly
req("/api/pair/switches", "POST", {"switches": {"pairing_required": False}})
c, j = req("/api/auth", "POST", {"pwd": ""}, xff="8.8.8.8")
check("T6 legacy path when pairing off", c == 200 and j.get("scope") == "legacy-readonly", (c, j))
req("/api/pair/switches", "POST", {"switches": {"pairing_required": True}})

# T7 local peers unaffected: result.json open on loopback
c, j = req("/result.json")
check("T7 local result.json -> 200", c == 200, (c, j))

# ---- T8 static ETag / If-None-Match -> 304 (2026-08-30 conditional requests)
def raw_req(path, headers=None):
    r = urllib.request.Request("http://127.0.0.1:8799" + path)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()

c1, h1, _ = raw_req("/result.json")
etag = (h1.get("ETag") or "").strip()
check("T8a 200 carries ETag", c1 == 200 and bool(etag), (c1, h1.get("ETag")))
c2, h2, body2 = raw_req("/result.json", headers={"If-None-Match": etag})
check("T8b If-None-Match -> 304 empty", c2 == 304 and len(body2) == 0, (c2, len(body2)))
c3, _, _ = raw_req("/result.json", headers={"If-None-Match": '"stale-etag"'})
check("T8c stale etag -> 200", c3 == 200, (c3,))

# ---- T9 career-data incremental: since_ts filter + uid injected per game
import sqlite3  # noqa
conn = serve._career_db_connect()
conn.execute("INSERT OR REPLACE INTO career_games VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             ("fp-old", "u1", 1000, 1, "m", 1, 10, 100, "t", json.dumps({"ts": 1000, "my_profit": 10}), ""))
conn.execute("INSERT OR REPLACE INTO career_games VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             ("fp-new", "u2", 2000, 2, "m", 0, -5, 50, "t", json.dumps({"ts": 2000, "my_profit": -5}), ""))
conn.commit(); conn.close()
c, j = req("/api/career-data")
g_all = j.get("games") or []
check("T9a all games carry uid", c == 200 and len(g_all) == 2
      and all("uid" in g for g in g_all), (c, [g.get("uid") for g in g_all]))
c, j = req("/api/career-data?since_ts=1001")
g_inc = j.get("games") or []
check("T9b since_ts returns only newer", c == 200 and len(g_inc) == 1 and g_inc[0].get("ts") == 2000, (c, g_inc))
c, j = req("/api/career-data?uid=u1")
g_u1 = j.get("games") or []
check("T9c uid filter still works", c == 200 and len(g_u1) == 1 and g_u1[0].get("uid") == "u1", (c, g_u1))

fails = [r for r in results if not r[1]]
print("=" * 50)
print("TOTAL %d  PASS %d  FAIL %d" % (len(results), len(results) - len(fails), len(fails)), flush=True)
srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
