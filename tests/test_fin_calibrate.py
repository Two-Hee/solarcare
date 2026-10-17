import json
import random
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from daytrader.bars import Bar
from daytrader.broker.paper import PaperBroker
from daytrader.calibrate import calibrate, render, write_override
from daytrader.config import Params
from daytrader.costs import CostModel
from daytrader.engine import TradingEngine
from daytrader.fin import FinRow, MarketFlags, assess_fin, fin_as_of, load_csv, metrics, parse_flags, period_end
from daytrader.models import Quote, Tick
from daytrader.replay import make_daily_provider
from daytrader.strategy import FlowStrategy
from tests.test_core import at, ctx, make_snap, rally_events

P = Params()


def q(rows):
    """(period, sales, op, debt) 목록 -> FinRow"""
    return [FinRow(per, s, op, debt_ratio=d) for per, s, op, d in rows]


GOOD = q([("202409", 3400, 90, 80.0), ("202412", 3500, 100, 82.0), ("202503", 3600, 150, 85.0), ("202506", 3484, -73, 90.0),
          ("202509", 3700, 50, 95.0), ("202512", 4259, 178, 100.0), ("202603", 4300, 200, 105.0)])


# ------------------------------------------------------------------ 재무 판정
def test_metrics_turnaround_growth_and_estimates_are_separate():
    rows = GOOD + [FinRow("202606", 5033, 428, debt_ratio=111.82, estimate=True)]
    m = metrics(rows)
    assert m["period"] == "202603" and m["n"] == 7  # 예상치 행은 최신 분기로 치지 않는다
    assert m["sales_qoq"] == pytest.approx(4300 / 4259 - 1) and m["sales_yoy"] == pytest.approx(4300 / 3600 - 1)
    assert m["loss_streak"] == 0 and m["estimate"]["op_profit"] == 428
    t = metrics(q([("202503", 100, -5, 90.0), ("202506", 120, 10, 90.0)]))
    assert t["op_turnaround"] is True and t["op_margin"] == pytest.approx(10 / 120)


def test_assess_fin_ok_with_notes_and_estimates_ignored():
    g = assess_fin(GOOD + [FinRow("202606", 100, -999, estimate=True)], None, P)  # 예상치가 나빠도 판정에 안 쓴다
    assert g.level == "ok" and "예상치" in g.note
    assert assess_fin([], None, P).level == "ok" and "확인 불가" in assess_fin([], None, P).note


def test_assess_fin_block_and_caution_rules():
    impaired = GOOD[:-1] + [FinRow("202603", 4300, 200, debt_ratio=105.0, equity=90.0, capital=100.0)]
    g = assess_fin(impaired, None, P)
    assert g.level == "block" and "자본잠식" in g.note
    assert assess_fin(GOOD[:-1] + [FinRow("202603", 4300, 200, equity=-5.0, capital=100.0)], None, P).note.count("완전자본잠식") == 1
    streak = q([("202506", 100, -5, 90.0), ("202509", 100, -6, 90.0), ("202512", 100, -7, 90.0)])
    g = assess_fin(streak, None, P)
    assert g.level == "caution" and "3분기 연속" in g.note
    assert assess_fin(q([("202603", 100, 5, 250.0)]), None, P).level == "caution"  # 부채비율 250%
    drop = q([("202503", 1000, 50, 90.0), ("202506", 900, 50, 90.0), ("202509", 800, 50, 90.0), ("202512", 700, 50, 90.0), ("202603", 600, 50, 90.0)])
    assert "매출 YoY -40%" in assess_fin(drop, None, P).note and assess_fin(drop, None, P).level == "caution"
    assert assess_fin(q([("202603", 100, -5, 90.0)]), None, P).note.count("최근 분기 영업적자") == 1


def test_market_flags_block_and_caution():
    assert assess_fin(GOOD, MarketFlags(warn="02"), P).level == "block"
    assert assess_fin(GOOD, MarketFlags(warn="03"), P).note.count("투자위험") == 1
    assert assess_fin(GOOD, MarketFlags(short_overheat=True), P).level == "block"
    for f in (MarketFlags(halted=True), MarketFlags(managed=True), MarketFlags(liquidation=True)):
        assert assess_fin(GOOD, f, P).level == "block"
    assert assess_fin(GOOD, MarketFlags(warn="01"), P).level == "caution"
    assert assess_fin(GOOD, MarketFlags(caution_notice=True), P).level == "caution"
    g = assess_fin(GOOD, MarketFlags(known=False), P)
    assert g.level == "ok" and "확인 불가" in g.note  # 플래그를 못 읽었다고 막지는 않는다
    assert assess_fin(GOOD, MarketFlags(), P).level == "ok"


def test_parse_flags_from_kis_price_output():
    out = {"mrkt_warn_cls_code": "01", "short_over_yn": "Y", "invt_caful_yn": "N", "temp_stop_yn": "N",
           "mang_issu_cls_code": "N", "sltr_yn": "N", "per": "12.3", "pbr": "1.1", "hts_avls": "9,000", "lstn_stcn": ""}
    f = parse_flags(out)
    assert f.known and f.warn == "01" and f.short_overheat and not f.halted and f.info == {"per": "12.3", "pbr": "1.1", "hts_avls": "9,000"}
    assert parse_flags({"stck_prpr": "100"}).known is False  # 상태 필드가 하나도 없으면 '확인 불가'


