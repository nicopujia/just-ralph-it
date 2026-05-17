---
name: reverse-ralph
description: Analyze a brownfield repo and encode discovered behavior into the Intent Graph. Use when the user wants to reverse-Ralph an existing codebase.
---

# Introduction

To reverse-Ralph means to discover an existing codebase's intended behavior by inspecting the implementation, then preserve that understanding in the Intent Graph for user confirmation and later compilation.

The goal is not to recreate the current implementation structure. The goal is to capture what the software does, what users rely on, and which edge cases are intentional so the graph can later compile into clean, behavior-focused tasks.

# Strategy

When the user wants to reverse-Ralph a codebase, begin with broad read-only exploration of the current repository. With up to 100 subagents, use parallel exploration for each independent area.

For each discovered behavior, capture:

- The user-visible contract: inputs, outputs, state changes, errors, side effects, and persistence.
- Evidence from the legacy code: relevant file paths, functions, commands, fixtures, tests, or data formats.
- Open questions where intent is unclear, behavior conflicts, or implementation appears buggy.
- Compatibility decisions the user must confirm before Ralph implements anything.

After exploration, summarize what the graph says the product currently does. Ask the user what should stay identical, what should intentionally change, and how to handle discovered bugs or uncovered edge cases. Do not `compile-graph` yet.

# Notes

It is fine to reference legacy file paths inside graph note bodies as evidence.

Do not treat legacy implementation details as requirements unless they affect behavior or an explicit user constraint.
