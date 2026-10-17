import json
from datetime import datetime, timedelta

import pytest

from daytrader.broker.paper import PaperBroker
from daytrader.config import PARAM_BOUNDS, Params
from daytrader.costs import CostModel, tick_size
from daytrader.engine import TradingEngine
from daytrader.flow import FlowAnalyzer
from daytrader.guard import apply_proposal, validate_proposal
from daytrader.kis_ws import parse_frame
from daytrader.models import DailyContext, Quote, Tick
from daytrader.replay import load_all, metrics, run_replay
from daytrader.review import summarize
from daytrader.synth import generate

D = datetime(2026, 9, 1)


def at(h, m, s=0):
    return D.replace(hour=h, minute=m, second=s)


def test_tick_size_bands():
    assert [tick_size(p) for p in (1500, 3000, 10000, 30000, 70000, 300000, 600000)] == [1, 5, 10, 50, 100, 500, 1000]


def feed_buy_pressure(an, start, seconds, price0=70000, side=1, step_every=3):
    """초당 2체결, 한쪽 방향 우세."""
    price = price0
    for i in range(seconds * 2):
        if i % (step_every * 2) == 0:
            price += 100 * side
        an.on_tick(Tick(start + timedelta(seconds=i / 2), "005930", price, 200, side, price + 100, price - 100))
    return price


def test_buy_pressure_scores_positive_and_sell_negative():
    p = Params()
    a, b = FlowAnalyzer("A", p), FlowAnalyzer("B", p)
    for an in (a, b):
        an.on_quote(Quote(at(9, 10), an.code, 70100, 69900, 1000, 1000, 100000, 100000))
    feed_buy_pressure(a, at(9, 10), 90, side=1)
    feed_buy_pressure(b, at(9, 10), 90, side=-1)
    sa, sb = a.snapshot(at(9, 11, 30)), b.snapshot(at(9, 11, 30))
    assert sa.score60 > 25 and sa.score10 > 15
    assert sb.score60 < -25
    assert sa.s60.strength > 500 and sa.cvd > 0


def test_stale_window_decays_to_zero():
    a = FlowAnalyzer("A", Params())
    feed_buy_pressure(a, at(9, 10), 30)
    assert a.snapshot(at(9, 12)).score10 == 0.0


def test_tick_rule_fallback_when_feed_side_missing():
    a = FlowAnalyzer("A", Params())
    prices = [70000, 70100, 70200, 70200, 70100]
    for i, px in enumerate(prices):
        a.on_tick(Tick(at(9, 10, i), "A", px, 100, 0))
    assert a.cvd == 100 * (0 + 1 + 1 + 1 - 1)  # 첫 틱은 방향 미상


def test_parse_tick_frame_and_pingpong():
    vals = {n: "" for n in __import__("daytrader.kis_ws", fromlist=["TICK_FIELDS"]).TICK_FIELDS}
    vals.update(MKSC_SHRN_ISCD="005930", STCK_CNTG_HOUR="091503", STCK_PRPR="70100", ASKP1="70200", BIDP1="70100",
                CNTG_VOL="35", CCLD_DVSN="1", BSOP_DATE="20260901")
    raw = "0|H0STCNT0|001|" + "^".join(vals.values())
    [t] = parse_frame(raw)
    assert (t.code, t.price, t.vol, t.side, t.ask1) == ("005930", 70100, 35, 1, 70200)
    assert t.ts == datetime(2026, 9, 1, 9, 15, 3)
    [ctrl] = parse_frame('{"header":{"tr_id":"PINGPONG","datetime":"20260901091503"}}')
    assert ctrl["header"]["tr_id"] == "PINGPONG"
    assert parse_frame("garbage") == []


