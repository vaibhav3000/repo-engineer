from textkit.old_strings import slugify, truncate


def page_url(title: str) -> str:
    return f"/pages/{slugify(title)}"


def teaser(title: str) -> str:
    return truncate(title, 12)
