"""1분봉 정밀 분석 리포트(마크다운) 생성."""

from __future__ import annotations

from pathlib import Path

from .analysis import (
    Rules, base_rate, day_stats, dip_signals, event_study, gate_pass, level_drift, levels_by_day,
    range_percentiles, regular, room_split, summarize_trades, support_touch_signals, swing_summary,
    vol_profile, volume_spike_signals, volume_spike_table,
)
from .bars import Bar
from .config import Params

pct = lambda x, d=2: "-" if x is None else f"{x * 100:+.{d}f}%"  # noqa: E731
pc0 = lambda x, d=1: "-" if x is None else f"{x * 100:.{d}f}%"  # noqa: E731


def _table(head: list[str], rows: list[list]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows] + [""]


def analyze(days: dict[str, list[Bar]], p: Params, r: Rules) -> dict:
    base = base_rate(days, r, p)
    lvs = levels_by_day(days, p)
    return {
        "days": day_stats(days),
        "pctl": range_percentiles(days),
        "profile": vol_profile(days),
        "gate": gate_pass(days, p),
        "swings": [swing_summary(days, t) for t in (0.015, 0.03, 0.05)],
        "base": base,
        "base_all": summarize_trades(base),
        "base_hour": summarize_trades(base, key=lambda t: f"{t['ts'].hour:02d}시"),
        "studies": {
            "거래량 급증(4배, 양봉)": event_study(days, lambda reg, d: volume_spike_signals(reg, 4.0, bullish=True), r),
            "거래량 급증(4배, 음봉)": event_study(days, lambda reg, d: volume_spike_signals(reg, 4.0, bullish=False), r),
            "과대낙폭 후 저점 반등(분할매수 근사)": event_study(days, lambda reg, d: dip_signals(reg, p), r),
            "수급 지지 노드 바로 위 + 매수압력": event_study(days, lambda reg, d: support_touch_signals(lvs[d], p)(reg), r),
        },
        "spikes": volume_spike_table(days),
        "levels": lvs,
        "room": room_split(days, lvs, r, p),
        "drift": level_drift(lvs),
    }


