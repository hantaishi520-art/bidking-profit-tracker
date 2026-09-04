#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地服务器 + 实时刷新后端，用于打开富报表 bidking_report.html / 独立解析器 bidking_standalone.html。
- 直接双击 html 用 file:// 打开时，浏览器会拦截 fetch('result.json')/api 请求，故用本服务器。
- 富报表里的「实时刷新」按钮会调用本服务器的 /api/refresh，后台用完整（S2C_89+S2C45 双数据源）解析器重新生成 result.json。
- 公网 IP (ngrok)：保存 Token → 启动 ngrok → 获取公网地址 → 复制链接。
- 可被 exe 启动器 import：serve.init(root) 设定根目录，serve.make_server(port, root) 返回服务器。

用法：python serve.py [端口]   默认端口 8766。Ctrl+C 停止。
"""
import http.server, os, sys, json, threading, re, urllib.parse, subprocess, time, signal, calendar, sqlite3, shutil
import urllib.request as urllib2
import hmac, hashlib, secrets, base64   # 阶段2：设备配对（HMAC 挑战-响应 / 设备密钥派生）
import bidking_parser   # 作为模块导入，直接调用 parse()
try:
    import winlaunch    # Windows 窗口/进程辅助（远程启动外部程序用，纯 ctypes）
except Exception:       # 非 Windows 或缺失时降级，不影响主流程
    winlaunch = None
try:
    import winsec      # 本机安全存储（PBKDF2 / DPAPI，纯标准库）
except Exception:
    winsec = None                              # 非 Windows/异常时降级

GAME_LOG = os.path.join(os.path.expanduser("~"), "AppData", "LocalLow", "laolin", "BidKing", "Player.log")

def _low_stock_list(result_json):
    """从 result.json 计算『面板内低库存道具』（与前端 renderInventory 一致）：
    竞拍使用过 且 档位≥3 且 数量≤10。返回 [{'cid','name','count','quality'}]。"""
    LOW = 10
    inv = (result_json or {}).get("inventory") or {}
    items_used = set()
    for g in (result_json or {}).get("games") or []:
        for it in (g.get("items_used") or []):
            cid = str(it.get("cid") or "")
            if cid:
                items_used.add(cid)
    out = []
    for cid, it in (inv or {}).items():
        q = int(it.get("quality") or 0)
        c = int(it.get("count") or 0)
        if q >= 3 and c <= LOW and str(cid) in items_used:
            out.append({"cid": str(cid), "name": it.get("name") or "",
                        "count": c, "quality": q})
    return out

def _detect_lan_ip():
    """探测本机局域网 IP（多网卡时优先 192.168/10./172. 私有段）。
    采用 UDP connect 法：向公网 DNS 发一个不真正发包的 UDP 连接，
    让系统选路返回本机出口网卡 IP。失败则回退列出全部 IPv4。"""
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        pass
    # 回退：枚举所有 IPv4 网卡，优先私有段
    try:
        import socket
        ips = []
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
        for ip in ips:
            if ip.startswith(("192.168.", "10.", "172.")):
                return ip
        if ips:
            return ips[0]
    except Exception:
        pass
    return None

def _get_root():
    """工作根目录：冻结（exe）时取 exe 所在目录，否则取本脚本所在目录。
    这样即使 init() 未被调用，兜底路径也等于 exe 目录，不会回退到打包/源码目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

# ---- 可配置根目录（exe 启动器会设为 exe 所在目录）----
ROOT = _get_root()
DEFAULT_HERE = ROOT

def _get_res_dir():
    """内嵌静态资源目录（网页/价格表/ngrok 等）。exe 模式下指向临时解压目录，
    源码模式下指向脚本所在目录（src/）。launcher 运行时会用探测到的真实值覆盖。"""
    cands = [getattr(sys, "_MEIPASS", ""),
             os.path.dirname(os.path.abspath(__file__)),
             os.path.dirname(os.path.abspath(sys.argv[0])) if getattr(sys, "argv", [""])[0] else ""]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "bidking_report.html")):
            return c
    return os.path.dirname(os.path.abspath(__file__))

# 静态资源目录（优先从此读取 html/csv/json/ngrok），可写数据目录用 ROOT
RES_DIR = _get_res_dir()
CSV = os.path.join(RES_DIR, "item_prices.csv")
OUT = os.path.join(ROOT, "result.json")
# 历史快照目录（2026-09-04 用户反馈）：局数变少时备份的 result-<时间戳>.json
# 收纳进子文件夹，不再散落根目录；网页「选择 JSON」可列出并打开（续看上次战绩）
SNAPSHOT_DIR = os.path.join(ROOT, "历史快照")

# ---- 解析状态（供 /api/status 轮询）----
STATE = {"status": "idle", "code": 0, "msg": ""}
STATE_LOCK = threading.Lock()
_PARSE_THREAD = None

# ---- 版本署名（2026-08-30）：页面页脚 / server-info / 启动打印 共用这一个来源 ----
APP_VERSION = "1.4.4"
APP_SIGNATURE = "开源版"

# ---- 自动缺失拍品扫描缓存 ----
_MISSING_SCAN_RESULT = None      # 自动扫描结果缓存（dict 或 None）
_MISSING_SCAN_READY = False      # 前端轮询标志：上一次解析后产生了新扫描结果
_MISSING_SCAN_LOCK = threading.Lock()

# ---- 覆盖前保存警告（解析局数减少时备份旧 result.json，供用户选择保留）----
SAVE_WARN = {"visible": False, "requires_confirmation": False, "old_games": 0,
             "new_games": 0, "deadline_ms": 0, "status": "", "saved": False,
             "saved_path": "", "local": True}
SAVE_WARN_SECONDS = 30
SAVE_WARN_LOCK = threading.Lock()

# ---- 自动刷新控制权锁（防多端同时触发解析/覆盖）----
CONTROL_TTL = 30   # 秒，与前端 CONTROL_HEARTBEAT_MS(30s) 对齐
CONTROL_LOCK = threading.Lock()

def _pass_set(pwd):
    """设置访问密码（空字符串=不启用）。新格式：写入 PBKDF2 哈希（永不落明文）。"""
    try:
        pf = _get_pass_file()
        if pwd == "":
            # 清除密码：写入空内容即视为未启用（兼容沙箱禁止 os.remove 的环境）
            with open(pf, "w", encoding="utf-8") as f:
                f.write("")
            return True
        if _pass_is_hash(pwd):
            stored = pwd.strip()
        elif winsec is not None:
            stored = winsec.pbkdf2_hash(pwd.strip())
        else:
            # 无 winsec（非 Windows 降级）：仍写明文，但正常环境不应走到这里
            stored = pwd.strip()
        with open(pf, "w", encoding="utf-8") as f:
            f.write(stored)
        return True
    except Exception:
        return False

# ---- 可选访问密码（APK/远程控制用）：密码文件 ROOT/.bidking_pass，空文件=不启用 ----
PASS_FILE_NAME = ".bidking_pass"
_AUTH_TOKENS = {}            # token -> 过期时间戳（内存会话，进程重启失效）
_AUTH_TOKENS_LOCK = threading.Lock()
AUTH_TOKEN_TTL = 8 * 3600    # 会话令牌有效期 8 小时

def _get_pass_file():
    return os.path.join(ROOT, PASS_FILE_NAME)

def _pass_enabled():
    """是否启用了访问密码（密码文件存在且非空）。"""
    try:
        pf = _get_pass_file()
        if not os.path.isfile(pf):
            return False
        v = open(pf, "r", encoding="utf-8").read().strip()
        return bool(v) and v != "0"
    except Exception:
        return False

def _pass_read():
    """读取当前密码明文（仅本机可查，远程不提供）。返回 "" 表示未启用。"""
    try:
        pf = _get_pass_file()
        if not os.path.isfile(pf):
            return ""
        return open(pf, "r", encoding="utf-8").read().strip()
    except Exception:
        return ""

def _pass_is_hash(v):
    """判断是否 PBKDF2 哈希格式（pbkdf2$iters$salt$hash）。"""
    return isinstance(v, str) and v.startswith("pbkdf2$")


def _pass_hint():
    """密码提示：哈希格式只提示已设置；旧明文仍给首字符+长度。"""
    try:
        v = _pass_read()
    except Exception:
        return ""
    if not v:
        return ""
    try:
        if _pass_is_hash(v):
            return "已设置密码（PBKDF2 加密存储）"
        return "%s%s（%d 位）" % (v[0], "*" * max(0, len(v) - 1), len(v))
    except Exception:
        return ""


# ---- 公网密码在线试错防护（2026-08-30）：连续错 5 次 → 锁 15 分钟 ----
# 仅对 public 来源计数（局域网视同本机，永不受限）；全局计数（单用户工具足够，
# 不按 IP 区分——ngrok 出口 IP 也可能变化）。锁定期内一切密码尝试直接 429，
# 不再校验、不再累计；校验通过即清零。
_PASS_GUESS_MAX = 5
_PASS_LOCK_SECONDS = 900
_PASS_GUESS_LOCK = threading.Lock()
_PASS_GUESS = {"count": 0, "until": 0.0}


def _pass_gate_public_locked():
    """公网密码认证当前是否处于锁定期。返回剩余秒数（0=未锁）。"""
    with _PASS_GUESS_LOCK:
        return max(0.0, _PASS_GUESS["until"] - time.time())


def _pass_gate_public_record(ok):
    """记录一次公网密码校验结果。"""
    with _PASS_GUESS_LOCK:
        if ok:
            _PASS_GUESS["count"] = 0
            _PASS_GUESS["until"] = 0.0
            return
        if _PASS_GUESS["until"] > time.time():
            return                                  # 锁定期内不重复累计
        _PASS_GUESS["count"] += 1
        if _PASS_GUESS["count"] >= _PASS_GUESS_MAX:
            _PASS_GUESS["until"] = time.time() + _PASS_LOCK_SECONDS
            _PASS_GUESS["count"] = 0


def _pass_verify_public(self, pwd):
    """带试错锁定的密码校验（/api/auth 与 /api/pair/request 共用）。
    返回 (ok, wait_secs)：wait_secs>0 表示处于锁定期直接拒绝。
    本机/局域网不受限（视同本机信任）；仅 public 计数。"""
    if _peer_class(self) != "public" or not _pass_enabled():
        return _pass_verify(pwd), 0
    wait = _pass_gate_public_locked()
    if wait > 0:
        return False, wait
    ok = _pass_verify(pwd)
    _pass_gate_public_record(ok)
    return ok, 0


def _pass_verify(pwd):
    """校验密码。未启用→任何密码通过；PBKDF2 哈希→winsec 校验；
旧明文→常量时间比对，比对通过后自动迁移为哈希（无感升级）。"""
    try:
        pf = _get_pass_file()
        if not os.path.isfile(pf):
            return True
        v = open(pf, "r", encoding="utf-8").read().strip()
        if not v or v == "0":
            return True
        if _pass_is_hash(v):
            if winsec is None:
                return True   # 无 winsec 时只能放行（降级环境）
            return winsec.pbkdf2_verify(pwd or "", v)
        # 旧明文：常量时间比较；通过则自动迁移
        import hmac
        ok = hmac.compare_digest(v.encode("utf-8"), (pwd or "").encode("utf-8"))
        if ok and winsec is not None:
            try:
                _pass_set(pwd)
            except Exception:
                pass
        return ok
    except Exception:
        return True

def _token_issue(scope="local-ui", device_id="", ip=""):
    """签发会话令牌，并记录它的"身份"与权限级别（阶段2）。

    scope 三档：
      local-ui        本机网页：全权（改密码、清库、改启动配置…）
      device          已配对的手机：读写报表，但**动不了**密码/清库/启动配置/ngrok
      legacy-readonly 旧版 App（只知道密码、没配对）：只读，改不了任何东西
    """
    import hashlib as _hl, secrets as _sc
    tk = _hl.sha256(_sc.token_bytes(32)).hexdigest()
    with _AUTH_TOKENS_LOCK:
        _AUTH_TOKENS[tk] = {"exp": time.time() + AUTH_TOKEN_TTL, "scope": scope,
                            "device_id": device_id, "ip": ip,
                            "created": time.time(), "last_used": time.time()}
    return tk


def _token_info(token):
    """取令牌的详细信息并续期；无效/过期返回 None。"""
    if not token:
        return None
    with _AUTH_TOKENS_LOCK:
        now = time.time()
        info = _AUTH_TOKENS.get(token)
        if not info:
            return None
        if isinstance(info, (int, float)):      # 兼容改造前的旧结构
            info = {"exp": info, "scope": "local-ui", "device_id": "", "ip": "",
                    "created": now, "last_used": now}
            _AUTH_TOKENS[token] = info
        if float(info.get("exp") or 0) < now:
            _AUTH_TOKENS.pop(token, None)
            return None
        info["exp"] = now + AUTH_TOKEN_TTL      # 续期
        info["last_used"] = now
        return info


def _token_check(token):
    """校验令牌是否有效（存在且未过期），有效则续期并返回 True。"""
    return _token_info(token) is not None


def _token_scope(token):
    return (_token_info(token) or {}).get("scope") or ""


# 只有本机网页能调的敏感写接口（配对设备也不行：丢了手机不该能改密码/清库）
_SCOPE_SENSITIVE = {"/api/pass-set", "/api/career-db-clear", "/api/career-db-clean",
                    "/api/launch/config", "/api/ngrok/token", "/api/ngrok/start",
                    "/api/ngrok/stop", "/api/control/claim", "/api/control/heartbeat",
                    "/api/control/release"}
# 旧版只读令牌唯一允许的 POST（本质是只读查询：读 result.json 算低库存）
_LEGACY_POST_ALLOW = {"/api/lowstock"}


def _scope_allows(self, path):
    """按令牌身份判断能否调用这个写接口（阶段2）。"""
    pc = _peer_class(self)
    if pc == "local":
        return True              # 本机一律全权，与 _auth_required 的本机放行保持一致
    if pc == "lan":
        # 2026-08-29 用户决策：局域网视同本机（能进来的都过了密码这一关，
        # 或者根本没设密码）；配对/只读降级/启动开关这些严苛防护只针对公网。
        return True
    scope = _token_scope(_request_token(self))
    if scope == "local-ui":
        return True
    sw = (_sec_load().get("switches") or {})
    if path == "/api/launch":
        # 让手机远程启动程序风险高（能拉起 exe），单独由一个开关控制，默认关
        return bool(sw.get("allow_remote_launch"))
    if scope == "device":
        return path not in _SCOPE_SENSITIVE
    if scope == "legacy-readonly":
        return path in _LEGACY_POST_ALLOW
    return False

def _request_token(self):
    """从请求头/查询参数/Cookie 取令牌。
    Cookie（2026-08-30）：App 连接成功时把令牌写进 WebView Cookie——页面发出的
    **第一个**请求就自动携带，消灭"页面先请求、令牌后注入"的 401 竞态
    （局域网+已设密码时表现为生涯/道具/价格库全没数据）。"""
    h = self.headers.get("X-Auth-Token") or ""
    if h.strip():
        return h.strip()
    c = self.headers.get("Cookie") or ""
    if "bk_token=" in c:
        for part in c.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "bk_token" and v.strip():
                return v.strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    return (qs.get("token") or [""])[0].strip()

# ---- 本机 IP 集合（用于 Host 白名单，防 DNS-rebinding）----
_LOCAL_IP_CACHE = {"ips": set(), "ts": 0.0}
_LOCAL_IP_TTL = 30.0   # 秒；换 WiFi 后 IP 会变，缓存不能太久


def _local_ip_set(force=False):
    """返回本机所有 IP 地址集合（含回环）。带 30 秒缓存，force=True 强制重算。"""
    now = time.time()
    if not force and _LOCAL_IP_CACHE["ips"] and (now - _LOCAL_IP_CACHE["ts"]) < _LOCAL_IP_TTL:
        return _LOCAL_IP_CACHE["ips"]
    ips = set()
    ips.add("127.0.0.1")
    ips.add("::1")
    try:
        import socket as _sk
        for info in _sk.getaddrinfo(_sk.gethostname(), None):
            try:
                ips.add(info[4][0])
            except Exception:
                pass
        # 再补一条"默认出口网卡"的 IP（getaddrinfo 有时拿不全）
        s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))   # 阿里 DNS，仅用于探测出口 IP，不发包
            ips.add(s.getsockname()[0])
        except Exception:
            pass
        finally:
            s.close()
    except Exception:
        pass
    _LOCAL_IP_CACHE["ips"] = ips
    _LOCAL_IP_CACHE["ts"] = now
    return ips


