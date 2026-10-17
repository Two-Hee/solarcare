from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """모든 시각은 타임존 없는(naive) KST 로 통일한다."""
    return datetime.now(KST).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class Tick:
    """체결 1건. side: +1 매수체결(매도호가 소진), -1 매도체결, 0 미상."""

    ts: datetime
    code: str
    price: int
    vol: int
    side: int = 0
    ask1: int = 0
    bid1: int = 0


@dataclass(frozen=True, slots=True)
class Quote:
    """호가 스냅샷(1호가 + 10호가 총잔량)."""

    ts: datetime
    code: str
    ask1: int
    bid1: int
    ask_rem1: int
    bid_rem1: int
    total_ask: int
    total_bid: int


@dataclass(frozen=True, slots=True)
class Fill:
    code: str
    side: str  # "buy" | "sell"
    qty: int
    price: float
    fee: float
    tax: float
    ts: datetime


@dataclass
class Position:
    code: str
    qty: int
    avg_price: float
    entry_ts: datetime
    entry_fee_ps: float = 0.0  # 주당 매수 수수료
    overnight: bool = False  # 1박2일 보유 확정 여부
    peak: float = 0.0
    features: dict = field(default_factory=dict)
    tp_pct: float = 0.0  # 진입 시점 변동성으로 정한 익절/손절 (0 이면 params 기본값)
    sl_pct: float = 0.0


@dataclass
class DailyContext:
    """전일까지의 외국인/기관 순매수(주 수량). 1박2일 판단의 보조 근거."""

    foreign_net_1d: int | None = None
    inst_net_1d: int | None = None
    foreign_net_5d: int | None = None
    inst_net_5d: int | None = None
    news: str = "ok"  # ok | caution | block  (뉴스 게이트)
    news_note: str = ""

    @property
    def investor_ok(self) -> bool | None:
        vals = (self.foreign_net_1d, self.inst_net_1d, self.foreign_net_5d, self.inst_net_5d)
        if all(v is None for v in vals):
            return None
        one = (self.foreign_net_1d or 0) + (self.inst_net_1d or 0)
        five = (self.foreign_net_5d or 0) + (self.inst_net_5d or 0)
        return one > 0 and five > 0
