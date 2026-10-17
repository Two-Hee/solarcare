"""한국투자증권 Open API (REST). 기본은 모의투자 서버.

엔드포인트/TR_ID 는 공식 문서 기준으로 작성했으나 실계정에서 검증되지 않았다.
먼저 `python -m daytrader selftest 005930` (읽기 전용: 토큰·현재가·잔고) 로 확인할 것.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

from .config import Settings
from .costs import CostModel, tick_size
from .models import DailyContext, Fill

log = logging.getLogger("daytrader.kis")


class KisError(RuntimeError):
    pass


class KisRest:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        settings.require_keys()
        self.s = settings
        self.http = session or requests.Session()
        self._min_interval = 0.55 if settings.mode == "mock" else 0.06  # 모의 초당 2건 제한
        self._last_call = 0.0
        self._token: str | None = None
        self._token_path = Path(f".kis_token_{settings.mode}.json")

    # ------------------------------------------------------------------ 인증
    def token(self) -> str:
        if self._token:
            return self._token
        if self._token_path.exists():
            try:
                d = json.loads(self._token_path.read_text())
                if d.get("expires_at", 0) > time.time() + 300 and d.get("appkey") == self.s.app_key[:8]:
                    self._token = d["token"]
                    return self._token
            except (OSError, json.JSONDecodeError):
                pass
        r = self.http.post(
            f"{self.s.base_url}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": self.s.app_key, "appsecret": self.s.app_secret},
            timeout=10,
        )
        if r.status_code != 200 or "access_token" not in r.json():
            raise KisError(f"토큰 발급 실패: {r.status_code} {r.text[:200]}")
        j = r.json()
        self._token = j["access_token"]
        self._token_path.write_text(
            json.dumps({"token": self._token, "expires_at": time.time() + int(j.get("expires_in", 86400)), "appkey": self.s.app_key[:8]})
        )
        return self._token

    def approval_key(self) -> str:
        r = self.http.post(
            f"{self.s.base_url}/oauth2/Approval",
            json={"grant_type": "client_credentials", "appkey": self.s.app_key, "secretkey": self.s.app_secret},
            timeout=10,
        )
        j = r.json()
        if "approval_key" not in j:
            raise KisError(f"웹소켓 접속키 발급 실패: {r.status_code} {r.text[:200]}")
        return j["approval_key"]

    # ------------------------------------------------------------------ 공통 호출
    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.token()}",
            "appkey": self.s.app_key,
            "appsecret": self.s.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _call(self, method: str, path: str, tr_id: str, params: dict | None = None, body: dict | None = None) -> dict:
        for attempt in range(3):
            wait = self._min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            r = self.http.request(
                method, f"{self.s.base_url}{path}", headers=self._headers(tr_id), params=params, json=body, timeout=10
            )
            if r.status_code in (429, 500, 502, 503):
                time.sleep(1.0 * (attempt + 1))
                continue
            try:
                j = r.json()
            except ValueError:
                raise KisError(f"JSON 아님: {r.status_code} {r.text[:200]}")
            if j.get("msg_cd") in ("EGW00123", "EGW00121"):  # 토큰 만료/오류
                self._token = None
                self._token_path.unlink(missing_ok=True)
                continue
            if r.status_code != 200 or j.get("rt_cd") not in (None, "0"):
                raise KisError(f"{path} 실패: {r.status_code} {j.get('msg_cd')} {j.get('msg1')}")
            return j
        raise KisError(f"{path} 재시도 초과")

    # ------------------------------------------------------------------ 시세
    def price(self, code: str) -> dict:
        j = self._call(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": self.s.market_div, "FID_INPUT_ISCD": code},
        )
        return j["output"]

    def investor_daily(self, code: str) -> DailyContext:
        """종목별 투자자 일별 순매수(주). 전일까지 확정분만 사용한다."""
        j = self._call(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900",
            params={"FID_COND_MRKT_DIV_CODE": self.s.market_div, "FID_INPUT_ISCD": code},
        )
        today = datetime.now().strftime("%Y%m%d")
        rows = [r for r in j.get("output", []) if r.get("stck_bsop_date", "") < today and r.get("frgn_ntby_qty", "") != ""]
        rows = rows[:5]
        if not rows:
            return DailyContext()

        def num(r, k):
            try:
                return int(r.get(k) or 0)
            except ValueError:
                return 0

        return DailyContext(
            num(rows[0], "frgn_ntby_qty"), num(rows[0], "orgn_ntby_qty"),
            sum(num(r, "frgn_ntby_qty") for r in rows), sum(num(r, "orgn_ntby_qty") for r in rows),
        )

    def daily_bars(self, code: str, days: int = 150):
        """최근 일봉(거래대금 포함). 오늘 진행 중인 봉은 제외한다."""
        from datetime import timedelta

        from .regime import DailyBar

        end = datetime.now()
        j = self._call(
            "GET", "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": self.s.market_div, "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": (end - timedelta(days=days)).strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"), "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
            },
        )
        today = end.strftime("%Y%m%d")
        bars = []
        for r in j.get("output2", []):
            d = r.get("stck_bsop_date", "")
            if not d or d >= today or not r.get("stck_clpr"):
                continue
            bars.append(DailyBar(d, float(r["stck_oprc"]), float(r["stck_hgpr"]), float(r["stck_lwpr"]),
                                 float(r["stck_clpr"]), float(r.get("acml_tr_pbmn") or 0)))
        return sorted(bars, key=lambda b: b.date)

    def minute_bars(self, code: str, day: str, session: str = "regular"):
        """특정 일자의 1분봉. 뒤(마감)에서 앞으로 페이지를 넘기며 모은다. 모의서버에서 미지원일 수 있다."""
        from .bars import Bar

        hour = "153000" if session == "regular" else "200000"
        floor = "090000" if session == "regular" else "080000"
        seen: dict[str, Bar] = {}
        for _ in range(14):
            j = self._call(
                "GET", "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice", "FHKST03010230",
                params={
                    "FID_COND_MRKT_DIV_CODE": self.s.market_div, "FID_INPUT_ISCD": code,
                    "FID_INPUT_HOUR_1": hour, "FID_INPUT_DATE_1": day,
                    "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": "",
                },
            )
            rows = [r for r in j.get("output2", []) if r.get("stck_bsop_date") == day and r.get("stck_cntg_hour")]
            fresh = 0
            for r in rows:
                hh = r["stck_cntg_hour"]
                if hh in seen:
                    continue
                fresh += 1
                seen[hh] = Bar(
                    datetime.strptime(day + hh, "%Y%m%d%H%M%S"), int(r["stck_oprc"]), int(r["stck_hgpr"]),
                    int(r["stck_lwpr"]), int(r["stck_prpr"]), int(r.get("cntg_vol") or 0), float(r.get("acml_tr_pbmn") or 0),
                )
            if not rows or fresh == 0:
                break
            earliest = min(r["stck_cntg_hour"] for r in rows)
            if earliest <= floor:
                break
            hour = earliest
        return sorted(seen.values(), key=lambda b: b.ts)

    def financials(self, code: str, quarterly: bool = True):
        """손익계산서 + 재무비율 + 대차대조표를 결산월로 합쳐 FinRow 목록으로. 금액은 억원."""
        from .fin import FinRow

        base = {
            "FID_DIV_CLS_CODE": "1" if quarterly else "0",
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": code,
        }

        def get(path, tr):
            return self._call("GET", f"/uapi/domestic-stock/v1/finance/{path}", tr, params=base).get("output", []) or []

        rows: dict[str, FinRow] = {}

        def fnum(r, k):
            try:
                v = str(r.get(k, "")).replace(",", "").strip()
                return float(v) if v not in ("", "-") else None
            except ValueError:
                return None

        for r in get("income-statement", "FHKST66430200"):
            per = r.get("stac_yymm", "")
            if len(per) == 6:
                x = rows.setdefault(per, FinRow(per))
                x.sales, x.op_profit, x.net_profit = fnum(r, "sale_account"), fnum(r, "bsop_prti"), fnum(r, "thtr_ntin")
        for r in get("financial-ratio", "FHKST66430300"):
            per = r.get("stac_yymm", "")
            if len(per) == 6:
                x = rows.setdefault(per, FinRow(per))
                x.debt_ratio, x.reserve_ratio = fnum(r, "lblt_rate"), fnum(r, "rsrv_rate")
        for r in get("balance-sheet", "FHKST66430100"):
            per = r.get("stac_yymm", "")
            if len(per) == 6:
                x = rows.setdefault(per, FinRow(per))
                x.equity, x.capital = fnum(r, "total_cptl"), fnum(r, "cpfn")
        return sorted(rows.values(), key=lambda x: x.period)

    def news_titles(self, code: str) -> list[tuple[str, str]]:
        """종합 시황/공시 제목. [(YYYYMMDD, 제목)]. 모의서버에서 미지원일 수 있다."""
        j = self._call(
            "GET", "/uapi/domestic-stock/v1/quotations/news-title", "FHKST01011800",
            params={
                "FID_NEWS_OFER_ENTP_CODE": "", "FID_COND_MRKT_CLS_CODE": "", "FID_INPUT_ISCD": code,
                "FID_TITL_CNTT": "", "FID_INPUT_DATE_1": "", "FID_INPUT_HOUR_1": "",
                "FID_RANK_SORT_CLS_CODE": "", "FID_INPUT_SRNO": "",
            },
        )
        return [(r.get("data_dt", ""), r.get("hts_pbnt_titl_cntt", "")) for r in j.get("output", [])]

    # ------------------------------------------------------------------ 계좌/주문
    def balance(self) -> tuple[float, dict[str, tuple[int, float]]]:
        """(주문가능 예수금, {종목: (보유수량, 평균단가)})"""
        j = self._call(
            "GET", "/uapi/domestic-stock/v1/trading/inquire-balance",
            "VTTC8434R" if self.s.mode == "mock" else "TTTC8434R",
            params={
                "CANO": self.s.cano, "ACNT_PRDT_CD": self.s.acnt_prdt_cd, "AFHR_FLPR_YN": "N", "OFL_YN": "",
                "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N", "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
            },
        )
        hold = {}
        for r in j.get("output1", []):
            q = int(r.get("hldg_qty") or 0)
            if q > 0:
                hold[r["pdno"]] = (q, float(r.get("pchs_avg_pric") or 0))
        o2 = (j.get("output2") or [{}])[0]
        cash = float(o2.get("dnca_tot_amt") or 0)
        return cash, hold

    def order(self, code: str, qty: int, side: str) -> str:
        tr = self.s.tr_buy if side == "buy" else self.s.tr_sell
        j = self._call(
            "POST", "/uapi/domestic-stock/v1/trading/order-cash", tr,
            body={
                "CANO": self.s.cano, "ACNT_PRDT_CD": self.s.acnt_prdt_cd, "PDNO": code,
                "ORD_DVSN": "01", "ORD_QTY": str(qty), "ORD_UNPR": "0", "EXCG_ID_DVSN_CD": "KRX",
            },
        )
        return (j.get("output") or {}).get("ODNO", "")


class KisBroker:
    """KIS 모의투자 계좌로 시장가 주문. 체결은 잔고 조회로 확인한다."""

    def __init__(self, rest: KisRest, cost: CostModel | None = None, cash_ttl: float = 30.0):
        self.rest = rest
        self.cost = cost or CostModel()
        self._cash: tuple[float, float] | None = None  # (값, 조회 시각) - 틱마다 REST 를 치지 않도록 캐시
        self._ttl = cash_ttl

    def cash(self) -> float:
        if self._cash is None or time.time() - self._cash[1] > self._ttl:
            self._cash = (self.rest.balance()[0], time.time())
        return self._cash[0]

    def _wait_fill(self, code: str, before_qty: int, want_up: bool, timeout: float = 6.0):
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.8)
            _, hold = self.rest.balance()
            q, avg = hold.get(code, (0, 0.0))
            if (q > before_qty) if want_up else (q < before_qty):
                return q, avg
        return None

    def buy(self, code: str, qty: int, ts: datetime, last: int, ask1: int, bid1: int) -> Fill | None:
        _, hold = self.rest.balance()
        bq, bavg = hold.get(code, (0, 0.0))
        self.rest.order(code, qty, "buy")
        got = self._wait_fill(code, bq, True)
        if not got:
            log.error("매수 체결 확인 실패(주문은 나갔을 수 있음): %s %d", code, qty)
            return None
        self._cash = None
        aq, aavg = got
        dq = aq - bq
        price = (aq * aavg - bq * bavg) / dq if dq else float(last)
        return Fill(code, "buy", dq, price, self.cost.fee(price * dq), 0.0, ts)

    def sell(self, code: str, qty: int, ts: datetime, last: int, ask1: int, bid1: int) -> Fill | None:
        _, hold = self.rest.balance()
        bq, _ = hold.get(code, (0, 0.0))
        if bq <= 0:
            return None
        qty = min(qty, bq)
        self.rest.order(code, qty, "sell")
        got = self._wait_fill(code, bq, False)
        if not got and self.rest.balance()[1].get(code, (0, 0.0))[0] >= bq:
            log.error("매도 체결 확인 실패: %s %d", code, qty)
            return None
        self._cash = None
        price = float(bid1 or last)  # 실제 체결가는 일별체결조회로 보정 필요(v1 은 호가 근사)
        notional = price * qty
        return Fill(code, "sell", qty, price, self.cost.fee(notional), self.cost.tax(notional), ts)