def _content_type_ok(self, path=""):
    """POST 的 Content-Type 校验（P0-4，防 CSRF）。

    浏览器跨域"简单请求"只允许三种 Content-Type：
    text/plain、multipart/form-data、application/x-www-form-urlencoded。
    攻击者正是用 text/plain 绕过预检，直接 POST 到 127.0.0.1 并利用本机免密。
    要求 application/json 后，跨域请求必然触发预检，
    而服务端不响应 Access-Control-Allow-Headers → 预检失败 → 被浏览器拦死。
    """
    ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if path in ("/api/career-import",):
        # 该接口接收 ZIP 二进制，允许二进制类型（前端未使用，属遗留接口）
        return ct in ("application/zip", "application/octet-stream",
                      "application/x-zip-compressed", "multipart/form-data", "")
    return ct == "application/json"


def _is_local_peer(self):
    """请求是否来自本机回环地址（用服务端视角的 TCP 对端 IP，客户端无法伪造）。"""
    return _peer_class(self) == "local"


def _classify_ipv4(ip):
    """按 IP 字面值分级：local 回环 / lan 私有段（含链路本地）/ public 其他。"""
    p = str(ip or "").strip().lower()
    if p.startswith("::ffff:"):
        p = p[7:]
    if p in ("127.0.0.1", "::1"):
        return "local"
    if ":" in p:
        return "public"                # 其余 IPv6 一律按公网处理
    parts = p.split(".")
    if len(parts) == 4 and all(x.isdigit() and 0 <= int(x) <= 255 for x in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
            return "lan"
        if a == 169 and b == 254:      # 链路本地（直连网段）也算局域网
            return "lan"
    return "public"


_TUNNEL_CACHE = {"ok": False, "ts": 0.0}


def _tunnel_active():
    """ngrok 隧道是否在转发（本程序启动的，或用户手动在外部启动的）。带 10 秒缓存。"""
    now = time.time()
    if _TUNNEL_CACHE["ts"] and (now - _TUNNEL_CACHE["ts"]) < 10.0:
        return _TUNNEL_CACHE["ok"]
    ok = NGROK_PROC is not None and NGROK_PROC.poll() is None
    if not ok:
        try:
            urllib2.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=1).read()
            ok = True
        except Exception:
            ok = False
    _TUNNEL_CACHE["ok"] = ok
    _TUNNEL_CACHE["ts"] = now
    return ok


def _peer_class(self):
    """把请求来源分成三级（2026-08-29 用户决策：严苛防护只针对公网/ngrok）：
      local  本机（回环直连）—— 全权
      lan    局域网私有地址（10.x / 172.16-31.x / 192.168.x / 169.254.x）
             —— 视同本机信任：未设密码免鉴权；设了密码凭密码换令牌即可，
             不要求配对、不降级只读
      public 其他（ngrok 公网访客、互联网）—— 走完整安全链（密码+配对+令牌）

    ⚠️ ngrok 的坑：`ngrok http <port>` 的转发流量到达本服务时 TCP 对端是
    127.0.0.1（ngrok agent 在本机回程连接），所以公网访客在 TCP 层看起来像本机。
    必须看 ngrok 注入的 X-Forwarded-For 才能还原真实来源。取**最右侧**一节
    （由可信代理 ngrok 追加的真实客户端 IP；左侧可能被客户端伪造）。
    只有隧道确实在运行时才采信 XFF；非回环对端的 XFF 一律不信（可伪造）。
    """
    try:
        peer = (self.client_address or ("", 0))[0] or ""
    except Exception:
        return "public"
    p = str(peer).strip().lower()
    if p.startswith("::ffff:"):
        p = p[7:]
    if p not in ("127.0.0.1", "::1"):
        return _classify_ipv4(p)
    # 回环对端：区分「本机浏览器」与「ngrok 转发来的公网访客」
    if _tunnel_active():
        try:
            xff = (self.headers.get("X-Forwarded-For") or "").strip()
            if xff:
                return _classify_ipv4(xff.split(",")[-1].strip())
        except Exception:
            pass
    return "local"


def _lan_trusted(self):
    """局域网信任判定：未设访问密码时，局域网请求视同本机放行。"""
    return _peer_class(self) == "lan" and not _pass_enabled()


def _host_ok(self):
    """校验 Host 头，防 DNS-rebinding（P0-3）。

    攻击原理：恶意域名首次解析到攻击者的 IP 让浏览器放行，随后把 DNS 重解析到
    127.0.0.1，浏览器便认为 evil.com 与本机服务同源，从而绕过同源策略读写本服务。
    防御：Host 必须是 localhost / 回环地址 / 本机网卡 IP / 当前 ngrok 域名。

    注意：Host 头缺失时放行——浏览器发起的跨站请求一定携带 Host，
    缺 Host 的通常是脚本类客户端，拒绝它们只会误伤。
    """
    try:
        host = (self.headers.get("Host") or "").strip()
    except Exception:
        return True
    if not host:
        return True
    h = host.lower()
    if h.startswith("["):                      # IPv6 形如 [::1]:8766
        h = h[1:h.find("]")] if "]" in h else h
    else:
        h = h.split(":")[0]
    h = h.strip(".")
    if not h:
        return True
    if h in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if h in _local_ip_set():
        return True
    if h in _local_ip_set(force=True):         # 换网络后重新探测一次，避免误伤
        return True
    # 当前 ngrok 公网域名（公网访问时 Host 是 ngrok 域名而非 IP）
    try:
        nu = (_ngrok_public_url() or "").lower()
        if nu:
            nh = (urllib.parse.urlparse(nu).hostname or "").lower()
            if nh and h == nh:
                return True
    except Exception:
        pass
    return False


def _auth_required(self, data):
    """POST 写接口统一鉴权（P0-4 重写；2026-08-29 按「局域网信任」放宽）。

    新规则：
      - 本机回环（127.0.0.1/::1）：放行。
      - 局域网（10.x/192.168.x/172.16-31.x）：未设访问密码 → 视同本机放行；
        已设密码 → 需有效令牌（用密码经 /api/auth 换来，局域网登录给全权）。
      - 公网（ngrok/互联网）：必须有有效令牌。
    """
    pc = _peer_class(self)
    if pc == "local":
        return True
    if _lan_trusted(self):
        return True
    token = _request_token(self)
    return _token_check(token)


# ==================== 阶段2：设备配对 · 安全凭据层 ====================
# 目标：手机连接电脑要过三关 —— 「密码（你知道）+ 电脑人工同意（你做）+ 设备密钥（你有）」。
# 只有密码不够：密码会 leaked、会被同 WiFi 的人撞；加了「电脑端必须人工点同意」，
# 即使密码泄露，攻击者也过不了第二关。配对成功后手机拿到一把设备密钥，
# 之后登录改用 HMAC 挑战-响应，**密码不再在网络上传输**。
#
# 凭据存在 ROOT/.bidking_sec.bin，内容用 Windows DPAPI 加密（绑定本机+本用户）：
#   master    32 字节主密钥，用来派生每个设备的设备密钥（主密钥本身永不出网）
#   server_id 服务端标识，参与登录签名，防重放
#   devices[] 已配对设备（含其设备密钥）
#   pending[] 待你批准的配对请求（5 分钟过期）
#   switches  4 个安全开关

SEC_FILE_NAME = ".bidking_sec.bin"
_SEC_CACHE = {"obj": None, "loaded": False}
_SEC_LOCK = threading.Lock()

API_VERSION = 2        # 接口协议版本：2 = 支持设备配对
MIN_APP_VERSION = 2    # 低于此版本的 App 走 legacy 只读分支（不强制升级，先保证能连上）

PAIR_PENDING_TTL = 300      # 配对请求 5 分钟过期
PAIR_PENDING_MAX = 3        # 最多同时 3 个待处理请求（防刷屏/撞库）
PAIR_NONCE_TTL = 120        # 登录挑战 nonce 2 分钟有效
PAIR_CLOCK_SKEW = 300       # 允许手机与电脑时钟偏差 5 分钟


def _sec_path():
    return os.path.join(ROOT, SEC_FILE_NAME)


def _sec_new():
    """首次运行时生成全新的凭据容器。"""
    return {
        "v": 1,
        "master": base64.b64encode(secrets.token_bytes(32)).decode(),
        "server_id": secrets.token_hex(16),
        "devices": [],
        "pending": [],
        "switches": {
            "pairing_required": True,       # 新设备必须配对
            "allow_legacy_readonly": True,  # 未配对的旧版 App 允许只读（不强制升级）
            "allow_remote_launch": False,   # 手机远程启动估价器，默认关
            "ngrok_need_pair": True,        # ngrok 需已设密码且已配对
        },
    }


def _sec_load():
    """读凭据（进程内缓存）。DPAPI 解不开（换机/文件损坏）时返回全新容器。"""
    with _SEC_LOCK:
        if _SEC_CACHE["loaded"] and isinstance(_SEC_CACHE["obj"], dict):
            return _SEC_CACHE["obj"]
        obj = None
        if winsec is not None:
            obj = winsec.secure_read_json(_sec_path(), None)
        if not isinstance(obj, dict) or not obj.get("master"):
            # 文件不存在、或换机后 DPAPI 解不开 → 重新生成（阶段3 会做显式提示）
            obj = _sec_new()
            if winsec is not None:
                winsec.secure_write_json(_sec_path(), obj)
        obj.setdefault("devices", [])
        obj.setdefault("pending", [])
        obj.setdefault("switches", {})
        if not obj.get("server_id"):
            obj["server_id"] = secrets.token_hex(16)
        # 阶段3：首次生成后把机器指纹写进凭据（作为换机检测的基线）
        if not obj.get("fp_guid") and winsec is not None:
            _sig = winsec.machine_signals()
            obj["fp_guid"] = _sig.get("guid")
            obj["fp_volserial"] = _sig.get("volserial")
            obj["fp_installdate"] = _sig.get("installdate")
            winsec.secure_write_json(_sec_path(), obj)
        for _k, _v in (("pairing_required", True), ("allow_legacy_readonly", True),
                       ("allow_remote_launch", False), ("ngrok_need_pair", True)):
            obj["switches"].setdefault(_k, _v)
        _SEC_CACHE["obj"] = obj
        _SEC_CACHE["loaded"] = True
        return obj


def _sec_save(obj=None):
    """写回凭据（DPAPI 加密 + 原子写）。"""
    with _SEC_LOCK:
        o = obj if isinstance(obj, dict) else _SEC_CACHE.get("obj")
        if not isinstance(o, dict) or winsec is None:
            return False
        return winsec.secure_write_json(_sec_path(), o)[0]


def _sec_master():
    """取主密钥原始字节（永不出网）。"""
    try:
        return base64.b64decode(_sec_load().get("master") or "")
    except Exception:
        return b""


def _device_key(did):
    """由主密钥派生某设备的设备密钥：HMAC-SHA256(master, "dev|"+device_id)。

    用派生而非随机存储的好处：主密钥是唯一需要保密的东西，
    设备密钥丢了可以按同一个 device_id 重新算出来，不必额外保存。
    """
    return hmac.new(_sec_master(), ("dev|" + str(did)).encode("utf-8"), hashlib.sha256).hexdigest()


def _device_find(did):
    for d in _sec_load().get("devices") or []:
        if d.get("id") == did:
            return d
    return None


def _pair_sweep():
    """清掉过期/已处理的配对请求。"""
    obj = _sec_load()
    now = time.time()
    kept = []
    changed = False
    for r in obj.get("pending") or []:
        if r.get("state") != "pending":
            changed = True
            continue
        if float(r.get("exp") or 0) <= now:
            changed = True
            continue
        kept.append(r)
    if changed:
        obj["pending"] = kept
    return obj


def _pair_request(did, name, model, pwd, ip):
    """手机发起配对请求。返回 (rid, error)。"""
    if not did or len(str(did)) > 128:
        return None, "device_id 无效"
    obj = _pair_sweep()
    # 已配对的设备不必再请求
    if _device_find(did):
        return None, "该设备已配对"
    # 同源请求合并（手机重复点「配对」不刷屏）
    for r in obj["pending"]:
        if r.get("id") == did:
            return r["rid"], ""
    # 必须知道密码才能排队等批准（第二道关）
    if _pass_enabled() and not _pass_verify(pwd or ""):
        return None, "密码错误"
    if len(obj["pending"]) >= PAIR_PENDING_MAX:
        return None, "待处理的配对请求过多，请先在电脑上处理"
    rid = secrets.token_hex(12)
    obj["pending"].append({
        "rid": rid, "id": did,
        "name": str(name or "")[:40], "model": str(model or "")[:40],
        "ip": str(ip or ""), "ts": time.time(),
        "exp": time.time() + PAIR_PENDING_TTL, "state": "pending",
    })
    _sec_save(obj)
    return rid, ""


def _pair_approve(rid):
    """电脑端同意配对：生成设备密钥并写入设备表。仅本机可调。"""
    obj = _sec_load()
    for r in obj.get("pending") or []:
        if r.get("rid") != rid or r.get("state") != "pending":
            continue
        if float(r.get("exp") or 0) <= time.time():
            r["state"] = "expired"
            _sec_save(obj)
            return None, "该请求已过期"
        did = r.get("id")
        if _device_find(did):
            r["state"] = "approved"
            _sec_save(obj)
            return did, ""
        obj["devices"].append({
            "id": did, "name": r.get("name") or "", "model": r.get("model") or "",
            "key": _device_key(did), "paired_at": time.time(),
            "last_ip": r.get("ip") or "", "last_seen": time.time(),
        })
        r["state"] = "approved"      # 保留到手机领走密钥为止
        _sec_save(obj)
        return did, ""
    return None, "请求不存在或已处理"


def _pair_reject(rid):
    obj = _sec_load()
    for r in obj.get("pending") or []:
        if r.get("rid") == rid and r.get("state") == "pending":
            r["state"] = "rejected"
            _sec_save(obj)
            return True
    return False


def _pair_status(rid, did):
    """手机轮询配对结果。approved → 下发设备密钥**一次**，随即作废该请求。"""
    obj = _sec_load()
    for r in obj.get("pending") or []:
        if r.get("rid") != rid or r.get("id") != did:
            continue
        st = r.get("state")
        if st == "pending":
            return {"state": "pending"}
        if st == "approved":
            dk = _device_key(did)
            obj["pending"] = [x for x in obj["pending"] if x.get("rid") != rid]
            _sec_save(obj)
            return {"state": "approved", "device_key": dk,
                    "server_id": obj.get("server_id") or "",
                    # 网页浏览器没有现成的挑战-响应登录 UI，直接发一个设备级令牌，
                    # 页面存起来即可看数据（device_key 仍供 App 走挑战-响应长期登录）
                    "browser_token": _token_issue(scope="device", device_id=did, ip="")}
        return {"state": st or "rejected"}
    if _device_find(did):
        return {"state": "already_paired"}
    return {"state": "not_found"}


def _pair_revoke(did):
    """撤销设备：删设备 + 立即作废它的所有令牌。仅本机可调。"""
    obj = _sec_load()
    before = len(obj.get("devices") or [])
    obj["devices"] = [d for d in obj["devices"] if d.get("id") != did]
    _sec_save(obj)
    with _AUTH_TOKENS_LOCK:
        for tk, info in list(_AUTH_TOKENS.items()):
            if isinstance(info, dict) and info.get("device_id") == did:
                _AUTH_TOKENS.pop(tk, None)
    return len(obj.get("devices") or []) < before


# ---- 登录挑战（防重放）：nonce 一次性，2 分钟有效 ----
_NONCES = {}                 # nonce -> 过期时间
_NONCES_LOCK = threading.Lock()


def _nonce_issue(did):
    n = secrets.token_hex(16)
    with _NONCES_LOCK:
        now = time.time()
        for k, v in list(_NONCES.items()):       # 顺手清理
            if v < now:
                _NONCES.pop(k, None)
        _NONCES[n] = now + PAIR_NONCE_TTL
    return n


def _nonce_consume(n):
    if not n:
        return False
    with _NONCES_LOCK:
        exp = _NONCES.pop(n, 0)
    return exp > time.time()


def _verify_device_sig(did, nonce, ts, sig):
    """校验设备登录签名：sig = HMAC-SHA256(device_key, "auth|"+server_id+"|"+nonce+"|"+ts)。

    签名里带上 server_id 和一次性 nonce，即使签名被截获也无法重放；
    ts 参与签名并检查时钟偏差，防止拿旧签名反复用。
    """
    d = _device_find(did)
    if not d:
        return False, "设备未配对"
    try:
        ts_i = int(ts)
    except Exception:
        return False, "时间戳无效"
    if abs(time.time() - ts_i) > PAIR_CLOCK_SKEW:
        return False, "设备与电脑时间相差过大，请检查手机时间"
    if not _nonce_consume(nonce):
        return False, "登录挑战已失效，请重试"
    want = hmac.new(d["key"].encode("utf-8"),
                    ("auth|%s|%s|%d" % (_sec_load().get("server_id") or "", nonce, ts_i))
                    .encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, str(sig or "")):
        return False, "签名校验失败"
    d["last_seen"] = time.time()
    _sec_save()
    return True, ""


# ---- ngrok 状态 ----
NGROK_PROC = None          # subprocess.Popen 对象
NGROK_TOKEN_FILE = None    # authtoken 文件路径（ROOT 下 .ngrok_token）
NGROK_LOCK = threading.Lock()
NGROK_PORT = 8766          # 默认映射端口

