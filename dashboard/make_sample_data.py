"""대시보드용 샘플 데이터: 합성 틱 30거래일을 프로그램의 실제 코드 경로(백테스트/분석)로 돌린 결과를 dashboard/sample/*.csv 로 저장.
사용: 저장소 루트에서  python dashboard/make_sample_data.py   (합성 데이터이므로 수익성의 근거가 아님)"""
import csv, logging, sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

logging.disable(logging.CRITICAL)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daytrader.analysis import (Rules, day_stats, squeeze_signals_by_day, squeeze_study, vol_profile)
from daytrader.bars import load_days, save_by_day, ticks_to_bars
from daytrader.config import Params
from daytrader.recorder import list_days, load_day
from daytrader.replay import load_all, run_replay
from daytrader.synth import generate

ROOT = Path(__file__).resolve().parent
DATA, OUT = ROOT / "_synth", ROOT / "sample"
OUT.mkdir(exist_ok=True)
CODE, CASH = "161000", 10_000_000

import shutil
shutil.rmtree(DATA, ignore_errors=True)  # 항상 깨끗한 폴더에서 시작
generate(CODE, 30, DATA, seed=11)
for d in list_days(DATA, [CODE]):
    save_by_day(DATA, CODE, ticks_to_bars(load_day(DATA, d, [CODE])))

