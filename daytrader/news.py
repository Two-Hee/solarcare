"""뉴스·공시 게이트.

판정은 두 단계다.
  1) 규칙(키워드): 악재성 키워드는 코드가 결정론적으로 block/caution 을 낸다. AI 가 낮출 수 없다.
  2) AI(선택): Claude 가 헤드라인을 읽고 재료의 성격·위험을 요약하고, 규칙보다 *더 보수적으로만* 등급을 올릴 수 있다.

헤드라인 출처: KIS 종합시황/공시 제목 API(모의서버 미지원일 수 있음) 또는 data/news/CODE.txt (한 줄에 한 헤드라인, 직접 붙여넣기).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

BLOCK_KEYWORDS = [
    "거래정지", "상장폐지", "관리종목", "횡령", "배임", "감사의견", "의견거절", "회생", "부도",
    "유상증자", "무상감자", "감자", "투자경고", "투자위험", "단기과열", "불성실공시", "상장적격성",
]
CAUTION_KEYWORDS = [
    "전환사채", "신주인수권", "교환사채", "최대주주 변경", "최대주주변경", "블록딜", "대량 매도", "대량매도",
    "투자주의", "소송", "공매도", "적자", "영업손실", "실적 쇼크", "어닝쇼크", "리콜", "화재", "압수수색",
]
_RANK = {"ok": 0, "caution": 1, "block": 2}


@dataclass
class NewsGate:
    level: str = "ok"
    note: str = ""
    hits: tuple[str, ...] = ()
    summary: str = ""


def classify_rules(headlines: list[str]) -> NewsGate:
    block = sorted({k for h in headlines for k in BLOCK_KEYWORDS if k in h})
    caution = sorted({k for h in headlines for k in CAUTION_KEYWORDS if k in h})
    if block:
        return NewsGate("block", f"악재 키워드: {', '.join(block)}", tuple(block))
    if caution:
        return NewsGate("caution", f"주의 키워드: {', '.join(caution)}", tuple(caution))
    return NewsGate("ok", "특이 악재 키워드 없음" if headlines else "헤드라인 없음(확인 불가)")


AI_SYSTEM = """당신은 한국 주식 단타 시스템의 뉴스 리스크 필터입니다. 헤드라인만 보고 판단하며 추측하지 않습니다.
출력은 JSON 한 개만: {"level": "ok|caution|block", "catalysts": ["상승 재료 요약"], "risks": ["하락/변동 위험"], "summary": "한 줄"}
- block: 거래정지·상장폐지·횡령/배임·유상증자 등 즉시 매매를 피해야 하는 사안
- caution: 재료는 있으나 CB/BW, 대주주 매도, 실적 악화, 이미 급등한 뒤의 소문성 재료 등 불확실성이 큰 경우
- 헤드라인이 없거나 판단 근거가 약하면 level 은 ok 로 두되 summary 에 '근거 부족'이라고 쓰세요."""


def ai_assess(code: str, headlines: list[str], model: str | None = None) -> dict | None:
    if not headlines or not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    resp = anthropic.Anthropic().messages.create(
        model=model or os.getenv("REVIEW_MODEL", "claude-opus-5-5"),
        max_tokens=1500,
        system=AI_SYSTEM,
        messages=[{"role": "user", "content": f"종목코드 {code}\n" + "\n".join(f"- {h}" for h in headlines[:40])}],
    )
    if resp.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in resp.content if b.type == "text")
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        return None
    try:
        return json.loads(text[a : b + 1])
    except json.JSONDecodeError:
        return None


def combine(rules: NewsGate, ai: dict | None) -> NewsGate:
    """AI 는 등급을 올릴 수만 있고, 규칙이 낸 등급을 낮출 수 없다."""
    if not ai:
        return rules
    lvl = ai.get("level", "ok")
    lvl = lvl if lvl in _RANK else "ok"
    final = lvl if _RANK[lvl] > _RANK[rules.level] else rules.level
    return NewsGate(final, rules.note, rules.hits, str(ai.get("summary", ""))[:300])


def headlines_from_file(code: str, data_dir: str | Path = "data") -> list[str]:
    p = Path(data_dir) / "news" / f"{code}.txt"
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]


def headlines_from_kis(rest, code: str, now: datetime | None = None) -> list[str]:
    """최근 이틀치 제목. API 미지원/오류면 빈 리스트."""
    now = now or datetime.now()
    since = (now - timedelta(days=1)).strftime("%Y%m%d")
    try:
        rows = rest.news_titles(code)
    except Exception:
        return []
    return [title for dt, title in rows if title and (not dt or dt >= since)]


def assess(code: str, rest=None, data_dir: str | Path = "data", use_ai: bool = True) -> NewsGate:
    heads = headlines_from_file(code, data_dir)
    if rest is not None:
        heads += headlines_from_kis(rest, code)
    gate = classify_rules(heads)
    return combine(gate, ai_assess(code, heads) if use_ai else None)