def init(root=None, res_dir=None):
    """设定工作根目录（result.json 与数据库等可写数据放这里）与内嵌资源目录。"""
    global ROOT, CSV, OUT, NGROK_TOKEN_FILE, DB_PATH, CAREER_DB_DIR, CAREER_DB, RES_DIR, SNAPSHOT_DIR
    ROOT = root or DEFAULT_HERE
    # 源码模式直接运行 serve.py 时脚本在 src/ 子目录，数据应落在项目根（src 的父目录）
    if os.path.basename(ROOT) == "src":
        ROOT = os.path.dirname(ROOT)
    if res_dir:
        RES_DIR = res_dir
    CSV = os.path.join(RES_DIR, "item_prices.csv")
    OUT = os.path.join(ROOT, "result.json")
    SNAPSHOT_DIR = os.path.join(ROOT, "历史快照")
    NGROK_TOKEN_FILE = os.path.join(ROOT, ".ngrok_token")
    DB_PATH = os.path.join(ROOT, "item_prices.db")
    CAREER_DB_DIR = os.path.join(ROOT, "生涯数据库")
    CAREER_DB = os.path.join(CAREER_DB_DIR, "career.db")

# ---- 生涯数据库 ----
CAREER_DB_DIR = os.path.join(ROOT, "生涯数据库")
CAREER_DB = os.path.join(CAREER_DB_DIR, "career.db")

def _career_fingerprint(g):
    """对局去重指纹：仅由对局自身字段构成（不含 uid）。
    同局无论解析时uid被识别成真实值还是字面'auto'，都算同一局，避免重复插入。
    使用较多字段（时间/地图/输赢/盈亏/拍品值/双方出价/回合）以降低"同秒同图同盈亏"的误碰撞。"""
    return f"{g.get('ts',0)}|{g.get('map_id',0)}|{int(bool(g.get('is_win',False)))}|{g.get('my_profit',0)}|{g.get('actual_value',0)}|{g.get('final_bid',0)}|{g.get('winner_final_bid',0)}|{g.get('rounds',0)}"

