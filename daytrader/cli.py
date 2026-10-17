from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from .config import Params, Settings, load_dotenv


def _fmt(m: dict) -> str:
    pf = m["profit_factor"]
    return (
        f"거래 {m['trades']}건 | 승률 {m['win_rate'] * 100:.1f}% | 평균 순손익 {m['avg_net_pct'] * 100:.3f}% | "
        f"PF {pf if pf == float('inf') else round(pf, 2)} | 순손익 {m['total_net']:,.0f}원 "
        f"({m['total_return'] * 100:.2f}%) | 최대낙폭 {m['max_dd'] * 100:.2f}%"
    )


def _params(a, codes):
    """공통 params.json + params/종목코드.json 덮어쓰기. 덮어쓴 종목은 알려 준다."""
    base, by_code = Params.load_for(codes, a.params)
    for c in by_code:
        print(f"[종목별 파라미터] {c}: params/{c}.json 적용")
    return base, by_code


def cmd_selftest(a) -> int:
    from .kis import KisRest

    s = Settings.from_env()
    rest = KisRest(s)
    rest.token()
    print(f"[OK] 토큰 발급 ({s.mode})")
    px = rest.price(a.code)
    print(f"[OK] 현재가 {a.code}: {px.get('stck_prpr')}원 (전일대비 {px.get('prdy_ctrt')}%)")
    cash, hold = rest.balance()
    print(f"[OK] 잔고 조회: 예수금 {cash:,.0f}원, 보유 {hold}")
    print(f"[OK] 투자자 수급(전일까지): {rest.investor_daily(a.code)}")
    print("읽기 전용 점검 완료. 주문은 나가지 않았습니다.")
    return 0


def cmd_probe(a) -> int:
    """웹소켓 원본 프레임과 파싱 결과를 N개 출력(필드 순서·체결방향 검증용)."""
    from .kis import KisRest
    from .kis_ws import KisStream, parse_frame

    s = Settings.from_env()
    rest = KisRest(s)
    stream = KisStream(s, rest.approval_key(), [a.code])
    stop = asyncio.Event()
    n = {"c": 0}

    def on_raw(raw: str) -> None:
        if raw[:1] in "{[":
            print("[CTRL]", raw[:160])
            return
        evs = parse_frame(raw)
        print("[RAW ]", raw[:200].replace("\n", " "))
        for e in evs:
            print("[PARS]", e)
        n["c"] += 1
        if n["c"] >= a.n:
            stop.set()

    asyncio.run(stream.run(lambda e: None, stop, on_raw))
    print("\n확인할 것: 체결가가 오르는 구간에서 side=+1(매수), 내리는 구간에서 -1 이 나오는지. 반대면 kis_ws._tick 의 1/5 매핑을 뒤집으세요.")
    return 0


def cmd_run(a) -> int:
    from .live import run_live

    s = Settings.from_env()
    p, by_code = _params(a, a.codes)
    eng = asyncio.run(run_live(a.codes, s, p, a.broker, a.cash, not a.no_record, a.until, by_code))
    print(_fmt(__import__("daytrader.replay", fromlist=["metrics"]).metrics(eng.trades, a.cash)))
    return 0


def cmd_record(a) -> int:
    from .kis import KisRest
    from .kis_ws import KisStream
    from .models import now_kst
    from .recorder import Recorder
    from .config import hms, sod
    from .models import Tick

    s = Settings.from_env()
    rest = KisRest(s)
    rec = Recorder(s.data_dir)
    stream = KisStream(s, rest.approval_key(), a.codes)
    stop = asyncio.Event()
    cnt = {"t": 0}

    def on_event(ev) -> None:
        (rec.tick if isinstance(ev, Tick) else rec.quote)(ev)
        cnt["t"] += 1

    async def watcher() -> None:
        while not stop.is_set():
            await asyncio.sleep(5)
            if sod(now_kst()) >= hms(a.until):
                stop.set()

    async def main() -> None:
        await asyncio.gather(stream.run(on_event, stop), watcher())

    try:
        asyncio.run(main())
    finally:
        rec.close()
    print(f"기록 이벤트 {cnt['t']}건 -> {s.data_dir}/ticks/")
    return 0


def cmd_synth(a) -> int:
    from .synth import generate

    days = generate(a.code, a.days, a.out, seed=a.seed)
    print(f"합성 데이터 {len(days)}일 생성: {a.out}/ticks/  ({days[0]} ~ {days[-1]})")
    print("※ 합성 데이터의 성과는 수익성의 증거가 아닙니다. 파이프라인 점검용입니다.")
    return 0


