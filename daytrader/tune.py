"""경계가 정해진 그리드 탐색 + 워크포워드 검증. 결과는 '제안 파일'로만 남기고 자동 적용하지 않는다.

과최적화 방지: (1) 앞 70% 구간에서 고르고 뒤 30% 구간에서 재검증, (2) 최소 거래 수,
(3) 현재 파라미터보다 검증 구간 성적이 나아야 제안.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from .config import Params
from .guard import validate_proposal
from .replay import load_all, run_replay

DEFAULT_GRID: dict[str, list] = {
    "tp_pct": [0.010, 0.013, 0.016],
    "sl_pct": [0.006, 0.008, 0.010],
    "entry_score_1m": [28.0, 35.0, 42.0],
    "entry_score_10s": [18.0, 25.0, 32.0],
}


def _score(m: dict) -> float:
    return m["expectancy_pct"] * math.sqrt(m["trades"]) if m["trades"] else -1e9


def tune(
    codes: list[str],
    data_dir: str,
    base: Params,
    grid: dict[str, list] | None = None,
    split: float = 0.7,
    min_train_trades: int = 20,
    min_valid_trades: int = 8,
    initial_cash: float = 10_000_000,
    out_dir: str = "proposals",
) -> dict:
    grid = grid or DEFAULT_GRID
    logging.getLogger("daytrader.engine").setLevel(logging.WARNING)  # 수백 번 재생하므로 매매 로그는 끈다
    all_days = load_all(data_dir, codes)
    days = sorted(all_days)
    if len(days) < 4:
        raise SystemExit(f"튜닝에는 최소 4거래일 데이터가 필요합니다 (현재 {len(days)}일). record 로 더 쌓으세요.")
    cut = max(1, int(len(days) * split))
    train = {d: all_days[d] for d in days[:cut]}
    valid = {d: all_days[d] for d in days[cut:]}

    _, base_train = run_replay(codes, train, base, initial_cash, data_dir)
    _, base_valid = run_replay(codes, valid, base, initial_cash, data_dir)

    keys = list(grid)
    results = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        cand = replace(base, **dict(zip(keys, combo)))
        _, m = run_replay(codes, train, cand, initial_cash, data_dir)
        if m["trades"] >= min_train_trades:
            results.append((_score(m), dict(zip(keys, combo)), m))
    results.sort(key=lambda r: r[0], reverse=True)

    best = None
    for _, combo, m_train in results[:5]:
        cand = replace(base, **combo)
        _, m_valid = run_replay(codes, valid, cand, initial_cash, data_dir)
        if m_valid["trades"] >= min_valid_trades and m_valid["expectancy_pct"] > base_valid["expectancy_pct"] > -1:
            best = {"combo": combo, "train": m_train, "valid": m_valid}
            break

    accepted: dict = {}
    rejected: dict = {}
    if best:
        changed = {k: v for k, v in best["combo"].items() if v != asdict(base)[k]}
        accepted, rejected = validate_proposal(base, changed)

    out = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "codes": codes,
        "days": {"train": list(train), "valid": list(valid)},
        "baseline": {"train": base_train, "valid": base_valid},
        "candidates_tested": len(results),
        "best": best,
        "params": accepted,
        "rejected": rejected,
        "note": "제안일 뿐입니다. 적용: python -m daytrader apply <파일> --approve",
    }
    Path(out_dir).mkdir(exist_ok=True)
    path = Path(out_dir) / f"tune_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    out["path"] = str(path)
    return out
