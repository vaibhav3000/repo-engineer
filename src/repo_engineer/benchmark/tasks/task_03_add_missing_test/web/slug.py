"""URL slugification."""
import re


def slugify(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace into single hyphens."""
    cleaned = re.sub(r"[^\w\s-]", "", text.lower())
    return "-".join(cleaned.split())
