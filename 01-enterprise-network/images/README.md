# 截图清单

建议按下面的顺序截图，文件名保持一致（README 里会引用）。

## 一、拓扑

| 文件名 | 内容 |
|---|---|
| `topology.png` | eNSP 工作区全图，能看清 4 台交换机 / 1 台路由器 / 各终端和设备接口编号 |

> 截图技巧：点 eNSP 空白处取消选中，然后用 `Ctrl + 滚轮` 缩放到能看清全部设备。

## 二、验证通过的证据（重要）

| 文件名 | 命令 / 操作 | 说明 |
|---|---|---|
| `01-ping-internet.png` | PC1 上 `ping 200.1.1.2` | 内网访问外网成功，证明 NAT 生效 |
| `02-ping-acl-deny.png` | PC3 上 `ping 192.168.99.1` | 被 ACL 拒绝，证明访客隔离生效 |
| `03-dhcp-ipconfig.png` | PC1 上 `ipconfig` | 看到自动获取的 192.168.10.x 和网关 .254 |
| `04-ospf-peer.png` | `display ospf peer brief` | State = Full |
| `05-routing-table.png` | `display ip routing-table` | 能看到 `0.0.0.0/0 OSPF` 这一行 |
| `06-nat-session.png` | `display nat session all` | 存在转换表项 |

## 三、故障排查过程截图（最有价值）

| 文件名 | 命令 | 对应故障 |
|---|---|---|
| `07-vlanif-down.png` | `display ip interface brief`（Vlanif99 为 down 时） | 故障一：现象 |
| `08-port-vlan.png` | `display port vlan active`（LSW1，Trunk 缺 VLAN 99） | 故障一：定位 |
| `09-routing-all-direct.png` | `display ip routing-table`（12 条全 Direct） | 故障二：现象 |
| `10-ospf-lsdb-ase.png` | `display ospf lsdb ase`（AR1，Type-5 LSA） | 故障二：验证 |

> 这些"故障现场"的截图比成功截图更有说服力——它证明你真的遇到过问题并解决了。

---

## 命名规范

```
序号-内容.png
```

序号方便在 README 里按顺序引用。截图前先想清楚"这张要证明什么"，避免截一堆无意义的图。
