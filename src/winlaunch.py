# -*- coding: utf-8 -*-
"""
Windows 窗口/进程辅助（纯 ctypes，零第三方依赖，方便 Nuitka onefile 打包）。

用途（远程启动外部程序功能）：
  1. 按进程名列出 PID（判断目标程序是否已在运行）
  2. 按 PID 找主窗口并置顶
  3. 查找弹窗（如"卡密验证"）里的按钮并自动点击"确定"
  4. 兜底：对窗口发送回车键

只依赖 Python 标准库 + Windows API，不需要 pywin32 / comtypes / uiautomation。
"""
import os
import subprocess
import sys
import time
import ctypes
from ctypes import wintypes

IS_WIN = (sys.platform == "win32")

# ---- Win32 常量 ----
BM_CLICK = 0x00F5
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_SETFOCUS = 0x0007
WM_ACTIVATE = 0x0006
WA_ACTIVE = 1
VK_RETURN = 0x0D
VK_TAB = 0x09
VK_SPACE = 0x20
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
MAPVK_VK_TO_VSC = 0
SMTO_ABORTIFHUNG = 0x0002
IDOK = 1
SW_RESTORE = 9
SW_SHOW = 5
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
INVALID_HANDLE_VALUE = -1

if IS_WIN:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumChildWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
else:  # pragma: no cover - 仅 Windows 使用
    user32 = None
    kernel32 = None
    WNDENUMPROC = None


# ---- SendInput 用的结构体（比 keybd_event 更接近真实键盘输入）----
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
               ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
               ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
               ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
               ("dwExtraInfo", ctypes.c_void_p)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
               ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


def _enum_callback_collect(hwnd, out):
    out.append(hwnd)
    return True


def enum_top_windows():
    """枚举所有顶层可见窗口，返回 hwnd 列表。"""
    if not IS_WIN:
        return []
    out = []
    try:
        cb = WNDENUMPROC(lambda h, l: _enum_callback_collect(h, out))
        user32.EnumWindows(cb, 0)
    except Exception:
        pass
    return out


def enum_child_windows(parent):
    if not IS_WIN or not parent:
        return []
    out = []
    try:
        cb = WNDENUMPROC(lambda h, l: _enum_callback_collect(h, out))
        user32.EnumChildWindows(wintypes.HWND(parent), cb, 0)
    except Exception:
        pass
    return out


def get_window_text(hwnd):
    if not IS_WIN or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, 512)
        return buf.value or ""
    except Exception:
        return ""


def get_class_name(hwnd):
    if not IS_WIN or not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        return buf.value or ""
    except Exception:
        return ""


def get_window_pid(hwnd):
    if not IS_WIN or not hwnd:
        return 0
    try:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def find_windows(title_sub=None, class_name=None, pid=None, visible_only=True):
    """按条件查找顶层窗口。title_sub 为标题子串；返回 hwnd 列表。"""
    res = []
    for h in enum_top_windows():
        try:
            if visible_only and not user32.IsWindowVisible(wintypes.HWND(h)):
                continue
            if pid is not None and get_window_pid(h) != pid:
                continue
            if class_name and get_class_name(h) != class_name:
                continue
            if title_sub and title_sub not in get_window_text(h):
                continue
            res.append(h)
        except Exception:
            continue
    return res


def find_button(hwnd, text_exact=None, text_sub=None):
    """在窗口的所有子孙控件里找按钮：优先类名含 Button，其次任意控件文本匹配。"""
    if not IS_WIN or not hwnd:
        return 0
    exact, fuzzy = 0, 0
    for ch in enum_child_windows(hwnd):
        try:
            txt = get_window_text(ch).strip()
            cls = get_class_name(ch)
            if text_exact and txt == text_exact:
                if "button" in cls.lower():
                    return ch          # 完全命中且确实是按钮：立刻返回
                exact = exact or ch
            elif text_sub and text_sub in txt:
                if "button" in cls.lower():
                    return ch
                fuzzy = fuzzy or ch
        except Exception:
            continue
    return exact or fuzzy


