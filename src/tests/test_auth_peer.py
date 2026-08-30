# ASCII-only test harness for peer-class auth (lan trust / ngrok strict).
# Runs a source-mode server on 127.0.0.1:8799 with a temp ROOT, then drives it:
#  - loopback client  -> peer local
#  - via LAN IP       -> peer lan (TCP source = LAN IP)
#  - loopback + X-Forwarded-For (fake alive ngrok) -> peer public/lan by XFF
import json, os, sys, threading, tempfile, shutil, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))   # src/tests/ -> src/
import serve  # noqa

TMP = tempfile.mkdtemp(prefix="bk_test_")
serve.init(root=TMP)

class FakeProc:
    def poll(self):
        return None
    def terminate(self):
        pass
serve.NGROK_PROC = FakeProc()   # pretend ngrok tunnel is alive -> XFF is honored

srv = serve.make_server(8799, TMP)
threading.Thread(target=srv.serve_forever, daemon=True).start()

LAN_IP = serve._detect_lan_ip() or "127.0.0.1"
results = []

def req(path, method="GET", body=None, headers=None, host=None, xff=None):
    url = ("http://%s:8799%s" % (host or "127.0.0.1", path))
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if xff:
        r.add_header("X-Forwarded-For", xff)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ("  | " + str(detail)[:200] if not cond else ""), flush=True)

# --- T1 loopback: server-info has lan_url, peer=local, no pairing needed
c, j = req("/api/server-info")
check("T1 local server-info", c == 200 and j.get("peer") == "local"
      and bool(j.get("lan_url")) and j.get("pairing_needed") is False, (c, j))

# --- T2 loopback: data GET without token -> 200
c, j = req("/api/career-db-status")
check("T2 local GET no token", c == 200, (c, j))

# --- T3 via LAN IP: peer=lan, lan_url present
c, j = req("/api/server-info", host=LAN_IP)
check("T3 lan server-info", c == 200 and j.get("peer") == "lan"
      and bool(j.get("lan_url")) and j.get("pairing_needed") is False, (c, j))

# --- T4 via LAN IP, no password yet: GET without token -> 200 (lan trusted)
c, j = req("/api/career-db-status", host=LAN_IP)
check("T4 lan GET no pwd no token", c == 200, (c, j))

# --- T5 via LAN IP: password login with empty pwd (no password set) -> local-ui
c, j = req("/api/auth", "POST", {"pwd": ""}, host=LAN_IP)
check("T5 lan auth empty pwd -> local-ui", c == 200 and j.get("scope") == "local-ui", (c, j))

# --- T6 loopback: set access password
c, j = req("/api/pass-set", "POST", {"pwd": "test123"})
check("T6 local pass-set", c == 200 and j.get("ok"), (c, j))

# --- T7 via LAN IP with password set: GET without token -> 401
c, j = req("/api/career-db-status", host=LAN_IP)
check("T7 lan GET no token with pwd -> 401", c == 401, (c, j))

# --- T8 via LAN IP: wrong password -> 401 with clear error
c, j = req("/api/auth", "POST", {"pwd": "wrong"}, host=LAN_IP)
check("T8 lan auth wrong pwd", c == 401 and "error" in j, (c, j))

# --- T9 via LAN IP: correct password -> local-ui + token works
c, j = req("/api/auth", "POST", {"pwd": "test123"}, host=LAN_IP)
tok = j.get("token", "")
check("T9a lan auth ok -> local-ui", c == 200 and j.get("scope") == "local-ui" and tok, (c, j))
c, j = req("/api/career-db-status", headers={"X-Auth-Token": tok}, host=LAN_IP)
check("T9b lan GET with token", c == 200, (c, j))

# --- T10 via LAN IP: pair request still works (not required, but allowed)
c, j = req("/api/pair/request", "POST",
           {"device_id": "dev-lan-test-1", "name": "TestPhone", "model": "X", "pwd": "test123"},
           host=LAN_IP)
check("T10 lan pair request -> rid", c == 200 and bool(j.get("rid")), (c, j))

# --- T11 loopback: pending list shows the request (exe received it)
c, j = req("/api/pair/pending")
check("T11 local sees pending", c == 200 and len(j.get("pending") or []) >= 1, (c, j))

# --- T12 fake-ngrok public visitor (XFF public IP): strict mode
c, j = req("/api/server-info", xff="8.8.8.8")
check("T12a public server-info no lan_url",
      c == 200 and j.get("peer") == "public" and not j.get("lan_url")
      and j.get("pairing_needed") is True, (c, j))
c, j = req("/api/career-db-status", xff="8.8.8.8")
check("T12b public GET no token -> 401", c == 401, (c, j))

# --- T13 fake-ngrok visitor with private XFF -> lan
c, j = req("/api/server-info", xff="192.168.50.5")
check("T13 xff private -> lan", c == 200 and j.get("peer") == "lan", (c, j))

# --- T14 XFF chain: rightmost wins (spoofable left ignored)
c, j = req("/api/server-info", xff="1.2.3.4, 192.168.50.5")
check("T14a xff rightmost lan", j.get("peer") == "lan", (c, j))
c, j = req("/api/server-info", xff="192.168.50.5, 8.8.8.8")
check("T14b xff rightmost public", j.get("peer") == "public", (c, j))

# --- T15 public peer cannot read pass-info (401 by GET gate before token, 403 by local-only check inside)
c, j = req("/api/pass-info", xff="8.8.8.8")
check("T15 public pass-info -> 401/403", c in (401, 403), (c, j))

