"""乌龙指策略回测入口：取数 → 注入 history_data → run_backtesting → 统计。

用法：
  /Users/mac/Quant/vnpy/.venv/bin/python run_backtest.py \
      --products RM,MA,TA --start 2025-04-01 --end 2025-06-30 --probe 2025-06-03

单 symbol 引擎，RM/MA/TA 各自独立回测；组合层风控由 portfolio_merge 事后归并。
首轮 rate=slippage=0 看"理论上限"，确认链路与状态机后再加真实成本。
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime

# 允许从任意 cwd 运行：把脚本所在目录加入 import 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vnpy.trader.constant import Exchange, Interval  # noqa: E402
from vnpy_ctastrategy.backtesting import BacktestingEngine, BacktestingMode  # noqa: E402

import contract_selector as cs  # noqa: E402
import tick_source as ts  # noqa: E402
from fat_finger_strategy import FatFingerStrategy  # noqa: E402
from ff_common import get_pricetick, get_real_multiplier  # noqa: E402

# 品种 → 交易所（首轮三品种均为郑商所）
_PRODUCT_EXCHANGE = {"RM": Exchange.CZCE, "MA": Exchange.CZCE, "TA": Exchange.CZCE}


@dataclass(slots=True)
class BacktestOutcome:
    product: str
    symbol: str
    main: str | None
    used_days: list[date]
    n_ticks: int
    stats: dict
    strat_stats: dict          # 策略行为统计（挂单/成交/各类平仓次数）
    trades: list               # engine.trades.values()，供组合归并


def run_single(
    product: str,
    symbol: str,
    exchange: Exchange,
    start_d: date,
    end_d: date,
    *,
    setting: dict | None = None,
    rate: float = 0.0,
    slippage: float = 0.0,
    capital: float = 30_000,
    fill_proxy: bool = False,
) -> BacktestOutcome | None:
    """对单品种单合约跑一次 tick 级回测。

    fill_proxy=True 时改用 avg_fill 口径撮合（捕捉 500ms 快照盘口抹平的瞬时乌龙）。
    """
    ticks, used_days = ts.load_ticks_for_range(
        symbol, product, exchange, start_d, end_d, fill_proxy=fill_proxy
    )
    if not ticks:
        print(f"[{product}] {symbol} 区间内零 tick，跳过")
        return None

    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=f"{symbol}.{exchange.value}",
        interval=Interval.TICK,
        start=ticks[0].datetime,
        end=ticks[-1].datetime,
        rate=rate,
        slippage=slippage,
        size=get_real_multiplier(product),
        pricetick=get_pricetick(product),
        capital=capital,
        mode=BacktestingMode.TICK,
    )
    engine.add_strategy(FatFingerStrategy, setting or {})

    # 直接注入构造好的升序 TickData，绝不调 engine.load_data()（它会 clear 并查 vnpy 库）
    engine.history_data = ticks
    engine.run_backtesting()

    df = engine.calculate_result()
    stats = engine.calculate_statistics(df, output=False)

    strat = engine.strategy
    strat_stats = {
        "n_quote": strat.n_quote,
        "n_fill": strat.n_fill,
        "n_stop_close": strat.n_stop_close,
        "n_revert_close": strat.n_revert_close,
        "n_timeout_close": strat.n_timeout_close,
    }
    return BacktestOutcome(
        product=product, symbol=symbol, main=None, used_days=used_days,
        n_ticks=len(ticks), stats=stats, strat_stats=strat_stats,
        trades=list(engine.trades.values()),
    )


def _print_outcome(o: BacktestOutcome):
    s = o.stats
    print(f"\n{'='*60}")
    print(f"品种 {o.product} | 合约 {o.symbol} | 主力 {o.main} | "
          f"交易日 {len(o.used_days)} | tick {o.n_ticks}")
    print(f"  区间 {s.get('start_date')} ~ {s.get('end_date')}")
    print(f"  成交笔数 total_trade_count = {s.get('total_trade_count')}")
    print(f"  净盈亏 total_net_pnl       = {s.get('total_net_pnl'):.2f}")
    print(f"  末值 end_balance           = {s.get('end_balance'):.2f}")
    print(f"  收益率 total_return        = {s.get('total_return'):.4f}%")
    print(f"  最大回撤 max_ddpercent     = {s.get('max_ddpercent'):.4f}%")
    print(f"  手续费 total_commission    = {s.get('total_commission'):.2f}")
    print(f"  策略行为 {o.strat_stats}")


def main():
    parser = argparse.ArgumentParser(description="乌龙指策略 tick 回测")
    parser.add_argument("--products", default="RM,MA,TA", help="逗号分隔品种")
    parser.add_argument("--start", default="2025-04-01")
    parser.add_argument("--end", default="2025-06-30")
    parser.add_argument("--probe", default="2025-06-03", help="选远月合约的探测交易日")
    parser.add_argument("--min-ticks", type=int, default=8000, help="远月合约流动性下限")
    parser.add_argument("--rate", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.0)
    args = parser.parse_args()

    start_d = datetime.strptime(args.start, "%Y-%m-%d").date()
    end_d = datetime.strptime(args.end, "%Y-%m-%d").date()
    probe_d = datetime.strptime(args.probe, "%Y-%m-%d").date()
    products = [p.strip() for p in args.products.split(",") if p.strip()]

    outcomes: list[BacktestOutcome] = []
    for product in products:
        exchange = _PRODUCT_EXCHANGE[product]
        pick = cs.pick_far_month(product, probe_d, min_ticks=args.min_ticks)
        print(f"[{product}] 选中远月 {pick.picked}（{pick.picked_ticks} ticks），"
              f"主力 {pick.main}，候选 {pick.candidates[:4]}")
        o = run_single(
            product, pick.picked, exchange, start_d, end_d,
            rate=args.rate, slippage=args.slippage,
        )
        if o:
            o.main = pick.main
            outcomes.append(o)
            _print_outcome(o)

    print(f"\n{'#'*60}\n汇总：{len(outcomes)} 个品种完成回测")
    for o in outcomes:
        print(f"  {o.product}/{o.symbol}: 挂单{o.strat_stats['n_quote']} "
              f"成交{o.strat_stats['n_fill']} 净盈亏{o.stats.get('total_net_pnl'):.2f}")


if __name__ == "__main__":
    main()
