#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
net_audit.py —— 华为 VRP 设备批量配置采集与合规巡检

背景：
    公司里十几台网络设备，靠人手一台台连上去敲 display、肉眼比对，既慢又容易漏。
    这个脚本做三件事：
        1. 批量登录设备，采集关键命令输出，按设备名归档
        2. 从配置里解析出「接口地址 / DHCP 地址池 / NAT 的 ACL」三组关键参数
        3. 按三条规则自动比对，把不一致的地方标出来

两种接入方式（在 devices.csv 里用 transport 列选择）：
    console : 连本机 TCP 端口，对应 eNSP 给每台设备开的控制台通道
              例：LSW1,127.0.0.1,2001,console
    ssh     : 标准 SSH，用于真机或带管理网段的实验环境
              例：AR1,10.0.0.2,22,ssh,admin,Admin@123

    这么设计的原因是：实验室里搭管理网段很麻烦（要 Cloud 桥接、要开 SSH、要配路由），
    而 eNSP 天然把每台设备的 console 映射到 127.0.0.1:200x。
    把「接入方式」抽象出来，同一套解析和巡检逻辑就能同时服务两种场景。

为什么 SSH 要用 invoke_shell 而不是 exec_command：
    华为 VRP 不支持 SSH 的 exec channel（exec_command 会直接报错），
    必须开交互式 shell，并且登录后立刻发送 screen-length 0 temporary 关闭分页，
    否则输出会卡在 "---- More ----" 上，永远等不到结尾。

用法：
    python net_audit.py                      # 采集全部设备 + 巡检
    python net_audit.py --only LSW1          # 只跑一台
    python net_audit.py --csv my_devices.csv # 指定设备清单
