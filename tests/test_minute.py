from datetime import datetime, timedelta

import pytest

from daytrader.analysis import (
    Rules, atr_at, base_rate, day_stats, dip_signals, event_study, gate_pass, pressure, range_percentiles,
    regular, simulate_exit, swing_summary, vol_profile, volume_spike_signals, zigzag,
)
from daytrader.bars import Bar, import_csv, load_bars, load_days, save_bars, save_by_day, ticks_to_bars
from daytrader.config import Params
from daytrader.models import Tick

D0 = datetime(2026, 10, 15)


def at(h, m, day=0):
    return (D0 + timedelta(days=day)).replace(hour=h, minute=m)


def bar(ts, o, h=None, l=None, c=None, v=1000, **kw):
    c = o if c is None else c
    return Bar(ts, o, max(o, c) if h is None else h, min(o, c) if l is None else l, c, v, **kw)


def series(day, start_h, start_m, prices, vol=1000, wiggle=0):
    """prices: 분마다의 종가. 시가=직전 종가."""
    out, prev = [], prices[0]
    for k, px in enumerate(prices):
        t = at(start_h, start_m, day) + timedelta(minutes=k)
        out.append(Bar(t, prev, max(prev, px) + wiggle, min(prev, px) - wiggle, px, vol))
        prev = px
    return out


# ------------------------------------------------------------------ 봉 만들기/입출력
def test_ticks_to_bars_aggregates_volume_and_sides():
    t = datetime(2026, 10, 15, 9, 0, 5)
    ev = [
        Tick(t, "A", 100, 10, 1), Tick(t + timedelta(seconds=20), "A", 102, 5, -1), Tick(t + timedelta(seconds=40), "A", 99, 7, 0),
        Tick(t + timedelta(seconds=70), "A", 101, 3, 1),
    ]
    b1, b2 = ticks_to_bars(ev)
    assert (b1.open, b1.high, b1.low, b1.close, b1.volume, b1.buy_vol, b1.sell_vol) == (100, 102, 99, 99, 22, 10, 5)
    assert b1.value == pytest.approx(100 * 10 + 102 * 5 + 99 * 7) and b2.ts == datetime(2026, 10, 15, 9, 1)


def test_csv_roundtrip_and_day_split(tmp_path):
    bars = [bar(at(9, 0), 100, 101, 99, 100, 5, buy_vol=3, sell_vol=2), bar(at(9, 1, day=1), 110)]
    assert save_by_day(tmp_path, "A", bars) == ["20261015", "20261016"]
    days = load_days(tmp_path, "A")
    assert list(days) == ["20261015", "20261016"] and days["20261015"][0] == bars[0]
    assert list(load_days(tmp_path, "A", last=1)) == ["20261016"] and load_days(tmp_path, "ZZ") == {}
    save_bars(tmp_path / "x.csv", bars)
    assert load_bars(tmp_path / "x.csv") == bars


def test_import_hts_csv_korean_headers_cp949_commas_and_signs(tmp_path):
    p = tmp_path / "hts.csv"
    p.write_bytes("일자,시간,시가,고가,저가,종가,거래량\n20261015,0905,\"11,000\",\"11,050\",\"10,980\",\"-11,020\",\"1,234\"\n합계,,,,,,\n".encode("cp949"))
    [b] = import_csv(p)
    assert b.ts == datetime(2026, 10, 15, 9, 5) and (b.open, b.high, b.low, b.close, b.volume) == (11000, 11050, 10980, 11020, 1234)
    q = tmp_path / "iso.csv"
    q.write_text("ts,open,high,low,close,volume\n2026-10-15 09:06,1,2,1,2,3\n", encoding="utf-8")
    assert import_csv(q)[0].ts == datetime(2026, 10, 15, 9, 6)
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        import_csv(bad)


def test_pressure_uses_real_flow_when_available_else_close_location():
    assert pressure(bar(at(9, 0), 100, 110, 100, 110, buy_vol=90, sell_vol=10)) == pytest.approx(0.8)
    assert pressure(bar(at(9, 0), 100, 110, 100, 110)) == 1.0  # 고가 마감
    assert pressure(bar(at(9, 0), 110, 110, 100, 100)) == -1.0  # 저가 마감
    assert pressure(bar(at(9, 0), 100)) == 0.0


