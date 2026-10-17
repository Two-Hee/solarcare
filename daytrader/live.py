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

    cache: dict = {}

    def daily_provider(code, d):
        if code not in cache:
            try:
                cache[code] = rest.investor_daily(code)
            except Exception as e:  # 수급 일봉이 안 와도 매매 자체는 가능
                log.warning("투자자 일별 수급 조회 실패(%s): %s", code, e)
                from .models import DailyContext

                cache[code] = DailyContext()
        return cache[code]

    engine = TradingEngine(codes, params, broker, daily_provider, sink, state_path)
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

    log.info("시작: %s broker=%s mode=%s 기록=%s", codes, broker_kind, settings.mode, record)
    try:
        await asyncio.gather(stream.run(on_event, stop), clock())
    finally:
        engine.save_state()
        if rec:
            rec.close()
    return engine