def test_parse_two_records_in_one_frame():
    from daytrader.kis_ws import TICK_FIELDS

    def rec(code, px):
        v = {n: "0" for n in TICK_FIELDS}
        v.update(MKSC_SHRN_ISCD=code, STCK_CNTG_HOUR="100000", STCK_PRPR=str(px), CNTG_VOL="10", CCLD_DVSN="5", BSOP_DATE="20260901")
        return "^".join(v.values())

    evs = parse_frame("0|H0STCNT0|002|" + rec("005930", 70000) + "^" + rec("000660", 180000))
    assert [(e.code, e.side) for e in evs] == [("005930", -1), ("000660", -1)]


def test_paper_broker_costs_and_slippage():
    b = PaperBroker(10_000_000, CostModel(fee_rate=0.00015, sell_tax=0.0018, slip_ticks=1))
    f = b.buy("A", 100, D, 70000, 70100, 70000)
    assert f.price == 70200 and b.cash() < 10_000_000 - 100 * 70200
    s = b.sell("A", 100, D, 70000, 70100, 70000)
    assert s.price == 69900 and s.tax == pytest.approx(69900 * 100 * 0.0018)
    assert b.sell("A", 1, D, 70000, 70100, 70000) is None


def run_script(engine, events):
    for e in events:
        (engine.on_tick if isinstance(e, Tick) else engine.on_quote)(e)


def make_engine(**kw):
    kw.setdefault("vol_adaptive", False)  # 변동성 게이트는 별도 테스트에서 검증
    p = Params(**kw)
    br = PaperBroker(10_000_000, CostModel(slip_ticks=0))
    return TradingEngine(["005930"], p, br, kill_file=None), br


def rally_events(start, price0, seconds, side=1):
    evs = [Quote(start, "005930", price0 + 100, price0 - 100, 1000, 3000, 80000, 140000)]
    price = price0
    for i in range(seconds * 2):
        if i % 4 == 0:
            price += 50 * side
        evs.append(Tick(start + timedelta(seconds=i / 2), "005930", price, 300, side, price + 50, price - 50))
        if i % 6 == 0:
            evs.append(Quote(start + timedelta(seconds=i / 2), "005930", price + 50, price - 50, 1000, 3000, 80000, 140000))
    return evs


def test_engine_enters_on_flow_and_takes_profit():
    eng, br = make_engine(min_trades_1m=10, vwap_tol=0.5, max_chase_pct=1.0, overnight_new_entry=False)
    run_script(eng, rally_events(at(10, 0), 70000, 300))
    reasons = [t["reason"] for t in eng.trades]
    assert "take_profit" in reasons
    tp = next(t for t in eng.trades if t["reason"] == "take_profit")
    assert tp["net"] > 0 and tp["f_score60"] >= 35


def test_no_entry_before_window_and_after_halt(tmp_path):
    eng, _ = make_engine(min_trades_1m=10, vwap_tol=0.5, max_chase_pct=1.0)
    run_script(eng, rally_events(at(9, 0), 70000, 120))  # 09:05 이전
    assert eng.trades == [] and all(cs.position is None for cs in eng.states.values())
    eng2, _ = make_engine(min_trades_1m=10, vwap_tol=0.5, max_chase_pct=1.0)
    eng2.risk.halted = True
    eng2.risk.day_start_equity = 1.0  # new_day 가 halted 를 풀지 않도록 날짜를 먼저 굴린다
    eng2.day = at(10, 0).date()
    run_script(eng2, rally_events(at(10, 0), 70000, 120))
    assert eng2.trades == [] and all(cs.position is None for cs in eng2.states.values())


def test_stop_loss_and_eod_flatten():
    eng, br = make_engine(min_trades_1m=10, vwap_tol=0.5, max_chase_pct=1.0, overnight_new_entry=False, overnight_enabled=False)
    evs = rally_events(at(10, 0), 70000, 120)
    run_script(eng, evs)
    cs = eng.states["005930"]
    assert cs.position or eng.trades  # 진입했음
    if cs.position:
        # 급락 -> 손절
        px = evs[-1].price if isinstance(evs[-1], Tick) else 70000
        t0 = at(10, 5)
        for i in range(10):
            p = int(px * (1 - 0.003 * (i + 1)))
            eng.on_tick(Tick(t0 + timedelta(seconds=i), "005930", p, 100, -1, p + 50, p - 50))
        assert any(t["reason"] in ("stop_loss", "flow_reversal", "breakeven_stop") for t in eng.trades)


