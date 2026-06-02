"""历史 tick 回放网关：在 vnpy 实盘引擎栈上模拟 CTP 的异步订单/成交/撤单回报。

与 BacktestingEngine 的同步撮合不同，本网关刻意复刻实盘的异步语义，用于实盘前验证：
- send_order：先推 SUBMITTING 再推 NOTTRADED（柜台接受），返回 vt_orderid；
- 成交：在**下一个** tick 的撮合阶段才发生（延迟一 tick），推 ALLTRADED + on_trade；
- cancel_order：标记待撤，下一 tick 才推 CANCELLED（撤单回报异步到达）；
- 持仓 pos 由 CtaEngine 在收到 on_trade 后更新（非同步即时）。

这些异步延迟正是回测同步撮合掩盖、而实盘真实存在的，能暴露策略状态机的时序 bug
（撤单未回报就重挂导致双挂单、平仓单回报延迟期间的处理等）。

撮合口径复用 tick_source.fill_proxy：False=盘口 ask1（保守），True=avg_fill（捕捉乌龙）。
"""
from __future__ import annotations

import threading
import time
from copy import copy
from datetime import date

from vnpy.trader.constant import Direction, Exchange, Product, Status
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    OrderRequest,
    SubscribeRequest,
    TickData,
    TradeData,
)

import tick_source as ts
from ff_common import get_pricetick, get_real_multiplier


class ReplayGateway(BaseGateway):
    """历史 tick 回放 + CTP 异步回报仿真网关。"""

    default_name = "REPLAY"
    default_setting: dict = {}
    exchanges = [Exchange.CZCE]

    def __init__(self, event_engine, gateway_name: str = "REPLAY"):
        super().__init__(event_engine, gateway_name)
        self._ticks: list[TickData] = []
        self._vt_symbol = ""
        self._order_count = 0
        self._trade_count = 0
        self._active: dict[str, "OrderData"] = {}   # vt_orderid -> OrderData
        self._pending_cancel: set[str] = set()      # 待撤 orderid（gateway scope）
        self._lock = threading.Lock()
        self._replay_thread: threading.Thread | None = None
        self._interval = 0.002
        # 观测记录（供验证）
        self.trades: list[TradeData] = []
        self.order_events: list[tuple] = []         # (ts, vt_orderid, status, dir, price)
        self.max_concurrent_active = 0              # 同时在途订单峰值（>1 即双挂单）

    # ---------------- BaseGateway 抽象实现 ----------------
    def connect(self, setting: dict) -> None:
        product, sym = setting["product"], setting["symbol"]
        d = date.fromisoformat(setting["date"])
        fill_proxy = setting.get("fill_proxy", False)
        self._interval = setting.get("interval", 0.002)

        ticks, _ = ts.load_ticks_for_range(
            sym, product, Exchange.CZCE, d, d, fill_proxy=fill_proxy
        )
        self._ticks = ticks
        self._vt_symbol = f"{sym}.{Exchange.CZCE.value}"

        contract = ContractData(
            symbol=sym, exchange=Exchange.CZCE, name=sym, product=Product.FUTURES,
            size=get_real_multiplier(product), pricetick=get_pricetick(product),
            min_volume=1, gateway_name=self.gateway_name,
        )
        self.on_contract(contract)   # 必须在策略 send_order 前到位
        self.write_log(
            f"REPLAY 连接 {sym} {d} | tick={len(ticks)} | fill_proxy={fill_proxy}"
        )

    def subscribe(self, req: SubscribeRequest) -> None:
        pass  # 单合约回放，订阅无需额外动作

    def send_order(self, req: OrderRequest) -> str:
        with self._lock:
            self._order_count += 1
            order = req.create_order_data(str(self._order_count), self.gateway_name)
            order.status = Status.SUBMITTING
            self.on_order(copy(order))                      # 异步回报①：已提交
            self._record(order)
            order.status = Status.NOTTRADED
            self._active[order.vt_orderid] = order
            self.on_order(copy(order))                      # 异步回报②：柜台接受
            self._record(order)
            self.max_concurrent_active = max(self.max_concurrent_active, len(self._active))
            return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        with self._lock:
            self._pending_cancel.add(req.orderid)           # 延迟到下一 tick 才回报撤单

    def query_account(self) -> None:
        pass

    def query_position(self) -> None:
        pass

    def close(self) -> None:
        pass

    # ---------------- 回放驱动 ----------------
    def start_replay(self) -> None:
        self._replay_thread = threading.Thread(target=self._run_replay, daemon=True)
        self._replay_thread.start()

    def join(self) -> None:
        if self._replay_thread:
            self._replay_thread.join()

    def _run_replay(self) -> None:
        for tick in self._ticks:
            self._match(tick)        # 撮合上一轮挂出的在途单（成交延迟一 tick = 异步）
            self._do_cancels(tick)   # 处理待撤单（撤单回报异步）
            self.on_tick(tick)       # 推 tick → 策略 on_tick（可能下/撤单，进下一轮）
            time.sleep(self._interval)  # 让 EventEngine 处理完该 tick 的级联事件
        self.write_log(
            f"REPLAY 回放结束 | 成交 {len(self.trades)} 笔 | 在途峰值 {self.max_concurrent_active}"
        )

    def _match(self, tick: TickData) -> None:
        with self._lock:
            for vt_orderid, order in list(self._active.items()):
                cross, trade_price = False, 0.0
                if order.direction == Direction.LONG:
                    if tick.ask_price_1 > 0 and order.price >= tick.ask_price_1:
                        cross, trade_price = True, min(order.price, tick.ask_price_1)
                else:
                    if tick.bid_price_1 > 0 and order.price <= tick.bid_price_1:
                        cross, trade_price = True, max(order.price, tick.bid_price_1)
                if not cross:
                    continue
                order.traded = order.volume
                order.status = Status.ALLTRADED
                order.datetime = tick.datetime
                self.on_order(copy(order))                  # 成交回报：全部成交
                self._record(order)
                self._active.pop(vt_orderid, None)

                self._trade_count += 1
                trade = TradeData(
                    symbol=order.symbol, exchange=order.exchange, orderid=order.orderid,
                    tradeid=str(self._trade_count), direction=order.direction,
                    offset=order.offset, price=trade_price, volume=order.volume,
                    datetime=tick.datetime, gateway_name=self.gateway_name,
                )
                self.on_trade(copy(trade))
                self.trades.append(trade)

    def _do_cancels(self, tick: TickData) -> None:
        with self._lock:
            if not self._pending_cancel:
                return
            for orderid in list(self._pending_cancel):
                for vt_orderid, order in list(self._active.items()):
                    if order.orderid == orderid:
                        order.status = Status.CANCELLED
                        order.datetime = tick.datetime
                        self.on_order(copy(order))          # 撤单回报
                        self._record(order)
                        self._active.pop(vt_orderid, None)
                self._pending_cancel.discard(orderid)

    def _record(self, order) -> None:
        self.order_events.append(
            (order.datetime, order.vt_orderid, order.status.value,
             order.direction.value, order.price)
        )
