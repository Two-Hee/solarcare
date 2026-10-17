"""종목별 파라미터 보정 제안. 어떤 종목이든 '그 종목 자신의 1분봉'에서 성질을 재서 제안한다.

제안하는 것(모두 사람이 검토 후 params/종목코드.json 으로 저장 — 자동 적용 없음)
  min_atr1m        하루 중 가장 조용한 25% 구간을 거르는 1분 변동폭 하한
  dip_min_drop_pct 15분 낙폭 분포 상위 15% 이상을 '과대낙폭'으로
  max_chase_pct    5분 상승률 분포 상위 10% 이상은 추격 금지
  overnight_gap_pct 정규장 마감~다음 정규장 사이 불리한 움직임의 관측 최대치(x1.25). 리스크를 푸는 방향(기본값보다 낮춤)은 표본 60개 이상일 때만
참고로만 보여주는 것: (익절, 손절)별 무작위 진입 기준선 표. 추세 없는 가격에서는 어떤 조합이든 기대값이 -비용이므로 조합 간 차이는
  표본 구간의 추세/시간청산 효과일 뿐이라 **선택 근거로 쓰지 않는다**(익절은 사용자 목표, 손절폭은 sl_atr_mult 로 변동성에 연동).
이 값들은 '지금까지의 이 종목'이 어땠는지를 요약할 뿐 미래를 보장하지 않는다. 표본이 적으면 제안 자체를 하지 않는다.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict
from pathlib import Path

from .analysis import Rules, atr_at, base_rate, day_stats, regular, summarize_trades, _entry_ok
from .bars import Bar
from .config import PARAM_BOUNDS, Params
from .costs import tick_size

TP_GRID = (0.010, 0.013, 0.016, 0.020)
SL_GRID = (0.006, 0.008, 0.010, 0.013, 0.016)
MIN_DAYS = 5
MIN_BARS = 1500


def _pctl(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(len(s) * q))]


def _clamp(name: str, v: float) -> float:
    lo, hi = PARAM_BOUNDS.get(name, (None, None))
    if lo is not None:
        v = min(max(v, lo), hi)
    return v


def overnight_adverse(days: dict[str, list[Bar]]) -> list[float]:
    """날짜별 '정규장 마감 후~다음 정규장 시작' 사이의 불리한(하락) 최대 움직임(양수). 장전/시간외 봉이 있으면 포함."""
    rows = day_stats(days)
    out = []
    for prev, cur in zip(rows, rows[1:]):
        moves = [cur["gap"]] if cur["gap"] is not None else []
        if prev.get("after_low") is not None:
            moves.append(prev["after_low"])
        if cur.get("pre_move") is not None:
            moves.append(cur["pre_move"])
        if moves:
            out.append(max(0.0, -min(moves)))
    return out


def calibrate(days: dict[str, list[Bar]], p: Params, slip_ticks: int | None = None) -> dict:
    regs = {d: regular(b) for d, b in days.items() if regular(b)}
    n_bars = sum(len(r) for r in regs.values())
    res: dict = {"days": len(regs), "bars": n_bars, "suggest": {}, "notes": [], "table": []}
    if len(regs) < MIN_DAYS or n_bars < MIN_BARS:
        res["notes"].append(f"표본 부족(거래일 {len(regs)}일 / 봉 {n_bars}개). 최소 {MIN_DAYS}일 · {MIN_BARS}봉이 필요해 제안하지 않습니다.")
        return res

    atrs, dd15, up5, closes = [], [], [], []
    for reg in regs.values():
        for i, b in enumerate(reg):
            closes.append(b.close)
            if _entry_ok(b, p):
                a = atr_at(reg, i, p.atr_minutes)
                if a > 0:
                    atrs.append(a)
                w = reg[max(0, i - 15) : i + 1]
                dd15.append(1 - b.close / max(x.high for x in w))
                if i >= 5 and reg[i - 5].close:
                    up5.append(b.close / reg[i - 5].close - 1)
    price = statistics.median(closes)
    sl = p.slip_ticks if slip_ticks is None else slip_ticks
    rt_cost = 2 * sl * tick_size(price) / price + 2 * p.fee_rate + p.sell_tax
    res["price"], res["round_trip_cost"] = price, rt_cost
    res["atr_median"] = statistics.median(atrs) if atrs else 0.0

    if len(atrs) >= 300:
        res["suggest"]["min_atr1m"] = round(_clamp("min_atr1m", _pctl(atrs, 0.25)), 5)
    pos_up = [x for x in up5 if x > 0]
    if len(dd15) >= 300:
        res["suggest"]["dip_min_drop_pct"] = round(_clamp("dip_min_drop_pct", _pctl(dd15, 0.85)), 4)
    if len(pos_up) >= 100:
        res["suggest"]["max_chase_pct"] = round(_clamp("max_chase_pct", _pctl(pos_up, 0.90)), 4)

    adv = overnight_adverse(days)
    res["overnight_n"] = len(adv)
    if adv:
        worst = max(adv)
        sug = min(0.08, max(0.015, worst * 1.25))
        res["overnight_worst"] = worst
        if len(adv) < 60 and sug < p.overnight_gap_pct:
            res["notes"].append(
                f"이월 갭 표본이 {len(adv)}개뿐이라(관측 최대 {worst * 100:.2f}%) 리스크를 푸는 방향(제안 {sug * 100:.1f}% < 현재 {p.overnight_gap_pct * 100:.1f}%)은 "
                "제안하지 않고 현재 값을 유지합니다. 표본 60개 이상에서 다시 보정하세요."
            )
        else:
            res["suggest"]["overnight_gap_pct"] = round(sug, 4)
            if len(adv) < 20:
                res["notes"].append(f"이월 갭 표본이 {len(adv)}개뿐입니다. 관측 최대×1.25 로 잡았지만 더 긴 기간으로 다시 보정하세요.")
    else:
        res["notes"].append("이월 갭을 계산할 수 없습니다(2거래일 이상 필요).")

    for tp in TP_GRID:
        for slx in SL_GRID:
            tr = base_rate(days, Rules(tp=tp, sl=slx, max_hold_min=p.max_hold_min, slip_ticks=sl, fee=p.fee_rate, tax=p.sell_tax), p, step=2)
            sm = (summarize_trades(tr) or [None])[0]
            if not sm:
                continue
            row = {"tp": tp, "sl": slx, "n": sm["n"], "tp_first": sm["tp"], "stop_first": sm["stop"], "avg_net": sm["avg_net"]}
            res["table"].append(row)
    if res["table"] and max(r["avg_net"] for r in res["table"]) <= 0:
        res["notes"].append("무작위 진입 기준선은 어떤 (익절, 손절) 조합에서도 비용 반영 순손익이 0 이하입니다. 신호의 우위가 이 비용을 넘는지 analyze 로 확인하기 전에는 매매하지 마세요.")
    return res


def write_override(code: str, suggest: dict, dir_: str | Path = "params") -> Path:
    path = Path(dir_) / f"{code}.json"
    cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cur.update(suggest)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def render(code: str, res: dict, p: Params) -> str:
    pc = lambda x, d=2: f"{x * 100:.{d}f}%"  # noqa: E731
    L = [f"# {code} 종목별 파라미터 보정 제안", "", f"- 거래일 {res['days']}일 · 정규장 1분봉 {res['bars']:,}개", ""]
    if "price" in res:
        L += [f"- 중앙 가격 {res['price']:,.0f}원 · **왕복 비용 약 {pc(res['round_trip_cost'])}** (슬리피지 {p.slip_ticks}틱씩 + 수수료 + 세금) · 1분 변동폭 중앙 {pc(res['atr_median'], 3)}", ""]
    cur = asdict(p)
    if res["suggest"]:
        L += ["| 파라미터 | 현재 | 제안 | 근거 |", "|---|---|---|---|"]
        why = {
            "min_atr1m": "진입 시간대 15분 평균 변동폭의 25% 분위", "dip_min_drop_pct": "15분 낙폭 분포의 85% 분위",
            "max_chase_pct": "5분 상승률(양수) 분포의 90% 분위", "overnight_gap_pct": f"마감~시작 불리한 움직임 최대 {pc(res.get('overnight_worst', 0))} x1.25 (n={res.get('overnight_n', 0)})",
        }
        for k, v in res["suggest"].items():
            L.append(f"| {k} | {cur[k]} | **{v}** | {why.get(k, '')} |")
        L.append("")
    if res["table"]:
        L += ["## 참고: (익절, 손절)별 무작위 진입 성적 (비용 반영) — 선택 근거로 쓰지 마세요", "",
              "추세 없는 가격에서는 어떤 조합이든 기대값이 -비용이라, 조합 간 차이는 표본 구간의 추세와 시간청산 효과입니다.", "",
              "| 익절 | 손절 | 진입 | 익절 먼저 | 손절 먼저 | 평균 순손익 |", "|---|---|---|---|---|---|"]
        for r in res["table"]:
            L.append(f"| {pc(r['tp'], 1)} | {pc(r['sl'], 1)} | {r['n']:,} | {pc(r['tp_first'], 0)} | {pc(r['stop_first'], 0)} | {r['avg_net'] * 100:+.3f}% |")
        L.append("")
    for n in res["notes"]:
        L.append(f"- ⚠️ {n}")
    L += ["", "제안은 **이 종목의 지나간 데이터**를 요약한 값입니다. 검토 후 `calibrate --write` 로 `params/종목코드.json` 에 저장하세요(공통 `params.json` 은 건드리지 않습니다)."]
    return "\n".join(L)