# ------------------------------------------------------------------ 일별/세션
def test_day_stats_gap_premarket_and_afterhours():
    d1 = series(0, 9, 0, [11000, 11100, 11320], vol=100)
    d1 += [bar(at(19, 55), 11320, 11320, 10840, 10840, 50)]  # 시간외 -4.24%
    d2 = [bar(at(8, 5, 1), 10900, 11000, 10850, 11000, 10)] + series(1, 9, 0, [10900, 11300, 11720], vol=100)
    rows = day_stats({"20261015": d1, "20261016": d2})
    a, b = rows
    assert a["gap"] is None and a["close"] == 11320 and a["after_move"] == pytest.approx(10840 / 11320 - 1)
    assert a["after_low"] == pytest.approx(10840 / 11320 - 1)
    assert b["gap"] == pytest.approx(10900 / 11320 - 1) and b["ret"] == pytest.approx(11720 / 11320 - 1)
    assert b["pre_move"] == pytest.approx(11000 / 11320 - 1) and b["pre_high"] == pytest.approx(11000 / 11320 - 1)
    assert b["t_high"] == "09:02" and b["after_move"] is None


def test_regular_session_filter_excludes_nxt_bars():
    bs = [bar(at(8, 30), 1), bar(at(9, 0), 1), bar(at(15, 30), 1), bar(at(15, 31), 1), bar(at(19, 0), 1)]
    assert [b.ts.strftime("%H:%M") for b in regular(bs)] == ["09:00", "15:30"]


def test_vol_profile_and_percentiles():
    day = series(0, 9, 0, [100, 101] * 15, vol=10) + series(0, 9, 30, [100, 100] * 15, vol=10)  # 앞 30분 출렁, 뒤 30분 정지
    prof = vol_profile({"d": day}, bucket_min=30)
    assert [x["bucket"] for x in prof] == ["09:00", "09:30"] and prof[0]["avg_range"] > prof[1]["avg_range"] == 0
    q = range_percentiles({"d": day})
    assert q["p99"] >= q["p90"] >= q["p50"] >= 0 and q["n"] == 60


def test_atr_matches_live_definition_and_needs_five_bars():
    reg = series(0, 9, 0, [10000, 10100] * 10)
    assert atr_at(reg, 3) == 0.0
    expected = sum((b.high - b.low) / b.close for b in reg[6:16]) / 10
    assert atr_at(reg, 15, n=10) == pytest.approx(expected)


def test_gate_pass_quiet_and_wild():
    p = Params()
    quiet = series(0, 10, 0, [10000] * 60)  # 변동폭 0
    wild = series(0, 10, 0, [10000, 10900] * 30)  # 분당 ~8% -> 손절폭이 상한을 넘는다
    gq, gw = gate_pass({"d": quiet}, p), gate_pass({"d": wild}, p)
    assert gq["quiet"] == 1.0 and gq["pass"] == 0.0
    assert gw["wild"] > 0.9 and gw["pass"] < 0.1
    mid = series(0, 10, 0, [10000, 10040] * 30)  # 분당 0.4% -> 통과
    assert gate_pass({"d": mid}, p)["pass"] > 0.9


# ------------------------------------------------------------------ 스윙
def test_zigzag_ignores_noise_and_finds_legs():
    px = [100, 100.5, 100.2, 101, 104, 103.5, 104.2, 101, 100.8, 106, 105.5]
    bars = [bar(at(9, k), int(p * 100)) for k, p in enumerate(px)]
    legs = zigzag(bars, 0.03)
    assert [round(x["pct"] * 100, 1) for x in legs] == [round((104.2 / 100 - 1) * 100, 1), round((100.8 / 104.2 - 1) * 100, 1), round((106 / 100.8 - 1) * 100, 1)]
    assert all(abs(x["pct"]) >= 0.03 for x in legs)
    assert zigzag(bars[:1], 0.03) == []
    s = swing_summary({"20261015": bars}, 0.03)
    assert s["up_n"] == 2 and s["down_n"] == 1 and s["up_per_day"] == 2.0


