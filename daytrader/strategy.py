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
    mode: str = "flow"
    full_frac: float = 0.0
    invalid_px: float = 0.0


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
    last_exit_ts: datetime | None = None


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

        if pos.invalid_px and not carry and px <= pos.invalid_px:
            # 진입 근거(저점 형성 / 압축 박스)가 깨졌다 -> 즉시 손절
            return Action("sell", reason="box_break" if pos.mode == "squeeze" else "dip_low_break")

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
        if (p.band_exit and pos.mode == "band" and s.band is not None and s.band.ready
                and s.band.state == "none" and held >= p.min_hold_sec):
            return Action("sell", reason="band_break")  # 진입 근거였던 밭이 사라졌다
        if pos.peak / pos.avg_price - 1 >= p.breakeven_trigger_pct and pnl <= p.breakeven_lock_pct:
            return Action("sell", reason="breakeven_stop")

        if t >= hms(p.flatten_time) and not pos.overnight:
            return Action("sell", reason="eod_flatten")

        if pos.mode == "dip" and not pos.overnight:
            add = self._dip_add(c, t, pnl, sl)
            if add:
                return add

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

        dip_underwater = pos.mode == "dip" and pnl <= 0  # 저점매수는 매도세 속에서 샀으므로 손실 구간 수급반전은 무시(저점 이탈이 대신 지킨다)
        band_pure = pos.mode == "band" and p.entry_mode == "band" and p.band_exit  # 밭 하나를 기준으로 삼은 포지션은 밭 소멸/익절/손절로만 정리
        sq_pure = pos.mode == "squeeze"  # 압축 돌파 포지션은 박스 저점 이탈/익절/본전방어/시간청산으로만 정리(진입 근거가 박스이므로)
        if held >= p.min_hold_sec and s.score60 <= p.exit_score_1m and s.score10 <= 0 and not dip_underwater and not band_pure and not sq_pure:
            return Action("sell", reason="flow_reversal")
        if not pos.overnight and held >= p.max_hold_min * 60:
            return Action("sell", reason="time_stop")
        return HOLD

    def _inv_pass(self, c: DecisionCtx) -> bool:
        inv = c.daily.investor_ok
        return inv is True or (inv is None and not self.p.require_investor_ok)

    def _news_ok_for_overnight(self, c: DecisionCtx) -> bool:
        """이월은 뉴스·재무 게이트가 모두 ok 일 때만."""
        p = self.p
        return (not p.news_gate or c.daily.news == "ok") and (not p.fin_gate or c.daily.fin == "ok")

    # ------------------------------------------------------------------ 진입
    def _entry(self, c: DecisionCtx, t: int) -> Action:
        p, s = self.p, c.snap
        if not c.entries_allowed:
            return HOLD
        if c.cooldown_until and c.now < c.cooldown_until:
            return HOLD
        if p.news_gate and c.daily.news == "block":
            return HOLD
        if p.fin_gate and c.daily.fin == "block":
            return HOLD  # 자본잠식·거래정지·관리/경고/단기과열 종목은 신규 진입 금지
        if p.regime_gate and c.daily.regime not in ("breakout", "expansion", "bandstart"):
            return HOLD  # 국면을 모르거나(unknown) 돌파/확장 구간이 아니면 쉰다
        size_mult = 0.5 if (p.news_gate and c.daily.news == "caution") else 1.0
        st = self.stops(s)
        squeeze = p.entry_mode == "squeeze"
        if st is None and not squeeze:
            return HOLD
        # squeeze 는 '조용한 구간'이 정의이므로 변동성 하한/상한 게이트(stops)를 쓰지 않고 구조적 손절을 쓴다
        tp, sl = st if st is not None else (p.tp_pct, p.sl_pct)

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
                cap = min(p.max_overnight_exposure, p.overnight_risk / max(p.overnight_gap_pct, 1e-6))
                return Action(
                    "buy", frac=min(size(p.overnight_entry_frac, max(sl, p.overnight_sl_pct)), cap),
                    reason="closing_bet", overnight=True, tp_pct=tp, sl_pct=sl,
                )
            return HOLD

        if not (hms(p.entry_start) <= t < hms(p.entry_end)):
            return HOLD
        if c.trades_today >= p.max_trades_per_day:
            return HOLD
        if p.require_investor_ok and c.daily.investor_ok is not True:
            return HOLD
        if s.spread_ticks > p.max_spread_ticks:
            return HOLD
        if squeeze:
            return self._squeeze_entry(c, tp, size)
        flow_ok = self._flow_ok(s)
        band_ok = p.entry_mode != "flow" and self._band_ok(c)
        mode = p.entry_mode
        go = {"flow": flow_ok, "band": band_ok, "either": flow_ok or band_ok, "both": flow_ok and band_ok}.get(mode, flow_ok)
        if go:
            if not self._room_ok(s, tp):
                return HOLD  # 가까운 강한 저항 때문에 익절폭만큼 올라갈 자리가 없다
            if band_ok and mode != "flow":
                return Action("buy", frac=size(p.position_frac, sl), reason="band_start" if not flow_ok else "band_start+flow",
                              tp_pct=tp, sl_pct=sl, mode="band")
            return Action("buy", frac=size(p.position_frac, sl), reason="flow_long", tp_pct=tp, sl_pct=sl)
        # 'band'/'both' 는 밭 시작 하나를 기준으로 삼는 모드라 저점매수 경로를 열지 않는다(기준을 섞지 않기 위해)
        if p.dip_enabled and mode in ("flow", "either") and self._dip_ok(c) and self._room_ok(s, tp):
            sup_ok, sup_px = self._dip_support(s)
            if sup_ok:
                full = size(p.position_frac, sl)
                base = min(s.lo_lb, sup_px) if sup_px else s.lo_lb
                return Action(
                    "buy", frac=full * p.dip_t1, reason="dip_buy", tp_pct=tp, sl_pct=sl, mode="dip",
                    full_frac=full, invalid_px=base * (1 - p.dip_break_buffer),
                )
        return HOLD

    # ------------------------------------------------------------------ 압축 -> 확장 돌파 (entry_mode == squeeze)
    def _squeeze_entry(self, c: DecisionCtx, tp: float, size) -> Action:
        """변동성이 줄어든 박스를 거래량·매수체결과 함께 상향 돌파. 손절은 박스 저점."""
        from .squeeze import stop_price, structural_risk

        p, s = self.p, c.snap
        q = s.sq
        if q is None or not q.ready or not q.compressed or q.box_hi <= 0:
            return HOLD
        if c.last_exit_ts is not None and q.box_start is not None and c.last_exit_ts >= q.box_start:
            return HOLD  # 이 박스(또는 그 일부)에서 이미 거래했다: 새 압축이 만들어질 때까지 기다린다
        px = s.price
        if not (q.box_hi * (1 + p.sq_break_pct) < px <= q.box_hi * (1 + p.sq_max_ext)):
            return HOLD  # 아직 돌파 전이거나 이미 많이 올라 늦음
        if q.vol_med > 0 and s.s60.vol < p.sq_vol_mult * q.vol_med:
            return HOLD  # 거래량 확장이 없는 돌파는 믿지 않는다
        if s.s60.delta_ratio < p.sq_min_delta or s.score10 < 0:
            return HOLD  # 매수체결 우위가 없다
        if s.s60.n < p.min_trades_1m:
            return HOLD
        if p.use_vwap_filter and s.vwap > 0 and px < s.vwap * (1 - p.vwap_tol):
            return HOLD
        stop = stop_price(q.box_hi, q.box_lo, p.sq_stop_frac, p.sq_stop_buffer)
        risk = structural_risk(px, stop)
        if risk < p.sq_min_risk or risk > p.sl_max_pct or tp / risk < p.sq_min_rr:
            return HOLD  # 박스가 너무 좁거나 넓어서 손익비가 안 맞는다
        if not self._room_ok(s, tp):
            return HOLD
        return Action(
            # 판단 기준은 구조적 손절가(invalid_px). 비율 손절은 체결 슬리피지/갭을 위한 보조 장치라 여유를 둔다
            "buy", frac=size(p.position_frac, risk), reason="squeeze_break", tp_pct=tp, sl_pct=risk * 1.25 + 0.001, mode="squeeze",
            invalid_px=stop,
        )

    # ------------------------------------------------------------------ 이평선 밭 시작 (entry_mode != flow)
    def _band_ok(self, c: DecisionCtx) -> bool:
        """밭이 막 확정된 구간(fresh)이고, 아직 slow 이평 근처이며, 이 밭에서 이미 한 번 거래하지 않았을 때."""
        p, s = self.p, c.snap
        b = s.band
        if b is None or not b.ready or b.state != "active" or not b.fresh or b.slow <= 0:
            return False
        if s.price < b.slow or s.price / b.slow - 1 > p.band_max_ext:
            return False  # slow 아래이거나 이미 많이 올라 늦음
        if c.last_exit_ts is not None and b.start_ts is not None and c.last_exit_ts >= b.start_ts:
            return False  # 이 밭에서는 이미 진입/청산했다
        return s.s60.n >= p.min_trades_1m  # 유동성

    # ------------------------------------------------------------------ 수급 기반 동적 지지·저항 (use_levels)
    def _room_ok(self, s: FlowSnapshot, tp: float) -> bool:
        p = self.p
        if not p.use_levels or s.lv is None:
            return True
        r = s.lv.nearest_resistance(p.lv_min_strength)
        return r is None or (r.price / s.price - 1) >= p.lv_room_frac * tp

    def _dip_support(self, s: FlowSnapshot) -> tuple[bool, float]:
        """저점이 수급 지지 노드 근처인가. 꺼져 있으면 항상 통과."""
        p = self.p
        if not p.use_levels or s.lv is None:
            return True, 0.0
        near = [x for x in s.lv.supports if x.strength >= p.lv_min_strength and abs(x.price / s.lo_lb - 1) <= 2 * p.lv_touch_pct]
        if not near:
            return False, 0.0
        best = max(near, key=lambda x: x.strength)
        return True, best.price

    def _flow_ok(self, s: FlowSnapshot) -> bool:
        p = self.p
        if s.score60 < p.entry_score_1m or s.score10 < p.entry_score_10s:
            return False
        if s.s60.n < p.min_trades_1m:
            return False
        if p.use_vwap_filter and s.vwap > 0 and s.price < s.vwap * (1 - p.vwap_tol):
            return False
        return s.s300.ret <= p.max_chase_pct

    # ------------------------------------------------------------------ 과대낙폭 후 저점 형성 -> 분할 매수
    def _dip_ok(self, c: DecisionCtx) -> bool:
        """고점 대비 크게 빠진 뒤, 매도세가 잦아들고 저점에서 반등이 확인될 때만(떨어지는 칼날은 잡지 않는다)."""
        p, s = self.p, c.snap
        if s.hi_lb <= 0 or s.lo_lb <= 0 or s.lo_ts is None:
            return False
        drop = 1 - s.lo_lb / s.hi_lb
        if drop < max(p.dip_min_drop_pct, p.dip_atr_mult * s.atr1m):
            return False
        bounce = s.price / s.lo_lb - 1
        if not (p.dip_bounce_pct <= bounce <= p.dip_max_bounce_pct):
            return False
        if (c.now - s.lo_ts).total_seconds() < p.dip_settle_sec:
            return False
        return (
            s.s10.n >= 3
            and s.s10.delta_ratio >= 0
            and s.score10 >= p.dip_score_10s
            and s.s60.delta_ratio >= p.dip_min_delta60
        )

    def _dip_add(self, c: DecisionCtx, t: int, pnl: float, sl: float) -> Action | None:
        p, s, pos = self.p, c.snap, c.pos
        assert pos is not None
        if pos.tranches >= p.dip_max_tranches or not c.entries_allowed:
            return None
        if p.news_gate and c.daily.news != "ok":
            return None
        if t >= hms(p.entry_end) or pos.last_add_ts is None:
            return None
        if (c.now - pos.last_add_ts).total_seconds() < p.dip_add_gap_sec:
            return None
        if pnl <= -0.6 * sl or s.price <= pos.invalid_px * 1.002:  # 손절·저점 이탈선 코앞이면 더 사지 않는다
            return None
        w = p.dip_t2 if pos.tranches == 1 else max(0.0, 1.0 - p.dip_t1 - p.dip_t2)
        frac = pos.full_frac * w
        if frac <= 0:
            return None
        confirm = s.hl2 > s.lo_lb > 0 and s.score60 >= p.dip_confirm_score and s.score10 >= 0
        if confirm:
            return Action("buy", frac=frac, reason="dip_add_confirm")
        deeper = (
            p.dip_allow_deeper_add
            and s.price <= pos.last_tranche_px * (1 - p.dip_add_step_pct)
            and s.s10.n >= 3
            and s.s10.delta_ratio >= 0
            and s.s60.delta_ratio >= p.dip_min_delta60
        )
        if deeper:
            return Action("buy", frac=frac, reason="dip_add_deeper")
        return None
