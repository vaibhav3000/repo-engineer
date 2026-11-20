"""Regex matching for ToolRegistry.search, run in a child process with a deadline.

A planner-supplied regex can backtrack catastrophically, and Python's re module
cannot be interrupted from another thread. The registry therefore runs the
matching here, in a separate interpreter started with -I -S, and kills it at
the deadline.

Reads a JSON job from stdin, {"pattern": str, "files": [[path, text], ...]},
and writes {"matches": [[path, line_no, line], ...], "truncated": bool}.
Only the standard library is used.
"""

import json
import re
import sys

MAX_MATCHES = 100


def main() -> None:
    job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    regex = re.compile(job["pattern"], re.IGNORECASE)
    matches: list[list[object]] = []
    for rel, text in job["files"]:
        for line_no, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                matches.append([rel, line_no, line.strip()[:200]])
                if len(matches) >= MAX_MATCHES:
                    report(matches, truncated=True)
                    return
    report(matches, truncated=False)


def report(matches: list[list[object]], *, truncated: bool) -> None:
    payload = json.dumps({"matches": matches, "truncated": truncated})
    sys.stdout.buffer.write(payload.encode("utf-8"))


if __name__ == "__main__":
    main()
