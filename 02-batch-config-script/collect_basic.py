"""
最小可运行的设备配置采集脚本

从 eNSP 的四台设备批量采集命令输出，按「设备_命令别名.txt」存盘。

前提：eNSP 里每台设备的 CLI 窗口必须保持打开。
      关掉窗口后端口仍在 LISTEN，但后端没有 console 会话，读到的全是 0 字节。

用法：
    python collect_basic.py

说明：这是本项目的第 1 版，只做「发 → 读 → 写」三件事，不含任何巡检规则。
      带规则检查的完整版见 net_audit.py。
"""

import socket
import time
from pathlib import Path

devices = {"LSW1": 2001, "LSW2": 2002, "LSW3": 2003, "AR1": 2004}
cmds = {"version": "display version", "interfaces": "display ip interface brief"}


def read_idle(sock, idle=1.0):
    """一直读到连续 idle 秒没有新数据为止"""
    buf = b""
    last = time.time()
    while True:
        try:
            chunk = sock.recv(4096)
            if chunk:
                buf += chunk
                last = time.time()
        except socket.timeout:
            if time.time() - last > idle:
                break
    return buf.decode("utf-8", "ignore").replace("\r", "*")


Path("outputs").mkdir(exist_ok=True)

for name, port in devices.items():
    try:
        sock = socket.socket()
        sock.connect(("127.0.0.1", port))
        sock.settimeout(0.3)

        for alias, cmd in cmds.items():
            sock.sendall(cmd.encode() + b"\r\n")
            time.sleep(0.6)
            text = read_idle(sock)

            path = Path("outputs") / f"{name}_{alias}.txt"
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

            if len(text) < 20:
                print(f"  [WARN] {name:<6} {alias:<11} 只读到 {len(text)} 字符，设备可能没响应")
            else:
                print(f"  [OK]   {name:<6} {alias:<11} {len(text):>5} 字符")

        sock.close()
    except Exception as e:
        print(f"  [FAIL] {name}: {type(e).__name__} {e}")
