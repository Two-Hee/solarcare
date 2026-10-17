from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path


def hms(s: str) -> int:
    """'HH:MM' 또는 'HH:MM:SS' -> 자정 기준 초."""
    parts = [int(x) for x in s.split(":")]
    parts += [0] * (3 - len(parts))
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def sod(ts: datetime) -> int:
    return ts.hour * 3600 + ts.minute * 60 + ts.second


@dataclass
class Params:
    """전략 파라미터. params.json 으로 덮어쓴다. LOCKED 항목은 AI/튜너가 못 건드린다."""

    # --- 진입(당일 단타)
    entry_start: str = "09:05"
    entry_end: str = "14:40"
    entry_score_1m: float = 35.0
    entry_score_10s: float = 25.0
    min_trades_1m: int = 15
    use_vwap_filter: bool = True
    vwap_tol: float = 0.002
    max_spread_ticks: int = 3
    max_chase_pct: float = 0.02  # 최근 5분 상승률이 이보다 크면 추격 금지
    cooldown_sec: int = 90
    max_trades_per_day: int = 6  # 종목당

    # --- 청산(당일)
    tp_pct: float = 0.013
    sl_pct: float = 0.008
    breakeven_trigger_pct: float = 0.008
    breakeven_lock_pct: float = 0.001
    exit_score_1m: float = -30.0
    min_hold_sec: int = 20
    max_hold_min: int = 45
    flatten_time: str = "15:15"  # 15:20 동시호가 전에 정리

    # --- 1박2일
    overnight_enabled: bool = True
    overnight_decide_time: str = "15:08"
    overnight_min_profit: float = 0.002
    overnight_min_score: float = 20.0
    overnight_keep_frac: float = 0.5  # 이익 중인 포지션 중 이월할 비율
    overnight_new_entry: bool = True  # 종가배팅(신규 이월 진입)
    overnight_entry_score: float = 45.0
    overnight_entry_frac: float = 0.3
    overnight_tp_pct: float = 0.02
    overnight_sl_pct: float = 0.012
    overnight_exit_time: str = "10:30"  # 이튿날 이 시각엔 무조건 정리
    require_investor_ok: bool = False  # True 면 외국인/기관 순매수 확인된 때만 이월·종가배팅

    # --- 자금/리스크 (LOCKED)
    position_frac: float = 0.45  # 종목당 최대 비중(자본 대비)
    daily_loss_limit: float = 0.02
    max_overnight_exposure: float = 0.35

    # --- 수급 분석
    eval_interval_sec: float = 1.0
    big_trade_floor_krw: int = 30_000_000
    big_trade_quantile: float = 0.95
    side_source: str = "feed"  # feed | quote | tick  (체결 방향 판정 방식)
    w_delta: float = 0.35
    w_big: float = 0.25
    w_ob: float = 0.15
    w_ret: float = 0.15
    w_absorb: float = 0.10

    # --- 비용
    fee_rate: float = 0.00015
    sell_tax: float = 0.0018
    slip_ticks: int = 1

    @classmethod
    def from_dict(cls, d: dict) -> "Params":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    @classmethod
    def load(cls, path: str | Path = "params.json") -> "Params":
        p = Path(path)
        if p.exists():
            try:
                return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{p} 를 읽을 수 없습니다(JSON 오류: {e}). 지우면 기본값으로 시작합니다.")
        return cls()

    def save(self, path: str | Path = "params.json") -> None:
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


# AI 제안/튜너가 바꿀 수 있는 범위. 여기 없는 항목은 변경 불가.
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "entry_score_1m": (15, 70),
    "entry_score_10s": (10, 60),
    "min_trades_1m": (5, 60),
    "tp_pct": (0.008, 0.025),
    "sl_pct": (0.004, 0.015),
    "breakeven_trigger_pct": (0.004, 0.012),
    "exit_score_1m": (-70, -10),
    "min_hold_sec": (5, 120),
    "max_hold_min": (10, 120),
    "cooldown_sec": (30, 600),
    "max_chase_pct": (0.005, 0.04),
    "overnight_min_score": (5, 60),
    "overnight_entry_score": (30, 80),
    "overnight_tp_pct": (0.01, 0.04),
    "overnight_sl_pct": (0.006, 0.02),
}

# 자금·리스크·안전장치 관련: 사람만 바꿀 수 있다.
LOCKED = {
    "position_frac",
    "daily_loss_limit",
    "max_overnight_exposure",
    "overnight_keep_frac",
    "overnight_entry_frac",
    "overnight_enabled",
    "overnight_new_entry",
    "require_investor_ok",
    "flatten_time",
    "overnight_exit_time",
    "fee_rate",
    "sell_tax",
    "slip_ticks",
}


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


KIS_BASES = {
    "mock": "https://openapivts.koreainvestment.com:29443",
    "real": "https://openapi.koreainvestment.com:9443",
}
KIS_WS = {
    "mock": "ws://ops.koreainvestment.com:31000",
    "real": "ws://ops.koreainvestment.com:21000",
}
REAL_GUARD = "I_UNDERSTAND_THE_RISK"


@dataclass
class Settings:
    app_key: str = ""
    app_secret: str = ""
    account_no: str = ""
    mode: str = "mock"
    ws_url: str = ""
    data_dir: str = "data"
    tr_buy: str = ""
    tr_sell: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        mode = os.getenv("KIS_MODE", "mock").strip().lower()
        if mode not in KIS_BASES:
            raise SystemExit(f"KIS_MODE 는 mock 또는 real 이어야 합니다 (현재: {mode})")
        if mode == "real" and os.getenv("DAYTRADER_ALLOW_REAL") != REAL_GUARD:
            raise SystemExit(
                "실전 모드는 막혀 있습니다. 모의투자로 충분히 검증한 뒤, 위험을 이해했다면 "
                f"환경변수 DAYTRADER_ALLOW_REAL={REAL_GUARD} 를 직접 설정하세요."
            )
        # 2024~2025 개편된 주문 TR_ID 가 기본값. 다르면 .env 로 덮어쓴다.
        d_buy, d_sell = ("VTTC0012U", "VTTC0011U") if mode == "mock" else ("TTTC0012U", "TTTC0011U")
        return cls(
            app_key=os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APP_SECRET", ""),
            account_no=os.getenv("KIS_ACCOUNT_NO", ""),
            mode=mode,
            ws_url=os.getenv("KIS_WS_URL", KIS_WS[mode]),
            data_dir=os.getenv("DAYTRADER_DATA_DIR", "data"),
            tr_buy=os.getenv("KIS_TR_BUY", d_buy),
            tr_sell=os.getenv("KIS_TR_SELL", d_sell),
        )

    @property
    def base_url(self) -> str:
        return KIS_BASES[self.mode]

    @property
    def cano(self) -> str:
        return self.account_no.replace("-", "")[:8]

    @property
    def acnt_prdt_cd(self) -> str:
        digits = self.account_no.replace("-", "")
        return digits[8:10] if len(digits) >= 10 else "01"

    def require_keys(self) -> None:
        missing = [n for n, v in (("KIS_APP_KEY", self.app_key), ("KIS_APP_SECRET", self.app_secret)) if not v]
        if missing:
            raise SystemExit(f".env 에 {', '.join(missing)} 를 설정하세요 (.env.example 참고).")
