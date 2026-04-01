# Mac 开发环境配置指南

## 概述

Mac 只负责**编辑代码**，不运行 vnpy。代码通过 Parallels 共享目录在 Windows VM 中执行。

目标：在 Mac 上获得完整的代码补全、语法检查和跳转，即使无法直接运行 vnpy_qmt。

## 1. Mac 侧安装 vnpy（仅用于 IDE 补全）

```bash
# 建议使用独立环境
conda create -n quant python=3.10
conda activate quant

pip install -e /Users/mac/Quant/vnpy
pip install -e /Users/mac/Quant/vnpy_qmt   # 会失败于 xtquant，但不影响补全
pip install vnpy_ctastrategy
```

`vnpy_qmt` 安装时 `xtquant` 会报错，这是预期行为——Mac 无法运行，但 IDE 已能读取类型信息。

## 2. VS Code 配置

推荐扩展：
- **Python**（ms-python.python）
- **Pylance**（语言服务器，提供类型推断）

`settings.json` 关键配置：
```json
{
    "python.defaultInterpreterPath": "/opt/homebrew/Caskroom/miniconda/base/envs/quant/bin/python",
    "python.analysis.extraPaths": [
        "/Users/mac/Quant/vnpy",
        "/Users/mac/Quant/vnpy_qmt"
    ]
}
```

## 3. 在 Windows VM 中调试

### 方式 A：VM 终端（简单）

在 Parallels 的 Windows 终端里运行，观察 print 输出：
```powershell
python \\Mac\Home\Quant\vnpy\docs\qmt\run.py
```

### 方式 B：VS Code Remote SSH（推荐，支持断点）

在 Windows VM 中开启 OpenSSH 服务：
```powershell
# 管理员 PowerShell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
```

Mac 侧 VS Code 安装 **Remote - SSH** 扩展后连接：
```
ssh 你的用户名@10.211.55.3
```

连接后，VS Code 在 Windows VM 内运行，可设断点、查变量，完整调试体验。

## 4. 回测（Mac 本地运行）

回测不依赖 xtquant，可以在 Mac 上直接运行：

```bash
pip install vnpy_backtester
```

历史数据用 HTTP Bridge 从 Windows 拉取后存入本地数据库，再供回测使用：

```python
# 从 Windows VM 拉取数据（VM 需先运行 qmt/bridge/server.py）
from qmt.bridge.client import QmtClient

client = QmtClient("http://10.211.55.3:8000")
df = client.get_stock_ohlcv("600519.SH", start="20230101", period="1d")
# 存入 vnpy 数据库，用于回测
```

## 5. 工作流总结

```
Mac（VS Code 编辑）
    ↓ 保存文件（自动同步，共享目录）
Windows VM（终端运行）
    python \\Mac\Home\Quant\vnpy\docs\qmt\run.py
    ↓ 观察日志
Mac（调整代码）
    ↓ 循环
```
