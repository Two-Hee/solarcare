"""틱/호가를 data/ticks/YYYYMMDD/CODE.csv 로 기록하고 다시 읽는다.

매일 쌓이는 이 데이터가 백테스트·튜닝·복기의 원료다(= '매일 학습'의 재료).
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import Quote, Tick

COLUMNS = ["kind", "ts", "code", "price", "vol", "side", "ask1", "bid1", "ask_rem1", "bid_rem1", "total_ask", "total_bid"]


class Recorder:
    def __init__(self, data_dir: str | Path = "data", flush_every: int = 200):
        self.root = Path(data_dir) / "ticks"
        self.flush_every = flush_every
        self._files: dict[tuple[str, str], tuple] = {}
        self._n = 0

    def _writer(self, day: str, code: str):
        key = (day, code)
        if key not in self._files:
            path = self.root / day / f"{code}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            new = not path.exists()
            fh = path.open("a", newline="", encoding="utf-8")
            w = csv.writer(fh)
            if new:
                w.writerow(COLUMNS)
            self._files[key] = (fh, w)
        return self._files[key][1]

    def _maybe_flush(self) -> None:
        self._n += 1
        if self._n % self.flush_every == 0:
            self.flush()

    def tick(self, t: Tick) -> None:
        self._writer(t.ts.strftime("%Y%m%d"), t.code).writerow(
            ["T", t.ts.isoformat(), t.code, t.price, t.vol, t.side, t.ask1, t.bid1, "", "", "", ""]
        )
        self._maybe_flush()

    def quote(self, q: Quote) -> None:
        self._writer(q.ts.strftime("%Y%m%d"), q.code).writerow(
            ["Q", q.ts.isoformat(), q.code, "", "", "", q.ask1, q.bid1, q.ask_rem1, q.bid_rem1, q.total_ask, q.total_bid]
        )
        self._maybe_flush()

    def flush(self) -> None:
        for fh, _ in self._files.values():
            fh.flush()

    def close(self) -> None:
        for fh, _ in self._files.values():
            fh.close()
        self._files.clear()


def list_days(data_dir: str | Path, codes: list[str]) -> list[str]:
    root = Path(data_dir) / "ticks"
    if not root.exists():
        return []
    days = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if any((d / f"{c}.csv").exists() for c in codes):
            days.append(d.name)
    return days


def read_events(path: str | Path) -> Iterator[Tick | Quote]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ts = datetime.fromisoformat(r["ts"])
            if r["kind"] == "T":
                yield Tick(ts, r["code"], int(r["price"]), int(r["vol"]), int(r["side"] or 0), int(r["ask1"] or 0), int(r["bid1"] or 0))
            else:
                yield Quote(
                    ts, r["code"], int(r["ask1"] or 0), int(r["bid1"] or 0), int(r["ask_rem1"] or 0),
                    int(r["bid_rem1"] or 0), int(r["total_ask"] or 0), int(r["total_bid"] or 0),
                )


def load_day(data_dir: str | Path, day: str, codes: list[str]) -> list[Tick | Quote]:
    events: list[Tick | Quote] = []
    for c in codes:
        p = Path(data_dir) / "ticks" / day / f"{c}.csv"
        if p.exists():
            events.extend(read_events(p))
    events.sort(key=lambda e: e.ts)  # 안정 정렬: 같은 시각이면 파일 내 순서 유지
    return events