def test_overnight_carry_and_next_day_exit():
    p = dict(min_trades_1m=10, vwap_tol=0.5, max_chase_pct=1.0, overnight_min_score=-100, overnight_min_profit=0.0,
             tp_pct=0.5, overnight_keep_frac=0.5, overnight_decide_time="15:08", flatten_time="15:15")
    eng, br = make_engine(**p)
    cs = eng.states["005930"]
    run_script(eng, rally_events(at(14, 0), 70000, 90))  # 진입
    assert cs.position is not None
    last = eng.states["005930"].analyzer.last_price
    # 15:09 가격 유지 -> 이월 결정(절반 매도)
    for i in range(0, 120, 2):
        eng.on_tick(Tick(at(15, 8, 30) + timedelta(seconds=i), "005930", last, 50, 1, last + 50, last - 50))
    pos = cs.position
    assert pos is not None and pos.overnight is True
    assert any(t["reason"] == "overnight_trim" for t in eng.trades)
    eng.on_clock(at(15, 20))
    assert cs.position is not None  # 15:15 이후에도 이월 확정분은 청산되지 않음
    # 다음날 10:30 -> 시간 청산
    nd = at(10, 30).replace(day=2)
    flat = int(pos.avg_price)  # 손익 0 부근 -> TP/SL 이 아니라 시간 청산이어야 함
    eng.on_tick(Tick(nd, "005930", flat, 10, 1, flat + 50, flat - 50))
    assert cs.position is None
    assert eng.trades[-1]["reason"] == "overnight_time_exit" and eng.trades[-1]["carried"] is True


def test_overnight_requires_investor_ok_when_configured():
    p = dict(min_trades_1m=10, vwap_tol=0.5, max_chase_pct=1.0, overnight_min_score=-100, overnight_min_profit=0.0,
             tp_pct=0.5, require_investor_ok=True)
    eng, _ = make_engine(**p)
    eng.states["005930"].daily = DailyContext(-5, -5, -10, -10)
    eng.daily_provider = lambda c, d: DailyContext(-5, -5, -10, -10)
    run_script(eng, rally_events(at(14, 0), 70000, 90))  # require_investor_ok 라 진입 자체가 막힘
    assert eng.states["005930"].position is None


def test_guard_blocks_locked_out_of_range_and_big_steps(tmp_path):
    cur = Params()
    ok, bad = validate_proposal(cur, {
        "position_frac": 0.9, "daily_loss_limit": 0.2, "entry_score_1m": 38.0, "tp_pct": 0.05,
        "sl_pct": 0.011, "not_a_param": 1, "exit_score_1m": "x",
    })
    assert ok == {"entry_score_1m": 38.0}
    assert set(bad) == {"position_frac", "daily_loss_limit", "tp_pct", "sl_pct", "not_a_param", "exit_score_1m"}
    pp, prop = tmp_path / "params.json", tmp_path / "p.json"
    cur.save(pp)
    prop.write_text(json.dumps({"params": {"entry_score_1m": 38.0, "position_frac": 0.9}}))
    with pytest.raises(SystemExit):
        apply_proposal(prop, pp, approve=False)
    r = apply_proposal(prop, pp, approve=True)
    assert r["applied"] == {"entry_score_1m": 38.0} and "position_frac" in r["rejected"]
    assert Params.load(pp).position_frac == 0.45 and Params.load(pp).entry_score_1m == 38.0


def test_all_bounds_contain_defaults():
    d = Params().__dict__
    for k, (lo, hi) in PARAM_BOUNDS.items():
        assert lo <= d[k] <= hi, k


