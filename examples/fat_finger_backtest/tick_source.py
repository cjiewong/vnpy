"""从 NAS TimescaleDB md.futures_tick 取数并构造 vnpy TickData 列表。

落地路径：单分区(instrument_id, trading_day) + ts 窗口裁剪逐日取数 →
构造按时间升序的 TickData 列表 → 直接注入 BacktestingEngine.history_data。

口径要点：
- 只取有盘口的 tick（bid_price1>0 AND ask_price1>0），与 fat-finger 一致，
  也契合 vnpy 撮合的 ask1>0 要求。
- futures_tick 无 pre_close 字段，用"上一交易日最后一笔 last_price"兜底作昨收，
  供策略计算当日涨跌幅；首个交易日无前值则取当日首 tick last_price（涨跌幅=0）。
"""
from __future__ import annotations

import time as time_mod
from datetime import date, datetime
from typing import Any

from vnpy.trader.constant import Exchange
from vnpy.trader.object import TickData

from ff_common import (
    _SH_TZ,
    fetch_all,
    get_algo_multiplier,
    tick_instrument_id,
    trading_day_ts_window,
)

_GATEWAY = "TSDB"

# fill_proxy 健全性防线（对齐 fat-finger，过滤伪 avg_fill）：
# - 相邻 tick 间隔超过此值，增量横跨大间隔/休市/补登成交，均价不可信
_MAX_TICK_GAP_S = 10.0

# 单交易日全量 tick。带 ts 窗口裁剪与盘口过滤，命名参数防注入。
_SQL_FULL_DAY = """
SELECT ts, bid_price1, ask_price1, bid_volume1, ask_volume1,
       last_price, volume, turnover, open_interest,
       upper_limit_price, lower_limit_price
FROM md.futures_tick
WHERE trading_day = %(td)s AND instrument_id = %(inst)s
  AND ts >= %(ts_start)s AND ts < %(ts_end)s
  AND bid_price1 > 0 AND ask_price1 > 0
ORDER BY ts;
"""

# 交易日历：continuous_map 是小表，DISTINCT trading_day 安全，不触发 tick 表的 lock。
_SQL_CALENDAR = """
SELECT DISTINCT trading_day
FROM md.futures_continuous_map
WHERE trading_day >= %(start)s AND trading_day <= %(end)s
ORDER BY trading_day;
"""


