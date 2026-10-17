from datetime import date, datetime, timedelta

import pytest

from daytrader.analysis import band_signals_by_day
from daytrader.bands import BandSnap, BandTracker, IntradayBand
from daytrader.bars import Bar
from daytrader.config import Params
from daytrader.flow import FlowAnalyzer
from daytrader.models import Position, Tick
from daytrader.regime import DailyBar, detect, detect_with
from daytrader.strategy import FlowStrategy
from tests.test_core import at, ctx, make_snap


def run(closes, **kw):
    cfg = dict(fast=2, slow=4, min_width=0.01, confirm_bars=3, slow_tol=0.5, fresh_bars=2)
    cfg.update(kw)
    tr = BandTracker(**cfg)
    evs = [tr.on_bar(i, c) for i, c in enumerate(closes)]
    return tr, evs


# ------------------------------------------------------------------ BandTracker
def test_cross_up_confirms_start_then_end_when_band_closes():
    closes = [10, 10, 10, 10, 9, 9, 9, 12, 12, 12, 5, 5]
    tr, evs = run(closes)
    assert [(i, e) for i, e in enumerate(evs) if e] == [(7, "start"), (10, "end")]
    assert tr.state == "none"
    # fresh 는 확정 후 fresh_bars(2) 개 봉까지
    tr2 = BandTracker(2, 4, 0.01, 3, 0.5, 2)
    fresh = []
    for i, c in enumerate(closes[:10]):
        tr2.on_bar(i, c)
        fresh.append(tr2.fresh)
    assert fresh[7:10] == [True, True, True] and tr2.bars_since_start == 2
    tr2.on_bar(10, 13)  # 계속 올라 밭이 유지된다
    assert tr2.state == "active" and not tr2.fresh and tr2.start_ts == 7  # 활성이지만 '시작 지점'은 지났다
    tr3 = BandTracker(2, 4, 0.01, 3, 0.5, 2)
    for i, c in enumerate(closes[:10] + [12]):
        tr3.on_bar(i, c)
    assert tr3.state == "none"  # 가격이 제자리면 두 이평이 같아져 밭이 사라진다(width 0)


def test_no_signal_during_warmup_or_when_started_already_expanded():
    tr, evs = run([10, 10, 10])
    assert not tr.ready and evs == [None, None, None]
    # 워밍업이 끝나는 첫 봉에서 이미 fast > slow: 이미 펼쳐진 밭은 '시작'이 아니다
    tr, evs = run([9, 10, 11, 12, 13, 14, 15])
    assert "start" not in evs and tr.state == "none" and tr.width > 0
    # 같은 값이 계속되다 갑자기 오르는 것은 폭 0 에서의 상향 돌파 = 시작
    assert "start" in run([9, 9, 9, 9, 12, 12, 12, 12])[1]


def test_brief_poke_above_is_a_false_start():
    tr, evs = run([10, 10, 10, 10, 9, 9, 9, 9.2, 8.5, 8.5], min_width=0.01)
    assert "start" not in evs and tr.state == "none"


def test_start_not_confirmed_if_gap_too_small_within_confirm_window():
    tr, evs = run([10, 10, 10, 10, 9, 9, 9, 9.2, 9.2, 9.2, 9.2, 9.2], min_width=0.02, confirm_bars=3)
    assert "start" not in evs  # 간격 0.5% < 2% 인 채로 confirm_bars 초과 -> 포기
    assert tr.state == "none"


def test_fast_must_be_shorter_than_slow():
    with pytest.raises(ValueError):
        BandTracker(20, 5)


# ------------------------------------------------------------------ IntradayBand (틱 -> N분봉)
def test_intraday_band_builds_n_minute_bars_and_reports_on_completion():
    ib = IntradayBand(tf_min=5, fast=2, slow=4, min_width=0.01, confirm_bars=3, slow_tol=0.5, fresh_bars=2)
    d = datetime(2026, 10, 15)
    prices = [(9, 0, 10), (9, 2, 10), (9, 5, 10), (9, 10, 10), (9, 15, 10), (9, 20, 9), (9, 25, 9), (9, 30, 9)]
    for h, m, px in prices:
        assert ib.on_price(d.replace(hour=h, minute=m), px) is None
    # 9:35 버킷에서 12 -> 아직 완성 전이라 이벤트 없음. 9:40 첫 틱이 9:35 봉(종가 12)을 완성시킨다
    assert ib.on_price(d.replace(hour=9, minute=35), 12) is None and ib.on_price(d.replace(hour=9, minute=37), 12) is None
    assert ib.on_price(d.replace(hour=9, minute=40), 12) == "start"
    s = ib.snap()
    assert s.fresh and s.state == "active" and s.ready and s.start_ts == d.replace(hour=9, minute=35)
    assert s.fast > s.slow and s.width == pytest.approx(s.fast / s.slow - 1)