def test_synth_backtest_pipeline_and_review(tmp_path):
    generate("005930", 4, tmp_path, seed=3)
    days = load_all(tmp_path, ["005930"])
    assert len(days) == 4
    eng, m = run_replay(["005930"], days, Params(), 10_000_000, tmp_path)
    assert m["trades"] > 0
    assert all(t["fees"] > 0 for t in eng.trades)
    # 모든 포지션은 데이터 종료 시 정리됨
    assert all(cs.position is None for cs in eng.states.values())
    s = summarize(eng.trades, 10_000_000)
    assert s["overall"]["trades"] == m["trades"]
    assert metrics([], 1.0)["trades"] == 0


# ---------------------------------------------------------------- 변동성 연동 / 뉴스 게이트
from daytrader.flow import FlowSnapshot, WindowStats  # noqa: E402
from daytrader.models import Position  # noqa: E402
from daytrader.news import classify_rules, combine  # noqa: E402
from daytrader.strategy import DecisionCtx, FlowStrategy  # noqa: E402


def make_snap(**over):
    strong = WindowStats(n=40, vol=5000, buy_vol=4000, sell_vol=1000, delta_ratio=0.6, strength=400.0, big_n=4,
                         big_delta_ratio=0.8, ret=0.004)
    base = dict(ts=at(10, 0), price=70000, ask1=70100, bid1=70000, vwap=69800.0, atr1m=0.004, cvd=9000, ob_imb=0.3,
                top_imb=0.2, spread_ticks=1.0, s10=strong, s60=strong, s300=WindowStats(n=100, ret=0.006),
                score10=40.0, score60=50.0, score300=30.0)
    base.update(over)
    return FlowSnapshot(**base)


def ctx(snap, pos=None, news="ok", **over):
    base = dict(now=at(10, 0), snap=snap, pos=pos, daily=DailyContext(news=news), trades_today=0, cooldown_until=None,
                entries_allowed=True, overnight_room=True, closebet_done=False)
    base.update(over)
    return DecisionCtx(**base)


def test_atr1m_from_minute_bars():
    a = FlowAnalyzer("A", Params())
    for m in range(8):  # 매 분 고가-저가 1%
        a.on_tick(Tick(at(9, 10 + m, 5), "A", 10000, 10, 1))
        a.on_tick(Tick(at(9, 10 + m, 30), "A", 10100, 10, 1))
        a.on_tick(Tick(at(9, 10 + m, 50), "A", 10050, 10, 1))
    assert a.snapshot(at(9, 18, 59)).atr1m == pytest.approx(0.01 / 1.005, rel=0.05)
    assert FlowAnalyzer("B", Params()).snapshot(at(9, 10)).atr1m == 0.0  # 표본 부족


def test_stops_follow_volatility_and_block_wild_or_dead_stocks():
    st = FlowStrategy(Params())
    assert st.stops(make_snap(atr1m=0.0)) is None  # 표본 없음
    assert st.stops(make_snap(atr1m=0.001)) is None  # 너무 조용 -> +1.3% 도달 어려움
    assert st.stops(make_snap(atr1m=0.0025)) == (0.013, 0.008)  # 0.625% < 기본 손절 0.8%
    tp, sl = st.stops(make_snap(atr1m=0.004))
    assert tp == 0.013 and sl == pytest.approx(0.01)
    assert st.stops(make_snap(atr1m=0.009)) is None  # 2.25% > 상한 2% -> 너무 출렁임


def test_position_size_shrinks_as_stop_widens():
    st = FlowStrategy(Params())
    narrow = st.decide(ctx(make_snap(atr1m=0.0025)))  # 손절 0.8% -> 0.6%/0.8% = 0.75 -> 상한 0.45
    wide = st.decide(ctx(make_snap(atr1m=0.007)))  # 손절 1.75% -> 0.6%/1.75% = 0.343
    assert narrow.kind == wide.kind == "buy"
    assert narrow.frac == pytest.approx(0.45) and wide.frac == pytest.approx(0.006 / 0.0175)
    assert wide.sl_pct == pytest.approx(0.0175) and wide.frac * wide.sl_pct <= 0.0061  # 손절 시 손실 ≈ 자본의 0.6%


