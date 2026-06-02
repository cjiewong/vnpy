# fat_finger_backtest — 30k 乌龙指策略 vnpy 回测

把 `fat-finger` 项目 `doc/30k-fat-finger-trading-plan.md` 的「3 万本金乌龙指挂单方案」
落成一套基于 `vnpy_ctastrategy` 的 **单品种 TICK 级回测系统**。数据直连 NAS
TimescaleDB（`md.futures_tick`，500ms 快照），绕过 vnpy 数据库，构造 `TickData`
列表直接注入回测引擎。

## 为什么用 vnpy（而不是直接扩展 fat-finger 的事件回测）

`fat-finger` 已有一套**事件驱动**回测（先扫出乌龙事件，再在事件点判触发）。
本系统的增量价值是**时间驱动**：把方案里大量「执行纪律」放进逐 tick 历史回放——
开盘 90 秒不挂、mid 漂移重挂、spread/涨跌幅停挂、收盘撤单、成交后 30 秒平仓/止损、
单笔与单日止损、成交后冷却——这些只能在逐 tick 回放里模拟，且策略代码将来可平滑接
CTP 实盘。

## 架构与文件

| 文件 | 职责 |
|---|---|
| `ff_common.py` | DB 连接（密码走环境变量/仓库外 secrets）、CZCE 三位码映射、真实乘数、最小变动价、ts 窗口 |
| `tick_source.py` | 单分区+ts 窗口取数、DB row→TickData、跨日拼接、pre_close 兜底 |
| `contract_selector.py` | 选每品种「非主力远月且有流动性」合约 |
| `ff_state.py` | 状态枚举 `FFState` |
| `fat_finger_strategy.py` | `FatFingerStrategy(CtaTemplate)` 五态状态机 |
| `run_backtest.py` | 回测编排：取数→注入→run→统计 |
| `portfolio_merge.py` | 跨品种组合风控（最多1持仓/单日1000）事后归并 |
| `tests/test_transition.py` | 人工 tick 流验证成交/止损/停挂链路 |

## 状态机

```
IDLE ──开盘90s后&spread合格&涨跌幅合格──▶ QUOTING ──ask1砸破挂单价成交──▶ FILLED_MANAGING
 ▲  ▲                                   │ mid漂移>0.8%→撤旧挂新(仍QUOTING)        │
 │  │                                   │ spread>0.3%→撤单回IDLE                  │ 止损/回归/超时/收盘
 │  └─────cooldown 600s 到─── COOLDOWN ◀─┴─平仓成交───────────────────────────────┘
 │
 └── 收盘前300s 全撤 / 新 session 重置 / 跨日重置
 任意态 ── 当日涨跌幅>2% 或 单日亏损达1000 ──▶ STOPPED_TODAY（当天停）
```

- **时间**全部用 `tick.datetime` 差值（回测无真实时钟）。
- **交易日**用 `tick.extra['trading_day']`（夜盘归次日），不靠时间推算，跨日重置精确。
- **session 锚点**用相邻 tick 大 gap 动态建立，不写死 09:00/21:00。

## 用法

```bash
# 数据库密码：环境变量优先，否则读 /Users/mac/dev/fat-finger/config/secrets.toml（仓库外）
export FAT_FINGER_DB_PASSWORD=...   # 可选

cd /Users/mac/Quant/vnpy/examples/fat_finger_backtest
PY=/Users/mac/Quant/vnpy/.venv/bin/python

# 单元测试（验证状态机，不连 DB 撮合逻辑）
$PY tests/test_transition.py

# 三品种全 Q2 回测（rate/slippage=0 看理论上限；ask1 口径，乌龙稀缺→零成交）
$PY run_backtest.py --products RM,MA,TA --start 2025-04-01 --end 2025-06-30 --probe 2025-06-03

# 真实乌龙捕捉对比：ask1 官方口径 vs avg_fill 口径（见 RESULTS.md 第二节）
$PY capture_demo.py

# 组合层风控验证：23 个真实乌龙归并，验证"最多1持仓+单日1000"（见 RESULTS.md 第四节）
$PY portfolio_demo.py
```

`run_single(..., fill_proxy=True)` 切换到 avg_fill 撮合口径——用 turnover/volume 增量
反推成交均价覆盖盘口 ask1，捕捉 500ms 快照盘口抹平的瞬时乌龙（带负增量/跨间隔/跌停防线）。

