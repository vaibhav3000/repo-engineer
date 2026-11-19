import re

NUMBER = r"(0|[1-9]\d*)"
VERSION = re.compile(
    rf"{NUMBER}\.{NUMBER}\.{NUMBER}"
    rf"(-[0-9A-Za-z.-]+)?"
)


def is_valid_semver(text: str) -> bool:
    """True for a MAJOR.MINOR.PATCH version with an optional pre-release tag."""
    return VERSION.fullmatch(text) is not None