def test_position_uses_its_own_stop_not_default():
    st = FlowStrategy(Params())
    pos = Position("A", 10, 70000.0, at(9, 30), peak=70000.0, tp_pct=0.013, sl_pct=0.015)
    # -1.2%: 기본 손절(0.8%)이면 청산이지만 이 포지션의 손절은 1.5% 라 버틴다
    assert st.decide(ctx(make_snap(price=69160, score60=5.0, score10=5.0), pos)).kind == "hold"
    assert st.decide(ctx(make_snap(price=68900, score60=5.0, score10=5.0), pos)).reason == "stop_loss"


def test_news_rules_block_and_ai_cannot_downgrade():
    assert classify_rules(["애경케미칼, 100억 규모 유상증자 결정"]).level == "block"
    assert classify_rules(["전환사채 발행 공시"]).level == "caution"
    assert classify_rules(["신소재 공급계약 체결"]).level == "ok"
    block = classify_rules(["횡령 혐의 발생"])
    assert combine(block, {"level": "ok", "summary": "괜찮음"}).level == "block"
    assert combine(classify_rules(["공급계약 체결"]), {"level": "caution", "summary": "소문성"}).level == "caution"


def test_news_gate_in_strategy():
    st = FlowStrategy(Params())
    snap = make_snap()
    assert st.decide(ctx(snap, news="block")).kind == "hold"
    ok, caution = st.decide(ctx(snap)), st.decide(ctx(snap, news="caution"))
    assert caution.frac == pytest.approx(ok.frac * 0.5)
    pos = Position("A", 10, 70000.0, at(9, 30), peak=70000.0)
    assert st.decide(ctx(snap, pos, news="block")).reason == "news_block"
    # caution 이면 종가배팅/이월 금지
    late = make_snap(ts=at(15, 10), score60=60.0)
    assert st.decide(ctx(late, now=at(15, 10))).reason == "closing_bet"
    assert st.decide(ctx(late, news="caution", now=at(15, 10))).kind == "hold"


# ---------------------------------------------------------------- 분할 매수(과대낙폭 후 저점 형성)
from daytrader.strategy import Action  # noqa: E402


def dip_snap(**over):
    """고점 71,500 -> 저점 70,000(-2.1%) 후 70,200 로 반등, 매도세는 잦아든 상태. 흐름 진입(score60>=35)은 불가."""
    calm = WindowStats(n=40, vol=3000, buy_vol=1500, sell_vol=1650, delta_ratio=-0.05, strength=91.0, ret=0.001)
    buyers = WindowStats(n=12, vol=800, buy_vol=560, sell_vol=240, delta_ratio=0.4, strength=233.0, ret=0.001)
    base = dict(ts=at(10, 0), price=70200, hi_lb=71500, lo_lb=70000, lo_ts=at(9, 59, 0), hl2=0, atr1m=0.004,
                s10=buyers, s60=calm, s300=WindowStats(n=100, ret=-0.01), score10=15.0, score60=-20.0, score300=-30.0, vwap=70800.0)
    base.update(over)
    return make_snap(**base)


def test_dip_buy_after_capitulation_and_bounce():
    st = FlowStrategy(Params())
    a = st.decide(ctx(dip_snap()))
    assert a.kind == "buy" and a.reason == "dip_buy" and a.mode == "dip"
    assert a.frac == pytest.approx(0.45 * 0.4) and a.full_frac == pytest.approx(0.45)
    assert a.invalid_px == pytest.approx(70000 * 0.997)  # 저점 이탈이 곧 손절선


