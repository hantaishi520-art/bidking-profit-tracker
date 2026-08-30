#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键重新解析 Player.log -> result.json （修复版，不再依赖有缺陷的 .exe）
用法：直接运行  python run_parser.py
"""
import subprocess, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
# BidKing 日志默认路径（如游戏装在别的盘，请自行修改下面这一行）
LOG = os.path.join(os.path.expanduser("~"), "AppData", "LocalLow", "laolin", "BidKing", "Player.log")
PARSER = os.path.join(HERE, "bidking_parser.py")
CSV = os.path.join(HERE, "item_prices.csv")
OUT = os.path.join(HERE, "result.json")

if not os.path.exists(LOG):
    print("未找到 Player.log：", LOG)
    print("请把 LOG 路径改为你本机实际的 Player.log 位置后重试。")
    sys.exit(1)

print("正在解析：", LOG)
rc = subprocess.run([sys.executable, PARSER, LOG, "auto", CSV, OUT])
if rc.returncode == 0:
    print("\n完成！已生成", OUT)
    print("接下来：运行  python serve.py  然后在浏览器打开提示的地址查看报表。")
sys.exit(rc.returncode)
