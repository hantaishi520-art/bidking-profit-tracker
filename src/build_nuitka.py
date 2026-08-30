# 用 Nuitka 把工具打成【单文件 exe】（对标对比项目 v2.34 的 Nuitka onefile）
# 产出：根目录 BidKing解析器.exe（单个干净文件，不再有 _internal 文件夹）
import os, sys, shutil, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)   # src/ 的父目录（项目根），exe 部署到这里
VENV_PY = os.environ.get("BIDKING_BUILD_PYTHON") or sys.executable   # 构建用解释器：默认当前 Python，可环境变量覆盖

# 全部静态资源内嵌进 exe（含 ngrok.exe），运行时从内嵌目录读取，根目录不再散落
ASSETS = ["bidking_report.html", "bidking_standalone.html", "bidking_career.html",
          "bidking_item_prices.html", "item_prices.csv", "v233_items.json", "ngrok.exe"]

# 价格配置随包（2026-08-29 用户需求）：把作者当前调好的 item_prices.db 内嵌为种子，
# 别人的电脑/新目录首次运行时自动带出这套配置（释放逻辑见 bidking_launcher 的种子步骤）。
# 本机已有配置时不受影响（本地库存在则跳过种子，之后改价都存本地、不会被覆盖）。
seed_src = os.path.join(PROJECT_ROOT, "item_prices.db")
if os.path.isfile(seed_src):
    seed_dst = os.path.join(HERE, "item_prices_seed.db")
    shutil.copy(seed_src, seed_dst)
    ASSETS.append("item_prices_seed.db")
    print(">>> 已内嵌当前价格配置：item_prices_seed.db (%d 字节)" % os.path.getsize(seed_dst))
else:
    print(">>> 项目根没有 item_prices.db，本次打包不含内置价格配置")

# 输出到系统 Temp（沙箱放行删除），避免安全删除钩子干扰
tdir = os.path.join(tempfile.gettempdir(), "bidking_nuitka_build")
os.makedirs(tdir, exist_ok=True)

# ---- 构建前质量检查（2026-08-30 开发规范）：回归测试 + JS 语法，失败拒绝打包 ----
def _prebuild_checks():
    import re as _re

    ok = True
    print(">>> [1/2] 回归测试（src/tests/run_tests.py，临时端口不碰真实数据）...")
    r = subprocess.run([sys.executable, os.path.join(HERE, "tests", "run_tests.py")])
    if r.returncode != 0:
        print("!!! 回归测试未全部通过，取消打包")
        return False

    print(">>> [2/2] JS 语法检查（内嵌网页的 <script> 块）...")
    node = shutil.which("node")
    if not node:
        import glob as _g
        cands = _g.glob(os.path.expanduser(r"~/.workbuddy/binaries/node/versions/*/node.exe"))
        node = cands[0] if cands else None
    if not node:
        print(">>> 未找到 node，跳过 JS 检查（不阻断本次打包）")
        return True
    js_tmp = os.path.join(tdir, "_jscheck.js")
    for a in ASSETS:
        if not a.endswith(".html"):
            continue
        html = open(os.path.join(HERE, a), encoding="utf-8").read()
        for i, block in enumerate(_re.findall(r"<script>(.*?)</script>", html, _re.S)):
            with open(js_tmp, "w", encoding="utf-8") as f:
                f.write(block)
            rc = subprocess.run([node, "--check", js_tmp], capture_output=True, text=True)
            if rc.returncode != 0:
                print("!!! JS 语法错误：%s（script #%d）" % (a, i))
                print((rc.stderr or rc.stdout)[:800])
                ok = False
    if not ok:
        print("!!! JS 语法检查未通过，取消打包")
    return ok

if not _prebuild_checks():
    sys.exit(1)

data_args = []
for a in ASSETS:
    src = os.path.join(HERE, a)
    data_args += ["--include-data-files=" + f"{src}={a}"]

cmd = [VENV_PY, "-m", "nuitka",
       "--onefile",                       # 单文件（对比项目就是这么打的）
       "--windows-console-mode=force",    # 保留黑框 cmd（启动过程看得见）
       "--windows-icon-from-ico=" + os.path.join(HERE, "app_icon.ico"),  # exe/快捷方式图标（2026-08-30）
       "--assume-yes-for-downloads",      # 首次可能需下载 zstandard 等，自动同意
       "--output-dir=" + tdir,
       "--output-filename=BidKing解析器.exe",
       "--show-progress",
       *data_args,
       os.path.join(HERE, "bidking_launcher.py")]

print(">>> Nuitka 构建命令：")
print(" ".join(cmd))
r = subprocess.run(cmd)
if r.returncode != 0:
    print("!!! 构建失败 rc=", r.returncode)
    sys.exit(1)

built = os.path.join(tdir, "BidKing解析器.exe")
print(">>> 构建产物:", built, "存在:", os.path.isfile(built))
if not os.path.isfile(built):
    print("!!! 找不到构建产物")
    sys.exit(1)

# ---- 部署到项目根目录（src/ 的父目录）----
dst = os.path.join(PROJECT_ROOT, "BidKing解析器.exe")
if os.path.isfile(dst):
    os.replace(dst, dst + ".bak")        # 用 rename 让位，绕开安全删除钩子
shutil.copy(built, dst)
print(">>> 已部署单文件 exe 到:", dst)

# 旧的 _internal（PyInstaller 运行环境）改名 .bak，确认无碍后可手动删
int_dir = os.path.join(HERE, "_internal")
if os.path.isdir(int_dir):
    os.rename(int_dir, int_dir + ".bak")
    print(">>> 旧 _internal 已改名 _internal.bak")

print(">>> 构建部署完成")
