# 02 · 网络设备批量采集与合规巡检脚本

用 Python + SSH 批量登录多台华为设备，自动采集配置、解析关键参数、比对一致性，把"人工逐台上机敲 display 再肉眼对比"变成一条命令。

配套实验环境见 [`../01-enterprise-network/`](../01-enterprise-network/)（VLAN / DHCP / OSPF / NAT 的企业网拓扑）。

---

## 一、要解决什么问题

企业里设备一多，有两类工作特别耗人：

| 场景 | 手工做法 | 脚本做法 |
|---|---|---|
| 改完配置要核对十几台设备的版本、接口、路由 | 一台台 telnet，几十次 `display` | 一条命令，输出全部归档 |
| 检查「DHCP 地址池网关」是否和「Vlanif 实际地址」一致 | 逐台比对，眼睛看花还容易漏 | 自动比对，不一致直接标红 |

第二类在运维里叫**配置合规性检查（compliance check）**，是银行、运营商等行业的硬性要求。

---

## 二、巡检的三条规则

这三条不是我编的，是这个实验里真实踩过的三个坑：

| 规则 | 检查什么 | 踩过的坑 |
|---|---|---|
| **R1** 网关一致性 | 地址池的 `gateway-list` 必须等于对应 `Vlanif` 的地址 | 改了地址池忘了改接口（或反过来），终端能 ping 通网关却上不了网 |
| **R2** NAT 覆盖范围 | 每个地址池的网段必须被 NAT 引用的 ACL 覆盖 | ACL 只写了 VLAN 10，导致 VLAN 20/30 的包以私网源地址发出，表现为"请求超时"而不是"不可达" |
| **R3** 默认路由存在 | 有地址池的设备，路由表里必须有 `0.0.0.0/0` | OSPF 没下发默认路由，内网 12 条路由全是 Direct |

> 这三条的共同点：**配置语法全对，设备也不报错，但业务不通。** 靠人眼看配置是查不出来的，必须做交叉比对。

---

## 三、两种接入方式

脚本把「怎么连设备」和「连上之后干什么」拆开了，所以同一套采集/巡检逻辑可以接两种通道（在 `devices.csv` 的 `transport` 列选）：

| 方式 | 用在哪 | 写法 |
|---|---|---|
| `console` | **eNSP 实验室（推荐，零配置）** | `LSW1,127.0.0.1,2001,console` |
| `ssh` | 真机、或已搭好管理网段的实验环境 | `AR1,10.0.0.2,22,ssh,admin,密码` |

### 3.1 console 方式：eNSP 自带，不用 Cloud

eNSP 每启动一台设备，就会在本机开一个 TCP 端口，把设备的 console 串口映射出来：

```
LSW1 → 127.0.0.1:2001     LSW3 → 127.0.0.1:2003
LSW2 → 127.0.0.1:2002     AR1  → 127.0.0.1:2004
```

端口号记在拓扑文件里（`<dev ... com_port="2001">`），设备启动顺序变了也不会乱。

设备启动后直接跑 `python net_audit.py` 即可，**不需要 Cloud、不需要管理 VLAN、不需要开 SSH**。

> ⚠️ **前提：eNSP 里每台设备的 CLI 窗口必须保持打开。**
>
> 这条违反直觉，是实测出来的：**关掉窗口，脚本读到 0 字节；打开窗口，一切正常。**
>
> 原因是 eNSP 的 2001-2004 端口并非「设备的串口服务器」，而是 eNSP 客户端内部 telnet 会话的转发口。
> 双击设备打开 CLI 窗口时，客户端才建立一条真实的 console 会话并挂到该端口上；
> 没有窗口时端口依然 `LISTENING`（TCP 能连上），但后端没有会话，发什么都不回应。
>
> | 设备 CLI 窗口 | `connect()` | 发命令后收到 | 结果 |
> |---|---|---|---|
> | 打开 | 成功 | 有回显 | ✅ 正常采集 |
> | 关闭 | 成功 | 0 字节 | ❌ 生成空文件，且不报错 |
>
> 副作用：**窗口和脚本共用同一个 console**（输出是镜像的，不是独占）。
> 所以采集期间不要在那个窗口里手动敲命令，否则无关输出会混进采集结果。
>
> 正因如此，脚本对每条结果都做了长度校验——**读到内容过短会打印 `[WARN]`，而不是安安静静写入空文件**。
> 「没抛异常」不等于「采集成功」，这是运维脚本和练习脚本最大的区别。

