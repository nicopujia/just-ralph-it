# JRI Web Capability

Use `jri --run-web search <projectDir> <loopId> "<query>"` for current external
facts and `jri --run-web fetch <projectDir> <loopId> "<url>"` for bounded
markdown from a source URL.

Search returns at most 5 timestamped results. Fetch returns at most 12,000
characters in context and writes omitted content to `.jri/logs/<loopId>/artifacts/`.
If web access is required but unavailable, report an actionable capability
blocker or a clearly labeled degraded response instead of guessing.