def write(name, header, rows):
    with (OUT / f"{name}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(name, len(rows), "rows")

ticks = load_all(DATA, [CODE])
minute = load_days(DATA, CODE)
base = Params(overnight_new_entry=False, band_tf_min=5)
MODES = [("squeeze", "압축→확장 돌파"), ("flow", "수급 점수"), ("band", "이평 밭 5분 5/20")]

mode_rows, trade_rows = [], []
for mode, label in MODES:
    eng, m = run_replay([CODE], ticks, replace(base, entry_mode=mode), CASH, DATA)
    n_days = len(ticks)
    mode_rows.append([mode, label, m["trades"], round(m["trades"] / n_days, 3), round(m["win_rate"], 4), round(m["avg_net_pct"], 5),
                      round(min(m["profit_factor"], 99.0), 3), round(m["total_return"], 5), round(m["max_dd"], 5)])
    cum = 0.0
    for i, t in enumerate(sorted(eng.trades, key=lambda x: x["exit_ts"]), 1):
        cum += t["net"] / CASH
        trade_rows.append([mode, label, i, t["exit_ts"][:16].replace("T", " "), t["reason"], t.get("f_entry_reason", ""), round(t["net_pct"], 5),
                           round(t["hold_sec"] / 60, 1), round(cum, 5)])
write("modes", ["mode", "label", "trades", "per_day", "win_rate", "avg_net_pct", "profit_factor", "total_return", "max_dd"], mode_rows)
write("trades", ["mode", "label", "seq", "exit_ts", "reason", "entry_reason", "net_pct", "hold_min", "cum_return"], trade_rows)

sq = replace(base, entry_mode="squeeze")
r = Rules(tp=sq.tp_pct, sl=0.01, max_hold_min=sq.max_hold_min, slip_ticks=sq.slip_ticks, fee=sq.fee_rate, tax=sq.sell_tax)
study = squeeze_study(minute, sq, r)
names = {"not_ready": "데이터 준비 전", "no_compress": "압축 아님", "no_break": "박스 돌파 아님/늦음", "no_vol": "거래량 확장 없음",
         "no_flow": "매수 우위 없음", "bad_rr": "손익비 안 맞음", "stale": "같은 박스 재진입", "ok": "신호"}
order = list(names)
write("funnel", ["order", "stage", "label", "minutes"], [[i, k, names[k], study["funnel"].get(k, 0)] for i, k in enumerate(order, 1)])
ctrl = study["rows"][-1]["summary"]["avg_net"] if study["rows"][-1]["summary"] else 0.0
vrows = []
for x in study["rows"]:
    sm = x["summary"]
    vrows.append([x["label"], x["n"], round(x["median_risk"], 5), round(x["fwd"][30], 5) if sm else "", round(sm["tp"], 4) if sm else "",
                  round(sm["stop"], 4) if sm else "", round(sm["avg_net"], 5) if sm else "", round(sm["avg_net"] - ctrl, 5) if sm else ""])
write("variants", ["variant", "signals", "median_risk", "fwd30", "tp_first", "stop_first", "avg_net", "vs_control"], vrows)

write("profile", ["bucket", "avg_range", "vol_share", "bars"], [[x["bucket"], round(x["avg_range"], 5), round(x["vol_share"], 4), x["n"]] for x in vol_profile(minute)])
write("daily", ["day", "gap", "ret", "range", "t_high", "t_low", "value_eok"],
      [[f"{d['day'][:4]}-{d['day'][4:6]}-{d['day'][6:]}", "" if d["gap"] is None else round(d["gap"], 5), round(d["ret"], 5), round(d["range"], 5),
        d["t_high"], d["t_low"], round(d["value"] / 1e8)] for d in day_stats(minute)])

p = sq
write("rules", ["group", "item", "value", "note"], [
    ["진입(압축→확장 돌파)", "박스 길이", f"{p.sq_base_min}분", "최근 같은 날짜의 완성된 1분봉"],
    ["진입(압축→확장 돌파)", "압축 기준", f"직전 {p.sq_ref_min}분 대비 {p.sq_compress_ratio:.0%} 이하", "박스 평균 1분 변동폭 ÷ 기준 구간 평균"],
    ["진입(압축→확장 돌파)", "돌파", f"박스 고점 +{p.sq_break_pct * 100:.2f}% ~ +{p.sq_max_ext * 100:.1f}%", "그 이상 올라 있으면 늦은 진입으로 제외"],
    ["진입(압축→확장 돌파)", "거래량 확장", f"박스 분당 중앙값의 {p.sq_vol_mult:g}배", "최근 60초 거래량"],
    ["진입(압축→확장 돌파)", "매수 우위", f"delta ≥ {p.sq_min_delta:g}", "최근 60초 매수-매도 체결 비율"],
    ["진입(압축→확장 돌파)", "손익비", f"익절폭 ÷ 손절폭 ≥ {p.sq_min_rr:g}", f"손절폭 {p.sq_min_risk * 100:.2f}% 이상"],
    ["청산", "익절", f"+{p.tp_pct * 100:.1f}%", "비용 반영 전 가격 기준"],
    ["청산", "구조적 손절", "박스 중간 아래", f"박스 고점에서 박스 폭의 {p.sq_stop_frac:.0%} 아래 − {p.sq_stop_buffer * 100:.1f}%"],
    ["청산", "최대 보유", f"{p.max_hold_min}분", "그 전에 정리되지 않으면 시간청산"],
    ["청산", "당일 정리", p.flatten_time, "종가배팅을 끄면 이월 없음"],
    ["안전장치", "일일 손실 한도", f"-{p.daily_loss_limit * 100:.1f}%", "도달하면 전량 청산 후 당일 중단"],
    ["안전장치", "종목당 최대 비중", f"{p.position_frac:.0%}", "자본 대비"],
    ["안전장치", "거래당 위험 목표", f"{p.risk_per_trade * 100:.1f}%", "손절 시 손실 ÷ 자본, 수량 = 위험 ÷ 손절폭"],
    ["안전장치", "이월 노출 한도", f"{min(p.max_overnight_exposure, p.overnight_risk / p.overnight_gap_pct):.2%}", f"시간외 갭 {p.overnight_gap_pct:.0%} 가정, 손실 {p.overnight_risk:.2%} 이내"],
    ["안전장치", "뉴스·재무 게이트", "block 이면 신규 진입 금지", "caution 이면 이월 금지"],
    ["안전장치", "킬스위치", "STOP 파일", "있으면 즉시 중단·청산"],
])
print("days", len(ticks), "funnel", study["funnel"])
