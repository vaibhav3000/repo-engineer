# Mount the workspace read-only and give test runs no writable host mount

Inside the Docker sandbox the workspace is bind-mounted read-only and the only
writable place is a size-limited tmpfs at `/tmp`. A test that writes into the
repository therefore fails under Docker, which surprises anyone used to tests
that drop files next to themselves. We accept that: a read-only workspace
removes every attack that plants or swaps files during a run (conftest
planting, the directory-to-link race, huge trees that stall later walks), and
with no writable host mount at all, test code cannot fill the host's disk.
All writes the agent is allowed to make go through the registry's tools, on
the host, under the jail.

The JUnit report the verifier needs is written to the tmpfs and printed after
pytest exits by a short runtime-supplied wrapper behind a random per-run
marker. A bind-mounted scratch directory was the obvious alternative and was
rejected because it is writable host disk with no size limit.
