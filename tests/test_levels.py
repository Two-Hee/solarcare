from datetime import datetime, timedelta

import pytest

from daytrader.analysis import level_drift, levels_by_day, room_split, support_touch_signals, Rules
from daytrader.bars import Bar
from daytrader.config import Params
from daytrader.flow import FlowAnalyzer
from daytrader.levels import Level, LevelTracker, Levels
from daytrader.models import Tick
from daytrader.strategy import FlowStrategy
from tests.test_core import ctx, dip_snap, make_snap

P = Params(use_levels=True)


def t(h, m, s=0, day=0):
    return datetime(2026, 10, 15 + day, h, m, s)


def cluster(tr, px, n, vol=100, buy_frac=0.5, start=(10, 0), step=2):
    base = t(*start)
    for k in range(n):
        side = 1 if (k % 10) < buy_frac * 10 else -1
        tr.add_tick(base + timedelta(seconds=k * step), px, vol, side)


def noise(tr, lo=10010, hi=10490, step=10, vol=10, start=(9, 30)):
    k = 0
    for px in range(lo, hi + 1, step):
        tr.add_tick(t(*start) + timedelta(seconds=k), px, vol, 1 if k % 2 else -1)
        k += 1


def near(px, target, tol=0.0016):
    return abs(px / target - 1) < tol


def test_volume_clusters_become_support_and_resistance():
    tr = LevelTracker(P)
    noise(tr)
    cluster(tr, 10000, 50)
    cluster(tr, 10500, 20, start=(10, 5))
    lv = tr.snapshot(10250, t(10, 10))
    assert near(lv.supports[0].price, 10000) and near(lv.resistances[0].price, 10500)
    assert lv.supports[0].strength > lv.resistances[0].strength > 0
    assert lv.nearest_support(min_strength=0.04) is lv.supports[0]
    assert lv.nearest_resistance(min_strength=0.99) is None


def test_levels_move_when_flow_changes():
    tr = LevelTracker(P)
    noise(tr)
    cluster(tr, 10000, 50)
    cluster(tr, 10500, 20, start=(10, 5))
    before = tr.snapshot(10250, t(10, 10))
    cluster(tr, 10300, 200, vol=200, start=(10, 12))  # 새 체결 밀집 -> 가장 가까운 저항이 바뀐다
    after = tr.snapshot(10250, t(10, 20))
    assert near(before.resistances[0].price, 10500) and near(after.resistances[0].price, 10300)
    assert after.resistances[0].price < before.resistances[0].price


def test_old_volume_decays_with_trading_time_but_not_overnight():
    tr = LevelTracker(P)
    cluster(tr, 10000, 100, vol=100)
    for k in range(0, 240):  # 4시간 동안 다른 가격에서 얇게 체결
        px = 10200 + (k % 20) * 10
        tr.add_tick(t(10, 5) + timedelta(minutes=k), px, 10, 1)
    later = tr.snapshot(10200, t(14, 5))
    early_tr = LevelTracker(P)
    cluster(early_tr, 10000, 100, vol=100)
    for k in range(0, 5):
        early_tr.add_tick(t(10, 5) + timedelta(minutes=k), 10200 + k * 10, 10, 1)
    early = early_tr.snapshot(10200, t(10, 10))
    assert early.supports and (not later.supports or later.supports[0].share < early.supports[0].share)

    # 장이 닫힌 밤: 시계가 늘지 않아 약해지지 않는다
    tr2 = LevelTracker(P)
    cluster(tr2, 10000, 50, start=(15, 0))
    clk = tr2._clk
    tr2.add_tick(t(9, 0, day=1), 10100, 10, 1)  # 다음 날 첫 체결
    assert tr2._clk == clk
    assert near(tr2.snapshot(10100, t(9, 1, day=1)).supports[0].price, 10000)


