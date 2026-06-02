"""用人工构造的 tick 流在真实 BacktestingEngine 上验证状态机关键路径。

真实历史数据中乌龙稀缺，正常回测几乎不触发成交，无法覆盖成交后管理与风控分支。
本测试构造确定性场景，驱动真实引擎撮合，覆盖：
  1. 远端买单砸盘成交 → mid 回归计划平仓 → 冷却（test_fill_and_revert）
  2. 成交后 mid 不回归继续下跌 → 单笔止损平仓（test_stop_loss）
  3. 当日涨跌幅超阈值 → 当天停挂不开仓（test_daily_change_stop）

运行：/Users/mac/Quant/vnpy/.venv/bin/python tests/test_transition.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import TickData
from vnpy_ctastrategy.backtesting import BacktestingEngine, BacktestingMode

from fat_finger_strategy import FatFingerStrategy
from ff_state import FFState

_SH = ZoneInfo("Asia/Shanghai")
_TD = date(2025, 6, 4)            # 夜盘 21:00 属次日交易日
_OPEN = datetime(2025, 6, 3, 21, 0, 0, tzinfo=_SH)


def _tick(dt: datetime, bid: float, ask: float, last: float, pre_close: float = 2000.0) -> TickData:
    t = TickData(
        symbol="TEST", exchange=Exchange.CZCE, datetime=dt, name="TEST",
        last_price=last, volume=1, turnover=last * 10,
        bid_price_1=bid, ask_price_1=ask, bid_volume_1=5, ask_volume_1=5,
        pre_close=pre_close, gateway_name="TEST",
    )
    t.extra = {"trading_day": _TD}
    return t


def _warmup(last: float = 2000.0, pre_close: float = 2000.0) -> list[TickData]:
    """开盘前 90 秒的正常盘（每 5 秒一个），mid≈2000，挂单价=floor(2000*0.96)=1920。"""
    return [
        _tick(_OPEN + timedelta(seconds=s), 1999, 2001, last, pre_close)
        for s in range(0, 90, 5)
    ]


def _run_engine(ticks: list[TickData]):
    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol="TEST.CZCE", interval=Interval.TICK,
        start=ticks[0].datetime, end=ticks[-1].datetime,
        rate=0.0, slippage=0.0, size=10, pricetick=1,
        capital=30_000, mode=BacktestingMode.TICK,
    )
    engine.add_strategy(FatFingerStrategy, {})
    engine.history_data = ticks
    engine.run_backtesting()
    return engine, engine.strategy


def test_fill_and_revert():
    ticks = _warmup()
    ticks.append(_tick(_OPEN + timedelta(seconds=95), 1999, 2001, 2000))   # 挂单
    ticks.append(_tick(_OPEN + timedelta(seconds=100), 1898, 1900, 1900))  # 砸盘成交@1900
    for s in (105, 110, 115, 120):                                          # mid 回归 → 平仓
        ticks.append(_tick(_OPEN + timedelta(seconds=s), 1999, 2001, 2000))

    engine, strat = _run_engine(ticks)
    trades = list(engine.trades.values())
    assert strat.n_fill == 1, f"应成交一次，实际 {strat.n_fill}"
    assert len(trades) == 2, f"应买入+平仓两笔，实际 {len(trades)}"
    assert abs(trades[0].price - 1900) < 1e-6, f"买入价应 1900，实际 {trades[0].price}"
    assert abs(trades[1].price - 1999) < 1e-6, f"平仓价应 1999，实际 {trades[1].price}"
    assert abs(strat.daily_realized_pnl - 990) < 1e-6, f"盈亏应 990，实际 {strat.daily_realized_pnl}"
    assert strat.n_revert_close == 1, "应为回归平仓"
    assert strat.state == FFState.COOLDOWN, f"应进入冷却，实际 {FFState(strat.state).name}"
    print(f"✓ 回归平仓链路：买入@{trades[0].price} 平仓@{trades[1].price} "
          f"盈亏{strat.daily_realized_pnl:.0f} 末态{FFState(strat.state).name}")


def test_stop_loss():
    ticks = _warmup()
    ticks.append(_tick(_OPEN + timedelta(seconds=95), 1999, 2001, 2000))   # 挂单
    ticks.append(_tick(_OPEN + timedelta(seconds=100), 1898, 1900, 1900))  # 砸盘成交@1900
    # mid 继续跌到 1810：floating=(1810-1900)*10=-900，触发单笔止损(800)，未达单日闸(1000)
    for s in (105, 110, 115):
        ticks.append(_tick(_OPEN + timedelta(seconds=s), 1809, 1811, 1810))

    engine, strat = _run_engine(ticks)
    trades = list(engine.trades.values())
    assert strat.n_fill == 1, f"应成交一次，实际 {strat.n_fill}"
    assert strat.n_stop_close == 1, f"应触发单笔止损，实际 {strat.n_stop_close}"
    assert abs(trades[1].price - 1809) < 1e-6, f"止损平仓价应 1809，实际 {trades[1].price}"
    assert abs(strat.daily_realized_pnl - (-910)) < 1e-6, f"盈亏应 -910，实际 {strat.daily_realized_pnl}"
    assert strat.state == FFState.COOLDOWN, f"止损后应冷却，实际 {FFState(strat.state).name}"
    print(f"✓ 止损平仓链路：平仓@{trades[1].price} 盈亏{strat.daily_realized_pnl:.0f} "
          f"止损次数{strat.n_stop_close}")


def test_daily_change_stop():
    # 开盘 90 秒后第一个 tick 当日涨幅 2.5% > 2% → 当天停挂，不开仓
    ticks = _warmup()
    ticks.append(_tick(_OPEN + timedelta(seconds=95), 2049, 2051, 2050))   # 涨 2.5%
    ticks.append(_tick(_OPEN + timedelta(seconds=100), 2049, 2051, 2050))

    engine, strat = _run_engine(ticks)
    assert strat.n_quote == 0, f"涨跌幅超阈应不挂单，实际 {strat.n_quote}"
    assert strat.n_fill == 0, "不应有成交"
    assert strat.state == FFState.STOPPED_TODAY, f"应停挂，实际 {FFState(strat.state).name}"
    print(f"✓ 涨跌幅停挂：挂单{strat.n_quote} 末态{FFState(strat.state).name}")


if __name__ == "__main__":
    test_fill_and_revert()
    test_stop_loss()
    test_daily_change_stop()
    print("\n全部状态机测试通过 ✓")
