import logging
from datetime import datetime, timedelta

import pytest

from daytrader.analysis import simulate_exit, Rules, squeeze_signals_by_day, squeeze_study
from daytrader.bars import Bar
from daytrader.broker.paper import PaperBroker
from daytrader.config import Params
from daytrader.costs import CostModel
from daytrader.engine import TradingEngine
from daytrader.flow import FlowAnalyzer
from daytrader.models import Position, Tick
from daytrader.squeeze import SqueezeSnap, SqueezeTracker, stop_price, structural_risk
from daytrader.strategy import FlowStrategy
from tests.test_core import at, ctx, dip_snap, make_snap

D = datetime(2026, 10, 15)


def t(h, m, s=0, day=0):
    return (D + timedelta(days=day)).replace(hour=h, minute=m, second=s)


# ------------------------------------------------------------------ SqueezeTracker
def feed(tr, n_vol, n_quiet, day=0, start=(9, 0), vol_v=100, vol_q=100, hi_lo_v=0.005, hi_lo_q=0.001, px=10000):
    k = 0
    for _ in range(n_vol):
        tr.add_bar(t(start[0], start[1], day=day) + timedelta(minutes=k), round(px * (1 + hi_lo_v)), round(px * (1 - hi_lo_v)), px, vol_v)
        k += 1
    for _ in range(n_quiet):
        tr.add_bar(t(start[0], start[1], day=day) + timedelta(minutes=k), round(px * (1 + hi_lo_q)), round(px * (1 - hi_lo_q)), px, vol_q)
        k += 1


def test_tracker_ready_compressed_and_box_values():
    tr = SqueezeTracker(base=20, ref=60, ratio_max=0.7)
    feed(tr, 59, 20)
    assert not tr.snap().ready  # 79 개 < 80
    tr = SqueezeTracker(20, 60, 0.7)
    feed(tr, 60, 20)
    s = tr.snap()
    assert s.ready and s.compressed and s.ratio == pytest.approx(0.2, rel=0.05)
    assert (s.box_hi, s.box_lo) == (10010, 9990) and s.vol_med == 100
    assert s.box_start == t(10, 0) and s.box_end == t(10, 19)
    uniform = SqueezeTracker(20, 60, 0.7)
    feed(uniform, 60, 20, hi_lo_q=0.005)  # 최근에도 똑같이 출렁임
    assert uniform.snap().ready and not uniform.snap().compressed


def test_tracker_rejects_box_that_spans_days_and_bad_bars():
    tr = SqueezeTracker(20, 60, 0.7)
    feed(tr, 60, 10, day=0, start=(14, 0))
    feed(tr, 0, 10, day=1, start=(9, 0))  # 박스가 어제 오후 + 오늘 아침(갭)에 걸침
    assert not tr.snap().ready
    tr2 = SqueezeTracker(2, 3, 0.7)
    tr2.add_bar(t(9, 0), 100, 101, 100, 5)  # high < low
    tr2.add_bar(t(9, 1), 100, 99, 0, 5)  # close 0
    assert len(tr2.bars) == 0


def test_stop_price_and_structural_risk():
    assert stop_price(10010, 9990, 1.0, 0.0) == 9990 and stop_price(10010, 9990, 0.5, 0.0) == 10000
    assert stop_price(10010, 9990, 0.5, 0.001) == pytest.approx(10000 * 0.999)
    assert structural_risk(10100, 9999.0) == pytest.approx(101 / 10100)


# ------------------------------------------------------------------ 전략 (기준 판정)
def qsnap(**over):
    base = dict(ready=True, compressed=True, ratio=0.5, box_hi=70000, box_lo=69600, box_start=at(9, 40), box_end=at(9, 59), vol_med=1000.0)
    base.update(over)
    return SqueezeSnap(**base)


SQ = Params(entry_mode="squeeze")


def squeeze_ctx(q=None, **over):
    snap_over = dict(price=70100, sq=q or qsnap())
    snap_over.update(over.pop("snap", {}))
    return ctx(make_snap(**snap_over), **over)


