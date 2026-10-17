from __future__ import annotations

from dataclasses import dataclass


def tick_size(price: float) -> int:
    """KRX 주식 호가단위(2023.1 개편, 코스피·코스닥 공통). ETF 는 다름."""
    if price < 2_000:
        return 1
    if price < 5_000:
        return 5
    if price < 20_000:
        return 10
    if price < 50_000:
        return 50
    if price < 200_000:
        return 100
    if price < 500_000:
        return 500
    return 1_000


@dataclass(frozen=True)
class CostModel:
    fee_rate: float = 0.00015  # 편도 위탁수수료(증권사별 상이)
    sell_tax: float = 0.0018  # 매도 시 거래세+농특세. 세율은 매년 바뀌니 확인 필요
    slip_ticks: int = 1  # 호가를 넘겨 체결된다고 가정하는 추가 슬리피지(틱)

    def fee(self, notional: float) -> float:
        return notional * self.fee_rate

    def tax(self, notional: float) -> float:
        return notional * self.sell_tax