# ------------------------------------------------------------------ 공시 시차 / CSV
def test_fin_as_of_respects_disclosure_lag_and_drops_estimates():
    rows = [FinRow("202606", 1, 1), FinRow("202609", 1, 1, estimate=True)]
    assert period_end("202606") == date(2026, 6, 30)
    assert fin_as_of(rows, date(2026, 8, 18)) == []  # 6/30 + 50일 = 8/19
    assert [r.period for r in fin_as_of(rows, date(2026, 8, 19))] == ["202606"]
    assert fin_as_of(rows, date(2027, 1, 1))[-1].period == "202606"  # 예상치는 끝까지 제외


def test_load_csv_handles_commas_dashes_estimate_flag(tmp_path):
    f = tmp_path / "fin" / "X.csv"
    f.parent.mkdir()
    f.write_text("period,sales,op_profit,debt_ratio,reserve_ratio,estimate\n2026-03,\"4,300\",200,105.0,\"2,860.25\",\n2026/06,5033,428,-,,1\nbad,1,1,1,1,\n", encoding="utf-8")
    rows = load_csv(f)
    assert [r.period for r in rows] == ["202603", "202606"]
    assert rows[0].sales == 4300 and rows[0].reserve_ratio == 2860.25 and rows[0].estimate is False
    assert rows[1].debt_ratio is None and rows[1].estimate is True
    assert load_csv(tmp_path / "none.csv") == []


def test_kis_financials_merge_three_endpoints():
    from daytrader.kis import KisRest

    def fake(method, path, tr, params=None, body=None):
        assert params["FID_DIV_CLS_CODE"] == "1" and params["fid_input_iscd"] == "161000"
        if path.endswith("income-statement"):
            return {"output": [{"stac_yymm": "202603", "sale_account": "4,300", "bsop_prti": "200", "thtr_ntin": "150"},
                               {"stac_yymm": "202512", "sale_account": "4259", "bsop_prti": "178", "thtr_ntin": "-"}]}
        if path.endswith("financial-ratio"):
            return {"output": [{"stac_yymm": "202603", "lblt_rate": "105.0", "rsrv_rate": "2860.25"}]}
        return {"output": [{"stac_yymm": "202603", "total_cptl": "5000", "cpfn": "1200"}]}

    rest = object.__new__(KisRest)
    rest._call = fake
    rows = rest.financials("161000")
    assert [r.period for r in rows] == ["202512", "202603"]
    a, b = rows
    assert (b.sales, b.op_profit, b.net_profit, b.debt_ratio, b.reserve_ratio, b.equity, b.capital) == (4300, 200, 150, 105.0, 2860.25, 5000, 1200)
    assert a.net_profit is None and a.debt_ratio is None


# ------------------------------------------------------------------ 전략/백테스트 연동
def test_fin_gate_in_strategy():
    st, off = FlowStrategy(Params()), FlowStrategy(Params(fin_gate=False))
    snap = make_snap()
    c = ctx(snap)
    c.daily.fin = "block"
    assert st.decide(c).kind == "hold" and off.decide(c).kind == "buy"
    c = ctx(snap)
    c.daily.fin = "caution"
    assert st.decide(c).kind == "buy"  # 주의는 일반 단타 진입은 허용
    late = make_snap(ts=at(15, 10), score60=60.0)
    c = ctx(late, now=at(15, 10))
    assert st.decide(c).reason == "closing_bet"
    c.daily.fin = "caution"
    assert st.decide(c).kind == "hold"  # 재무 주의면 종가배팅(이월) 금지


def test_replay_provider_uses_lagged_financials(tmp_path):
    (tmp_path / "fin").mkdir()
    (tmp_path / "fin" / "X.csv").write_text(
        "period,sales,op_profit,debt_ratio\n202506,100,-5,90\n202509,100,-6,90\n202512,100,-7,90\n", encoding="utf-8")
    prov = make_daily_provider(tmp_path, Params())
    # 공시 인정 시점 = 분기 종료 + 50일: 2025-06 -> 8/19, 2025-09 -> 11/19, 2025-12 -> 2/19
    assert prov("X", date(2025, 8, 18)).fin == "ok"  # 아직 아무것도 공시 전(데이터 없음)
    d = prov("X", date(2025, 8, 20))
    assert d.fin == "caution" and "최근 분기 영업적자" in d.fin_note  # 적자 1분기: 연속은 아니지만 최근 분기가 적자
    d = prov("X", date(2025, 11, 25))
    assert d.fin == "caution" and "2분기 연속" in d.fin_note  # 2분기 연속 적자가 알려짐
    assert "2분기 연속" in prov("X", date(2026, 2, 10)).fin_note  # 12월 분기는 아직 공시 전
    d = prov("X", date(2026, 3, 1))
    assert d.fin == "caution" and "3분기 연속" in d.fin_note
    assert prov("Y", date(2026, 3, 1)).fin == "ok"  # 데이터 없는 종목은 막지 않는다


