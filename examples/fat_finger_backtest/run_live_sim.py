"""实盘前仿真：用 vnpy 实盘引擎栈(EventEngine+MainEngine+CtaEngine)跑 FatFingerStrategy。

行情来自 ReplayGateway 的历史 tick 回放，订单/成交/撤单走 CTP 异步回报语义。
对比同一乌龙日的回测结果，验证策略在异步环境下纪律正确、无双挂单/closing 卡死等时序 bug。

用法：/Users/mac/Quant/vnpy/.venv/bin/python run_live_sim.py [SYMBOL] [DATE] [PRODUCT]
默认 TA2509 2025-04-10 TA（一个真实乌龙日）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy_ctastrategy import CtaStrategyApp  # noqa: E402

from fat_finger_strategy import FatFingerStrategy  # noqa: E402
from ff_common import product_code_from_instrument  # noqa: E402
from ff_state import FFState  # noqa: E402
from replay_gateway import ReplayGateway  # noqa: E402


def run_sim(symbol: str, day: str, product: str, *, fill_proxy: bool = True, interval: float = 0.002):
    ee = EventEngine()
    me = MainEngine(ee)
    me.add_gateway(ReplayGateway)
    cta = me.add_app(CtaStrategyApp)
    cta.init_engine()
    cta.classes["FatFingerStrategy"] = FatFingerStrategy   # 手动注册自定义策略类

    vt_symbol = f"{symbol}.CZCE"
    setting = {"symbol": symbol, "product": product, "date": day,
               "fill_proxy": fill_proxy, "interval": interval}
    me.connect(setting, "REPLAY")

    # 等 ContractData 经事件引擎落入 OmsEngine（实盘 send_order 依赖 get_contract）
    for _ in range(60):
        if me.get_contract(vt_symbol):
            break
        time.sleep(0.05)
    if not me.get_contract(vt_symbol):
        raise RuntimeError("ContractData 未就绪，无法下单")

    cta.add_strategy("FatFingerStrategy", "ff_sim", vt_symbol, {})
    cta.init_strategy("ff_sim").result()   # 等 on_init 完成（线程池 Future）
    cta.start_strategy("ff_sim")

    gw = me.get_gateway("REPLAY")
    gw.start_replay()
    gw.join()
    time.sleep(0.5)                        # 等最后一批回报事件处理完

    strat = cta.strategies["ff_sim"]
    result = {
        "n_quote": strat.n_quote, "n_fill": strat.n_fill,
        "n_stop_close": strat.n_stop_close, "n_revert_close": strat.n_revert_close,
        "n_timeout_close": strat.n_timeout_close,
        "final_pos": strat.pos, "final_state": FFState(strat.state).name,
        "daily_realized_pnl": strat.daily_realized_pnl,
        "gw_trades": len(gw.trades), "max_concurrent_active": gw.max_concurrent_active,
    }
    me.close()
    return result


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "TA2509"
    day = sys.argv[2] if len(sys.argv) > 2 else "2025-04-10"
    product = sys.argv[3] if len(sys.argv) > 3 else product_code_from_instrument(symbol)

    print(f"实盘仿真：{symbol} {day}（异步 CTP 回报语义，avg_fill 口径）\n")
    r = run_sim(symbol, day, product, fill_proxy=True)
    print("\n" + "=" * 56)
    print(f"  挂单 n_quote          = {r['n_quote']}")
    print(f"  成交 n_fill           = {r['n_fill']}  (网关成交 {r['gw_trades']})")
    print(f"  平仓 止损/回归/超时    = {r['n_stop_close']}/{r['n_revert_close']}/{r['n_timeout_close']}")
    print(f"  已实现盈亏            = {r['daily_realized_pnl']:.1f}")
    print(f"  末态 / 末持仓         = {r['final_state']} / {r['final_pos']}")
    print(f"  在途订单峰值          = {r['max_concurrent_active']}  (>1 表示出现双挂单)")
    # 健壮性判据
    ok = (r["final_pos"] == 0 and r["max_concurrent_active"] <= 1)
    print(f"\n  异步健壮性：{'✓ 通过（无残留持仓、无双挂单）' if ok else '✗ 异常，需排查'}")


if __name__ == "__main__":
    main()
