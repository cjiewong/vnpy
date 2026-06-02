"""30k 乌龙指远端挂买策略（单品种 tick 回测版）。

实现 doc/30k-fat-finger-trading-plan.md 的执行纪律，跑在 vnpy_ctastrategy 的
TICK 级 BacktestingEngine 上。核心是一个五态状态机：

    IDLE → QUOTING → FILLED_MANAGING → COOLDOWN → (回 QUOTING)
                                    ↘ STOPPED_TODAY（当天停挂）

设计约束（与回测引擎特性强相关）：
- 回测无真实时钟：所有 90s/30s/5min/10min 一律用 tick.datetime 差值判定。
- 交易日用 tick.extra['trading_day']（夜盘归次日），不靠时间推算，避免跨日重置错。
- session 开盘锚点用"相邻 tick 大 gap"动态建立，不写死 09:00/21:00。
- 单 symbol 引擎：本策略只管单品种纪律；"同时最多1持仓 / 单日总亏损1000"中的
  跨品种组合部分由外层 run_backtest.portfolio_merge 事后归并，这里只实现单品种单日闸。
"""
from __future__ import annotations

import math
from datetime import datetime

from vnpy.trader.constant import Direction
from vnpy.trader.object import OrderData, TickData, TradeData
from vnpy_ctastrategy.template import CtaTemplate

from ff_state import FFState

# 日盘/夜盘收盘时刻（CZCE 化工品：日盘 15:00，夜盘 23:00）。RM/MA/TA 适用。
# 其它品种夜盘收盘时刻不同（23:30/01:00/02:30），换品种需调整。
_SESSION_CLOSES: tuple[tuple[int, int], ...] = ((15, 0), (23, 0))


