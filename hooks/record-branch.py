#!/usr/bin/env python3
"""Record which git branch / GitHub issue each Claude Code prompt runs on, in Loki.

Claude Code's telemetry has no branch, so the "cost by issue" dashboard joins costs
to these records by prompt_id (the same id as in the telemetry events) and agent: a
subagent often works in its own git worktree, on another branch than the session.
Its model calls carry the parent's prompt_id and its type as agent_name.

Hook mode, reading the hook JSON on stdin:
- UserPromptSubmit, Stop: the main thread's branch. Stop also covers turns Claude starts
  itself (a subagent report), which never go through UserPromptSubmit, and records the
  branch as it is at the end of the turn.
- SubagentStop: the branch of each message in the subagent's transcript, since the
  subagent's worktree is not the session's directory.
Backfill mode: `record-branch.py --backfill` reads past prompts from the transcripts in
~/.claude/projects, subagents included.

A transcript message's branch is the one checked out in its `cwd` (the agent's own
worktree) at the message's time, read from that checkout's HEAD reflog. Not the message's
`gitBranch`: Claude Code keeps one for the whole session, shared by every agent and
refreshed at odd moments (collection-manager#112). `gitBranch` is only the fallback when
the checkout is gone.

A main-thread prompt whose branch names no issue (an agent manager staying on main) gets
the issue of the subagents it spawns, continues or gets a hand-back from, when they agree on
one, with the subagent in "via".

A branch with no number in its name (claude/keep-agent-worktrees) gets the issue its pull
request closes, from `gh`. The PR is often opened after the work on the branch started, so
when a branch's PR is first found, the records already sent for that branch are sent again
with the issue.

Never fails and prints nothing: UserPromptSubmit hook output is added to the prompt.
Set CLAUDE_BRANCH_DEBUG=<file> to append each hook input to that file.
"""
import glob
import shutil
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

