"""1분봉 모델과 입출력.

데이터 출처 3가지
  1) record 로 쌓은 틱  -> ticks_to_bars (매수/매도 체결량이 같이 들어가 가장 정밀)
  2) KIS 일별분봉 조회   -> KisRest.minute_bars (kis.py)
  3) HTS 에서 내려받은 CSV -> import_csv (한글 헤더 허용)
저장 위치: data/minute/종목코드/YYYYMMDD.csv
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import Tick

COLUMNS = ["ts", "open", "high", "low", "close", "volume", "value", "buy_vol", "sell_vol"]


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime  # 분봉 시각(제공된 라벨 그대로)
    open: int
    high: int
    low: int
    close: int
    volume: int
    value: float = 0.0  # 거래대금(없으면 0)
    buy_vol: int = 0  # 틱에서 만든 봉만 값이 있다(체결 방향 정보)
    sell_vol: int = 0


def sec_of_day(ts: datetime) -> int:
    return ts.hour * 3600 + ts.minute * 60 + ts.second


def ticks_to_bars(events: Iterable) -> list[Bar]:
    out: list[Bar] = []
    cur: list | None = None

    def done(c: list) -> Bar:
        return Bar(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8])

    for e in events:
        if not isinstance(e, Tick):
            continue
        key = e.ts.replace(second=0, microsecond=0)
        if cur is None or cur[0] != key:
            if cur is not None:
                out.append(done(cur))
            cur = [key, e.price, e.price, e.price, e.price, 0, 0.0, 0, 0]
        cur[2] = max(cur[2], e.price)
        cur[3] = min(cur[3], e.price)
        cur[4] = e.price
        cur[5] += e.vol
        cur[6] += e.price * e.vol
        if e.side > 0:
            cur[7] += e.vol
        elif e.side < 0:
            cur[8] += e.vol
    if cur is not None:
        out.append(done(cur))
    return out


# ---------------------------------------------------------------- 저장/읽기
def minute_path(data_dir: str | Path, code: str, day: str) -> Path:
    return Path(data_dir) / "minute" / code / f"{day}.csv"


def save_bars(path: str | Path, bars: list[Bar]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for b in bars:
            w.writerow([b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume, round(b.value, 1), b.buy_vol, b.sell_vol])


def load_bars(path: str | Path) -> list[Bar]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        rows = [
            Bar(datetime.fromisoformat(r["ts"]), int(r["open"]), int(r["high"]), int(r["low"]), int(r["close"]),
                int(r["volume"]), float(r.get("value") or 0), int(r.get("buy_vol") or 0), int(r.get("sell_vol") or 0))
            for r in csv.DictReader(fh)
        ]
    return sorted(rows, key=lambda b: b.ts)


def save_by_day(data_dir: str | Path, code: str, bars: list[Bar]) -> list[str]:
    by_day: dict[str, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.ts.strftime("%Y%m%d"), []).append(b)
    for day, bs in by_day.items():
        save_bars(minute_path(data_dir, code, day), sorted(bs, key=lambda b: b.ts))
    return sorted(by_day)


def load_days(data_dir: str | Path, code: str, since: str = "", until: str = "", last: int = 0) -> dict[str, list[Bar]]:
    root = Path(data_dir) / "minute" / code
    if not root.exists():
        return {}
    days = sorted(p.stem for p in root.glob("*.csv"))
    if since:
        days = [d for d in days if d >= since]
    if until:
        days = [d for d in days if d <= until]
    if last:
        days = days[-last:]
    return {d: load_bars(minute_path(data_dir, code, d)) for d in days}


# ---------------------------------------------------------------- HTS CSV 가져오기
_ALIASES = {
    "ts": ["ts", "datetime", "일시", "체결일시"],
    "date": ["date", "일자", "날짜", "거래일", "stck_bsop_date"],
    "time": ["time", "시간", "체결시간", "stck_cntg_hour"],
    "open": ["open", "시가", "stck_oprc"],
    "high": ["high", "고가", "stck_hgpr"],
    "low": ["low", "저가", "stck_lwpr"],
    "close": ["close", "종가", "현재가", "stck_prpr"],
    "volume": ["volume", "거래량", "cntg_vol"],
    "value": ["value", "거래대금", "acml_tr_pbmn"],
}


def _num(s: str) -> float:
    s = re.sub(r"[,+\s]", "", str(s))
    return abs(float(s)) if s not in ("", "-") else 0.0  # HTS 는 하락 시 부호를 붙이기도 한다


def _parse_ts(row: dict, cols: dict) -> datetime:
    if "ts" in cols:
        raw = row[cols["ts"]].strip().replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                pass
        return datetime.fromisoformat(raw)
    d = re.sub(r"\D", "", row[cols["date"]])
    t = re.sub(r"\D", "", row[cols["time"]]).zfill(4)
    t = (t + "00")[:6] if len(t) <= 4 else t.zfill(6)
    return datetime(int(d[:4]), int(d[4:6]), int(d[6:8]), int(t[:2]), int(t[2:4]), int(t[4:6]))


def import_csv(path: str | Path) -> list[Bar]:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 인코딩을 읽을 수 없습니다(utf-8/cp949 만 지원)")
    rdr = csv.DictReader(text.splitlines())
    names = {(n or "").strip().lower(): n for n in (rdr.fieldnames or [])}
    cols: dict[str, str] = {}
    for key, aliases in _ALIASES.items():
        for a in aliases:
            if a.lower() in names:
                cols[key] = names[a.lower()]
                break
    need = ["open", "high", "low", "close", "volume"]
    missing = [k for k in need if k not in cols] + ([] if "ts" in cols or ("date" in cols and "time" in cols) else ["일자+시간"])
    if missing:
        raise ValueError(f"필요한 열을 찾지 못했습니다: {missing} (헤더: {list(names.values())})")
    bars = []
    for row in rdr:
        try:
            bars.append(
                Bar(_parse_ts(row, cols), int(_num(row[cols["open"]])), int(_num(row[cols["high"]])),
                    int(_num(row[cols["low"]])), int(_num(row[cols["close"]])), int(_num(row[cols["volume"]])),
                    _num(row[cols["value"]]) if "value" in cols else 0.0)
            )
        except (ValueError, KeyError):
            continue  # 합계/빈 줄 등
    return sorted(bars, key=lambda b: b.ts)
