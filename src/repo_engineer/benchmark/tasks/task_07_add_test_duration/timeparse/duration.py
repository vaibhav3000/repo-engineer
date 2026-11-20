import re

UNITS = {"h": 3600, "m": 60, "s": 1}
PART = re.compile(r"(\d+)([hms])")


def parse_duration(text: str) -> int:
    """Seconds in a duration such as 1h30m or 45s."""
    total = 0
    consumed = 0
    for match in PART.finditer(text):
        if match.start() != consumed:
            break
        total += int(match.group(1)) * UNITS[match.group(2)]
        consumed = match.end()
    if not text or consumed != len(text):
        raise ValueError(f"not a duration: {text!r}")
    return total
