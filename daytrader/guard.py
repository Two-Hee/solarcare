"""AI/튜너가 낸 파라미터 변경안을 걸러내는 안전장치.

- LOCKED(자금·리스크·청산시각·수수료 등)는 절대 변경 불가
- PARAM_BOUNDS 범위 안에서만, 한 번에 최대 25% 까지만 이동
- 적용(apply)은 사람이 --approve 를 줄 때만
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .config import LOCKED, PARAM_BOUNDS, Params

MAX_REL_STEP = 0.25


def validate_proposal(current: Params, proposal: dict) -> tuple[dict, dict]:
    cur = asdict(current)
    ok: dict = {}
    bad: dict = {}
    for k, v in proposal.items():
        if k in LOCKED:
            bad[k] = "잠금 항목(사람만 변경 가능)"
        elif k not in PARAM_BOUNDS:
            bad[k] = "허용 목록에 없음"
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            bad[k] = "숫자가 아님"
        else:
            lo, hi = PARAM_BOUNDS[k]
            base = cur[k]
            if not (lo <= v <= hi):
                bad[k] = f"범위 초과 [{lo}, {hi}]"
            elif base != 0 and abs(v - base) / abs(base) > MAX_REL_STEP:
                bad[k] = f"한 번에 {int(MAX_REL_STEP * 100)}% 초과 변경 (현재 {base})"
            else:
                ok[k] = type(base)(v) if isinstance(base, int) and float(v).is_integer() else v
    return ok, bad


def apply_proposal(path: str | Path, params_path: str | Path = "params.json", approve: bool = False) -> dict:
    if not approve:
        raise SystemExit("적용하려면 --approve 가 필요합니다 (사람의 승인 단계).")
    prop = json.loads(Path(path).read_text(encoding="utf-8"))
    cur = Params.load(params_path)
    ok, bad = validate_proposal(cur, prop.get("params", prop))
    new = Params.from_dict({**asdict(cur), **ok})
    new.save(params_path)
    return {"applied": ok, "rejected": bad}
