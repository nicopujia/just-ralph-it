# JRI Web Capability

Use the JRI-owned web search and web fetch capability for current external
facts. Search returns timestamped results and fetch returns bounded
markdown/plain text from a source URL.

Hidden compatibility entrypoints such as `jri --run-web ...` are JRI adapter
details, not general shell commands for agents. Ownership metadata is emitted by
JRI and includes `projectDir`, `capability: "web"`, and either a loop owner or
chat-turn owner.

Search returns at most 5 timestamped results. Fetch returns at most 12,000
characters in context and writes omitted content to loop artifacts or
`.jri/logs/interrogation-artifacts/` for chat-owned interrogator fetches.
If web access is required but unavailable, report an actionable capability
blocker or a clearly labeled degraded response instead of guessing.
