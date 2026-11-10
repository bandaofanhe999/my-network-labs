#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_config.py —— 批量下发配置到华为 VRP 设备

配套 net_audit.py 使用：一个负责"读"（采集+巡检），一个负责"写"（下发配置）。

为什么单独写一个脚本、而不是直接复制粘贴到每台设备：
    1. 一致性 —— 四台设备用同一份配置文件下发，不会出现"这台漏了一行"
    2. 可追溯 —— 每次下发都留日志，出问题能查到哪一行没进去
    3. 可重复 —— 实验重做时一条命令恢复环境

安全提醒：
    生产环境的下发脚本必须带「先备份、再预览 diff、失败可回滚」三件套，
    这个版本是实验用的最小实现，只适合 eNSP 这类可随时重置的环境。

用法：
    python push_config.py                    # 按 devices.csv + config/ 下发全部
    python push_config.py --only LSW1        # 只下发一台
    python push_config.py --dry-run          # 只打印将要下发的行，不连设备
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from net_audit import connect, load_devices   # 复用接入层，不重复造轮子

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "outputs"


def read_config(path):
    """读配置文件，跳过注释和空行。"""
    lines = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        s = raw.rstrip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    return lines


def push(dev, lines, delay=0.12):
    """把若干行配置下发到一台设备，返回 (是否成功, 错误信息列表, 日志文本)。"""
    log = []
    errors = []
    link = None
    try:
        link = connect(dev, timeout=10)
        link.send("system-view")
        log.append(link.read_idle(idle=0.5))

        for i, line in enumerate(lines, 1):
            link.send(line)
            time.sleep(delay)
            if i % 8 == 0:                       # 定期排空缓冲，避免设备回显把小 buffer 塞满
                log.append(link.read_idle(idle=0.2))
        log.append(link.read_idle(idle=1.0))

        link.send("return")
        log.append(link.read_idle(idle=0.5))

        text = "\n".join(log)
        # VRP 命令出错时会在回显里出现 Error / ^ 指示符
        for l in text.splitlines():
            if "Error" in l or "Unrecognized" in l or "Wrong parameter" in l:
                errors.append(l.strip())

        # 保存配置，让设备重启后依然生效
        link.send("save")
        time.sleep(0.8)
        link.read_idle(idle=0.6)
        link.send("y")
        time.sleep(1.5)
        log.append(link.read_idle(idle=1.0))

        return (len(errors) == 0), errors, "\n".join(log)
    except Exception as e:
        return False, [f"{type(e).__name__}: {e}"], "\n".join(log)
    finally:
        if link:
            link.close()


def main():
    ap = argparse.ArgumentParser(description="批量下发配置到华为 VRP 设备")
    ap.add_argument("--csv", default=str(BASE / "devices.csv"))
    ap.add_argument("--config-dir", default=str(BASE.parent / "01-enterprise-network" / "config"))
    ap.add_argument("--only", help="只下发指定设备")
    ap.add_argument("--delay", type=float, default=0.12, help="每行之间的间隔（秒）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要下发的行")
    args = ap.parse_args()

    devices = load_devices(args.csv, args.only)
    if not devices:
        sys.exit(f"没有读到设备清单：{args.csv}")

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] 配置下发开始\n")

    total_err = 0
    for dev in devices:
        cfg_path = Path(args.config_dir) / f"{dev['name']}.txt"
        if not cfg_path.exists():
            print(f"  [SKIP] {dev['name']:<8} 找不到配置文件 {cfg_path}")
            continue
        lines = read_config(cfg_path)
        print(f"  [{dev['name']}] {cfg_path.name} 共 {len(lines)} 行")

        if args.dry_run:
            for l in lines[:8]:
                print(f"        {l}")
            print("        ...（dry-run，未连接设备）\n")
            continue

        ok, errors, log = push(dev, lines, args.delay)
        (OUT_DIR / f"push_{dev['name']}.log").write_text(log, encoding="utf-8")

        if ok:
            print(f"        下发完成，无报错\n")
        else:
            total_err += len(errors)
            print(f"        发现 {len(errors)} 处报错：")
            for e in errors[:10]:
                print(f"          - {e}")
            print()

    if not args.dry_run:
        print(f"完成。报错合计 {total_err} 处，日志在 {OUT_DIR}")


if __name__ == "__main__":
    main()