"""

import argparse
import csv
import ipaddress
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT_DIR = BASE / "outputs"

# 每台设备上要采集的命令（顺序执行，输出按命令名归档）
COMMANDS = [
    ("version",    "display version"),
    ("interfaces", "display ip interface brief"),
    ("routing",    "display ip routing-table"),
    ("config",     "display current-configuration"),
]


# ================================================================ 接入层
# 两种连接方式对外暴露同一套接口：send() / read_idle() / close()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def clean_console(text):
    """清掉分页留下的垃圾。

    控制台线路不接受 screen-length 0 temporary（只对 VTY 生效），
    所以长输出必然出现 "---- More ----"，脚本边翻页边清理：
    去掉 ANSI 光标控制符、More 标记、以及被覆盖区域留下的填充空格。
    """
    text = ANSI_RE.sub("", text)
    text = text.replace("---- More ----", "")
    lines = [re.sub(r"^[ ]{2,}", " ", l) for l in text.split("\n")]
    return "\n".join(lines)

class ConsoleLink:
    """eNSP 控制台通道：一个裸 TCP 连接，收到的是设备串口输出的原始字符。"""

    def __init__(self, host, port, timeout=10):
        self.sock = socket.create_connection((host, int(port)), timeout)
        self.sock.settimeout(0.3)
        self._wait_prompt(5)                          # 等登录提示符真正出现，再发命令
        self.send("screen-length 0 temporary")        # 关闭分页
        self.read_idle(idle=0.4)

    def _wait_prompt(self, seconds=5):
        """等设备吐出登录提示符（<Huawei> / [Huawei]）。

        不这样做的话，连接刚建立就发命令，会撞上设备的 "Press ENTER" 阶段，
        出现 Unrecognized command 之类的假报错。
        """
        buf = ""
        end = time.time() + seconds
        while time.time() < end:
            try:
                d = self.sock.recv(16384)
                if d:
                    buf += d.decode("utf-8", "ignore")
                    if re.search(r"[<\[][\w\-.]+[>\]]\s*$", buf.replace("\r", "")):
                        return buf
            except socket.timeout:
                continue
            except OSError:
                break
        return buf

    def send(self, text):
        self.sock.sendall(text.encode("utf-8") + b"\r\n")

    def read_idle(self, idle=0.7, limit=30):
        buf = b""
        last = time.time()
        start = time.time()
        while True:
            if time.time() - start > limit:
                buf += b"\n[!] read timeout"
                break
            try:
                d = self.sock.recv(16384)
                if d:
                    buf += d
                    last = time.time()
                    # 分页没关掉时输出会停在 ---- More ----，补一个空格继续翻页
                    if b"---- More ----" in d:
                        self.sock.sendall(b" ")
                        start = time.time()
                    continue
                break                                 # 对端关闭
            except socket.timeout:
                if time.time() - last > idle:
                    break
            except OSError:
                break
        return clean_console(buf.decode("utf-8", "ignore").replace("\r", ""))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class SshLink:
    """标准 SSH 通道，走 paramiko 的交互式 shell。"""

    def __init__(self, host, port, user, password, timeout=10):
        try:
            import paramiko
        except ImportError:
            raise RuntimeError("缺少依赖：pip install paramiko")
        self.client = paramiko.SSHClient()
        # 实验环境图省事；生产上应该用 known_hosts 校验，别照抄这一行
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=host, port=int(port), username=user, password=password,
            timeout=timeout, look_for_keys=False, allow_agent=False,
        )
        self.chan = self.client.invoke_shell()
        self.chan.settimeout(timeout)
        self.read_idle(idle=1.0)
        self.send("screen-length 0 temporary")
        self.read_idle(idle=0.5)

    def send(self, text):
        self.chan.send(text + "\n")

    def read_idle(self, idle=0.7, limit=30):
        buf = ""
        last = time.time()
        start = time.time()
        while True:
            if self.chan.recv_ready():
                buf += self.chan.recv(65535).decode("utf-8", "ignore")
                last = time.time()
            elif time.time() - last > idle:
                break
            elif time.time() - start > limit:
                buf += "\n[!] read timeout"
                break
            else:
                time.sleep(0.1)
        return buf

    def close(self):
        self.client.close()


def connect(dev, timeout=10):
    """按 devices.csv 里的 transport 列建立连接。"""
    kind = (dev.get("transport") or "ssh").strip().lower()
    if kind == "console":
        return ConsoleLink(dev["host"], dev["port"], timeout)
    if kind == "ssh":
        return SshLink(dev["host"], dev["port"], dev["user"], dev["password"], timeout)
    raise ValueError(f"不支持的接入方式：{kind}")


def run_command(link, cmd):
    """发一条命令，回收输出，去掉回显的首行。"""
    link.send(cmd)
    raw = link.read_idle()
    lines = raw.split("\n")
    if lines and cmd.split()[0] in lines[0]:
        lines = lines[1:]
    return "\n".join(lines).strip()


# ================================================================ 采集层

def collect(dev, timeout=10):
    """采集一台设备的全部命令输出。"""
    result = {"name": dev["name"], "host": f'{dev["host"]}:{dev["port"]}',
              "ok": False, "error": "", "data": {}}
    link = None
    try:
        link = connect(dev, timeout)
        for key, cmd in COMMANDS:
            result["data"][key] = run_command(link, cmd)
        result["ok"] = True
    except Exception as e:                            # 单台失败不影响其余设备
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        if link:
            link.close()
    return result


# ================================================================ 解析层

def parse_pools(config):
    """从完整配置里解析地址池：{vlan10: {'network': ..., 'gateway': ...}}"""
    pools = {}
    current = None
    for line in config.splitlines():
        s = line.strip()
        m = re.match(r"ip pool (\S+)", s)
        if m:
            current = m.group(1)
            pools[current] = {"network": "", "gateway": ""}
            continue
        if current:
            m = re.match(r"network\s+([\d.]+)\s+mask\s+([\d.]+)", s)
            if m:
                pools[current]["network"] = f"{m.group(1)}/{m.group(2)}"
            m = re.match(r"gateway-list\s+([\d.]+)", s)
            if m:
                pools[current]["gateway"] = m.group(1)
            if s.startswith("interface ") or s.startswith("acl "):
                current = None
    return pools


def parse_vlanif_ip(config):
    """从完整配置里解析 Vlanif 地址：{10: '192.168.10.254/255.255.255.0'}"""
    out = {}
    current = None
    for line in config.splitlines():
        s = line.strip()
        m = re.match(r"interface Vlanif(\d+)", s)
        if m:
            current = int(m.group(1))
            continue
        if current is not None:
            m = re.match(r"ip address\s+([\d.]+)\s+([\d.]+)", s)
            if m:
                out[current] = f"{m.group(1)}/{m.group(2)}"
            elif s.startswith("interface ") or s == "#":
                current = None
    return out


def parse_nat_acl(config, acl_no=2000):
    """解析 NAT 引用的基本 ACL，返回被允许做地址转换的网段列表。"""
    nets = []
    in_acl = False
    for line in config.splitlines():
        s = line.strip()
        if re.match(rf"acl number {acl_no}\b", s):
            in_acl = True
            continue
        if in_acl:
            if s.startswith("acl ") or s.startswith("interface "):
                in_acl = False
                continue
            m = re.match(r"rule \d+ permit source ([\d.]+) ([\d.]+)", s)
            if m:
                try:
                    # ACL 里写的是反掩码（0.0.255.255），先换算成前缀长度
                    mask_int = int(ipaddress.IPv4Address(m.group(2)))
                    prefix = 32 - bin(mask_int).count("1")
                    nets.append(ipaddress.ip_network(f"{m.group(1)}/{prefix}", strict=False))
                except ValueError:
                    pass
    return nets


# ================================================================ 巡检层

def check(devices_data):
    """三条规则，返回 [(级别, 设备, 规则, 说明)]。

    注意 R2 是跨设备比对：NAT 的 ACL 在出口路由器上，地址池在核心交换机上，
    所以先把全网的 ACL 网段汇总起来，再用它去校验每台设备的地址池。
    """
    issues = []

    global_acl = []
    for d in devices_data.values():
        global_acl.extend(parse_nat_acl(d.get("config", "")))

    for name, d in devices_data.items():
        config = d.get("config", "")
        if not config:
            continue
        vlanifs = parse_vlanif_ip(config)
        pools = parse_pools(config)

        # 规则一：地址池网关必须等于对应 Vlanif 的地址
        for pool, info in pools.items():
            if not info["network"] or not info["gateway"]:
                continue
            pool_net = ipaddress.ip_interface(info["network"]).network
            matched = None
            for vid, addr in vlanifs.items():
                if ipaddress.ip_interface(addr).network == pool_net:
                    matched = (vid, addr.split("/")[0])
                    break
            if not matched:
                issues.append(("警告", name, "地址池无对应 Vlanif",
                               f"{pool} 的网段 {info['network']} 找不到匹配的三层接口"))
                continue
            if matched[1] != info["gateway"]:
                issues.append(("严重", name, "网关不一致",
                               f"{pool}: gateway-list = {info['gateway']}，"
                               f"但 Vlanif{matched[0]} 实际地址是 {matched[1]}"))

        # 规则二：地址池网段必须被 NAT 的 ACL 覆盖（跨设备比对）
        for pool, info in pools.items():
            if not info["network"] or not global_acl:
                continue
            net = ipaddress.ip_interface(info["network"]).network
            if not any(net.subnet_of(a) for a in global_acl if net.version == a.version):
                issues.append(("严重", name, "NAT 未覆盖该网段",
                               f"{pool} 的网段 {net} 不在 NAT ACL 的允许范围内，"
                               f"该网段访问外网会失败（表现为请求超时）"))

        # 规则三：有地址池的设备必须有一条默认路由
        routing = d.get("routing", "")
        if pools and "0.0.0.0/0" not in routing:
            issues.append(("严重", name, "缺少默认路由",
                           "路由表中没有 0.0.0.0/0，内网无法访问外网"))

    return issues


# ================================================================ 主流程

def load_devices(path, only=None):
    with open(path, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("host")]
    if only:
        rows = [r for r in rows if r["name"].lower() == only.lower()]
    return rows


def write_report(results, issues, stamp):
    lines = ["# 网络设备巡检报告\n", f"生成时间：{stamp}\n", "## 一、采集结果\n",
             "| 设备 | 地址 | 状态 |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['host']} | "
                     f"{'成功' if r['ok'] else '失败：' + r['error']} |")

    lines.append("\n## 二、巡检发现\n")
    if not issues:
        lines.append("未发现问题。")
    else:
        lines.append("| 级别 | 设备 | 规则 | 说明 |")
        lines.append("|---|---|---|---|")
        for lvl, name, rule, msg in issues:
            lines.append(f"| {lvl} | {name} | {rule} | {msg} |")
    (OUT_DIR / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="华为 VRP 设备批量采集与合规巡检")
    ap.add_argument("--csv", default=str(BASE / "devices.csv"), help="设备清单（默认 devices.csv）")
    ap.add_argument("--only", help="只采集指定设备名")
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--workers", type=int, default=4, help="并发数（设备多时有用）")
    args = ap.parse_args()

    devices = load_devices(args.csv, args.only)
    if not devices:
        sys.exit(f"没有读到设备清单，检查路径：{args.csv}")

    OUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] 待采集设备：{', '.join(d['name'] for d in devices)}\n")

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(collect, d, args.timeout): d for d in devices}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            flag = "OK " if r["ok"] else "FAIL"
            print(f"  [{flag}] {r['name']:<8} {r['host']:<22} {r['error']}")

    for r in results:
        if not r["ok"]:
            continue
        for key, text in r["data"].items():
            (OUT_DIR / f"{r['name']}_{key}.txt").write_text(text, encoding="utf-8")

    data = {r["name"]: r["data"] for r in results if r["ok"]}
    issues = check(data)

    print(f"\n巡检结果：{len(issues)} 项发现")
    for lvl, name, rule, msg in issues:
        print(f"  [{lvl}] {name} · {rule}\n         {msg}")

    write_report(results, issues, stamp)
    print(f"\n原始输出与报告已写入：{OUT_DIR}")


if __name__ == "__main__":
    main()
