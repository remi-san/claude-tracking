---
name: issue-cost-tracking
description: Git workflow that gets an agent's token cost counted against the right GitHub issue on the "cost by issue" dashboard. Use before working on a GitHub issue — creating or naming a branch, starting a worktree, spawning or briefing subagents on issues, opening a pull request, or checking out a PR to review it — and when a branch has no issue number in its name.
---

# Issue cost tracking

A hook records, for each prompt and each agent, the branch it ran on; the dashboard reads
the issue from that branch and charges the prompt's model calls to it. It never sees what
you did, only which branch was checked out in your own working directory. A prompt that
ends on `main`, on a detached HEAD, or on a branch with no issue is charged to "(no issue)".

## Rules

1. **Put the issue number in the branch name, first.** `123-fix-login`, `feat/123-login`,
   `issue-123`, `gh-123`. The issue is the first number standing alone between `/`, `-`,
   `_` or the ends of the name, so keep any other number after it: not `2026-10-123-fix`
   (read as 2026) or `fix123` (no separator, no issue).
2. **Create the branch before doing the work**, at the start of the prompt:
   `git switch -c 123-fix-login` (or `git branch -m 123-fix-login` to rename the branch
   you are on). Work done before it is charged to the previous branch.
3. **End the turn on the issue branch.** The main thread's record is its branch at the end
   of the turn. Don't switch back to `main` in the same turn after working on an issue
   (e.g. after merging): do that at the start of the next prompt instead.
4. **One issue per prompt.** A prompt that moved between issues is charged entirely to the
   branch it ends on. Finish one issue (or stop) before switching to another.
5. **Work in your own directory.** Records use the branch of the agent's own working
   directory: Claude Code resets the shell to it, so a `cd` into another checkout (a
   `git worktree add` under `$TMPDIR`, a sibling clone) is charged to the branch you started
   on. For a separate checkout, use a real worktree: the `Agent` tool's
   `isolation: "worktree"` or `EnterWorktree`.
6. **Never work on a detached HEAD.** Checking out a commit, a tag, `FETCH_HEAD` or a remote
   branch (`origin/main`) records nothing. Branch from it: `git switch -c 123-x origin/main`.
7. **No number in the name? Link the PR.** A branch without an issue number gets the
   lowest-numbered issue its pull request closes. Open the PR early, with `Closes #123`
   in its body, so the issue shows as a closing reference; earlier records on the branch
   are then sent again with it.
8. **The repository needs a GitHub `origin` remote** (`owner/repo`). Without one, records
   are filed under the directory name and PR lookups are skipped.

## Subagents

- A subagent in its own worktree is tracked on that worktree's branch, not the session's.
  Its first step must put the worktree on the issue branch (`git switch -c 123-fix-login`,
  or `git branch -m 123-fix-login` for the branch the worktree was created on). Say so in
  its prompt, with the issue number.
- One issue per subagent. A manager staying on `main` gets the issue of the subagents it
  spawns, messages or hears back from in that prompt, but only when they all agree on one:
  brief subagents for different issues in separate prompts.
- Two subagents of the same type on different issues in one prompt can't be told apart:
  the last record wins for both. Run them in separate prompts, or as different agent types.

## Reviewing a pull request

Check the PR out on `<PR branch>-code-review` (or any `-<word>-review` suffix, e.g.
`-security-review`; add `-2`, `-3` if the name is taken). The review is then charged to the
PR's issue, even when the PR branch has no number. Don't review on a detached checkout of
the PR head.

## Check

`git branch --show-current` in your working directory must print a branch whose first
standalone number is the issue (or whose PR closes it) — before you start, and when you stop.
