"""Set POSIX resource limits on this process, then exec the real command.

    python -I -S _launcher.py '<limits json>' -- <argv...>

The local sandbox starts pytest through this launcher instead of using
subprocess's preexec_fn, which is not safe when the parent has threads. The
limits are applied in a fresh single-threaded interpreter and inherited by
everything the command starts. A limit the system refuses is reported on
stderr and skipped, never silently.

Only the standard library is used, and the file is run with -I -S so nothing
from the workspace or site-packages is imported.
"""

import json
import os
import sys

MIB = 1024 * 1024


def apply_limits(limits: dict[str, int]) -> None:
    assert sys.platform != "win32", "the launcher is for POSIX systems"
    import resource

    wanted = [
        ("RLIMIT_AS", limits.get("memory_mb", 0) * MIB),
        ("RLIMIT_CPU", limits.get("cpu_seconds", 0)),
        ("RLIMIT_FSIZE", limits.get("file_size_mb", 0) * MIB),
        ("RLIMIT_NOFILE", limits.get("nofile", 0)),
        ("RLIMIT_CORE", -1),  # -1 here means: set to zero
    ]
    for name, value in wanted:
        if value == 0 or not hasattr(resource, name):
            continue
        which = getattr(resource, name)
        value = 0 if value < 0 else value
        try:
            hard = resource.getrlimit(which)[1]
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError) as exc:
            sys.stderr.write(f"sandbox launcher: could not set {name}: {exc}\n")


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        sys.stderr.write("usage: _launcher.py '<limits json>' -- <argv...>\n")
        sys.exit(2)
    apply_limits(json.loads(sys.argv[1]))
    command = sys.argv[3:]
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
