"""Relay an external MCP server's stdout, stopping it if one message grows too large.

    python -I -S _stdio_relay.py <max line bytes> -- <argv...>

The MCP client reads a stdio server's output into a buffer until it sees a
newline, with no limit, so a hostile server that writes without newlines
would grow the runtime's memory and CPU use for as long as the run lasts.
This relay sits between the two: the server inherits the relay's stdin and
stderr, its stdout is copied through, and once more than the limit arrives
without a newline the server is killed and the relay exits, which the client
sees as a closed connection.

Only the standard library is used, and the file runs with -I -S so nothing
from the workspace or site-packages is imported.
"""

import os
import subprocess
import sys

CHUNK = 65536


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        sys.stderr.write("usage: _stdio_relay.py <max line bytes> -- <argv...>\n")
        return 2
    limit = int(sys.argv[1])
    child = subprocess.Popen(sys.argv[3:], stdout=subprocess.PIPE)
    assert child.stdout is not None
    out = sys.stdout.buffer
    since_newline = 0
    while chunk := os.read(child.stdout.fileno(), CHUNK):
        newline = chunk.rfind(b"\n")
        since_newline = len(chunk) - newline - 1 if newline >= 0 else since_newline + len(chunk)
        if since_newline > limit:
            child.kill()
            sys.stderr.write(
                f"stdio relay: the server sent more than {limit} bytes without a newline; stopped\n"
            )
            return 1
        out.write(chunk)
        out.flush()
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())
