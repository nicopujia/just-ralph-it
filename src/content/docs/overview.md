---
title: Overview
description: Introduction to Just Ralph It and how it works
order: 1
---

# Overview

## What is JRI?

What JRI is: an AI-powered platform that builds software from your ideas. You describe what you want, the system clarifies what you actually mean, and then it builds it for you. Most software projects fail not because the builder is incompetent, that builds software from your ideas. You describe what you want, the system clarifies what you actually mean, and then it builds it for you. Most software projects fail not because the builder is incompetent, but because the person with the idea and the person writing the code do not share the same understanding of what "done" looks like. Every missed detail, every unspoken assumption, every vague word becomes a bug, a delay, or a rebuild.

Just Ralph It solves this with a simple idea: clarify first, build second.

The platform uses two AI agents that work together. The first one interviews you to make sure the goal is clear and buildable. The second one builds from that clarified foundation. The result is software that actually matches what you had in mind.

## Who is it for?

You do not need to know how to code. You need to know what you want to build. non-coders can use Just Ralph It to turn their ideas into working software without writing a single line of code.

If you have ever tried to describe a project to a developer and felt the conversation slip into jargon you could not follow, Just Ralph It is designed for you. The system talks in plain language and works through your requirements with you before a single line of code is written.

If you are a developer who wants to offload implementation, you can feed the system a clear spec and watch it build. But the platform shines brightest when someone with domain expertise, not coding expertise, can articulate what they need.

That said, users with software engineering knowledge will get the most out of the platform. The clarification stage is a conversation, and people who understand technical constraints, can write precise specs, and know how to break problems into concrete requirements will naturally guide Ralphy toward better outcomes. You do not need to code, but thinking like an engineer helps.

## The flow: idea → spec → build → iterate

Every project follows the same path.

1. **Idea** - You describe what you want to build in plain language. A product, a tool, a workflow, a website. Anything.
2. **Spec** - The system asks you questions to fill in the gaps. What should it look like? What should happen when X? How should it behave in edge cases? This is the most important step.
3. **Build** - With a clear plan in place, the system implements your project piece by piece. You watch progress in real time.
4. **Iterate** - You review what was built, request changes, and the system adjusts.

This cycle continues until you are satisfied with the result.

## What makes it different

**v0 (by Vercel)** generates UI components from prompts. It is fast for prototyping frontend pieces, but it does not clarify what you actually need before generating code. You get what you asked for literally, not what you meant. There is no spec stage, no task structure, and no deployment pipeline.

**Lovable** lets you prompt your way to a full-stack app. It skips straight to building, which means vague input produces vague output. When the AI fills in your gaps with its own assumptions, you end up with something that looks right but does not behave the way you expected.

**Replit** gives you an AI-assisted development environment. It is powerful if you already know how to code, but it still expects you to drive the implementation. There is no structured clarification step and no separation between understanding the goal and executing it.

**Claude Code** is a strong general-purpose coding agent. It works inside your local environment and follows your instructions well, but the burden of defining what to build, in what order, with what acceptance criteria, stays entirely on you.

Just Ralph It is different because it refuses to build on unclear ground. The platform uses a two-stage workflow: Ralphy interviews you first to clarify intent, then Ralph builds from that clarified foundation. The clarification step is not optional or skippable. The system will not let you move forward until the goal is well understood. Shallow specs produce shallow output, so JRI forces depth before construction begins.

Ralph works autonomously on a VPS, not in your local environment. Work is broken into tasks with dependencies and acceptance criteria. When building is done, JRI deploys your project for you. The result is software that actually matches what you had in mind, built and shipped without you needing to manage a dev environment or deployment pipeline.

## Getting started

Log in with your GitHub account, create a project, and describe what you want to build. Ralphy will guide you from there.
