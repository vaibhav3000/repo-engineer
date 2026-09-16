# References

## Concepts and prior art (ideas, not code)

- SWE-bench - Jimenez et al., 2023, arXiv:2310.06770 - repository-level SWE
  benchmark; our 4-task benchmark is a tiny demonstration harness, NOT a
  SWE-bench result.
- ReAct - Yao et al., 2022, arXiv:2210.03629 - reasoning-plus-acting agent
  pattern; our planner/observation loop is a strict, validated variant.
- OpenAI function calling / tool use - https://platform.openai.com/docs/guides/function-calling (structured tool-call shape our JSON contract follows)
- LangChain agents - https://python.langchain.com/ and OpenHands -
  https://github.com/All-Hands-AI/OpenHands - existing agent frameworks;
  we deliberately implemented the runtime core (validation, jail, policies,
  verification) from first principles instead - see README.
- Git Credential Manager - https://github.com/git-ecosystem/git-credential-manager
  (authentication mechanism used by pushes; no credentials stored in this repo).

## What is ours vs referenced

- All runtime code (state machine, tool registry, jail, planners, harness) in
  src/repo_engineer/ is original, written for this project.
- The benchmark tasks in benchmark/tasks/ are original mini-repositories.
- The LLMPlanner's prompt/JSON contract follows the OpenAI tool-calling
  convention shape; the provider client is original stdlib code.