def cmd_backtest(a) -> int:
    from .replay import load_all, run_replay

    p, by_code = _params(a, a.codes)
    days = load_all(a.data, a.codes)
    if not days:
        print(f"{a.data}/ticks 에 {a.codes} 데이터가 없습니다. record 또는 synth 를 먼저 실행하세요.")
        return 1
    eng, m = run_replay(a.codes, days, p, a.cash, a.data, params_by_code=by_code)
    print(f"{len(days)}거래일 | {_fmt(m)}")
    carried = [t for t in eng.trades if t["carried"]]
    if carried:
        from .replay import metrics

        print(f"  └ 1박2일(이월) 거래: {_fmt(metrics(carried, a.cash))}")
    if a.save:
        Path("logs").mkdir(exist_ok=True)
        with open(a.save, "w", encoding="utf-8") as fh:
            for t in eng.trades:
                fh.write(json.dumps(t, ensure_ascii=False) + "\n")
        print(f"거래 내역 저장: {a.save}")
    return 0


def cmd_review(a) -> int:
    from .review import run_review

    p = Params.load(a.params)
    r = run_review(a.trades, p, a.cash, use_ai=not a.no_ai)
    print(f"리포트: {r['report_path']}")
    if r["ai"] is None and not a.no_ai:
        print("(ANTHROPIC_API_KEY 가 없어 AI 분석은 건너뜀)")
    if r["proposal"]:
        print("AI 제안(미적용):", r["proposal"])
    return 0


def cmd_news(a) -> int:
    from .news import assess

    rest = None
    if not a.offline:
        from .kis import KisRest

        rest = KisRest(Settings.from_env())
    g = assess(a.code, rest, a.data, use_ai=not a.no_ai)
    print(f"[{g.level.upper()}] {g.note}")
    if g.summary:
        print("AI 요약:", g.summary)
    return 0


def cmd_bars_fetch(a) -> int:
    from datetime import datetime, timedelta

    from .bars import minute_path, save_bars
    from .kis import KisRest

    rest = KisRest(Settings.from_env())
    d, end = datetime.strptime(a.start, "%Y%m%d"), datetime.strptime(a.end, "%Y%m%d")
    got = 0
    while d <= end:
        day = d.strftime("%Y%m%d")
        if d.weekday() < 5:
            bars = rest.minute_bars(a.code, day, a.session)
            if bars:
                save_bars(minute_path(Settings.from_env().data_dir, a.code, day), bars)
                got += 1
                print(f"{day}: 1분봉 {len(bars)}개 ({bars[0].ts:%H:%M}~{bars[-1].ts:%H:%M})")
            else:
                print(f"{day}: 데이터 없음(휴장일이거나 미지원)")
        d += timedelta(days=1)
    print(f"{got}일 저장 -> data/minute/{a.code}/")
    return 0 if got else 1


def cmd_bars_import(a) -> int:
    from .bars import import_csv, save_by_day

    bars = import_csv(a.file)
    days = save_by_day(a.data, a.code, bars)
    print(f"{len(bars)}개 봉 -> {len(days)}일 ({days[0]} ~ {days[-1]}) 저장: {a.data}/minute/{a.code}/")
    return 0


def cmd_bars_build(a) -> int:
    from .bars import save_by_day, ticks_to_bars
    from .recorder import list_days, load_day

    days = list_days(a.data, [a.code])
    if not days:
        print(f"{a.data}/ticks 에 {a.code} 기록이 없습니다. record 를 먼저 실행하세요.")
        return 1
    n = 0
    for d in days:
        bars = ticks_to_bars(load_day(a.data, d, [a.code]))
        save_by_day(a.data, a.code, bars)
        n += len(bars)
    print(f"틱 {len(days)}일 -> 1분봉 {n}개 (매수/매도 체결량 포함) 저장")
    return 0


def cmd_analyze(a) -> int:
    from .analysis import Rules
    from .bars import load_days
    from .minute_report import run

    days = load_days(a.data, a.code, a.since, a.until, a.last)
    if not days:
        print(f"{a.data}/minute/{a.code} 에 1분봉이 없습니다. bars-fetch / bars-import / bars-build 중 하나로 먼저 만드세요.")
        return 1
    p = Params.load(a.params)
    r = Rules(tp=a.tp, sl=a.sl, max_hold_min=a.hold, slip_ticks=a.slip, fee=p.fee_rate, tax=p.sell_tax)
    md, path = run(a.code, days, p, r)
    print(md)
    print(f"\n리포트 저장: {path}")
    return 0


def cmd_calibrate(a) -> int:
    from .bars import load_days
    from .calibrate import calibrate, render, write_override

    days = load_days(a.data, a.code, a.since, a.until, a.last)
    if not days:
        print(f"{a.data}/minute/{a.code} 에 1분봉이 없습니다. bars-fetch / bars-import / bars-build 로 먼저 만드세요.")
        return 1
    p = Params.load(a.params)
    res = calibrate(days, p)
    print(render(a.code, res, p))
    if a.write and res["suggest"]:
        print(f"\n저장: {write_override(a.code, res['suggest'])}  (이후 run/backtest 가 이 종목에 자동 적용)")
    elif a.write:
        print("\n제안이 없어 저장하지 않았습니다.")
    return 0


