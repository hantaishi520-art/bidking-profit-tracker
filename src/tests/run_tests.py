# 一键回归（2026-08-30 开发规范）：跑全部源码级自测。
# 全部使用临时目录 + 8799 端口，不碰真实用户数据；跑之前确保 8799 空闲。
# 真 exe 相关的验收见 manual_*.py（需先在 8766 起一个真 exe，属发布前手动步骤）。
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = ["test_auth_peer.py", "test_launch_gate.py", "test_public_gate.py"]


def main():
    fails = []
    for t in TESTS:
        print("=" * 62, flush=True)
        print("RUN " + t, flush=True)
        r = subprocess.run([sys.executable, os.path.join(HERE, t)])
        if r.returncode != 0:
            fails.append(t)
    print("=" * 62, flush=True)
    if fails:
        print("FAILED: " + ", ".join(fails), flush=True)
        sys.exit(1)
    print("ALL PASS (%d tests)" % len(TESTS), flush=True)


if __name__ == "__main__":
    main()
