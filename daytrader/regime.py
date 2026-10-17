"""일봉 기반 '수급 변화' 국면 감지기.

찾는 모양: 좁은 횡보(저변동·저거래) -> 거래대금 폭발 + 장대양봉 + 직전 N일 고가 돌파 -> 이후 변동성 확장 구간.
상태:
  unknown   일봉이 부족
  none      특이 없음
  base      횡보(압축) 중 - 돌파 후보. 아직 매매 대상 아님
  breakout  돌파 당일(장 마감 후 판단용)
  expansion 돌파 후 expansion_days 이내이고 최근 일중 변동폭이 큰 구간  <- 단타 대상
  fading    돌파 후 시간이 지났거나 변동폭이 줄어든 구간
이 모양이 실제로 돈이 되는지는 검증되지 않았다. regime_gate 는 기본 꺼져 있다.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

TRADABLE = ("breakout", "expansion", "bandstart")


@dataclass(frozen=True)
class DailyBar:
    date: str  # YYYYMMDD
    open: float
    high: float
    low: float
    close: float
    value: float  # 거래대금


@dataclass
class Regime:
    state: str = "unknown"
    note: str = ""
    signals: dict = field(default_factory=dict)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def detect_with(bars: list[DailyBar], p) -> Regime:
    """Params 의 임계값으로 국면 판정(일봉 밭 시작 포함)."""
    return detect(bars, band_fast=p.band_daily_fast, band_slow=p.band_daily_slow,
                  band_min_width=p.band_daily_min_width, band_recent=p.band_daily_recent,
                  band_confirm_bars=p.band_daily_confirm_bars)


def detect(bars: list[DailyBar], band_fast: int = 10, band_slow: int = 20, band_min_width: float = 0.01,
           band_recent: int = 5, band_confirm_bars: int = 8, **kw) -> Regime:
    r = _detect_events(bars, **kw)
    if r.state in ("none", "base", "fading") and len(bars) > band_slow:
        from .bands import BandTracker

        tr = BandTracker(band_fast, band_slow, band_min_width, confirm_bars=band_confirm_bars, slow_tol=0.005, fresh_bars=band_recent)
        for b in bars:
            tr.on_bar(b.date, b.close)
        if tr.fresh:
            sig = {**r.signals, "band_start": tr.start_ts, "band_days": tr.bars_since_start, "band_width": round(tr.width, 4)}
            return Regime("bandstart", f"일봉 {band_fast}/{band_slow} 이평 밭 시작({tr.start_ts}) 후 {tr.bars_since_start}일째, 간격 {tr.width * 100:.1f}%", sig)
    return r


def _detect_events(
    bars: list[DailyBar],
    burst_mult: float = 3.0,
    burst_ret: float = 0.04,
    breakout_days: int = 20,
    value_lookback: int = 20,
    base_days: int = 15,
    base_range_max: float = 0.15,
    expansion_days: int = 10,
    expansion_range_min: float = 0.03,
) -> Regime:
    n = len(bars)
    need = max(breakout_days, value_lookback) + 2
    if n < need:
        return Regime("unknown", f"일봉 {n}개 (최소 {need}개 필요)")

    event = None
    for i in range(n - 1, max(need - 2, n - 2 - expansion_days - 5), -1):
        prev_val = _mean([b.value for b in bars[i - value_lookback : i]])
        if prev_val <= 0:
            continue
        ratio = bars[i].value / prev_val
        ret = bars[i].close / bars[i - 1].close - 1 if bars[i - 1].close else 0.0
        hi_prev = max(b.high for b in bars[i - breakout_days : i])
        if ratio >= burst_mult and ret >= burst_ret and bars[i].close > hi_prev:
            event = (i, ratio, ret, hi_prev)
            break

    if event:
        i, ratio, ret, hi_prev = event
        since = n - 1 - i
        recent = [(b.high - b.low) / b.close for b in bars[-3:] if b.close]
        rr = _mean(recent)
        sig = {
            "event_date": bars[i].date, "days_since": since, "value_ratio": round(ratio, 1),
            "event_ret": round(ret, 4), "broke_high": hi_prev, "recent_range": round(rr, 4),
        }
        if since == 0:
            return Regime("breakout", f"{bars[i].date} 거래대금 {ratio:.1f}배 + {ret * 100:.1f}% 돌파", sig)
        if since <= expansion_days and rr >= expansion_range_min:
            return Regime("expansion", f"돌파({bars[i].date}) 후 {since}일째, 최근 일중 변동폭 {rr * 100:.1f}%", sig)
        return Regime("fading", f"돌파({bars[i].date}) 후 {since}일째, 최근 일중 변동폭 {rr * 100:.1f}%", sig)

    win = bars[-base_days:]
    avg_close = _mean([b.close for b in win])
    rng = (max(b.high for b in win) - min(b.low for b in win)) / avg_close if avg_close else 9.9
    if rng <= base_range_max:
        return Regime("base", f"최근 {base_days}일 횡보폭 {rng * 100:.1f}% - 돌파 후보", {"base_range": round(rng, 4)})
    return Regime("none", "특이 국면 없음", {"base_range": round(rng, 4)})


def load_daily_bars(path: str | Path) -> list[DailyBar]:
    """CSV: date,open,high,low,close,value (HTS 에서 내려받아도 됨)."""
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        rows = [
            DailyBar(r["date"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["value"]))
            for r in csv.DictReader(fh)
        ]
    return sorted(rows, key=lambda b: b.date)