# --- T16 public password login, pairing_required ON (default) -> 403 pairing_required
#     (2026-08-29 晚起：公网只凭密码不再直接给令牌，必须电脑端同意配对)
c, j = req("/api/auth", "POST", {"pwd": "test123"}, xff="8.8.8.8")
check("T16 public auth default -> 403 pairing_required",
      c == 403 and "尚未配对" in (j.get("error") or ""), (c, j))

# --- T17 switch matrix for public password login
req("/api/pair/switches", "POST", {"switches": {"pairing_required": False}})
c, j = req("/api/auth", "POST", {"pwd": "test123"}, xff="8.8.8.8")
check("T17a pairing off + legacy on -> legacy-readonly",
      c == 200 and j.get("scope") == "legacy-readonly", (c, j))
req("/api/pair/switches", "POST", {"switches": {"allow_legacy_readonly": False}})
c, j = req("/api/auth", "POST", {"pwd": "test123"}, xff="8.8.8.8")
check("T17b pairing off + legacy off -> 403", c == 403, (c, j))
req("/api/pair/switches", "POST", {"switches": {"pairing_required": True,
                                                "allow_legacy_readonly": True}})

# --- T18 device pairing still works end-to-end (public visitor with approval)
c, j = req("/api/pair/request", "POST",
           {"device_id": "dev-pub-test-1", "name": "PubPhone", "model": "Y", "pwd": "test123"},
           xff="8.8.8.8")
rid = j.get("rid", "")
check("T18a public pair request", c == 200 and bool(rid), (c, j))
c, j = req("/api/pair/approve", "POST", {"rid": rid})
did = j.get("device_id", "")
check("T18b local approve", c == 200 and bool(did), (c, j))
c, j = req("/api/pair/status?rid=%s&did=dev-pub-test-1" % rid, xff="8.8.8.8")
dkey = j.get("device_key", "")
check("T18c poll returns device_key once", c == 200 and bool(dkey), (c, j))
c, j = req("/api/pair/status?rid=%s&did=dev-pub-test-1" % rid, xff="8.8.8.8")
check("T18d key consumed", j.get("state") != "approved", (c, j))

# --- T19 already-paired device re-pairs by password (no second consent, no deadlock)
#     2026-08-30：同一设备只需同意一次——App 重新添加/换地址后凭密码直接重新绑定
c, j = req("/api/pair/request", "POST",
           {"device_id": "dev-pub-test-1", "name": "PubPhone", "model": "Y", "pwd": "test123"},
           xff="8.8.8.8")
check("T19a re-pair paired did -> auto_paired + key + token",
      c == 200 and j.get("ok") and j.get("auto_paired") and j.get("device_key") == dkey
      and bool(j.get("token")) and not j.get("rid"), (c, j))
c, j = req("/api/pair/request", "POST",
           {"device_id": "dev-pub-test-1", "name": "PubPhone", "model": "Y", "pwd": "WRONG"},
           xff="8.8.8.8")
check("T19b re-pair wrong pwd -> 400 密码错误", c == 400 and j.get("error") == "密码错误", (c, j))
# auto_paired 签发的 token 是 device scope：能读数据、不能动敏感接口
t19tok = (req("/api/pair/request", "POST",
              {"device_id": "dev-pub-test-1", "name": "PubPhone", "model": "Y", "pwd": "test123"},
              xff="8.8.8.8")[1] or {}).get("token", "")
c, j = req("/api/career-db-status", headers={"X-Auth-Token": t19tok}, xff="8.8.8.8")
check("T19c auto-pair token reads data", c == 200, (c, j))
c, j = req("/api/pass-info", headers={"X-Auth-Token": t19tok}, xff="8.8.8.8")
check("T19d auto-pair token cannot touch sensitive", c == 403, (c, j))

# --- T20 public password brute-force lockout (2026-08-30): 5 fails -> 15min lock
#     注意：本用例会把公网密码锁 15 分钟（进程内存态），必须放在所有公网密码用例之后；
#     run_tests 按文件独立进程运行，不会泄漏到其他文件。
codes = [req("/api/auth", "POST", {"pwd": "wrong"}, xff="8.8.8.8")[0] for _ in range(5)]
check("T20a public wrong pwd x5 -> all 401", codes == [401] * 5, codes)
c, j = req("/api/auth", "POST", {"pwd": "test123"}, xff="8.8.8.8")
check("T20b 6th attempt (correct pwd) -> 429 locked", c == 429, (c, j))
c, j = req("/api/pair/request", "POST",
           {"device_id": "dev-pub-test-1", "name": "P", "model": "Y", "pwd": "test123"},
           xff="8.8.8.8")
check("T20c pair request also locked", c == 429, (c, j))
c, j = req("/api/auth", "POST", {"pwd": "test123"}, host=LAN_IP)
check("T20d lan auth unaffected by public lock", c == 200 and j.get("scope") == "local-ui", (c, j))

fails = [r for r in results if not r[1]]
print("=" * 50)
print("TOTAL %d  PASS %d  FAIL %d" % (len(results), len(results) - len(fails), len(fails)), flush=True)
with open(os.path.join(TMP, "test_summary.json"), "w") as f:
    json.dump({"pass": len(results) - len(fails), "total": len(results),
               "fails": [r[0] for r in fails]}, f)
srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
