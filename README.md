# Claude Code usage observability

Claude Code → OTel Collector → Prometheus (metrics) + Loki (events) + Tempo (traces) → Grafana.

```sh
docker compose up -d
open http://localhost:3000      # "Claude Code usage" dashboard (no login)
open http://localhost:3000/d/claude-code-prompt-timeline   # per-prompt agent/subagent timeline
open http://localhost:3000/d/claude-code-cost-by-issue     # cost per GitHub issue
```

| Service        | Port        |
|----------------|-------------|
| Grafana        | 3000        |
| OTLP gRPC/HTTP | 4317 / 4318 |
| Prometheus     | 9090        |
| Loki           | 3100        |
| Tempo          | 3200        |

## Sending telemetry

Merge this `env` block into `~/.claude/settings.json`. Claude Code ignores OTel exporter
variables in a repository's `.claude/settings.json`, so it has to be the global one. Settings
are read at startup: restart running sessions after changing them.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_LOGS_EXPORT_INTERVAL": "5000",
    "OTEL_LOG_USER_PROMPTS": "1",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_TRACES_EXPORTER": "otlp"
  }
}
```

What gets recorded:

- `OTEL_LOG_USER_PROMPTS=1`: prompt text (remove it to log only the prompt length).
- `OTEL_LOG_TOOL_DETAILS=1`: tool inputs, including the prompt given to each subagent and
  every Bash command (values truncated at 512 characters).
- `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` + `OTEL_TRACES_EXPORTER=otlp`: traces (beta).
  A subagent's model and tool spans nest under the `Agent` tool span that started it.

## Prompt timeline dashboard

Pick a prompt, then read its events in order: which agent made each model call, what each
subagent was asked, Bash commands, tokens and cost. Tool events are not tagged with an agent
by Claude Code; open the trace (bottom panel) for the exact call tree. A subagent's report
arrives as a new prompt, so subagents run one after another span several prompt ids.

## Cost by issue

Claude Code's telemetry has no git branch, so `hooks/record-branch.py` records it in Loki
(`{service_name="claude-code-branch"}`), per prompt and agent:

- Main thread (UserPromptSubmit, Stop): the session's branch. Stop also covers turns Claude
  starts itself (a subagent report), and its end-of-turn branch wins when a prompt switched.
- Subagents (SubagentStop): a subagent often works in its own git worktree, on another branch
  than the session. The hook takes each message's working directory (`cwd`) from its
  transcript, and the branch checked out there at the message's time from that checkout's
  HEAD reflog. Not the transcript's `gitBranch`: Claude Code keeps one per session, shared
  by all its agents and refreshed at odd moments. It is only the fallback when the worktree
  is gone. A detached HEAD (a commit, `FETCH_HEAD`, `origin/main`) has no branch and is skipped.
- Main thread with no issue in its branch (an agent manager staying on `main`): the prompt
  takes the issue of the subagents it spawns, continues with SendMessage or gets a hand-back
  from, when they all agree on one; the record names the subagent in `via`.

The issue is the first number in the branch name (`123-fix`, `feat/123-foo`, `issue-123`,
`gh-123`). Numbers joined by dashes are a version or a date, never an issue:
`deps-node-24-22-1` and `2026-10-28-fix` have none (nor does `123-2-fix`). A branch without
one (`claude/keep-agent-worktrees`) gets the lowest-numbered issue its GitHub pull request
closes, looked up with `gh`; a reviewer's branch named
`<PR branch>-code-review` (any `-<word>-review` suffix, optionally followed by a `-<n>` that
is not read as an issue, as in `<PR branch>-code-review-2`) gets its PR's issue (cached in
`~/.cache/claude-code-branch-issues.json`; a branch with no PR yet is asked again after 10
minutes). When a branch's PR is first found, the hook sends its earlier records again with the
issue. Calls on a branch with neither are grouped as "(no issue)" per repository. Work done in
a directory the agent only `cd`s into (e.g. a `git worktree add` under `$TMPDIR`) is recorded
on the branch of the agent's own directory: Claude Code resets the shell to it. The dashboard joins each model call's cost to it by `prompt_id` and agent type
(`agent_name`, or `main`), so a session that moved between issues, or ran subagents on other
issues' worktrees, is split between them. Two subagents of the same type working on different
branches within one prompt can't be told apart: the last record wins.

Register the hook in `~/.claude/settings.json` so it runs in every repository: merge this
`hooks` block into it, with `/path/to/claude-tracking` replaced by this repository's path
(append to an event's list if you already have hooks on it):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-tracking/hooks/record-branch.py",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-tracking/hooks/record-branch.py",
            "timeout": 5
          }
        ]
      }
    ],
    "SubagentStop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-tracking/hooks/record-branch.py",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

The `issue-cost-tracking` skill (`skills/issue-cost-tracking/SKILL.md`) tells agents the
git workflow this relies on: issue number first in the branch name, end the turn on it, one
issue per prompt, subagents on their own issue branch, reviews on `<PR branch>-code-review`.
Link it into your user skills so it applies in every repository:

```sh
ln -s "$PWD/skills/issue-cost-tracking" ~/.claude/skills/issue-cost-tracking
```

To load prompts from before the hook, run `hooks/record-branch.py --backfill`: it reads the
transcripts in `~/.claude/projects`, subagents included, then sends again, with the current
time, every prompt and agent whose latest record in Loki differs from what the transcripts
give, since the dashboard keeps the latest. Running it again is harmless.

## Context size

Claude Code does not export context size directly. The dashboards work it out from each
`api_request` event: input + cache read + cache write tokens, which is everything sent to
the model on that call. The "Context window" row charts it for each session's main thread
(subagents excluded). The prompt timeline shows it per call in the `context tok` column.

## Notes

- Sessions run from the Claude desktop app report as `service_name="claude-code-desktop"`,
  the CLI as `claude-code`: the dashboards' Loki queries match both.
- Retention: 90 days (Prometheus, Loki and Tempo).
- Totals use `max_over_time` per session series, so a session that started before the
  selected time range is counted in full.
- `docker compose down -v` wipes all stored data.
