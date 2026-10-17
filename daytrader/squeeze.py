"""압축 -> 확장 돌파: 변동성이 줄어든 박스에서 거래량·매수체결이 동반된 상향 돌파의 시작.

왜 이 기준인가 (가설이지 증명이 아니다)
  1) 변동성은 군집한다 - 조용한 구간 뒤에는 큰 움직임이 나오는 경향이 있다.
  2) 박스 돌파에 거래량 증가와 매수 체결 우위가 같이 붙으면 단순한 가격 이탈보다 정보가 있을 가능성이 크다.
  3) 박스 안쪽(기본: 중간~저점)이 '이 가정이 틀렸다'는 자리라 손절이 구조적으로 정해지고, 진입 전에 손익비(익절폭 / 손절폭)를 계산할 수 있다.
  4) 종목의 절대 변동성이 아니라 '그 종목의 직전 1시간 대비' 압축 정도를 쓰므로 종목이 달라도 같은 정의가 통한다.

정의
  box   최근 sq_base_min 개 완성된 1분봉(같은 날짜). 고점 box_hi, 저점 box_lo
  압축   box 의 평균 1분 변동폭 / 직전 sq_ref_min 개 봉의 평균 1분 변동폭 <= sq_compress_ratio
  트리거 (strategy.py) 가격이 box_hi 를 넘고, 최근 60초 거래량 >= box 분당 거래량 중앙값 x sq_vol_mult,
         매수체결 우위(delta >= sq_min_delta), 손익비 >= sq_min_rr
이 기준이 실제로 돈이 되는지는 검증 전이다. analyze 10절이 구성요소를 하나씩 빼 가며 기준선과 비교한다.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class SqueezeSnap:
    ready: bool = False
    compressed: bool = False
    ratio: float = 0.0  # 박스 평균 변동폭 / 직전 기준 구간 평균 변동폭
    box_hi: int = 0
    box_lo: int = 0
    box_start: datetime | None = None
    box_end: datetime | None = None
    vol_med: float = 0.0  # 박스 구간 분당 거래량 중앙값


class SqueezeTracker:
    def __init__(self, base: int = 20, ref: int = 60, ratio_max: float = 0.7):
        self.base, self.ref, self.ratio_max = base, ref, ratio_max
        self.bars: deque[tuple] = deque(maxlen=base + ref)  # (ts, high, low, close, volume)

    def add_bar(self, ts: datetime, high: int, low: int, close: int, volume: int) -> None:
        if close > 0 and high >= low > 0:
            self.bars.append((ts, high, low, close, volume))

    def snap(self) -> SqueezeSnap:
        if len(self.bars) < self.base + self.ref:
            return SqueezeSnap()
        bars = list(self.bars)
        ref, box = bars[: self.ref], bars[-self.base :]
        if box[0][0].date() != box[-1][0].date():
            return SqueezeSnap()  # 박스가 날짜를 가로지르면(갭 포함) 압축으로 보지 않는다
        rng = lambda b: (b[1] - b[2]) / b[3]  # noqa: E731
        atr_ref = sum(rng(b) for b in ref) / len(ref)
        if atr_ref <= 0:
            return SqueezeSnap()
        atr_box = sum(rng(b) for b in box) / len(box)
        ratio = atr_box / atr_ref
        return SqueezeSnap(
            ready=True, compressed=ratio <= self.ratio_max, ratio=ratio,
            box_hi=max(b[1] for b in box), box_lo=min(b[2] for b in box),
            box_start=box[0][0], box_end=box[-1][0], vol_med=statistics.median(b[4] for b in box),
        )


def stop_price(box_hi: float, box_lo: float, frac: float, buffer: float) -> float:
    """구조적 손절가. frac=1.0 이면 박스 저점, 0.5 면 박스 중간(돌파 실패 시 보통 중간까지 되돌아온다)."""
    return (box_hi - frac * (box_hi - box_lo)) * (1 - buffer)


def structural_risk(price: float, stop_px: float) -> float:
    """진입가에서 구조적 손절가까지의 거리(비율)."""
    return (price - stop_px) / price if price > 0 else 0.0
