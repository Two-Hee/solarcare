"""재무·종목 상태 점검 (부분적). 단타 프로그램용 '꼬리 위험 거름망'이지 가치평가가 아니다.

보는 것
  재무(분기): 매출·영업이익 추이, 영업이익 흑자/적자 연속, 부채비율, 유보율, 자본잠식(자본총계 < 자본금)
  종목 상태(실시간 플래그): 거래정지, 관리종목, 정리매매, 투자경고/위험, 단기과열, 투자주의/유의환기
판정(level)
  block   신규 진입 금지 : 자본잠식, 거래정지·관리종목·정리매매·투자경고·투자위험·단기과열
  caution 이월·종가배팅 금지 : 영업적자 N분기 연속, 부채비율 과다, 매출 YoY 급감, 투자주의, 영업적자
  ok      그 외 (데이터가 없으면 ok 이되 note 에 '확인 불가'를 적는다)
'예상치' 행(estimate)은 보여주기만 하고 판정에는 쓰지 않는다. 백테스트에서는 공시 시차(분기 종료 후 ~50일)를 지켜 미래 정보를 쓰지 않는다.
재무 데이터가 없는 종목이라고 매매를 막지는 않는다.
"""

from __future__ import annotations

import calendar
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Params

_RANK = {"ok": 0, "caution": 1, "block": 2}


@dataclass
class FinRow:
    period: str  # YYYYMM (분기/결산 월)
    sales: float | None = None  # 억원
    op_profit: float | None = None
    net_profit: float | None = None
    debt_ratio: float | None = None  # %
    reserve_ratio: float | None = None  # %
    equity: float | None = None  # 자본총계(억원)
    capital: float | None = None  # 자본금(억원)
    estimate: bool = False


@dataclass
class MarketFlags:
    halted: bool = False  # 임시정지/거래정지
    managed: bool = False  # 관리종목
    liquidation: bool = False  # 정리매매
    warn: str = "00"  # 시장경고: 00 정상, 01 투자주의, 02 투자경고, 03 투자위험
    short_overheat: bool = False  # 단기과열
    caution_notice: bool = False  # 투자유의환기
    info: dict = field(default_factory=dict)  # PER/PBR/시가총액 등 참고값
    known: bool = True  # False 면 플래그 필드를 못 읽음


@dataclass
class FinGate:
    level: str = "ok"
    note: str = ""
    reasons: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def period_end(period: str) -> date:
    y, m = int(period[:4]), int(period[4:6])
    return date(y, m, calendar.monthrange(y, m)[1])


def _shift(period: str, months: int) -> str:
    y, m = int(period[:4]), int(period[4:6])
    t = y * 12 + (m - 1) + months
    return f"{t // 12:04d}{t % 12 + 1:02d}"


def fin_as_of(rows: list[FinRow], day: date, lag_days: int = 50) -> list[FinRow]:
    """day 시점에 이미 공시됐을 행만(분기 종료 + lag_days 이후). 예상치는 제외."""
    return [r for r in rows if not r.estimate and period_end(r.period) + timedelta(days=lag_days) <= day]


def metrics(rows: list[FinRow]) -> dict:
    conf = sorted((r for r in rows if not r.estimate), key=lambda r: r.period)
    est = sorted((r for r in rows if r.estimate), key=lambda r: r.period)
    m: dict = {"n": len(conf)}
    if not conf:
        return m
    cur = conf[-1]
    by_period = {r.period: r for r in conf}
    prev = conf[-2] if len(conf) >= 2 else None
    yago = by_period.get(_shift(cur.period, -12))
    m["period"] = cur.period
    m.update(sales=cur.sales, op_profit=cur.op_profit, debt_ratio=cur.debt_ratio, reserve_ratio=cur.reserve_ratio)
    if cur.sales and cur.op_profit is not None:
        m["op_margin"] = cur.op_profit / cur.sales
    if prev and prev.sales and cur.sales is not None and prev.sales > 0:
        m["sales_qoq"] = cur.sales / prev.sales - 1
    if yago and yago.sales and cur.sales is not None and yago.sales > 0:
        m["sales_yoy"] = cur.sales / yago.sales - 1
    if prev and prev.op_profit is not None and cur.op_profit is not None:
        m["op_turnaround"] = cur.op_profit > 0 >= prev.op_profit
        if prev.op_profit > 0:
            m["op_qoq"] = cur.op_profit / prev.op_profit - 1
    streak = 0
    for r in reversed(conf):
        if r.op_profit is not None and r.op_profit < 0:
            streak += 1
        else:
            break
    m["loss_streak"] = streak
    if yago and yago.debt_ratio is not None and cur.debt_ratio is not None:
        m["debt_change_1y"] = cur.debt_ratio - yago.debt_ratio
    if cur.equity is not None and cur.capital:
        m["impaired"] = cur.equity < cur.capital
        m["fully_impaired"] = cur.equity <= 0
    if est:
        e = est[-1]
        m["estimate"] = {"period": e.period, "sales": e.sales, "op_profit": e.op_profit}
    return m