def _fetch_with_retry(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """带指数退避重试的查询：应对 TimescaleDB lock 高峰与 DB 抖动。"""
    last_err: Exception | None = None
    for delay in [3, 10, 20, 40, 0]:
        try:
            return fetch_all(sql, params)
        except Exception as e:  # noqa: BLE001 - 需按错误文本判定是否可重试
            last_err = e
            msg = str(e).lower()
            transient = any(
                k in msg
                for k in (
                    "shared memory", "max_locks", "connection refused",
                    "could not receive data", "starting up",
                    "server closed the connection", "connection failed",
                )
            )
            if transient and delay:
                time_mod.sleep(delay)
                continue
            raise
    raise RuntimeError(f"查询重试耗尽：{last_err}")


def trading_calendar(start: date, end: date) -> list[date]:
    """区间内的中国期货交易日列表（升序）。"""
    rows = _fetch_with_retry(_SQL_CALENDAR, {"start": start, "end": end})
    return [r["trading_day"] for r in rows]


def fetch_full_day_rows(std_symbol: str, product: str, td: date) -> list[dict[str, Any]]:
    """取单合约单交易日的全量盘口 tick 原始行。"""
    inst = tick_instrument_id(std_symbol, product)
    ts_start, ts_end = trading_day_ts_window(td)
    return _fetch_with_retry(
        _SQL_FULL_DAY,
        {"td": td, "inst": inst, "ts_start": ts_start, "ts_end": ts_end},
    )


def _row_to_tick(
    r: dict[str, Any], std_symbol: str, exchange: Exchange,
    pre_close: float, trading_day: date, ask_override: float | None = None,
) -> TickData:
    """单行 DB 记录 → vnpy TickData。撮合必需字段：datetime/ask_price_1/bid_price_1/last_price。

    trading_day 写入 tick.extra，供策略精确判定交易日（夜盘归次日），
    避免靠相邻 tick 时间差推算交易日导致的跨日重置错误。

    ask_override：fill_proxy 模式下用该帧 avg_fill 覆盖 ask_price_1，使 vnpy 撮合
    改用"成交均价穿越挂单价"的 fat-finger 口径，捕捉 500ms 快照盘口抹平的瞬时乌龙。
    """
    dt: datetime = r["ts"].astimezone(_SH_TZ)  # 统一上海时区，避免夜盘跨日错位
    ask1 = float(r["ask_price1"]) if ask_override is None else ask_override
    t = TickData(
        symbol=std_symbol,                       # 四位标准码，vt_symbol 自动生成
        exchange=exchange,
        datetime=dt,
        name=std_symbol,
        last_price=float(r["last_price"]),
        volume=float(r["volume"] or 0),
        turnover=float(r["turnover"] or 0),
        open_interest=float(r["open_interest"] or 0),
        bid_price_1=float(r["bid_price1"]),
        ask_price_1=ask1,
        bid_volume_1=float(r["bid_volume1"] or 0),
        ask_volume_1=float(r["ask_volume1"] or 0),
        limit_up=float(r["upper_limit_price"] or 0),
        limit_down=float(r["lower_limit_price"] or 0),
        pre_close=pre_close,                     # 昨收兜底值，策略算当日涨跌幅
        gateway_name=_GATEWAY,
    )
    t.extra = {"trading_day": trading_day, "raw_ask1": float(r["ask_price1"])}
    return t


def load_ticks_for_range(
    std_symbol: str,
    product: str,
    exchange: Exchange,
    start: date,
    end: date,
    *,
    fill_proxy: bool = False,
) -> tuple[list[TickData], list[date]]:
    """跨多个交易日逐日取数，拼成一条按时间升序的 TickData 流。

    pre_close 维护：按交易日顺序处理，本日所有 tick 的 pre_close = 上一交易日
    最后一笔 last_price；首个有数据的交易日用当日首 tick last_price（涨跌幅=0）。

    fill_proxy=False（默认）：用 DB 原始盘口 ask1 撮合（vnpy 官方口径，保守，
        500ms 快照会漏掉砸穿盘口的瞬时乌龙）。
    fill_proxy=True：逐帧用 turnover/volume 增量反推 avg_fill，当 avg_fill 低于
        盘口 ask1 时用 avg_fill 覆盖 ask_price_1，使 vnpy 撮合改用 fat-finger 的
        "成交均价穿越挂单价"口径，能捕捉瞬时乌龙（成交价≈avg_fill）。

    Returns:
        (ticks 升序列表, 实际有数据的交易日列表)
    """
    days = trading_calendar(start, end)
    algo_mult = get_algo_multiplier(product) if fill_proxy else 1.0
    all_ticks: list[TickData] = []
    used_days: list[date] = []
    prev_close: float | None = None

    for td in days:
        rows = fetch_full_day_rows(std_symbol, product, td)
        if not rows:
            continue
        day_pre_close = prev_close if prev_close is not None else float(rows[0]["last_price"])
        prev_to: float | None = None
        prev_vol: float | None = None
        prev_ts: datetime | None = None
        for r in rows:
            ask_override: float | None = None
            if fill_proxy:
                cur_to = float(r["turnover"] or 0)
                cur_vol = float(r["volume"] or 0)
                cur_ts = r["ts"]
                if prev_to is not None:
                    d_to = cur_to - prev_to
                    d_vol = cur_vol - prev_vol
                    gap = (cur_ts - prev_ts).total_seconds()
                    lower = float(r["lower_limit_price"] or 0)
                    # 健全性防线：成交额/量均为正、间隔不过大、均价不低于跌停板
                    if d_to > 0 and d_vol > 0 and gap <= _MAX_TICK_GAP_S:
                        avg_fill = d_to / (d_vol * algo_mult)
                        if (lower <= 0 or avg_fill >= lower) and avg_fill < float(r["ask_price1"]):
                            ask_override = avg_fill     # 可信成交均价低于盘口 → 作撮合参考
                prev_to, prev_vol, prev_ts = cur_to, cur_vol, cur_ts
            all_ticks.append(
                _row_to_tick(r, std_symbol, exchange, day_pre_close, td, ask_override)
            )
        prev_close = float(rows[-1]["last_price"])  # 本日收盘价 → 次日昨收
        used_days.append(td)

    all_ticks.sort(key=lambda t: t.datetime)  # 引擎不排序，必须自己保证升序
    return all_ticks, used_days