def test_absorption_boosts_strength_and_delta_sign():
    sell_heavy, neutral = LevelTracker(P), LevelTracker(P)
    for tr, bf in ((sell_heavy, 0.2), (neutral, 0.5)):
        noise(tr)
        cluster(tr, 10000, 50, buy_frac=bf)
    a, b = sell_heavy.snapshot(10250, t(10, 10)).supports[0], neutral.snapshot(10250, t(10, 10)).supports[0]
    assert a.delta == pytest.approx(-0.6, abs=0.1) and abs(b.delta) < 0.1
    assert a.strength > b.strength  # 팔아도 가격이 그 위에 있다 = 흡수 -> 지지 강화
    # 저항: 매수가 우세했는데 가격이 그 아래
    buy_heavy, neutral2 = LevelTracker(P), LevelTracker(P)
    for tr, bf in ((buy_heavy, 0.8), (neutral2, 0.5)):
        noise(tr, lo=10010, hi=10300)
        cluster(tr, 10500, 50, buy_frac=bf)
    assert buy_heavy.snapshot(10250, t(10, 10)).resistances[0].strength > neutral2.snapshot(10250, t(10, 10)).resistances[0].strength


def test_anchored_vwap_starts_at_largest_volume_minute():
    tr = LevelTracker(P)
    tr.add_tick(t(9, 0), 10000, 10, 1)
    tr.add_tick(t(9, 30), 10200, 1000, 1)  # 그날 최대 거래량 분 = 앵커
    tr.add_tick(t(9, 31), 10300, 500, 1)
    tr.add_tick(t(10, 0), 10100, 500, -1)
    expected = (10200 * 1000 + 10300 * 500 + 10100 * 500) / 2000
    assert tr.snapshot(10100, t(10, 1)).avwap == pytest.approx(expected)
    tr.add_tick(t(10, 30), 10150, 5000, 1)  # 더 큰 분이 나오면 앵커가 옮겨간다
    assert tr.snapshot(10150, t(10, 31)).avwap == pytest.approx(10150, rel=1e-6)


def test_bars_spread_volume_and_infer_direction():
    tr = LevelTracker(P)
    for k in range(20):  # 10,000~10,030 에서 거래 많고 저가 마감(매도 우세)
        tr.add_bar(t(10, k), 10000, 10030, 10000, 5000)
    for k in range(20):
        tr.add_bar(t(10, 20 + k), 10300, 10330, 10330, 3000)  # 첫 밀집대의 60% (10배 약한 밀집대는 노드로 보지 않는다)
    lv = tr.snapshot(10150, t(10, 40))
    assert 10000 * 0.998 < lv.supports[0].price < 10030 * 1.002 and lv.supports[0].delta < -0.5
    assert lv.resistances and lv.resistances[0].price > 10290
    assert lv.supports[0].strength > lv.resistances[0].strength


def test_overwhelmed_tiny_cluster_is_not_a_node():
    tr = LevelTracker(P)
    for k in range(20):
        tr.add_bar(t(10, k), 10000, 10030, 10000, 5000)
    for k in range(20):
        tr.add_bar(t(10, 20 + k), 10300, 10330, 10330, 300)
    assert tr.snapshot(10150, t(10, 40)).resistances == ()


def test_empty_and_edge_inputs():
    tr = LevelTracker(P)
    assert tr.snapshot(10000) == Levels()
    tr.add_tick(t(10, 0), 0, 5, 1)
    tr.add_tick(t(10, 0), 10000, 0, 1)
    assert tr.snapshot(10000) == Levels()
    tr.add_bar(t(10, 0), 0, 0, 0, 10)
    assert tr.snapshot(10000, t(10, 1)).supports == ()


# ------------------------------------------------------------------ 전략/분석기 연동
def lv(res=(), sup=()):
    return Levels(supports=tuple(sup), resistances=tuple(res))


def test_room_filter_blocks_entry_just_below_strong_resistance():
    on = FlowStrategy(Params(use_levels=True))
    off = FlowStrategy(Params())
    strong_close = lv(res=[Level(70300, 0.10, 0.08, 0.2)])  # +0.43% < 0.6 x 1.3%
    assert on.decide(ctx(make_snap(lv=strong_close))).kind == "hold"
    assert off.decide(ctx(make_snap(lv=strong_close))).kind == "buy"  # 꺼져 있으면 영향 없음
    far = lv(res=[Level(71500, 0.10, 0.08, 0.2)])
    weak = lv(res=[Level(70300, 0.01, 0.01, 0.2)])
    for snap in (make_snap(lv=far), make_snap(lv=weak), make_snap(lv=lv()), make_snap(lv=None)):
        assert on.decide(ctx(snap)).kind == "buy"