LOKI = os.environ.get("CLAUDE_BRANCH_LOKI_URL", "http://localhost:3100")
STREAM = {"service_name": "claude-code-branch"}
# First number standing alone in a path segment: 123-fix, feat/123-foo, issue-123, gh-123, fix_123
ISSUE_RE = re.compile(r"(?:^|[/_#-])(\d+)(?=$|[/_-])")
# A subagent named in a main-thread message, as raw transcript JSON: the Agent tool's result,
# a SendMessage to it, its hand-back, its task notification.
AGENT_REF_RE = re.compile(r'agentId: (a[0-9a-f]{16})\b|"to": ?"(a[0-9a-f]{16})"|<agent-message from=\\?"(a[0-9a-f]{16})|<task-id>(a[0-9a-f]{16})</task-id>')
# Branch -> issue its PR closes. A branch without a PR is asked about again after MISS_TTL.
PR_CACHE = os.path.expanduser("~/.cache/claude-code-branch-issues.json")
MISS_TTL = 600
# A reviewer's branch: <PR branch>-<word>-review, with -<n> added when that name is taken in
# another worktree. The -<n> is not an issue number.
REVIEW_SUFFIX_RE = re.compile(r"-[a-z]+-review(?:-\d+)?$")
GH = shutil.which("gh") or next((p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", os.path.expanduser("~/.local/bin/gh")) if os.path.exists(p)), "")


def git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def repo_name(cwd, cache={}):
    # A subagent's worktree may be gone by backfill time: use the repository holding it.
    cwd = cwd.split("/.claude/worktrees/")[0]
    if cwd not in cache:
        url = git(cwd, "remote", "get-url", "origin")
        m = re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
        top = git(cwd, "rev-parse", "--show-toplevel")
        cache[cwd] = m.group(1) if m else os.path.basename(top or cwd.rstrip("/"))
    return cache[cwd]


def load_pr_cache(cache={}):
    if not cache:
        try:
            with open(PR_CACHE) as f:
                cache.update(json.load(f))
        except (OSError, ValueError):
            pass
        cache.setdefault("", {})  # marks it loaded
    return cache


def pr_issue(repo, branch, found):
    """Issue that the branch's PR closes, or "". Adds (repo, branch) to found when its PR is
    seen for the first time, so that its earlier records can be sent again."""
    if branch in ("main", "master") or "/" not in repo or not GH:
        return ""
    cache, key = load_pr_cache(), repo + " " + branch
    hit = cache.get(key) or {}
    if hit.get("issue") or time.time() - hit.get("checked", 0) < MISS_TTL:
        return hit.get("issue", "")
    # A reviewer checks a PR out on <PR branch>-code-review or -security-review (collection-manager).
    for head in dict.fromkeys([branch, REVIEW_SUFFIX_RE.sub("", branch)]):
        try:
            out = subprocess.run([GH, "pr", "list", "-R", repo, "--head", head, "--state", "all", "--limit", "1",
                                  "--json", "closingIssuesReferences"], capture_output=True, text=True, timeout=3)
            prs = json.loads(out.stdout) if out.returncode == 0 else None
        except Exception:
            prs = None
        if prs is None:  # gh failing or offline: ask again next time
            return ""
        if prs:
            break
    refs = prs[0]["closingIssuesReferences"] if prs else []
    issue = str(min(r["number"] for r in refs)) if refs else ""
    cache[key] = {"issue": issue} if issue else {"checked": time.time()}
    try:
        os.makedirs(os.path.dirname(PR_CACHE), exist_ok=True)
        with open(PR_CACHE, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass
    if issue:
        found.add((repo, branch))
    return issue


def branch_issue(repo, branch, found):
    m = ISSUE_RE.search(REVIEW_SUFFIX_RE.sub("", branch))
    return m.group(1) if m else pr_issue(repo, branch, found)


def branch_at(cwd, ts, cache={}):
    """Branch checked out in cwd at ts (seconds), from its HEAD reflog: "HEAD" when detached,
    None when the checkout is gone."""
    if cwd not in cache:
        hist = None
        if os.path.isdir(cwd):
            hist = []
            for line in reversed(git(cwd, "reflog", "show", "--date=unix", "--format=%gd%x09%gs", "HEAD").splitlines()):
                sel, _, subject = line.partition("\t")
                when = re.search(r"@\{(\d+)\}", sel)
                move = (re.match(r"checkout: moving from (\S+) to (\S+)$", subject)
                        or re.match(r"Branch: renamed refs/heads/(\S+) to refs/heads/(\S+)$", subject)
                        or re.match(r"rebase \(finish\): returning to refs/heads/()(\S+)$", subject))
                if when and move:
                    if not hist:
                        hist.append((0, move.group(1)))  # before the first move
                    hist.append((int(when.group(1)), move.group(2)))
            hist = hist or [(0, git(cwd, "branch", "--show-current"))]
            # Checking out a commit, FETCH_HEAD or a remote branch (origin/main) detaches HEAD.
            remotes = tuple(r + "/" for r in git(cwd, "remote").split()) or ("\0",)
            hist = [(t, "HEAD" if not b or b.endswith("HEAD") or re.fullmatch(r"[0-9a-f]{7,40}", b)
                     or b.startswith(remotes) else b) for t, b in hist]
        cache[cwd] = hist
    hist = cache[cwd]
    if hist is None:
        return None
    return next((b for t, b in reversed(hist) if t <= ts), hist[0][1])


def message_branch(msg):
    ts = datetime.fromisoformat(msg["timestamp"].replace("Z", "+00:00")).timestamp()
    return branch_at(msg["cwd"], ts) or msg.get("gitBranch")


def related_agents(main_transcript, cache={}):
    """prompt_id -> ids of the subagents its main-thread messages name."""
    if main_transcript not in cache:
        refs = {}
        try:
            with open(main_transcript, errors="replace") as f:
                for raw in f:
                    if not AGENT_REF_RE.search(raw):
                        continue
                    pid = json.loads(raw).get("promptId")
                    if pid:
                        ids = refs.setdefault(pid, [])
                        ids += [i for m in AGENT_REF_RE.finditer(raw) for i in m.groups() if i and i not in ids]
        except (OSError, ValueError):
            pass
        cache[main_transcript] = refs
    return cache[main_transcript]


def subagent_issue(main_transcript, agent_id, prompt_id, found, cache={}):
    """Issue of a subagent's branch: in its messages of this prompt, else its last message."""
    # Only this session's subagents: an id merely quoted from another session's output has none.
    session = os.path.basename(main_transcript)[:-len(".jsonl")]
    key = (session, agent_id)
    if key not in cache:
        # Subagents started while the session is in a worktree go to that worktree's project folder.
        paths = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session}/subagents/agent-{agent_id}.jsonl"))
        last = {}
        for path in paths:
            with open(path, errors="replace") as f:
                for raw in f:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if msg.get("type") in ("user", "assistant") and msg.get("cwd") and msg.get("timestamp"):
                        last[msg.get("promptId")] = last[None] = msg
        cache[key] = last
    msg = cache[key].get(prompt_id) or cache[key].get(None)
    branch = message_branch(msg) if msg else ""
    return branch_issue(repo_name(msg["cwd"]), branch, found) if branch and branch != "HEAD" else ""


def record(session_id, prompt_id, agent, cwd, branch, ts_ns, found, main_transcript=None):
    repo = repo_name(cwd)
    line = {"session_id": session_id, "prompt_id": prompt_id, "agent": agent, "repo": repo, "branch": branch}
    issue = branch_issue(repo, branch, found)
    if not issue and main_transcript:
        by_agent = {a: subagent_issue(main_transcript, a, prompt_id, found)
                    for a in related_agents(main_transcript).get(prompt_id, [])}
        if len(set(by_agent.values()) - {""}) == 1:
            line["via"], issue = next(("agent-" + a, i) for a, i in by_agent.items() if i)
    if issue:
        line["issue"] = issue
    return [str(ts_ns), json.dumps(line)]


def query(logql, days=30, direction="backward"):
    now = time.time_ns()
    url = LOKI + "/loki/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": logql, "start": now - days * 86400 * 10**9, "end": now, "limit": 5000, "direction": direction})
    with urllib.request.urlopen(url, timeout=5) as resp:
        return [(int(t), json.loads(l)) for s in json.load(resp)["data"]["result"] for t, l in s["values"]]


