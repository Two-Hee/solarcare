from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..models import Fill


class Broker(Protocol):
    def cash(self) -> float: ...

    def buy(self, code: str, qty: int, ts: datetime, last: int, ask1: int, bid1: int) -> Fill | None: ...

    def sell(self, code: str, qty: int, ts: datetime, last: int, ask1: int, bid1: int) -> Fill | None: ...
