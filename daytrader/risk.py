from __future__ import annotations

from pathlib import Path

from .config import Params


class RiskManager:
    """일일 손실 한도와 킬스위치. 전략/AI 가 우회할 수 없는 하드 리밋."""

    def __init__(self, params: Params, kill_file: str | Path | None = "STOP"):
        self.p = params
        self.kill_file = Path(kill_file) if kill_file else None
        self.day_start_equity = 0.0
        self.halted = False

    def new_day(self, equity: float) -> None:
        self.day_start_equity = equity
        self.halted = False

    def kill_switch(self) -> bool:
        return bool(self.kill_file and self.kill_file.exists())

    def check(self, equity: float) -> bool:
        """한도 초과면 halted 를 켜고 True 반환."""
        if self.kill_switch():
            self.halted = True
        elif self.day_start_equity > 0:
            if equity / self.day_start_equity - 1 <= -self.p.daily_loss_limit:
                self.halted = True
        return self.halted

    def entries_allowed(self) -> bool:
        return not self.halted and not self.kill_switch()

    def overnight_cap(self) -> float:
        """이월 총 노출 상한(자본 대비). 가정한 갭이 났을 때 손실이 overnight_risk 를 넘지 않게 한다."""
        gap = max(self.p.overnight_gap_pct, 1e-6)
        return min(self.p.max_overnight_exposure, self.p.overnight_risk / gap)

    def overnight_room(self, equity: float, overnight_exposure: float) -> bool:
        return equity > 0 and overnight_exposure / equity < self.overnight_cap()