def test_intraday_band_continues_across_days():
    ib = IntradayBand(1, 2, 4, 0.01, 3, 0.5, 2)
    for k, px in enumerate([10, 10, 10, 10, 9, 9, 9]):
        ib.on_price(datetime(2026, 10, 15, 15, 20 + k), px)
    ev = [ib.on_price(datetime(2026, 10, 16, 9, k), px) for k, px in enumerate([12, 12, 12])]  # 다음 날 장 시작
    assert "start" in ev  # 어제 종가들과 이어서 이평을 계산(차트와 동일)


# ------------------------------------------------------------------ 분석 연동(룩어헤드 없음)
def test_band_signals_by_day_fire_on_first_bar_after_confirmation():
    p = Params(band_tf_min=1, band_fast=2, band_slow=4, band_min_width=0.01, band_max_ext=0.5, band_fresh_bars=2)
    prices = [100] * 4 + [90, 90, 90, 120, 120, 120, 120]
    day = [Bar(at(10, k), pr, pr, pr, pr, 100) for k, pr in enumerate(prices)]
    sig = band_signals_by_day({"20261015": day}, p)["20261015"]
    # 120 이 처음 나온 봉(인덱스 7)이 완성되는 시점 = 다음 봉(인덱스 8)이 시작될 때 알 수 있다
    assert sig == [8]
    # 확장 한도를 좁히면(이미 slow 대비 너무 위) 신호가 사라진다
    assert band_signals_by_day({"20261015": day}, Params(**{**p.__dict__, "band_max_ext": 0.01}))["20261015"] == []


# ------------------------------------------------------------------ 전략 모드
def bsnap(**over):
    base = dict(state="active", fresh=True, width=0.012, fast=70000.0, slow=69800.0, bars_since_start=0, start_ts=at(9, 55), ready=True)
    base.update(over)
    return BandSnap(**base)


WEAK = dict(score60=0.0, score10=0.0)  # 수급 점수 기준 미달


def test_default_flow_mode_ignores_band():
    st = FlowStrategy(Params())
    assert st.decide(ctx(make_snap(band=bsnap(), **WEAK))).kind == "hold"  # 수급이 약하면 밭이 있어도 진입 안 함
    assert st.decide(ctx(make_snap(band=None))).reason == "flow_long"


def test_band_mode_buys_at_fresh_band_even_with_weak_flow_and_tags_position_mode():
    st = FlowStrategy(Params(entry_mode="band"))
    a = st.decide(ctx(make_snap(band=bsnap(), **WEAK)))
    assert a.kind == "buy" and a.reason == "band_start" and a.mode == "band"
    for over, why in [
        (dict(fresh=False), "이미 시작 시점이 지남"), (dict(state="none"), "밭 없음"), (dict(ready=False), "워밍업 중"),
        (dict(slow=71000.0), "현재가가 slow 아래"), (dict(slow=68000.0), "slow 대비 너무 위(늦음)"),
    ]:
        assert st.decide(ctx(make_snap(band=bsnap(**over), **WEAK))).kind == "hold", why
    assert st.decide(ctx(make_snap(band=None, **WEAK))).kind == "hold"


def test_band_mode_needs_liquidity_and_does_not_reenter_same_band():
    st = FlowStrategy(Params(entry_mode="band"))
    thin = make_snap(band=bsnap(), **WEAK)
    thin = make_snap(band=bsnap(), s60=type(thin.s60)(n=3, vol=100, buy_vol=60, sell_vol=40, delta_ratio=0.2), **WEAK)
    assert st.decide(ctx(thin)).kind == "hold"  # 최근 1분 체결 수 부족
    c = ctx(make_snap(band=bsnap(), **WEAK), last_exit_ts=at(9, 58))  # 이 밭(9:55 확정)에서 이미 청산함
    assert st.decide(c).kind == "hold"
    c = ctx(make_snap(band=bsnap(start_ts=at(10, 0)), **WEAK), last_exit_ts=at(9, 58))  # 새 밭이면 재진입 가능
    assert st.decide(c).kind == "buy"


def test_either_and_both_modes():
    strong = make_snap()  # 수급 강함, 밭 없음
    both_ok = make_snap(band=bsnap())
    band_only = make_snap(band=bsnap(), **WEAK)
    either, both = FlowStrategy(Params(entry_mode="either")), FlowStrategy(Params(entry_mode="both"))
    assert either.decide(ctx(strong)).reason == "flow_long"
    assert either.decide(ctx(band_only)).reason == "band_start"
    assert both.decide(ctx(both_ok)).reason == "band_start+flow" and both.decide(ctx(both_ok)).mode == "band"
    assert both.decide(ctx(strong)).kind == "hold" and both.decide(ctx(band_only)).kind == "hold"


