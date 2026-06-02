"""乌龙指回测的公共基础设施：数据库连接、合约码映射、乘数与时间窗口。

本模块自包含，逻辑口径精确对齐 fat-finger 项目
(`/Users/mac/dev/fat-finger/src/fat_finger/{tick_scanner,instruments}.py`)，
在此重写仅为让 vnpy/examples 不强依赖 fat-finger 的包结构与重型依赖
(polars/duckdb/streamlit)。若 fat-finger 侧口径变更，这里需同步。

安全红线：数据库密码绝不硬编码、绝不写入 vnpy 仓库。
  优先读环境变量 FAT_FINGER_DB_PASSWORD，否则回退读 fat-finger 仓库外的
  config/secrets.toml（该文件不在 vnpy 仓库内，不会被本仓库 git 跟踪）。
"""
from __future__ import annotations

import os
import tomllib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row

_SH_TZ = ZoneInfo("Asia/Shanghai")

# fat-finger 仓库外的密码文件（vnpy 仓库不跟踪它）。
# 路径可用环境变量 FAT_FINGER_SECRETS 覆盖，便于换机器/换用户运行。
_FF_SECRETS = Path(
    os.environ.get("FAT_FINGER_SECRETS", "/Users/mac/dev/fat-finger/config/secrets.toml")
)

# 非敏感连接参数（对齐 fat-finger config/default.toml）。
# host=dxp2800 是 tailscale magic DNS，本机可直达 NAS，无需 ssh tunnel。
DB_PARAMS: dict[str, Any] = {
    "host": "dxp2800",
    "port": 15432,
    "dbname": "quant",
    "user": "quant",
    "connect_timeout": 10,
}

# 郑商所品种集合：DB tick 表用 CTP 三位年月码（FG603），日线候选用四位年份码（FG2603）。
# 精确照搬 fat-finger tick_scanner._CZCE_PRODUCTS。
_CZCE_PRODUCTS = {
    "AP", "CF", "CJ", "CY", "FG", "JR", "LR", "MA", "OI", "PF", "PK",
    "PL", "PM", "PR", "PX", "RI", "RM", "RS", "SA", "SF", "SH", "SM",
    "SR", "TA", "UR", "WH", "ZC",
}

# CZCE 品种真实合约乘数（吨/手）。展示与盈亏计算必须用真实乘数；
# 算法侧反推 avg_fill 才用归一乘数 1。两者不可混用（fat-finger 已踩过的坑）。
# 照搬 fat-finger instruments._CZCE_REAL_MULTIPLIERS。
_CZCE_REAL_MULTIPLIERS: dict[str, float] = {
    "AP": 10, "CJ": 5, "PK": 5, "RS": 10, "SF": 5, "SM": 5, "UR": 20,
    "CF": 5, "FG": 20, "MA": 10, "OI": 10, "PF": 5, "PR": 15, "PX": 5,
    "RM": 10, "SA": 20, "SH": 30, "SR": 10, "TA": 5, "WH": 20, "ZC": 100,
}

# 郑商所最小变动价位（元/吨），来源：郑商所合约规格。
# 回测 set_parameters(pricetick=...) 与策略 round_to 用。
_CZCE_PRICETICK: dict[str, float] = {
    "RM": 1.0,   # 菜粕
    "MA": 1.0,   # 甲醇
    "TA": 2.0,   # PTA
    "FG": 1.0,   # 玻璃
    "SA": 1.0,   # 纯碱
}


def product_code_from_instrument(instrument_id: str) -> str:
    """从合约代码提取品种代码，保留原始大小写（CZCE 大写、其它小写）。"""
    i = 0
    while i < len(instrument_id) and instrument_id[i].isalpha():
        i += 1
    if i == 0:
        raise ValueError(f"无法识别品种代码：{instrument_id}")
    return instrument_id[:i]


