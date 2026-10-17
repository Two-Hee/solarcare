"""1분봉 정밀 분석.

무엇을 재는가
  - 일별 요약: 갭, 일중 변동폭, 고/저가 시각, 장전·시간외 이동(데이터에 있을 때)
  - 1분 변동폭 분포와 시간대별 프로필 (시초 쏠림 확인)
  - ATR 게이트 통과율: 지금 파라미터가 이 종목에서 하루 중 얼마나 매매를 허용/차단하나
  - 스윙(지그재그): 몇 % 짜리 오르내림이 하루에 몇 번 나오나 (사후적 상한이지 달성 가능 수익이 아님)
  - 기준선: 아무 분에나 진입해도 +TP 가 -SL 보다 먼저 닿는 비율과 비용 반영 기대값
  - 이벤트 스터디: 거래량 급증/저점 반등 신호 뒤의 성적을 기준선과 비교

주의: 봉 데이터에는 체결 방향이 없다(틱으로 만든 봉 제외). 봉 압력(pressure)은 종가 위치로 추정한 대용치다.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from .bars import Bar, sec_of_day
from .config import Params, hms
from .costs import tick_size

REG_OPEN = 9 * 3600
REG_END = 15 * 3600 + 31 * 60  # 15:30 봉까지 정규장
PRE_OPEN = 8 * 3600


def regular(bars: list[Bar]) -> list[Bar]:
    return [b for b in bars if REG_OPEN <= sec_of_day(b.ts) < REG_END]


def _pre(bars: list[Bar]) -> list[Bar]:
    return [b for b in bars if PRE_OPEN <= sec_of_day(b.ts) < REG_OPEN]


def _after(bars: list[Bar]) -> list[Bar]:
    return [b for b in bars if sec_of_day(b.ts) >= REG_END]


def bar_range(b: Bar) -> float:
    return (b.high - b.low) / b.close if b.close else 0.0


def pressure(b: Bar) -> float:
    """-1~+1. 틱에서 만든 봉은 실제 매수/매도 체결량, 아니면 종가 위치로 추정."""
    cls = b.buy_vol + b.sell_vol
    if cls > 0:
        return (b.buy_vol - b.sell_vol) / cls
    rng = b.high - b.low
    return ((b.close - b.low) - (b.high - b.close)) / rng if rng > 0 else 0.0


def atr_at(reg: list[Bar], i: int, n: int = 15) -> float:
    """i 번째 봉이 끝난 시점의 최근 n개 봉 평균 변동폭. 5개 미만이면 0 (flow.py 와 같은 정의)."""
    w = reg[max(0, i - n + 1) : i + 1]
    return sum(bar_range(b) for b in w) / len(w) if len(w) >= 5 else 0.0


# ---------------------------------------------------------------- 일별 요약
def day_stats(days: dict[str, list[Bar]]) -> list[dict]:
    rows, prev_close = [], None
    for d in sorted(days):
        reg = regular(days[d])
        if not reg:
            continue
        o, c = reg[0].open, reg[-1].close
        hi, lo = max(b.high for b in reg), min(b.low for b in reg)
        vol = sum(b.volume for b in reg)
        val = sum(b.value for b in reg) or sum(b.close * b.volume for b in reg)
        t_hi = next(b.ts for b in reg if b.high == hi)
        t_lo = next(b.ts for b in reg if b.low == lo)
        first30 = [b for b in reg if sec_of_day(b.ts) < REG_OPEN + 1800]
        r30 = (max(b.high for b in first30) - min(b.low for b in first30)) if first30 else 0
        pre, aft = _pre(days[d]), _after(days[d])
        rows.append({
            "day": d, "open": o, "high": hi, "low": lo, "close": c,
            "gap": (o / prev_close - 1) if prev_close else None,
            "ret": (c / prev_close - 1) if prev_close else (c / o - 1),
            "range": (hi - lo) / o,
            "t_high": t_hi.strftime("%H:%M"), "t_low": t_lo.strftime("%H:%M"),
            "first30_share": r30 / (hi - lo) if hi > lo else 0.0,
            "vwap_close_dev": c / (val / vol) - 1 if vol else 0.0,
            "value": val,
            "pre_move": (pre[-1].close / prev_close - 1) if pre and prev_close else None,
            "pre_high": (max(b.high for b in pre) / prev_close - 1) if pre and prev_close else None,
            "after_move": (aft[-1].close / c - 1) if aft else None,
            "after_low": (min(b.low for b in aft) / c - 1) if aft else None,
        })
        prev_close = c
    return rows


# ---------------------------------------------------------------- 변동폭 분포 / 시간대 프로필 / ATR 게이트
def range_percentiles(days: dict[str, list[Bar]]) -> dict:
    r = sorted(bar_range(b) for bs in days.values() for b in regular(bs))
    if not r:
        return {}
    q = lambda p: r[min(len(r) - 1, int(len(r) * p))]  # noqa: E731
    return {"n": len(r), "p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "mean": sum(r) / len(r)}


def vol_profile(days: dict[str, list[Bar]], bucket_min: int = 30) -> list[dict]:
    agg: dict[int, list[float]] = defaultdict(lambda: [0.0, 0, 0.0])
    total = 0.0
    for bs in days.values():
        for b in regular(bs):
            k = (sec_of_day(b.ts) - REG_OPEN) // (bucket_min * 60)
            a = agg[k]
            a[0] += bar_range(b)
            a[1] += 1
            a[2] += b.volume
            total += b.volume
    out = []
    for k in sorted(agg):
        s = REG_OPEN + k * bucket_min * 60
        out.append({"bucket": f"{s // 3600:02d}:{s % 3600 // 60:02d}", "avg_range": agg[k][0] / agg[k][1],
                    "vol_share": agg[k][2] / total if total else 0.0, "n": agg[k][1]})
    return out


def gate_pass(days: dict[str, list[Bar]], p: Params) -> dict:
    """현재 파라미터의 변동성 게이트(stops)가 하루 중 진입을 허용/차단하는 비율."""
    tot = quiet = wild = 0
    by_hour: dict[int, list[int]] = defaultdict(lambda: [0, 0, 0])
    for bs in days.values():
        reg = regular(bs)
        for i, b in enumerate(reg):
            if sec_of_day(b.ts) < hms(p.entry_start) or sec_of_day(b.ts) >= hms(p.entry_end):
                continue
            atr = atr_at(reg, i, p.atr_minutes)
            h = b.ts.hour
            tot += 1
            by_hour[h][0] += 1
            if atr < p.min_atr1m:
                quiet += 1
                by_hour[h][1] += 1
            elif max(p.sl_pct, p.sl_atr_mult * atr) > p.sl_max_pct:
                wild += 1
                by_hour[h][2] += 1
    return {
        "n": tot, "quiet": quiet / tot if tot else 0.0, "wild": wild / tot if tot else 0.0,
        "pass": 1 - (quiet + wild) / tot if tot else 0.0,
        "by_hour": {h: {"n": v[0], "quiet": v[1] / v[0], "wild": v[2] / v[0]} for h, v in sorted(by_hour.items())},
    }


# ---------------------------------------------------------------- 스윙(지그재그)
def zigzag(bars: list[Bar], thr: float) -> list[dict]:
    pts = [(b.ts, b.close) for b in bars]
    if len(pts) < 2:
        return []
    hi = lo = pts[0]
    direction = 0
    piv = []
    for pt in pts[1:]:
        p = pt[1]
        if p > hi[1]:
            hi = pt
        if p < lo[1]:
            lo = pt
        if direction == 0:
            if p >= lo[1] * (1 + thr) and lo[0] < pt[0]:
                piv.append(lo)
                direction, hi = 1, pt
            elif p <= hi[1] * (1 - thr) and hi[0] < pt[0]:
                piv.append(hi)
                direction, lo = -1, pt
        elif direction == 1:
            if p <= hi[1] * (1 - thr):
                piv.append(hi)
                direction, lo = -1, pt
        elif p >= lo[1] * (1 + thr):
            piv.append(lo)
            direction, hi = 1, pt
    if direction == 1:
        piv.append(hi)
    elif direction == -1:
        piv.append(lo)
    legs = []
    for a, b in zip(piv, piv[1:]):
        legs.append({"t0": a[0], "t1": b[0], "p0": a[1], "p1": b[1], "pct": b[1] / a[1] - 1,
                     "minutes": (b[0] - a[0]).total_seconds() / 60})
    return legs


def swing_summary(days: dict[str, list[Bar]], thr: float) -> dict:
    ups, downs, per_day_up = [], [], []
    for bs in days.values():
        legs = zigzag(regular(bs), thr)
        u = [x for x in legs if x["pct"] > 0]
        ups += u
        downs += [x for x in legs if x["pct"] < 0]
        per_day_up.append(sum(x["pct"] for x in u))
    med = lambda xs: statistics.median(xs) if xs else 0.0  # noqa: E731
    return {
        "thr": thr, "days": len(days),
        "up_n": len(ups), "down_n": len(downs),
        "up_per_day": len(ups) / len(days) if days else 0.0,
        "up_mean": sum(x["pct"] for x in ups) / len(ups) if ups else 0.0,
        "up_median": med([x["pct"] for x in ups]), "up_minutes_median": med([x["minutes"] for x in ups]),
        "down_mean": sum(x["pct"] for x in downs) / len(downs) if downs else 0.0,
        "hindsight_up_per_day": sum(per_day_up) / len(per_day_up) if per_day_up else 0.0,
    }


# ---------------------------------------------------------------- 진입 기준선 (TP/SL 먼저 닿기)
@dataclass
class Rules:
    tp: float = 0.013
    sl: float = 0.010
    max_hold_min: int = 45
    flatten: str = "15:15"
    slip_ticks: int = 1
    fee: float = 0.00015
    tax: float = 0.0018


def simulate_exit(reg: list[Bar], i: int, r: Rules) -> dict:
    """i 번째 봉 종가에 매수(+슬리피지)한 롱의 결과. 같은 봉에서 TP/SL 이 모두 닿으면 SL 먼저(보수적)."""
    entry = reg[i].close + r.slip_ticks * tick_size(reg[i].close)
    tp_px, sl_px = entry * (1 + r.tp), entry * (1 - r.sl)
    t0 = reg[i].ts
    flat = hms(r.flatten)
    exit_px, reason, j_end = reg[-1].close, "eod", len(reg) - 1
    for j in range(i + 1, len(reg)):
        b = reg[j]
        held = (b.ts - t0).total_seconds() / 60
        if b.open <= sl_px:
            exit_px, reason, j_end = b.open, "stop_gap", j
        elif b.open >= tp_px:
            exit_px, reason, j_end = b.open, "tp_gap", j
        elif b.low <= sl_px:
            exit_px, reason, j_end = sl_px, "stop", j
        elif b.high >= tp_px:
            exit_px, reason, j_end = tp_px, "tp", j
        elif held >= r.max_hold_min:
            exit_px, reason, j_end = b.close, "time", j
        elif sec_of_day(b.ts) >= flat:
            exit_px, reason, j_end = b.close, "eod", j
        else:
            continue
        break
    sell = max(1.0, exit_px - r.slip_ticks * tick_size(exit_px))
    net = sell * (1 - r.fee - r.tax) / (entry * (1 + r.fee)) - 1
    return {"net": net, "reason": reason, "minutes": (reg[j_end].ts - t0).total_seconds() / 60, "entry": entry}


def _entry_ok(b: Bar, p: Params) -> bool:
    s = sec_of_day(b.ts)
    return hms(p.entry_start) <= s < hms(p.entry_end)


def base_rate(days: dict[str, list[Bar]], r: Rules, p: Params, step: int = 1) -> list[dict]:
    out = []
    for d, bs in days.items():
        reg = regular(bs)
        for i in range(0, len(reg) - 1, step):
            if _entry_ok(reg[i], p):
                out.append({"day": d, "ts": reg[i].ts, **simulate_exit(reg, i, r)})
    return out


def summarize_trades(trades: list[dict], key=None) -> list[dict]:
    groups: dict = defaultdict(list)
    for t in trades:
        groups[key(t) if key else "전체"].append(t)
    rows = []
    for k in sorted(groups, key=str):
        g = groups[k]
        n = len(g)
        nets = [t["net"] for t in g]
        sd = statistics.pstdev(nets) if n > 1 else 0.0
        rows.append({
            "key": k, "n": n,
            "win": sum(1 for x in nets if x > 0) / n,
            "tp": sum(1 for t in g if t["reason"].startswith("tp")) / n,
            "stop": sum(1 for t in g if t["reason"].startswith("stop")) / n,
            "timeout": sum(1 for t in g if t["reason"] in ("time", "eod")) / n,
            "avg_net": sum(nets) / n,
            "t_stat": (sum(nets) / n) / (sd / n ** 0.5) if sd > 0 else 0.0,  # 겹치는 표본이라 과대평가된다
            "avg_min": sum(t["minutes"] for t in g) / n,
        })
    return rows


# ---------------------------------------------------------------- 신호 / 이벤트 스터디
def volume_spike_signals(reg: list[Bar], mult: float = 4.0, lookback: int = 30, min_gap: int = 10, bullish: bool = True) -> list[int]:
    sig, last = [], -10**9
    for i in range(lookback, len(reg)):
        med = statistics.median(b.volume for b in reg[i - lookback : i])
        b = reg[i]
        if med > 0 and b.volume >= mult * med and (b.close > b.open if bullish else b.close < b.open) and i - last >= min_gap:
            sig.append(i)
            last = i
    return sig


def dip_signals(reg: list[Bar], p: Params, min_gap: int = 10) -> list[int]:
    """전략의 분할매수 진입 조건을 봉으로 근사: 낙폭 -> 저점 확인 -> 반등폭 -> 매도 압력 소진."""
    sig, last = [], -10**9
    for i in range(5, len(reg)):
        b = reg[i]
        if not _entry_ok(b, p) or i - last < min_gap:
            continue
        cut = b.ts - timedelta(minutes=p.dip_lookback_min)
        idx = [k for k in range(max(0, i - p.dip_lookback_min - 2), i + 1) if reg[k].ts >= cut]
        hi = max(reg[k].high for k in idx)
        lo = min(reg[k].low for k in idx)
        lo_k = max(k for k in idx if reg[k].low == lo)
        if lo_k >= i:  # 이번 봉이 신저점이면 아직 떨어지는 중
            continue
        drop = 1 - lo / hi
        if drop < max(p.dip_min_drop_pct, p.dip_atr_mult * atr_at(reg, i, p.atr_minutes)):
            continue
        bounce = b.close / lo - 1
        if not (p.dip_bounce_pct <= bounce <= p.dip_max_bounce_pct):
            continue
        if min(pressure(reg[i]), pressure(reg[i - 1])) < -0.2 or (pressure(reg[i]) + pressure(reg[i - 1])) / 2 < 0:
            continue
        sig.append(i)
        last = i
    return sig


def event_study(days: dict[str, list[Bar]], signal_fn, r: Rules, horizons=(1, 5, 15, 30)) -> dict:
    trades, fwd, mfe, mae = [], {h: [] for h in horizons}, [], []
    for d, bs in days.items():
        reg = regular(bs)
        times = [b.ts for b in reg]
        for i in signal_fn(reg):
            if i >= len(reg) - 1:
                continue
            e = reg[i].close
            trades.append({"day": d, "ts": reg[i].ts, **simulate_exit(reg, i, r)})
            for h in horizons:
                k = bisect_right(times, reg[i].ts + timedelta(minutes=h)) - 1
                fwd[h].append(reg[k].close / e - 1)
            w = [b for b in reg[i + 1 :] if (b.ts - reg[i].ts).total_seconds() <= 1800]
            if w:
                mfe.append(max(b.high for b in w) / e - 1)
                mae.append(min(b.low for b in w) / e - 1)
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return {
        "n": len(trades), "summary": (summarize_trades(trades) or [None])[0],
        "fwd": {h: avg(v) for h, v in fwd.items()}, "mfe30": avg(mfe), "mae30": avg(mae), "trades": trades,
    }


def volume_spike_table(days: dict[str, list[Bar]], mult: float = 4.0, top: int = 10) -> list[dict]:
    rows = []
    for d, bs in days.items():
        reg = regular(bs)
        times = [b.ts for b in reg]
        for i in range(30, len(reg) - 1):
            med = statistics.median(b.volume for b in reg[i - 30 : i])
            if med > 0 and reg[i].volume >= mult * med:
                e = reg[i].close
                out = {"ts": reg[i].ts, "mult": reg[i].volume / med, "bar_ret": reg[i].close / reg[i].open - 1}
                for h in (5, 15):
                    k = bisect_right(times, reg[i].ts + timedelta(minutes=h)) - 1
                    out[f"f{h}"] = reg[k].close / e - 1
                rows.append(out)
    return sorted(rows, key=lambda x: -x["mult"])[:top]
