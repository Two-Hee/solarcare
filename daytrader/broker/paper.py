"""로컬 모의 체결. 백테스트와 '실시간 시세 + 가상 체결' 모드에서 쓴다.

매수는 매도1호가 + slip_ticks, 매도는 매수1호가 - slip_ticks 에 체결된다고 가정(시장가 보수적 가정).
"""

from __future__ import annotations

from datetime import datetime

from ..costs import CostModel, tick_size
from ..models import Fill


class PaperBroker:
    def __init__(self, cash: float, cost: CostModel | None = None):
        self._cash = float(cash)
        self.cost = cost or CostModel()
        self.holdings: dict[str, int] = {}

    def cash(self) -> float:
        return self._cash

    def buy(self, code: str, qty: int, ts: datetime, last: int, ask1: int, bid1: int) -> Fill | None:
        base = ask1 or last
        price = base + self.cost.slip_ticks * tick_size(base)
        notional = price * qty
        fee = self.cost.fee(notional)
        if qty <= 0 or notional + fee > self._cash:
            return None
        self._cash -= notional + fee
        self.holdings[code] = self.holdings.get(code, 0) + qty
        return Fill(code, "buy", qty, float(price), fee, 0.0, ts)

    def sell(self, code: str, qty: int, ts: datetime, last: int, ask1: int, bid1: int) -> Fill | None:
        if qty <= 0 or self.holdings.get(code, 0) < qty:
            return None
        base = bid1 or last
        price = max(1, base - self.cost.slip_ticks * tick_size(base))
        notional = price * qty
        fee, tax = self.cost.fee(notional), self.cost.tax(notional)
        self._cash += notional - fee - tax
        self.holdings[code] -= qty
        if self.holdings[code] == 0:
            del self.holdings[code]
        return Fill(code, "sell", qty, float(price), fee, tax, ts)
