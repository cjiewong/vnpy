"""组合层风控验证：把 fat-finger 已检测的乌龙事件批量喂进来，验证 portfolio_merge。

输入 umbrella_events.json（fat-finger 检测的 RM/MA/TA/FG 可捕捉乌龙：下砸 4%-8% 的孤立瞬间），
对每个事件日跑 avg_fill 口径单日回测收集成交，按品种配对后用 merge_portfolio 套用
"同时最多 1 持仓 + 单日组合亏损 1000 即停"，对比单品种独立盈亏与组合裁剪后盈亏。

这验证的是 vnpy 单 symbol 回测测不出的跨品种组合风控——尤其同一交易日多品种/多合约
同时出乌龙时，组合层只允许 1 个持仓、单日亏损达闸即停的裁剪效果。

用法：/Users/mac/Quant/vnpy/.venv/bin/python portfolio_demo.py [events.json]
events.json 由 fat-finger 侧导出（见 README）；默认读同目录 umbrella_events.json。
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vnpy.trader.constant import Exchange  # noqa: E402

from ff_common import get_real_multiplier  # noqa: E402
from portfolio_merge import merge_portfolio, pair_trades  # noqa: E402
from run_backtest import run_single  # noqa: E402

_DEFAULT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "umbrella_events.json")


def main():
    events_path = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_JSON
    with open(events_path, encoding="utf-8") as f:
        events = json.load(f)

    pairs_by_product: dict[str, list] = defaultdict(list)
    indep_pnl = 0.0
    n_captured = 0
    captured_events = 0

    print(f"对 {len(events)} 个乌龙事件逐日跑 avg_fill 口径回测并收集成交...\n")
    for ev in events:
        product, inst, d = ev["product"], ev["instrument"], date.fromisoformat(ev["date"])
        try:
            o = run_single(product, inst, Exchange.CZCE, d, d, fill_proxy=True)
        except Exception as e:  # noqa: BLE001
            print(f"  {inst} {ev['date']} 跳过: {repr(e)[:50]}")
            continue
        if o is None or not o.trades:
            continue
        size = get_real_multiplier(product)
        pairs = pair_trades(product, o.trades, size)
        if pairs:
            pairs_by_product[product].extend(pairs)
            indep_pnl += sum(p.pnl for p in pairs)
            n_captured += len(pairs)
            captured_events += 1

    res = merge_portfolio(pairs_by_product)

    print(f"\n{'='*64}")
    print("单品种独立口径（各自回测，不考虑跨品种约束）：")
    print(f"  捕捉事件 {captured_events}/{len(events)} | 成交对 {n_captured} | "
          f"独立总盈亏 {indep_pnl:.0f} 元")
    print("\n组合层口径（同时最多 1 持仓 + 单日组合亏损 1000 即停）：")
    print(f"  {res.summary()}")
    cut = indep_pnl - res.total_pnl
    print(f"\n组合风控裁剪掉 {cut:.0f} 元（独立 {indep_pnl:.0f} → 组合 {res.total_pnl:.0f}）")
    if res.rejected_overlap:
        print(f"  其中 {len(res.rejected_overlap)} 笔因'已有持仓'被拒（同时多乌龙只吃 1 个）：")
        for p in res.rejected_overlap[:5]:
            print(f"    {p.product} {p.entry_dt:%Y-%m-%d %H:%M} pnl={p.pnl:.0f}")
    if res.rejected_daily_stop:
        print(f"  其中 {len(res.rejected_daily_stop)} 笔因'单日闸'被拒")


if __name__ == "__main__":
    main()