def test_squeeze_entry_when_every_condition_holds():
    a = FlowStrategy(SQ).decide(squeeze_ctx())
    stop = (70000 - 0.5 * 400) * 0.999
    risk = (70100 - stop) / 70100
    assert a.kind == "buy" and a.reason == "squeeze_break" and a.mode == "squeeze"
    assert a.invalid_px == pytest.approx(stop) and a.sl_pct == pytest.approx(risk * 1.25 + 0.001)  # 비율 손절은 구조적 손절보다 여유 있게
    assert a.frac == pytest.approx(0.45) and a.tp_pct == 0.013  # 손절이 가까워 위험 기반 수량이 상한(45%)을 넘으므로 상한


@pytest.mark.parametrize("what,kw", [
    ("압축 아님", dict(q=qsnap(compressed=False))),
    ("준비 전", dict(q=qsnap(ready=False))),
    ("박스 돌파 전", dict(snap=dict(price=70010))),
    ("이미 늦음(박스 고점 +0.6% 초과)", dict(snap=dict(price=70500))),
    ("거래량 확장 없음", dict(snap=dict(s60=type(make_snap().s60)(n=40, vol=1500, buy_vol=1200, sell_vol=300, delta_ratio=0.6)))),
    ("매수 우위 없음", dict(snap=dict(s60=type(make_snap().s60)(n=40, vol=5000, buy_vol=2500, sell_vol=2400, delta_ratio=0.02)))),
    ("10초 매도 우위", dict(snap=dict(score10=-5.0))),
    ("체결 수 부족", dict(snap=dict(s60=type(make_snap().s60)(n=5, vol=5000, buy_vol=4000, sell_vol=1000, delta_ratio=0.6)))),
    ("VWAP 한참 아래", dict(snap=dict(vwap=72000.0))),
    ("박스가 넓어 손익비 미달", dict(q=qsnap(box_lo=68000))),
    ("박스가 너무 좁음", dict(q=qsnap(box_lo=69999))),
])
def test_squeeze_entry_rejections(what, kw):
    assert FlowStrategy(SQ).decide(squeeze_ctx(**kw)).kind == "hold", what


def test_squeeze_does_not_reenter_same_box_but_takes_a_new_one():
    st = FlowStrategy(SQ)
    assert st.decide(squeeze_ctx(last_exit_ts=at(9, 50))).kind == "hold"  # 박스(9:40~)와 겹치는 구간에서 이미 청산함
    assert st.decide(squeeze_ctx(q=qsnap(box_start=at(9, 55)), last_exit_ts=at(9, 50))).kind == "buy"  # 청산 이후에 새로 만들어진 박스


def test_squeeze_mode_is_a_single_criterion():
    st = FlowStrategy(SQ)
    assert st.decide(ctx(make_snap(sq=None))).kind == "hold"  # 수급 점수가 강해도 박스가 없으면 진입 안 함
    assert st.decide(ctx(dip_snap(sq=None))).kind == "hold"  # 저점매수 경로도 닫힘
    assert FlowStrategy(Params()).decide(squeeze_ctx()).reason == "flow_long"  # 기본(flow) 모드는 박스를 무시하고 기존대로


def test_squeeze_ignores_volatility_floor_gate():
    quiet = Params(entry_mode="squeeze", min_atr1m=0.01)  # 일반 모드라면 '너무 조용'으로 막힐 설정
    assert FlowStrategy(quiet).decide(squeeze_ctx(snap=dict(atr1m=0.0005))).reason == "squeeze_break"
    assert FlowStrategy(Params(min_atr1m=0.01)).decide(ctx(make_snap(atr1m=0.0005))).kind == "hold"


def test_squeeze_exits():
    st = FlowStrategy(SQ)
    stop = 69800.0 * 0.999
    pos = Position("A", 10, 70100.0, at(10, 0), peak=70100.0, mode="squeeze", invalid_px=stop, sl_pct=0.0053, tp_pct=0.013)
    snap = lambda **o: make_snap(sq=qsnap(), **o)  # noqa: E731
    assert st.decide(ctx(snap(price=int(stop) - 10), pos, now=at(10, 5))).reason == "box_break"
    assert st.decide(ctx(snap(price=70100 + 1000), pos, now=at(10, 5))).reason == "take_profit"
    weak = snap(price=70050, score60=-60.0, score10=-20.0)
    assert st.decide(ctx(weak, pos, now=at(10, 5))).kind == "hold"  # 수급 반전 청산은 쓰지 않는다(진입 근거가 박스)
    dip_pos = Position("A", 10, 70100.0, at(10, 0), peak=70100.0, mode="dip", invalid_px=stop)
    assert st.decide(ctx(snap(price=int(stop) - 10), dip_pos, now=at(10, 5))).reason == "dip_low_break"  # 이름은 모드별


