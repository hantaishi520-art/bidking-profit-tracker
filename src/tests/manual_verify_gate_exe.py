# ASCII-only: verify the REAL packaged exe no longer deadlocks /api/launch.
# Safe: config is pointed at a NONEXISTENT whitelisted-name exe with empty search_dirs,
# so nothing can actually launch. We only assert the "disabled" gate is gone.
import json, socket, sys, urllib.request, urllib.error

NAME_CN = "".join(chr(x) for x in (31478, 25293, 20043, 29579, 20840, 33258,
                                   21160, 20272, 20215, 22120))  # whitelisted exe name
SAFE_PATH = "D:\\tmp\\bk_gate_test\\nope_dir\\" + NAME_CN + ".exe"

def req(path, method="GET", body=None, host="127.0.0.1"):
    url = "http://%s:8766%s" % (host, path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); LAN = s.getsockname()[0]
finally:
    s.close()
print("LAN=" + LAN, flush=True)

fails = []
c, j = req("/api/launch/config", "POST", {"exact_path": SAFE_PATH, "search_dirs": []})
print("config save: %s ok=%s err=%s" % (c, j.get("ok"), j.get("error", "")), flush=True)
if not j.get("ok"):
    print("FAIL config-save", flush=True)
    fails.append("config-save")

c, j = req("/api/launch", "POST", {})
err = j.get("error") or ""
if c == 200 and "已关闭" not in err:
    print("PASS local passes gate (state=%s error=%s)" % (j.get("state"), err[:40]), flush=True)
else:
    print("FAIL local: %s" % ((c, j),), flush=True)
    fails.append("local")

c, j = req("/api/launch", "POST", {}, host=LAN)
err = j.get("error") or ""
if c == 200 and "已关闭" not in err:
    print("PASS lan passes gate (state=%s error=%s)" % (j.get("state"), err[:40]), flush=True)
else:
    print("FAIL lan: %s" % ((c, j),), flush=True)
    fails.append("lan")

print("=" * 50, flush=True)
print("RESULT " + ("ALL PASS" if not fails else "FAIL: " + ",".join(fails)), flush=True)
sys.exit(1 if fails else 0)