def click_button(hwnd):
    """发送 BM_CLICK（模拟点击按钮），成功返回 True。"""
    if not IS_WIN or not hwnd:
        return False
    try:
        return bool(user32.PostMessageW(wintypes.HWND(hwnd), BM_CLICK, 0, 0))
    except Exception:
        return False


def get_foreground_window():
    if not IS_WIN:
        return 0
    try:
        return int(user32.GetForegroundWindow())
    except Exception:
        return 0


def bring_to_front(hwnd):
    """把窗口还原并置到最前；会等待直到确实成为前台窗口（最多约 2 秒）。"""
    if not IS_WIN or not hwnd:
        return False
    try:
        h = wintypes.HWND(hwnd)
        if user32.IsIconic(h):
            user32.ShowWindow(h, SW_RESTORE)
        else:
            user32.ShowWindow(h, SW_SHOW)
        for _ in range(10):
            user32.SetForegroundWindow(h)
            time.sleep(0.2)
            if get_foreground_window() == int(hwnd):
                return True
        return get_foreground_window() == int(hwnd)
    except Exception:
        return False


def force_foreground(hwnd, timeout=2.0):
    """尽最大努力把窗口变成前台窗口。

    Windows 有"防焦点抢占"机制：普通 SetForegroundWindow 在别的程序占用前台时经常无效，
    回车就会发到错误的窗口（这就是"卡在卡密验证"的主因之一）。
    这里用经典手法：先附着到当前前台线程的输入队列，再置前，最后逐个 API 兜底并校验结果。
    """
    if not IS_WIN or not hwnd:
        return False
    h = wintypes.HWND(hwnd)
    try:
        if user32.IsIconic(h):
            user32.ShowWindow(h, SW_RESTORE)
        else:
            user32.ShowWindow(h, SW_SHOW)
    except Exception:
        pass
    end = time.time() + max(0.5, float(timeout))
    while time.time() < end:
        try:
            fg = int(user32.GetForegroundWindow() or 0)
            if fg == int(hwnd):
                return True
            fg_thread = user32.GetWindowThreadProcessId(wintypes.HWND(fg), None) if fg else 0
            cur_thread = kernel32.GetCurrentThreadId()
            attached = False
            if fg_thread and fg_thread != cur_thread:
                attached = bool(user32.AttachThreadInput(fg_thread, cur_thread, True))
            try:
                user32.BringWindowToTop(h)
                user32.SetForegroundWindow(h)
                user32.SetActiveWindow(h)
                user32.SetFocus(h)
            finally:
                if attached:
                    try:
                        user32.AttachThreadInput(fg_thread, cur_thread, False)
                    except Exception:
                        pass
            if get_foreground_window() == int(hwnd):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return get_foreground_window() == int(hwnd)


def send_key(vk, hold=0.05):
    """用 SendInput 发一次真实按键（带硬件扫描码），比 keybd_event 更接近人手敲键盘。"""
    if not IS_WIN:
        return False
    try:
        user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        user32.MapVirtualKeyW.restype = wintypes.UINT
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        vk = int(vk)
        scan = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) or 0)
        extra = ctypes.cast(ctypes.pointer(ctypes.c_ulong(0)), ctypes.c_void_p)

        def make(flags):
            return INPUT(type=INPUT_KEYBOARD,
                         ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags,
                                       time=0, dwExtraInfo=extra))

        down = make(KEYEVENTF_SCANCODE)
        up = make(KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)
        events = (INPUT * 2)(down, up)
        sent = user32.SendInput(2, events, ctypes.sizeof(INPUT))
        time.sleep(hold)
        return bool(sent)
    except Exception:
        return False


def send_command(hwnd, cmd_id=IDOK, timeout_ms=800):
    """给窗口发 WM_COMMAND（cmd_id=1 即 IDOK，很多原生对话框的「确定」就是它）。
    用 SendMessageTimeout，窗口无响应也不会把我们的线程卡死。"""
    if not IS_WIN or not hwnd:
        return False
    try:
        user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM,
                                               wintypes.UINT, wintypes.UINT,
                                               ctypes.POINTER(wintypes.DWORD)]
        user32.SendMessageTimeoutW.restype = ctypes.c_long
        result = wintypes.DWORD(0)
        ok = user32.SendMessageTimeoutW(wintypes.HWND(hwnd), WM_COMMAND,
                                        wintypes.WPARAM(int(cmd_id)), wintypes.LPARAM(0),
                                        SMTO_ABORTIFHUNG, int(timeout_ms),
                                        ctypes.byref(result))
        return bool(ok)
    except Exception:
        return False