def test_analyzer_builds_squeeze_only_when_needed():
    ticks = [Tick(t(9, 0) + timedelta(minutes=k // 2, seconds=(k % 2) * 30), "A", 10000 + (k % 5), 10, 1) for k in range(260)]
    on, off = FlowAnalyzer("A", Params(entry_mode="squeeze")), FlowAnalyzer("A", Params())
    for tk in ticks:
        on.on_tick(tk)
        off.on_tick(tk)
    assert off.snapshot(t(11, 15)).sq is None
    s = on.snapshot(t(11, 15)).sq
    assert s is not None and s.ready and s.box_hi >= s.box_lo


# ------------------------------------------------------------------ 분석(룩어헤드 없음 / 구성요소 기여도 / 구조적 손절)
def sq_day(breakout_vol=500, breakout_close=10060, quiet_vol=100):
    bars = []
    for k in range(60):  # 변동 큰 구간(분당 변동폭 1%)
        bars.append(Bar(t(9, 0) + timedelta(minutes=k), 10000, 10050, 9950, 10000, 100))
    for k in range(20):  # 압축 구간(0.2%)
        bars.append(Bar(t(10, 0) + timedelta(minutes=k), 10000, 10010, 9990, 10000, quiet_vol))
    bars.append(Bar(t(10, 20), 10000, breakout_close, 9995, breakout_close, breakout_vol))  # 돌파: 박스 고점 위, 거래량 확장, 고가 마감
    for k in range(1, 15):
        bars.append(Bar(t(10, 20) + timedelta(minutes=k), breakout_close, breakout_close + 5, breakout_close - 5, breakout_close, 100))
    return {"20261015": bars}


def test_squeeze_signal_fires_on_breakout_bar_with_structural_stop_and_funnel():
    p = Params(entry_mode="squeeze")
    days = sq_day()
    fun: dict = {}
    sig = squeeze_signals_by_day(days, p, funnel=fun)["20261015"]
    assert [i for i, _, _ in sig] == [80]  # 박스(직전 20분)는 돌파 봉 자신을 포함하지 않는다(룩어헤드 없음)
    i, stop, risk = sig[0]
    assert stop == pytest.approx((10010 - 0.5 * 20) * 0.999) and risk == pytest.approx((10060 - stop) / 10060)
    assert fun["ok"] == 1 and fun["no_break"] > 0 and sum(fun.values()) > 1  # 퍼널이 단계별로 센다


def test_ablation_removes_components_and_keeps_superset_property():
    p = Params(entry_mode="squeeze")
    weak_vol = sq_day(breakout_vol=150)  # 거래량 확장 없음
    assert squeeze_signals_by_day(weak_vol, p)["20261015"] == []
    assert len(squeeze_signals_by_day(weak_vol, p, ("vol",))["20261015"]) == 1  # 거래량 조건을 빼면 신호
    no_squeeze = sq_day()
    bars = no_squeeze["20261015"]
    noisy = [Bar(b.ts, b.open, 10050, 9950, b.close, b.volume) if 60 <= k < 80 else b for k, b in enumerate(bars)]  # 박스도 출렁임
    assert squeeze_signals_by_day({"20261015": noisy}, p)["20261015"] == []
    # 압축 조건만 빼면 '출렁이는 박스'도 돌파로 볼 수 있지만, 박스가 넓어 손익비/최대 손절폭에서 걸러질 수 있다(예외 없이 동작해야 함)
    assert isinstance(squeeze_signals_by_day({"20261015": noisy}, p, ("compress",))["20261015"], list)
    full = squeeze_signals_by_day(no_squeeze, p)["20261015"]
    for drop in (("vol",), ("flow",), ("compress",), ("compress", "vol", "flow")):
        assert len(squeeze_signals_by_day(no_squeeze, p, drop)["20261015"]) >= len(full)  # 조건을 빼면 신호가 줄지 않는다


def test_simulate_exit_with_structural_stop_price():
    reg = [Bar(t(10, 0), 10000, 10000, 10000, 10000, 1), Bar(t(10, 1), 10000, 10020, 9985, 9990, 1)]
    r = Rules(tp=0.013, sl=0.05, max_hold_min=30, slip_ticks=0)
    assert simulate_exit(reg, 0, r)["reason"] != "stop"  # 고정 -5% 손절은 닿지 않는다
    o = simulate_exit(reg, 0, r, stop_px=9990.0)  # 구조적 손절(9,990) 은 닿는다
    assert o["reason"] == "stop" and o["net"] < 0
    up = [reg[0], Bar(t(10, 1), 10000, 10300, 10000, 10250, 1)]
    assert simulate_exit(up, 0, r, stop_px=9990.0)["reason"] == "tp"


def test_squeeze_study_has_five_variants_and_funnel():
    res = squeeze_study(sq_day(), Params(entry_mode="squeeze"), Rules(tp=0.013, sl=0.01))
    assert [r["label"] for r in res["rows"]][0].startswith("전체 기준") and len(res["rows"]) == 5
    assert res["funnel"]["ok"] == 1 and res["n_days"] == 1


# ------------------------------------------------------------------ 엔진 end-to-end
def scenario(path="up"):
    """09:00~10:19 변동 후 압축, 10:20 에 거래량·매수체결과 함께 박스 돌파. path: up(익절) | down(박스 이탈)"""
    ticks = []

    def minute(m_idx, prices, vol, side_fn):
        for j, px in enumerate(prices):
            ticks.append(Tick(t(9, 0) + timedelta(minutes=m_idx, seconds=j * 3), "005930", px, vol, side_fn(j), px + 5, px - 5))

    for k in range(60):  # 큰 변동(분당 ~1%)
        minute(k, [10000 + (50 if j % 2 else -50) for j in range(20)], 10, lambda j: 1 if j % 2 else -1)
    for k in range(60, 80):  # 압축(분당 ~0.2%)
        minute(k, [10000 + (10 if j % 2 else -10) for j in range(20)], 10, lambda j: 1 if j % 2 else -1)
    minute(80, [10010 + int(50 * j / 19) for j in range(20)], 50, lambda j: 1)  # 돌파 분: 고점(10,010) 위로 상승, 거래량 5배
    if path == "up":
        for m in range(81, 90):
            minute(m, [10060 + (m - 80) * 25 + j for j in range(20)], 10, lambda j: 1)
    else:
        minute(81, [10060 - int(100 * j / 19) for j in range(20)], 10, lambda j: -1)  # 곧바로 박스 안으로 밀려 들어옴
        minute(82, [9960 - j for j in range(20)], 10, lambda j: -1)
    return ticks


def run_scenario(path):
    logging.disable(logging.CRITICAL)
    p = Params(entry_mode="squeeze", overnight_new_entry=False)
    eng = TradingEngine(["005930"], p, PaperBroker(10_000_000, CostModel(slip_ticks=0)), kill_file=None)
    for tk in scenario(path):
        eng.on_tick(tk)
    return eng


def test_engine_squeeze_breakout_takes_profit():
    eng = run_scenario("up")
    assert eng.trades, "압축 돌파에서 진입해야 한다"
    tr = eng.trades[0]
    assert tr["f_entry_reason"] == "squeeze_break" and tr["mode"] == "squeeze" and tr["reason"] == "take_profit" and tr["net"] > 0
    assert tr["entry_ts"] >= t(10, 20).isoformat()  # 돌파 분 이전에는 진입하지 않는다
    assert len([x for x in eng.trades if x["f_entry_reason"] == "squeeze_break"]) == 1  # 같은 박스에서 재진입 없음


def test_engine_squeeze_failed_breakout_exits_at_structural_stop():
    eng = run_scenario("down")
    assert eng.trades and eng.trades[0]["reason"] == "box_break" and eng.trades[0]["net"] < 0
    assert eng.trades[0]["exit_price"] > 9900  # 손절은 박스 중간 근처에서 끝나고 더 깊이 끌려가지 않는다