def retag(found, skip=()):
    """Send again, with their issue, the records of the branches whose PR was just found.

    Only for a prompt and agent whose latest record is on that branch: the dashboard keeps
    the latest record of each, so a newer one on another branch must stay the latest."""
    for repo, branch in found:
        issue = pr_issue(repo, branch, set())
        if not issue:
            continue
        old = query('{service_name="claude-code-branch"} |= %s' % json.dumps(json.dumps(branch)[1:-1]))
        sessions = {r["session_id"] for _, r in old if r.get("repo") == repo and r.get("branch") == branch}
        if not sessions:
            continue
        latest = {}
        for t, r in query('{service_name="claude-code-branch"} | json | session_id=~"%s"' % "|".join(sorted(sessions))):
            key = (r.get("prompt_id"), r.get("agent") or "")
            if t > latest.get(key, (0,))[0]:
                latest[key] = (t, r)
        values, now = [], time.time_ns()
        for key, (_, r) in latest.items():
            if key not in skip and r.get("repo") == repo and r.get("branch") == branch and not r.get("issue"):
                values.append([str(now + len(values)), json.dumps({**r, "issue": issue})])
        if values:
            push(values, source="retag")


def correct(values):
    """Send again, with the current time, each prompt and agent's last record from the
    transcripts where it differs from its latest record in Loki: the dashboard keeps the
    latest, and live records are newer than backfilled ones."""
    want = {}
    for _, l in values:  # in time order: the last one per key wins
        r = json.loads(l)
        want[(r["prompt_id"], r["agent"])] = r
    have = {}
    for session in sorted({r["session_id"] for r in want.values()}):
        for t, r in query('{service_name="claude-code-branch"} |= "%s"' % session):  # newest first in each stream
            key = (r.get("prompt_id"), r.get("agent") or "")
            if t > have.get(key, (0,))[0]:
                have[key] = (t, r)
    have = {key: r for key, (_, r) in have.items()}
    fixed, now = [], time.time_ns()
    for key, r in want.items():
        if any((have.get(key) or {}).get(k) != r.get(k) for k in ("repo", "branch", "issue", "via")):
            fixed.append([str(now + len(fixed)), json.dumps(r)])
    for i in range(0, len(fixed), 500):
        push(fixed[i:i + 500], source="correct")
    return len(fixed)


