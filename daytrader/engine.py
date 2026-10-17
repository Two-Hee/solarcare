"""틱/호가 이벤트를 받아 분석 -> 판단 -> 주문까지 수행한다.

백테스트(replay)와 실시간(live)이 이 엔진을 그대로 공유한다. 시각은 항상 틱의 타임스탬프를 쓴다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .broker.base import Broker
from .config import Params
from .costs import tick_size
from .flow import FlowAnalyzer, FlowSnapshot
from .models import DailyContext, Position, Quote, Tick
from .risk import RiskManager
from .strategy import Action, DecisionCtx, FlowStrategy

log = logging.getLogger("daytrader.engine")

DailyProvider = Callable[[str, date], DailyContext]


@dataclass
class CodeState:
    code: str
    analyzer: FlowAnalyzer
    position: Position | None = None
    last_tick: Tick | None = None
    cooldown_until: datetime | None = None
    trades_today: int = 0
    last_eval: datetime | None = None
    daily: DailyContext | None = None
    closebet_date: date | None = None
    last_exit_ts: datetime | None = None
    p: Params | None = None  # 종목별 파라미터(없으면 공통)
    strategy: FlowStrategy | None = None


class TradingEngine:
    def __init__(
        self,
        codes: list[str],
        params: Params,
        broker: Broker,
        daily_provider: DailyProvider | None = None,
        trade_sink: Callable[[dict], None] | None = None,
        state_path: str | Path | None = None,
        kill_file: str | Path | None = "STOP",
        params_by_code: dict[str, Params] | None = None,
    ):
        self.p = params
        self.params_by_code = params_by_code or {}
        pp = lambda c: self.params_by_code.get(c, params)  # noqa: E731
        self.broker = broker
        self.strategy = FlowStrategy(params)
        # 계좌 단위 리스크는 공통 값. 이월 갭 가정은 종목들 중 가장 보수적인 값을 쓴다
        risk_p = replace(
            params,
            overnight_gap_pct=max(pp(c).overnight_gap_pct for c in codes),
            overnight_risk=min(pp(c).overnight_risk for c in codes),
        )
        self.risk = RiskManager(risk_p, kill_file)
        self.states = {
            c: CodeState(c, FlowAnalyzer(c, pp(c)), daily=DailyContext(), p=pp(c), strategy=FlowStrategy(pp(c)))
            for c in codes
        }
        self.daily_provider = daily_provider
        self.trade_sink = trade_sink
        self.state_path = Path(state_path) if state_path else None
        self.trades: list[dict] = []
        self.day: date | None = None
        self.now: datetime | None = None

    # ------------------------------------------------------------------ 이벤트 입력
    def on_quote(self, q: Quote) -> None:
        cs = self.states.get(q.code)
        if cs:
            cs.analyzer.on_quote(q)

    def on_tick(self, t: Tick) -> None:
        cs = self.states.get(t.code)
        if cs is None:
            return
        if self.day != t.ts.date():
            self._roll_day(t.ts)
        cs.analyzer.on_tick(t)
        cs.last_tick = t
        self.now = t.ts
        if cs.position:
            cs.position.peak = max(cs.position.peak, float(t.price))
        self._manage(cs, t.ts)

    def on_clock(self, now: datetime) -> None:
        """체결이 뜸한 종목도 시간 조건(청산·이월)이 돌도록 1초마다 호출."""
        if self.day != now.date():
            self._roll_day(now)
        self.now = now
        for cs in self.states.values():
            if cs.last_tick is not None:
                self._manage(cs, now)

    # ------------------------------------------------------------------ 내부
    def _roll_day(self, ts: datetime) -> None:
        self.day = ts.date()
        for cs in self.states.values():
            cs.trades_today = 0
            cs.cooldown_until = None
            cs.last_eval = None
            if self.daily_provider:
                cs.daily = self.daily_provider(cs.code, self.day)
        self.risk.new_day(self.equity())

    def equity(self) -> float:
        eq = self.broker.cash()
        for cs in self.states.values():
            if cs.position:
                last = cs.analyzer.last_price or cs.position.avg_price
                eq += cs.position.qty * last
        return eq

    def overnight_exposure(self) -> float:
        tot = 0.0
        for cs in self.states.values():
            pos = cs.position
            if pos and (pos.overnight or pos.entry_ts.date() < (self.day or pos.entry_ts.date())):
                tot += pos.qty * (cs.analyzer.last_price or pos.avg_price)
        return tot

    def _manage(self, cs: CodeState, now: datetime) -> None:
        pos = cs.position
        if not pos and cs.last_eval and (now - cs.last_eval).total_seconds() < cs.p.eval_interval_sec:
            return
        cs.last_eval = now
        eq = self.equity()
        if self.risk.check(eq) and pos:
            snap = cs.analyzer.snapshot(now)
            self._sell(cs, now, snap, 1.0, "risk_halt")
            return
        snap = cs.analyzer.snapshot(now)
        ctx = DecisionCtx(
            now=now,
            snap=snap,
            pos=pos,
            daily=cs.daily or DailyContext(),
            trades_today=cs.trades_today,
            cooldown_until=cs.cooldown_until,
            entries_allowed=self.risk.entries_allowed(),
            overnight_room=self.risk.overnight_room(eq, self.overnight_exposure()),
            closebet_done=cs.closebet_date == now.date(),
            last_exit_ts=cs.last_exit_ts,
        )
        act = cs.strategy.decide(ctx)
        if act.kind == "buy":
            self._buy(cs, now, snap, act)
        elif act.kind == "sell" and pos:
            self._sell(cs, now, snap, act.frac, act.reason, act.overnight)
        elif act.kind == "mark_overnight" and pos:
            pos.overnight = True
            self.save_state()

    def _quote_prices(self, cs: CodeState, snap: FlowSnapshot) -> tuple[int, int, int]:
        lt = cs.last_tick
        last = snap.price or (lt.price if lt else 0)
        ask1 = snap.ask1 or (lt.ask1 if lt else 0)
        bid1 = snap.bid1 or (lt.bid1 if lt else 0)
        return last, ask1, bid1

    def _buy(self, cs: CodeState, now: datetime, snap: FlowSnapshot, act: Action) -> None:
        last, ask1, bid1 = self._quote_prices(cs, snap)
        ref = (ask1 or last) + cs.p.slip_ticks * tick_size(ask1 or last)
        budget = min(self.equity() * act.frac, self.broker.cash() * 0.995)
        qty = int(budget // ref) if ref > 0 else 0
        if qty <= 0:
            return
        fill = self.broker.buy(cs.code, qty, now, last, ask1, bid1)
        if fill is None:
            log.warning("매수 체결 실패 %s qty=%d", cs.code, qty)
            return
        pos = cs.position
        if pos is not None:  # 분할 매수(추가 차수): 평균단가·수수료를 합산한다
            total = pos.qty + fill.qty
            pos.avg_price = (pos.avg_price * pos.qty + fill.price * fill.qty) / total
            pos.entry_fee_ps = (pos.entry_fee_ps * pos.qty + fill.fee) / total
            pos.qty = total
            pos.tranches += 1
            pos.last_add_ts = now
            pos.last_tranche_px = fill.price
            pos.peak = max(pos.peak, fill.price)
            log.info("ADD  %s +%d @ %.0f (%s) -> %d주 평단 %.0f [%d차]", cs.code, fill.qty, fill.price, act.reason, pos.qty, pos.avg_price, pos.tranches)
            self.save_state()
            return
        cs.position = Position(
            code=cs.code,
            qty=fill.qty,
            avg_price=fill.price,
            entry_ts=now,
            entry_fee_ps=fill.fee / fill.qty,
            overnight=act.overnight,
            peak=fill.price,
            features={**snap.as_features(), "entry_reason": act.reason},
            tp_pct=act.tp_pct,
            sl_pct=act.sl_pct,
            mode=act.mode,
            full_frac=act.full_frac,
            invalid_px=act.invalid_px,
            last_add_ts=now,
            last_tranche_px=fill.price,
        )
        cs.trades_today += 1
        if act.overnight:
            cs.closebet_date = now.date()
        log.info("BUY  %s %d @ %.0f (%s) score60=%.0f score10=%.0f", cs.code, fill.qty, fill.price, act.reason, snap.score60, snap.score10)
        self.save_state()

    def _sell(
        self, cs: CodeState, now: datetime, snap: FlowSnapshot, frac: float, reason: str, mark_overnight: bool = False
    ) -> None:
        pos = cs.position
        if pos is None:
            return
        qty = pos.qty if frac >= 0.999 else max(1, int(pos.qty * frac))
        if qty >= pos.qty:
            qty = pos.qty
        last, ask1, bid1 = self._quote_prices(cs, snap)
        fill = self.broker.sell(cs.code, qty, now, last, ask1, bid1)
        if fill is None:
            log.warning("매도 체결 실패 %s qty=%d", cs.code, qty)
            return
        gross = (fill.price - pos.avg_price) * qty
        fees = pos.entry_fee_ps * qty + fill.fee + fill.tax
        net = gross - fees
        rec = {
            "code": cs.code,
            "entry_ts": pos.entry_ts.isoformat(),
            "exit_ts": now.isoformat(),
            "qty": qty,
            "entry_price": round(pos.avg_price, 2),
            "exit_price": round(fill.price, 2),
            "gross": round(gross, 1),
            "fees": round(fees, 1),
            "net": round(net, 1),
            "net_pct": round(net / (pos.avg_price * qty), 5),
            "reason": reason,
            "hold_sec": int((now - pos.entry_ts).total_seconds()),
            "carried": pos.entry_ts.date() < now.date(),
            "sl_pct": pos.sl_pct,
            "mode": pos.mode,
            "tranches": pos.tranches,
            **{f"f_{k}": v for k, v in pos.features.items()},
        }
        self.trades.append(rec)
        if self.trade_sink:
            self.trade_sink(rec)
        log.info("SELL %s %d @ %.0f (%s) net=%.0f (%.2f%%)", cs.code, qty, fill.price, reason, net, rec["net_pct"] * 100)
        pos.qty -= qty
        if pos.qty <= 0:
            cs.position = None
            cs.last_exit_ts = now
            cs.cooldown_until = now + timedelta(seconds=cs.p.cooldown_sec)
        elif mark_overnight:
            pos.overnight = True
        self.save_state()

    def flatten_all(self, reason: str = "manual_flatten") -> None:
        """남은 포지션을 전부 정리(종료/비상용)."""
        for cs in self.states.values():
            if cs.position and self.now:
                self._sell(cs, self.now, cs.analyzer.snapshot(self.now), 1.0, reason)

    # ------------------------------------------------------------------ 상태 저장(재시작/이월 보존)
    def save_state(self) -> None:
        if not self.state_path:
            return
        data = {
            "cash": self.broker.cash(),
            "positions": [
                {
                    "code": cs.code,
                    "qty": cs.position.qty,
                    "avg_price": cs.position.avg_price,
                    "entry_ts": cs.position.entry_ts.isoformat(),
                    "entry_fee_ps": cs.position.entry_fee_ps,
                    "overnight": cs.position.overnight,
                    "peak": cs.position.peak,
                    "tp_pct": cs.position.tp_pct,
                    "sl_pct": cs.position.sl_pct,
                    "mode": cs.position.mode,
                    "full_frac": cs.position.full_frac,
                    "tranches": cs.position.tranches,
                    "invalid_px": cs.position.invalid_px,
                    "last_add_ts": cs.position.last_add_ts.isoformat() if cs.position.last_add_ts else None,
                    "last_tranche_px": cs.position.last_tranche_px,
                    "features": cs.position.features,
                }
                for cs in self.states.values()
                if cs.position
            ],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_state(self) -> dict:
        if not self.state_path or not self.state_path.exists():
            return {}
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        for d in data.get("positions", []):
            cs = self.states.get(d["code"])
            if cs:
                cs.position = Position(
                    code=d["code"],
                    qty=d["qty"],
                    avg_price=d["avg_price"],
                    entry_ts=datetime.fromisoformat(d["entry_ts"]),
                    entry_fee_ps=d.get("entry_fee_ps", 0.0),
                    overnight=d.get("overnight", False),
                    peak=d.get("peak", d["avg_price"]),
                    tp_pct=d.get("tp_pct", 0.0),
                    sl_pct=d.get("sl_pct", 0.0),
                    mode=d.get("mode", "flow"),
                    full_frac=d.get("full_frac", 0.0),
                    tranches=d.get("tranches", 1),
                    invalid_px=d.get("invalid_px", 0.0),
                    last_add_ts=datetime.fromisoformat(d["last_add_ts"]) if d.get("last_add_ts") else None,
                    last_tranche_px=d.get("last_tranche_px", 0.0),
                    features=d.get("features", {}),
                )
        return data