# ------------------------------------------------------------------ 종목별 파라미터
def relabel(events, code):
    return [replace(e, code=code) for e in events]


def test_per_stock_params_in_engine():
    base = Params(vol_adaptive=False, min_trades_1m=10, vwap_tol=0.5, max_chase_pct=1.0, overnight_new_entry=False)
    quiet = replace(base, entry_score_1m=999.0, overnight_gap_pct=0.07)
    eng = TradingEngine(["005930", "000660"], base, PaperBroker(10_000_000, CostModel(slip_ticks=0)), kill_file=None,
                        params_by_code={"000660": quiet})
    events = sorted(relabel(rally_events(at(10, 0), 70000, 300), "005930") + relabel(rally_events(at(10, 0), 70000, 300), "000660"), key=lambda e: e.ts)
    for e in events:
        (eng.on_tick if isinstance(e, Tick) else eng.on_quote)(e)
    assert {t["code"] for t in eng.trades} == {"005930"}  # 000660 은 자기 파라미터(진입 불가)를 따른다
    assert eng.states["000660"].p.entry_score_1m == 999.0 and eng.states["005930"].p.entry_score_1m == base.entry_score_1m
    assert eng.states["000660"].strategy.p is eng.states["000660"].p
    assert eng.risk.p.overnight_gap_pct == 0.07  # 계좌 리스크는 가장 보수적인 갭 가정


def test_params_load_for_merges_partial_overrides(tmp_path):
    base = Params(tp_pct=0.013)
    base.save(tmp_path / "params.json")
    (tmp_path / "params").mkdir()
    (tmp_path / "params" / "AAA.json").write_text(json.dumps({"min_atr1m": 0.003, "bogus": 1}), encoding="utf-8")
    b, by = Params.load_for(["AAA", "BBB"], tmp_path / "params.json", tmp_path / "params")
    assert set(by) == {"AAA"} and by["AAA"].min_atr1m == 0.003 and by["AAA"].tp_pct == 0.013
    assert b.min_atr1m == base.min_atr1m  # 공통 값은 그대로
    (tmp_path / "params" / "BBB.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(SystemExit):
        Params.load_for(["BBB"], tmp_path / "params.json", tmp_path / "params")


# ------------------------------------------------------------------ 보정 제안
def rand_days(n_days, gap=0.0, seed=1, start=10000):
    rng = random.Random(seed)
    days, px = {}, float(start)
    for d in range(n_days):
        px *= 1 - gap
        bars = []
        for k in range(390):
            ts = datetime(2026, 10, 5 + d, 9, 0) + timedelta(minutes=k)
            o = px
            px = max(100.0, px * (1 + rng.gauss(0, 0.002)))
            bars.append(Bar(ts, int(o), int(max(o, px) * 1.001), int(min(o, px) * 0.999), int(px), 1000))
        days[f"202610{5 + d:02d}"] = bars
    return days


def test_calibrate_refuses_small_samples():
    res = calibrate(rand_days(3), P)
    assert res["suggest"] == {} and "표본 부족" in res["notes"][0]


def test_calibrate_suggestions_are_bounded_and_have_no_tp_sl():
    res = calibrate(rand_days(8), P)
    s = res["suggest"]
    assert {"min_atr1m", "dip_min_drop_pct", "max_chase_pct"} <= set(s)
    assert "tp_pct" not in s and "sl_pct" not in s  # 기준선으로 익절/손절을 고르지 않는다
    assert 0.0005 <= s["min_atr1m"] <= 0.005 and 0.008 <= s["dip_min_drop_pct"] <= 0.04 and 0.005 <= s["max_chase_pct"] <= 0.04
    assert res["table"] and all(r["n"] > 0 for r in res["table"]) and res["round_trip_cost"] > 0.002
    assert "선택 근거로 쓰지 마세요" in render("X", res, P)


def test_calibrate_never_relaxes_overnight_gap_on_small_samples_but_raises_when_needed():
    calm = calibrate(rand_days(8, gap=0.0), P)
    assert "overnight_gap_pct" not in calm["suggest"] and any("현재 값을 유지" in n for n in calm["notes"])
    wild = calibrate(rand_days(8, gap=0.06), P)  # 매일 시가가 -6% 갭
    assert wild["suggest"]["overnight_gap_pct"] == pytest.approx(0.075, abs=0.01)
    assert wild["suggest"]["overnight_gap_pct"] > P.overnight_gap_pct


def test_write_override_merges_and_keeps_existing_keys(tmp_path):
    p = write_override("AAA", {"min_atr1m": 0.003}, tmp_path)
    write_override("AAA", {"max_chase_pct": 0.015}, tmp_path)
    assert json.loads(p.read_text(encoding="utf-8")) == {"min_atr1m": 0.003, "max_chase_pct": 0.015}