def push(values, **labels):
    body = json.dumps({"streams": [{"stream": {**STREAM, **labels}, "values": values}]}).encode()
    req = urllib.request.Request(LOKI + "/loki/api/v1/push", body, {"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2)


def transcript_records(path, agent, found, ts_ns=None):
    """One record per (prompt, branch) a transcript's messages ran on, in message order.

    ts_ns: timestamp of the first record, the next ones 1ns apart (live). Default: the
    message timestamps (backfill)."""
    values, seen = [], set()
    with open(path, errors="replace") as f:
        for raw in f:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            sid, pid, cwd, ts = (msg.get(k) for k in ("sessionId", "promptId", "cwd", "timestamp"))
            if not agent and msg.get("isSidechain"):  # subagent messages copied into the session transcript
                continue
            if msg.get("type") not in ("user", "assistant") or not (sid and pid and cwd and ts):
                continue
            branch = message_branch(msg)
            if not branch or branch == "HEAD":
                continue
            if (pid, branch) in seen:
                continue
            seen.add((pid, branch))
            t = ts_ns + len(values) if ts_ns else int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1e9)
            values.append(record(sid, pid, agent, cwd, branch, t, found, None if agent else path))
    return values


def agent_type(transcript):
    try:
        with open(transcript[:-len(".jsonl")] + ".meta.json") as f:
            return json.load(f).get("agentType", "")
    except (OSError, ValueError):
        return ""


def hook():
    try:
        raw = sys.stdin.read()
        if os.environ.get("CLAUDE_BRANCH_DEBUG"):
            with open(os.environ["CLAUDE_BRANCH_DEBUG"], "a") as f:
                f.write(raw.strip() + "\n")
        event = json.loads(raw)
        values, found = [], set()
        if event.get("hook_event_name") == "SubagentStop":
            path = event.get("agent_transcript_path") or ""
            agent = event.get("agent_type") or agent_type(path)
            if path and agent:
                values = transcript_records(path, agent, found, time.time_ns())
        else:
            cwd = event.get("cwd") or os.getcwd()
            branch = git(cwd, "branch", "--show-current")
            if event.get("session_id") and event.get("prompt_id") and branch:
                values = [record(event["session_id"], event["prompt_id"], "", cwd, branch, time.time_ns(), found,
                                 event.get("transcript_path"))]
        if values:
            push(values)
        if found:
            sent = {(r["prompt_id"], r["agent"]) for r in (json.loads(v[1]) for v in values)}
            retag(found, sent)
    except Exception:
        pass


def backfill():
    values, found = [], set()
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        values += transcript_records(path, "", found)
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*/subagents/*.jsonl")):
        agent = agent_type(path)
        if agent:
            values += transcript_records(path, agent, found)
    # A stream per run: Loki rejects entries more than ~1h older than the newest one in a
    # stream. The dashboard keeps one record per prompt, so running it again is harmless.
    values.sort()
    run = str(int(time.time()))
    for i in range(0, len(values), 500):
        push(values[i:i + 500], source="backfill", run=run)
    # Queries only read the ingester for the last 3h of timestamps: write old entries to storage now.
    urllib.request.urlopen(urllib.request.Request(LOKI + "/flush", b""), timeout=10)
    fixed = correct(values)
    print(f"pushed {len(values)} branch records for {len({json.loads(v[1])['prompt_id'] for v in values})} prompts, "
          f"{fixed} sent again to correct the latest one")


if __name__ == "__main__":
    backfill() if "--backfill" in sys.argv else hook()
