"""真实乌龙捕捉演示：对 fat-finger 已检测的历史乌龙日跑回测，看策略能否吃到。

挑选标准：RM/MA/TA 非主力合约、当日下砸>4%、单日仅 1-3 个事件（孤立瞬间乌龙，
非趋势日）。每个事件多取前几日让 pre_close 准确、策略充分热身。

这是对"零成交的 Q2 回测"的补充：证明一旦真有 4%+ 的瞬时乌龙，策略+vnpy 撮合
能够捕捉；同时直观展示 vnpy 的 ask1 触及口径与 fat-finger avg_fill 口径的差异。
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vnpy.trader.constant import Exchange  # noqa: E402

from ff_common import product_code_from_instrument  # noqa: E402
from run_backtest import run_single  # noqa: E402

# (合约四位码, 事件日, fat-finger 检测到的均价偏离%)
EVENTS = [
    ("TA2508", "2025-05-12", -4.46),
    ("MA2508", "2025-06-13", -4.37),
    ("RM2501", "2024-11-07", -4.25),
    ("RM2509", "2025-08-12", -4.20),
    ("MA2307", "2023-05-26", -4.19),
    ("TA2509", "2025-04-10", -4.10),
]


def _run(sym, product, td, fill_proxy):
    # 单交易日窗口：夜盘 20:00 起即有充分热身；pre_close 取当日首 tick，
    # 避免乌龙瞬间 last_price 暴跌相对昨收触发 daily_change 停挂而撤掉挂单。
    return run_single(product, sym, Exchange.CZCE, td, td, fill_proxy=fill_proxy)


def main():
    print("对比两种撮合口径在真实乌龙日的捕捉效果：")
    print("  ask1 = vnpy 官方盘口口径（保守）；avgfill = fat-finger 成交均价口径\n")
    header = (f"{'合约':9s}{'事件日':11s}{'偏离':>7s} | "
              f"{'ask1:挂单/成交/净盈亏':>22s} | {'avgfill:挂单/成交/净盈亏':>24s}")
    print(header)
    print("-" * len(header))
    for sym, d, dev in EVENTS:
        product = product_code_from_instrument(sym)
        td = date.fromisoformat(d)
        a = _run(sym, product, td, fill_proxy=False)
        b = _run(sym, product, td, fill_proxy=True)
        if a is None or b is None:
            print(f"{sym:9s}{d:11s}{dev:6.2f}%  无 tick 数据")
            continue
        sa, sb = a.strat_stats, b.strat_stats
        col_a = f"{sa['n_quote']}/{sa['n_fill']}/{a.stats.get('total_net_pnl'):.0f}"
        col_b = f"{sb['n_quote']}/{sb['n_fill']}/{b.stats.get('total_net_pnl'):.0f}"
        print(f"{sym:9s}{d:11s}{dev:6.2f}% | {col_a:>22s} | {col_b:>24s}")


if __name__ == "__main__":
    main()