# ------------------------------------------------------------------ 기준선: 익절/손절 먼저 닿기
R0 = Rules(tp=0.013, sl=0.010, max_hold_min=3, slip_ticks=1, fee=0.00015, tax=0.0018)


def net_of(entry, exit_px, r=R0):
    from daytrader.costs import tick_size

    sell = exit_px - r.slip_ticks * tick_size(exit_px)
    return sell * (1 - r.fee - r.tax) / (entry * (1 + r.fee)) - 1


def test_simulate_exit_take_profit_with_costs():
    reg = [bar(at(10, 0), 10000), bar(at(10, 1), 10000, 10200, 9990, 10100)]
    out = simulate_exit(reg, 0, R0)
    entry = 10000 + 10  # 종가 + 1틱
    assert out["reason"] == "tp" and out["entry"] == entry
    assert out["net"] == pytest.approx(net_of(entry, entry * 1.013))
    assert out["net"] < 0.013  # 비용만큼 깎인다


def test_simulate_exit_stop_and_same_bar_conflict_is_pessimistic():
    stop = [bar(at(10, 0), 10000), bar(at(10, 1), 10000, 10050, 9890, 9950)]
    assert simulate_exit(stop, 0, R0)["reason"] == "stop"
    both = [bar(at(10, 0), 10000), bar(at(10, 1), 10000, 10300, 9850, 10000)]  # 같은 봉에서 둘 다 닿음
    o = simulate_exit(both, 0, R0)
    assert o["reason"] == "stop" and o["net"] < 0


def test_simulate_exit_gap_through_stop_and_tp():
    down = [bar(at(10, 0), 10000), bar(at(10, 1), 9700, 9750, 9650, 9700)]  # 시가부터 손절선 아래
    o = simulate_exit(down, 0, R0)
    assert o["reason"] == "stop_gap" and o["net"] == pytest.approx(net_of(10010, 9700))  # 손절선이 아니라 시가에 체결
    up = [bar(at(10, 0), 10000), bar(at(10, 1), 10300, 10350, 10290, 10320)]
    assert simulate_exit(up, 0, R0)["reason"] == "tp_gap"


def test_simulate_exit_time_and_eod():
    flat = [bar(at(10, k), 10000) for k in range(8)]
    o = simulate_exit(flat, 0, R0)
    assert o["reason"] == "time" and o["minutes"] == 3
    late = [bar(at(15, 12), 10000), bar(at(15, 14), 10000), bar(at(15, 15), 10000), bar(at(15, 16), 10500)]
    r = Rules(max_hold_min=60)
    assert simulate_exit(late, 0, r)["reason"] == "eod" and simulate_exit(late, 0, r)["minutes"] == 3


def test_base_rate_only_in_entry_window_and_counts():
    p = Params()
    reg = series(0, 9, 0, [10000] * 400)
    trades = base_rate({"d": reg}, R0, p)
    hours = {t["ts"].strftime("%H:%M") for t in trades}
    assert min(hours) == "09:05" and max(hours) == "14:39"
    assert len(trades) == (14 * 60 + 40) - (9 * 60 + 5)
    assert all(t["reason"] in ("time", "eod") for t in trades)


# ------------------------------------------------------------------ 신호 / 이벤트 스터디
def test_volume_spike_signal_and_dedupe():
    reg = series(0, 10, 0, [10000] * 60, vol=100)
    reg[40] = Bar(reg[40].ts, 10000, 10050, 10000, 10040, 900)  # 9배, 양봉
    reg[44] = Bar(reg[44].ts, 10040, 10100, 10040, 10090, 900)  # 4분 뒤 또 급증 -> min_gap 로 제외
    reg[55] = Bar(reg[55].ts, 10040, 10100, 10000, 10010, 900)  # 음봉
    assert volume_spike_signals(reg, 4.0, bullish=True) == [40]
    assert volume_spike_signals(reg, 4.0, bullish=False) == [55]


