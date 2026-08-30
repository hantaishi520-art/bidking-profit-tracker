# -*- coding: utf-8 -*-
"""winsec.py —— 本机凭据安全存储（纯 ctypes / 标准库，零第三方依赖）。

安全目标(P1/P2)：
  - 密码不再明文落盘（改 PBKDF2 哈希）。
  - 配对凭据/ngrok token 放进 DPAPI 加密文件（机器绑定）。
  - 整个文件夹被拷给朋友时，DPAPI 解不开 -> 自动安全重置（凭证失效，数据保留）。

对外提供：
  - secure_write_json(path, obj) / secure_read_json(path) —— JSON 容器，DPAPI 加密
  - machine_signals()           —— 3 个机器指纹信号（用于辅助判机）
  - pbkdf2_hash(pwd) / pbkdf2_verify(pwd, stored) —— 密码强哈希
  - dpapi_protect(data) / dpapi_unprotect(blob)    —— 底层 DPAPI（LOCAL_MACHINE 域）
"""
import os, sys, json, base64, hashlib, hmac, secrets
import ctypes
from ctypes import wintypes

IS_WIN = (sys.platform == "win32")

# ---- DPAPI 常量 ----
CRYPTPROTECT_LOCAL_MACHINE = 0x4
CRYPTPROTECT_UI_FORBIDDEN = 0x1

if IS_WIN:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32


# ---- DPAPI 结构 ----
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def dpapi_protect(data: bytes, entropy: bytes = b"") -> bytes:
    """DPAPI 加密（LOCAL_MACHINE 域 -> 绑定本机+用户）。失败返回 b""。"""
    if not IS_WIN:
        return b""
    try:
        inblob = DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte)))
        ent = DATA_BLOB(len(entropy), ctypes.cast(ctypes.create_string_buffer(entropy), ctypes.POINTER(ctypes.c_byte))) if entropy else DATA_BLOB(0, None)
        outblob = DATA_BLOB()
        ok = crypt32.CryptProtectData(ctypes.byref(inblob), "bidking", ctypes.byref(ent),
                                      None, None, CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(outblob))
        if not ok:
            return b""
        out = ctypes.string_at(outblob.pbData, outblob.cbData)
        kernel32.LocalFree(outblob.pbData)
        return out
    except Exception:
        return b""


def dpapi_unprotect(blob: bytes, entropy: bytes = b"") -> bytes:
    """DPAPI 解密（失败返回 b""，表示机器/用户已变更）。"""
    if not IS_WIN or not blob:
        return b""
    try:
        inblob = DATA_BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob), ctypes.POINTER(ctypes.c_byte)))
        ent = DATA_BLOB(len(entropy), ctypes.cast(ctypes.create_string_buffer(entropy), ctypes.POINTER(ctypes.c_byte))) if entropy else DATA_BLOB(0, None)
        outblob = DATA_BLOB()
        ok = crypt32.CryptUnprotectData(ctypes.byref(inblob), None, ctypes.byref(ent),
                                        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(outblob))
        if not ok:
            return b""
        out = ctypes.string_at(outblob.pbData, outblob.cbData)
        kernel32.LocalFree(outblob.pbData)
        return out
    except Exception:
        return b""


# ---- 机器指纹（辅助判机，不作主判据）----
def machine_signals():
    """返回 3 个信号：MachineGuid / 系统盘卷序列号 / InstallDate。
    读不到返回 None。仅用于"换机提示"，不作安全主判据。"""
    signals = {"guid": None, "volserial": None, "installdate": None}
    if not IS_WIN:
        return signals
    try:
        import winreg
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
            signals["guid"], _ = winreg.QueryValueEx(k, "MachineGuid")
            winreg.CloseKey(k)
        except Exception:
            pass
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
            signals["installdate"], _ = winreg.QueryValueEx(k, "InstallDate")
            winreg.CloseKey(k)
        except Exception:
            pass
    except Exception:
        pass
    # 系统盘卷序列号
    try:
        vol = ctypes.create_unicode_buffer(4)
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(os.environ.get("SystemDrive", "C:") + "\\"),
            None, 0, None, None, None, vol, 4)
        if ok:
            signals["volserial"] = hex(vol.value) if vol.value else None
    except Exception:
        pass
    return signals


# ---- 密码强哈希（PBKDF2-HMAC-SHA256）----
PBKDF2_ITER = 200_000


def pbkdf2_hash(pwd: str) -> str:
    """返回 "pbkdf2$<iters>$<salt_b64>$<hash_b64>"。"""
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, PBKDF2_ITER)
    return "pbkdf2$%d$%s$%s" % (PBKDF2_ITER,
                                base64.b64encode(salt).decode(),
                                base64.b64encode(h).decode())


def pbkdf2_verify(pwd: str, stored: str) -> bool:
    """校验 PBKDF2 哈希。stored 是 pbkdf2$... 格式。"""
    try:
        parts = stored.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2":
            return False
        iters = int(parts[1])
        salt = base64.b64decode(parts[2])
        want = base64.b64decode(parts[3])
        h = hashlib.pbkdf2_hmac("sha256", pwd.encode("utf-8"), salt, iters)
        return hmac.compare_digest(h, want)
    except Exception:
        return False


# ---- JSON 安全读写（DPAPI 加密）----
def secure_write_json(path, obj):
    """把 obj 转 JSON -> DPAPI 加密 -> 原子写。返回 (ok, error)。"""
    try:
        data = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        blob = dpapi_protect(data)
        if not blob:
            return False, "DPAPI 加密失败（当前环境不支持）"
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(b"BK1" + blob)   # 文件头标识版本
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def secure_read_json(path, default=None):
    """读取并解密 JSON。DPAPI 解不开（换机/损坏）返回 default。"""
    try:
        if not os.path.isfile(path):
            return default
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:3] != b"BK1":
            return default
        data = dpapi_unprotect(raw[3:])
        if not data:
            return default
        return json.loads(data.decode("utf-8"))
    except Exception:
        return default


if __name__ == "__main__":
    # 自测
    h = pbkdf2_hash("test123")
    print("pbkdf2 hash:", h)
    print("verify ok:", pbkdf2_verify("test123", h))
    print("verify bad:", pbkdf2_verify("wrong", h))
    blob = dpapi_protect(b"hello")
    print("dpapi ok:", bool(blob), "len:", len(blob))
    print("unprotect:", dpapi_unprotect(blob))
    print("signals:", machine_signals())