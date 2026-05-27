# JRI Web Capability

Use `jri --run-web search <ownerJson> "<query>"` for current external facts and
`jri --run-web fetch <ownerJson> "<url>"` for bounded markdown from a source URL.
The owner JSON is emitted by JRI and includes `projectDir`, `capability: "web"`,
and either a loop owner or chat-turn owner.

Search returns at most 5 timestamped results. Fetch returns at most 12,000
characters in context and writes omitted content to loop artifacts or
`.jri/logs/interrogation-artifacts/` for chat-owned interrogator fetches.
If web access is required but unavailable, report an actionable capability
blocker or a clearly labeled degraded response instead of guessing.
