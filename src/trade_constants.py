"""交易系统常量定义。"""


class CloseReason:
    """仓位关闭原因枚举。"""
    SYNC = "SYNC"               # 状态同步发现仓位已关闭
    SYNC_DUST = "SYNC_DUST"     # 残仓清理（名义价值 < MIN_NOTIONAL）
    REPLACED = "REPLACED"       # 换仓淘汰（被新信号替换）
    TIMEOUT = "TIMEOUT"          # 超时平仓（持仓超过上限）
    TIMEOUT_LOSS = "TIMEOUT_LOSS"  # 超时亏损平仓（持仓>4h + 浮亏>3%）
    TP_FILLED = "TP_FILLED"      # 止盈成交
    SL_HIT = "SL_HIT"            # 止损触发
    MANUAL_CLOSE = "MANUAL_CLOSE"  # 用户手动平仓
    LIQUIDATED = "LIQUIDATED"    # 爆仓
