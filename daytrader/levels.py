"""수급 기반 동적 지지·저항.

고정된 가로선이 아니라 '체결량이 실제로 많이 쌓인 가격대'를 계속 다시 계산한다.
  - 가격을 같은 % 폭(기본 0.15%)의 버킷으로 나누고, 버킷마다 매수체결량/매도체결량을 누적한다.
  - 오래된 거래량은 감쇠한다(반감기는 *체결이 있는 시간* 기준이라 장이 닫힌 밤에는 약해지지 않는다).
  - 이웃 버킷과 평활한 뒤 국소 최대 + 가격 범위 평균 버킷(빈 버킷 포함)의 N배 이상이면 '거래량 노드'로 본다.
  - 현재가 아래 노드는 지지, 위 노드는 저항. 강도 = 거래량 비중 x (1 + 0.5 x 흡수)
    흡수: 지지 노드에서 매도 체결이 우세했는데도 가격이 그 위에 있음 / 저항 노드에서 매수 체결이 우세했는데도 가격이 그 아래에 있음
  - 별도로 '그날 거래량이 가장 컸던 분봉부터의 평균가(anchored VWAP)' 를 낸다.
이 신호가 실제로 예측력이 있는지는 검증 전이다. analyze 리포트 8절로 종목별로 확인할 것.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .config import Params


@dataclass(frozen=True)
class Level:
    price: float
    strength: float
    share: float  # 감쇠 반영 거래량 비중(0~1)
    delta: float  # (매수-매도)/(매수+매도) at node, -1~+1


@dataclass(frozen=True)
class Levels:
    supports: tuple[Level, ...] = ()  # 현재가 아래, 가까운 순
    resistances: tuple[Level, ...] = ()  # 현재가 위, 가까운 순
    avwap: float = 0.0

    def nearest_support(self, min_strength: float = 0.0) -> Level | None:
        return next((x for x in self.supports if x.strength >= min_strength), None)

    def nearest_resistance(self, min_strength: float = 0.0) -> Level | None:
        return next((x for x in self.resistances if x.strength >= min_strength), None)


class LevelTracker:
    def __init__(self, p: Params):
        self.p = p
        self._ln = math.log1p(p.lv_bucket_pct)
        self._idx_cache: dict[float, int] = {}
        self._b: dict[int, list[float]] = {}  # idx -> [buy, sell, 마지막 갱신 시계]
        self._clk = 0.0  # 체결 시간 시계(분). 날짜가 바뀌어도 늘지 않는다
        self._last: datetime | None = None
        self._day = None
        self._minutes: dict[datetime, list[float]] = {}  # 분 -> [가격x거래량 합, 거래량 합] (그날)
        self._anchor: datetime | None = None  # 그날 거래량이 가장 큰 분
        self._asum = [0.0, 0.0]  # 앵커 분부터의 [가격x거래량, 거래량] 누적 -> avwap 을 O(1) 로
        self._cache: tuple[datetime, list] | None = None  # (계산 시각, 노드) - lv_refresh_sec 동안 재사용

    # --- 내부
    def _idx(self, price: float) -> int:
        i = self._idx_cache.get(price)
        if i is None:
            i = self._idx_cache[price] = int(round(math.log(price) / self._ln))
        return i

    def _px(self, idx: int) -> float:
        return math.exp(idx * self._ln)

    def _advance(self, ts: datetime) -> None:
        if self._day != ts.date():
            self._day = ts.date()
            self._minutes = {}
            self._anchor = None
            self._asum = [0.0, 0.0]
        elif self._last is not None and ts > self._last:
            self._clk += (ts - self._last).total_seconds() / 60.0
        if self._last is None or ts > self._last or self._last.date() != ts.date():
            self._last = ts

    def _decay(self, rec: list[float]) -> float:
        return 0.5 ** ((self._clk - rec[2]) / self.p.lv_halflife_min)

    def _put(self, idx: int, buy: float, sell: float) -> None:
        rec = self._b.get(idx)
        if rec is None:
            rec = self._b[idx] = [0.0, 0.0, self._clk]
        f = self._decay(rec)
        rec[0] = rec[0] * f + buy
        rec[1] = rec[1] * f + sell
        rec[2] = self._clk

    def _minute(self, ts: datetime, price: float, vol: float) -> None:
        key = ts.replace(second=0, microsecond=0)
        m = self._minutes.setdefault(key, [0.0, 0.0])
        m[0] += price * vol
        m[1] += vol
        if self._anchor is None or (key != self._anchor and m[1] > self._minutes[self._anchor][1]):
            self._anchor = key  # 더 큰 분이 나오면 앵커 이동(그 분 이후 거래는 아직 없다)
            self._asum = [m[0], m[1]]
        elif key >= self._anchor:
            self._asum[0] += price * vol
            self._asum[1] += vol

    # --- 입력
    def add_tick(self, ts: datetime, price: int, vol: int, side: int) -> None:
        if price <= 0 or vol <= 0:
            return
        self._advance(ts)
        buy, sell = (vol, 0) if side > 0 else (0, vol) if side < 0 else (vol / 2, vol / 2)
        self._put(self._idx(price), buy, sell)
        self._minute(ts, price, vol)

    def add_bar(self, ts: datetime, low: int, high: int, close: int, volume: int, buy_vol: int = 0, sell_vol: int = 0) -> None:
        """봉 거래량을 [저가, 고가] 버킷에 고르게 퍼뜨린다. 체결 방향이 없으면 종가 위치로 매수/매도를 추정."""
        if volume <= 0 or low <= 0:
            return
        self._advance(ts)
        if buy_vol + sell_vol > 0:
            frac_buy = buy_vol / (buy_vol + sell_vol)
        else:
            rng = high - low
            frac_buy = 0.5 + 0.5 * (((close - low) - (high - close)) / rng if rng > 0 else 0.0)
        lo, hi = self._idx(low), self._idx(high)
        n = hi - lo + 1
        for i in range(lo, hi + 1):
            self._put(i, volume * frac_buy / n, volume * (1 - frac_buy) / n)
        self._minute(ts, (high + low + close) / 3, volume)

    # --- 출력
    def _compute_nodes(self) -> list[tuple[float, float, float]]:
        """(가격, 거래량 비중, 매수-매도 비율) 노드 목록. 현재가와 무관하므로 잠깐 캐시해도 된다."""
        if not self._b:
            return []
        buys, sells = {}, {}
        for i, rec in self._b.items():
            f = self._decay(rec)
            buys[i], sells[i] = rec[0] * f, rec[1] * f
        vol = {i: buys[i] + sells[i] for i in buys}
        tot = sum(vol.values())
        if tot <= 0:
            return []
        lo_i, hi_i = min(vol), max(vol)
        sm = {i: 0.5 * vol.get(i, 0.0) + 0.25 * (vol.get(i - 1, 0.0) + vol.get(i + 1, 0.0)) for i in range(lo_i - 1, hi_i + 2)}
        base = sum(sm.values()) / len(sm)  # 빈 버킷을 포함한 평균 -> 데이터가 적어도 기준이 노드 크기에 끌려가지 않는다
        nodes: list[tuple[float, float, float]] = []
        prev_peak = None
        for i in sorted(sm):
            v = sm[i]
            if v < self.p.lv_hvn_mult * base or v < max(sm.get(i + k, 0.0) for k in (-2, -1, 1, 2)):
                continue
            if prev_peak is not None and i - prev_peak <= 2 and nodes and v <= nodes[-1][1] * tot:
                continue  # 이웃한 같은 봉우리(동률/근접)는 하나로
            b3 = sum(buys.get(i + k, 0.0) for k in (-1, 0, 1))
            s3 = sum(sells.get(i + k, 0.0) for k in (-1, 0, 1))
            nodes.append((self._px(i), v / tot, (b3 - s3) / (b3 + s3) if b3 + s3 > 0 else 0.0))
            prev_peak = i
        return nodes

    def snapshot(self, price: float, now: datetime | None = None) -> Levels:
        if now is not None:
            self._advance(now)
        if not self._b or price <= 0:
            return Levels()
        at = self._last
        c = self._cache
        if c is None or at is None or not (0 <= (at - c[0]).total_seconds() < self.p.lv_refresh_sec):
            self._cache = c = (at, self._compute_nodes())
        nodes = []
        for px, share, delta in c[1]:
            below = px <= price
            absorb = max(0.0, -delta) if below else max(0.0, delta)
            nodes.append(Level(px, share * (1 + 0.5 * absorb), share, delta))
        sup = sorted((x for x in nodes if x.price <= price), key=lambda x: price - x.price)[: self.p.lv_max_levels]
        res = sorted((x for x in nodes if x.price > price), key=lambda x: x.price - price)[: self.p.lv_max_levels]
        return Levels(tuple(sup), tuple(res), self._avwap())

    def _avwap(self) -> float:
        """그날 거래량이 가장 컸던 분부터의 평균가."""
        return self._asum[0] / self._asum[1] if self._asum[1] > 0 else 0.0
