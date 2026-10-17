"""기록된 틱을 엔진에 그대로 흘려보내는 백테스트. 실시간과 같은 코드 경로를 쓴다."""

from __future__ import annotations

import csv
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .broker.paper import PaperBroker
from .config import Params
from .costs import CostModel
from .engine import TradingEngine
from .models import DailyContext, Quote, Tick
from .recorder import list_days, load_day


def load_daily_context(path: str | Path) -> dict[str, dict[str, tuple[int, int]]]:
    """data/daily/CODE.csv (date,foreign,inst) 가 있으면 읽는다. 없으면 빈 dict."""
    out: dict[str, tuple[int, int]] = {}
    p = Path(path)
    if p.exists():
        with p.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                out[r["date"]] = (int(r["foreign"]), int(r["inst"]))
    return out


def make_daily_provider(data_dir: str | Path):
    cache: dict[str, dict[str, tuple[int, int]]] = {}
    bars_cache: dict[str, list] = {}

    def provider(code: str, d: date) -> DailyContext:
        from dataclasses import replace

        from .regime import detect, load_daily_bars

        if code not in cache:
            cache[code] = load_daily_context(Path(data_dir) / "daily" / f"{code}.csv")
            bars_cache[code] = load_daily_bars(Path(data_dir) / "daily_bars" / f"{code}.csv")
        ds = d.strftime("%Y%m%d")
        ctx = DailyContext()
        rows = cache[code]
        prev = sorted(k for k in rows if k < ds)[-5:]
        if prev:
            f1, i1 = rows[prev[-1]]
            ctx = DailyContext(f1, i1, sum(rows[k][0] for k in prev), sum(rows[k][1] for k in prev))
        if bars_cache[code]:  # 당일 이후 데이터는 보지 않는다(룩어헤드 방지)
            r = detect([b for b in bars_cache[code] if b.date < ds])
            ctx = replace(ctx, regime=r.state, regime_note=r.note)
        return ctx

    return provider


def metrics(trades: list[dict], start_equity: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "avg_net_pct": 0.0, "profit_factor": 0.0, "total_net": 0.0,
                "total_return": 0.0, "max_dd": 0.0, "expectancy_pct": 0.0}
    wins = [t for t in trades if t["net"] > 0]
    gp = sum(t["net"] for t in wins)
    gl = -sum(t["net"] for t in trades if t["net"] <= 0)
    eq, peak, mdd = start_equity, start_equity, 0.0
    for t in sorted(trades, key=lambda x: x["exit_ts"]):
        eq += t["net"]
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    avg = sum(t["net_pct"] for t in trades) / n
    return {
        "trades": n,
        "win_rate": len(wins) / n,
        "avg_net_pct": avg,
        "expectancy_pct": avg,
        "profit_factor": (gp / gl) if gl > 0 else (math.inf if gp > 0 else 0.0),
        "total_net": sum(t["net"] for t in trades),
        "total_return": sum(t["net"] for t in trades) / start_equity,
        "max_dd": mdd,
    }


def load_all(data_dir: str | Path, codes: list[str], days: list[str] | None = None) -> dict[str, list[Tick | Quote]]:
    days = days or list_days(data_dir, codes)
    return {d: load_day(data_dir, d, codes) for d in days}


def run_replay(
    codes: list[str],
    day_events: dict[str, list[Tick | Quote]],
    params: Params,
    initial_cash: float = 10_000_000,
    data_dir: str | Path = "data",
    force_close_at_end: bool = True,
) -> tuple[TradingEngine, dict]:
    """날짜 순서대로 재생한다. 이월 포지션은 다음 날로 이어진다."""
    broker = PaperBroker(initial_cash, CostModel(params.fee_rate, params.sell_tax, params.slip_ticks))
    engine = TradingEngine(codes, params, broker, daily_provider=make_daily_provider(data_dir), kill_file=None)
    for day in sorted(day_events):
        events = day_events[day]
        for e in events:
            if isinstance(e, Tick):
                engine.on_tick(e)
            else:
                engine.on_quote(e)
        if events:
            # 장 마감 후 시각으로 한 번 시계를 돌려 시간 조건(청산·이월)이 빠짐없이 평가되게 한다
            last = events[-1].ts
            for hh, mm in ((15, 8), (15, 16), (15, 30)):
                t = datetime.combine(last.date(), time(hh, mm))
                if t > last:
                    engine.on_clock(t)
    if force_close_at_end:
        engine.flatten_all("end_of_data")
    return engine, metrics(engine.trades, initial_cash)