class FatFingerStrategy(CtaTemplate):
    """乌龙指远端挂买单品种策略。"""

    author = "fat-finger-backtest"

    # ---- 策略参数（可经 setting 覆盖）----
    x_pct = 0.04                 # 挂单价 = mid*(1-x_pct)，远端被动买
    drift_pct = 0.008            # mid 漂移超过此比例则撤旧挂新
    spread_max_pct = 0.003       # spread 超过此比例不挂/撤单
    daily_change_max = 0.02      # 当日涨跌幅超过此比例当天停挂
    open_skip_secs = 90          # 每个 session 开盘后跳过的秒数
    close_flat_secs = 300        # 收盘前多少秒全撤并停止开新仓
    hold_seconds = 30            # 成交后平仓观察窗口
    cooldown_secs = 600          # 成交后冷却时长（10 分钟不重挂）
    stop_loss_yuan = 800.0       # 单笔止损（元）
    daily_loss_yuan = 1000.0     # 单日总亏损闸（元）
    mid_revert_pct = 0.002       # 平仓回归判定阈值（|mid-fill_mid| 占比）
    session_gap_secs = 1800      # 相邻 tick 间隔超过此值视为新 session
    fixed_size = 1               # 固定下单手数
    close_retry_secs = 5         # 平仓单挂出后超时未成交则按最新对手价重发

    parameters = [
        "x_pct", "drift_pct", "spread_max_pct", "daily_change_max",
        "open_skip_secs", "close_flat_secs", "hold_seconds", "cooldown_secs",
        "stop_loss_yuan", "daily_loss_yuan", "mid_revert_pct",
        "session_gap_secs", "fixed_size", "close_retry_secs",
    ]

    # ---- 可视化/统计变量（标量）----
    state = int(FFState.IDLE)
    quote_mid = 0.0
    fill_price = 0.0
    fill_mid = 0.0
    daily_realized_pnl = 0.0
    n_quote = 0
    n_fill = 0
    n_stop_close = 0
    n_revert_close = 0
    n_timeout_close = 0

    variables = [
        "state", "quote_mid", "fill_price", "fill_mid", "daily_realized_pnl",
        "n_quote", "n_fill", "n_stop_close", "n_revert_close", "n_timeout_close",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting):
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        # 合约规格（单一真相源 = set_parameters；on_init 时取）
        self.contract_size: float = 0.0
        self.price_tick: float = 0.0
        # 非序列化运行状态
        self.session_start_dt: datetime | None = None
        self.last_tick_dt: datetime | None = None
        self.fill_dt: datetime | None = None
        self.cooldown_start_dt: datetime | None = None
        self.cur_trading_day = None
        self.session_closing = False   # 本 session 已进入收盘撤单期
        self.closing = False           # 已发平仓单，等待成交
        self.closing_dt: datetime | None = None  # 平仓单挂出时刻，用于超时重发

    # ---------------- 生命周期 ----------------
    def on_init(self):
        self.contract_size = float(self.get_size())
        self.price_tick = float(self.get_pricetick())
        self.write_log(
            f"FatFinger 初始化 size={self.contract_size} tick={self.price_tick}"
        )

    def on_start(self):
        self.write_log("FatFinger 启动")

    def on_stop(self):
        self.write_log(
            f"FatFinger 停止 | 挂单{self.n_quote} 成交{self.n_fill} "
            f"止损平{self.n_stop_close} 回归平{self.n_revert_close} "
            f"超时平{self.n_timeout_close}"
        )

    # ---------------- 主循环 ----------------
    def on_tick(self, tick: TickData):
        # 0) 交易日切换（夜盘归次日，取自 DB trading_day）
        td = tick.extra.get("trading_day") if tick.extra else tick.datetime.date()
        if td != self.cur_trading_day:
            self._reset_day(td)

        # 1) session 锚点（相邻 tick 大 gap = 新 session）
        self._update_session(tick)

        mid = (tick.bid_price_1 + tick.ask_price_1) / 2.0

        # 2) 收盘前撤单闸（任意态）
        if self._near_session_close(tick):
            self.session_closing = True
            if self.state == FFState.QUOTING:
                self.cancel_all()
                self.state = int(FFState.IDLE)

        # 3) 单日亏损闸（任意态）：撤挂单并平掉持仓，当天停止
        if self.state != FFState.STOPPED_TODAY:
            total = self.daily_realized_pnl + self._floating(mid)
            if total <= -self.daily_loss_yuan:
                if self.pos > 0 and not self.closing:
                    self._close(tick)            # 持仓裸露时先平，避免停后不管
                self.cancel_all()
                self.state = int(FFState.STOPPED_TODAY)

        # 4) 按状态分发
        s = self.state
        if s == FFState.IDLE:
            self._on_idle(tick, mid)
        elif s == FFState.QUOTING:
            self._on_quoting(tick, mid)
        elif s == FFState.FILLED_MANAGING:
            self._on_filled(tick, mid)
        elif s == FFState.COOLDOWN:
            self._on_cooldown(tick)
        # STOPPED_TODAY：当天不再动作

        self.last_tick_dt = tick.datetime
        self.put_event()

    # ---------------- 状态处理 ----------------
    def _on_idle(self, tick: TickData, mid: float):
        if self.session_closing or self.session_start_dt is None:
            return
        if (tick.datetime - self.session_start_dt).total_seconds() < self.open_skip_secs:
            return                                  # 开盘 90 秒内不挂
        self._try_quote(tick, mid)

    def _try_quote(self, tick: TickData, mid: float):
        if abs(self._daily_change(tick)) > self.daily_change_max:
            self.state = int(FFState.STOPPED_TODAY)
            return
        spread = (tick.ask_price_1 - tick.bid_price_1) / mid
        if spread > self.spread_max_pct:
            return                                  # spread 太宽，留 IDLE 等下个 tick
        if self._place_quote(tick, mid):
            self.state = int(FFState.QUOTING)

    def _on_quoting(self, tick: TickData, mid: float):
        if abs(self._daily_change(tick)) > self.daily_change_max:
            self.cancel_all()
            self.state = int(FFState.STOPPED_TODAY)
            return
        spread = (tick.ask_price_1 - tick.bid_price_1) / mid
        if spread > self.spread_max_pct:
            self.cancel_all()
            self.state = int(FFState.IDLE)
            return
        if self.quote_mid > 0:
            drift = abs(mid - self.quote_mid) / self.quote_mid
            if drift > self.drift_pct:              # mid 漂移：撤旧挂新
                self.cancel_all()
                self._place_quote(tick, mid)        # 仍 QUOTING

    def _on_filled(self, tick: TickData, mid: float):
        if self.closing:
            # 平仓单已挂出。极端情况下（对手价持续低于挂出价）可能多 tick 不成交，
            # 超过 close_retry_secs 仍未成交则按最新对手价重发，避免持仓卡死。
            if self.closing_dt is not None and (
                tick.datetime - self.closing_dt
            ).total_seconds() > self.close_retry_secs:
                self._close(tick)
            return
        floating = self._floating(mid)
        held = (tick.datetime - self.fill_dt).total_seconds() if self.fill_dt else 0.0
        revert = (
            self.fill_mid > 0
            and abs(mid - self.fill_mid) <= self.fill_mid * self.mid_revert_pct
        )
        if floating <= -self.stop_loss_yuan:        # 单笔止损
            self.n_stop_close += 1
            self._close(tick)
        elif self.session_closing:                  # 收盘强平
            self.n_timeout_close += 1
            self._close(tick)
        elif revert:                                # mid 回归，计划平仓
            self.n_revert_close += 1
            self._close(tick)
        elif held > self.hold_seconds:              # 超时未回归，对手价平
            self.n_timeout_close += 1
            self._close(tick)

    def _on_cooldown(self, tick: TickData):
        if self.cooldown_start_dt is None:
            self.state = int(FFState.IDLE)
            return
        if (tick.datetime - self.cooldown_start_dt).total_seconds() > self.cooldown_secs:
            self.state = int(FFState.IDLE)

    # ---------------- 下单 ----------------
    def _place_quote(self, tick: TickData, mid: float) -> bool:
        raw = mid * (1 - self.x_pct)
        price = math.floor(raw / self.price_tick) * self.price_tick  # 向下取整不抬价
        if price <= 0 or price >= tick.ask_price_1:
            return False                            # 必须低于卖一价才是"等待"，否则放弃
        self.buy(price, self.fixed_size)
        self.quote_mid = mid
        self.n_quote += 1
        return True

    def _close(self, tick: TickData):
        if self.pos <= 0:
            return
        self.cancel_all()                           # 重发前撤掉旧平仓单
        self.sell(tick.bid_price_1, abs(self.pos))  # 对手价平多，回测下个 tick 成交
        self.closing = True
        self.closing_dt = tick.datetime

    # ---------------- 回调 ----------------
    def on_trade(self, trade: TradeData):
        if trade.direction == Direction.LONG:       # 买入开仓成交
            self.cancel_all()                       # 成交即撤其它未成交单
            self.fill_price = trade.price
            self.fill_dt = trade.datetime
            self.fill_mid = self.quote_mid           # 事件前 mid 近似
            self.n_fill += 1
            self.closing = False
            self.state = int(FFState.FILLED_MANAGING)
        else:                                        # 卖出平仓成交
            realized = (trade.price - self.fill_price) * self.contract_size * trade.volume
            self.daily_realized_pnl += realized
            self.closing = False
            if self.state == FFState.STOPPED_TODAY:
                pass                                 # 单日闸/涨跌幅停后的平仓，保持停止
            else:
                self.cooldown_start_dt = trade.datetime
                self.state = int(FFState.COOLDOWN)

    def on_order(self, order: OrderData):
        pass

    # ---------------- 工具 ----------------
    def _floating(self, mid: float) -> float:
        if self.pos == 0:
            return 0.0
        return (mid - self.fill_price) * self.contract_size * self.pos

    def _daily_change(self, tick: TickData) -> float:
        if tick.pre_close > 0:
            return (tick.last_price - tick.pre_close) / tick.pre_close
        return 0.0

    def _near_session_close(self, tick: TickData) -> bool:
        t = tick.datetime
        cur = t.hour * 3600 + t.minute * 60 + t.second
        for h, m in _SESSION_CLOSES:
            close_sec = h * 3600 + m * 60
            if close_sec - self.close_flat_secs <= cur < close_sec:
                return True
        return False

    def _update_session(self, tick: TickData):
        if self.last_tick_dt is None:
            self.session_start_dt = tick.datetime
            self.session_closing = False
            return
        gap = (tick.datetime - self.last_tick_dt).total_seconds()
        if gap > self.session_gap_secs:             # 跨集合竞价/休市/跨日
            self.session_start_dt = tick.datetime
            self.session_closing = False
            # QUOTING 撤旧挂单回 IDLE；COOLDOWN 也在新 session 显式归零，不依赖隐式超时
            if self.state in (FFState.QUOTING, FFState.COOLDOWN):
                self.cancel_all()
                self.state = int(FFState.IDLE)

    def _reset_day(self, td):
        self.cur_trading_day = td
        self.daily_realized_pnl = 0.0
        if self.state == FFState.STOPPED_TODAY:     # 新交易日恢复
            self.state = int(FFState.IDLE)
        # 跨日兜底：若持仓平仓单未成交带入次日，清 closing 让次日重新管理，避免死锁
        if self.state == FFState.FILLED_MANAGING and self.closing:
            self.closing = False
            self.closing_dt = None
