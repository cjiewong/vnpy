"""验证 portfolio_merge 的跨品种组合风控裁剪逻辑。

真实乌龙事件多为盈利，归并时只触发了"最多 1 持仓"分支、未触发"单日闸"。
本测试用构造的交易对覆盖两个裁剪分支：
  1. 同时持仓重叠 → 拒绝第二笔（最多 1 持仓）
  2. 单日组合累计亏损达闸 → 拒绝当日后续（单日 1000 闸）
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portfolio_merge import TradePair, merge_portfolio

_D = date(2025, 6, 4)


def _pair(product, h_open, m_open, h_close, m_close, pnl):
    return TradePair(
        product=product,
        entry_dt=datetime(2025, 6, 4, h_open, m_open),
        exit_dt=datetime(2025, 6, 4, h_close, m_close),
        entry_price=2000, exit_price=2000 + pnl / 10, volume=1, size=10,
        pnl=pnl, trading_day=_D,
    )


def test_max_one_position():
    # B 在 A 持仓期内开仓 → 被拒（同时最多 1 持仓）
    a = _pair("RM", 10, 0, 10, 5, +500)
    b = _pair("MA", 10, 2, 10, 7, +800)   # entry 10:02 落在 A 的 [10:00,10:05]
    res = merge_portfolio({"RM": [a], "MA": [b]})
    assert len(res.accepted) == 1 and res.accepted[0].product == "RM"
    assert len(res.rejected_overlap) == 1 and res.rejected_overlap[0].product == "MA"
    assert abs(res.total_pnl - 500) < 1e-6, res.total_pnl
    print(f"✓ 最多1持仓：接受 RM(+500)，拒绝重叠 MA(+800)，组合 {res.total_pnl:.0f}")


def test_daily_loss_stop():
    # A、B 累计亏损达 -1100 ≤ -1000 → C 当日被拒（单日闸）
    a = _pair("RM", 10, 0, 10, 5, -600)
    b = _pair("MA", 10, 10, 10, 15, -500)  # 不与 A 重叠
    c = _pair("TA", 10, 20, 10, 25, +900)  # 单日闸触发后被拒
    res = merge_portfolio({"RM": [a], "MA": [b], "TA": [c]})
    assert len(res.accepted) == 2, [p.product for p in res.accepted]
    assert len(res.rejected_daily_stop) == 1 and res.rejected_daily_stop[0].product == "TA"
    assert abs(res.total_pnl - (-1100)) < 1e-6, res.total_pnl
    print(f"✓ 单日闸：A(-600)+B(-500) 触发后拒绝 C(+900)，组合 {res.total_pnl:.0f}")


if __name__ == "__main__":
    test_max_one_position()
    test_daily_loss_stop()
    print("\n组合风控测试通过 ✓")
