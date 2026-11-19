"""Kept for truncate(); slugify moved to textkit.text."""


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "..."