@pytest.mark.parametrize("over,why", [
    (dict(price=70000), "저점에서 반등 확인 전(떨어지는 칼날)"),
    (dict(lo_ts=at(9, 59, 50)), "신저점 직후"),
    (dict(hi_lb=70500), "낙폭 부족"),
    (dict(price=71000), "이미 반등해 추격"),
    (dict(s60=WindowStats(n=40, vol=3000, buy_vol=700, sell_vol=2300, delta_ratio=-0.53, ret=-0.004)), "아직 투매 중"),
    (dict(s10=WindowStats(n=12, vol=800, buy_vol=240, sell_vol=560, delta_ratio=-0.4, ret=-0.001)), "최근 10초 매도 우위"),
    (dict(atr1m=0.0), "변동성 표본 부족"),
])
def test_dip_buy_rejected_when_falling_knife(over, why):
    assert FlowStrategy(Params()).decide(ctx(dip_snap(**over))).kind == "hold", why


def test_dip_disabled_or_news_block():
    snap = dip_snap()
    assert FlowStrategy(Params(dip_enabled=False)).decide(ctx(snap)).kind == "hold"
    assert FlowStrategy(Params()).decide(ctx(snap, news="block")).kind == "hold"
    half = FlowStrategy(Params()).decide(ctx(snap, news="caution"))
    assert half.frac == pytest.approx(0.45 * 0.4 * 0.5)


def dip_pos(**over):
    base = dict(code="A", qty=10, avg_price=70200.0, entry_ts=at(9, 59, 30), peak=70200.0, tp_pct=0.013, sl_pct=0.01,
                mode="dip", full_frac=0.45, tranches=1, invalid_px=69790.0, last_add_ts=at(9, 59, 30), last_tranche_px=70200.0)
    base.update(over)
    return Position(**base)


def test_dip_add_on_higher_low_confirmation():
    st = FlowStrategy(Params())
    snap = dip_snap(price=70300, hl2=70100, score60=15.0, score10=5.0)  # 마지막 두 분봉 저점이 70,100 > 70,000
    a = st.decide(ctx(snap, dip_pos(), now=at(10, 0, 30)))
    assert a.kind == "buy" and a.reason == "dip_add_confirm" and a.frac == pytest.approx(0.45 * 0.3)
    # 3차(마지막): 1 - 0.4 - 0.3
    a3 = st.decide(ctx(snap, dip_pos(tranches=2), now=at(10, 0, 30)))
    assert a3.reason == "dip_add_confirm" and a3.frac == pytest.approx(0.45 * 0.3)
    assert st.decide(ctx(snap, dip_pos(tranches=3), now=at(10, 0, 30))).kind == "hold"  # 최대 3차
    assert st.decide(ctx(snap, dip_pos(last_add_ts=at(10, 0, 10)), now=at(10, 0, 30))).kind == "hold"  # 간격 45초
    assert st.decide(ctx(snap, dip_pos(), news="caution", now=at(10, 0, 30))).kind == "hold"  # 뉴스 주의면 추가 금지


def test_dip_add_deeper_needs_exhaustion_and_room_above_invalidation():
    st = FlowStrategy(Params())
    # 변동성이 커서 손절폭이 1.75% 인 포지션. 평단 대비 -0.6% 는 손절선의 60% 미만이라 추가 여력이 있다
    pos = dip_pos(avg_price=70800.0, last_tranche_px=70800.0, peak=70800.0, sl_pct=0.0175)
    snap = dip_snap(price=70370, hl2=0)  # 직전 매수가 대비 -0.6% 이상, 10초는 매수 우위, 저점 이탈선(69,790)과는 여유
    a = st.decide(ctx(snap, pos, now=at(10, 0, 30)))
    assert a.kind == "buy" and a.reason == "dip_add_deeper"
    # 손절폭 1% 인 포지션은 같은 가격에서 이미 손절선의 60% 를 넘어 물타기 금지
    tight = dip_pos(avg_price=70800.0, last_tranche_px=70800.0, peak=70800.0, sl_pct=0.01)
    assert st.decide(ctx(snap, tight, now=at(10, 0, 30))).kind == "hold"
    assert st.decide(ctx(dip_snap(price=70370, hl2=0, s10=WindowStats(n=12, delta_ratio=-0.3)), pos, now=at(10, 0, 30))).kind == "hold"
    assert FlowStrategy(Params(dip_allow_deeper_add=False)).decide(ctx(snap, pos, now=at(10, 0, 30))).kind == "hold"
    near = dip_snap(price=69900, hl2=0)  # 저점 이탈선 코앞에서는 물타지 않는다
    assert st.decide(ctx(near, dip_pos(last_tranche_px=70800.0), now=at(10, 0, 30))).kind == "hold"


