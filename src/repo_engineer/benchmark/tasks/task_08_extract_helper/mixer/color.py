"""RGB channels are integers from 0 to 255."""


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def brighten(channel: int, amount: int) -> int:
    return int(clamp(channel + amount, 0, 255))