def press_key(vk, hold=0.05):
    """按下并释放一个虚拟键（优先 SendInput，失败回退 keybd_event）。"""
    if not IS_WIN:
        return False
    try:
        if send_key(vk, hold):
            return True
    except Exception:
        pass
    try:
        user32.keybd_event(int(vk), 0, 0, 0)
        time.sleep(hold)
        user32.keybd_event(int(vk), 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


def is_foreground(hwnd):
    """判断 hwnd 是否就是当前前台窗口。"""
    if not IS_WIN or not hwnd:
        return False
    try:
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        return False


def restore_foreground(hwnd):
    """把前台还给指定窗口。

    用途：后台按键会让 Tk 把窗口提到前台，点掉之后应尽量把焦点还给
    用户原来在用的窗口（游戏），把打断压到最短。

    失败也无妨：窗口销毁后 Windows 通常会自动把焦点交给 Z 序上的下一个窗口，
    所以这里只是"尽力而为"。
    """
    if not IS_WIN or not hwnd:
        return False
    try:
        h = wintypes.HWND(hwnd)
        if not user32.IsWindow(h):
            return False
        user32.SetForegroundWindow(h)
        return is_foreground(hwnd)
    except Exception:
        return False


def send_background_key(hwnd, vk=VK_RETURN):
    """后台按键：不抢前台，直接把按键消息投递进目标窗口的消息队列。

    为什么要先发 WM_SETFOCUS：
    Tk（tkinter）在窗口不在前台时会认为"没有焦点控件"，收到的 WM_KEYDOWN
    被直接丢弃（实测探针确认消息根本没进 Tk 事件循环）。补一条 WM_SETFOCUS
    把它的内部焦点唤醒后，按键才会被正常派发。

    ⚠️ 副作用（实测确认，无法避免）：Tk 收到 WM_SETFOCUS 后会把窗口提到前台。
    这是 Tk 自身的行为，不是我们强制抢的——想让它处理按键，它就一定会冒出来。
    因此调用方应在确认成功后用 restore_foreground() 把焦点还回原窗口。

    ⚠️ 不要发 WM_ACTIVATE：它同样会让窗口冒出来，且激活语义更强，
    实测与 WM_SETFOCUS 效果相同（3/3），没必要叠加。

    实测数据（2026-08-29，每个用例 3 轮）：
        裸发按键            0/3
        只 WM_SETFOCUS      3/3   ← 采用
        只 WM_ACTIVATE      3/3
        ACTIVATE+SETFOCUS   3/3
        只 WM_CHAR          0/3   ← 必须发 WM_KEYDOWN，WM_CHAR 无效
    按键生效极快：实测从发出到窗口关闭仅 0.05 秒。

    ✅ 相比旧的「抢前台 + SendInput 真回车」，本手段的关键优势是
    **按键消息定向投递给目标窗口，绝不会外泄到用户正在用的窗口**
    （旧方案抢前台失败时，回车会被发给全屏游戏，2 分钟内可误触几十次）。

    返回 True 只代表消息投递成功，不代表窗口已关闭（需调用方再验证）。
    """
    if not IS_WIN or not hwnd:
        return False
    try:
        h = wintypes.HWND(hwnd)
        user32.PostMessageW(h, WM_SETFOCUS, wintypes.WPARAM(0), wintypes.LPARAM(0))
        down = user32.PostMessageW(h, WM_KEYDOWN, wintypes.WPARAM(int(vk)), wintypes.LPARAM(0))
        up = user32.PostMessageW(h, WM_KEYUP, wintypes.WPARAM(int(vk)), wintypes.LPARAM(0))
        return bool(down) and bool(up)
    except Exception:
        return False


def press_enter(hwnd=None):
    """对指定窗口发送**真实**回车键（全局按键，会先抢前台）。

    ⚠️ 安全闸：只有确认窗口确实成为前台后才发按键。
    旧实现丢弃了 force_foreground() 的返回值，导致抢前台失败时，回车被发给
    当时最前面的窗口——全屏游戏时会误触发游戏内的操作（回车/Chat、Tab 记分牌等）。
    """
    if not IS_WIN:
        return False
    try:
        if hwnd:
            if not force_foreground(hwnd):
                return False          # 没抢到前台：绝不能发全局按键
            if not is_foreground(hwnd):
                return False          # 二次确认，防止抢完又被别的窗口抢走
        else:
            time.sleep(0.1)
        time.sleep(0.25)
        return press_key(VK_RETURN)
    except Exception:
        return False


def wait_window_gone(title_sub, timeout=3.0, poll=0.3):
    """等待标题含 title_sub 的窗口消失；消失返回 True，超时返回 False。"""
    if not IS_WIN:
        return True
    end = time.time() + max(0.5, float(timeout))
    while time.time() < end:
        if not find_windows(title_sub=title_sub, visible_only=True):
            return True
        time.sleep(poll)
    return not find_windows(title_sub=title_sub, visible_only=True)


def process_pids_by_name(exe_name):
    """按进程映像名列出所有 PID（不区分大小写）。exe_name 例如 竞拍之王全自动估价器.exe"""
    if not IS_WIN or not exe_name:
        return []
    pids = []
    target = exe_name.lower()
    try:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if (entry.szExeFile or "").lower() == target:
                pids.append(int(entry.th32ProcessID))
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        kernel32.CloseHandle(snap)
    except Exception:
        pass
    return pids


def get_process_path(pid):
    """取进程的完整映像路径（失败返回空串）。"""
    if not IS_WIN or not pid:
        return ""
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return buf.value if ok else ""
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def list_processes(with_path=True):
    """返回 [(pid, exe_name, exe_path), ...]。"""
    out = []
    if not IS_WIN:
        return out
    try:
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == INVALID_HANDLE_VALUE:
            return out
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            name = entry.szExeFile or ""
            path = get_process_path(pid) if (with_path and pid) else ""
            out.append((pid, name, path))
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        kernel32.CloseHandle(snap)
    except Exception:
        pass
    return out


def process_pids_under_dir(dir_prefix, exclude_names=None, exclude_pid=None):
    """列出映像路径位于该目录下的所有 PID。
    用途：目标程序是「启动器 exe + 真程序 runtime exe」两段式，
    只看启动器 PID 会误判成已退出。

    exclude_names：映像文件名排除集（不含路径，不区分大小写）——2026-08-30 新增。
    背景：把解析器放进估价器同目录使用时，解析器自身进程也位于该目录下，
    会被误判成「估价器已在运行」导致永远不真正启动；调用方传入自己的
    exe 文件名集合（onefile 是引导+实际双进程同名，按文件名排除才能全覆盖）。"""
    if not IS_WIN or not dir_prefix:
        return []
    try:
        p = os.path.abspath(dir_prefix).lower().rstrip("\\/")
    except Exception:
        return []
    excl = {str(n).lower() for n in (exclude_names or []) if n}
    out = []
    for pid, name, path in list_processes():
        if exclude_pid and pid == exclude_pid:
            continue
        if not path:
            continue
        lp = os.path.abspath(path).lower()
        if not lp.startswith(p):
            continue
        if excl and os.path.basename(lp) in excl:
            continue
        out.append(pid)
    return out


def is_pid_alive(pid):
    """判断 PID 是否仍在运行。"""
    if not IS_WIN or not pid:
        return False
    try:
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        code = wintypes.DWORD(0)
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        kernel32.CloseHandle(h)
        return bool(ok) and int(code.value) == STILL_ACTIVE
    except Exception:
        return False


def find_main_window_by_pid(pid):
    if not pid:
        return 0
    for h in find_windows(pid=pid, visible_only=True):
        # 过滤掉对话框/工具提示，取第一个有标题的主窗口
        if get_window_text(h):
            return h
    ws = find_windows(pid=pid, visible_only=True)
    return ws[0] if ws else 0


def confirm_once(title_sub, button_text, settle=1.2):
    """对弹窗做"一轮"确认尝试，立刻返回，不阻塞（供外部循环每秒调用）。

    策略栈按**副作用从小到大**排列。前 3 招都是"定向消息"——只投递给目标窗口，
    不会外泄到用户当前正在用的窗口，因此在全屏游戏等场景下也是安全的：
      1. BM_CLICK        —— 有真实句柄的标准按钮（Win32 / Qt / WinForms）
      2. WM_COMMAND(IDOK) —— 原生对话框的「确定」
      3. 后台按键         —— tkinter 专用：先 WM_SETFOCUS 唤醒，再发 WM_KEYDOWN
                            （实测 3/3 命中，全屏游戏无感，这是主力手段）
      4. 抢前台 + 真回车  —— 最后兜底，带安全闸：不是前台就绝不发全局按键

    已删除旧的「Tab + 空格」兜底：它是全局按键，副作用最大、收益最低，
    在全屏游戏下会把 Tab/空格误发给游戏。

    返回 {"found": 是否看到弹窗, "confirmed": 是否点掉, "method": 手段}
    """
    res = {"found": False, "confirmed": False, "method": ""}
    if not IS_WIN:
        return res
    try:
        wins = find_windows(title_sub=title_sub, visible_only=True)
    except Exception:
        return res
    if not wins:
        return res
    res["found"] = True
    dlg = wins[0]
    try:
        btn = find_button(dlg, text_exact=button_text, text_sub=button_text)
        if btn and click_button(btn) and wait_window_gone(title_sub, settle):
            res.update(confirmed=True, method="button")
            return res
        if send_command(dlg, IDOK) and wait_window_gone(title_sub, settle):
            res.update(confirmed=True, method="idok")
            return res
        # 后台按键：定向投递给目标窗口（tkinter 的主力手段）
        # Tk 收到 WM_SETFOCUS 会把窗口提到前台，所以先记下原前台，
        # 点掉之后用 restore_foreground 把焦点还回去，尽量少打扰用户。
        orig_fg = get_foreground_window()
        if send_background_key(dlg, VK_RETURN) and wait_window_gone(title_sub, settle):
            res.update(confirmed=True, method="bgkey")
            restore_foreground(orig_fg)
            return res
        # 最后兜底：抢前台 + 真回车（press_enter 内部有安全闸，抢不到就不发）
        if press_enter(dlg) and wait_window_gone(title_sub, settle):
            res.update(confirmed=True, method="enter")
            return res
    except Exception:
        pass
    return res


def confirm_loop(title_sub, button_text, total_timeout=90, quiet_seconds=8, poll=0.5):
    """反复确认"卡密验证"这类弹窗：每次出现都点掉，直到连续 quiet_seconds 秒不再出现。

    为什么要循环：目标程序是「启动器 exe → 真程序 runtime exe」两段式启动，
    卡密验证弹窗可能先后出现多次（启动器一次、真程序一次），只处理一次不够。

    兼容 tkinter：tkinter 的按钮不是独立窗口句柄、也不支持 UI Automation，
    点不动也命令不了，靠的是 confirm_once 第 3 招「后台按键」——
    先发 WM_SETFOCUS 唤醒 Tk 的内部焦点，再定向发 WM_KEYDOWN 回车。
    全程不抢前台，因此用户全屏打游戏时也能正常确认，且不会误伤游戏。

    返回 dict: {"found": 出现次数, "confirmed": 成功点掉次数,
                "method": 用到的手段, "seconds": 耗时}
    """
    res = {"found": 0, "confirmed": 0, "method": "", "seconds": 0.0}
    if not IS_WIN:
        return res
    start = time.time()
    end = start + max(5, int(total_timeout or 90))
    quiet = 0.0
    methods = []
    wanted_quiet = max(2, float(quiet_seconds or 8))
    while time.time() < end:
        step = confirm_once(title_sub, button_text)
        if step.get("found"):
            res["found"] += 1
            quiet = 0.0
            if step.get("confirmed"):
                res["confirmed"] += 1
                m = step.get("method") or ""
                if m and m not in methods:
                    methods.append(m)
                time.sleep(0.8)      # 给主程序一点时间，可能还会弹下一个
            else:
                time.sleep(poll)
            continue
        quiet += poll
        if quiet >= wanted_quiet:
            break
        time.sleep(poll)
    res["seconds"] = round(time.time() - start, 2)
    res["method"] = ",".join(methods) or "none"
    return res


# ===== 作业对象（Job Object）：让子进程随本进程一起死亡（2026-08-30）=====
# 背景：ngrok.exe 由解析器 spawn，此前解析器退出（控制台×/任务管理器/崩溃）时
# Windows 不会自动杀子进程 → ngrok 残留后台，公网隧道继续开着。
# 方案：把子进程放进带 KILL_ON_JOB_CLOSE 的作业对象——本进程无论以何种方式
# 终止，内核关闭作业句柄时都会连带终止作业内所有进程。这是 Windows 上
# 「父死子随」的唯一可靠机制（atexit/信号处理在强杀路径下不可靠）。

_JOB_HANDLE = None   # 模块级保活：句柄被关闭前作业一直有效；进程退出时内核统一回收


def assign_job_kill_on_close(proc):
    """把 Popen 子进程加入「父死子随」作业。返回是否成功（失败不影响启动本身）。"""
    global _JOB_HANDLE
    if not IS_WIN or proc is None:
        return False
    try:
        k32 = ctypes.windll.kernel32

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _ExtLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimit),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        hjob = k32.CreateJobObjectW(None, None)
        if not hjob:
            return False
        info = _ExtLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000   # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # JobObjectExtendedLimitInformation = 9
        if not k32.SetInformationJobObject(hjob, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(hjob)
            return False
        if not k32.AssignProcessToJobObject(hjob, int(proc._handle)):
            k32.CloseHandle(hjob)
            return False
        _JOB_HANDLE = hjob
        return True
    except Exception:
        return False


def kill_images(image_names):
    """按映像文件名（不带路径，不区分大小写）强杀进程。返回杀掉的 PID 数。
    用于清理本工具此前遗留的 ngrok.exe 孤儿（旧版本没有作业对象保护）。"""
    if not IS_WIN or not image_names:
        return 0
    wanted = {str(n).lower() for n in image_names}
    killed = 0
    for pid, name, _path in list_processes(with_path=False):
        if str(name or "").lower() in wanted:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10)
                killed += 1
            except Exception:
                pass
    return killed