def _career_db_connect():
    """确保生涯数据库目录和文件存在，返回连接。"""
    os.makedirs(CAREER_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(CAREER_DB)
    conn.row_factory = sqlite3.Row
    # 确保表结构存在
    conn.execute("""CREATE TABLE IF NOT EXISTS career_games (
        fingerprint TEXT PRIMARY KEY,
        uid TEXT, ts INTEGER, map_id INTEGER, map_name TEXT,
        is_win INTEGER, my_profit INTEGER, actual_value INTEGER,
        source TEXT, game_json TEXT, imported_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS career_settings (
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS item_price_overrides (
        cid TEXT PRIMARY KEY, price INTEGER DEFAULT 0, updated_at INTEGER)""")
    # 缺失拍品扫描状态：记录"已扫描过的对局 fingerprint"，实现增量扫描（只扫新场次）
    conn.execute("""CREATE TABLE IF NOT EXISTS missing_scan_log (
        fp TEXT PRIMARY KEY, scanned_at TEXT)""")
    # 初始化默认设置
    for k, v in [("retention_days", "0"), ("last_import", "")]:
        conn.execute("INSERT OR IGNORE INTO career_settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    return conn

def _career_db_status():
    """返回生涯数据库状态。"""
    try:
        conn = _career_db_connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM career_games")
        total_games = cur.fetchone()[0]
        cur.execute("SELECT MIN(ts), MAX(ts) FROM career_games")
        row = cur.fetchone()
        earliest_ts = row[0] if row and row[0] else None
        latest_ts = row[1] if row and row[1] else None
        cur.execute("SELECT value FROM career_settings WHERE key='retention_days'")
        rd_row = cur.fetchone()
        retention_days = int(rd_row[0]) if rd_row and rd_row[0] else 0
        cur.execute("SELECT value FROM career_settings WHERE key='last_import'")
        li_row = cur.fetchone()
        last_import = li_row[0] if li_row and li_row[0] else ""
        conn.close()
        return {
            "enabled": True,
            "db_path": CAREER_DB,
            "total_games": total_games or 0,
            "earliest_ts": earliest_ts,
            "latest_ts": latest_ts,
            "retention_days": retention_days,
            "last_import": last_import or "暂无"
        }
    except Exception as e:
        return {"enabled": False, "error": str(e), "db_path": CAREER_DB,
                "total_games": 0, "earliest_ts": None, "latest_ts": None,
                "retention_days": 0, "last_import": "暂无"}

def _career_db_merge_result(result_path=None):
    """将 result.json 的游戏数据合并到生涯数据库（按 fingerprint 去重）。"""
    result_path = result_path or OUT
    if not os.path.isfile(result_path):
        return 0, 0
    try:
        with open(result_path, "r", encoding="utf-8") as f:
            result = json.load(f)
        games = result.get("games", [])
        uid = result.get("uid", "auto")
        if not games:
            return 0, 0
        conn = _career_db_connect()
        cur = conn.cursor()
        inserted = 0
        skipped = 0
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for g in games:
            # fingerprint = uid|ts|map_id|is_win|my_profit|actual_value
            fp = _career_fingerprint(g)
            cur.execute("SELECT 1 FROM career_games WHERE fingerprint=?", (fp,))
            if cur.fetchone():
                skipped += 1
                continue
            game_json = json.dumps(g, ensure_ascii=False)
            cur.execute("""INSERT INTO career_games
                (fingerprint, uid, ts, map_id, map_name, is_win, my_profit, actual_value, source, game_json, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fp, uid, g.get("ts",0), g.get("map_id",0), g.get("map_name",""),
                 int(g.get("is_win",False)), g.get("my_profit",0), g.get("actual_value",0),
                 "result.json", game_json, now))
            inserted += 1
        conn.commit()
        conn.close()
        return inserted, skipped
    except Exception:
        return 0, 0

def _career_db_insert_one(conn, g, uid, source="live-parse"):
    """把单局实时写入生涯数据库（按 fingerprint 去重），立即提交。供解析过程中逐局落库，防关机丢数据。"""
    fp = _career_fingerprint(g)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM career_games WHERE fingerprint=?", (fp,))
    if cur.fetchone():
        return False
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    game_json = json.dumps(g, ensure_ascii=False)
    conn.execute("""INSERT INTO career_games
        (fingerprint, uid, ts, map_id, map_name, is_win, my_profit, actual_value, source, game_json, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fp, uid, g.get("ts",0), g.get("map_id",0), g.get("map_name",""),
         int(g.get("is_win",False)), g.get("my_profit",0), g.get("actual_value",0),
         source, game_json, now))
    conn.commit()
    return True

def _career_db_touch_last_import():
    """更新「上次导入时间」为当前时间。"""
    try:
        conn = _career_db_connect()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT OR REPLACE INTO career_settings (key, value) VALUES ('last_import', ?)", (now,))
        conn.commit()
        conn.close()
    except Exception:
        pass

def _career_db_get_games(uid=None, since_ts=None):
    """从生涯数据库读取游戏数据；uid 给定时只返回该账号（P0-2 分账）。

    since_ts 给定时只返回 ts >= since_ts 的对局（增量拉取，2026-08-30）：
    配合客户端 IndexedDB 缓存，手机经 ngrok 打开生涯页只拿新局，不必每次全量 48MB。
    uid 注入每一条（game_json 本身不含 uid，uid 是表列），前端才能本地分账过滤。
    """
    try:
        conn = _career_db_connect()
        cur = conn.cursor()
        sql = "SELECT uid, game_json FROM career_games"
        cond, args = [], []
        if uid:
            cond.append("uid=?")
            args.append(uid)
        if since_ts is not None:
            cond.append("ts>=?")
            args.append(int(since_ts))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY ts DESC"
        cur.execute(sql, args)
        games = []
        for row in cur.fetchall():
            try:
                g = json.loads(row[1])
                g["uid"] = row[0]
                games.append(g)
            except Exception:
                pass
        conn.close()
        return games
    except Exception:
        return []

def _career_db_list_uids():
    """返回生涯库中各账号 uid 及其局数（按局数降序），供前端分账选择。"""
    try:
        conn = _career_db_connect()
        cur = conn.cursor()
        cur.execute("SELECT uid, COUNT(*) FROM career_games GROUP BY uid ORDER BY COUNT(*) DESC")
        rows = [{"uid": r[0], "count": r[1]} for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []

def _career_db_clean(retention_days):
    """按保留天数清理旧数据。"""
    if retention_days <= 0:
        return 0, _career_db_status()
    try:
        conn = _career_db_connect()
        cur = conn.cursor()
        cutoff = int(time.time()) - retention_days * 86400
        cur.execute("SELECT COUNT(*) FROM career_games WHERE ts < ?", (cutoff,))
        deleted = cur.fetchone()[0]
        cur.execute("DELETE FROM career_games WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
        return deleted, _career_db_status()
    except Exception:
        return 0, _career_db_status()

def _career_db_clear():
    """清空所有生涯数据。"""
    try:
        conn = _career_db_connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM career_games")
        deleted = cur.fetchone()[0]
        cur.execute("DELETE FROM career_games")
        conn.commit()
        conn.close()
        return deleted, _career_db_status()
    except Exception:
        return 0, _career_db_status()

def _career_db_import_zip(zip_bytes):
    """导入 ZIP 中的 result.json 文件，合并到生涯数据库。"""
    import zipfile, io
    inserted = 0
    skipped = 0
    invalid = 0
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception:
        return 0, 0, 1
    conn = _career_db_connect()
    cur = conn.cursor()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for name in zf.namelist():
        if not name.endswith(".json"):
            continue
        try:
            content = zf.read(name).decode("utf-8")
            data = json.loads(content)
            games = data.get("games", [])
            uid = data.get("uid", "auto")
            if not games:
                continue
            for g in games:
                fp = _career_fingerprint(g)
                cur.execute("SELECT 1 FROM career_games WHERE fingerprint=?", (fp,))
                if cur.fetchone():
                    skipped += 1
                    continue
                game_json = json.dumps(g, ensure_ascii=False)
                cur.execute("""INSERT INTO career_games
                    (fingerprint, uid, ts, map_id, map_name, is_win, my_profit, actual_value, source, game_json, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (fp, uid, g.get("ts",0), g.get("map_id",0), g.get("map_name",""),
                     int(g.get("is_win",False)), g.get("my_profit",0), g.get("actual_value",0),
                     f"ZIP:{name}", game_json, now))
                inserted += 1
        except Exception:
            invalid += 1
    # 更新上次导入时间
    cur.execute("UPDATE career_settings SET value=? WHERE key='last_import'", (now,))
    conn.commit()
    conn.close()
    return inserted, skipped, invalid
DB_PATH = os.path.join(ROOT, "item_prices.db")

def _db_read_prices():
    """从 SQLite 读取所有 cid→price 映射。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT cid, price FROM item_prices")
        rows = cur.fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}

def _db_save_prices(prices_dict):
    """将 prices_dict 保存到 SQLite，返回保存后的 prices。
    使用 upsert（只更新 price，保留 name 列），避免 INSERT OR REPLACE 清空用户自定义名称。"""
    try:
        _db_ensure_prices_schema()
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        for cid, price in prices_dict.items():
            # ON CONFLICT 只更新 price，不触碰 name；新行 name 取已有值（通常为 NULL，由 CSV 提供）
            cur.execute(
                "INSERT INTO item_prices (cid, price, name) "
                "VALUES (?, ?, (SELECT name FROM item_prices WHERE cid = ?)) "
                "ON CONFLICT(cid) DO UPDATE SET price = excluded.price",
                (cid, int(price), cid))
        conn.commit()
        # 读取全部确认
        cur.execute("SELECT cid, price FROM item_prices")
        rows = cur.fetchall()
        conn.close()
        return {r[0]: r[1] for r in rows}
    except Exception as e:
        return {"error": str(e)}

def _db_ensure_prices_schema():
    """确保 item_prices 表存在且含 name 列（兼容旧库）。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS item_prices (cid TEXT PRIMARY KEY, price INTEGER DEFAULT 0, name TEXT)")
        cols = [r[1] for r in cur.execute("PRAGMA table_info(item_prices)")]
        if "name" not in cols:
            cur.execute("ALTER TABLE item_prices ADD COLUMN name TEXT")
        conn.commit()
        conn.close()
    except Exception:
        pass

def _db_read_prices_full():
    """读取 item_prices 全量（含 name），返回 [{cid, price, name}]。"""
    try:
        _db_ensure_prices_schema()
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT cid, price, name FROM item_prices")
        rows = cur.fetchall()
        conn.close()
        return [{"cid": r[0], "price": r[1], "name": r[2] or ""} for r in rows]
    except Exception:
        return []

def _db_add_item(cid, name, price):
    """新增/覆盖一个拍品（cid + 名称 + 价格）。"""
    try:
        _db_ensure_prices_schema()
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO item_prices (cid, price, name) VALUES (?, ?, ?)",
                    (str(cid), int(price or 0), str(name or "")))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def _db_delete_item(cid):
    """删除一个拍品行，返回是否成功。"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM item_prices WHERE cid = ?", (str(cid),))
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False

def _get_missing_items():
    """增量扫描日志找缺失拍品（日志出现但未收录的物品）。
    - 只扫描尚未扫描过的对局（按 career fingerprint 记录扫描状态），已扫描的不重复扫描/统计。
    - 返回本次扫描新发现的缺失拍品；added 为用户自添加物品（不依赖日志）。
    """
    result = {"missing": [], "added": [], "log_found": False,
              "scanned_games": 0, "skipped_games": 0}
    # 用户自添加的新物品：在 db 中、但不在基础物品表（csv+v233）里
    try:
        base = bidking_parser.get_base_item_cids(CSV)
    except Exception:
        base = set()
    try:
        result["added"] = [it for it in _db_read_prices_full() if it["cid"] not in base]
    except Exception:
        result["added"] = []
    # 缺失拍品：直接读生涯数据库已落库对局（game_json 内含 missing_cids），
    # 不再重新解析日志——避免「auto」模式识别不到 UID 导致整批对局被跳过（表现为「扫描 0 场」）。
    conn = _career_db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM career_games")
    has_games = (cur.fetchone() or (0,))[0] > 0
    if not has_games:
        # 生涯库还没有对局数据，提示用户先解析
        conn.close()
        result["log_found"] = False
        return result
    result["log_found"] = True

    cur.execute("SELECT fp FROM missing_scan_log")
    scanned = set(r[0] for r in cur.fetchall())

    new_missing = {}        # cid -> {count, maps:set}
    scanned_count = 0
    skipped_count = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("SELECT fingerprint, game_json FROM career_games")
    for fp, gj in cur.fetchall():
        if fp in scanned:
            skipped_count += 1
            continue
        scanned.add(fp)
        scanned_count += 1
        cur.execute("INSERT OR IGNORE INTO missing_scan_log (fp, scanned_at) VALUES (?, ?)", (fp, now))
        try:
            rec = json.loads(gj)
        except Exception:
            continue
        for cid in (rec.get("missing_cids") or []):
            cs = str(cid)
            d = new_missing.get(cs)
            if d is None:
                d = {"count": 0, "maps": set()}
                new_missing[cs] = d
            d["count"] += 1
            if rec.get("map_id") is not None:
                d["maps"].add(rec.get("map_id"))

    conn.commit()
    conn.close()

    for cid, d in new_missing.items():
        result["missing"].append({"cid": cid, "count": d["count"],
                                  "maps": [bidking_parser.map_name(m) for m in sorted(d["maps"])]})
    # 按出现次数降序，方便优先补全高频缺失物品
    result["missing"].sort(key=lambda x: x["count"], reverse=True)
    result["scanned_games"] = scanned_count
    result["skipped_games"] = skipped_count
    return result

def _build_variable_items(prices_dict):
    """根据 bidking_parser.ITEM_COST 构建3档及以上道具列表（含价格）。"""
    from bidking_parser import ITEM_COST
    # ITEM_COST 包含所有道具 cid → 购买成本
    # 前端 VARIABLE_BATTLE_ITEMS 是硬编码的 cid+name+grade 列表
    # 我们需要返回: cid, name, grade, price(来自DB), cost(来自ITEM_COST)
    # grade 映射: 根据购买成本区间推断
    #   3档: 成本 1000~9999 (如 100102 十方窥视 cost=6000)
    #   4档: 成本 10000~99999 (如 100107 极品扫描 cost=7100... 不对)
    # 前端有硬编码的 VARIABLE_BATTLE_ITEMS，我们直接返回 price 给前端，
    # 前端自己有 name+grade 的硬编码列表
    # 实际上前端期望的格式: {prices: {cid: price}, items: [{cid, name, grade, price}]}
    # items 就用前端硬编码的，但 price 从 DB 合并
    # 所以 GET 只需返回 prices，前端自己合并
    return prices_dict

def log_candidates():
    """候选游戏日志路径（按优先级）。
    关键修复：不再写死某个具体的 Windows 用户名——云电脑/其他账号下该路径不存在，
    会导致 find_log() 直接返回 None、解析 0 局。改用当前用户目录动态拼接，
    并对 LocalLow 做兜底扫描，兼容任意用户名与部署环境。"""
    import glob as _glob
    cands = []
    # 1) exe/脚本同目录（用户常把 Player.log 放这里）
    cands.append(os.path.join(ROOT, "Player.log"))
    # 2) 当前用户实际的游戏日志目录（动态用户名，兼容云电脑）
    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    cands.append(os.path.join(profile, "AppData", "LocalLow", "laolin", "BidKing", "Player.log"))
    # 3) 父目录
    cands.append(os.path.join(ROOT, "..", "Player.log"))
    # 4) 旧机器兜底：原硬编码路径
    cands.append(GAME_LOG)
    # 5) 兜底：扫描 LocalLow 下任意 laolin/BidKing/Player.log（用户名未知时）
    local_low = os.path.join(profile, "AppData", "LocalLow")
    if os.path.isdir(local_low):
        for hit in _glob.glob(os.path.join(local_low, "*", "laolin", "BidKing", "Player.log")):
            cands.append(hit)
    # 去重保序
    seen = set(); out = []
    for p in cands:
        try:
            ap = os.path.abspath(p)
        except Exception:
            continue
        if ap not in seen:
            seen.add(ap); out.append(ap)
    return out

def find_log():
    for p in log_candidates():
        if os.path.isfile(p):
            return os.path.abspath(p)
    return None

def run_parse(uid="auto", skip_history=False):
    """后台线程：运行完整解析器，写入 result.json，并增量合并到生涯数据库。
    解析过程中逐局实时落库（每解析一局立即写入 career.db 并提交），
    即使运行中电脑突然关机，已经解析过的局数也已安全入库，不会整批丢失。
    """
    global STATE, _MISSING_SCAN_RESULT, _MISSING_SCAN_READY
    with STATE_LOCK:
        STATE["status"] = "running"
        STATE["msg"] = ""
    log = find_log()
    conn = None
    inserted_live = 0
    backup_path = None
    old_games = 0
    try:
        if not log:
            raise RuntimeError("未找到 Player.log（尝试了运行目录与游戏默认目录）")
        # 覆盖前保护：若旧 result.json 局数 > 新解析局数，先备份旧文件，供用户选择保留
        if os.path.isfile(OUT):
            try:
                with open(OUT, "r", encoding="utf-8") as _f:
                    _old = json.load(_f)
                old_games = len(_old.get("games", []))
                backup_path = _json_save_backup_path()
                os.makedirs(SNAPSHOT_DIR, exist_ok=True)
                shutil.copy2(OUT, backup_path)
            except Exception:
                old_games = 0
                backup_path = None
        conn = _career_db_connect()   # 确保数据库已建表
        def sink(rec, u):
            nonlocal inserted_live
            if _career_db_insert_one(conn, rec, u):
                inserted_live += 1
        # 显式传入 db_path（EXE/Nuitka onefile 下确保读的是 exe 同目录的 item_prices.db，P1-2）
        result = bidking_parser.parse(log, uid, CSV, OUT, verbose=False, on_game=sink,
                                      db_path=DB_PATH, skip_history=skip_history)
        new_games = len(result.get("games", [])) if isinstance(result, dict) else 0
        if old_games and new_games < old_games and backup_path and os.path.isfile(backup_path):
            # 解析后局数变少：弹出保存警告，备份保留供用户决定
            with SAVE_WARN_LOCK:
                SAVE_WARN.update(visible=True, requires_confirmation=True,
                                 old_games=old_games, new_games=new_games,
                                 deadline_ms=int((time.time() + SAVE_WARN_SECONDS) * 1000),
                                 status="", saved=False, saved_path="", local=True)
        else:
            if backup_path and os.path.isfile(backup_path):
                try:
                    os.remove(backup_path)
                except Exception:
                    pass
            with SAVE_WARN_LOCK:
                SAVE_WARN.update(visible=False, requires_confirmation=False)
        # 兜底：实时落库若完全未写入（如数据库被占用），再从 result.json 整体合并一次（按 fingerprint 去重）
        if inserted_live == 0:
            ins2, skipped = _career_db_merge_result(OUT)
        else:
            ins2, skipped = 0, 0
        inserted = inserted_live + ins2
        if inserted > 0:
            _career_db_touch_last_import()
        with STATE_LOCK:
            STATE["status"] = "done"
            STATE["msg"] = f"ok (生涯数据库新增{inserted}局,跳过{skipped}局)"
        # ---- 自动增量扫描缺失拍品，结果缓存供前端轮询 ----
        try:
            missing = _get_missing_items()
            with _MISSING_SCAN_LOCK:
                _MISSING_SCAN_RESULT = missing
                _MISSING_SCAN_READY = True
        except Exception:
            pass
    except Exception as e:
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["code"] = 1
            STATE["msg"] = str(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

def get_candidate_players():
    """扫描日志，统计出现最多的玩家 UID 作为候选。
    优化(2026-08-03)：逐行流式读取，避免一次性把整份日志(可达数十 MB)读进内存导致云电脑卡死。"""
    log = find_log()
    if not log:
        return []
    freq = {}
    name_map = {}
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r'"UserUid"\s*:\s*"(\d+)"', line)
                if m:
                    uid = m.group(1)
                    freq[uid] = freq.get(uid, 0) + 1
                m2 = re.search(r'"UserUid"\s*:\s*"(\d+)"\s*,\s*"Name"\s*:\s*"([^"]*)"', line)
                if m2:
                    name_map.setdefault(m2.group(1), m2.group(2))
    except Exception:
        return []
    top = sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return [{"uid": uid, "name": name_map.get(uid, ""), "count": cnt} for uid, cnt in top]

# ============ ngrok 管理 ============

def _ngrok_exe():
    """返回 ngrok.exe 的路径（优先内嵌 RES_DIR，其次 ROOT 目录，最后 PATH）。"""
    for base in (RES_DIR, ROOT):
        exe = os.path.join(base, "ngrok.exe")
        if os.path.isfile(exe):
            return exe
    # 尝试 PATH 中的 ngrok
    return "ngrok"

def _read_token():
    """读取保存的 ngrok authtoken。

    阶段3：优先读 DPAPI 加密文件 `.ngrok_token.enc`（绑定本机+用户），
    旧版明文 `.ngrok_token` 若存在则自动迁移为加密格式并删除明文。
    """
    enc = NGROK_TOKEN_FILE + ".enc"
    if os.path.isfile(enc) and winsec is not None:
        # 写入与读取必须用同一套容器（secure_write_json/secure_read_json），
        # 不要手拆 DPAPI 字节 —— 里面包的是 JSON 对象，不是裸 token。
        obj = winsec.secure_read_json(enc, None)
        if isinstance(obj, dict) and obj.get("t"):
            return str(obj["t"]).strip()
        # 解不开（换机/被拷走）→ 当作没存过
        return None
    # 旧明文文件：迁移
    if NGROK_TOKEN_FILE and os.path.isfile(NGROK_TOKEN_FILE):
        try:
            v = open(NGROK_TOKEN_FILE, "r").read().strip()
        except Exception:
            v = ""
        if v and winsec is not None:
            ok = winsec.secure_write_json(enc, {"t": v})
            if ok[0]:
                try:
                    os.remove(NGROK_TOKEN_FILE)
                except Exception:
                    pass
                return v
        return v or None
    return None

def _save_token(token):
    """保存 ngrok authtoken（DPAPI 加密）并配置 ngrok。"""
    token = (token or "").strip()
    if not token:
        return False, "token 不能为空"
    if winsec is not None:
        enc = NGROK_TOKEN_FILE + ".enc"
        ok, err = winsec.secure_write_json(enc, {"t": token})
        if not ok:
            return False, f"加密保存失败: {err}"
        # 删除旧明文
        try:
            if os.path.isfile(NGROK_TOKEN_FILE):
                os.remove(NGROK_TOKEN_FILE)
        except Exception:
            pass
    else:
        # 无 winsec（非 Windows）降级明文
        try:
            with open(NGROK_TOKEN_FILE, "w", encoding="utf-8") as f:
                f.write(token)
        except Exception as e:
            return False, f"写入文件失败: {e}"
    # 配置 ngrok authtoken
    exe = _ngrok_exe()
    try:
        result = subprocess.run([exe, "authtoken", token], capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")[:200]
            return False, f"ngrok 配置失败(退出码{result.returncode}): {stderr}"
        return True, None
    except FileNotFoundError:
        return False, "未找到 ngrok.exe，请确认项目目录下有 ngrok.exe"
    except subprocess.TimeoutExpired:
        return False, "ngrok authtoken 命令超时(15秒)"
    except Exception as e:
        return False, f"运行 ngrok 失败: {type(e).__name__}: {e}"

def _mask_token(token):
    """将 token 掩码显示（只显示前4和后4位）。"""
    if not token:
        return ""
    if len(token) <= 8:
        return token[:2] + "****"
    return token[:4] + "****" + token[-4:]

def ngrok_start(port):
    """启动 ngrok http 隧道（地址由 ngrok 账号自动分配：免费账号自带一个永久域名）。"""
    global NGROK_PROC
    exe = _ngrok_exe()
    # 先清孤儿（2026-08-30）：旧版本退出解析器时不带走 ngrok，会残留后台隧道；
    # 本工具是 ngrok.exe 唯一的来源，按映像名清掉再启动，避免新旧隧道并存。
    if winlaunch is not None:
        try:
            n = winlaunch.kill_images(["ngrok.exe"])
            if n:
                time.sleep(1.0)   # 等端口/连接释放
        except Exception:
            pass
    try:
        # 日志写文件而非 DEVNULL：token 失效/域名类错误时，agent 会带着明确原因退出，
        # 从日志里捞出来给用户看（ngrok_state().error）。
        lf = open(os.path.join(ROOT, ".ngrok_last.log"), "w", encoding="utf-8")
        NGROK_PROC = subprocess.Popen(
            [exe, "http", str(port), "--log=stdout", "--log-format=json"],
            stdout=lf, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        lf.close()          # 子进程已继承句柄，父进程侧即可关闭
        # 父死子随（2026-08-30）：放进 KILL_ON_JOB_CLOSE 作业对象——解析器无论以
        # 何种方式退出（控制台×/任务管理器/崩溃/正常退出），内核关闭作业句柄时
        # 都会连带终止 ngrok，不再残留后台隧道。
        if winlaunch is not None:
            winlaunch.assign_job_kill_on_close(NGROK_PROC)
        return True
    except Exception as e:
        NGROK_PROC = None
        return str(e)


def _ngrok_log_reason():
    """从最近一次 ngrok 日志里捞出退出原因（取最后一条错误），供 ngrok_state 展示。"""
    try:
        with open(os.path.join(ROOT, ".ngrok_last.log"), "r", encoding="utf-8", errors="ignore") as f:
            lines = [x for x in f.read().strip().splitlines() if x.strip()]
        for ln in reversed(lines[-10:]):
            try:
                o = json.loads(ln)
            except Exception:
                continue
            m = str(o.get("msg") or "")
            e = str(o.get("err") or "")
            if o.get("lvl") == "err" or e:
                t = (m + ("：" + e if e and e not in m else "")).strip()
                return "：" + t[:220]
    except Exception:
        pass
    return ""

def ngrok_stop():
    """停止 ngrok 进程。"""
    global NGROK_PROC
    if NGROK_PROC and NGROK_PROC.poll() is None:
        try:
            NGROK_PROC.terminate()
            NGROK_PROC.wait(timeout=5)
        except Exception:
            try:
                NGROK_PROC.kill()
            except Exception:
                pass
    NGROK_PROC = None


def _ngrok_kill_orphans():
    """清掉上次遗留的 ngrok 孤儿进程（2026-08-29，用户反馈：关了开关 ngrok 还在后台）。

    来历：直接关解析器窗口/进程被强杀时，来不及执行 ngrok_stop，ngrok.exe 是
    独立进程会继续挂在后台占着隧道。启动时清一次，让「公网开关」状态和真实
    进程保持一致。只杀「我们自己的」agent（命令行带本程序固定参数
    --log-format=json，或映射本工具端口/固定域名），不影响用户自装的 ngrok。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='ngrok.exe'\" | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except Exception:
        return
    for line in (out.stdout or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid, _, cmdline = line.partition("|")
        cl = (cmdline or "").lower()
        # 只杀本程序启动的 agent（固定参数 --log-format=json 或映射本工具端口）
        if "--log-format=json" in cl or "http 8766" in cl:
            try:
                os.kill(int(pid.strip()), signal.SIGTERM)
            except Exception:
                pass

# ngrok 公网地址缓存：Host 白名单每个请求都要用，不能每次都去查 ngrok API（有超时开销）
_NGROK_URL_CACHE = {"url": "", "ts": 0.0}
_NGROK_URL_TTL = 20.0


def _ngrok_public_url():
    """返回当前 ngrok 公网 URL；未运行/未知则返回空串。带 20 秒缓存。"""
    global NGROK_PROC
    now = time.time()
    if (_NGROK_URL_CACHE["ts"] and
            (now - _NGROK_URL_CACHE["ts"]) < _NGROK_URL_TTL):
        return _NGROK_URL_CACHE["url"]
    url = ""
    try:
        if NGROK_PROC is not None and NGROK_PROC.poll() is None:
            resp = urllib2.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2)
            data = json.loads(resp.read().decode("utf-8"))
            for t in data.get("tunnels", []):
                if t.get("public_url") and t.get("proto") in ("https", "http"):
                    url = t["public_url"]
                    if t.get("proto") == "https":
                        break
    except Exception:
        pass
    _NGROK_URL_CACHE["url"] = url
    _NGROK_URL_CACHE["ts"] = now
    return url


def ngrok_state():
    """获取 ngrok 当前状态。返回 {running, url, error, token_saved, token_masked}。"""
    global NGROK_PROC
    token = _read_token()
    result = {
        "running": False,
        "url": None,
        "error": None,
        "token_saved": bool(token),
        "token_masked": _mask_token(token) if token else None,
    }
    if NGROK_PROC is None:
        return result
    if NGROK_PROC.poll() is not None:
        # 进程已退出（常见：固定域名没在 ngrok 领取/token 失效）→ 带上日志里的原因
        NGROK_PROC = None
        result["error"] = "进程已意外退出" + _ngrok_log_reason()
        return result
    result["running"] = True
    # 查询 ngrok API 获取公网地址
    try:
        resp = urllib2.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3)
        data = json.loads(resp.read().decode("utf-8"))
        tunnels = data.get("tunnels", [])
        for t in tunnels:
            if t.get("proto") == "https" and t.get("public_url"):
                result["url"] = t["public_url"]
                break
            elif t.get("proto") == "http" and t.get("public_url"):
                result["url"] = t["public_url"]
                break
    except Exception:
        # API 可能还没就绪（ngrok 启动后需要几秒）
        pass
    return result

# ============ 自动刷新控制权锁 + 覆盖前保存警告 ============

def _control_lock_path():
    return os.path.join(ROOT, "parse.lock.json")

def _control_read():
    try:
        with open(_control_lock_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _control_write(d):
    try:
        tmp = _control_lock_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, _control_lock_path())
    except Exception:
        pass

def _control_valid(d):
    return bool(d) and (time.time() - float(d.get("ts", 0))) < CONTROL_TTL

def _control_state(client_id):
    d = _control_read()
    if not _control_valid(d):
        # 锁已过期：顺手清掉磁盘上的旧锁文件，避免前端一直读到"被占用"
        # 用「写回空对象 + 更新旧时间戳」代替 remove——沙箱环境拦截 os.remove，
        # 且写空对象让后续 _control_valid 永远返回 False，等效于"锁不生效"。
        if d:
            try:
                _control_write({"client_id": "", "ts": 0})
            except Exception:
                pass
        return {"locked": False, "owner": ""}
    return {"locked": True, "owner": d.get("client_id", "")}

def _control_acquire(client_id, force=False):
    """获取/续期控制权。被他人有效持有则返回 (state, False)；否则 (state, True)。
    force=True 时无条件接管（用于用户遇到「另一台设备持锁」时手动强制接管）。"""
    with CONTROL_LOCK:
        d = _control_read()
        if not force and _control_valid(d) and d.get("client_id") != client_id:
            return {"locked": True, "owner": d.get("client_id", "")}, False
        _control_write({"client_id": client_id, "ts": time.time()})
        return {"locked": True, "owner": client_id}, True

def _control_release(client_id):
    with CONTROL_LOCK:
        d = _control_read()
        if _control_valid(d) and d.get("client_id") == client_id:
            try:
                os.remove(_control_lock_path())
            except Exception:
                pass

# ============ 远程启动外部程序（exe 端白名单，手机只能启动配置好的程序）============

LAUNCHER_CONFIG_FILE_NAME = ".bidking_launcher.json"
LAUNCH_ALIVE_WINDOW = 180          # 启动后存活观察窗口（秒），3 分钟

# 状态：idle / launching / confirming / running / exited / error
# confirm_pending：卡密验证窗口还在（可能需要在电脑上手动点确定）
# confirm_detail：自动确认的进展描述，给前端/App 显示
LAUNCH_STATE = {"state": "idle", "pid": None, "started_at": None,
                "confirmed_at": None, "error": "", "message": "", "program_path": "",
                "confirm_pending": False, "confirm_detail": ""}
LAUNCH_STATE_LOCK = threading.Lock()
LAUNCH_PROC = None      # subprocess.Popen 对象
LAUNCH_THREAD = None    # 监控线程


# confirm_once 返回的手段名 -> 手机/网页上显示的中文
CONFIRM_METHOD_LABELS = {
    "button": "点按钮",
    "idok": "发确定命令",
    "bgkey": "后台按键",
    "enter": "抢前台敲回车",
    "tab+space": "Tab 加空格",
}


def _launcher_default_config():
    """默认配置。allowed_filename 是安全白名单，永远取这里的硬编码值。"""
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    return {
        "allowed_filename": "竞拍之王全自动估价器.exe",
        "exact_path": "",
        "search_dirs": [desktop] if os.path.isdir(desktop) else [],
        "auto_confirm": True,
        # launch_enabled 字段已废弃（2026-08-29）：不再参与判断，保留键只为兼容旧配置文件。
        # 启动管控统一走「安全与配对」的 allow_remote_launch 开关（公网需开，局域网/本机放行）。
        "launch_enabled": True,
        "exe_sha256": "",                  # P0-6：哈希锁定（首次启动时记录）
        "confirm_dialog_title": "卡密验证",
        "confirm_button_text": "确定",
        "confirm_timeout_seconds": 120,  # 弹窗处理总时长上限（两段式启动会弹多次）
        "confirm_quiet_seconds": 6,      # 连续 6 秒不再出现弹窗就认为确认完毕
    }


def _launcher_config_path():
    return os.path.join(ROOT, LAUNCHER_CONFIG_FILE_NAME)


def _launcher_load_config():
    """读取配置；不存在则生成默认文件。客户端传来的 allowed_filename 一律忽略。

    阶段3：search_dirs 支持 %USERPROFILE% 变量（分享给他人时避免泄露你的用户名）。
    读取时自动展开变量；写入时按用户选择决定是否存变量（见 _launcher_save_config）。
    """
    p = _launcher_config_path()
    base = _launcher_default_config()
    try:
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                for k in ("exact_path", "search_dirs", "auto_confirm",
                          "confirm_dialog_title", "confirm_button_text",
                          "confirm_timeout_seconds", "confirm_quiet_seconds"):
                    if k in cfg:
                        base[k] = cfg[k]
                # %USERPROFILE% 展开
                for k in ("exact_path", "search_dirs"):
                    v = base.get(k)
                    if isinstance(v, str) and "%USERPROFILE%" in v:
                        base[k] = v.replace("%USERPROFILE%", os.path.expanduser("~"))
                    elif isinstance(v, list):
                        base[k] = [x.replace("%USERPROFILE%", os.path.expanduser("~"))
                                   if isinstance(x, str) and "%USERPROFILE%" in x else x
                                   for x in v]
                return base
    except Exception:
        pass
    _launcher_save_config(base)
    return base


def _launcher_save_config(cfg):
    """写入配置文件，返回 (ok, error)。error 为具体异常文本，便于前端提示"为什么保存不了"。

    阶段3（分享防护）：写入前把本机用户名目录规范成 %USERPROFILE% 变量，
    这样配置文件分享给别人时不泄露你的用户名；读取时再展开回真实路径。
    """
    p = _launcher_config_path()
    try:
        _profile = os.path.expanduser("~")
        c2 = dict(cfg)
        # 精确路径字符串
        if isinstance(c2.get("exact_path"), str) and c2["exact_path"]:
            _v = c2["exact_path"]
            if _profile and _v.lower().startswith(_profile.lower()):
                c2["exact_path"] = "%USERPROFILE%" + _v[len(_profile):]
        # 搜索目录列表
        dirs = c2.get("search_dirs")
        if isinstance(dirs, list):
            c2["search_dirs"] = []
            for x in dirs:
                if not x or not x.strip():
                    continue
                _v = x.strip()
                if _profile and _v.lower().startswith(_profile.lower()):
                    c2["search_dirs"].append("%USERPROFILE%" + _v[len(_profile):])
                else:
                    c2["search_dirs"].append(_v)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c2, f, ensure_ascii=False, indent=1)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
        # 回读校验：确认真的写进去了（写盘失败/被占用时能立刻暴露）
        try:
            with open(p, "r", encoding="utf-8") as f:
                back = json.load(f)
            if not isinstance(back, dict):
                return False, "配置文件写入后读回的内容不是 JSON 对象"
        except Exception as e:
            return False, "写入后校验失败：%s: %s" % (type(e).__name__, e)
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def _launcher_validate_dir(d):
    """校验搜索目录是否合法（P0-6）：返回 (ok, error)。
    禁止 UNC 网络路径、可移动盘根目录、以及不在用户目录/exe 目录下的路径，
    防止攻击者把 search_dirs 指向 \\\\attacker\\share 或 U 盘来塞同名 exe。"""
    try:
        if not d or not d.strip():
            return False, "搜索目录为空"
        dd = d.strip()
        # 拒绝 UNC（\\server\share 或 //server/share）
        if dd.startswith("\\\\") or dd.startswith("//") or dd.startswith("\\\\?\\"):
            return False, "拒绝网络共享路径（UNC）：%s" % dd
        if not os.path.isdir(dd):
            return False, "搜索目录不存在：%s" % dd
        try:
            home = os.path.expanduser("~")
            app_dir = os.path.dirname(os.path.abspath(sys.argv[0] or __file__))
        except Exception:
            home, app_dir = "", ""
        abs_d = os.path.abspath(dd).lower()
        ok = False
        for base in (home, app_dir, ROOT):
            if base and (abs_d == os.path.abspath(base).lower() or
                         abs_d.startswith(os.path.abspath(base).lower() + os.sep)):
                ok = True
                break
        if not ok:
            return False, "搜索目录必须在用户目录或程序目录下：%s" % dd
        return True, ""
    except Exception as e:
        return False, "目录校验异常：%s" % e


def _launcher_check_hash(path):
    """校验可执行文件的 SHA-256 与配置中记录的一致（P0-6 哈希锁定）。
    防止攻击者放置同名 exe（内容被替换）后被远程启动。"""
    import hashlib
    try:
        cfg = _launcher_load_config()
        locked = (cfg.get("exe_sha256") or "").strip()
        if not locked:
            return True, ""   # 未锁定过（首次配置），不强制
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        if h.hexdigest().lower() != locked.lower():
            return False, "可执行文件已变更（SHA-256 不匹配），拒绝启动。请到电脑上重新确认。"
        return True, ""
    except Exception as e:
        return False, "哈希校验异常：%s" % e


def _launcher_resolve(cfg):
    """解析出真实可执行文件路径：(path, error)。
    只认 allowed_filename；精确路径失效时按 search_dirs 递归找（最多 3 层）。
    搜索目录一律过 _launcher_validate_dir（禁止 UNC/可移动盘/目录外）。"""
    allowed = _launcher_default_config()["allowed_filename"]
    ep = (cfg.get("exact_path") or "").strip()
    if ep:
        if os.path.basename(ep).lower() != allowed.lower():
            return None, ("配置的路径文件名不是 %s，已拒绝" % allowed)
        if os.path.isfile(ep):
            ok, err = _launcher_validate_dir(os.path.dirname(ep) or ep)
            if not ok:
                return None, err
            return ep, ""
    for d in (cfg.get("search_dirs") or []):
        d = (d or "").strip()
        okd, errd = _launcher_validate_dir(d)
        if not okd:
            continue
        try:
            for root, dirs, files in os.walk(d):
                if root[len(d):].count(os.sep) >= 3:
                    dirs[:] = []
                    continue
                dirs[:] = [x for x in dirs if not x.startswith(".")]
                if allowed in files:
                    return os.path.join(root, allowed), ""
        except Exception:
            continue
    return None, ("未在配置的路径/搜索目录中找到 %s" % allowed)


def _launcher_update(**kw):
    with LAUNCH_STATE_LOCK:
        LAUNCH_STATE.update(kw)


def _launcher_snapshot():
    with LAUNCH_STATE_LOCK:
        d = dict(LAUNCH_STATE)
    d["uptime_seconds"] = int(time.time() - d["started_at"]) if d.get("started_at") else 0
    return d


def _estimator_pids(workdir):
    """估价器目录下的活进程 PID 列表，**排除解析器自身**（2026-08-30）。
    背景：把解析器放进估价器同目录（或其子目录）使用时，解析器进程也在该
    目录下，旧的目录扫描会把自己误判成「估价器已在运行」→ 永远不真正启动。
    onefile 是引导+实际双进程同名，按 exe 文件名排除才能全覆盖。"""
    if winlaunch is None:
        return []
    names = set()
    for src in (sys.executable, (sys.argv[0] if sys.argv else "")):
        if src:
            names.add(os.path.basename(src))
    return winlaunch.process_pids_under_dir(workdir, exclude_names=names,
                                            exclude_pid=os.getpid())


def _launcher_find_running(workdir):
    """返回正在运行的 PID，没有则 0。

    注意：不能只看启动器进程——目标程序是「启动器 exe → 真程序 runtime exe」
    两段式，启动器拉起真程序后自己就退出了。所以按"程序所在目录"判断有没有活进程。
    """
    if winlaunch is None:
        return 0
    with LAUNCH_STATE_LOCK:
        pid = LAUNCH_STATE.get("pid")
    if pid and winlaunch.is_pid_alive(pid):
        pids = _estimator_pids(workdir)
        if pids:
            return pid
    pids = _estimator_pids(workdir)
    return pids[0] if pids else 0


def _launcher_monitor(proc, cfg, path):
    """监控线程（单循环，两件事一起做）：

    ① 存活监控：程序目录里一有活进程就**立刻**把状态置为 running，
       不再等弹窗确认跑完（旧逻辑要先跑最多 90 秒确认，导致手机端 3 分钟终判时
       状态还是 confirming，被误判成"启动失败"）。
    ② 弹窗确认：在整整 3 分钟窗口内**持续**检测并点掉"卡密验证"。
       目标程序是「启动器 exe → 真程序 runtime exe」两段式，弹窗会先后出现多次，
       只处理一轮不够，必须跟着存活监控一起跑满整个窗口。
    """
    start = time.time()
    workdir = os.path.dirname(path) or ROOT
    auto = bool(cfg.get("auto_confirm")) and winlaunch is not None
    title = cfg.get("confirm_dialog_title") or "卡密验证"
    btn_text = cfg.get("confirm_button_text") or "确定"
    quiet_need = max(2.0, float(cfg.get("confirm_quiet_seconds") or 6))
    total_timeout = max(15, int(cfg.get("confirm_timeout_seconds") or 120))

    _launcher_update(state="launching", confirm_pending=False, confirm_detail="", error="")

    def alive():
        """按"程序目录里还有没有活进程"判断（启动器会退出，真程序才是主角）。"""
        if winlaunch is not None:
            return bool(_estimator_pids(workdir))
        return proc is not None and proc.poll() is None

    def refresh_pid():
        """os.startfile 拿不到 PID，启动后按目录反查真实 PID。"""
        if winlaunch is None:
            return
        pids = _estimator_pids(workdir)
        if pids:
            _launcher_update(pid=pids[0])

    confirmed = 0
    methods = []
    quiet = 0.0
    pending = False
    last_poll = 0.0
    saw_alive = False

    while True:
        now = time.time()
        elapsed = now - start
        if not alive():
            if not saw_alive:
                _launcher_update(state="exited", confirm_pending=False,
                                 error="启动阶段未检测到运行中的进程，程序可能已退出")
            else:
                _launcher_update(state="exited", confirm_pending=False,
                                 error="程序在启动后 %d 秒退出" % int(elapsed))
            return
        saw_alive = True
        refresh_pid()
        _launcher_update(state="running", error="")

        # 弹窗确认：每秒检测一次，只在超过总时长上限后停止
        if auto and elapsed <= total_timeout and (now - last_poll) >= 1.0:
            gap = max(1.0, now - last_poll) if last_poll else 1.0
            last_poll = now
            try:
                r = winlaunch.confirm_once(title, btn_text)
            except Exception as e:
                r = {"found": False, "confirmed": False, "error": str(e)}
            if r.get("found"):
                quiet = 0.0
                if r.get("confirmed"):
                    confirmed += 1
                    m = r.get("method") or ""
                    if m and m not in methods:
                        methods.append(m)
                    pending = False
                    _launcher_update(confirmed_at=now)
                else:
                    pending = True
            else:
                quiet += gap
                if quiet >= quiet_need:
                    pending = False
            detail = ""
            if confirmed:
                detail = "已自动确认 %d 次（%s）" % (
                    confirmed,
                    "、".join(CONFIRM_METHOD_LABELS.get(m, m) for m in methods) or "无")
            if pending:
                detail = (detail + " · " if detail else "") + "卡密验证窗口仍在，持续尝试中"
            _launcher_update(confirm_pending=pending, confirm_detail=detail)

        if elapsed >= LAUNCH_ALIVE_WINDOW:
            break
        time.sleep(1)

    msg = "已持续运行 %d 秒" % int(LAUNCH_ALIVE_WINDOW)
    if confirmed:
        msg += "，已自动确认卡密 %d 次（%s）" % (confirmed, ",".join(methods) or "无")
    elif auto:
        msg += "，未检测到卡密验证窗口（可能已记住卡密）"
    if pending:
        msg += "；⚠ 卡密验证窗口仍未关闭，可能需要在电脑上手动点「确定」"
    _launcher_update(state="running", message=msg)


def _launcher_do_confirm(cfg):
    """阻塞式弹窗确认（用于"程序已在运行，顺手点掉卡住的弹窗"这条路径），返回提示文案。"""
    if not (cfg.get("auto_confirm") and winlaunch is not None):
        return "已跳过弹窗自动确认（auto_confirm=false）"
    r = winlaunch.confirm_loop(
        cfg.get("confirm_dialog_title") or "卡密验证",
        cfg.get("confirm_button_text") or "确定",
        int(cfg.get("confirm_timeout_seconds") or 120),
        int(cfg.get("confirm_quiet_seconds") or 6),
    )
    if r.get("confirmed"):
        _launcher_update(confirmed_at=time.time())
        return "已自动确认 %d 次（%s，用时 %ss）" % (r["confirmed"], r["method"], r["seconds"])
    if r.get("found"):
        return "出现卡密验证窗口但没能自动确认，请手动点击确定"
    return "未检测到卡密验证窗口（可能已记住卡密）"


def _launcher_confirm_only(cfg, path, pid):
    """程序已在运行时：只做弹窗确认 + 置顶，不再启动新实例。"""
    workdir = os.path.dirname(path) or ROOT
    msg = _launcher_do_confirm(cfg)
    alive = bool(_estimator_pids(workdir)) if winlaunch else False
    if alive:
        hwnd = winlaunch.find_main_window_by_pid(pid) if winlaunch else 0
        if hwnd:
            winlaunch.bring_to_front(hwnd)
        _launcher_update(state="running", confirmed_at=time.time(),
                         message="程序已在运行并已置顶；" + msg)
    else:
        _launcher_update(state="exited", error="程序已不在运行；" + msg)


def launcher_start():
    """启动白名单里的程序。手机不能传路径，全部由 exe 端配置决定。"""
    global LAUNCH_PROC, LAUNCH_THREAD
    if winlaunch is None:
        _launcher_update(state="error", error="当前环境不支持窗口操作（需要 Windows）")
        return _launcher_snapshot()
    cfg = _launcher_load_config()
    # 启动管控统一收口（2026-08-29）：原 P0-6 的 launch_enabled 配置标志已移除——
    # 它默认关、页面没有开启它的界面、配置接口也不接受该字段，等于把启动功能
    # 永久锁死（本机点也会报"远程启动已关闭"）。现在唯一的开关是「安全与配对」
    # 里的 allow_remote_launch（在 do_POST 的 _scope_allows 里检查）：
    # 本机/局域网放行（局域网信任策略），公网(ngrok)手机需在电脑端打开开关。
    # 白名单文件名 / 路径校验（拒 UNC/可移动盘根）/ SHA-256 哈希锁定等 P0-6
    # 真正的防线全部保留，不受影响。
    allowed = cfg["allowed_filename"]
    path, err = _launcher_resolve(cfg)
    if not path:
        _launcher_update(state="error", error=err or "未找到程序", program_path="")
        return _launcher_snapshot()
    # P0-6：哈希锁定——首次允许时记录 SHA-256，之后每次启动前校验，防止"同名 exe 被替换"
    okh, errh = _launcher_check_hash(path)
    if not okh:
        _launcher_update(state="error", error=errh or "哈希校验失败", program_path=path)
        return _launcher_snapshot()
    if not (cfg.get("exe_sha256") or "").strip():
        import hashlib
        _h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for _ch in iter(lambda: f.read(1 << 16), b""):
                    _h.update(_ch)
            cfg["exe_sha256"] = _h.hexdigest()
            _launcher_save_config(cfg)
        except Exception:
            pass
    workdir = os.path.dirname(path)
    # 已在运行 → 只置顶 + 顺手点掉可能卡住的卡密弹窗，不重复启动
    pid = _launcher_find_running(workdir)
    if pid:
        hwnd = winlaunch.find_main_window_by_pid(pid)
        if hwnd:
            winlaunch.bring_to_front(hwnd)
        _launcher_update(state="confirming", pid=pid, program_path=path, error="",
                         started_at=time.time(),
                         message="程序已在运行，正在检查弹窗")
        threading.Thread(target=_launcher_confirm_only, args=(cfg, path, pid), daemon=True).start()
        return _launcher_snapshot()
    _launcher_update(state="launching", pid=None, started_at=time.time(),
                     confirmed_at=None, error="", message="正在启动", program_path=path)
    # ⚠️ 关键：Windows 上必须用 os.startfile（等价于资源管理器里双击）。
    # 用 subprocess.Popen 启动会继承本程序的控制台，目标程序随后会收到
    # CTRL_CLOSE_EVENT（console_ctrl_2）而自动退出（实测启动 60 秒后自杀）。
    proc = None
    try:
        if sys.platform == "win32":
            os.startfile(path)
        else:
            proc = subprocess.Popen([path], cwd=os.path.dirname(path))
    except Exception as e:
        _launcher_update(state="error", error="启动失败：%s" % e)
        return _launcher_snapshot()
    LAUNCH_PROC = proc
    # startfile 拿不到 PID，稍后按"程序目录"反查
    if proc is not None:
        _launcher_update(pid=proc.pid)
    LAUNCH_THREAD = threading.Thread(target=_launcher_monitor, args=(proc, cfg, path), daemon=True)
    LAUNCH_THREAD.start()
    return _launcher_snapshot()


def _json_save_backup_path():
    # 中间态备份也放进 历史快照/（2026-09-04），根目录不再出现 result.json.warn-bak
    return os.path.join(SNAPSHOT_DIR, "result.json.warn-bak")

def _json_save_do_save():
    """将备份的旧 result.json 另存为带时间戳文件，标记已保存。"""
    global SAVE_WARN
    bp = _json_save_backup_path()
    if not os.path.isfile(bp):
        return False
    ts = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(SNAPSHOT_DIR, "result-" + ts + ".json")
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        shutil.move(bp, dst)
        with SAVE_WARN_LOCK:
            SAVE_WARN.update(visible=True, requires_confirmation=False,
                             status="saved", saved=True, saved_path=dst)
        return True
    except Exception:
        return False

# ---- 历史快照列表/读取（2026-09-04：「选择 JSON」改造，供网页续看上次战绩）----
_SNAPSHOT_NAME_RE = re.compile(r"^result-\d{8}-\d{6}\.json$")

def _snapshots_list():
    """列出 历史快照/ 下的战绩快照（新→旧）。名字必须严格匹配快照命名，其他文件忽略。"""
    out = []
    try:
        names = os.listdir(SNAPSHOT_DIR)
    except Exception:
        return out
    for n in names:
        if not _SNAPSHOT_NAME_RE.match(n):
            continue
        try:
            with open(os.path.join(SNAPSHOT_DIR, n), "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        s = d.get("summary") or {}
        out.append({
            "name": n,
            "generated_at": (d.get("meta") or {}).get("generated_at", ""),
            "games": len(d.get("games", [])),
            "wins": s.get("wins", 0),
            "profit": s.get("profit", 0),
        })
    out.sort(key=lambda x: x["name"], reverse=True)
    return out

def _snapshot_read(name):
    """按名读单个快照；名字必须严格匹配 result-YYYYMMDD-HHMMSS.json（防目录穿越）。"""
    if not _SNAPSHOT_NAME_RE.match(str(name or "")):
        return None
    fp = os.path.join(SNAPSHOT_DIR, name)
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _json_save_do_discard():
    global SAVE_WARN
    bp = _json_save_backup_path()
    if os.path.isfile(bp):
        try:
            os.remove(bp)
        except Exception:
            pass
    with SAVE_WARN_LOCK:
        SAVE_WARN.update(visible=True, requires_confirmation=False,
                         status="discarded", saved=False, saved_path="")

def _json_save_ack():
    global SAVE_WARN
    with SAVE_WARN_LOCK:
        SAVE_WARN.update(visible=False, requires_confirmation=False)

def _json_save_warning_state():
    with SAVE_WARN_LOCK:
        st = dict(SAVE_WARN)
    # 待确认且已超时 → 默认保存旧 JSON（与前端"无应答默认保存"文案一致）
    if st.get("requires_confirmation") and st.get("deadline_ms") and time.time() * 1000 >= st["deadline_ms"]:
        _json_save_do_save()
        with SAVE_WARN_LOCK:
            st = dict(SAVE_WARN)
    return st

# ============ HTTP Handler ============

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        import sys
        print(f"[serve] {format % args}", file=sys.stderr, flush=True)

    def _safe_write(self, buf):
        # 客户端提前断开（关页面/切换标签）时，写响应会抛 ConnectionAbortedError
        # / BrokenPipeError。这是无害噪音（2026-08-31 启动日志里那个异常就是它），
        # 静默忽略即可，不再刷 stderr。
        try:
            self.wfile.write(buf)
            self.wfile.flush()
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        # 大响应 gzip 压缩（2026-08-30）：生涯全量 JSON 约 60MB，经 ngrok 未压缩
        # 传输要几分钟（手机端"生涯没数据/很慢"的元凶）。JSON 压缩比约 10:1。
        # 仅在客户端声明支持 gzip 时启用（浏览器/HttpURLConnection 自动携带并透明解压）。
        if len(data) > 65536 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            import gzip as _gzip
            data = _gzip.compress(data, 5)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._safe_write(data)
            return
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # 安全：不发送 Access-Control-Allow-Origin。
        # 带此头时任意网站可跨域读取本机接口的响应（曾可跨域读出明文密码）。
        # App 的页面与接口同源、原生网络请求不走 CORS，删除不影响功能。
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._safe_write(data)

    # 静态资源访问控制：改为**白名单**制（默认拒绝）。
    # 原黑名单漏了 .exe/.apk/.zip/.md，导致 BidKing解析器.exe(17MB)、
    # 内嵌的 ngrok.exe(32MB)、apk、备份 result-*.json 都能被匿名下载。
    _DENY_FILES = {"ngrok_token", "parse.lock.json"}
    _ALLOW_EXTS = {".html", ".htm", ".js", ".css", ".csv", ".json",
                   ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".woff2"}
    # 白名单之外但确实需要提供的固定文件名（数据文件，战绩数据按约定不算泄露）
    _ALLOW_NAMES = {"result.json", "silver_cache.json", "v233_items.json"}
    # 额外显式拒绝（即便扩展名在白名单里）：可执行/归档/数据库/源码/备份
    _FORBID_EXTS = {".exe", ".apk", ".zip", ".dll", ".msi", ".bat", ".cmd", ".ps1",
                    ".db", ".sqlite", ".sqlite3", ".py", ".log", ".md", ".key", ".pem"}

    def _send_file(self):
        path = urllib.parse.urlparse(self.path).path
        path = urllib.parse.unquote(path)   # 还原中文/特殊字符路径（修复中文路径 404）
        if path == "/" or path == "":
            path = "/bidking_report.html"
        # 子目录一律拒绝（2026-09-04）：历史快照/ 里的 result-*.json 是私人战绩，
        # 快照请走 /api/snapshot（带鉴权）。静态服务只服务根目录一层文件，
        # 防止快照目录被当普通 .json 白名单成员匿名下载（旧漏洞复发）。
        if "/" in path.lstrip("/"):
            self.send_error(403); return
        # 静态资源优先从内嵌 RES_DIR 读取，数据文件回退到 ROOT
        fp = None
        for _base in (RES_DIR, ROOT):
            _c = os.path.normpath(os.path.join(_base, path.lstrip("/")))
            if os.path.isfile(_c):
                fp = _c; break
        if fp is None:
            self.send_error(404); return
        # 安全检查：确保文件在 RES_DIR 或 ROOT 内（防目录穿越）
        _abs = os.path.abspath(fp)
        if not any(_abs == os.path.abspath(b) or _abs.startswith(os.path.abspath(b) + os.sep) for b in (RES_DIR, ROOT)):
            self.send_error(403); return
        # 安全拦截（白名单制）：
        #  1) 点开头的隐藏文件一律拒绝（.ngrok_token / .bidking_pass / .bidking_sec.bin 等）
        #  2) 显式拒绝可执行文件、归档、数据库、源码、备份
        #  3) 其余必须命中"允许的扩展名"或"允许的文件名"
        base = os.path.basename(fp)
        ext = os.path.splitext(fp)[1].lower()
        base_l = base.lower()
        if base.startswith("."):
            self.send_error(403, "Forbidden: hidden file"); return
        if base in self._DENY_FILES or ext in self._FORBID_EXTS:
            self.send_error(403, "Forbidden: sensitive file"); return
        if not (ext in self._ALLOW_EXTS or base_l in self._ALLOW_NAMES):
            self.send_error(403, "Forbidden: file type not allowed"); return
        if os.path.isfile(fp):
            st = os.stat(fp)
            etag = '"%x-%x"' % (int(st.st_mtime), st.st_size)
            # 条件请求（2026-08-30）：If-None-Match 命中 → 304 空响应。
            # result.json / silver_cache.json 在两次解析之间不变，手机经 ngrok
            # 重开页面时 304 只回几百字节，不必再拖全量文件。
            # no-cache = 每次都回服务器校验（304 极快），不是不缓存。
            if (self.headers.get("If-None-Match") or "").strip() == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self.send_response(200)
            ext = os.path.splitext(fp)[1].lower()
            ct = {".html":"text/html; charset=utf-8",".js":"application/javascript; charset=utf-8",
                  ".json":"application/json; charset=utf-8",".css":"text/css; charset=utf-8",
                  ".csv":"text/csv; charset=utf-8"}.get(ext, "application/octet-stream")
            self.send_header("Content-Type", ct)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            with open(fp, "rb") as f:
                data = f.read()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._safe_write(data)
        else:
            self.send_error(404, "Not found: " + path)

    def do_GET(self):
        global _MISSING_SCAN_READY, _MISSING_SCAN_RESULT
        # P0-3：Host 白名单，防 DNS-rebinding（放在最前面，先于任何业务逻辑）
        if not _host_ok(self):
            self._send_json({"error": "bad_host",
                             "msg": "请求的 Host 不被信任"}, code=403)
            return
        p = urllib.parse.urlparse(self.path).path
        p = re.sub(r"/{2,}", "/", p)   # 客户端拼出来的 //api/xxx 归一，免得落错路由返回 unauthorized
        # P0-1：GET 读接口统一鉴权。
        # 放行的白名单：页面本身(/)与静态资源、ping、server-info(降敏版)、
        # 配对相关(auth/challenge、pair/*)——这些是"未认证也要能连上"的入口。
        # 例外：result.json / silver_cache.json 是战绩数据，对**公网**访客同样要求令牌
        # （2026-08-29 用户要求：地址泄露也不能随便看数据，须电脑端同意）。
        _sensitive_static = p in ("/result.json", "/silver_cache.json")
        _get_pass = not (p == "/" or p == "/api/ping"
                         or p == "/api/server-info"
                         or p == "/api/auth/challenge"
                         or p.startswith("/api/pair/")
                         or (not p.startswith("/api/") and not _sensitive_static))
        # 本机回环放行，与 POST 的 _auth_required() 保持一致（2026-08-29 修复）。
        # 原实现漏了这一条：本人在自己电脑上打开页面，所有读接口一律 401，
        # 页面拿不到数据 → 表现为「点了按钮没反应」。
        # 2026-08-29 用户决策：局域网同样信任——未设密码免鉴权；设了密码凭
        # /api/auth 换的令牌即可。严苛防护只针对公网（ngrok）。
        # 注意：_peer_class 是顶层函数，不能写成 self._peer_class()
        try:
            _peer = _peer_class(self)
        except Exception:
            _peer = "public"
        _open_peer = _peer == "local" or (_peer == "lan" and not _pass_enabled())
        _need_auth = _get_pass or (_sensitive_static and _peer == "public")
        if _need_auth and not _open_peer and not _token_check(_request_token(self)):
            self._send_json({"error": "unauthorized",
                             "msg": "需要访问密码或会话令牌"}, code=401)
            return
        if p == "/api/status":
            with STATE_LOCK:
                self._send_json(dict(STATE))
        elif p == "/api/server-info":
            # 2026-08-29：恢复返回 lan_url（安全加固时被误删，导致 exe 页面顶部
            # 「另一台电脑访问」链接消失、用户给 App 填地址没了依据）。
            # 只对 本机/局域网 来源返回，公网（ngrok）不泄露内网拓扑。
            # 阶段2：额外返回版本协商字段。老 App 不认这些字段会直接忽略，
            # 继续走密码登录的 legacy 只读分支 —— 不会因为升级 exe 就连不上。
            _obj = _sec_load()
            _sw = _obj.get("switches") or {}
            _pc = _peer_class(self)
            _info = {"version": "2.34-fixed", "refresh": True,
                     "port": NGROK_PORT,
                     "pass_enabled": _pass_enabled(),
                     "api_version": API_VERSION,
                     "min_app": MIN_APP_VERSION,
                     "app_version": APP_VERSION,
                     "app_signature": APP_SIGNATURE,
                     "peer": _pc,
                     "pairing_required": bool(_sw.get("pairing_required")),
                     # App 据此判断要不要走配对：局域网/本机不需要，公网按开关
                     "pairing_needed": bool(_sw.get("pairing_required")) if _pc == "public" else False,
                     "server_id": _obj.get("server_id") or ""}
            if _pc in ("local", "lan"):
                _lip = _detect_lan_ip()
                if _lip:
                    _info["lan_url"] = "http://%s:%d" % (_lip, NGROK_PORT)
            self._send_json(_info)
        elif p == "/api/auth/challenge":
            # 阶段2：设备登录的挑战值。手机拿 nonce 和设备密钥算出签名再换令牌，
            # 密码全程不上网；nonce 一次性 + 2 分钟过期，被抓包也重放不了。
            _qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            _did = (_qs.get("device_id") or [""])[0].strip()
            if not _did:
                self._send_json({"error": "device_id 不能为空"}, code=400)
                return
            self._send_json({"nonce": _nonce_issue(_did),
                             "server_id": _sec_load().get("server_id") or "",
                             "ts": int(time.time())})
        elif p == "/api/pair/status":
            # 手机轮询配对结果。已批准时**只在这一次**返回设备密钥，领走即作废。
            _qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            _rid = (_qs.get("rid") or [""])[0].strip()
            _did = (_qs.get("did") or (_qs.get("device_id") or [""]))[0].strip()
            self._send_json(_pair_status(_rid, _did))
        elif p == "/api/pair/notice":
            # 阶段3：换机/拷贝后的安全重置提示（仅本机，读后即删）
            if not _is_local_peer(self):
                self._send_json({"error": "local_only"}, code=403)
                return
            _warn = _read_security_warning()
            self._send_json({"notice": _warn})
        elif p in ("/api/pair/pending", "/api/pair/devices"):
            # 配对列表属于安全敏感信息（能看到谁在试图连你），仅本机可见
            if not _is_local_peer(self):
                self._send_json({"error": "local_only",
                                 "msg": "配对列表只能在本机电脑上查看"}, code=403)
                return
            _obj = _pair_sweep()
            if p == "/api/pair/devices":
                _devs = []
                for d in _obj.get("devices") or []:
                    _devs.append({"id": d.get("id"), "name": d.get("name"),
                                  "model": d.get("model"), "last_ip": d.get("last_ip"),
                                  "paired_at": d.get("paired_at"),
                                  "last_seen": d.get("last_seen")})
                self._send_json({"devices": _devs,
                                 "switches": _obj.get("switches") or {}})
            else:
                _ps = []
                for r in _obj.get("pending") or []:
                    _ps.append({"rid": r.get("rid"), "name": r.get("name"),
                                "model": r.get("model"), "ip": r.get("ip"),
                                "ts": r.get("ts"), "exp": r.get("exp")})
                self._send_json({"pending": _ps})
        elif p == "/api/pass-info":
            # P0-5：不再返回密码明文。
            # 原实现对本机返回明文，而响应曾带 Access-Control-Allow-Origin: * ，
            # 导致任意网站都能跨域读出你的密码（该密码很可能与其他账号复用）。
            # 现在只返回"是否启用 + 提示"，忘记密码请走本机重设流程。
            # 本机判定必须走 _peer_class（2026-08-30 修）：ngrok 转发流量的 TCP 对端
            # 是 127.0.0.1，裸看 client_address 会把公网访客误判成本机（真实漏洞）。
            if _peer_class(self) == "local":
                self._send_json({"pass_enabled": _pass_enabled(),
                                 "hint": _pass_hint()})
            else:
                self._send_json({"error": "forbidden"}, code=403)
        elif p == "/api/players":
            self._send_json({"players": get_candidate_players()})
        elif p == "/api/career-db-status":
            self._send_json(_career_db_status())
        elif p == "/api/career-data":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            uid = (qs.get("uid") or [""])[0].strip()
            # since_ts（2026-08-30）：只返回 ts >= since_ts 的对局，客户端增量合并
            _since_raw = (qs.get("since_ts") or [""])[0].strip()
            since_ts = int(_since_raw) if _since_raw.isdigit() else None
            games = _career_db_get_games(uid if uid and uid != "all" else None,
                                         since_ts=since_ts)
            status = _career_db_status()
            uids = _career_db_list_uids()
            self._send_json({"games": games, "db_status": status, "json_sources": [],
                             "uids": uids,
                             "current_uid": uid or (uids[0]["uid"] if uids else "")})
        elif p == "/api/item-prices":
            prices = _db_read_prices()
            self._send_json({"prices": prices})
        elif p == "/api/missing-items":
            self._send_json(_get_missing_items())
        elif p == "/api/snapshots":
            # 历史快照列表（2026-09-04）：走 /api/ 自动继承令牌鉴权，不走静态文件
            self._send_json({"ok": True, "dir_name": os.path.basename(SNAPSHOT_DIR),
                             "snapshots": _snapshots_list()})
        elif p == "/api/snapshot":
            qs2 = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            _snap = _snapshot_read((qs2.get("name") or [""])[0])
            if _snap is None:
                self._send_json({"ok": False, "error": "快照不存在或名称非法"}, code=404)
            else:
                self._send_json({"ok": True, "snapshot": _snap})
        elif p == "/api/missing-items/auto":            # 返回自动扫描缓存（如果 ready），清除 ready 标志
            try:
                with _MISSING_SCAN_LOCK:
                    ready = _MISSING_SCAN_READY
                    result = _MISSING_SCAN_RESULT
                    if ready:
                        _MISSING_SCAN_READY = False  # 消费后清除标志，避免重复读取
                if ready and result:
                    self._send_json({"ready": True, **result})
                elif ready and not result:
                    # ready=True 但缓存为空——说明已扫描过但没有新缺失
                    self._send_json({"ready": True, "missing": [], "added": [],
                                     "log_found": True, "scanned_games": 0, "skipped_games": 0})
                else:
                    self._send_json({"ready": False})
            except Exception as e:
                import traceback
                self._send_json({"ready": False, "error": str(e), "tb": traceback.format_exc()})
        elif p == "/api/ping":
            self._send_json({"ok": True})
        elif p == "/api/ngrok/state":
            self._send_json(ngrok_state())
        elif p.startswith("/api/ngrok"):
            self._send_json(ngrok_state())
        elif p == "/api/json-save-warning":
            self._send_json(_json_save_warning_state())
        elif p == "/api/control":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            cid = (qs.get("client_id") or [""])[0]
            self._send_json(_control_state(cid))
        elif p == "/api/launch/status":
            self._send_json(_launcher_snapshot())
        elif p == "/api/launch/config":
            cfg = _launcher_load_config()
            path, err = _launcher_resolve(cfg)
            self._send_json({"ok": True, "config": cfg, "resolved_path": path or "",
                             "resolved": bool(path), "error": err or "",
                             "config_path": _launcher_config_path()})
        elif p.startswith("/api/"):
            self._send_json({"error": "unknown_endpoint", "path": p}, code=404)
        else:
            self._send_file()

    def do_POST(self):
        # P0-3：Host 白名单，防 DNS-rebinding（放在最前面，先于读取 body）
        if not _host_ok(self):
            self._send_json({"error": "bad_host",
                             "msg": "请求的 Host 不被信任"}, code=403)
            return
        p = urllib.parse.urlparse(self.path).path
        p = re.sub(r"/{2,}", "/", p)   # 客户端拼出来的 //api/xxx 归一，免得落错路由返回 unauthorized
        # P1-5：body 大小限制，防止超大请求打爆内存（career-import 例外，单独限 50MB）
        _max_body = 50 * 1024 * 1024 if p == "/api/career-import" else 5 * 1024 * 1024
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except Exception:
            length = 0
        if length > _max_body:
            self._send_json({"error": "payload_too_large",
                             "msg": "请求体过大"}, code=413)
            return
        # P0-4：Content-Type 必须是 application/json（防跨域"简单请求"CSRF）
        if not _content_type_ok(self, p):
            self._send_json({"error": "unsupported_content_type",
                             "msg": "请求必须使用 Content-Type: application/json"}, code=415)
            return
        body = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            data = {}
        # 统一鉴权：/api/auth 与 /api/pair/request 是"还没拿到凭据"的入口，不校验令牌，
        # 但二者都必须在内部校验密码；配对请求之后还要电脑端人工批准才算数（阶段2）。
        if p not in ("/api/auth", "/api/pair/request") and not _auth_required(self, data):
            self._send_json({"error": "unauthorized", "msg": "需要访问密码或会话令牌"}, code=401)
            return
        # 阶段2：按令牌身份限制写接口（本机全权 / 配对设备受限 / 旧版只读）
        if p not in ("/api/auth", "/api/pair/request") and not _scope_allows(self, p):
            _sc = _token_scope(_request_token(self))
            _msg = ("手机已配对，但此操作只能在电脑上做" if _sc == "device"
                    else "旧版 App 处于只读模式，请在电脑端配对后使用")
            if p == "/api/launch":
                _msg = "手机远程启动已关闭，请在电脑端报表页的「安全与配对」里打开"
            self._send_json({"error": "forbidden", "msg": _msg}, code=403)
            return
        try:
            if p == "/api/auth":
                # 两种登录方式（阶段2）：
                #   A) 设备登录：{device_id, nonce, ts, sig} —— HMAC 挑战-响应，
                #      密码根本不上网，即使被抓包也重放不了（nonce 一次性）。
                #   B) 密码登录：{pwd} —— 旧版 App 兼容路径，远程时降级为只读。
                _did = str(data.get("device_id") or "").strip()
                if _did:
                    _ok, _err = _verify_device_sig(_did, str(data.get("nonce") or ""),
                                                   data.get("ts"), str(data.get("sig") or ""))
                    if not _ok:
                        self._send_json({"ok": False, "error": _err or "设备校验失败"}, code=401)
                        return
                    _scope = "device"
                else:
                    pwd = str(data.get("pwd") or "")
                    _ok_pwd, _lock_wait = _pass_verify_public(self, pwd)
                    if not _ok_pwd:
                        if _lock_wait > 0:
                            # 公网密码连续错误触发临时锁定（2026-08-30）
                            self._send_json({"ok": False, "error": "尝试过于频繁",
                                             "msg": "密码连续错误已临时锁定，约 %d 分钟后可再试"
                                                    % max(1, int(_lock_wait / 60 + 0.99))}, code=429)
                        else:
                            self._send_json({"ok": False, "error": "密码错误"}, code=401)
                        return
                    _pc = _peer_class(self)
                    if _pc in ("local", "lan"):
                        # 2026-08-29 用户决策：局域网视同本机——密码对上即全权，
                        # 不再降级只读、不再强制配对。严苛防护只针对公网（ngrok）。
                        _scope = "local-ui"
                    else:
                        # 公网且没走设备登录：
                        # 2026-08-29（用户要求）：「新设备必须配对」开关现在真正生效——
                        # 开（默认）= 公网只凭密码不能看数据，必须电脑端点「同意」；
                        # App 与网页浏览器一视同仁，拿到 403 后各自走配对流程
                        # （error 里带「尚未配对」供 App 识别转配对）。
                        _sw = (_sec_load().get("switches") or {})
                        if _sw.get("pairing_required"):
                            self._send_json({"ok": False,
                                             "error": "该设备尚未配对，公网访问需要在电脑端「安全与配对」点同意",
                                             "code": "pairing_required"}, code=403)
                            return
                        if not _sw.get("allow_legacy_readonly"):
                            self._send_json({"ok": False,
                                             "error": "该设备尚未配对，请在电脑端批准后重试"},
                                            code=403)
                            return
                        _scope = "legacy-readonly"
                try:
                    _ip = (self.client_address or ("", 0))[0]
                except Exception:
                    _ip = ""
                token = _token_issue(scope=_scope, device_id=_did, ip=_ip)
                self._send_json({"ok": True, "token": token, "scope": _scope,
                                 "pass_enabled": _pass_enabled()})
            elif p == "/api/pass-set":
                # 安全（P0-4）：**仅允许本机**设置/清除访问密码。
                # 原实现在"未设密码"时对任何来源放行，导致局域网内任何人都能
                # 设一个密码把原用户彻底锁死，或把已有密码清除掉。
                if not _is_local_peer(self):
                    self._send_json({"ok": False, "error": "local_only",
                                     "msg": "设置访问密码只能在本机电脑上操作"}, code=403)
                    return
                pwd = str(data.get("pwd") or "").strip()
                ok = _pass_set(pwd)
                if ok:
                    # 改密码立即作废所有旧令牌（修掉长期遗留问题）
                    with _AUTH_TOKENS_LOCK:
                        _AUTH_TOKENS.clear()
                self._send_json({"ok": ok, "pass_enabled": _pass_enabled()})

            # ---- 阶段2：设备配对（写操作，除 request 外一律仅限本机）----
            elif p == "/api/pair/request":
                # 手机发起配对。必须知道密码（第一关），然后排队等电脑端人工同意（第二关）。
                try:
                    _ip = (self.client_address or ("", 0))[0]
                except Exception:
                    _ip = ""
                _pdid = str(data.get("device_id") or "").strip()
                # 已配对设备凭正确密码直接重发密钥（2026-08-30）：
                # 此前返回 400「该设备已配对」，但 App 侧密钥按服务器地址 hash 存储，
                # 地址一变（如 ngrok 域名变化）密钥就找不到 → 想重新配对又被拒 → 死锁，
                # 只能去电脑端手动撤销。改为：人工同意这道关对该 device_id 已经过过一次，
                # 凭密码即可自动重新绑定（同一设备只需要同意一次）。
                if _pdid and _device_find(_pdid):
                    if _pass_enabled():
                        _ok_pwd, _lock_wait = _pass_verify_public(self, data.get("pwd") or "")
                        if not _ok_pwd:
                            if _lock_wait > 0:
                                self._send_json({"ok": False, "error": "尝试过于频繁",
                                                 "msg": "密码连续错误已临时锁定，约 %d 分钟后可再试"
                                                        % max(1, int(_lock_wait / 60 + 0.99))}, code=429)
                            else:
                                self._send_json({"ok": False, "error": "密码错误"}, code=400)
                            return
                    _sec_obj = _sec_load()
                    for _dev in _sec_obj.get("devices") or []:
                        if _dev.get("id") == _pdid:
                            if data.get("name"):
                                _dev["name"] = str(data.get("name"))[:40]
                            if data.get("model"):
                                _dev["model"] = str(data.get("model"))[:40]
                            _dev["last_seen"] = time.time()
                            if _ip:
                                _dev["last_ip"] = _ip
                            break
                    _sec_save(_sec_obj)
                    self._send_json({"ok": True, "auto_paired": True,
                                     "device_key": _device_key(_pdid),
                                     "server_id": _sec_obj.get("server_id") or "",
                                     "token": _token_issue(scope="device", device_id=_pdid, ip=_ip),
                                     "msg": "该设备此前已同意过配对，凭密码直接重新绑定"})
                    return
                if _pass_enabled():
                    # 新设备排队路径同样过试错锁定闸（2026-08-30）：否则攻击者
                    # 可换新 device_id 绕开 already-paired 分支无限试密码
                    _ok_pwd, _lock_wait = _pass_verify_public(self, data.get("pwd") or "")
                    if not _ok_pwd:
                        if _lock_wait > 0:
                            self._send_json({"ok": False, "error": "尝试过于频繁",
                                             "msg": "密码连续错误已临时锁定，约 %d 分钟后可再试"
                                                    % max(1, int(_lock_wait / 60 + 0.99))}, code=429)
                        else:
                            self._send_json({"ok": False, "error": "密码错误"}, code=400)
                        return
                rid, err = _pair_request(_pdid, data.get("name"), data.get("model"),
                                         data.get("pwd"), _ip)
                if err:
                    self._send_json({"ok": False, "error": err}, code=400)
                    return
                self._send_json({"ok": True, "rid": rid,
                                 "msg": "已提交，请在电脑端报表页点「同意」"})
            elif p in ("/api/pair/approve", "/api/pair/reject", "/api/pair/revoke",
                       "/api/pair/switches"):
                # 这四个是"改安全状态"的操作，只允许在电脑本机点。
                # 哪怕密码泄露，攻击者也只能在电脑上看见，不能远程批准自己。
                if not _is_local_peer(self):
                    self._send_json({"ok": False, "error": "local_only",
                                     "msg": "配对管理只能在本机电脑上操作"}, code=403)
                    return
                if p == "/api/pair/approve":
                    did, err = _pair_approve(str(data.get("rid") or "").strip())
                    if err:
                        self._send_json({"ok": False, "error": err}, code=400)
                        return
                    self._send_json({"ok": True, "device_id": did})
                elif p == "/api/pair/reject":
                    ok = _pair_reject(str(data.get("rid") or "").strip())
                    self._send_json({"ok": bool(ok)})
                elif p == "/api/pair/revoke":
                    did = str(data.get("device_id") or "").strip()
                    ok = _pair_revoke(did)
                    self._send_json({"ok": bool(ok),
                                     "msg": "已撤销该设备，它下次需要重新配对" if ok
                                            else "未找到该设备"})
                else:
                    sw = data.get("switches") or {}
                    obj = _sec_load()
                    for k in ("pairing_required", "allow_legacy_readonly",
                              "allow_remote_launch", "ngrok_need_pair"):
                        if k in sw:
                            obj["switches"][k] = bool(sw[k])
                    _sec_save(obj)
                    self._send_json({"ok": True, "switches": obj["switches"]})

            elif p == "/api/lowstock":
                # APK/手机轮询：当前低库存道具（面板内：竞拍使用过 且 ≥3档 且 ≤10个）
                try:
                    with open(OUT, "r", encoding="utf-8") as f:
                        rj = json.load(f)
                except Exception:
                    rj = {}
                items = _low_stock_list(rj)
                self._send_json({"items": items, "count": len(items),
                                 "ts": time.time()})
            elif p == "/api/exit":
                # 安全（P0-7）：远程退出能力已移除。
                # 任何能发请求的人都可让程序退出，属无收益的破坏性接口；
                # 使用说明早已声明该功能不存在，此处保持接口名但拒绝执行。
                self._send_json({"ok": False, "error": "remote_exit_disabled",
                                 "msg": "远程退出已停用，请在电脑上直接关闭程序"}, code=403)
            elif p == "/api/refresh":
                uid = str(data.get("uid") or "auto")
                global _PARSE_THREAD
                if _PARSE_THREAD and _PARSE_THREAD.is_alive():
                    with STATE_LOCK:
                        self._send_json({"status": STATE["status"], "msg": "已在解析中"})
                else:
                    _PARSE_THREAD = threading.Thread(target=run_parse, args=(uid, True), daemon=True)
                    _PARSE_THREAD.start()
                    self._send_json({"status": "running", "msg": "started"})
            elif p == "/api/item-prices":
                prices = data.get("prices") or {}
                saved = _db_save_prices(prices)
                if "error" in saved:
                    self._send_json({"ok": False, "error": saved["error"]})
                    return
                # 保存成功后触发重新解析
                uid = str(data.get("uid") or "auto")
                if _PARSE_THREAD and _PARSE_THREAD.is_alive():
                    self._send_json({"ok": True, "prices": saved, "status": "already_running"})
                else:
                    _PARSE_THREAD = threading.Thread(target=run_parse, args=(uid, True), daemon=True)
                    _PARSE_THREAD.start()
                    self._send_json({"ok": True, "prices": saved, "status": "running"})
            elif p == "/api/item-add":
                cid = str(data.get("cid") or "").strip()
                name = str(data.get("name") or "").strip()
                try:
                    price = int(data.get("price") or 0)
                except Exception:
                    price = 0
                if not cid:
                    self._send_json({"ok": False, "error": "CID 不能为空"})
                    return
                # 基础物品表里已存在的 cid 只允许改价，不允许在此新增（避免覆盖内置名称）
                try:
                    base = bidking_parser.get_base_item_cids(CSV)
                except Exception:
                    base = set()
                if cid in base:
                    self._send_json({"ok": False, "error": "该 CID 已在基础物品表中，请在上方价格配置区修改价格"})
                    return
                ok = _db_add_item(cid, name, price)
                self._send_json({"ok": ok})
            elif p == "/api/item-delete":
                cid = str(data.get("cid") or "").strip()
                if not cid:
                    self._send_json({"ok": False, "error": "CID 不能为空"})
                    return
                # 只允许删除"用户自添加"的新物品，基础物品表里的不动
                try:
                    base = bidking_parser.get_base_item_cids(CSV)
                except Exception:
                    base = set()
                if cid in base:
                    self._send_json({"ok": False, "error": "该物品属于基础物品表，不可删除（只能在价格配置页改价）"})
                    return
                ok = _db_delete_item(cid)
                self._send_json({"ok": ok})
            elif p == "/api/ngrok/token":
                token = str(data.get("token") or "").strip()
                if not token:
                    self._send_json({"ok": False, "error": "Token 不能为空"})
                    return
                ok, err = _save_token(token)
                self._send_json({"ok": ok, "error": err,
                                 "token_saved": bool(_read_token()),
                                 "token_masked": _mask_token(_read_token()) if _read_token() else None})
            elif p == "/api/ngrok/start":
                token = _read_token()
                if not token:
                    self._send_json({"running": False, "error": "请先保存 ngrok authtoken"})
                    return
                # P0-9 安全前置：公网暴露是最高风险动作，必须
                #   1) 已设置访问密码（否则等于把你电脑裸奔到全球公网）
                #   2) 仅本机可启动（远程/恶意网页不得把本机隧道开到公网）
                if not _pass_enabled():
                    self._send_json({"running": False,
                                     "error": "请先在电脑端设置访问密码，再启动公网访问（防止未设密码裸奔公网）"},
                                    code=403)
                    return
                if not _is_local_peer(self):
                    self._send_json({"running": False,
                                     "error": "启动公网访问只能在本机电脑上操作"},
                                    code=403)
                    return
                # 确定要映射的端口（从 server 的当前端口获取）
                port = NGROK_PORT
                with NGROK_LOCK:
                    result = ngrok_start(port)
                if result is True:
                    self._send_json({"running": True, "url": None, "error": None,
                                    "token_saved": True, "token_masked": _mask_token(token)})
                else:
                    self._send_json({"running": False, "error": f"启动失败: {result}",
                                    "token_saved": True, "token_masked": _mask_token(token)})
            elif p == "/api/ngrok/stop":
                with NGROK_LOCK:
                    ngrok_stop()
                self._send_json({"running": False, "url": None, "error": None,
                                "token_saved": bool(_read_token()),
                                "token_masked": _mask_token(_read_token()) if _read_token() else None})
            elif p == "/api/career-db-clean":
                # 安全（P0-4）：清库/裁剪是破坏性操作，仅限本机。
                if not _is_local_peer(self):
                    self._send_json({"ok": False, "error": "local_only",
                                     "msg": "清理生涯数据只能在本机电脑上操作"}, code=403)
                    return
                retention = int(data.get("retention_days") or 0)
                deleted, status = _career_db_clean(retention)
                self._send_json({"ok": True, "deleted": deleted, **status})
            elif p == "/api/career-db-clear":
                # 安全（P0-4）：清空生涯库是破坏性操作，仅限本机。
                if not _is_local_peer(self):
                    self._send_json({"ok": False, "error": "local_only",
                                     "msg": "清空生涯数据只能在本机电脑上操作"}, code=403)
                    return
                deleted, status = _career_db_clear()
                self._send_json({"ok": True, "deleted": deleted, **status})
            elif p == "/api/career-import":
                # body 是 ZIP 二进制数据
                zip_data = body if body else b""
                inserted, skipped, invalid = _career_db_import_zip(zip_data)
                # 更新导入时间
                conn = _career_db_connect()
                conn.execute("UPDATE career_settings SET value=? WHERE key='last_import'",
                             (time.strftime("%Y-%m-%d %H:%M:%S"),))
                conn.commit()
                conn.close()
                self._send_json({"ok": True, "inserted": inserted,
                                 "skipped_duplicates": skipped, "invalid_files": invalid})
            elif p == "/api/control/claim":
                cid = str(data.get("client_id") or "").strip()
                force = bool(data.get("force"))
                state, ok = _control_acquire(cid, force)
                self._send_json(state, code=200 if ok else 409)
            elif p == "/api/control/heartbeat":
                cid = str(data.get("client_id") or "").strip()
                force = bool(data.get("force"))
                state, ok = _control_acquire(cid, force)
                self._send_json(state, code=200 if ok else 409)
            elif p == "/api/control/release":
                cid = str(data.get("client_id") or "").strip()
                _control_release(cid)
                self._send_json({"locked": False, "owner": ""})
            elif p == "/api/launch":
                # 手机只能触发启动，路径完全由 exe 端配置决定（请求体一律忽略）
                self._send_json(launcher_start())
            elif p == "/api/launch/pick-file":
                # 2026-09-04 用户反馈：网页手填估价器路径太麻烦 → 点「浏览」由
                # 后端弹 Windows 原生文件选择框（浏览器安全限制拿不到完整本地路径，
                # 只能让服务端弹）。仅限本机：弹窗发生在服务进程里，远程触发会把
                # 对话框弹到别人电脑上；且 pick-file 与改配置同级敏感。
                if not _is_local_peer(self):
                    self._send_json({"ok": False, "error": "local_only",
                                     "msg": "选择文件只能在本机电脑上操作"}, code=403)
                    return
                try:
                    if winlaunch is None:
                        raise RuntimeError("当前环境不支持弹窗选择（winlaunch 不可用）")
                    _allowed_name = _launcher_load_config().get("allowed_filename", "竞拍之王全自动估价器.exe")
                    _picked = winlaunch.native_pick_exe_file(title="选择「%s」" % _allowed_name)
                except Exception as _pe:
                    self._send_json({"ok": False, "path": "", "error": f"弹窗失败：{_pe}"})
                    return
                self._send_json({"ok": bool(_picked), "path": _picked or "",
                                 "error": "" if _picked else "未选择文件"})
            elif p == "/api/launch/config":
                # P0-6（加固）：仅允许本机修改启动配置。远程（手机/局域网）不得改
                # exact_path / search_dirs，否则攻击者可把搜索目录指向 UNC 或任意盘符
                # 塞入同名 exe 再远程启动 = 任意代码执行。
                if not _is_local_peer(self):
                    self._send_json({"ok": False, "error": "local_only",
                                     "msg": "修改启动配置只能在本机电脑上操作"}, code=403)
                    return
                cfg = _launcher_load_config()
                allowed = cfg["allowed_filename"]
                cpath = _launcher_config_path()
                ep = str(data.get("exact_path") or "").strip()
                if ep and os.path.basename(ep).lower() != allowed.lower():
                    self._send_json({"ok": False,
                                     "error": "文件名必须是 %s（你填的是 %s）" % (allowed, os.path.basename(ep)),
                                     "config_path": cpath}, code=400)
                    return
                old_ep = str(cfg.get("exact_path") or "").strip()
                cfg["exact_path"] = ep
                if ep and old_ep and os.path.abspath(ep).lower() != os.path.abspath(old_ep).lower():
                    # 换了目标路径 → 重置哈希锁（2026-08-30）：旧哈希对应旧位置的文件，
                    # 不重置会让换文件夹后的首次启动被「SHA-256 不匹配」拦死。
                    # 防替换语义不变：新路径在首次启动时重新记录哈希并锁定。
                    cfg["exe_sha256"] = ""
                dirs = data.get("search_dirs")
                if isinstance(dirs, list):
                    cfg["search_dirs"] = [str(x).strip() for x in dirs if str(x).strip()]
                elif isinstance(dirs, str):
                    cfg["search_dirs"] = [x.strip() for x in dirs.split(",") if x.strip()]
                ok, save_err = _launcher_save_config(cfg)
                path, err = _launcher_resolve(cfg)
                if not ok:
                    self._send_json({"ok": False, "config": cfg, "resolved_path": path or "",
                                     "resolved": bool(path),
                                     "error": save_err or "配置写入失败（未知原因）",
                                     "config_path": cpath}, code=500)
                    return
                self._send_json({"ok": True, "config": cfg, "resolved_path": path or "",
                                 "resolved": bool(path), "error": err or "",
                                 "config_path": cpath})
            elif p == "/api/json-save/save":
                _json_save_do_save()
                self._send_json(_json_save_warning_state())
            elif p == "/api/json-save/discard":
                _json_save_do_discard()
                self._send_json(_json_save_warning_state())
            elif p == "/api/json-save/ack":
                _json_save_ack()
                self._send_json(_json_save_warning_state())
            elif p == "/api/json-save/open-folder":
                host = self.headers.get("Host", "")
                if "localhost" in host or "127.0.0.1" in host or "::1" in host:
                    try:
                        if sys.platform == "win32":
                            os.startfile(ROOT)
                        else:
                            subprocess.Popen(["xdg-open", ROOT])
                        self._send_json(_json_save_warning_state())
                    except Exception as e:
                        self._send_json({"error": str(e)}, code=500)
                else:
                    self._send_json({"error": "forbidden: only localhost can open folder"}, code=403)
            else:
                self._send_json({"error": "unknown_endpoint", "path": p}, code=404)
        except Exception as e:
            self._send_json({"ok": False, "error": f"服务器内部错误: {type(e).__name__}: {e}"}, code=500)

def make_server(port=8766, root=None):
    """创建并返回本地 HTTP 服务器（根目录为 root，缺省为脚本目录）。"""
    global NGROK_PORT
    init(root)
    NGROK_PORT = port
    return http.server.ThreadingHTTPServer(("", port), Handler)

# ==================== 阶段3：机器绑定 / 分享防护 ====================
# 目标：整个文件夹被拷给朋友、或换电脑后 —— 凭据自动作废（DPAPI 解不开），
# 但战绩数据一律保留。只有「密码/配对/ngrok token」被重置，绝不碰 career.db。

SEC_ERROR_KEY = "bk_sec_error"     # 网页黄条读取的全局状态（启动自检结果）

def _machine_fingerprint():
    """当前机器的 3 个指纹信号（与 winsec.machine_signals 相同口径）。"""
    if winsec is not None:
        return winsec.machine_signals()
    return {"guid": None, "volserial": None, "installdate": None}

def _boot_security_check():
    """启动自检（阶段3）：
    1. .bidking_sec.bin 存在且能解密 → 继续往下比指纹；
    2. 解不开（换机/被拷走）→ 判定"换机"，调用 _security_reset()；
    3. 能解密但 3 个指纹里命中少于 2 个 → 也视为换机，重置凭据。
    重置只清凭据/敏感配置文件，绝不动 生涯数据库、result*.json、item_prices.db。
    返回 True 表示正常；False 表示已执行安全重置。
    """
    path = _sec_path()
    # 首次运行（没有凭据文件）不算换机——全新开始
    if not os.path.isfile(path):
        return True
    obj = None
    if winsec is not None:
        obj = winsec.secure_read_json(path, None)
    if not isinstance(obj, dict) or not obj.get("master"):
        return _security_reset("凭据文件无法解密（已换电脑或文件夹被拷贝）")
    # 能解密 → 比指纹（3 选 2 容忍，允许装系统/换网卡等小变动）
    sig = _machine_fingerprint()
    got = 0
    for k in ("guid", "volserial", "installdate"):
        if obj.get("fp_" + k) and sig.get(k) and str(obj["fp_" + k]) == str(sig.get(k)):
            got += 1
    if got < 2:
        return _security_reset(
            "机器指纹不匹配（已换电脑或文件夹被拷贝；%d/3 命中）" % got)
    return True

def _security_reset(reason):
    """安全重置：只清"凭据与敏感配置"，绝不动战绩数据。
    返回值 True。由启动自检调用，也会把原因写进全局，让网页显示黄条。
    """
    global _SEC_CACHE, _AUTH_TOKENS
    # 1) 清凭据文件
    for f in (SEC_FILE_NAME, ".ngrok_token", ".ngrok_token.enc",
              ".bidking_pass", "parse.lock.json", PASS_FILE_NAME):
        try:
            _p = os.path.join(ROOT, f)
            if os.path.exists(_p):
                os.remove(_p)
        except Exception:
            pass
    # 2) 重置 launcher 配置（去掉敏感路径，保留默认值）
    try:
        _p = _launcher_config_path()
        if os.path.isfile(_p):
            with open(_p, "w", encoding="utf-8") as f:
                json.dump(_launcher_default_config(), f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    # 3) 内存状态复位
    _SEC_CACHE = {"obj": None, "loaded": False}
    _AUTH_TOKENS = {}
    # 4) 记录原因供网页显示（黄条）
    try:
        _sec_warn_path = os.path.join(ROOT, ".bidking_sec_warn.json")
        with open(_sec_warn_path, "w", encoding="utf-8") as f:
            json.dump({"reason": reason, "ts": time.time()}, f, ensure_ascii=False)
    except Exception:
        pass
    print("[安全] %s —— 已自动重置密码与配对（战绩数据未受影响）。" % reason, file=sys.stderr, flush=True)
    return True

def _read_security_warning():
    """读取启动自检产生的警告（网页黄条用），读完即删。"""
    try:
        _p = os.path.join(ROOT, ".bidking_sec_warn.json")
        if os.path.isfile(_p):
            with open(_p, "r", encoding="utf-8") as f:
                d = json.load(f)
            os.remove(_p)
            return d.get("reason") or ""
    except Exception:
        pass
    return ""

def _prepare_share():
    """--prepare-share 一键清理（阶段3）：删除全部凭据文件 + 重置 launcher 配置，
    然后打印「该发什么 / 不该发什么」清单。返回 True。"""
    print("=" * 56)
    print("准备分享：正在清理私人凭据 …")
    # 凭据与敏感文件
    targets = [SEC_FILE_NAME, ".ngrok_token", ".ngrok_token.enc",
               ".bidking_pass", ".bidking_sec_warn.json", "parse.lock.json"]
    for f in targets:
        _p = os.path.join(ROOT, f)
        if os.path.exists(_p):
            try:
                os.remove(_p)
                print("  已删除：%s" % f)
            except Exception as e:
                print("  删除失败：%s（%s）" % (f, e))
        else:
            print("  不存在：%s" % f)
    # launcher 配置重置为默认（去掉你的用户名路径）
    try:
        _p = _launcher_config_path()
        with open(_p, "w", encoding="utf-8") as f:
            json.dump(_launcher_default_config(), f, ensure_ascii=False, indent=1)
        print("  已重置：.bidking_launcher.json（默认配置，无个人路径）")
    except Exception as e:
        print("  重置失败：.bidking_launcher.json（%s）" % e)
    print("-" * 56)
    print("以下文件【应该】随包发给朋友：")
    print("  BidKing解析器.exe")
    print("  最终交付/BidKing远程助手.apk")
    print("  最终交付/使用说明.md")
    print("以下文件【绝不能】发出去（含你的私人凭据/机器信息）：")
    print("  你的用户名目录下的 .bidking_sec.bin / .ngrok_token* / .bidking_pass")
    print("  生涯数据库/career.db（你的战绩，除非你明确想给）")
    print("  item_prices.db / result*.json / silver_cache.json")
    print("  .bidking_launcher.json（已重置为默认，但里面仍含电脑路径）")
    print("=" * 56)
    return True


def _handle_cli_args(args):
    """统一命令行入口（阶段3b）。
    供两处调用：serve.py 的 __main__（源码直跑）与 bidking_launcher.py 的 main()
    （Nuitka 打包后的 exe 入口）。返回 True 表示命令已处理、调用方应立即退出。
    """
    args = list(args or [])
    if "--prepare-share" in args:
        _prepare_share()
        return True
    if "--reset-security" in args:
        _security_reset("手动重置（--reset-security）")
        print("[安全] 已手动重置密码与配对（战绩数据未受影响）。")
        return True
    return False


if __name__ == "__main__":
    # 阶段3b：命令行入口
    _args = sys.argv[1:]
    if _handle_cli_args(_args):
        sys.exit(0)
    # 阶段3a：启动自检（换机/被拷贝后自动重置凭据，数据不动）
    _boot_security_check()
    # 端口参数：只认纯数字（避免把 --xxx 当端口）
    _port = 8766
    if len(_args) > 0 and str(_args[0]).isdigit():
        _port = int(_args[0])
    print("已在本地启动服务器： http://localhost:%d" % _port)
    print("报表(富功能)：     http://localhost:%d/bidking_report.html" % _port)
    print("独立解析器(拖文件)：http://localhost:%d/bidking_standalone.html" % _port)
    print("点报表里的「实时刷新」即可用完整解析器重新生成战绩（无需使用旧 .exe）。Ctrl+C 停止。")
    _srv = make_server(_port)
    try:
        _srv.serve_forever()
    except KeyboardInterrupt:
        # 清理 ngrok
        ngrok_stop()
        print("\n已停止。")
