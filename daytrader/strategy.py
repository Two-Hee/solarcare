"""수급 점수 기반 의사결정. 순수 함수형: 입력(DecisionCtx) -> Action.

당일 단타:  1분 점수 + 10초 점수가 동시에 강할 때 매수, +1.3% 익절 / 변동성 연동 손절 / 수급 반전 / 시간 청산.
            손절폭 = max(sl_pct, sl_atr_mult x 1분봉 평균변동폭), 포지션 크기 = risk_per_trade / 손절폭
            -> 출렁이는 종목은 손절을 넓히되 수량을 줄여 '한 번 틀렸을 때 잃는 금액'을 고정한다.
1박2일:     (a) 이익 중인 포지션을 마감 직전 수급이 살아있으면 일부 이월,
            (b) 종가배팅: 마감 직전 수급이 강하면 소량 신규 이월.
            이월 포지션은 이튿날 TP/SL/수급반전 또는 overnight_exit_time 에 정리.
뉴스 게이트: block 이면 신규 진입 금지 + 보유분 청산, caution 이면 이월 금지 + 진입 수량 절반.
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
    tp_pct: float = 0.0
    sl_pct: float = 0.0


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

    # ------------------------------------------------------------------ 변동성 연동 손익폭
    def stops(self, s: FlowSnapshot) -> tuple[float, float] | None:
        """(tp, sl). 변동성 정보가 없거나 너무 출렁이면 None(진입 금지)."""
        p = self.p
        if not p.vol_adaptive:
            return p.tp_pct, p.sl_pct
        if s.atr1m <= 0 or s.atr1m < p.min_atr1m:
            return None
        sl = max(p.sl_pct, p.sl_atr_mult * s.atr1m)
        if sl > p.sl_max_pct:
            return None
        return max(p.tp_pct, p.tp_atr_mult * s.atr1m), sl

    # ------------------------------------------------------------------ 보유 중
    def _manage(self, c: DecisionCtx, t: int) -> Action:
        p, s, pos = self.p, c.snap, c.pos
        assert pos is not None
        px = s.price
        pnl = px / pos.avg_price - 1
        held = (c.now - pos.entry_ts).total_seconds()
        carry = pos.entry_ts.date() < c.now.date()
        tp = pos.tp_pct or p.tp_pct
        sl = pos.sl_pct or p.sl_pct

        if p.news_gate and c.daily.news == "block":
            return Action("sell", reason="news_block")

        if carry:  # 이튿날(1박2일 후반)
            if pnl <= -max(p.overnight_sl_pct, sl):
                return Action("sell", reason="overnight_stop")
            if pnl >= max(p.overnight_tp_pct, tp):
                return Action("sell", reason="overnight_tp")
            if t >= hms(p.overnight_exit_time):
                return Action("sell", reason="overnight_time_exit")
            if s.score60 <= p.exit_score_1m and s.score10 <= 0:
                return Action("sell", reason="overnight_flow_exit")
            return HOLD

        if pnl <= -sl:
            return Action("sell", reason="stop_loss")
        if pnl >= tp:
            return Action("sell", reason="take_profit")
        if pos.peak / pos.avg_price - 1 >= p.breakeven_trigger_pct and pnl <= p.breakeven_lock_pct:
            return Action("sell", reason="breakeven_stop")

        if t >= hms(p.flatten_time) and not pos.overnight:
            return Action("sell", reason="eod_flatten")

        if not pos.overnight and p.overnight_enabled and hms(p.overnight_decide_time) <= t < hms(p.flatten_time):
            if (
                c.overnight_room
                and self._inv_pass(c)
                and self._news_ok_for_overnight(c)
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

    def _inv_pass(self, c: DecisionCtx) -> bool:
        inv = c.daily.investor_ok
        return inv is True or (inv is None and not self.p.require_investor_ok)

    def _news_ok_for_overnight(self, c: DecisionCtx) -> bool:
        return not self.p.news_gate or c.daily.news == "ok"

    # ------------------------------------------------------------------ 진입
    def _entry(self, c: DecisionCtx, t: int) -> Action:
        p, s = self.p, c.snap
        if not c.entries_allowed:
            return HOLD
        if c.cooldown_until and c.now < c.cooldown_until:
            return HOLD
        if p.news_gate and c.daily.news == "block":
            return HOLD
        size_mult = 0.5 if (p.news_gate and c.daily.news == "caution") else 1.0
        st = self.stops(s)
        if st is None:
            return HOLD
        tp, sl = st

        def size(base_frac: float, stop: float) -> float:
            f = base_frac
            if p.vol_adaptive and stop > 0:
                f = min(f, p.risk_per_trade / stop)
            return f * size_mult

        # 종가배팅(신규 이월)
        if (
            p.overnight_enabled
            and p.overnight_new_entry
            and not c.closebet_done
            and c.overnight_room
            and hms(p.overnight_decide_time) <= t < hms(p.flatten_time)
        ):
            if (
                self._inv_pass(c)
                and self._news_ok_for_overnight(c)
                and s.score60 >= p.overnight_entry_score
                and s.score10 >= 0
                and s.cvd > 0
                and s.price >= s.vwap
                and s.spread_ticks <= p.max_spread_ticks
            ):
                return Action(
                    "buy", frac=size(p.overnight_entry_frac, max(sl, p.overnight_sl_pct)),
                    reason="closing_bet", overnight=True, tp_pct=tp, sl_pct=sl,
                )
            return HOLD

        if not (hms(p.entry_start) <= t < hms(p.entry_end)):
            return HOLD
        if c.trades_today >= p.max_trades_per_day:
            return HOLD
        if p.require_investor_ok and c.daily.investor_ok is not True:
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
        return Action("buy", frac=size(p.position_frac, sl), reason="flow_long", tp_pct=tp, sl_pct=sl)
