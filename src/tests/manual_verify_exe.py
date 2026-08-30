# ASCII-only acceptance checks against the REAL packaged exe (port 8766, temp ROOT).
import json, sys, urllib.request, urllib.error, socket

def req(path, method="GET", body=None, headers=None, host=None, xff=None):
    url = "http://%s:8766%s" % (host or "127.0.0.1", path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if xff:
        r.add_header("X-Forwarded-For", xff)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("  | " + str(detail)[:300] if not cond else ""), flush=True)

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); LAN_IP = s.getsockname()[0]
finally:
    s.close()
print("LAN_IP=" + LAN_IP, flush=True)

# 1) loopback server-info: lan_url restored + peer local
c, b = req("/api/server-info")
j = json.loads(b)
check("E1 local server-info lan_url/peer", c == 200 and j.get("peer") == "local"
      and j.get("lan_url", "").startswith("http://") and j.get("pairing_needed") is False, (c, b))

# 2) LAN peer server-info
c, b = req("/api/server-info", host=LAN_IP)
j = json.loads(b)
check("E2 lan server-info peer=lan", c == 200 and j.get("peer") == "lan" and bool(j.get("lan_url")), (c, b))

# 3) LAN GET data without token/password -> 200 (trusted)
c, b = req("/api/career-db-status", host=LAN_IP)
check("E3 lan GET no creds -> 200", c == 200, (c, b))

# 4) LAN password login (no password set) -> local-ui full
c, b = req("/api/auth", "POST", {"pwd": ""}, host=LAN_IP)
j = json.loads(b)
check("E4 lan auth -> local-ui", c == 200 and j.get("scope") == "local-ui" and j.get("token"), (c, b))

# 5) anti-spoof: XFF present but NO tunnel running -> must be IGNORED (loopback stays local).
#    (The XFF-honoring public path is covered by _test_auth_peer.py with a fake tunnel process.)
c, b = req("/api/server-info", xff="8.8.8.8")
j = json.loads(b)
check("E5a xff ignored without tunnel", c == 200 and j.get("peer") == "local"
      and j.get("pairing_needed") is False, (c, b))

# 6) report page served + contains new panel wording (embedded resources updated)
c, b = req("/bidking_report.html")
check("E6 page has new security wording",
      c == 200 and "局域网无需配对" in b and "lanAccessBox" in b, (c, len(b)))

fails = [r for r in results if not r[1]]
print("=" * 50)
print("TOTAL %d  PASS %d  FAIL %d" % (len(results), len(results) - len(fails), len(fails)), flush=True)
sys.exit(1 if fails else 0)
