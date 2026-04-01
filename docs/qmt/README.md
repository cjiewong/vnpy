# Mac + Windows VM 量化交易架构

## 设计目标

| 阶段 | 目标 |
|------|------|
| 开发/回测 | Mac 上编辑代码，Windows VM 上运行（共享目录） |
| 开发期实盘 | Windows VM 直连本机 miniQMT，使用 QmtGateway |
| 生产 | 腾讯云 Windows Server，代码路径与开发一致 |

## 核心约束

**xtquant（miniQMT 的 Python SDK）只能在 Windows + miniQMT 安装目录下运行。**

策略代码在 Mac 上编写，通过 Parallels 共享目录直接在 Windows VM 上执行，无需网络桥接层。

## 架构

```
Mac（编辑代码）
/Users/mac/Quant/  ←──── Parallels 共享目录 ────►  \\Mac\Home\Quant\
                                                      Windows VM（运行代码）
                                                      ├── vnpy MainEngine
                                                      ├── QmtGateway
                                                      │   └── xtquant → miniQMT
                                                      └── CtaStrategyApp
```

代码只有一份，Mac 编辑，Windows VM 执行。开发环境与生产环境（腾讯云）架构完全一致。

## 开发流程

```
1. Mac：用 VS Code / PyCharm 编辑策略代码
2. Windows VM：在终端执行 python \\Mac\Home\Quant\vnpy\docs\qmt\run.py
3. 观察日志，调整策略
4. 回测：在 Windows VM 上执行，或拉取历史数据后在 Mac 上运行
```

## 现有资产的定位

| 组件 | 位置 | 用途 |
|------|------|------|
| vnpy_qmt | `vnpy_qmt/` | QmtGateway（直接使用） |
| HTTP Bridge Server | `qmt/bridge/server.py` | 历史数据采集（回测数据准备） |
| HTTP Bridge Client | `qmt/bridge/client.py` | Mac 侧拉取历史数据 |

## 文件说明

| 文件 | 说明 |
|------|------|
| `run.py` | **Windows VM / 腾讯云运行** — 连接 miniQMT 并启动策略 |
| `setup-windows-vm.md` | Windows VM 环境搭建（依赖、共享目录、miniQMT） |
| `setup-mac-dev.md` | Mac 编辑器配置（代码补全、远程调试） |
| `deploy-cloud.md` | 腾讯云 Windows Server 生产部署 |

## 快速开始

### 1. Windows VM：安装依赖

```powershell
pip install vnpy vnpy_qmt vnpy_ctastrategy
```

### 2. 编辑 run.py，填写账号

```python
QMT_ACCOUNT = "你的交易账号"
```

### 3. Windows VM：启动

```powershell
python \\Mac\Home\Quant\vnpy\docs\qmt\run.py
```
