"""실시간 실행: 웹소켓 시세 -> 엔진. 브로커는 paper(가상체결) 또는 kis-mock(KIS 모의계좌 주문).

종료 시각(기본 15:31)에 상태를 저장하고 끝난다. 이월 포지션은 state/ 에 남아 다음 날 이어진다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .broker.paper import PaperBroker
from .config import Params, Settings, hms, sod
from .costs import CostModel
from .engine import TradingEngine
from .kis import KisBroker, KisRest
from .kis_ws import KisStream
from .models import Quote, Tick, now_kst
from .recorder import Recorder

log = logging.getLogger("daytrader.live")


async def run_live(
    codes: list[str],
    settings: Settings,
    params: Params,
    broker_kind: str = "paper",
    paper_cash: float = 10_000_000,
    record: bool = True,
    until: str = "15:31",
    params_by_code: dict[str, Params] | None = None,
) -> TradingEngine:
    rest = KisRest(settings)
    cost = CostModel(params.fee_rate, params.sell_tax, params.slip_ticks)
    state_path = Path("state") / f"{broker_kind}_{'_'.join(codes)}.json"

    # 브로커 & 엔진
    if broker_kind == "kis-mock":
        if settings.mode != "mock":
            raise SystemExit("kis-mock 브로커는 KIS_MODE=mock 에서만 사용합니다.")
        broker = KisBroker(rest, cost)
    else:
        saved = json.loads(state_path.read_text()) if state_path.exists() else {}
        broker = PaperBroker(saved.get("cash", paper_cash), cost)

    trade_log = Path("logs") / "trades.jsonl"
    trade_log.parent.mkdir(exist_ok=True)

    def sink(rec: dict) -> None:
        with trade_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    from dataclasses import replace

    from .models import DailyContext
    from .news import assess

    cache: dict = {}

    def news_fields(code):
        if not params.news_gate:
            return {}
        try:
            g = assess(code, rest, settings.data_dir)
            return {"news": g.level, "news_note": (g.note + " " + g.summary).strip()}
        except Exception as e:  # 뉴스 조회가 실패해도 매매 자체는 가능(게이트는 ok 로 둔다)
            log.warning("뉴스 게이트 조회 실패(%s): %s", code, e)
            return {}

    def fin_fields(code):
        if not params.fin_gate:
            return {}
        try:
            from .fin import assess as fin_assess

            g, _rows, _flags = fin_assess(code, params, rest, settings.data_dir)
            return {"fin": g.level, "fin_note": g.note}
        except Exception as e:  # 재무/상태 조회 실패는 매매를 막지 않는다(ok 로 둔다)
            log.warning("재무·종목상태 조회 실패(%s): %s", code, e)
            return {}

    def daily_provider(code, d):
        if code not in cache:
            try:
                ctx = rest.investor_daily(code)
            except Exception as e:  # 수급 일봉이 안 와도 매매 자체는 가능
                log.warning("투자자 일별 수급 조회 실패(%s): %s", code, e)
                ctx = DailyContext()
            reg = {}
            if params.regime_gate:
                try:
                    from .regime import detect_with

                    r = detect_with(rest.daily_bars(code), params)
                    reg = {"regime": r.state, "regime_note": r.note}
                except Exception as e:  # 일봉 조회 실패 -> unknown (게이트가 켜져 있으면 쉰다)
                    log.warning("일봉 국면 조회 실패(%s): %s", code, e)
            cache[code] = replace(ctx, **news_fields(code), **fin_fields(code), **reg)
            log.info("게이트 %s | 뉴스: %s %s | 재무/상태: %s %s | 국면: %s %s", code, cache[code].news, cache[code].news_note,
                     cache[code].fin, cache[code].fin_note, cache[code].regime, cache[code].regime_note)
        return cache[code]

    engine = TradingEngine(codes, params, broker, daily_provider, sink, state_path, params_by_code=params_by_code)
    saved = engine.load_state()
    if isinstance(broker, PaperBroker):
        for p in saved.get("positions", []):
            broker.holdings[p["code"]] = p["qty"]
    elif saved.get("positions"):
        _, hold = rest.balance()
        for cs in engine.states.values():
            if cs.position:
                q, avg = hold.get(cs.code, (0, 0.0))
                if q <= 0:
                    cs.position = None
                else:
                    cs.position.qty = q
    rec = Recorder(settings.data_dir) if record else None

    stop = asyncio.Event()
    stream = KisStream(settings, rest.approval_key(), codes)

    def on_event(ev) -> None:
        if rec:
            (rec.tick if isinstance(ev, Tick) else rec.quote)(ev)
        (engine.on_tick if isinstance(ev, Tick) else engine.on_quote)(ev)

    async def clock() -> None:
        end = hms(until)
        while not stop.is_set():
            await asyncio.sleep(1)
            now = now_kst()
            if sod(now) >= end:
                log.info("종료 시각 도달. 상태 저장 후 종료합니다.")
                stop.set()
                break
            if hms("09:00") <= sod(now):
                engine.on_clock(now)

    async def news_refresh() -> None:
        """장중 10분마다 뉴스 게이트를 다시 평가한다(블로킹 호출은 스레드로)."""
        while not stop.is_set():
            await asyncio.sleep(600)
            for cs in engine.states.values():
                f = await asyncio.to_thread(news_fields, cs.code)
                g = await asyncio.to_thread(fin_fields, cs.code)  # 종목 상태 플래그(단기과열 등)는 장중에 바뀔 수 있다
                if cs.daily is not None:
                    if f and f["news"] != cs.daily.news:
                        log.warning("뉴스 게이트 변경 %s: %s -> %s (%s)", cs.code, cs.daily.news, f["news"], f["news_note"])
                    if g and g["fin"] != cs.daily.fin:
                        log.warning("재무/상태 게이트 변경 %s: %s -> %s (%s)", cs.code, cs.daily.fin, g["fin"], g["fin_note"])
                    cs.daily = replace(cs.daily, **f, **g)

    log.info("시작: %s broker=%s mode=%s 기록=%s", codes, broker_kind, settings.mode, record)
    try:
        await asyncio.gather(stream.run(on_event, stop), clock(), news_refresh())
    finally:
        engine.save_state()
        if rec:
            rec.close()
    return engine
