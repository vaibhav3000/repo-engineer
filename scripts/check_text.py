"""Fail if any tracked text file contains an en dash or an em dash.

    python scripts/check_text.py

The project's prose uses commas, colons and parentheses instead; this keeps
it that way in code, docs and the report source alike.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {chr(0x2013): "en dash", chr(0x2014): "em dash"}


def main() -> int:
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode("utf-8")
    problems = []
    for name in filter(None, listed.split("\0")):
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary files (the PDF, images)
        for char, label in FORBIDDEN.items():
            if char in text:
                problems.append(f"{name}: contains an {label}")
    for problem in problems:
        print(problem, file=sys.stderr)
    print(f"checked tracked text files: {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