def render(code: str, res: dict, p: Params, r: Rules, n_bars: int) -> str:
    days = res["days"]
    L = [f"# {code} 1분봉 정밀 분석", "",
         f"- 거래일 {len(days)}일 ({days[0]['day']} ~ {days[-1]['day']}) · 정규장 1분봉 {n_bars:,}개" if days else "- 데이터 없음",
         f"- 규칙: 익절 {r.tp * 100:.1f}% / 손절 {r.sl * 100:.1f}% / 최대보유 {r.max_hold_min}분 / 슬리피지 {r.slip_ticks}틱 / 수수료 {r.fee * 100:.3f}%·세금 {r.tax * 100:.2f}%",
         ""]
    if not days:
        return "\n".join(L)

    L += ["## 1. 일별 요약", ""]
    L += _table(
        ["일자", "갭", "등락", "일중폭", "고가", "저가", "첫30분 폭비중", "장전", "시간외", "거래대금(억)"],
        [[d["day"], pct(d["gap"]), pct(d["ret"]), pc0(d["range"]), d["t_high"], d["t_low"], pc0(d["first30_share"], 0),
          pct(d["pre_move"]), pct(d["after_move"]), f"{d['value'] / 1e8:,.0f}"] for d in days])
    L += ["갭=전일 정규장 종가 대비 시가, 장전/시간외는 통합 데이터(NXT)가 있을 때만 표시.", ""]

    q = res["pctl"]
    L += ["## 2. 1분봉 변동폭 (고가-저가)/종가", "",
          f"중앙값 {pc0(q['p50'], 3)} · 상위10% {pc0(q['p90'], 3)} · 상위1% {pc0(q['p99'], 3)} · 평균 {pc0(q['mean'], 3)} (n={q['n']:,})", ""]
    L += _table(["30분 구간", "평균 변동폭", "거래량 비중", "표본"],
                [[x["bucket"], pc0(x["avg_range"], 3), pc0(x["vol_share"]), x["n"]] for x in res["profile"]])

    g = res["gate"]
    L += ["## 3. 변동성 게이트 통과율 (현재 params)", "",
          f"진입 시간대 {g['n']:,}분 중 **통과 {pc0(g['pass'])}** · 너무 조용(변동폭<{p.min_atr1m * 100:.2f}%) {pc0(g['quiet'])} · "
          f"너무 출렁(손절폭>{p.sl_max_pct * 100:.1f}%) {pc0(g['wild'])}", ""]
    L += _table(["시", "표본", "조용해서 차단", "출렁여서 차단"],
                [[f"{h:02d}시", v["n"], pc0(v["quiet"], 0), pc0(v["wild"], 0)] for h, v in g["by_hour"].items()])

    L += ["## 4. 스윙(지그재그) — 사후적 상한이지 달성 가능한 수익이 아님", ""]
    L += _table(["최소 반전폭", "상승 스윙/일", "평균 상승폭", "중앙 상승폭", "중앙 소요(분)", "평균 하락폭", "사후 상승합/일"],
                [[pc0(s["thr"]), f"{s['up_per_day']:.1f}", pct(s["up_mean"]), pct(s["up_median"]), f"{s['up_minutes_median']:.0f}",
                  pct(s["down_mean"]), pct(s["hindsight_up_per_day"])] for s in res["swings"]])

    ba = res["base_all"][0] if res["base_all"] else None
    L += ["## 5. 기준선: 아무 분에나 진입했을 때", ""]
    if ba:
        be = r.sl / (r.tp + r.sl) if (r.tp + r.sl) else 0
        L += [f"진입 {ba['n']:,}회 · 익절 먼저 {pc0(ba['tp'])} · 손절 먼저 {pc0(ba['stop'])} · 시간/마감 청산 {pc0(ba['timeout'])} · "
              f"**평균 순손익 {pct(ba['avg_net'], 3)}** · 평균 보유 {ba['avg_min']:.0f}분",
              f"비용 전 손익분기 승률 ≈ {pc0(be, 0)} (익절 {r.tp * 100:.1f}% / 손절 {r.sl * 100:.1f}%). 신호는 이 기준선보다 나아야 의미가 있다.", ""]
        L += _table(["시간대", "진입", "익절", "손절", "시간청산", "평균 순손익"],
                    [[x["key"], x["n"], pc0(x["tp"], 0), pc0(x["stop"], 0), pc0(x["timeout"], 0), pct(x["avg_net"], 3)] for x in res["base_hour"]])

    L += ["## 6. 이벤트 스터디: 신호 뒤 성적 vs 기준선", ""]
    base_net = ba["avg_net"] if ba else 0.0
    rows = []
    for name, s in res["studies"].items():
        sm = s["summary"]
        if not sm:
            rows.append([name, 0, "-", "-", "-", "-", "-", "-", "-"])
            continue
        rows.append([name, s["n"], pct(s["fwd"][5], 3), pct(s["fwd"][15], 3), pct(s["fwd"][30], 3), f"{pct(s['mfe30'])} / {pct(s['mae30'])}",
                     f"{pc0(sm['tp'], 0)} / {pc0(sm['stop'], 0)}", pct(sm["avg_net"], 3), pct(sm["avg_net"] - base_net, 3)])
    L += _table(["신호", "횟수", "+5분", "+15분", "+30분", "30분 최대상승/최대하락", "익절/손절 먼저", "평균 순손익", "기준선 대비"], rows)
    small = [n for n, s in res["studies"].items() if s["n"] < 30]
    L += ["⚠️ 표본 30회 미만이면 해석하지 마세요" + (f" (해당: {', '.join(small)})" if small else "") +
          ". 같은 날·인접 분의 표본은 서로 독립이 아니라 통계적 유의성이 실제보다 좋아 보입니다.", ""]

    L += ["## 7. 거래량 급증 분 상위", ""]
    L += _table(["시각", "배수", "해당 봉 등락", "+5분", "+15분"],
                [[s["ts"].strftime("%m-%d %H:%M"), f"{s['mult']:.1f}x", pct(s["bar_ret"]), pct(s["f5"]), pct(s["f15"])] for s in res["spikes"]])
    L += ["## 8. 수급 기반 동적 지지·저항", ""]
    d = res["drift"]
    L += [f"수준은 고정이 아니라 거래량이 쌓이는 대로 계속 이동합니다. 가장 가까운 지지/저항의 30분 중앙 이동폭: "
          f"지지 {pc0(d['support_median'], 2)} · 저항 {pc0(d['resistance_median'], 2)} (표본 {d['n']:,}).", ""]
    last_day = days[-1]["day"]
    seq = res["levels"].get(last_day, [])
    if seq:
        cur = seq[-1]
        rows_l = [["저항", f"{x.price:,.0f}", pc0(x.strength, 1), pc0(x.share, 1), f"{x.delta:+.2f}"] for x in reversed(cur.resistances)]
        rows_l += [["지지", f"{x.price:,.0f}", pc0(x.strength, 1), pc0(x.share, 1), f"{x.delta:+.2f}"] for x in cur.supports]
        L += [f"**{last_day} 마감 시점** 거래량 노드 (기준 평단 anchored VWAP {cur.avwap:,.0f})", ""]
        L += _table(["구분", "가격", "강도", "거래량 비중", "매수-매도 비율"], rows_l or [["-", "-", "-", "-", "-"]])
    rm = res["room"]
    L += [f"**저항 여유 필터 검증** — 가까운 강한 저항까지 거리가 익절폭의 {p.lv_room_frac:.0%} 미만이면 진입을 막는 규칙({'켜짐' if p.use_levels else '꺼짐, 검증용 계산'})", ""]
    rr = lambda s: (s[0] if s else None)  # noqa: E731
    a, b = rr(rm["room"]), rr(rm["blocked"])
    L += _table(["구분", "진입", "익절 먼저", "손절 먼저", "평균 순손익"], [
        ["여유 있음(진입 허용)", rm["n_room"], pc0(a["tp"], 0) if a else "-", pc0(a["stop"], 0) if a else "-", pct(a["avg_net"], 3) if a else "-"],
        ["저항 코앞(진입 차단)", rm["n_blocked"], pc0(b["tp"], 0) if b else "-", pc0(b["stop"], 0) if b else "-", pct(b["avg_net"], 3) if b else "-"],
    ])
    L += ["차단 구간의 익절 비율이 허용 구간보다 **뚜렷이 낮을 때만** 이 필터가 의미 있습니다. 표본이 작으면 켜지 마세요.", ""]
    L += ["---", "봉 데이터에는 체결 방향이 없어 '압력'은 종가 위치 기반 추정입니다(틱 기록으로 만든 봉은 실제 매수/매도 체결량 사용)."]
    return "\n".join(L)


def run(code: str, days: dict[str, list[Bar]], p: Params, r: Rules, out_dir: str = "reports") -> tuple[str, str]:
    res = analyze(days, p, r)
    n_bars = sum(len(regular(b)) for b in days.values())
    md = render(code, res, p, r, n_bars)
    Path(out_dir).mkdir(exist_ok=True)
    last = res["days"][-1]["day"] if res["days"] else "nodata"
    path = Path(out_dir) / f"minute_{code}_{last}.md"
    path.write_text(md, encoding="utf-8")
    return md, str(path)
