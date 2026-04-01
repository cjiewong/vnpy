"""
主启动脚本 — 在 Windows VM（或腾讯云 Windows Server）上运行。

前置条件：
    1. miniQMT 已安装并以极简模式登录
    2. pip install vnpy vnpy_qmt vnpy_ctastrategy

用法（在 Windows VM 终端中）：
    python \\Mac\Home\Quant\vnpy\docs\qmt\run.py
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

# ── 配置区（按实际情况修改）──────────────────────────────────────────────────

QMT_ACCOUNT = ""          # 交易账号，例如 "1234567890"
QMT_MINI_PATH = r"C:\国金证券QMT交易端\userdata_mini"

# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


def process_log_event(event: Event) -> None:
    log: LogData = event.data
    print(f"{log.time}  {log.msg}")


def main() -> None:
    if not QMT_ACCOUNT:
        print("错误：请在脚本顶部设置 QMT_ACCOUNT（交易账号）")
        sys.exit(1)

    event_engine = EventEngine()
    event_engine.register(EVENT_LOG, process_log_event)

    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(QmtGateway)
    cta_engine = main_engine.add_app(CtaStrategyApp)

    setting = {
        "交易账号": QMT_ACCOUNT,
        "mini路径": QMT_MINI_PATH,
    }

    print(f"正在连接 QmtGateway（账号：{QMT_ACCOUNT}）...")
    main_engine.connect(setting, "QMT")
    sleep(5)  # 等待合约/持仓初始化完成

    cta_engine.init_engine()
    cta_engine.init_all_strategies()
    cta_engine.start_all_strategies()

    print("\n策略已启动，运行中。按 Enter 退出。\n")
    input()

    main_engine.close()
    print("已关闭。")


if __name__ == "__main__":
    main()
