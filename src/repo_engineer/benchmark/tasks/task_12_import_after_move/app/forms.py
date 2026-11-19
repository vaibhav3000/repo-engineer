from textkit.old_strings import slugify


def default_handle(name: str) -> str:
    return "@" + slugify(name)
