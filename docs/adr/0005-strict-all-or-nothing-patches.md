# apply_patch is strict and all-or-nothing

`apply_patch` accepts unified diffs only when every hunk matches the file
exactly, either at the line its header names or at exactly one other place
(reported as an offset). There is no fuzz factor, and a multi-file patch is
applied completely or not at all: new contents are computed in memory for
every file first, and a failed write rolls back the files already written.

GNU patch's fuzz and partial application are friendly to humans and hostile
to an agent loop: a hunk applied in the wrong place produces a plausible edit
the model never intended, and a half-applied patch leaves the workspace in a
state no planner reasoned about. A clear rejection that names the failing
hunk costs one more turn and keeps every observed state one the model asked
for.
