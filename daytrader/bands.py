"""이평선 '밭'(두 이평선 사이 색칠 영역)이 생기기 시작하는 지점 감지.

정의 (모든 값은 설정 가능)
  fast/slow  두 단순이평(완성된 봉의 종가 기준, 진행 중인 봉으로 다시 그려지지 않음)
  밭 형성    fast 가 slow 를 아래에서 위로 돌파 (색이 칠해지기 시작)
  확정(start) 돌파 후 confirm_bars 개 봉 안에 [간격 (fast-slow)/slow >= min_width] 이고 [종가 > slow] 이고
              [slow 가 가파르게 꺾이지 않음] 을 만족. 잠깐 스쳐 지나간 돌파(가짜 시작)는 걸러진다
  활성       확정 후 fast > slow 인 동안. fresh = 확정 후 fresh_bars 개 봉 이내
  소멸(end)  fast <= slow (밭이 사라짐)
워밍업 중(slow 개 봉 미만)에는 신호가 없고, 이미 밭이 펼쳐진 상태에서 시작했다면 다음 돌파를 기다린다(가짜 시작 방지).
어느 두 선·어느 시간봉이 '노란 밭'에 해당하는지는 차트마다 다르니 설정으로 둔다. 효과는 analyze 9절에서 종목별로 측정한다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BandSnap:
    state: str  # none | pending | active
    fresh: bool  # 확정 직후(fresh_bars 이내)이고 아직 활성
    width: float  # (fast - slow) / slow, 준비 전엔 0
    fast: float
    slow: float
    bars_since_start: int
    start_ts: datetime | None
    ready: bool


class BandTracker:
    def __init__(self, fast: int, slow: int, min_width: float = 0.001, confirm_bars: int = 3,
                 slow_tol: float = 0.002, fresh_bars: int = 2):
        if fast >= slow:
            raise ValueError("fast 는 slow 보다 짧아야 합니다")
        self.fast_n, self.slow_n = fast, slow
        self.min_width, self.confirm_bars = min_width, confirm_bars
        self.slow_tol, self.fresh_bars = slow_tol, fresh_bars
        self.closes: deque[float] = deque(maxlen=slow)
        self.state = "none"
        self.width = self.fast = self.slow = 0.0
        self.start_ts: datetime | None = None
        self.bars_since_start = 0
        self._prev_width: float | None = None
        self._prev_slow: float | None = None
        self._pending = 0

    @property
    def ready(self) -> bool:
        return len(self.closes) >= self.slow_n

    @property
    def fresh(self) -> bool:
        return self.state == "active" and self.bars_since_start <= self.fresh_bars

    def on_bar(self, ts, close: float) -> str | None:
        """완성된 봉 하나를 넣는다. 'start' | 'end' | None 반환."""
        self.closes.append(close)
        if not self.ready:
            return None
        cl = list(self.closes)
        fast = sum(cl[-self.fast_n :]) / self.fast_n
        slow = sum(cl) / self.slow_n
        width = fast / slow - 1
        ev = None
        if self.state == "none" and self._prev_width is not None and self._prev_width <= 0 < width:
            self.state, self._pending = "pending", 0
        if self.state == "pending":
            slope_ok = self._prev_slow is None or slow / self._prev_slow - 1 >= -self.slow_tol
            if width <= 0:
                self.state = "none"  # 가짜 시작
            elif width >= self.min_width and close > slow and slope_ok:
                self.state, self.start_ts, self.bars_since_start, ev = "active", ts, 0, "start"
            else:
                self._pending += 1
                if self._pending >= self.confirm_bars:
                    self.state = "none"
        elif self.state == "active":
            self.bars_since_start += 1
            if width <= 0:
                self.state, ev = "none", "end"
        self.width, self.fast, self.slow = width, fast, slow
        self._prev_width, self._prev_slow = width, slow
        return ev

    def snap(self) -> BandSnap:
        return BandSnap(self.state, self.fresh, self.width, self.fast, self.slow, self.bars_since_start, self.start_ts, self.ready)


class IntradayBand:
    """틱(또는 1분봉 종가)을 N분봉으로 묶어 BandTracker 에 넣는다. 날짜가 바뀌어도 이어서 계산(차트와 동일)."""

    def __init__(self, tf_min: int, fast: int, slow: int, min_width: float, confirm_bars: int, slow_tol: float, fresh_bars: int):
        self.tf = max(1, tf_min)
        self.tracker = BandTracker(fast, slow, min_width, confirm_bars, slow_tol, fresh_bars)
        self._key: datetime | None = None
        self._last = 0.0

    @classmethod
    def from_params(cls, p) -> "IntradayBand":
        return cls(p.band_tf_min, p.band_fast, p.band_slow, p.band_min_width, p.band_confirm_bars, p.band_slow_tol, p.band_fresh_bars)

    def _bucket(self, ts: datetime) -> datetime:
        return ts.replace(minute=(ts.minute // self.tf) * self.tf, second=0, microsecond=0)

    def on_price(self, ts: datetime, price: float) -> str | None:
        """가격 1건. 직전 N분봉이 막 완성되면 그 봉으로 계산한 이벤트('start'/'end')를 돌려준다."""
        key = self._bucket(ts)
        ev = None
        if self._key is not None and key != self._key:
            ev = self.tracker.on_bar(self._key, self._last)
        self._key, self._last = key, price
        return ev

    def snap(self) -> BandSnap:
        return self.tracker.snap()
