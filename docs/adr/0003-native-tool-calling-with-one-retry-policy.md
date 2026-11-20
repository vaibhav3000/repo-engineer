# Native function calling through provider adapters, with one retry policy

Planners now talk to models through each provider's function-calling API
(Gemini, OpenAI, Anthropic, and OpenAI-compatible local servers) behind a
`ModelAdapter` interface that speaks a provider-neutral conversation. Tool
schemas are generated from the same `ToolSpec` fields the registry validates,
so the model is never told something the registry would not enforce. The
prompt-parsed JSON planner remains only for endpoints without function
calling.

Every SDK's built-in retries are turned off (`max_retries=0`, no
`retry_options`) and one loop in our code retries the same classes everywhere:
408, 409, 429, 500, 502, 503, 504, 529 and connection failures, never other
4xx statuses and never a malformed reply. Leaving retries to each SDK would
have meant three different policies, defaults that change between major
versions, and retry behaviour the tests cannot see.

## Consequences

- Each adapter maps its SDK's exceptions to `ProviderError(status,
  retryable)`; adding a provider means writing that mapping.
- The SDKs are optional extras and imported lazily. Tests run each adapter
  against the real SDK client with an in-process `httpx2.MockTransport`, so
  request serialisation is exercised without a network.