def cmd_fin(a) -> int:
    from .fin import assess, load_csv, csv_path, assess_fin

    p = Params.load(a.params)
    if a.offline:
        rows = load_csv(csv_path(a.data, a.code))
        gate, flags = assess_fin(rows, None, p), None
    else:
        from .kis import KisRest

        rest = KisRest(Settings.from_env())
        gate, rows, flags = assess(a.code, p, rest, a.data)
        if a.raw:
            raw = rest.price(a.code)
            keys = ("mrkt_warn_cls_code", "short_over_yn", "invt_caful_yn", "temp_stop_yn", "mang_issu_cls_code", "sltr_yn", "vi_cls_code", "per", "pbr", "hts_avls")
            print("현재가 응답의 상태 필드 원본:", {k: raw.get(k, "<없음>") for k in keys})
    if not rows:
        print("재무 데이터가 없습니다. KIS 조회가 안 되면 data/fin/종목코드.csv 에 직접 입력하세요(README 참고).")
    else:
        print(f"{'분기':>8} {'매출(억)':>9} {'영업이익':>9} {'영업이익률':>8} {'부채비율':>8} {'유보율':>9}  비고")
        for r in rows[-8:]:
            om = f"{r.op_profit / r.sales * 100:.1f}%" if r.sales and r.op_profit is not None else "-"
            f = lambda v, d=0: "-" if v is None else f"{v:,.{d}f}"  # noqa: E731
            print(f"{r.period:>8} {f(r.sales):>9} {f(r.op_profit):>9} {om:>8} {f(r.debt_ratio, 1):>8} {f(r.reserve_ratio, 0):>9}  {'예상치' if r.estimate else ''}")
    if flags is not None:
        print(f"종목 상태: 경고코드 {flags.warn} · 단기과열 {flags.short_overheat} · 관리 {flags.managed} · 정리매매 {flags.liquidation} · "
              f"정지 {flags.halted} · 유의환기 {flags.caution_notice}" + ("" if flags.known else "  (플래그 필드를 읽지 못함 - --raw 로 확인)"))
        if flags.info:
            print("참고(PER/PBR/시총 등):", flags.info)
    print(f"\n[{gate.level.upper()}] {gate.note}")
    return 0


def cmd_regime(a) -> int:
    from .regime import detect, load_daily_bars

    if a.csv:
        bars = load_daily_bars(a.csv)
    else:
        from .kis import KisRest

        bars = KisRest(Settings.from_env()).daily_bars(a.code)
    r = detect(bars)
    print(f"[{r.state.upper()}] {r.note}")
    print(f"  신호: {r.signals}")
    print(f"  일봉 {len(bars)}개 ({bars[0].date} ~ {bars[-1].date})" if bars else "  일봉 없음")
    return 0


def cmd_tune(a) -> int:
    from .tune import DEFAULT_GRID, VOL_GRID, tune

    r = tune(a.codes, a.data, Params.load(a.params), VOL_GRID if a.vol else DEFAULT_GRID, initial_cash=a.cash)
    print(f"후보 {r['candidates_tested']}개 검증. 기준선 검증구간: {_fmt(r['baseline']['valid'])}")
    if r["best"]:
        print(f"제안: {r['params']}  (거부: {r['rejected']})")
        print(f"  학습: {_fmt(r['best']['train'])}")
        print(f"  검증: {_fmt(r['best']['valid'])}")
    else:
        print("현재 파라미터를 검증 구간에서 이기는 후보가 없습니다 -> 변경하지 않는 것이 맞습니다.")
    print(f"저장: {r['path']}")
    return 0


