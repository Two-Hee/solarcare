"""합성(가짜) 틱 생성기. 파이프라인/테스트 점검용이며 수익성의 근거가 될 수 없다.

'매집 구간(매수체결 우세 + 가격이 뒤따라 상승)'과 '분배 구간(반대)'을 일부러 심어두므로,
여기서 전략이 돈을 버는 것은 당연하다. 실제 검증은 반드시 실데이터(record)로 해야 한다.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path

from .costs import tick_size
from .models import Quote, Tick
from .recorder import Recorder


def gen_day(code: str, day: datetime, start_price: int, rng: random.Random) -> tuple[list, int]:
    events: list = []
    price = start_price
    t = day.replace(hour=9, minute=0, second=0, microsecond=0)
    end = day.replace(hour=15, minute=20)
    regime, regime_until = "normal", t
    ob_bias = 0.0
    n = 0
    while t < end:
        if t >= regime_until:
            r = rng.random()
            if r < 0.15:
                regime, dur = "accum", rng.randint(120, 480)
            elif r < 0.27:
                regime, dur = "distrib", rng.randint(120, 400)
            else:
                regime, dur = "normal", rng.randint(120, 600)
            regime_until = t + timedelta(seconds=dur)
        buy_p = {"normal": 0.5, "accum": 0.68, "distrib": 0.32}[regime]
        drift = {"normal": 0.0, "accum": 0.45, "distrib": -0.45}[regime]
        ob_bias = {"normal": 0.0, "accum": 0.35, "distrib": -0.35}[regime]
        t += timedelta(seconds=rng.expovariate(1 / 1.2))
        if t >= end:
            break
        ts = tick_size(price)
        side = 1 if rng.random() < buy_p else -1
        step = rng.choice([0, 0, 0, 1]) * (1 if rng.random() < 0.5 + drift * 0.25 else -1)
        price = max(ts, price + step * ts)
        vol = max(1, int(rng.lognormvariate(3.2, 1.0)))
        if rng.random() < 0.03:  # 큰 손
            vol *= rng.randint(8, 25)
        ts_ = tick_size(price)
        ask1, bid1 = price + (ts_ if side < 0 else 0), price - (ts_ if side > 0 else 0)
        if ask1 <= bid1:
            ask1 = bid1 + ts_
        events.append(Tick(t, code, price, vol, side, ask1, bid1))
        n += 1
        if n % 3 == 0:
            tot = rng.randint(80_000, 140_000)
            bid_share = min(0.9, max(0.1, 0.5 + ob_bias * 0.5 + rng.uniform(-0.1, 0.1)))
            events.append(
                Quote(t, code, ask1, bid1, rng.randint(500, 4000), rng.randint(500, 4000),
                      int(tot * (1 - bid_share)), int(tot * bid_share))
            )
    return events, price


def generate(code: str, days: int, out_dir: str | Path, start_price: int = 70_000, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    rec = Recorder(out_dir)
    day = datetime(2026, 9, 1)
    price = start_price
    made: list[str] = []
    while len(made) < days:
        if day.weekday() < 5:
            events, price = gen_day(code, day, price, rng)
            for e in events:
                (rec.tick if isinstance(e, Tick) else rec.quote)(e)
            made.append(day.strftime("%Y%m%d"))
        day += timedelta(days=1)
    rec.close()
    return made
