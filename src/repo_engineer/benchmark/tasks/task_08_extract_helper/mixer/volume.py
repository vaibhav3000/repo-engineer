"""Volume is a float from 0.0 (silent) to 1.0 (full)."""


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def louder(level: float, step: float = 0.1) -> float:
    return clamp(level + step, 0.0, 1.0)