> 这条路只适合实验室。真机没有这种通道，生产环境必须走 SSH——所以脚本保留了 SSH 实现。

### 3.2 为什么 SSH 那条路更容易踩坑

给 eNSP 配 Cloud 时，"无可用端口"这个报错非常常见，本质是**端口配置不完整**：只加了一个绑定网卡的端口，没有 UDP 端口和端口映射，Cloud 就不会生成可连线的 Ethernet 接口。

完整做法（照着做成功率最高）：

1. 绑定信息选 `UDP` → 点「增加」
2. 绑定信息选 `VirtualBox Host-Only Ethernet Adapter` → 点「增加」
3. 端口映射设置：出端口编号填 `2`，勾选「双向通道」→ 点「增加」
4. 此时端口映射表自动关联，Cloud 才生成 `Ethernet 0/0/1`，可以连线了

另外几个前置条件：eNSP 最好**以管理员身份运行**（否则读不到虚拟网卡列表）；Cloud 配置面板要**关掉之后**再拖线，否则端口状态不刷新。

### 3.3 SSH 方式的配置（真机 / 已搭好通道时）

脚本要能 SSH 到设备，前提是主机和设备在同一个二层网络里。本实验用 **VirtualBox Host-Only 网卡**（eNSP 装好后自带，IP 通常是 `192.168.56.1`）。

#### ① eNSP 侧：加一台 Cloud

1. 左侧设备栏拖入 **Cloud（云）**
2. 按 3.2 的四步把端口配齐（UDP 端口 + 网卡端口 + 端口映射）
3. 关掉配置面板，把 Cloud 的 `Ethernet0/0/1` 连到 **LSW1 的 GE0/0/3**

### 2. LSW1：加管理 VLAN 并把地址通告进 OSPF

```text
[LSW1] vlan batch 100
[LSW1] interface Vlanif100
[LSW1-Vlanif100] ip address 192.168.56.10 255.255.255.0
[LSW1-Vlanif100] quit
[LSW1] interface GigabitEthernet0/0/3
[LSW1-GigabitEthernet0/0/3] port link-type access
[LSW1-GigabitEthernet0/0/3] port default vlan 100
[LSW1-GigabitEthernet0/0/3] quit
[LSW1] ospf 1
[LSW1-ospf-1] area 0
[LSW1-ospf-1-area-0.0.0.0] network 192.168.56.0 0.0.0.255
[LSW1-ospf-1-area-0.0.0.0] quit
[LSW1-ospf-1] quit
```

> 最后那三行很关键：把管理网段也宣告进 OSPF，AR1 才能知道怎么把包回给 `192.168.56.0/24`。否则你能连上 LSW1，却连不上 AR1。

### 3. 两台设备：开启 SSH

```text
<设备> system-view
[设备] stelnet server enable
[设备] rsa local-key-pair create          # 提示时输入 2048
[设备] aaa
[设备-aaa] local-user admin password irreversible-cipher Admin@123
[设备-aaa] local-user admin privilege level 15
[设备-aaa] local-user admin service-type ssh
[设备-aaa] quit
[设备] user-interface vty 0 4
[设备-ui-vty0-4] authentication-mode aaa
[设备-ui-vty0-4] protocol inbound ssh
[设备-ui-vty0-4] quit
```

### 4. Windows：加一条静态路由（让本机能到 AR1）

管理员身份打开 PowerShell：

```powershell
route -p add 10.0.0.0 mask 255.255.255.252 192.168.56.10
```

`-p` 表示永久生效。加完验证：

```powershell
ping 192.168.56.10     # LSW1
ping 10.0.0.2          # AR1（经过 LSW1 转发）
```

两个都通，才说明管理通道建好了。

---

## 四、跑起来

项目里有两个入口，按需要选一个：

| 脚本 | 规模 | 作用 |
|---|---|---|
| `collect_basic.py` | 约 50 行 | **最小版本**：只做「发命令 → 读输出 → 存文件」，适合先看清基本流程 |
| `net_audit.py` | 约 280 行 | **完整版**：双通道接入 + 采集 + 三条巡检规则 + 生成报告 |

### 4.1 最小版本：先跑通采集

```bash
python collect_basic.py
```

```
  [OK]   LSW1   version       250 字符
  [OK]   LSW1   interfaces   1029 字符
  [OK]   LSW2   version       250 字符
  ...
```

核心只有三步，没有任何封装：

```python
sock.sendall(cmd.encode() + b"\r\n")   # 发
text = read_idle(sock)                 # 读（读到安静为止）
f.write(text)                          # 写
```

