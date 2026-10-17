"""수급 점수 기반 의사결정. 순수 함수형: 입력(DecisionCtx) -> Action.

당일 단타:  1분 점수 + 10초 점수가 동시에 강할 때 매수, +1.3% 익절 / -0.8% 손절 / 수급 반전 / 시간 청산.
1박2일:     (a) 이익 중인 포지션을 마감 직전 수급이 살아있으면 일부 이월,
            (b) 종가배팅: 마감 직전 수급이 강하면 소량 신규 이월.
            이월 포지션은 이튿날 TP/SL/수급반전 또는 overnight_exit_time 에 정리.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import Params, hms, sod
from .flow import FlowSnapshot
from .models import DailyContext, Position


@dataclass
class Action:
    kind: str  # buy | sell | mark_overnight | hold
    frac: float = 1.0  # buy: 자본 대비 비중 / sell: 보유 대비 비중
    reason: str = ""
    overnight: bool = False


HOLD = Action("hold")


@dataclass
class DecisionCtx:
    now: datetime
    snap: FlowSnapshot
    pos: Position | None
    daily: DailyContext
    trades_today: int
    cooldown_until: datetime | None
    entries_allowed: bool
    overnight_room: bool
    closebet_done: bool


class FlowStrategy:
    def __init__(self, params: Params):
        self.p = params

    def decide(self, c: DecisionCtx) -> Action:
        if c.snap.price <= 0:
            return HOLD
        t = sod(c.now)
        return self._manage(c, t) if c.pos else self._entry(c, t)

    # ------------------------------------------------------------------ 보유 중
    def _manage(self, c: DecisionCtx, t: int) -> Action:
        p, s, pos = self.p, c.snap, c.pos
        assert pos is not None
        px = s.price
        pnl = px / pos.avg_price - 1
        held = (c.now - pos.entry_ts).total_seconds()
        carry = pos.entry_ts.date() < c.now.date()

        if carry:  # 이튿날(1박2일 후반)
            if pnl <= -p.overnight_sl_pct:
                return Action("sell", reason="overnight_stop")
            if pnl >= p.overnight_tp_pct:
                return Action("sell", reason="overnight_tp")
            if t >= hms(p.overnight_exit_time):
                return Action("sell", reason="overnight_time_exit")
            if s.score60 <= p.exit_score_1m and s.score10 <= 0:
                return Action("sell", reason="overnight_flow_exit")
            return HOLD

        if pnl <= -p.sl_pct:
            return Action("sell", reason="stop_loss")
        if pnl >= p.tp_pct:
            return Action("sell", reason="take_profit")
        if pos.peak / pos.avg_price - 1 >= p.breakeven_trigger_pct and pnl <= p.breakeven_lock_pct:
            return Action("sell", reason="breakeven_stop")

        if t >= hms(p.flatten_time) and not pos.overnight:
            return Action("sell", reason="eod_flatten")

        if not pos.overnight and p.overnight_enabled and hms(p.overnight_decide_time) <= t < hms(p.flatten_time):
            inv = c.daily.investor_ok
            inv_pass = inv is True or (inv is None and not p.require_investor_ok)
            if (
                c.overnight_room
                and inv_pass
                and pnl >= p.overnight_min_profit
                and s.score60 >= p.overnight_min_score
            ):
                keep = p.overnight_keep_frac
                if keep >= 1.0:
                    return Action("mark_overnight", reason="overnight_hold")
                return Action("sell", frac=1.0 - keep, reason="overnight_trim", overnight=True)

        if held >= p.min_hold_sec and s.score60 <= p.exit_score_1m and s.score10 <= 0:
            return Action("sell", reason="flow_reversal")
        if not pos.overnight and held >= p.max_hold_min * 60:
            return Action("sell", reason="time_stop")
        return HOLD

    # ------------------------------------------------------------------ 진입
    def _entry(self, c: DecisionCtx, t: int) -> Action:
        p, s = self.p, c.snap
        if not c.entries_allowed:
            return HOLD
        if c.cooldown_until and c.now < c.cooldown_until:
            return HOLD
        inv = c.daily.investor_ok

        # 종가배팅(신규 이월)
        if (
            p.overnight_enabled
            and p.overnight_new_entry
            and not c.closebet_done
            and c.overnight_room
            and hms(p.overnight_decide_time) <= t < hms(p.flatten_time)
        ):
            inv_pass = inv is True or (inv is None and not p.require_investor_ok)
            if (
                inv_pass
                and s.score60 >= p.overnight_entry_score
                and s.score10 >= 0
                and s.cvd > 0
                and s.price >= s.vwap
                and s.spread_ticks <= p.max_spread_ticks
            ):
                return Action("buy", frac=p.overnight_entry_frac, reason="closing_bet", overnight=True)
            return HOLD

        if not (hms(p.entry_start) <= t < hms(p.entry_end)):
            return HOLD
        if c.trades_today >= p.max_trades_per_day:
            return HOLD
        if p.require_investor_ok and inv is not True:
            return HOLD
        if s.score60 < p.entry_score_1m or s.score10 < p.entry_score_10s:
            return HOLD
        if s.s60.n < p.min_trades_1m:
            return HOLD
        if p.use_vwap_filter and s.vwap > 0 and s.price < s.vwap * (1 - p.vwap_tol):
            return HOLD
        if s.spread_ticks > p.max_spread_ticks:
            return HOLD
        if s.s300.ret > p.max_chase_pct:
            return HOLD
        return Action("buy", frac=p.position_frac, reason="flow_long")
