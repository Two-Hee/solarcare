"""매일 장 마감 후 복기: 통계 -> 마크다운 리포트 -> (선택) Claude 의 분석/파라미터 제안.

Claude 는 '제안'만 한다. 제안은 guard 로 걸러지고, 적용은 사람이 승인해야 한다.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import Params
from .guard import validate_proposal
from .replay import metrics


def load_trades(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def _bucket(trades: list[dict], keyfn) -> dict:
    g: dict = defaultdict(list)
    for t in trades:
        g[keyfn(t)].append(t)
    out = {}
    for k, v in sorted(g.items(), key=lambda kv: str(kv[0])):
        m = metrics(v, 1.0)
        out[str(k)] = {"n": m["trades"], "win_rate": round(m["win_rate"], 3), "avg_net_pct": round(m["avg_net_pct"] * 100, 3)}
    return out


def summarize(trades: list[dict], start_equity: float) -> dict:
    m = metrics(trades, start_equity)
    return {
        "overall": {k: (round(v, 4) if isinstance(v, float) and v != float("inf") else v) for k, v in m.items()},
        "by_reason": _bucket(trades, lambda t: t["reason"]),
        "by_hour": _bucket(trades, lambda t: t["entry_ts"][11:13]),
        "by_carried": _bucket(trades, lambda t: "이월" if t.get("carried") else "당일"),
        "by_entry_reason": _bucket(trades, lambda t: t.get("f_entry_reason", "?")),
        "by_score60": _bucket(trades, lambda t: f"{int(t.get('f_score60', 0) // 10) * 10:+d}"),
        "by_code": _bucket(trades, lambda t: t["code"]),
    }


def render_markdown(s: dict, title: str) -> str:
    o = s["overall"]
    lines = [
        f"# {title}",
        "",
        f"- 거래 {o['trades']}건 · 승률 {o['win_rate'] * 100:.1f}% · 평균 순손익 {o['avg_net_pct'] * 100:.3f}% "
        f"· PF {o['profit_factor']} · 순손익 {o['total_net']:,.0f}원 · 최대낙폭 {o['max_dd'] * 100:.2f}%",
        "",
    ]
    for key, label in (
        ("by_reason", "청산 사유별"),
        ("by_carried", "당일 vs 이월"),
        ("by_entry_reason", "진입 유형별"),
        ("by_hour", "진입 시각별"),
        ("by_score60", "진입 시 1분 점수 구간별"),
        ("by_code", "종목별"),
    ):
        lines += [f"## {label}", "", "| 구분 | 건수 | 승률 | 평균 순손익% |", "|---|---|---|---|"]
        for k, v in s[key].items():
            lines.append(f"| {k} | {v['n']} | {v['win_rate'] * 100:.0f}% | {v['avg_net_pct']:.3f} |")
        lines.append("")
    return "\n".join(lines)


SYSTEM = """당신은 한국 주식 단타 자동매매 시스템의 퀀트 리뷰어입니다.
시스템은 체결 흐름(수급) 점수로 진입하고, +1.3% 익절/-0.8% 손절 및 1박2일 이월 규칙으로 청산합니다.
주어진 통계로 (1) 무엇이 돈을 벌고 잃는지, (2) 표본이 적어 결론 내리면 안 되는 부분, (3) 파라미터 조정안을 제시하세요.
규칙: 표본이 30건 미만이면 파라미터 변경을 제안하지 말고 '데이터 더 필요'라고 하세요.
자금/리스크 항목은 제안 대상이 아닙니다. 반드시 아래 JSON 한 개만 출력하세요(설명 문장 금지):
{"observations": ["..."], "param_suggestions": {"파라미터명": 값}, "need_more_data": true|false}"""


def ai_review(summary: dict, params: Params, model: str | None = None) -> dict | None:
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    client = anthropic.Anthropic()
    user = json.dumps(
        {"current_params": asdict(params), "stats": summary}, ensure_ascii=False, default=str
    )
    resp = client.messages.create(
        model=model or os.getenv("REVIEW_MODEL", "claude-opus-5-5"),
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        return {"error": "refusal"}
    text = "".join(b.text for b in resp.content if b.type == "text")
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        return {"error": "no_json", "raw": text[:500]}
    try:
        return json.loads(text[a : b + 1])
    except json.JSONDecodeError:
        return {"error": "bad_json", "raw": text[:500]}


def run_review(trades_path: str, params: Params, start_equity: float, out_dir: str = "reports", use_ai: bool = True) -> dict:
    trades = load_trades(trades_path)
    s = summarize(trades, start_equity)
    stamp = datetime.now().strftime("%Y%m%d")
    md = render_markdown(s, f"복기 리포트 {stamp}")
    ai = ai_review(s, params) if (use_ai and trades) else None
    result = {"summary": s, "ai": ai, "proposal": None}
    if ai and isinstance(ai.get("param_suggestions"), dict) and not ai.get("need_more_data"):
        ok, bad = validate_proposal(params, ai["param_suggestions"])
        result["proposal"] = {"params": ok, "rejected": bad}
        Path("proposals").mkdir(exist_ok=True)
        pp = Path("proposals") / f"ai_{stamp}.json"
        pp.write_text(json.dumps(result["proposal"], ensure_ascii=False, indent=2), encoding="utf-8")
        md += f"\n## AI 파라미터 제안(미적용)\n\n- 통과: {ok}\n- 거부: {bad}\n- 파일: `{pp}`\n"
    if ai and ai.get("observations"):
        md += "\n## AI 관찰\n\n" + "\n".join(f"- {x}" for x in ai["observations"]) + "\n"
    Path(out_dir).mkdir(exist_ok=True)
    rp = Path(out_dir) / f"{stamp}.md"
    rp.write_text(md, encoding="utf-8")
    result["report_path"] = str(rp)
    return result