两个设计点值得注意：

- **`read_idle()` 用「安静 1 秒」判断输出结束**，而不是找结束符。因为设备是流式输出，没有「这批数据到此为止」的边界
- **每条结果都校验长度**，读到过短就打印 `[WARN]`。不加这一步的话，设备无响应时会安静地生成 8 个空文件，而脚本看起来「全部成功」

### 4.2 完整版本：带巡检规则

```bash
pip install -r requirements.txt

python net_audit.py                     # 全部设备
python net_audit.py --only LSW1         # 只跑一台
python net_audit.py --timeout 15        # 慢设备延长超时
```

设备清单在 `devices.csv`，加设备就加一行：

```csv
name,host,port,user,password
LSW1,192.168.56.10,22,admin,Admin@123
AR1,10.0.0.2,22,admin,Admin@123
```

输出：

```
[2026-11-10 13:20:04] 待采集设备：LSW1, AR1

  [OK ] LSW1     192.168.56.10
  [OK ] AR1      10.0.0.2

巡检结果：2 项发现
  [严重] LSW1 · 网关不一致
         vlan20: gateway-list = 192.168.20.1，但 Vlanif20 实际地址是 192.168.20.254
  [严重] AR1 · NAT 未覆盖该网段
         vlan20 的网段 192.168.20.0/24 不在 NAT ACL 的允许范围内...
```

结果落在 `outputs/`：每台设备 4 个原始输出文件 + 一份 `report.md`。

---

## 五、批量下发配置（push_config.py）

采集是"读"，`push_config.py` 是"写"——把 `01-enterprise-network/config/*.txt` 逐行灌进设备，用于环境重建。

```bash
python push_config.py --dry-run      # 只看会下发什么，不连设备
python push_config.py --only LSW1    # 单台
python push_config.py                # 全部（按 devices.csv）
```

每次下发都会在 `outputs/push_<设备>.log` 留完整回显，出错的行会被标出来。

### 实测踩到的两个坑

**坑一：控制台线路不接受 `screen-length 0 temporary`**

```text
<LSW1> screen-length 0 temporary
Error: The type of the included user interface must be VTY.
```

这条命令只对 VTY 生效。走 console 通道时，长输出必然出现 `---- More ----`，还会附带一堆 ANSI 光标控制符。脚本的处理是：读到 `More` 就自动补一个空格翻页，再把 `\x1b[42D` 这类残留和填充空格清掉。**不清的话，解析会拿到 `---- More ---- ␛[42D ip address ...` 这种烂行。**

**坑二：地址池有租约时不能改网关**

```text
[LSW1-ip-pool-vlan20] gateway-list 192.168.20.1
Error: Please delete all sections of the pool before modifying the gateway IP address.
```

必须先释放租约（`reset ip pool name vlan20 all`）再删 `network` 段，才能改网关。这和 OSPF 的 `undo area 0` 必须先删 `network` 是同一类"容器保护"机制——**设备在防止你改出一个自相矛盾的配置**。

## 六、一个技术取舍：为什么用 `invoke_shell()` 而不是 `exec_command()`

这是写这个脚本最容易卡住的地方，也是面试时最值得讲的一点。

`exec_command()` 走的是 SSH 的 **exec channel**，适用于 Linux 这类"一条命令一个进程"的系统。但**华为 VRP 不支持 exec channel**，直接调用会失败或返回空。

所以必须：

```python
chan = client.invoke_shell()          # 开一个交互式终端
chan.send("screen-length 0 temporary\n")   # 关掉分页
```

**第二行同样不能省。** VRP 输出超过一屏会停在 `---- More ----` 等按键，脚本读到一半就卡死，看起来像是"命令没输出"，实际是在等一个永远不会来的回车。

同类问题还有提示符：`<LSW1>`、`[LSW1]`、`[LSW1-ospf-1]` 会随视图变化，所以脚本不用"等提示符"的方式读输出，而是**读到一段时间没有新数据为止**（`read_until_idle`），这样更稳。

---

## 七、局限与可以继续做的方向

- **串行变并发**：现在用线程池并发，设备多了可以改成异步（`asyncio` + `asyncssh`）
- **只读不改**：当前只做采集和巡检，不下发配置。做下发必须加"配置备份 + diff 预览 + 回滚"三件套
- **能换就换 netmiko**：生产环境建议用 `netmiko`（它内置了华为设备的提示符和分页处理），paramiko 版本更适合用来理解原理
- **接 CI**：`report.md` 可以直接作为流水线产物，配置漂移时让流水线失败