def tick_instrument_id(std_symbol: str, product_code: str) -> str:
    """标准四位年份码 → DB tick 表所用的合约码。

    CZCE 品种在 tick 表用 CTP 三位年月码，如 RM2603 -> RM603；
    其它交易所合约码与标准码一致，原样返回。
    """
    if product_code.upper() not in _CZCE_PRODUCTS:
        return std_symbol
    suffix = std_symbol[len(product_code):]
    if len(suffix) == 4 and suffix.isdigit():
        return f"{product_code}{suffix[-3:]}"
    return std_symbol


def get_real_multiplier(instrument_or_product: str) -> float:
    """真实合约乘数（吨/手），用于回测 size 与盈亏计算。"""
    pc = product_code_from_instrument(instrument_or_product).upper()
    if pc in _CZCE_REAL_MULTIPLIERS:
        return _CZCE_REAL_MULTIPLIERS[pc]
    raise KeyError(f"品种 {pc} 缺真实乘数定义，请在 _CZCE_REAL_MULTIPLIERS 补充")


def get_pricetick(instrument_or_product: str) -> float:
    """最小变动价位（元）。"""
    pc = product_code_from_instrument(instrument_or_product).upper()
    if pc in _CZCE_PRICETICK:
        return _CZCE_PRICETICK[pc]
    raise KeyError(f"品种 {pc} 缺最小变动价定义，请在 _CZCE_PRICETICK 补充")


def get_algo_multiplier(instrument_or_product: str) -> float:
    """算法归一乘数，用于从 turnover 增量反推每帧成交均价 avg_fill。

    CZCE 品种 turnover 字段本身不乘合约乘数，故归一为 1（avg_fill=dT/dV）；
    其它交易所 turnover 已乘乘数，需除以真实乘数（avg_fill=dT/(dV*mult)）。
    """
    pc = product_code_from_instrument(instrument_or_product).upper()
    if pc in _CZCE_PRODUCTS:
        return 1.0
    return get_real_multiplier(instrument_or_product)


def trading_day_ts_window(td: date) -> tuple[datetime, datetime]:
    """单交易日的 ts 裁剪窗口：[(td-1) 20:00, td 16:00) Asia/Shanghai。

    TimescaleDB 的 trading_day 不是时间分区维度，单交易日查询必须同时带 ts 窗口，
    否则 ChunkAppend 会扫大量压缩 chunk 触发 lock 超限。窗口按中国期货夜盘口径取。
    """
    return (
        datetime.combine(td - timedelta(days=1), time(20, 0), tzinfo=_SH_TZ),
        datetime.combine(td, time(16, 0), tzinfo=_SH_TZ),
    )


def get_db_password() -> str:
    """获取数据库密码：环境变量优先，回退 fat-finger 仓库外 secrets.toml。"""
    pw = os.environ.get("FAT_FINGER_DB_PASSWORD")
    if pw:
        return pw
    if _FF_SECRETS.exists():
        with _FF_SECRETS.open("rb") as f:
            secrets = tomllib.load(f)
        if "db_password" in secrets:
            return str(secrets["db_password"])
    raise RuntimeError(
        "缺少数据库密码：请设置环境变量 FAT_FINGER_DB_PASSWORD，"
        f"或确保 {_FF_SECRETS} 存在且含 db_password 字段"
    )


def _conn_kwargs() -> dict[str, Any]:
    return {**DB_PARAMS, "password": get_db_password()}


def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """便捷只读查询，返回 list[dict]。只读消费 quant 库，不建任何表。"""
    with psycopg.connect(**_conn_kwargs(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return cur.fetchall()


def stream_rows(
    sql: str, params: dict[str, Any] | None = None, *, chunk_size: int = 20_000
) -> Iterator[dict[str, Any]]:
    """服务端游标流式拉取大结果集，避免一次性占用过多内存。"""
    with psycopg.connect(**_conn_kwargs(), row_factory=dict_row) as conn:
        conn.autocommit = False
        with conn.cursor(name="ff_bt_stream") as cur:
            cur.itersize = chunk_size
            cur.execute(sql, params or {})
            for row in cur:
                yield row
