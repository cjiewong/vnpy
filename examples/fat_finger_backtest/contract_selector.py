"""选取每品种的"非主力远月且有流动性"合约。

30k 方案要在非主力、但仍有流动性的远月合约上挂远端买单等乌龙。本模块：
  1. 从 md.futures_continuous_map 取探测日主力（用于排除）；
  2. 从 md.futures_tick 取探测日该品种各合约盘口 tick 量作流动性代理；
  3. 在流动性达标(>=min_ticks)且非主力的合约里，选月份最远的一个。

合约码统一用 DB tick 表的原始码（CZCE 三位年月码，如 RM601），直接作 vnpy symbol，
不反推四位标准码以规避三位码的世纪歧义。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ff_common import fetch_all, trading_day_ts_window

_SQL_MAIN = """
SELECT instrument_id FROM md.futures_continuous_map
WHERE trading_day = %(td)s AND product_code = %(pc)s AND continuous_type = 'main'
LIMIT 1;
"""

_SQL_LIQUIDITY = """
SELECT instrument_id, count(*) AS n
FROM md.futures_tick
WHERE instrument_id LIKE %(prefix)s AND trading_day = %(td)s
  AND ts >= %(ts_start)s AND ts < %(ts_end)s
  AND bid_price1 > 0 AND ask_price1 > 0
GROUP BY instrument_id
ORDER BY n DESC;
"""


@dataclass(slots=True)
class FarMonthPick:
    product: str
    probe_day: date
    main: str | None              # 探测日主力合约
    picked: str                   # 选中的非主力远月合约
    picked_ticks: int             # 选中合约当日盘口 tick 数
    candidates: list[tuple[str, int]]  # 全部达标候选 (合约, tick数)，按月份远→近


def _contract_year_month(inst: str, product: str, ref: date) -> tuple[int, int]:
    """把合约码解析成 (年, 月) 以比较远近。

    支持 CZCE 三位年月码(RM601=Y6 月01)与四位年份码(RM2601=26年01月)。
    三位码用 ref 交易日锚定世纪：取同一年代里 >= ref.year 的最近年份。
    """
    suffix = inst[len(product):]
    if len(suffix) == 4 and suffix.isdigit():        # 四位 YYMM
        return 2000 + int(suffix[:2]), int(suffix[2:])
    if len(suffix) == 3 and suffix.isdigit():        # 三位 YMM
        y_digit = int(suffix[0])
        month = int(suffix[1:])
        decade = (ref.year // 10) * 10
        year = decade + y_digit
        if year < ref.year:                          # 已过期则进下一个十年
            year += 10
        return year, month
    return ref.year, 99                              # 异常码排最后


def fetch_main(product: str, probe_td: date) -> str | None:
    rows = fetch_all(_SQL_MAIN, {"td": probe_td, "pc": product})
    return rows[0]["instrument_id"] if rows else None


def pick_far_month(product: str, probe_td: date, *, min_ticks: int = 8000) -> FarMonthPick:
    """选探测日该品种流动性达标、非主力、月份最远的合约。"""
    main = fetch_main(product, probe_td)
    ts_start, ts_end = trading_day_ts_window(probe_td)
    rows = fetch_all(
        _SQL_LIQUIDITY,
        {"prefix": f"{product}%", "td": probe_td, "ts_start": ts_start, "ts_end": ts_end},
    )
    candidates = [
        (r["instrument_id"], int(r["n"]))
        for r in rows
        if int(r["n"]) >= min_ticks and r["instrument_id"] != main
    ]
    if not candidates:
        raise RuntimeError(
            f"{product} @ {probe_td} 无流动性达标(>={min_ticks})的非主力合约，"
            "请降低 min_ticks 或换探测日"
        )
    # 按月份远→近排序，取最远的达标合约
    candidates.sort(key=lambda x: _contract_year_month(x[0], product, probe_td), reverse=True)
    picked, picked_ticks = candidates[0]
    return FarMonthPick(
        product=product, probe_day=probe_td, main=main,
        picked=picked, picked_ticks=picked_ticks, candidates=candidates,
    )