def test_dip_low_break_exit_and_underwater_flow_reversal_ignored():
    st = FlowStrategy(Params())
    assert st.decide(ctx(dip_snap(price=69780), dip_pos(), now=at(10, 2))).reason == "dip_low_break"
    weak = dip_snap(price=70100, score60=-50.0, score10=-10.0)
    assert st.decide(ctx(weak, dip_pos(), now=at(10, 2))).kind == "hold"  # 손실 구간 수급반전은 무시
    flow_pos = dip_pos(mode="flow", invalid_px=0.0)
    assert st.decide(ctx(weak, flow_pos, now=at(10, 2))).reason == "flow_reversal"
    up = dip_snap(price=70500, score60=-50.0, score10=-10.0)  # 반등해 이익 중에 수급이 꺾이면 챙긴다
    assert st.decide(ctx(up, dip_pos(), now=at(10, 2))).reason == "flow_reversal"


def test_engine_merges_tranches_into_one_position_and_one_trade_record():
    eng, br = make_engine(slip_ticks=0)
    eng.broker.cost = CostModel(slip_ticks=0)
    cs = eng.states["005930"]
    eng.on_tick(Tick(at(10, 0), "005930", 70200, 10, 1, 70300, 70200))
    snap = cs.analyzer.snapshot(at(10, 0))
    first = Action("buy", frac=0.18, reason="dip_buy", mode="dip", full_frac=0.45, invalid_px=69790.0, tp_pct=0.013, sl_pct=0.01)
    eng._buy(cs, at(10, 0), snap, first)
    p1 = cs.position
    q1, px1 = p1.qty, p1.avg_price
    assert p1.mode == "dip" and p1.tranches == 1 and cs.trades_today == 1
    eng.on_tick(Tick(at(10, 1), "005930", 70600, 10, 1, 70700, 70600))
    snap = cs.analyzer.snapshot(at(10, 1))
    eng._buy(cs, at(10, 1), snap, Action("buy", frac=0.135, reason="dip_add_confirm"))
    pos = cs.position
    assert pos is p1 and pos.tranches == 2 and pos.qty > q1 and cs.trades_today == 1  # 거래 횟수는 차수가 아니라 포지션 단위
    assert px1 < pos.avg_price < 70700
    assert pos.invalid_px == 69790.0 and pos.full_frac == 0.45  # 저점 이탈선·목표 수량은 1차 기준 유지
    qty_total = pos.qty
    eng._sell(cs, at(10, 5), cs.analyzer.snapshot(at(10, 5)), 1.0, "take_profit")
    [rec] = eng.trades
    assert rec["qty"] == qty_total and rec["mode"] == "dip" and rec["tranches"] == 2 and rec["fees"] > 0


# ---------------------------------------------------------------- 일봉 국면(횡보 후 거래대금 폭발 돌파)
from datetime import date as _date  # noqa: E402

from daytrader.regime import DailyBar, detect  # noqa: E402
from daytrader.replay import make_daily_provider  # noqa: E402