# ---- 原生文件选择框（2026-09-04 用户反馈：网页手填估价器路径太麻烦）----
# 用 comdlg32.GetOpenFileNameW 弹 Windows 原生「打开文件」对话框，零第三方依赖。
# 刻意不用 tkinter.filedialog：Nuitka 打 tkinter 要开 tk-inter 插件，onefile 体积
# +10~20MB 且是打包翻车重灾区（见防踩坑指南·构）。

_OFN_FILEMUSTEXIST = 0x00001000
_OFN_HIDEREADONLY = 0x00000004


def native_pick_exe_file(title="选择程序", initial_dir=None):
    """弹出系统文件选择框，返回选中的完整路径（绝对路径）；取消/失败返回 ""。

    可在任意线程调用（含 ThreadingHTTPServer 的 HTTP 工作线程）：对话框自带
    模态消息循环。注意调用会阻塞该线程直到用户选择/取消，调用方需自行接受。
    """
    if not IS_WIN:
        return ""

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            # 2000+ 扩展字段（钩子/占位符需要），Win32 文档要求按此完整结构
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    buf = ctypes.create_unicode_buffer(2048)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = None
    # 双 null 结尾的 filter 对：显示文本 \0 模式 \0 …… 末尾再补一个 \0
    ofn.lpstrFilter = "程序文件 (*.exe)\x00*.exe\x00所有文件 (*.*)\x00*.*\x00"
    ofn.lpstrFile = buf
    ofn.nMaxFile = len(buf)
    ofn.lpstrInitialDir = str(initial_dir) if initial_dir else None
    ofn.lpstrTitle = str(title) if title else None
    ofn.Flags = _OFN_FILEMUSTEXIST | _OFN_HIDEREADONLY

    try:
        ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    except Exception:
        return ""
    if not ok:
        return ""
    return os.path.abspath(buf.value)