def capitulation_day():
    flat = [10000] * 25
    fall = [9950, 9900, 9850, 9810, 9790, 9780]  # -2.2% 하락
    base = [9780, 9785, 9790]
    bounce = [9800, 9815, 9830]  # 저점 대비 +0.5%
    reg = series(0, 10, 0, flat + fall + base + bounce, vol=100)
    # 반등 구간 봉은 종가가 고가 근처(매수 우위)
    return [Bar(b.ts, b.open, b.close, b.low, b.close, b.volume) if k >= 34 else b for k, b in enumerate(reg)]


def test_dip_signal_fires_on_bounce_not_during_fall():
    reg = capitulation_day()
    sig = dip_signals(reg, Params())
    assert sig, "반등 구간에서 신호가 나와야 함"
    assert min(sig) >= 34 and all(i > 30 for i in sig)  # 하락 중(인덱스 25~30)에는 신호 없음
    assert dip_signals(series(0, 10, 0, [10000] * 40), Params()) == []  # 낙폭 없으면 신호 없음


def test_event_study_forward_returns_and_mfe_mae():
    reg = capitulation_day()
    res = event_study({"d": reg}, lambda r: [34], Rules(tp=0.05, sl=0.05, max_hold_min=10))
    e = reg[34].close
    assert res["n"] == 1
    assert res["fwd"][1] == pytest.approx(reg[35].close / e - 1) and res["fwd"][5] == pytest.approx(reg[min(39, len(reg) - 1)].close / e - 1)
    assert res["mfe30"] == pytest.approx(max(b.high for b in reg[35:]) / e - 1)
    assert res["mae30"] == pytest.approx(min(b.low for b in reg[35:]) / e - 1)


# ------------------------------------------------------------------ KIS 분봉 페이징(가짜 서버)
def test_kis_minute_bars_pages_back_to_open_and_dedupes():
    from daytrader.config import Settings
    from daytrader.kis import KisRest

    day = "20261015"
    times = [datetime(2026, 10, 15, 9, 0) + timedelta(minutes=k) for k in range(391)]  # 09:00 ~ 15:30
    calls = []

    def fake_call(method, path, tr_id, params=None, body=None):
        calls.append(params["FID_INPUT_HOUR_1"])
        limit = params["FID_INPUT_HOUR_1"]
        rows = [t for t in times if t.strftime("%H%M%S") <= limit][-120:][::-1]  # 최신순 120개
        return {"output2": [
            {"stck_bsop_date": day, "stck_cntg_hour": t.strftime("%H%M%S"), "stck_prpr": "100", "stck_oprc": "100",
             "stck_hgpr": "101", "stck_lwpr": "99", "cntg_vol": "10", "acml_tr_pbmn": "1000"} for t in rows]}

    rest = object.__new__(KisRest)
    rest.s = Settings(market_div="J")
    rest._call = fake_call
    bars = rest.minute_bars("161000", day)
    assert len(bars) == 391 and bars[0].ts == times[0] and bars[-1].ts == times[-1]
    assert [b.ts for b in bars] == sorted({b.ts for b in bars})  # 중복 없음
    assert 4 <= len(calls) <= 6 and calls[0] == "153000"


# ------------------------------------------------------------------ 리포트 end-to-end
def test_report_end_to_end(tmp_path):
    from daytrader.minute_report import run

    days = {}
    for d in range(4):
        days[f"2026101{5 + d}"] = series(d, 9, 0, ([10000, 10060, 10120, 10060, 9990, 9930] * 70)[:390], vol=200)
    md, path = run("161000", days, Params(), Rules(), out_dir=str(tmp_path))
    for h in ("## 1. 일별 요약", "## 3. 변동성 게이트", "## 4. 스윙", "## 5. 기준선", "## 6. 이벤트 스터디", "## 7. 거래량 급증"):
        assert h in md
    assert (tmp_path / "minute_161000_20261018.md").exists() and path.endswith("20261018.md")
    empty_md, _ = run("161000", {}, Params(), Rules(), out_dir=str(tmp_path))
    assert "데이터 없음" in empty_md