def make_bars(n_base=30, after=()):
    """9,925~10,400 횡보(거래대금 15억/일) -> 돌파일(+7.5%, 거래대금 20배) -> after: (일중변동폭, 종가등락) 리스트."""
    bars, d = [], _date(2026, 8, 17)

    def nxt():
        nonlocal d
        d = d + __import__("datetime").timedelta(days=1)
        while d.weekday() >= 5:
            d = d + __import__("datetime").timedelta(days=1)
        return d.strftime("%Y%m%d")

    px = 10100.0
    for i in range(n_base):
        px = 10100 + (150 if i % 2 else -150)
        bars.append(DailyBar(nxt(), px, px + 120, px - 120, px, 1.5e9))
    base_close = px
    ev_close = base_close * 1.075
    bars.append(DailyBar(nxt(), base_close, ev_close * 1.01, base_close * 0.995, ev_close, 3.0e10))
    c = ev_close
    for rng, chg in after:
        c = c * (1 + chg)
        bars.append(DailyBar(nxt(), c / (1 + chg), c * (1 + rng / 2), c * (1 - rng / 2), c, 1.2e10))
    return bars


def test_regime_needs_enough_bars_and_detects_base():
    assert detect(make_bars(n_base=10)[:10]).state == "unknown"
    assert detect(make_bars()[:-1]).state == "base"  # 돌파 직전: 횡보 중 -> 후보


def test_regime_breakout_day_then_expansion_then_fading():
    r = detect(make_bars())
    assert r.state == "breakout" and r.signals["value_ratio"] >= 15 and r.signals["event_ret"] == pytest.approx(0.075, abs=0.002)
    ex = detect(make_bars(after=[(0.05, 0.03), (0.045, 0.02), (0.05, -0.01)]))
    assert ex.state == "expansion" and ex.signals["days_since"] == 3
    quiet = detect(make_bars(after=[(0.05, 0.02)] + [(0.01, 0.0)] * 6))
    assert quiet.state == "fading"  # 변동폭이 줄면 단타 대상에서 제외
    assert detect(make_bars(after=[(0.05, 0.02)] * 14)).state == "fading"  # 돌파 14일째: 확장 구간(10일)은 지남
    assert detect(make_bars(after=[(0.05, 0.02)] * 20)).state == "none"  # 돌파가 탐색 범위(15일) 밖
    assert detect(make_bars(n_base=30, after=[(0.04, 0.0)])).signals["days_since"] == 1


def test_no_event_when_volume_or_price_lacking():
    bars = make_bars()
    ev = bars[-1]
    weak_vol = bars[:-1] + [DailyBar(ev.date, ev.open, ev.high, ev.low, ev.close, 2.0e9)]
    assert detect(weak_vol).state in ("base", "none")  # 거래대금 배수 부족
    flat = bars[:-1] + [DailyBar(ev.date, ev.open, ev.high, ev.low, ev.open * 1.01, 3.0e10)]
    assert detect(flat).state in ("base", "none")  # 상승률/고점 돌파 부족


def test_regime_gate_blocks_entries_unless_breakout_or_expansion():
    snap = make_snap()
    off = FlowStrategy(Params())
    on = FlowStrategy(Params(regime_gate=True))
    assert off.decide(ctx(snap)).kind == "buy"
    for state in ("unknown", "none", "base", "fading"):
        c = ctx(snap)
        c.daily.regime = state
        assert on.decide(c).kind == "hold", state
    for state in ("breakout", "expansion"):
        c = ctx(snap)
        c.daily.regime = state
        assert on.decide(c).kind == "buy", state


def test_replay_regime_provider_has_no_lookahead(tmp_path):
    import csv

    (tmp_path / "daily_bars").mkdir()
    bars = make_bars()
    with (tmp_path / "daily_bars" / "161000.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "open", "high", "low", "close", "value"])
        for b in bars:
            w.writerow([b.date, b.open, b.high, b.low, b.close, b.value])
    prov = make_daily_provider(tmp_path)
    ev = bars[-1].date
    d_event = _date(int(ev[:4]), int(ev[4:6]), int(ev[6:]))
    assert prov("161000", d_event).regime == "base"  # 돌파일 당일 아침엔 아직 돌파를 모른다
    nxt = d_event + __import__("datetime").timedelta(days=1)
    assert prov("161000", nxt).regime == "breakout"  # 다음 거래일 아침에 비로소 인지
    assert prov("999999", nxt).regime == "unknown"
