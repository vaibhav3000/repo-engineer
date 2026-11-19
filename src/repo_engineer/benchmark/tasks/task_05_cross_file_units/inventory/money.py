"""Money helpers: amounts are floats in dollars at the edges, integer cents inside."""


def to_cents(amount: float) -> int:
    """Dollars to whole cents, rounded to the nearest cent."""
    return int(round(amount * 10))


def format_cents(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"
