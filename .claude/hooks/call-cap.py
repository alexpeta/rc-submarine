#!/usr/bin/env python3
"""The builder call cap (2026-09-14). A Claude Code hook: one JSON event on stdin.

Why: five builds measured from their transcripts put 91 % of their price in cache re-reads —
every tool call re-sends the whole context, so a build's cost grows with the square of its
length. A 362-call build cost a sixth of a 1,300-call one at the same QC bar. The playbook
scopes a dispatch to ~400 calls; this hook is the counter a builder cannot keep for itself.

Applies ONLY to agents whose `agent_type` is in CAPPED_AGENTS — the env
`CLAUDE_CALL_CAP_AGENTS`, comma-separated, default `builder,builder-sonnet` (the playbook's two builder
names; a repo that renamed its builders sets the env in the hook command). Two kinds of builder carry it: a
subagent (`agent_type` and `agent_id` both set), and a top-level session started as one, such as
`claude --agent builder` in its own terminal (`agent_type` set, no `agent_id`: read off a real
session's events, 2026-11-10). A subagent is counted per `agent_id`, a top-level one per
`session_id`, across every task the session is given and across a resume, which keeps the id: its
context grows the same way. Agents whose `agent_type` is in COUNTED_AGENTS (env
`CLAUDE_CALL_COUNT_AGENTS`, default `advisor,qc-reader,reader`) are counted and get their ledger
line, and are never warned or denied: their line is how a trial of that seat is counted. The
chair's session (no `agent_type`) and every other agent are untouched. The counter is a file
under the temp folder (operational state; never in the repo).

  * below WARN_AT: silent.
  * WARN_AT..CAP-1: PostToolUse `additionalContext` every WARN_EVERY calls — "write the handoff".
  * CAP..CAP+GRACE: PreToolUse DENIES everything except a handoff-shaped call — a Write/Edit
    under a scratchpad path, or a Bash command that is `git add|commit|push|status|log|
    rev-parse|diff` or writes into a scratchpad path — so the builder can record what it
    decided, what moved, what is left and the head sha, commit and push, and report.
  * past CAP+GRACE: everything denied (a runaway).
  * SubagentStop (a subagent) or SessionEnd (a top-level session): one JSON line to
    ~/.claude/builder-calls.jsonl (the measurement: calls by tool, capped or not). The counter file
    stays in the temp folder: a resumed session keeps its `session_id` and a subagent continued with
    SendMessage keeps its `agent_id` (both read off real events, 2026-11-10), so each goes on from its
    total, and each stop writes a line with the running total (the last line per `agent_id` is the
    build's). Only a build that was counted gets a line: Claude Code also fires
    SubagentStop for its own side agents (prompt suggestions, /btw), carrying the session's `--agent` name.
    The line's `repo` is the main checkout of the repo the build ran in: the first `worktree` line of
    `git worktree list --porcelain` run in the event's `cwd`, as git prints it, so a build in one of the
    repo's worktrees names the repo, not the worktree (which is often deleted soon after). It is read at
    the build's first counted call and kept in the counter (a resumed session's cwd may be gone by its
    exit); when git cannot say (no cwd, not a repo, git missing or slower than 3 s) it is null, never a guess.

Never breaks a build: any error → exit 0 with no output. Self-test: `call-cap.py --self-test`.
"""
import json, os, re, sys, tempfile, time

CAPPED_AGENTS = set(filter(None, os.environ.get("CLAUDE_CALL_CAP_AGENTS", "builder,builder-sonnet").split(",")))
COUNTED_AGENTS = set(filter(None, os.environ.get("CLAUDE_CALL_COUNT_AGENTS", "advisor,qc-reader,reader").split(",")))
CAP = int(os.environ.get("CLAUDE_CALL_CAP", "400"))
GRACE = int(os.environ.get("CLAUDE_CALL_CAP_GRACE", "25"))
WARN_AT = int(os.environ.get("CLAUDE_CALL_CAP_WARN_AT", "360"))
WARN_EVERY = 20
STATE_DIR = os.path.join(tempfile.gettempdir(), "claude-call-cap")
LEDGER = os.path.expanduser(os.environ.get("CLAUDE_CALL_CAP_LEDGER", "~/.claude/builder-calls.jsonl"))
_GIT_OK = re.compile(r"^\s*(cd\s+\S+\s*(&&|;)\s*)?git\s+(add|commit|push|status|log|rev-parse|diff|branch\s+--show-current)\b")


