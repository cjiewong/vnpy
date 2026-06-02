"""乌龙指策略状态枚举。独立模块便于策略与单元测试共用。"""
from enum import IntEnum


class FFState(IntEnum):
    IDLE = 0              # 等开盘锚点 / session 切换
    QUOTING = 1          # 已挂远端买单等成交
    FILLED_MANAGING = 2  # 已成交，进入平仓观察窗口
    COOLDOWN = 3         # 成交后冷却，不重挂
    STOPPED_TODAY = 4    # 当日涨跌幅>2% 或单日亏损达闸，当天停挂
