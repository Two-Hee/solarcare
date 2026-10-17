"""KIS 실시간 웹소켓: H0STCNT0(체결) / H0STASP0(호가).

주의: 필드 순서는 KIS 공식 문서 기준으로 작성했지만 실제 계정에서 검증되지 않았다.
처음에는 `python -m daytrader probe 005930` 으로 원본 프레임과 파싱 결과를 눈으로 확인할 것.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
from typing import Awaitable, Callable

from .config import Settings
from .models import Quote, Tick, now_kst

log = logging.getLogger("daytrader.ws")

TICK_FIELDS = [
    "MKSC_SHRN_ISCD", "STCK_CNTG_HOUR", "STCK_PRPR", "PRDY_VRSS_SIGN", "PRDY_VRSS", "PRDY_CTRT",
    "WGHN_AVRG_STCK_PRC", "STCK_OPRC", "STCK_HGPR", "STCK_LWPR", "ASKP1", "BIDP1", "CNTG_VOL",
    "ACML_VOL", "ACML_TR_PBMN", "SELN_CNTG_CSNU", "SHNU_CNTG_CSNU", "NTBY_CNTG_CSNU", "CTTR",
    "SELN_CNTG_SMTN", "SHNU_CNTG_SMTN", "CCLD_DVSN", "SHNU_RATE", "PRDY_VOL_VRSS_ACML_VOL_RATE",
    "OPRC_HOUR", "OPRC_VRSS_PRPR_SIGN", "OPRC_VRSS_PRPR", "HGPR_HOUR", "HGPR_VRSS_PRPR_SIGN",
    "HGPR_VRSS_PRPR", "LWPR_HOUR", "LWPR_VRSS_PRPR_SIGN", "LWPR_VRSS_PRPR", "BSOP_DATE",
    "NEW_MKOP_CLS_CODE", "TRHT_YN", "ASKP_RSQN1", "BIDP_RSQN1", "TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN",
    "VOL_TNRT", "PRDY_SMNS_HOUR_ACML_VOL", "PRDY_SMNS_HOUR_ACML_VOL_RATE", "HOUR_CLS_CODE",
    "MRKT_TRTM_CLS_CODE", "VI_STND_PRC",
]
ASK_FIELDS = (
    ["MKSC_SHRN_ISCD", "BSOP_HOUR", "HOUR_CLS_CODE"]
    + [f"ASKP{i}" for i in range(1, 11)]
    + [f"BIDP{i}" for i in range(1, 11)]
    + [f"ASKP_RSQN{i}" for i in range(1, 11)]
    + [f"BIDP_RSQN{i}" for i in range(1, 11)]
    + ["TOTAL_ASKP_RSQN", "TOTAL_BIDP_RSQN", "OVTM_TOTAL_ASKP_RSQN", "OVTM_TOTAL_BIDP_RSQN"]
)
FIELDS = {"H0STCNT0": TICK_FIELDS, "H0STASP0": ASK_FIELDS}


def _i(row: dict, name: str) -> int:
    v = row.get(name, "")
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _ts(row: dict, hour_key: str, today: date | None) -> datetime:
    hhmmss = (row.get(hour_key) or "000000").zfill(6)
    d = today or now_kst().date()
    ds = row.get("BSOP_DATE") or ""
    if len(ds) == 8 and ds.isdigit():
        d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:]))
    return datetime(d.year, d.month, d.day, int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6]))


def _tick(row: dict, today: date | None) -> Tick | None:
    price, vol = _i(row, "STCK_PRPR"), _i(row, "CNTG_VOL")
    if price <= 0 or vol <= 0:
        return None
    dv = row.get("CCLD_DVSN", "")
    side = 1 if dv == "1" else -1 if dv == "5" else 0  # 1: 매수체결, 5: 매도체결 (probe 로 검증)
    return Tick(_ts(row, "STCK_CNTG_HOUR", today), row["MKSC_SHRN_ISCD"], price, vol, side, _i(row, "ASKP1"), _i(row, "BIDP1"))


def _quote(row: dict, today: date | None) -> Quote | None:
    ask1, bid1 = _i(row, "ASKP1"), _i(row, "BIDP1")
    if ask1 <= 0 or bid1 <= 0:
        return None
    return Quote(
        _ts(row, "BSOP_HOUR", today), row["MKSC_SHRN_ISCD"], ask1, bid1,
        _i(row, "ASKP_RSQN1"), _i(row, "BIDP_RSQN1"), _i(row, "TOTAL_ASKP_RSQN"), _i(row, "TOTAL_BIDP_RSQN"),
    )


def parse_frame(raw: str, today: date | None = None) -> list:
    """프레임 1개 -> [Tick | Quote | dict(제어메시지)]"""
    if raw[:1] in ("{", "["):
        try:
            return [json.loads(raw)]
        except json.JSONDecodeError:
            return []
    parts = raw.split("|", 3)
    if len(parts) < 4:
        return []
    flag, tr_id, count, data = parts
    names = FIELDS.get(tr_id)
    if names is None or flag != "0":
        return []
    vals = data.split("^")
    try:
        n = max(1, int(count))
    except ValueError:
        n = 1
    nf = len(names)
    chunks = [vals] if n == 1 else [vals[i * nf : (i + 1) * nf] for i in range(n)]
    out = []
    for ch in chunks:
        if len(ch) < 12:
            continue
        row = dict(zip(names, ch))
        ev = _tick(row, today) if tr_id == "H0STCNT0" else _quote(row, today)
        if ev is not None:
            out.append(ev)
    return out


class KisStream:
    def __init__(self, settings: Settings, approval_key: str, codes: list[str]):
        self.s = settings
        self.key = approval_key
        self.codes = codes

    def _sub(self, tr_id: str, code: str) -> str:
        return json.dumps(
            {
                "header": {"approval_key": self.key, "custtype": "P", "tr_type": "1", "content-type": "utf-8"},
                "body": {"input": {"tr_id": tr_id, "tr_key": code}},
            }
        )

    async def run(
        self,
        on_event: Callable[[Tick | Quote], None],
        stop: asyncio.Event,
        on_raw: Callable[[str], None] | None = None,
    ) -> None:
        import websockets

        backoff = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(self.s.ws_url, ping_interval=None) as ws:
                    for c in self.codes:
                        await ws.send(self._sub("H0STCNT0", c))
                        await ws.send(self._sub("H0STASP0", c))
                    log.info("웹소켓 연결·구독 완료: %s", self.codes)
                    backoff = 1.0
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        if on_raw:
                            on_raw(raw)
                        for ev in parse_frame(raw):
                            if isinstance(ev, dict):
                                if ev.get("header", {}).get("tr_id") == "PINGPONG":
                                    await ws.pong(raw.encode())
                                elif ev.get("body", {}).get("rt_cd") not in (None, "0"):
                                    log.error("구독 오류: %s", ev)
                            else:
                                on_event(ev)
            except Exception as e:  # 재연결
                if stop.is_set():
                    break
                log.warning("웹소켓 끊김(%s). %.0fs 후 재연결", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2)