def cmd_apply(a) -> int:
    from .guard import apply_proposal

    r = apply_proposal(a.file, a.params, a.approve)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_params(a) -> int:
    p = Params.load(a.params)
    print(json.dumps(asdict(p), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="daytrader", description="수급 기반 1~2종목 단타 + 1박2일 매매")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--params", default="params.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("selftest", cmd_selftest, "KIS 연결 점검(읽기 전용)")
    sp.add_argument("code")
    sp = add("probe", cmd_probe, "웹소켓 원본 프레임/파싱 확인")
    sp.add_argument("code")
    sp.add_argument("-n", type=int, default=30)
    sp = add("record", cmd_record, "틱/호가만 기록(매매 없음)")
    sp.add_argument("codes", nargs="+")
    sp.add_argument("--until", default="15:31")
    sp = add("run", cmd_run, "실시간 매매 (기본: 가상체결 paper)")
    sp.add_argument("codes", nargs="+", help="1~2종목 코드")
    sp.add_argument("--broker", choices=["paper", "kis-mock"], default="paper")
    sp.add_argument("--cash", type=float, default=10_000_000)
    sp.add_argument("--no-record", action="store_true")
    sp.add_argument("--until", default="15:31")
    sp = add("synth", cmd_synth, "합성 데이터 생성(점검용)")
    sp.add_argument("code")
    sp.add_argument("--days", type=int, default=8)
    sp.add_argument("--out", default="data/synth")
    sp.add_argument("--seed", type=int, default=7)
    sp = add("backtest", cmd_backtest, "기록된 틱으로 백테스트")
    sp.add_argument("codes", nargs="+")
    sp.add_argument("--data", default="data")
    sp.add_argument("--cash", type=float, default=10_000_000)
    sp.add_argument("--save", default="")
    sp = add("review", cmd_review, "거래 복기 리포트 (+AI 분석)")
    sp.add_argument("--trades", default="logs/trades.jsonl")
    sp.add_argument("--cash", type=float, default=10_000_000)
    sp.add_argument("--no-ai", action="store_true")
    sp = add("news", cmd_news, "뉴스 게이트 판정(키워드 규칙 + 선택적 AI)")
    sp.add_argument("code")
    sp.add_argument("--data", default="data")
    sp.add_argument("--offline", action="store_true", help="KIS 호출 없이 data/news/CODE.txt 만 사용")
    sp.add_argument("--no-ai", action="store_true")
    sp = add("bars-fetch", cmd_bars_fetch, "KIS 에서 과거 1분봉 내려받기")
    sp.add_argument("code")
    sp.add_argument("start", help="YYYYMMDD")
    sp.add_argument("end", help="YYYYMMDD")
    sp.add_argument("--session", choices=["regular", "all"], default="regular", help="all=08:00~20:00(통합)")
    sp = add("bars-import", cmd_bars_import, "HTS 에서 내려받은 1분봉 CSV 가져오기(한글 헤더 허용)")
    sp.add_argument("code")
    sp.add_argument("file")
    sp.add_argument("--data", default="data")
    sp = add("bars-build", cmd_bars_build, "record 로 기록한 틱 -> 1분봉(매수/매도 체결량 포함)")
    sp.add_argument("code")
    sp.add_argument("--data", default="data")
    sp = add("analyze", cmd_analyze, "1분봉 정밀 분석 리포트")
    sp.add_argument("code")
    sp.add_argument("--data", default="data")
    sp.add_argument("--since", default="")
    sp.add_argument("--until", default="")
    sp.add_argument("--last", type=int, default=0, help="최근 N거래일만")
    sp.add_argument("--tp", type=float, default=0.013)
    sp.add_argument("--sl", type=float, default=0.010)
    sp.add_argument("--hold", type=int, default=45, help="최대 보유(분)")
    sp.add_argument("--slip", type=int, default=1, help="슬리피지(틱)")
    sp = add("calibrate", cmd_calibrate, "종목별 파라미터 보정 제안(자기 1분봉 기반)")
    sp.add_argument("code")
    sp.add_argument("--data", default="data")
    sp.add_argument("--since", default="")
    sp.add_argument("--until", default="")
    sp.add_argument("--last", type=int, default=0)
    sp.add_argument("--write", action="store_true", help="제안을 params/종목코드.json 에 저장(사람이 검토한 뒤에만)")
    sp = add("fin", cmd_fin, "재무·종목 상태 점검(부분 분석)")
    sp.add_argument("code")
    sp.add_argument("--data", default="data")
    sp.add_argument("--offline", action="store_true", help="KIS 호출 없이 data/fin/종목코드.csv 만 사용")
    sp.add_argument("--raw", action="store_true", help="종목 상태 필드 원본 출력(필드명 검증용)")
    sp = add("regime", cmd_regime, "일봉 국면 감지(횡보 후 거래대금 폭발 돌파)")
    sp.add_argument("code")
    sp.add_argument("--csv", default="", help="date,open,high,low,close,value CSV (없으면 KIS 에서 조회)")
    sp = add("tune", cmd_tune, "워크포워드 튜닝(제안만 생성)")
    sp.add_argument("--vol", action="store_true", help="변동성 큰 종목용 탐색 격자")
    sp.add_argument("codes", nargs="+")
    sp.add_argument("--data", default="data")
    sp.add_argument("--cash", type=float, default=10_000_000)
    sp = add("apply", cmd_apply, "승인된 제안을 params.json 에 반영")
    sp.add_argument("file")
    sp.add_argument("--approve", action="store_true")
    add("params", cmd_params, "현재 파라미터 출력")
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if getattr(args, "codes", None) and args.cmd in ("run",) and len(args.codes) > 2:
        print("이 프로그램은 1~2종목에 집중하도록 설계됐습니다.", file=sys.stderr)
        return 2
    return args.fn(args)
