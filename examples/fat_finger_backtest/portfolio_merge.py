"""跨品种组合风控的事后归并。

vnpy CTA BacktestingEngine 是单 symbol 引擎，RM/MA/TA 各自独立回测，
"同时最多 1 持仓 / 单日组合总亏损 1000 即全停" 这类跨品种约束测不出。
本模块把各品种成交配对成交易对，按时间轴归并，套用组合层风控做保守裁剪：
  - 某交易对开仓时若组合已持有未平仓位 → 拒绝该对（方案：同时最多 1 持仓）；
  - 某交易日组合已实现亏损达 daily_loss_yuan → 当日后续开仓一律拒绝。
裁剪后的 accepted 才是组合层真实可成交的交易，其盈亏之和是组合净盈亏。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from vnpy.trader.constant import Direction


@dataclass(slots=True)
class TradePair:
    product: str
    entry_dt: datetime
    exit_dt: datetime
    entry_price: float
    exit_price: float
    volume: float
    size: float
    pnl: float
    trading_day: date


def _infer_trading_day(dt: datetime) -> date:
    """夜盘(>=20点)归次日交易日；跨周末顺延。

    局限：不含节假日日历，跨长假（春节/国庆）时与 DB 的 trading_day 可能差几日，
    导致组合"按日"统计的分桶边界偏移。仅用于 portfolio_merge 的近似日聚合；
    若要精确到交易日，应让成交携带 DB trading_day（vnpy TradeData 无此字段）。
    """
    d = dt.date()
    if dt.hour >= 20:
        d = d + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
    return d


def pair_trades(product: str, trades: list, size: float) -> list[TradePair]:
    """把单品种成交序列(买开/卖平交替)配对成交易对。

    受控状态机保证 LONG→SHORT 交替；孤儿成交（无配对的 SHORT、或数据末尾未平仓的
    dangling LONG）极罕见（如收盘强平失败），无完整价对无法计盈亏，按 dropped 返回数提示。
    """
    pairs: list[TradePair] = []
    dropped = 0
    open_trade = None
    for t in sorted(trades, key=lambda x: x.datetime):
        if t.direction == Direction.LONG:
            if open_trade is not None:              # 上一笔开仓未平就来新开仓
                dropped += 1
            open_trade = t
        elif open_trade is not None:                # 卖出平仓，与最近开仓配对
            pnl = (t.price - open_trade.price) * size * t.volume
            pairs.append(TradePair(
                product=product, entry_dt=open_trade.datetime, exit_dt=t.datetime,
                entry_price=open_trade.price, exit_price=t.price,
                volume=t.volume, size=size, pnl=pnl,
                trading_day=_infer_trading_day(open_trade.datetime),
            ))
            open_trade = None
        else:                                       # 孤儿平仓（无对应开仓）
            dropped += 1
    if open_trade is not None:                      # 末尾未平仓
        dropped += 1
    if dropped:
        print(f"[portfolio_merge] {product} 丢弃 {dropped} 笔未配对成交（受控状态机下应为 0）")
    return pairs


@dataclass(slots=True)
class MergeResult:
    accepted: list[TradePair] = field(default_factory=list)
    rejected_overlap: list[TradePair] = field(default_factory=list)   # 因已有持仓被拒
    rejected_daily_stop: list[TradePair] = field(default_factory=list)  # 因单日闸被拒
    total_pnl: float = 0.0

    def summary(self) -> str:
        return (
            f"组合归并：接受 {len(self.accepted)} 笔，"
            f"持仓冲突拒绝 {len(self.rejected_overlap)} 笔，"
            f"单日闸拒绝 {len(self.rejected_daily_stop)} 笔，"
            f"组合净盈亏 {self.total_pnl:.2f} 元"
        )


def merge_portfolio(
    pairs_by_product: dict[str, list[TradePair]],
    *,
    daily_loss_yuan: float = 1000.0,
) -> MergeResult:
    """按时间轴归并多品种交易对，套用"最多 1 持仓 + 单日组合亏损闸"。"""
    all_pairs: list[TradePair] = []
    for ps in pairs_by_product.values():
        all_pairs.extend(ps)
    all_pairs.sort(key=lambda p: p.entry_dt)

    res = MergeResult()
    active_until: datetime | None = None     # 当前组合持仓的平仓时间
    daily_pnl: dict[date, float] = {}

    for p in all_pairs:
        if daily_pnl.get(p.trading_day, 0.0) <= -daily_loss_yuan:
            res.rejected_daily_stop.append(p)        # 当日组合已停
            continue
        if active_until is not None and p.entry_dt < active_until:
            res.rejected_overlap.append(p)           # 组合已有持仓
            continue
        res.accepted.append(p)
        active_until = p.exit_dt
        daily_pnl[p.trading_day] = daily_pnl.get(p.trading_day, 0.0) + p.pnl

    res.total_pnl = sum(p.pnl for p in res.accepted)
    return res