## ⚠️ 口径声明（必读，否则会高估实盘可行性）

本回测给出的是**理论成交上限**，实盘需大幅打折。

### 撮合口径：vnpy 官方 vs fat-finger 保守口径

| 维度 | vnpy 回测（本系统） | fat-finger 保守口径 | 影响 |
|---|---|---|---|
| 成交价 | `ask1` 触及即成交，价=`min(挂单价, ask1)`（给最优价） | `avg_fill=dT/(dV×mult)` 反推真实成交均价 | vnpy **偏乐观** |
| 成交量 | 满足条件即全额成交，**不看 ask_volume1** | 受真实盘口挂单量限制 | vnpy **高估成交** |
| 盘口为空 | `bid/ask1=0` 的 tick 已在取数阶段过滤 | 用 turnover 反推，盘口空也能算 | 砸盘瞬间快照盘口被打空时，vnpy **可能漏掉** |
| 滑点 | `slippage` 不进成交价，仅按手数扣成本 | 冲击成本应折进成交价 | 首轮 slippage=0 则**无冲击成本** |
| 队列 | 无排队，挂单价≥ask1 即成交 | 真实有队列优先级/撤单延迟/部分成交 | 成交率**显著高于实盘** |

### 其它必声明项

- **500ms 快照**：瞬时极价可能被采样漏掉，挂单触发概率以「整帧」口径估计。
- **保证金/资金成本未计**：年化收益被高估。
- **组合风控**「最多1持仓/单日1000」是跨品种约束，单 symbol 回测测不出，仅由
  `portfolio_merge.py` 事后近似裁剪。
- **平仓价偏乐观**：回测对手价平 `sell(bid1)`，乌龙回归瞬间 bid1 也剧烈跳动，止损滑点被低估。

**结论**：回测盈利 ≠ 实盘可复制，「排得到队」是独立前提。fat-finger 文档建议对此类
结果打 30–50% 折扣。

## 已知限制

1. **单 symbol 引擎**：RM/MA/TA 各自独立回测，组合层风控靠 `portfolio_merge` 事后归并。
2. **收盘时刻硬编码** `_SESSION_CLOSES=((15,0),(23,0))`，仅适用 RM/MA/TA（CZCE 化工夜盘 23:00）；换品种需调整。
3. **远月清淡**：离到期远的合约盘口常空、spread 宽，挂单/成交都稀疏，属正常现象。
4. **首轮区间** 2025 Q2 避开 2026-04 末端数据残缺区。

## 实盘前仿真（第一层，mac 可跑）

`replay_gateway.py` + `run_live_sim.py`：在 vnpy **实盘引擎栈**(EventEngine+MainEngine+
CtaEngine)上用历史 tick 回放驱动策略，订单/成交/撤单走 **CTP 异步回报语义**
(SUBMITTING→NOTTRADED→ALLTRADED，撤单回报延迟，成交延迟一 tick)，验证策略从回测的
同步撮合迁移到实盘异步环境是否健壮。

```bash
$PY run_live_sim.py            # 默认 TA2509 2025-04-10（一个真实乌龙日）
```

健壮性判据：末持仓=0 且 在途订单峰值≤1（无双挂单）。本仿真暴露并修复了两个回测掩盖的
真实问题：

1. **双挂单**：漂移重挂时 `cancel_all()` 后立即挂新单——回测同步撤单即时生效没事，
   实盘异步撤单有延迟，旧单未离场新单已挂出，两个买单同时在场可能超买。
   修复：策略层用 `active_orderids` 跟踪活动委托，**撤完(active 空)再挂**。
2. **统计计数持久化累积**：`n_fill` 等运行统计原放进 `variables`，被 CtaEngine 持久化，
   每次重启恢复旧值再累加致虚高。修复：`variables` 只留需重启恢复的交易状态
   (state/quote_mid/fill_price/fill_mid/daily_realized_pnl)，统计计数移出。

> 第二层（NAS x86_64 Docker 跑 vnpy_ctp 连 SimNow 真实模拟柜台）见任务进度，需 SimNow 账号。

## 回测结果

见 `RESULTS.md`（由 `run_backtest.py` 产出后整理）。
