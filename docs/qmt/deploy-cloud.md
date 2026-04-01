# 腾讯云 Windows Server 生产部署指南

## 架构说明

生产环境在腾讯云 Windows Server 上**本机直连**，不需要 RPC 分层：

```
腾讯云 Windows Server
├── miniQMT（国金证券 QMT 交易端，极简模式）
├── vnpy MainEngine
├── QmtGateway → xtquant → miniQMT
└── CtaStrategyApp（策略在本机直接运行）
```

与开发环境的对比：

| 项目 | 开发环境 | 生产环境 |
|------|---------|---------|
| 策略运行位置 | Mac | 腾讯云 Windows |
| Gateway 类型 | RpcGateway | QmtGateway（直连） |
| miniQMT 位置 | Parallels VM | 腾讯云 Windows |
| 是否需要 RPC | 是 | **否** |

## 1. 腾讯云服务器选型

| 配置项 | 建议 |
|--------|------|
| 操作系统 | Windows Server 2019/2022 |
| CPU | 4 核以上（miniQMT 较占资源） |
| 内存 | 8 GB 以上 |
| 带宽 | 5 Mbps 以上（行情数据） |
| 存储 | 100 GB 以上（历史数据缓存） |
| 地区 | 上海（降低与交易所的网络延迟） |

## 2. 服务器初始化

### 安全组配置（仅允许必要端口）

```
入站规则：
- 3389 TCP  → 你的 IP  （RDP 远程桌面，限制来源 IP）
- 22 TCP    → 你的 IP  （SSH，可选）

出站规则：
- ALL（允许所有出站，用于连接交易所）
```

**不要**对外开放 2014/4102 端口（生产环境无需 RPC）。

### RDP 连接

在 Mac 上使用 Microsoft Remote Desktop（App Store 免费）连接服务器。

## 3. 环境安装

在服务器上执行（PowerShell 管理员）：

```powershell
# 安装 Python 3.10（推荐）
# 从 python.org 下载 Windows 安装包，选"Add to PATH"

# 安装依赖
pip install vnpy vnpy_qmt pyzmq

# 安装策略 App
pip install vnpy_ctastrategy
```

**xtquant 安装**（随 miniQMT 附带，参考 setup-windows-vm.md 第 1 节）。

## 4. 代码部署

### 方式 A：Git（推荐）

```powershell
# 安装 Git for Windows
git clone https://github.com/你的仓库/quant.git C:\Quant

# 或直接拉取 vnpy_qmt
git clone https://github.com/vnpy/vnpy_qmt.git C:\Quant\vnpy_qmt
pip install -e C:\Quant\vnpy_qmt
```

### 方式 B：SCP/SFTP 上传

```bash
# 从 Mac 上传
scp -r /Users/mac/Quant/vnpy/docs/qmt/ username@cloud-ip:C:/Quant/
```

## 5. 生产启动脚本

创建 `C:\Quant\run_production.py`：

```python
"""
生产环境启动脚本 — 腾讯云 Windows Server
直接连接本机 miniQMT，无需 RPC。
"""

import sys
import logging
from time import sleep

from vnpy.event import EventEngine, Event
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.object import LogData
from vnpy_qmt import QmtGateway
from vnpy_ctastrategy import CtaStrategyApp

QMT_ACCOUNT = "你的交易账号"
QMT_MINI_PATH = r"C:\国金证券QMT交易端\userdata_mini"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    filename=r"C:\Quant\logs\vnpy.log",
)


def process_log_event(event: Event) -> None:
    log: LogData = event.data
    print(f"{log.time}  {log.msg}")


def main() -> None:
    event_engine = EventEngine()
    event_engine.register(EVENT_LOG, process_log_event)

    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(QmtGateway)
    cta_engine = main_engine.add_app(CtaStrategyApp)

    setting = {
        "交易账号": QMT_ACCOUNT,
        "mini路径": QMT_MINI_PATH,
    }
    main_engine.connect(setting, "QMT")
    sleep(5)

    cta_engine.init_engine()
    cta_engine.init_all_strategies()
    cta_engine.start_all_strategies()

    print("策略已启动，运行中...")
    input("Press Enter to stop...\n")

    main_engine.close()


if __name__ == "__main__":
    main()
```

## 6. 设置开机自启

使用 Windows 任务计划程序实现自动启动：

```powershell
# 创建任务（开机后 2 分钟启动，留时间给 miniQMT 登录）
schtasks /create /tn "vnpy_production" /tr "python C:\Quant\run_production.py" /sc onlogon /delay 0002:00 /ru 你的用户名
```

或使用批处理：
```batch
@echo off
:: start_trading.bat
cd C:\Quant
python run_production.py
```

## 7. 监控建议

### 日志

```python
# 在 run_production.py 中添加文件日志
import logging
logging.basicConfig(
    filename=r"C:\Quant\logs\vnpy.log",
    level=logging.INFO,
)
```

### 远程查看日志

```bash
# 从 Mac 实时查看日志（需服务器开启 SSH）
ssh user@cloud-ip "Get-Content C:\Quant\logs\vnpy.log -Wait"
```

### 可选：开启 RPC 供远程监控

如需在 Mac 上实时监控生产环境（不发单，只看状态），可以在生产脚本中同时启动 RpcServiceApp，仅限内网或 VPN 访问：

```python
from vnpy_rpcservice import RpcServiceApp
rpc_engine = main_engine.add_app(RpcServiceApp)
rpc_engine.start("tcp://127.0.0.1:2014", "tcp://127.0.0.1:4102")
# 通过 SSH 隧道从 Mac 访问：
# ssh -L 2014:localhost:2014 -L 4102:localhost:4102 user@cloud-ip
```

## 8. 迁移检查清单

从开发环境迁移到生产时确认：

- [ ] miniQMT 已在服务器安装并能正常登录
- [ ] xtquant 已复制到 Python 环境
- [ ] 策略文件已上传，路径正确
- [ ] `run_production.py` 中账号和路径已更新
- [ ] 安全组未对外暴露不必要端口
- [ ] 本地回测已通过，策略参数已调好
- [ ] 日志目录 `C:\Quant\logs\` 已创建
- [ ] 已设置任务计划程序自动启动