def test_band_break_exit_only_for_band_positions():
    st = FlowStrategy(Params(entry_mode="band"))
    pos = Position("A", 10, 70000.0, at(9, 56), peak=70000.0, mode="band")
    dead = make_snap(band=bsnap(state="none", fresh=False), price=70100, score60=20.0, score10=5.0)
    assert st.decide(ctx(dead, pos, now=at(10, 0))).reason == "band_break"
    assert st.decide(ctx(make_snap(band=bsnap(), price=70100), pos, now=at(10, 0))).kind == "hold"  # 밭이 살아 있으면 보유
    flow_pos = Position("A", 10, 70000.0, at(9, 56), peak=70000.0, mode="flow")
    assert st.decide(ctx(dead, flow_pos, now=at(10, 0))).kind == "hold"  # 수급 진입 포지션은 영향 없음
    assert FlowStrategy(Params(entry_mode="band", band_exit=False)).decide(ctx(dead, pos, now=at(10, 0))).kind == "hold"
    young = Position("A", 10, 70000.0, at(9, 59, 50), peak=70000.0, mode="band")
    assert st.decide(ctx(dead, young, now=at(10, 0))).kind == "hold"  # 최소 보유시간(20초) 전


def test_analyzer_builds_band_only_when_needed():
    ticks = [Tick(datetime(2026, 10, 15, 10, k // 6, (k % 6) * 10), "A", 10000 + (k % 3) * 10, 100, 1) for k in range(300)]
    on, off = FlowAnalyzer("A", Params(entry_mode="band", band_tf_min=1)), FlowAnalyzer("A", Params())
    for t in ticks:
        on.on_tick(t)
        off.on_tick(t)
    assert off.snapshot(datetime(2026, 10, 15, 10, 51)).band is None
    s = on.snapshot(datetime(2026, 10, 15, 10, 51)).band
    assert s is not None and s.ready  # 1분봉 20개 이상 쌓임


# ------------------------------------------------------------------ 일봉 밭(국면)
def daily_closes(flat=40, rally=0, step=0.012):
    px, out, d = 100.0, [], date(2026, 6, 1)

    def nxt():
        nonlocal d
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d.strftime("%Y%m%d")

    for k in range(flat):
        px = 100.0 + (0.3 if k % 2 else -0.3)
        out.append(DailyBar(nxt(), px, px * 1.002, px * 0.998, px, 1e9))
    for _ in range(rally):
        px *= 1 + step
        out.append(DailyBar(nxt(), px / (1 + step), px * 1.002, px / (1 + step) * 0.998, px, 1e9))
    return out


def test_daily_band_start_regime_appears_then_expires():
    first = None
    for n in range(0, 25):
        r = detect(daily_closes(rally=n))
        if r.state == "bandstart":
            first = n
            break
    assert first is not None and 4 <= first <= 8 and r.signals["band_days"] == 0  # 하루 +1.2% 상승: 간격 1% 도달(약 6일째)에 확정
    assert "밭 시작" in r.note
    # 확정 후 band_recent(5) 일이 지나면 더 이상 '시작'이 아니다
    assert detect(daily_closes(rally=first + 8)).state != "bandstart"
    # 횡보만 있을 때는 시작이 아니다(간격이 min_width 에 못 미침)
    assert detect(daily_closes(rally=0)).state != "bandstart"


def test_regime_gate_accepts_bandstart_and_params_wiring():
    c = ctx(make_snap())
    c.daily.regime = "bandstart"
    assert FlowStrategy(Params(regime_gate=True)).decide(c).kind == "buy"
    p = Params(band_daily_fast=5, band_daily_slow=10, band_daily_min_width=0.005)
    r = detect_with(daily_closes(rally=6), p)
    assert r.state in ("bandstart", "none", "base", "fading", "breakout", "expansion")  # 파라미터가 전달돼 예외 없이 동작


def test_band_and_both_modes_do_not_leak_into_dip_entries():
    from tests.test_core import dip_snap

    assert FlowStrategy(Params()).decide(ctx(dip_snap())).reason == "dip_buy"  # 기본(flow)은 그대로
    assert FlowStrategy(Params(entry_mode="either")).decide(ctx(dip_snap())).reason == "dip_buy"
    for mode in ("band", "both"):
        assert FlowStrategy(Params(entry_mode=mode)).decide(ctx(dip_snap())).kind == "hold"  # 단일 기준 모드에서는 저점매수 없음


def test_band_only_positions_ignore_flow_reversal_but_other_modes_do_not():
    weak = make_snap(band=bsnap(), price=70100, score60=-50.0, score10=-10.0)  # 밭은 살아 있고 수급은 꺾임
    pos = Position("A", 10, 70000.0, at(9, 56), peak=70000.0, mode="band")
    assert FlowStrategy(Params(entry_mode="band")).decide(ctx(weak, pos, now=at(10, 0))).kind == "hold"
    assert FlowStrategy(Params(entry_mode="both")).decide(ctx(weak, pos, now=at(10, 0))).reason == "flow_reversal"
    assert FlowStrategy(Params(entry_mode="band", band_exit=False)).decide(ctx(weak, pos, now=at(10, 0))).reason == "flow_reversal"
