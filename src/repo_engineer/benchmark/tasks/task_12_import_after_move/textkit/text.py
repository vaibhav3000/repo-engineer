import re


def slugify(text: str) -> str:
    return "-".join(re.sub(r"[^\w\s-]", "", text.lower()).split())
