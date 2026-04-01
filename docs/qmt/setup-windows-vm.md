# Windows VM 环境搭建指南

## 概述

Windows VM 负责运行所有 vnpy 代码（直连 miniQMT）。代码存放在 Mac 上，通过 Parallels 共享目录访问。

## 1. Parallels 共享目录确认

Parallels 默认将 Mac 的 home 目录挂载到 Windows：

```
Mac 路径：/Users/mac/Quant/
Windows 路径：\\Mac\Home\Quant\
```

在 Windows VM 中验证：
```powershell
dir \\Mac\Home\Quant\
# 应能看到 vnpy、vnpy_qmt、qmt 三个目录
```

如果无法访问，检查 Parallels 设置：
- 虚拟机设置 → 选项 → 共享 → 启用"共享 Mac 卷"

## 2. miniQMT 安装与配置

1. 登录国金证券官网，下载"QMT 交易端"
2. 默认安装路径：`C:\国金证券QMT交易端\`
3. **以极简模式（mini 模式）启动并登录**，保持运行

`run.py` 中使用的 `mini路径`：
```
C:\国金证券QMT交易端\userdata_mini
```

### xtquant 安装

xtquant 随 miniQMT 附带，不走 pip 分发。将其复制到当前 Python 环境：

```powershell
# 找到 miniQMT 内置的 xtquant 目录，例如：
# C:\国金证券QMT交易端\Lib\site-packages\xtquant\

# 复制到你的 Python 环境（路径按实际 Python 版本调整）：
xcopy "C:\国金证券QMT交易端\Lib\site-packages\xtquant" `
      "C:\Users\你的用户名\AppData\Local\Programs\Python\Python310\Lib\site-packages\xtquant" /E /I
```

验证：
```python
from xtquant import xtdata
print(xtdata.get_stock_list_in_sector("沪深A股"))
```

## 3. Python 依赖安装

```powershell
# 从共享目录以开发模式安装（Mac 上修改代码后立即生效，无需重装）
pip install -e \\Mac\Home\Quant\vnpy
pip install -e \\Mac\Home\Quant\vnpy_qmt

# 其他依赖
pip install vnpy_ctastrategy
```

## 4. 启动运行

确认 miniQMT 已登录后：

```powershell
python \\Mac\Home\Quant\vnpy\docs\qmt\run.py
```

正常启动日志：
```
正在连接 QmtGateway（账号：xxxxxxxxxx）...
2024-xx-xx xx:xx:xx  QMT: 合约查询完成，共 5000 条
2024-xx-xx xx:xx:xx  QMT: 持仓查询完成
策略已启动，运行中。按 Enter 退出。
```

## 5. 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| `\\Mac\Home\Quant\` 无法访问 | 共享目录未启用 | Parallels 设置 → 共享 → 启用 |
| `ModuleNotFoundError: xtquant` | xtquant 未复制 | 按第 2 节复制 xtquant 目录 |
| `连接失败 code=-1` | miniQMT 未登录或路径错误 | 确认 miniQMT 运行中，检查 `mini路径` |
| 策略文件找不到 | 路径写死为绝对路径 | 使用 `\\Mac\Home\Quant\...` 开头的路径 |