def _path(agent_id):
    return os.path.join(STATE_DIR, re.sub(r"[^A-Za-z0-9_-]", "_", agent_id) + ".json")


def _load(agent_id):
    try:
        with open(_path(agent_id)) as f:
            return json.load(f)
    except Exception:
        return {"calls": 0, "by_tool": {}, "first": time.time(), "capped": False}


def _key(ev):
    """The counter's key, or None for an event the hook does not touch. Per subagent `agent_id`; for a
    top-level session started as a capped or counted agent (no `agent_id`), per `session_id`."""
    if ev.get("agent_type") not in CAPPED_AGENTS | COUNTED_AGENTS:
        return None
    if ev.get("agent_id"):
        return ev["agent_id"]
    return "session-" + ev["session_id"] if ev.get("session_id") else None


def _repo(cwd):
    """The main checkout of the repo at `cwd`, as git prints it, or None when git cannot say. Bounded."""
    try:
        import subprocess
        out = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=cwd, capture_output=True, text=True,
                             timeout=3).stdout
        first = out.splitlines()[0] if out.strip() else ""
        if not first.startswith("worktree "):
            return None
        return first[len("worktree "):].strip() or None
    except Exception:
        return None


def _save(agent_id, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = _path(agent_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, _path(agent_id))


def _handoff_shaped(tool, inp):
    if tool in ("Write", "Edit", "MultiEdit"):
        return "scratchpad" in str(inp.get("file_path", ""))
    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        return bool(_GIT_OK.match(cmd)) or "scratchpad" in cmd
    return False


def _handoff_reason(n):
    return (
        f"CALL CAP: this is tool call {n} of a dispatch scoped to {CAP} (the builder definition's call cap, "
        "2026-09-14 — every call re-sends your whole context, so the build's cost is now growing with the "
        "square of its length). Stop building. Do exactly this, in this order: (1) write a HANDOFF to your "
        "scratch path (`…/scratchpad/<ticket>/handoff.md`): what was decided and why, what moved (files, "
        "functions, tests), what is left, the gate/tier state, and the head sha; (2) `git add` + `git commit` "
        "what is committable and `git push`; (3) reply with the handoff path and the head sha. Only "
        "handoff-shaped calls are allowed now: Write/Edit under a scratchpad path, and `git add|commit|push|"
        "status|log|rev-parse|diff`. The chair dispatches the next phase into a fresh context."
    )


def main():
    raw = sys.stdin.read()
    ev = json.loads(raw) if raw.strip() else {}
    agent_id = _key(ev)
    if not agent_id:
        return
    capped_type = ev.get("agent_type") in CAPPED_AGENTS
    event = ev.get("hook_event_name")
    st = _load(agent_id)
    if event == "PreToolUse":
        st["calls"] += 1
        n = st["calls"]
        tool = ev.get("tool_name", "?")
        st["by_tool"][tool] = st["by_tool"].get(tool, 0) + 1
        if "repo" not in st:
            st["repo"] = _repo(ev["cwd"]) if ev.get("cwd") else None
        if capped_type and n >= CAP:
            st["capped"] = True
            allowed = n <= CAP + GRACE and _handoff_shaped(tool, ev.get("tool_input") or {})
            _save(agent_id, st)
            if not allowed:
                reason = _handoff_reason(n) if n <= CAP + GRACE else (
                    f"CALL CAP EXCEEDED: {n} calls, {GRACE} past the {CAP} cap. Every call is denied now. "
                    "Reply with what you have: the handoff path if you wrote one, the head sha, and what is left."
                )
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}))
            return
        _save(agent_id, st)
    elif event == "PostToolUse":
        n = st["calls"]
        if capped_type and WARN_AT <= n < CAP and (n - WARN_AT) % WARN_EVERY == 0:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": (
                f"CALL CAP WARNING: tool call {n} of {CAP}. Finish the current step and write the handoff "
                "(decided / moved / left / head sha) to your scratch path before call " + str(CAP) + "; "
                "past it only handoff-shaped calls are allowed."
            )}}))
    elif event in ("SubagentStop", "SessionEnd"):
        if not os.path.exists(_path(agent_id)):
            return  # never counted: a side agent's SubagentStop, or a session that made no tool call
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agent_id": agent_id, "agent_type": ev.get("agent_type"),
                "repo": st.get("repo"),
                "calls": st["calls"], "by_tool": st["by_tool"], "capped": st["capped"], "minutes": round((time.time() - st.get("first", time.time())) / 60, 1)}
        if event == "SessionEnd":
            line["session"] = True
        os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
        with open(LEDGER, "a") as f:
            f.write(json.dumps(line) + "\n")
        # the counter stays in the temp folder: a resumed session and a continued subagent carry on from it,
        # so a stop does not reset the cap


