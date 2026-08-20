---
name: ship
description: Work until your changes are in production. Use when a change is written and must reach users—pull request, verification, merge, release.
---
# Shipping
Merged ≠ shipped. Done = the new version is installable by users on PyPI. Report the release, not the merge.

A step that fails gets three rounds of correction. Then stop and report what was tried.

## Scope
One pull request = one change a reviewer can hold in the head at once. Several changes = a stack: each branch off the one before it, each reviewable alone. The `gh stack` extension holds the chain together — `gh extension install github/gh-stack` once per machine.

`gh stack init` starts the stack on the bottom branch, `gh stack add <branch>` puts each later change on top of the one before it, `gh stack rebase` cascades a branch that moved through everything above it, `gh stack view` shows the order and the pull request of each one.

Verify the stack bottom up. Merge it in one operation (step 3). Release once, after it lands, at the version the whole stack earns.

## 1. Verify
Work on its own branch, in conventional-commit style. Delegate the `verify` skill to a subagent that did not write the change, and take back the verdict. Verify first, so the pull request reports work that is already tested.

## 2. Pull request
Push, then `gh pr create`. A stack: `gh stack submit --auto --open` pushes every branch and opens or updates the whole chain, then `gh pr edit` writes the title and the body of each one.

Title: a conventional commit — `type(scope): imperative sentence`, lower case, no final period, 72 characters at most. Example: `feat(cli): control a run from the command line`. Scope names the module or the area the change lands in; leave it out when the change is repo-wide. The squash carries the title into `main` word for word, so the title is the line the history keeps.

Body in plain English, for a person who did not do the work:
1. **TL;DR** at the top — the change in one or two sentences.
2. **What** changed, in terms of what a user does with it.
3. **Why** it was needed — the problem before it.
4. **How it was tested** — what stage 1 ran, what the manual tests did, what came out.
5. **Why it is this many lines** — `git diff --shortstat <base>..HEAD`, split into source and tests, and what earns each part. Say what the additions buy and what they replaced, and name any part that a different choice would have made smaller. A reviewer who must take the size on trust cannot review it.

Say it as you would to a colleague. Keep the compressed vocabulary for the commits and the code.

## 3. Merge
Once the checks on the pull request are green: `gh pr merge --squash --delete-branch`. GitHub takes the squash subject from the pull request title, so correct the title before you merge, not the subject after. Red: delegate fix to subagent, push.

A stack: wait for every pull request in it to go green, then `gh stack merge --yes --squash <pr-number>` lands that one and each unmerged one below it, bottom up, all or nothing.

## 4. Release
```bash
git switch main && git pull
./scripts/ship.py <version>
```
Run it from the `main` checkout, not from the worktree of the merged branch. It runs the gate, writes the version, commits, tags, and pushes. It sets a dirty tree aside and returns it after. A failed run removes the commit and the tag it created — read the error, correct the cause, run again.

Number by what a user sees. The project is pre-1.0:
- minor — behavior that is new, changed, or removed
- patch — bug fixes, documentation, refactors, internal changes

Take the next free number after the newest tag. Reuse none, skip none.

## 5. Confirm
The `v` tag starts "Publish to PyPI". Watch it:
```bash
gh run watch "$(gh run list --workflow=publish.yml --limit=1 --json databaseId --jq '.[0].databaseId')"
```
Green = shipped. Red = the release did not go out. Cause in the code → correct it and ship the next number. Any other cause → stop and report it.
