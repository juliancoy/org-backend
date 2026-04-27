from __future__ import annotations

import math
import random
from decimal import Decimal, ROUND_DOWN
from typing import Any


class EconomicEngine:
    @staticmethod
    def _enum_value(value: Any) -> str:
        raw = getattr(value, "value", value)
        return str(raw).strip().lower()

    @staticmethod
    def calculate_tax(income: Decimal, entity_type: Any) -> Decimal:
        if EconomicEngine._enum_value(entity_type) == "nonprofit":
            return Decimal("0.00")

        tax_brackets = [
            (Decimal("0.00"), Decimal("25000.00"), Decimal("0.10")),
            (Decimal("25000.01"), Decimal("50000.00"), Decimal("0.15")),
            (Decimal("50000.01"), Decimal("100000.00"), Decimal("0.20")),
            (Decimal("100000.01"), Decimal("500000.00"), Decimal("0.25")),
            (Decimal("500000.01"), None, Decimal("0.30")),
        ]

        tax = Decimal("0.00")
        remaining_income = income
        for lower, upper, rate in tax_brackets:
            if remaining_income <= Decimal("0.00"):
                break
            if upper is None or remaining_income <= (upper - lower):
                tax += remaining_income * rate
                break
            bracket_income = upper - lower
            tax += bracket_income * rate
            remaining_income -= bracket_income

        return tax.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    @staticmethod
    def calculate_insurance_premium(insurance_type: Any, coverage_amount: Decimal, risk_factors: dict[str, Any]) -> Decimal:
        base_rates = {
            "life": Decimal("0.0005"),
            "health": Decimal("0.01"),
            "fire": Decimal("0.0015"),
            "acts_of_god": Decimal("0.002"),
        }
        insurance_key = EconomicEngine._enum_value(insurance_type)
        base_premium = coverage_amount * base_rates[insurance_key]

        risk_multiplier = Decimal("1.0")
        if insurance_key == "life":
            age = risk_factors.get("age", 35)
            if age > 60:
                risk_multiplier *= Decimal("2.5")
            elif age > 45:
                risk_multiplier *= Decimal("1.5")
            elif age < 25:
                risk_multiplier *= Decimal("0.7")
        elif insurance_key == "health":
            health_score = risk_factors.get("health_score", 75)
            if health_score < 50:
                risk_multiplier *= Decimal("2.0")
            elif health_score > 85:
                risk_multiplier *= Decimal("0.8")
        elif insurance_key == "fire":
            location_risk = risk_factors.get("location_risk", "medium")
            if location_risk == "high":
                risk_multiplier *= Decimal("2.0")
            elif location_risk == "low":
                risk_multiplier *= Decimal("0.8")

        return (base_premium * risk_multiplier).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    @staticmethod
    def calculate_stock_price_variation(
        current_price: Decimal,
        volume: int,
        market_sentiment: Decimal,
        volatility: Decimal = Decimal("0.02"),
    ) -> Decimal:
        z = Decimal(str(random.gauss(0, 1)))
        drift = (market_sentiment - Decimal("0.5")) * Decimal("0.01")
        volume_factor = Decimal(str(min(math.log(volume + 1) / 10, 0.1)))

        price_change = drift + (volatility * z) + volume_factor
        new_price = current_price * (Decimal("1.0") + price_change)
        min_price = current_price * Decimal("0.01")
        return max(new_price, min_price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
