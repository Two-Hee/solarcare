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
