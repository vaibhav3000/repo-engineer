# In-process report forging is a residual, not a sandbox goal

The sandbox contains what agent-written code can do to the host. It cannot
make a test verdict trustworthy against code written to deceive it: code under
test runs inside the pytest process, so it can return objects that compare
equal to anything, patch pytest, or write a well-formed JUnit report and exit
0. Moving pytest into a container moves the deception with it.

We record this explicitly (an attack in the jail suite marked residual on
every backend) instead of claiming the verifier is unforgeable. The defences
that address it are the existing ones: tests and pytest control files the
agent cannot change and that are restored before verification, mutants and
behaviour-preserving edits on add-a-test tasks, and human review of the patch
before `--apply`.

## Considered options

- **A trusted parent process that runs each test in a child with dropped
  privileges.** Collection itself imports agent code, and the child's exit
  status is as forgeable as the report; it adds complexity without closing
  the gap.
- **Sentinel tests with a secret nonce.** The nonce is readable from the
  process's own arguments and memory, so it raises the bar only against
  forgers that do not look.