def _self_test():
    import subprocess
    me = os.path.abspath(__file__)
    env = dict(os.environ, CLAUDE_CALL_CAP="5", CLAUDE_CALL_CAP_GRACE="2", CLAUDE_CALL_CAP_WARN_AT="3", TMPDIR=tempfile.mkdtemp())
    for name in ("CLAUDE_CALL_CAP_AGENTS", "CLAUDE_CALL_COUNT_AGENTS"):
        env.pop(name, None)  # the defaults are under test: a session that sets them must not mask them
    aid = "selftest-" + str(int(time.time()))
    def run(ev):
        p = subprocess.run([sys.executable, me], input=json.dumps(ev), capture_output=True, text=True, env=env)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout) if p.stdout.strip() else {}
    pre = lambda tool, inp: run({"hook_event_name": "PreToolUse", "agent_id": aid, "agent_type": "builder", "tool_name": tool, "tool_input": inp})
    post = lambda: run({"hook_event_name": "PostToolUse", "agent_id": aid, "agent_type": "builder", "tool_name": "Bash", "tool_input": {}})
    assert run({"hook_event_name": "PreToolUse", "agent_type": "Explore", "agent_id": "x", "tool_name": "Read", "tool_input": {}}) == {}, "an agent neither capped nor counted must be untouched"
    assert run({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}) == {}, "the main session must be untouched"
    assert pre("Bash", {"command": "ls"}) == {} and post() == {}          # 1
    assert pre("Bash", {"command": "ls"}) == {} and post() == {}          # 2
    assert pre("Bash", {"command": "ls"}) == {}                            # 3 → warn
    assert "CALL CAP WARNING" in post()["hookSpecificOutput"]["additionalContext"]
    assert pre("Bash", {"command": "ls"}) == {}                            # 4
    d = pre("Bash", {"command": "poetry run pytest"})                      # 5 = cap → deny
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny" and "HANDOFF" in d["hookSpecificOutput"]["permissionDecisionReason"]
    assert pre("Write", {"file_path": "/tmp/x/scratchpad/2684/handoff.md", "content": "x"}) == {}   # 6: handoff allowed
    assert pre("Bash", {"command": "git add -A && git commit -m x && git push"}) == {}               # 7: git allowed (cap+2 = grace edge)
    d = pre("Bash", {"command": "git status"})                                                      # 8: past grace → denied
    assert d["hookSpecificOutput"]["permissionDecision"] == "deny" and "EXCEEDED" in d["hookSpecificOutput"]["permissionDecisionReason"]
    # The ledger goes to the test's own folder. HOME does not move it on Windows (expanduser reads USERPROFILE).
    env["CLAUDE_CALL_CAP_LEDGER"] = os.path.join(env["TMPDIR"], "builder-calls.jsonl")
    run({"hook_event_name": "SubagentStop", "agent_id": aid, "agent_type": "builder"})
    led = open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()
    rec = json.loads(led[-1]); assert rec["calls"] == 8 and rec["capped"] is True and rec["by_tool"]["Bash"] == 7, rec
    # A top-level session started as a builder (`claude --agent builder`): agent_type, no agent_id.
    sid = "sess-" + aid
    top = lambda tool: run({"hook_event_name": "PreToolUse", "session_id": sid, "agent_type": "builder", "tool_name": tool, "tool_input": {"command": "ls"}})
    assert run({"hook_event_name": "PreToolUse", "session_id": sid, "tool_name": "Bash", "tool_input": {}}) == {}, "the chair's session must be untouched"
    for _ in range(4):
        assert top("Bash") == {}
    d = top("Bash")                                                                                 # 5 = cap → deny, as a subagent's
    assert d.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", "a top-level builder must be capped"
    # A side agent's SubagentStop carries the session's --agent name and an id never counted: no line.
    run({"hook_event_name": "SubagentStop", "session_id": sid, "agent_id": "side-" + aid, "agent_type": "builder"})
    assert len(open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()) == 1, "a side agent must write no line"
    run({"hook_event_name": "SessionEnd", "session_id": sid, "agent_type": "builder", "reason": "other"})
    led = open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()
    top_rec = json.loads(led[-1]); assert len(led) == 2 and top_rec["calls"] == 5 and top_rec["session"] is True, top_rec
    # A resumed session keeps its session_id: the count goes on from its total, and its next line carries it.
    assert top("Bash").get("hookSpecificOutput", {}).get("permissionDecision") == "deny", "a resumed top-level builder is still capped"
    run({"hook_event_name": "SessionEnd", "session_id": sid, "agent_type": "builder", "reason": "other"})
    led = open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()
    again = json.loads(led[-1]); assert len(led) == 3 and again["calls"] == 6 and again["agent_id"] == top_rec["agent_id"], again
    # A counted seat (the advisor) is never warned or denied, however long it runs, and gets its ledger line.
    adv = "selftest-adv-" + str(int(time.time()))
    for _ in range(9):
        assert run({"hook_event_name": "PreToolUse", "agent_id": adv, "agent_type": "advisor", "tool_name": "Read", "tool_input": {}}) == {}, "a counted seat is never denied"
        assert run({"hook_event_name": "PostToolUse", "agent_id": adv, "agent_type": "advisor", "tool_name": "Read", "tool_input": {}}) == {}, "a counted seat is never warned"
    run({"hook_event_name": "SubagentStop", "agent_id": adv, "agent_type": "advisor"})
    rec2 = json.loads(open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()[-1])
    assert rec2["agent_type"] == "advisor" and rec2["calls"] == 9 and rec2["capped"] is False, rec2
    run({"hook_event_name": "SubagentStop", "agent_id": "x", "agent_type": "Explore"})
    assert len(open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()) == 4, "an uncounted agent writes no ledger line"
    state = os.path.join(env["TMPDIR"], "claude-call-cap")
    assert os.path.isdir(state) and not os.path.exists(os.path.join(state, "x.json")), "an uncounted agent leaves no counter"
    # builder-sonnet is capped with no env at all: the default list names both builders.
    son = "selftest-son-" + str(int(time.time()))
    for _ in range(4):
        assert run({"hook_event_name": "PreToolUse", "agent_id": son, "agent_type": "builder-sonnet", "tool_name": "Bash", "tool_input": {}}) == {}
    d = run({"hook_event_name": "PreToolUse", "agent_id": son, "agent_type": "builder-sonnet", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert d.get("hookSpecificOutput", {}).get("permissionDecision") == "deny", "builder-sonnet must be capped by default"
    run({"hook_event_name": "SubagentStop", "agent_id": son, "agent_type": "builder-sonnet"})
    led = open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()
    assert len(led) == 5 and json.loads(led[-1])["agent_type"] == "builder-sonnet", "a Sonnet build writes its line"
    # The repo: a build in a linked worktree names the main checkout, read at its first call.
    base = tempfile.mkdtemp()
    main_co, wt = os.path.join(base, "main"), os.path.join(base, "wt")
    git = lambda *a: subprocess.run(["git", *a], capture_output=True, text=True, check=True).stdout.strip()
    git("init", "-q", main_co)
    git("-C", main_co, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "t")
    git("-C", main_co, "worktree", "add", "-q", wt)
    want = git("-C", main_co, "rev-parse", "--show-toplevel")
    assert want and git("-C", wt, "rev-parse", "--show-toplevel") != want, "fixture: the worktree's own toplevel differs from the main checkout"

    def build(aid_, cwd):
        ev = {"agent_id": aid_, "agent_type": "builder", "tool_name": "Read", "tool_input": {}}
        if cwd is not None:
            ev["cwd"] = cwd
        assert run(dict(ev, hook_event_name="PreToolUse")) == {}
        assert run(dict(ev, hook_event_name="PreToolUse", cwd=main_co if cwd is None else os.path.join(base, "gone"))) == {}  # a later cwd does not move it
        run({"hook_event_name": "SubagentStop", "agent_id": aid_, "agent_type": "builder"})
        return json.loads(open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()[-1])
    rec3 = build("selftest-wt-" + aid, wt)
    assert rec3.get("repo") == want and rec3["calls"] == 2, ("a worktree build's repo is the main checkout", want, rec3)
    # git cannot say (not a repo, a cwd that is gone, no cwd): the field is null, the build is untouched, the line is written.
    for k, cwd in enumerate((tempfile.mkdtemp(), os.path.join(base, "gone"), None)):
        r = build(f"selftest-norepo{k}-" + aid, cwd)
        assert "repo" in r and r["repo"] is None and r["calls"] == 2, ("an unresolvable repo is null, never a guess", cwd, r)
    # A subagent continued after its stop (SendMessage) keeps its agent_id: the count goes on, and its next line carries it.
    cont = "selftest-cont-" + aid
    go = lambda: run({"hook_event_name": "PreToolUse", "agent_id": cont, "agent_type": "builder", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    n0 = len(open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines())
    assert go() == {} and go() == {}
    run({"hook_event_name": "SubagentStop", "agent_id": cont, "agent_type": "builder"})
    assert go() == {} and go() == {}                                                                  # 3, 4
    assert go().get("hookSpecificOutput", {}).get("permissionDecision") == "deny", "a continued subagent is capped on its total"
    run({"hook_event_name": "SubagentStop", "agent_id": cont, "agent_type": "builder"})
    led = open(env["CLAUDE_CALL_CAP_LEDGER"]).read().strip().splitlines()
    c1, c2 = json.loads(led[-2]), json.loads(led[-1])
    assert len(led) == n0 + 2 and (c1["agent_id"], c1["calls"], c2["agent_id"], c2["calls"]) == (cont, 2, cont, 5), (c1, c2)
    print("self-test OK: reader/chair untouched; warn at 3; deny at 5; handoff Write + git allowed through grace; hard deny after; ledger line", rec["calls"], "calls;",
          "a top-level builder capped per session, across a resume, and logged at SessionEnd,", again["calls"], "calls; a side agent's stop writes nothing;",
          "a counted advisor: 9 calls, no warn, no deny, its own line; builder-sonnet capped with no env;",
          "a worktree build's repo is its main checkout; an unresolvable repo is null")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        try:
            main()
        except Exception:
            pass
        sys.exit(0)