def test_dip_buy_requires_support_node_near_the_low_and_uses_it_for_invalidation():
    on = FlowStrategy(Params(use_levels=True))
    at_low = lv(sup=[Level(70010, 0.10, 0.08, -0.4)])
    a = on.decide(ctx(dip_snap(lv=at_low)))
    assert a.reason == "dip_buy" and a.invalid_px == pytest.approx(70000 * 0.997)
    below = lv(sup=[Level(69900, 0.10, 0.08, -0.4)])  # 저점 아래 0.14% 의 노드 -> 손절선을 그 아래로
    assert on.decide(ctx(dip_snap(lv=below))).invalid_px == pytest.approx(69900 * 0.997)
    far = lv(sup=[Level(69000, 0.10, 0.08, -0.4)])
    assert on.decide(ctx(dip_snap(lv=far))).kind == "hold"  # 저점이 수급 지지와 무관 -> 진입 안 함
    assert on.decide(ctx(dip_snap(lv=lv()))).kind == "hold"
    assert FlowStrategy(Params()).decide(ctx(dip_snap(lv=far))).kind == "buy"


def test_analyzer_populates_levels_only_when_enabled():
    ticks = [Tick(t(10, 0, k), "A", 10000 + (k % 3) * 10, 100, 1 if k % 2 else -1) for k in range(60)]
    on, off = FlowAnalyzer("A", Params(use_levels=True)), FlowAnalyzer("A", Params())
    for tk in ticks:
        on.on_tick(tk)
        off.on_tick(tk)
    assert off.snapshot(t(10, 1)).lv is None
    snap = on.snapshot(t(10, 1))
    assert snap.lv is not None and snap.lv.avwap > 0


# ------------------------------------------------------------------ 분석 연동
def bars_day(day, prices, vol=1000):
    out, prev = [], prices[0]
    for k, px in enumerate(prices):
        out.append(Bar(t(9, 0, day=day) + timedelta(minutes=k), prev, max(prev, px) + 5, min(prev, px) - 5, px, vol))
        prev = px
    return out


def test_levels_by_day_has_no_lookahead_and_persists_across_days():
    d0 = bars_day(0, [10000] * 40 + [10300] * 40, vol=1000)
    d1 = bars_day(1, [10300] * 60, vol=10)
    days = {"20261015": d0, "20261016": d1}
    lvs = levels_by_day(days, P)
    assert len(lvs["20261015"]) == len(d0) and len(lvs["20261016"]) == len(d1)
    # 첫 봉 시점에는 첫 봉의 정보만 반영 -> 10,300 대의 거래는 아직 모른다
    assert all(x.price < 10100 for x in lvs["20261015"][0].supports + lvs["20261015"][0].resistances)
    # 둘째 날에도 첫째 날 노드(10,000 대)가 지지로 남아 있다
    assert any(near(x.price, 10000, 0.004) for x in lvs["20261016"][-1].supports)


def test_support_touch_signal_room_split_and_drift():
    p = Params(use_levels=True, lv_min_strength=0.02)
    day = bars_day(0, [10000] * 60 + [10080] * 30 + [10020] * 10 + [10300] * 40, vol=500)
    days = {"20261015": day}
    lvs = levels_by_day(days, p)
    sig = support_touch_signals(lvs["20261015"], p)(day)
    assert all(day[i].ts.hour >= 9 for i in sig)
    rs = room_split(days, lvs, Rules(tp=0.013, sl=0.01), p)
    base_n = sum(1 for b in day[:-1] if (b.ts.hour, b.ts.minute) >= (9, 5) and (b.ts.hour, b.ts.minute) < (14, 40))
    assert rs["n_room"] + rs["n_blocked"] == base_n
    d = level_drift(lvs, minutes=10)
    assert d["support_median"] >= 0 and d["resistance_median"] >= 0
