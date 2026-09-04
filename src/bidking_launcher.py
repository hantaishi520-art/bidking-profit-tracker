#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BidKing 解析器（exe 入口）
双击即用：自动定位 Player.log -> 解析(双数据源) -> 启动本地服务器 -> 打开富报表。
也支持把 Player.log 拖到本程序上（作为第一个参数）。
"""
import os, sys, shutil, threading, time, webbrowser, json, hashlib

# 运行时需要随 exe 一起落到工作目录的资源（报表/价格表/道具名）
# 必须在 FROZEN 块之前定义，否则冻结成 exe 启动时会因 ASSETS 未定义而闪退
ASSETS = ["bidking_report.html", "bidking_standalone.html", "bidking_career.html",
          "bidking_item_prices.html", "item_prices.csv", "v233_items.json"]

# APP_DIR：真实部署目录（result.json / career.db / silver_cache.json 等都写到这里）。
# 必须始终用 sys.argv[0] 解析——Nuitka onefile 下 sys.executable 指向临时解压目录，
# 而 Windows 启动时会把用户实际放置 exe 的真实路径放进 argv[0]。
# （注意：不能依赖 sys.frozen 判断，因为 Nuitka onefile 不会设置 sys.frozen，
#  只有 PyInstaller 才会；统一用 argv[0] 在开发/各种打包模式下都正确。）
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))

# RES_DIR：随包资源（html / csv / json）所在目录，按候选顺序探测第一个命中者：
#   1) 直接运行脚本 或 Nuitka onefile：__file__ 同目录（onefile 下为临时解压目录，资源已解压至此）
#   2) PyInstaller onefile：sys._MEIPASS
#   3) PyInstaller onedir：_internal 子目录
#   4) 兜底：APP_DIR 自身
_cands = [os.path.dirname(os.path.abspath(__file__)),
          getattr(sys, "_MEIPASS", ""),
          os.path.join(APP_DIR, "_internal"),
          APP_DIR]
RES_DIR = APP_DIR
for _c in _cands:
    if _c and os.path.isfile(os.path.join(_c, ASSETS[0])):
        RES_DIR = _c
        break

def pause_exit(msg=None):
    if msg:
        print(msg)
    try:
        input("按回车退出...")
    except Exception:
        pass

# ---- 端口占用处理（2026-09-04 用户反馈）----
# 之前端口被占直接报错退出，小白用户只能干瞪眼。现在改为：列出占用进程 →
# 控制台询问是否释放（y/n）→ 用户确认才 taskkill → 重试绑定。
# 注意：绝不静默杀进程；PID 来自 netstat -ano 解析（不用 cmd for /f，
# IPv6 行/中文列头下 tokens=5 会错位），杀之前必须让用户亲眼确认。
def _port_listen_pids(port):
    """返回正在 LISTENING 该 TCP 端口的进程 PID 列表（字符串）。"""
    import subprocess
    pids = set()
    try:
        # 中文 Windows 控制台工具输出是 GBK 编码，用默认 utf-8 解码会 UnicodeDecodeError
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10,
                             encoding="gbk", errors="replace").stdout or ""
    except Exception:
        return pids
    for line in out.splitlines():
        parts = line.split()
        # 形如: TCP  0.0.0.0:8766  0.0.0.0:0  LISTENING  1234
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            local = parts[1]
            if local.rsplit(":", 1)[-1] == str(port):
                pid = parts[-1]
                if pid.isdigit() and pid != "0":
                    pids.add(pid)
    return pids

def _pid_image_name(pid):
    """查 PID 的进程映像名，仅用于展示给用户确认，不参与自动判断。"""
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             encoding="gbk", errors="replace").stdout or ""
        line = out.strip().splitlines()[0] if out.strip() else ""
        if line.startswith('"'):
            return line.split('","')[0].strip('"')
    except Exception:
        pass
    return "(未知进程)"

def _try_free_port(port):
    """端口被占时在控制台询问用户。返回 True=已释放（或本来就没占住），False=用户拒绝/释放失败。"""
    import subprocess
    for _attempt in range(2):   # 询问最多两轮（杀完可能还有残留监听）
        pids = _port_listen_pids(port)
        if not pids:
            return True
        listing = "\n".join(f"  PID {p}  {_pid_image_name(p)}" for p in sorted(pids, key=int))
        print(f"\n端口 {port} 已被以下进程占用：\n{listing}")
        try:
            ans = input(f"是否结束上面这些进程并继续启动？(y=结束并继续 / n=退出)：").strip().lower()
        except Exception:
            ans = ""
        if ans != "y":
            return False
        for p in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", p],
                               capture_output=True, text=True, timeout=10)
            except Exception:
                pass
        time.sleep(1)   # 等 TIME_WAIT/句柄释放
    return not _port_listen_pids(port)

def main():
    import bidking_parser
    import serve
    print("BidKing解析器 v%s · %s" % (serve.APP_VERSION, serve.APP_SIGNATURE))
    # 内嵌静态资源目录（exe 模式=临时解压目录，源码模式=src/），供 serve/parser 读取网页/价格表/ngrok
    bidking_parser.RES_DIR = RES_DIR

    # ngrok 孤儿清理（2026-08-29 用户反馈）：上次直接关窗口时 ngrok 来不及带走、
    # 一直挂后台。启动时清一次，公网开关状态才和真实进程一致。
    try:
        serve._ngrok_kill_orphans()
    except Exception:
        pass

    # 1) 日志路径：拖拽参数优先，否则自动探测
    serve.init(APP_DIR, RES_DIR)   # 设定数据目录(APP_DIR)与资源目录(RES_DIR)

    # 价格配置随包（2026-08-29 用户需求）：打包时内嵌了作者调好的 item_prices.db
    # （种子名 item_prices_seed.db）。本目录**首次**运行（还没有本地价格库）时自动
    # 带出这套配置，实现"价格配置伴随 exe"。之后本地 item_prices.db 就是权威——
    # 作者/用户改价都存本地，种子只在库不存在时生效，永不覆盖已有配置。
    try:
        if RES_DIR != APP_DIR:   # 源码开发模式（src 即 APP_DIR）不做种子拷贝
            _seed = os.path.join(RES_DIR, "item_prices_seed.db")
            _local_db = os.path.join(APP_DIR, "item_prices.db")
            if os.path.isfile(_seed) and not os.path.isfile(_local_db):
                shutil.copy(_seed, _local_db)
                print("已内置作者调好的价格配置（item_prices.db）")
    except Exception as e:
        print("释放内置价格配置失败：", e)
    # 阶段3b：命令行入口（--prepare-share / --reset-security）。
    # 必须放在启动自检之前：prepare-share 本身就是清凭据，不该被自检的重置逻辑干扰。
    # 注：exe 走的是本文件而非 serve.py 的 __main__，所以这里必须显式处理。
    try:
        if serve._handle_cli_args(sys.argv[1:]):
            pause_exit("")
            return
    except Exception as e:
        print("命令行处理出错：", e)
    # 阶段3：启动自检 —— 换机/文件夹被拷贝时自动重置密码与配对（战绩数据不动），网页会显示黄条
    try:
        serve._boot_security_check()
    except Exception:
        pass   # 自检异常不阻断启动（宁可降级也不要卡死）
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        log = os.path.abspath(sys.argv[1])
    else:
        log = serve.find_log()

    if not log:
        pause_exit("未找到 Player.log。\n请把 Player.log 拖到本程序上，或放到本程序同目录/游戏默认目录后重试。")
        return

    print("找到日志：", log)
    print("正在启动本地服务器并打开报表...")

    PORT = 8766
    # 单实例保护（2026-08-29）：http.server 在 Windows 上允许重复绑定同一端口，
    # 双击两次 exe 会冒出多个实例同时监听 8766——新连接随机落到哪个实例，
    # 登录令牌/公网隧道状态就随机失效（表现为 App「时好时坏」的连接失败）。
    # 检测到已有实例在服务时，直接打开它的页面并退出，绝不再起第二个。
    import urllib.request as _ur
    try:
        with _ur.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1.5) as _r:
            if _r.status == 200:
                print(f"检测到 BidKing解析器 已在运行（端口 {PORT}），直接打开它的页面，不重复启动。")
                try:
                    webbrowser.open(f"http://127.0.0.1:{PORT}/bidking_report.html")
                except Exception:
                    pass
                print("如需完全重启，请先关闭旧的解析器窗口再双击。")
                time.sleep(2)
                return
    except Exception:
        pass   # 没有在跑的实例（或 ping 不通）→ 按正常流程启动

    try:
        srv = serve.make_server(PORT, APP_DIR)
    except OSError as e:
        # 端口被占：询问用户是否释放（2026-09-04 用户反馈），不再直接退出
        if not _try_free_port(PORT):
            pause_exit(f"端口 {PORT} 启动失败：{e}\n"
                       "该端口仍被占用。请关闭占用它的程序（或重启电脑）后再试。")
            return
        try:
            srv = serve.make_server(PORT, APP_DIR)
        except OSError as e2:
            pause_exit(f"端口 {PORT} 仍无法启动：{e2}\n请重启电脑后再试。")
            return
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 等服务器就绪再打开浏览器（否则可能空白/下载）
    import urllib.request
    for _ in range(20):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.3)

    url = f"http://127.0.0.1:{PORT}/bidking_report.html"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("已打开报表页面：", url)
    # 启动加速（2026-08-31）：页面先出，解析放后台跑。
    # 之前是先解析完（5.2GB 日志全扫）才开页面，导致等待约 1 分钟；
    # 现在页面秒出（先显示上一次的 result.json），后台解析完成后页面自动刷新。
    # 复用 serve.run_parse（现成后台线程：STATE 状态机 + 生涯库逐局落库 + 覆盖保护），
    # 语义与旧流程完全一致，只是挪进了后台线程。
    try:
        threading.Thread(target=serve.run_parse, args=("auto",), daemon=True).start()
    except Exception as e:
        print("后台解析启动失败：", e)
    print("页面已打开，正在后台解析（控制台可见进度）；解析完成后报表自动刷新，"
          "期间可继续查看上一次战绩。")
    print("关闭此窗口（或 Ctrl+C）即可退出；期间可点报表内「实时刷新」重新解析战绩。")

    # 公网开关联动退出（2026-08-29 用户反馈）：解析器退出时把 ngrok 一并带走，
    # 不再留后台进程。点窗口 X 触发 CTRL_CLOSE_EVENT → SIGBREAK。
    import atexit
    import signal as _signal

    def _kill_ngrok_on_exit(*_a):
        try:
            serve.ngrok_stop()
        except Exception:
            pass

    atexit.register(_kill_ngrok_on_exit)
    for _sig in ("SIGBREAK", "SIGINT", "SIGTERM"):
        try:
            _signal.signal(getattr(_signal, _sig), _kill_ngrok_on_exit)
        except Exception:
            pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在关闭...")
        _kill_ngrok_on_exit()
        srv.shutdown()

if __name__ == "__main__":
    main()
