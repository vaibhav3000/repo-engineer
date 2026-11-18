"""Redaction of credentials and host paths from tool output.

Tool output can travel to three places the runtime does not control: the
planner's prompt (sent to a third-party model), the JSON trace, and committed
benchmark results. Every observation therefore passes through a Redactor
before it leaves the registry.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

# Environment variables whose values are treated as credentials.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|PRIVATE", re.IGNORECASE)
# Shorter values are too likely to collide with ordinary output.
MIN_SECRET_LENGTH = 8
SECRET_PLACEHOLDER = "<redacted>"


class Redactor:
    """Replaces credential values and absolute host paths with placeholders."""

    def __init__(self, workspace: Path) -> None:
        anchors = [
            (workspace, "<workspace>"),
            (Path(sys.prefix), "<python-env>"),
            (Path(sys.exec_prefix), "<python-env>"),
            (Path(sys.base_prefix), "<python>"),
            (Path(sys.base_exec_prefix), "<python>"),
            (Path(tempfile.gettempdir()), "<tmp>"),
            (Path.home(), "~"),
        ]
        variants: dict[str, str] = {}
        for path, token in anchors:
            for form in _path_forms(path):
                # Skip roots and very short paths, which would mangle all output.
                if len(form) >= 4 and form not in variants:
                    variants[form] = token
        flags = re.IGNORECASE if os.name == "nt" else 0
        self._path_rules = [
            (re.compile(r"(?<![\w.~-])" + re.escape(form) + r"(?![\w~-])", flags), token)
            for form, token in sorted(variants.items(), key=lambda item: -len(item[0]))
        ]

    def __call__(self, text: str) -> str:
        if not text:
            return text
        for value in _secret_values():
            text = text.replace(value, SECRET_PLACEHOLDER)
        for pattern, token in self._path_rules:
            text = pattern.sub(token, text)
        return text


def _secret_values() -> list[str]:
    """Current values of credential-looking environment variables, longest first."""
    values = {
        value.strip()
        for name, value in os.environ.items()
        if SECRET_NAME.search(name) and len(value.strip()) >= MIN_SECRET_LENGTH
    }
    return sorted(values, key=len, reverse=True)


def _path_forms(path: Path) -> set[str]:
    """Spellings of a path that may appear in output, including repr() escaping."""
    forms: set[str] = set()
    for candidate in {str(path), os.path.realpath(path)}:
        forms.add(candidate)
        forms.add(candidate.replace("\\", "/"))
        forms.add(candidate.replace("\\", "\\\\"))
    return forms
