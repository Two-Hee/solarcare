"""체결 흐름(수급) 분석기: 10초 / 1분 / 5분 창.

'스마트머니'는 공개되지 않으므로 아래 신호로 추정한다.
  - delta_ratio : 매수체결량 - 매도체결량 (CVD 의 창 단위 버전), -1~+1
  - big_delta   : 큰 체결(종목별 상위 분위 + 최소 금액)만의 순매수 비율
  - ob_imb      : 10호가 총잔량 불균형 (매수잔량 - 매도잔량)
  - ret         : 창 내 가격 변화
  - absorption  : 한쪽이 대량으로 때리는데 가격이 안 밀리는 현상(흡수)
각 창의 점수는 -100~+100 이고, 거래가 평소의 절반도 안 되는 창은 신뢰도를 깎는다.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime

from .config import Params
from .costs import tick_size
from .models import Quote, Tick

WINDOWS = {"s10": 10, "s60": 60, "s300": 300}
_RET_SCALE = {"s10": 0.003, "s60": 0.006, "s300": 0.012}


@dataclass(frozen=True)
class WindowStats:
    n: int = 0
    vol: int = 0
    buy_vol: int = 0
    sell_vol: int = 0
    delta_ratio: float = 0.0
    strength: float = 100.0  # 체결강도 = 매수체결량/매도체결량*100
    big_n: int = 0
    big_delta_ratio: float = 0.0
    ret: float = 0.0


class _Window:
    def __init__(self, seconds: int):
        self.sec = seconds
        self.q: deque[tuple[datetime, int, int, int, bool]] = deque()
        self.vol = self.buy = self.sell = 0
        self.bbuy = self.bsell = self.nbig = 0

    def _acc(self, vol: int, side: int, big: bool, sign: int) -> None:
        self.vol += sign * vol
        if side > 0:
            self.buy += sign * vol
        elif side < 0:
            self.sell += sign * vol
        if big:
            self.nbig += sign
            if side > 0:
                self.bbuy += sign * vol
            elif side < 0:
                self.bsell += sign * vol

    def add(self, ts: datetime, price: int, vol: int, side: int, big: bool) -> None:
        self.q.append((ts, price, vol, side, big))
        self._acc(vol, side, big, +1)

    def trim(self, now: datetime) -> None:
        q = self.q
        while q and (now - q[0][0]).total_seconds() > self.sec:
            _, _, v, s, b = q.popleft()
            self._acc(v, s, b, -1)

    def stats(self) -> WindowStats:
        n = len(self.q)
        if n == 0:
            return WindowStats()
        cls = self.buy + self.sell
        bcls = self.bbuy + self.bsell
        first, last = self.q[0][1], self.q[-1][1]
        return WindowStats(
            n=n,
            vol=self.vol,
            buy_vol=self.buy,
            sell_vol=self.sell,
            delta_ratio=(self.buy - self.sell) / cls if cls else 0.0,
            strength=min(999.0, self.buy / self.sell * 100) if self.sell else (999.0 if self.buy else 100.0),
            big_n=self.nbig,
            big_delta_ratio=(self.bbuy - self.bsell) / bcls if bcls else 0.0,
            ret=(last / first - 1) if first else 0.0,
        )


@dataclass(frozen=True)
class FlowSnapshot:
    ts: datetime
    price: int
    ask1: int
    bid1: int
    vwap: float
    cvd: int  # 장 시작 후 누적 순매수 체결량
    ob_imb: float
    top_imb: float
    spread_ticks: float
    s10: WindowStats
    s60: WindowStats
    s300: WindowStats
    score10: float
    score60: float
    score300: float

    @property
    def vwap_dev(self) -> float:
        return (self.price / self.vwap - 1) if self.vwap else 0.0

    def as_features(self) -> dict:
        return {
            "score10": round(self.score10, 1),
            "score60": round(self.score60, 1),
            "score300": round(self.score300, 1),
            "delta10": round(self.s10.delta_ratio, 3),
            "delta60": round(self.s60.delta_ratio, 3),
            "big60": round(self.s60.big_delta_ratio, 3),
            "strength60": round(self.s60.strength, 1),
            "ob_imb": round(self.ob_imb, 3),
            "vwap_dev": round(self.vwap_dev, 4),
            "ret300": round(self.s300.ret, 4),
            "trades60": self.s60.n,
        }


class FlowAnalyzer:
    def __init__(self, code: str, params: Params):
        self.code = code
        self.p = params
        self.win = {k: _Window(v) for k, v in WINDOWS.items()}
        self.day: date | None = None
        self.last_quote: Quote | None = None
        self.last_price = 0
        self.last_side = 0
        self.last_ts: datetime | None = None
        self.first_ts: datetime | None = None
        self.cvd = 0
        self.sess_vol = 0
        self.sess_notional = 0.0
        self._hist: deque[float] = deque(maxlen=2000)
        self._n = 0
        self.big_thr = float(params.big_trade_floor_krw)

    # --- 입력
    def _reset_day(self, d: date) -> None:
        self.day = d
        self.win = {k: _Window(v) for k, v in WINDOWS.items()}
        self.cvd = self.sess_vol = 0
        self.sess_notional = 0.0
        self.first_ts = None
        self.last_price = 0
        self.last_side = 0
        self.last_quote = None

    def on_quote(self, q: Quote) -> None:
        if self.day != q.ts.date():
            self._reset_day(q.ts.date())
        self.last_quote = q

    def _side(self, t: Tick) -> int:
        src = self.p.side_source
        if src == "feed" and t.side:
            return t.side
        if src == "quote":
            if t.ask1 and t.price >= t.ask1:
                return 1
            if t.bid1 and t.price <= t.bid1:
                return -1
        if self.last_price:  # tick rule
            if t.price > self.last_price:
                return 1
            if t.price < self.last_price:
                return -1
            return self.last_side
        return 0

    def on_tick(self, t: Tick) -> None:
        if self.day != t.ts.date():
            self._reset_day(t.ts.date())
        side = self._side(t)
        notional = float(t.price) * t.vol
        big = notional >= self.big_thr
        self._hist.append(notional)
        self._n += 1
        if self._n % 100 == 0 and len(self._hist) >= 200:
            s = sorted(self._hist)
            q = s[min(len(s) - 1, int(len(s) * self.p.big_trade_quantile))]
            self.big_thr = max(float(self.p.big_trade_floor_krw), q)
        for w in self.win.values():
            w.add(t.ts, t.price, t.vol, side, big)
            w.trim(t.ts)
        self.cvd += side * t.vol
        self.sess_vol += t.vol
        self.sess_notional += notional
        if self.first_ts is None:
            self.first_ts = t.ts
        self.last_price = t.price
        self.last_ts = t.ts
        if side:
            self.last_side = side

    # --- 출력
    def _expected_vol(self, now: datetime, seconds: int) -> float:
        if self.first_ts is None or self.sess_vol == 0:
            return 0.0
        elapsed = max(120.0, (now - self.first_ts).total_seconds())
        return self.sess_vol / elapsed * seconds

    def _score(self, key: str, st: WindowStats, ob_imb: float, now: datetime) -> float:
        if st.vol == 0:
            return 0.0
        p = self.p
        expected = self._expected_vol(now, WINDOWS[key])
        gate = min(1.0, st.vol / (0.5 * expected)) if expected > 0 else 0.5
        c_delta = st.delta_ratio
        c_big = st.big_delta_ratio * min(1.0, st.big_n / 3)
        c_ret = math.tanh(st.ret / _RET_SCALE[key])
        c_abs = 0.0
        if st.delta_ratio <= -0.3 and st.ret >= -0.0005:
            c_abs = min(1.0, -st.delta_ratio)  # 팔아도 안 밀림 -> 흡수(강세)
        elif st.delta_ratio >= 0.3 and st.ret <= 0.0005:
            c_abs = -min(1.0, st.delta_ratio)  # 사도 안 오름 -> 분배(약세)
        raw = (
            p.w_delta * c_delta
            + p.w_big * c_big
            + p.w_ob * ob_imb
            + p.w_ret * c_ret
            + p.w_absorb * c_abs
        )
        return 100.0 * gate * raw

    def snapshot(self, now: datetime) -> FlowSnapshot:
        for w in self.win.values():
            w.trim(now)
        q = self.last_quote
        ob_imb = top_imb = spread_ticks = 0.0
        ask1 = bid1 = 0
        if q:
            ask1, bid1 = q.ask1, q.bid1
            tot = q.total_ask + q.total_bid
            if tot > 0:
                ob_imb = (q.total_bid - q.total_ask) / tot
            top = q.ask_rem1 + q.bid_rem1
            if top > 0:
                top_imb = (q.bid_rem1 - q.ask_rem1) / top
            if ask1 and bid1 and ask1 >= bid1:
                spread_ticks = (ask1 - bid1) / tick_size(bid1)
        stats = {k: w.stats() for k, w in self.win.items()}
        scores = {k: self._score(k, stats[k], ob_imb, now) for k in stats}
        vwap = self.sess_notional / self.sess_vol if self.sess_vol else 0.0
        return FlowSnapshot(
            ts=now,
            price=self.last_price,
            ask1=ask1,
            bid1=bid1,
            vwap=vwap,
            cvd=self.cvd,
            ob_imb=ob_imb,
            top_imb=top_imb,
            spread_ticks=spread_ticks,
            s10=stats["s10"],
            s60=stats["s60"],
            s300=stats["s300"],
            score10=scores["s10"],
            score60=scores["s60"],
            score300=scores["s300"],
        )
