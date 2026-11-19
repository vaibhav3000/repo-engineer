# Replay re-executes recorded planner decisions

`repo-engineer replay` does not play back recorded observations. It feeds the
planner decisions recorded in an audit trace to the real runtime, against a
fresh copy of the starting workspace whose digest must match, and compares
what happens with what was recorded. Playing back observations would only
prove the trace file can be read; re-executing proves that the same decisions
still produce the same tool results, rejections and verdict under the current
code, which is what a regression check or an incident review needs.

## Consequences

- The trace must hold every decision the planner made (tool calls, `finish`,
  malformed replies) and a digest of the starting workspace; it does not need
  the model's raw text to replay.
- Output that legitimately varies (pytest durations) is normalised before
  comparison, the same way the golden benchmark files are.
- A live run is replayed offline: nothing calls a model.
