# ASCII-only end-to-end: mimic the NEW APK connect() flow against the real exe, from LAN IP.
import json, socket, sys, urllib.request, urllib.error

def req(path, method="GET", body=None, token=None, host=None):
    url = "http://%s:8766%s" % (host, path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80)); LAN = s.getsockname()[0]
finally:
    s.close()

ok_all = True
def check(name, cond, detail=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print(("PASS " if cond else "FAIL ") + name + ("  | " + str(detail)[:250] if not cond else ""), flush=True)

# Step A: app fetches server-info (no auth) from LAN
c, b = req("/api/server-info", host=LAN)
j = json.loads(b)
check("A server-info(lan) lan_url+pairing_needed=false",
      c == 200 and j.get("peer") == "lan" and j.get("lan_url") and j.get("pairing_needed") is False, (c, b))
print("  -> app would show LAN address: " + j.get("lan_url", ""), flush=True)

# Step B: app POST /api/auth with stored (possibly empty) password -> local-ui token, NO pairing wait
c, b = req("/api/auth", "POST", {"pwd": ""}, host=LAN)
j = json.loads(b)
tok = j.get("token", "")
check("B auth(lan) -> local-ui token, no pairing", c == 200 and j.get("scope") == "local-ui" and tok, (c, b))

# Step C: app loads report data with token
c, b = req("/api/career-db-status", token=tok, host=LAN)
check("C career-db-status with token", c == 200, (c, b))

# Step D: app polls launch status + lowstock + status (autoSync) with token
c, b = req("/api/launch/status", token=tok, host=LAN)
check("D1 launch/status", c == 200 and "state" in b, (c, b))
c, b = req("/api/lowstock", "POST", {}, token=tok, host=LAN)
check("D2 lowstock poll", c == 200 and "items" in b, (c, b))
c, b = req("/api/status", token=tok, host=LAN)
check("D3 /api/status autosync", c == 200 and "status" in b, (c, b))

print("=" * 50)
print("E2E " + ("ALL PASS" if ok_all else "HAS FAILURES"), flush=True)
sys.exit(0 if ok_all else 1)