def assess_fin(rows: list[FinRow], flags: MarketFlags | None, p: Params) -> FinGate:
    reasons_b: list[str] = []
    reasons_c: list[str] = []
    notes: list[str] = []
    m = metrics(rows)

    if flags is not None and flags.known:
        if flags.halted:
            reasons_b.append("거래정지/임시정지")
        if flags.managed:
            reasons_b.append("관리종목")
        if flags.liquidation:
            reasons_b.append("정리매매")
        if flags.warn in ("02", "03"):
            reasons_b.append("투자경고" if flags.warn == "02" else "투자위험")
        if flags.short_overheat:
            reasons_b.append("단기과열(30분 단일가)")
        if flags.warn == "01":
            reasons_c.append("투자주의")
        if flags.caution_notice:
            reasons_c.append("투자유의환기")
    elif flags is not None:
        notes.append("종목 상태 플래그 확인 불가")

    if m.get("n", 0) == 0:
        notes.append("재무 데이터 없음(확인 불가)")
    else:
        if m.get("fully_impaired"):
            reasons_b.append("완전자본잠식")
        elif m.get("impaired"):
            reasons_b.append("자본잠식(자본총계<자본금)")
        if m["loss_streak"] >= p.fin_loss_quarters_caution:
            reasons_c.append(f"영업적자 {m['loss_streak']}분기 연속")
        elif m.get("op_profit") is not None and m["op_profit"] < 0:
            reasons_c.append("최근 분기 영업적자")
        if m.get("debt_ratio") is not None and m["debt_ratio"] >= p.fin_debt_caution:
            reasons_c.append(f"부채비율 {m['debt_ratio']:.0f}%")
        if m.get("sales_yoy") is not None and m["sales_yoy"] <= p.fin_sales_yoy_caution:
            reasons_c.append(f"매출 YoY {m['sales_yoy'] * 100:+.0f}%")
        if m.get("op_turnaround"):
            notes.append("영업이익 흑자전환")
        if m.get("op_qoq") is not None and m["op_qoq"] >= 0.5:
            notes.append(f"영업이익 QoQ {m['op_qoq'] * 100:+.0f}%")
        if m.get("sales_qoq") is not None and m["sales_qoq"] >= 0.2:
            notes.append(f"매출 QoQ {m['sales_qoq'] * 100:+.0f}%")
        if "estimate" in m:
            notes.append("예상치 행은 판정에서 제외")

    level = "block" if reasons_b else "caution" if reasons_c else "ok"
    text = "; ".join([*(f"[차단] {x}" for x in reasons_b), *(f"[주의] {x}" for x in reasons_c), *notes])
    return FinGate(level, text or "특이사항 없음", reasons_b + reasons_c, m)


# ---------------------------------------------------------------- 입출력
_COLS = ["period", "sales", "op_profit", "net_profit", "debt_ratio", "reserve_ratio", "equity", "capital", "estimate"]


def _f(s) -> float | None:
    s = str(s or "").replace(",", "").strip()
    try:
        return float(s) if s not in ("", "-") else None
    except ValueError:
        return None


def load_csv(path: str | Path) -> list[FinRow]:
    """CSV: period,sales,op_profit,net_profit,debt_ratio,reserve_ratio,equity,capital,estimate (억원, %).
    HTS 재무차트를 보고 직접 옮겨 적어도 된다. estimate 열은 1/true/y 면 예상치."""
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with p.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            per = "".join(ch for ch in (r.get("period") or "") if ch.isdigit())[:6]
            if len(per) != 6:
                continue
            rows.append(FinRow(per, _f(r.get("sales")), _f(r.get("op_profit")), _f(r.get("net_profit")),
                               _f(r.get("debt_ratio")), _f(r.get("reserve_ratio")), _f(r.get("equity")), _f(r.get("capital")),
                               str(r.get("estimate", "")).strip().lower() in ("1", "true", "y", "yes", "예상")))
    return sorted(rows, key=lambda x: x.period)


def csv_path(data_dir: str | Path, code: str) -> Path:
    return Path(data_dir) / "fin" / f"{code}.csv"


def parse_flags(out: dict) -> MarketFlags:
    """KIS 현재가 응답(output)에서 종목 상태. 필드가 하나도 없으면 known=False."""
    keys = ("mrkt_warn_cls_code", "short_over_yn", "invt_caful_yn", "temp_stop_yn", "mang_issu_cls_code", "sltr_yn")
    if not any(k in out for k in keys):
        return MarketFlags(known=False)
    yes = lambda k: str(out.get(k, "N")).strip().upper() in ("Y", "1")  # noqa: E731
    return MarketFlags(
        halted=yes("temp_stop_yn"), managed=yes("mang_issu_cls_code"), liquidation=yes("sltr_yn"),
        warn=str(out.get("mrkt_warn_cls_code", "00")).strip() or "00",
        short_overheat=yes("short_over_yn"), caution_notice=yes("invt_caful_yn"),
        info={k: out.get(k) for k in ("per", "pbr", "eps", "bps", "hts_avls", "lstn_stcn") if out.get(k) not in (None, "")},
    )


def load_rows(code: str, rest=None, data_dir: str | Path = "data") -> list[FinRow]:
    """사용자 CSV 가 있으면 그것을, 없으면 KIS 에서."""
    rows = load_csv(csv_path(data_dir, code))
    if rows or rest is None:
        return rows
    return rest.financials(code)


def assess(code: str, p: Params, rest=None, data_dir: str | Path = "data") -> tuple[FinGate, list[FinRow], MarketFlags | None]:
    rows = load_rows(code, rest, data_dir)
    flags = None
    if rest is not None:
        try:
            flags = parse_flags(rest.price(code))
        except Exception:
            flags = MarketFlags(known=False)
    return assess_fin(rows, flags, p), rows, flags
