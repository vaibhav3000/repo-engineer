"""Invoice calculation."""

TAX_RATE = 0.20


def total_with_tax(amount: float) -> float:
    return round(amount * (1 + TAX_RATE), 2)
