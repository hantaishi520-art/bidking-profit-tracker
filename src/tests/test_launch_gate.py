# ASCII-only: verify the launch gate after removing the launch_enabled deadlock.
# Safe: launcher config points to a NONEXISTENT exe path with empty search_dirs,
# so nothing can actually be launched. We only assert which gate fires.
import json, os, sys, threading, tempfile, shutil, urllib.request, urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(BASE))   # src/tests/ -> src/
import serve  # noqa

TMP = tempfile.mkdtemp(prefix="bk_launch_test_")
serve.init(root=TMP)

class FakeProc:
    def poll(self):
        return None
serve.NGROK_PROC = FakeProc()   # pretend tunnel alive so XFF -> public works

cfg = serve._launcher_default_config()
cfg["exact_path"] = os.path.join(TMP, "no_such_dir", "estimator.exe")
cfg["search_dirs"] = []
cfg["launch_enabled"] = False   # simulate the OLD deadlocked config file
serve._launcher_save_config(cfg)

srv = serve.make_server(8799, TMP)
threading.Thread(target=srv.serve_forever, daemon=True).start()

def req(path, method="GET", body=None, token=None, host=None, xff=None):
    url = "http://%s:8799%s" % (host or "127.0.0.1", path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-Auth-Token", token)
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

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("  | " + str(detail)[:250] if not cond else ""), flush=True)

s = socket_lan = __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); LAN = s.getsockname()[0]
finally:
    s.close()

# T1 loopback: must get PAST the launch_enabled gate -> resolve fails with not-found
c, j = req("/api/launch", "POST", {})
check("T1 local passes gate", c == 200 and j.get("state") == "error"
      and "已关闭" not in (j.get("error") or ""), (c, j))

# T2 LAN peer: same
c, j = req("/api/launch", "POST", {}, host=LAN)
check("T2 lan passes gate", c == 200 and j.get("state") == "error"
      and "已关闭" not in (j.get("error") or ""), (c, j))

# T3 public peer (XFF), device-scoped token, switch off -> 403 scope gate still applies
c, j = req("/api/auth", "POST", {"pwd": ""}, xff="8.8.8.8")
dev_tok = serve._token_issue(scope="device", device_id="dev-x", ip="8.8.8.8")
c, j = req("/api/launch", "POST", {}, token=dev_tok, xff="8.8.8.8")
check("T3 public launch switch-off -> 403", c == 403, (c, j))

# T4 public peer with switch ON -> passes scope, then not-found (no deadlock)
req("/api/pair/switches", "POST", {"switches": {"allow_remote_launch": True}})
c, j = req("/api/launch", "POST", {}, token=dev_tok, xff="8.8.8.8")
check("T4 public launch switch-on -> proceeds", c == 200 and j.get("state") == "error"
      and "已关闭" not in (j.get("error") or ""), (c, j))
req("/api/pair/switches", "POST", {"switches": {"allow_remote_launch": False}})

fails = [r for r in results if not r[1]]
print("=" * 50)
print("TOTAL %d  PASS %d  FAIL %d" % (len(results), len(results) - len(fails), len(fails)), flush=True)
srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if fails else 0)
