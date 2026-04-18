---
name: reverse-ralph
description: Analyze a brownfield repo and map its functionality into tasks. Use when the user inputs a messy repo (no standarize testing, no JRI tasks) and/or wants to reverse-Ralph it.
---

# Introduction

To *reverse-Ralph* means to discover the intended functionality and create tasks accordingly by inspecting legacy code, rather than exclusively by making questions. The goal is to recreate existing functionality (maybe a bit different based on what the user wants) but in a clean way.

# Stategy

When the user wants to reverse-Ralph a codebase, start by spawning up to 100 parallel subagents to investigate the current behavior. After this research, you should be able to create tasks that, once implemented, match 1-1 existing functionality.

After that, start by asking the user if they want anything different from what the code already does, and point any discovered bugs and uncovered edge-cases asking what to do with them.

Once the user's intent is clear, proceed to create tasks. Write them based on behavior, not implementation details, as we aim to craft a much cleaner codebase with a similar or identical (depending on what the user wants) functionality. As long as you focus on I/O, Ralph will ensure to keep it clean.

**IMPORTANT**: Be very specific. Take every single line of legacy code into consideration regarding what it does. This is important because legacy code may have been iterated several times through figuring out edge cases or bugs, thus being filled with patches. Those patches might be messy, but matter *a lot*, and they should definitely be reflected in the tasks.
