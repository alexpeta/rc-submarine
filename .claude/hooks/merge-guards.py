#!/usr/bin/env python3
"""Three guards and a warning on Bash commands. A Claude Code PreToolUse hook: one JSON event on stdin.

Why: each of these is a mistake that is easy to make, invisible when made, and expensive to
undo, and each is a plain pattern on the command line. A prompt asks the model to remember;
this hook does not rely on it.

  1. MERGE NEEDS CHECKS. `gh pr merge` is denied unless the same command line first runs
     `gh pr checks <that same PR> --watch` joined with `&&`, so a failed or pending check stops
     the merge. Watching one PR does not clear the merge of another.
     Passes: `gh pr merge --auto` (the merge itself waits for the required checks), and a
     command that starts with `MERGE_GUARDS_CI_DOWN=1` (CI itself is down; say so in the QC
     comment, the assignment is visible in the transcript).
  2. NO PUSH TO A MERGED BRANCH. `git push` to a branch whose PR is MERGED (and has no open
     PR) is denied: a squash merge leaves the branch looking unmerged, the push goes nowhere,
     and the follow-up reads as shipped. Branch fresh from main instead. Needs `gh`; if `gh`
     cannot answer, the push is allowed (the guard never blocks offline work).
  3. FORCE-PUSH AN EXPLICIT SHA. A forced push whose refspec is `HEAD` or `HEAD:<branch>` is
     denied. `HEAD` is wherever the shell happens to be; push the sha you gated:
     `git push --force-with-lease origin <sha>:<branch>`. Plain `git push -u origin HEAD` is
     fine: it cannot overwrite anything.

  4. QC COMMENT ON A MERGE (a warning, never a deny). A `gh pr merge` the rules above allow is let through, and
     the model is told, through `additionalContext`, when its PR (resolved as rule 1 resolves it) has no single
     comment carrying both a `Filed:` and a `Playbook:` line, or has a `Second read:` line that records a read
     (does not open with `not triggered` / `not run`) and carries no literal `missed by the first read: <digit>`.
     The comments are read with `gh pr view <n> --json comments`; if they cannot be read, nothing is said. A deny
     keeps precedence: a denied merge gets the deny and no warning. Promote to a deny only on the ledger's evidence.

Matching rules (so a quoted word is never a command): heredoc bodies are dropped, quoted
strings are collapsed, and a command counts only where one starts: at the head of the line
or after `&&`, `||`, `;`, `|`. Any error in the hook → exit 0 with no output.

Ledger: each deny, and each merge the CI-down prefix lets through, appends one JSON line (ts, gate, rule
tag, repo, agent_type; never the command) to `~/.claude/gate-firings.jsonl`, or to
CLAUDE_GATE_FIRINGS_LEDGER. Each rule-4 warning appends one too, with its own rule tag and `"warn": true`. A
ledger that cannot be written changes nothing about the decision.

Test seams: MERGE_GUARDS_PR_STATES='{"branch": ["MERGED", "OPEN"]}' answers rule 2 without `gh`;
MERGE_GUARDS_PR_COMMENTS='{"5": ["body", …], "": [...]}' answers rule 4 ("" = the current branch's PR; a PR
not in the map is a failed read).
Self-test: `merge-guards.py --self-test`.
"""
import json, os, re, subprocess, sys, time

CI_DOWN = "MERGE_GUARDS_CI_DOWN"
FIRINGS_LEDGER = os.path.expanduser(os.environ.get("CLAUDE_GATE_FIRINGS_LEDGER", "~/.claude/gate-firings.jsonl"))
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
_REDIRECT = re.compile(r"\d*[<>]&\d+")
_SPLIT = re.compile(r"(&&|\|\||[;|\n]|&(?!&))")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _scrub(cmd):
    """Drop heredoc bodies and line continuations, collapse quoted strings."""
    cmd = cmd.replace("\\\n", " ")
    out, lines, i = [], cmd.split("\n"), 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC.search(line)
        i += 1
        if m:
            delim = m.group(2)
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1
    s = "\n".join(out)
    s = _QUOTED.sub("Q", s)
    return _REDIRECT.sub(" ", s)


def _segments(cmd):
    """[(joiner_before, env_assignments, tokens)] for each command start in the line."""
    parts = _SPLIT.split(_scrub(cmd))
    segs, joiner = [], ""
    for k, part in enumerate(parts):
        if k % 2:
            joiner = part
            continue
        toks = part.replace("(", " ").replace("{", " ").split()
        env = []
        while toks and (_ENV.match(toks[0]) or toks[0] in ("command", "time", "exec")):
            if _ENV.match(toks[0]):
                env.append(toks[0])
            toks = toks[1:]
        if toks:
            segs.append((joiner, env, toks))
        joiner = joiner if not toks else ""
    return segs


def _sub(toks, valued):
    """The tokens after `gh`/`git` global options: gh -R x pr merge → pr merge."""
    rest, i = toks[1:], 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in valued else 1
    return rest[i:], rest[:i]


def _is(toks, tool, valued, *sub):
    if not toks or toks[0] != tool:
        return None
    rest, _ = _sub(toks, valued)
    return rest if rest[: len(sub)] == list(sub) else None


_PR_VALUED = {"--body", "-b", "--body-file", "-F", "--subject", "-t", "--match-head-commit",
              "--author-email", "-A", "--interval", "-i", "--json", "--jq", "-q", "--template"}


def _pr_ref(toks):
    """(repo, PR) that `gh [-R repo] pr checks|merge [PR] …` names; PR None = the current branch's."""
    repo, i = None, 1
    while i < len(toks) and toks[i].startswith("-"):
        if toks[i] in ("-R", "--repo") and i + 1 < len(toks):
            repo = toks[i + 1]
        i += 2 if toks[i] in ("-R", "--repo") else 1
    rest = toks[i + 2:]  # after `pr checks`
    j = 0
    while j < len(rest):
        if rest[j].startswith("-"):
            j += 2 if rest[j] in _PR_VALUED else 1
            continue
        return repo, rest[j].rstrip("/").rsplit("/", 1)[-1].lstrip("#")  # a number, a branch or a URL
    return repo, None


def _push_parts(rest):
    """(flags, positional args after `push`) of `git push …`."""
    flags, pos = [], []
    for t in rest[1:]:
        (flags if t.startswith("-") else pos).append(t)
    return flags, pos


def _is_force(flags, refspecs):
    for f in flags:
        if f in ("--force", "-f") or f.startswith("--force-with-lease") or f == "--force-if-includes":
            return True
        if re.fullmatch(r"-[A-Za-z]+", f) and "f" in f[1:]:
            return True
    return any(r.startswith("+") for r in refspecs)


def _target_branch(spec, cwd):
    spec = spec.lstrip("+")
    dst = spec.split(":", 1)[1] if ":" in spec else spec
    if spec.startswith(":"):
        return None  # a delete
    if dst == "HEAD":
        try:
            dst = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True,
                                 text=True, timeout=5).stdout.strip()
        except Exception:
            return None
    return re.sub(r"^refs/heads/", "", dst) or None


def _pr_states(branch, cwd):
    seam = os.environ.get("MERGE_GUARDS_PR_STATES")
    if seam is not None:
        return json.loads(seam).get(branch, [])
    out = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "state",
                          "--limit", "20", "--jq", ".[].state"], cwd=cwd, capture_output=True, text=True,
                         timeout=10)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return out.stdout.split()


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def decide(cmd, cwd=None):
    """None to allow, or a deny dict."""
    return _decide(cmd, cwd)[0]


def _decide(cmd, cwd=None):
    """(None or a deny dict, True when the CI-down prefix is what let a merge through)."""
    watched = set()  # the PRs a `gh pr checks --watch` chained into this point has watched
    bypassed = False
    for joiner, env, toks in _segments(cmd):
        if joiner and joiner != "&&":
            watched = set()
        checks = _is(toks, "gh", ("-R", "--repo"), "pr", "checks")
        if checks is not None:
            if "--watch" in checks:
                watched.add(_pr_ref(toks))
            continue
        merge = _is(toks, "gh", ("-R", "--repo"), "pr", "merge")
        if merge is not None:
            if _pr_ref(toks) in watched or "--auto" in merge:
                continue
            if any(e.startswith(CI_DOWN + "=") for e in env):
                bypassed = True
                continue
            return _deny("MERGE NEEDS CHECKS: run it as `gh pr checks <n> --watch --fail-fast && gh pr merge <n> …` "
                         "with the same <n> in both, so a failed or pending check stops the merge (`gh pr merge "
                         "--auto` also waits for the required checks). If CI itself is down, say so in the QC "
                         "comment and prefix the command "
                         f"with `{CI_DOWN}=1`."), False
        push = _is(toks, "git", ("-C", "-c", "--git-dir", "--work-tree"), "push")
        if push is None:
            continue
        flags, pos = _push_parts(push)
        refspecs = pos[1:]
        if _is_force(flags, refspecs) and any(r.lstrip("+") == "HEAD" or r.lstrip("+").startswith("HEAD:")
                                              for r in refspecs):
            return _deny("FORCE-PUSH AN EXPLICIT SHA: `HEAD` is wherever the shell happens to be. Push the sha you "
                         "gated, `git push --force-with-lease origin <sha>:<branch>`, and print `pwd` and "
                         "`git rev-parse HEAD` in the same command."), False
        if "--delete" in flags or "-d" in flags or "--tags" in flags:
            continue
        branches = [b for b in (_target_branch(r, cwd) for r in refspecs) if b] or [None]
        for b in branches:
            if b is None:
                try:
                    b = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, capture_output=True,
                                       text=True, timeout=5).stdout.strip() if not refspecs else None
                except Exception:
                    b = None
            if not b or b in ("HEAD", "main", "master"):
                continue
            try:
                states = _pr_states(b, cwd)
            except Exception:
                continue  # gh cannot answer: allow
            if "MERGED" in states and "OPEN" not in states:
                return _deny(f"NO PUSH TO A MERGED BRANCH: the PR for `{b}` is already merged (a squash merge leaves "
                             "the branch looking unmerged). Branch fresh from origin/main and cherry-pick what is "
                             "left."), False
    return None, bypassed


# The QC comment's labels, read the way `bin/trials` reads them (bold or plain, a list marker allowed); a labelled
# line runs on over its wrapped lines to a blank line or the next bold label. Copied, not imported: this hook is
# placed alone in a project.
_QC_LABEL = re.compile(r"^\s*(?:[-*>]\s+)?(?:\*\*)?\s*(filed|playbook|second read)\s*:\s*(?:\*\*)?\s*(.*)$", re.I)
_MISSED = re.compile(r"missed by the first read\s*\**\s*:\s*\**\s*(\d+)", re.I)
_NOT_RUN = ("not triggered", "not run")
QC_LINES_RULE = "QC COMMENT LINES MISSING"
SECOND_READ_RULE = "SECOND READ COUNT MISSING"


def _qc_labels(body):
    """[(label, text)] for each labelled line of one comment body, wrapped lines joined."""
    out, cur = [], None
    for ln in (body or "").splitlines():
        m = _QC_LABEL.match(ln)
        if m:
            cur = [m.group(1).lower(), m.group(2)]
            out.append(cur)
        elif not ln.strip() or ln.lstrip().startswith(("**", "#", "|")):
            cur = None
        elif cur is not None:
            cur[1] += " " + ln.strip()
    return [tuple(t) for t in out]


def _merged_prs(cmd):
    """The (repo, PR) of each `gh pr merge` on the line, in order, once each."""
    out = []
    for _, _, toks in _segments(cmd):
        if _is(toks, "gh", ("-R", "--repo"), "pr", "merge") is not None and _pr_ref(toks) not in out:
            out.append(_pr_ref(toks))
    return out


def _pr_comments(repo, pr, cwd):
    """The comment bodies of a PR; raises when they cannot be read. Its own timeout keeps the hook inside its 15 s."""
    seam = os.environ.get("MERGE_GUARDS_PR_COMMENTS")
    if seam is not None:
        got = json.loads(seam).get(pr or "")
        if not isinstance(got, list):
            raise RuntimeError("no such PR in the seam")
        return got
    args = ["gh", "pr", "view"] + ([pr] if pr else []) + (["-R", repo] if repo else []) + ["--json", "comments"]
    out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=8)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return [c.get("body") or "" for c in json.loads(out.stdout).get("comments") or []]


def _qc_warnings(cmd, cwd=None):
    """[(rule, text)] for an allowed merge whose PR's QC comment is short of a line. Never raises: a PR whose
    comments cannot be read (no gh, network, timeout, no PR) gets no warning."""
    warns = []
    for repo, pr in _merged_prs(cmd):
        try:
            bodies = _pr_comments(repo, pr, cwd)
        except Exception:
            continue
        name = f"PR #{pr}" if pr else "the current branch's PR"
        labelled = [{label for label, _ in _qc_labels(b)} for b in bodies]
        if not any({"filed", "playbook"} <= ls for ls in labelled):
            has = [lab for lab in ("filed", "playbook") if any(lab in ls for ls in labelled)]
            if len(has) == 2:
                seen = "each line exists, but in separate comments"
            elif has:
                other = "Playbook" if has[0] == "filed" else "Filed"
                seen = f"missing: `{other}:`; a `{has[0].capitalize()}:` line exists"
            else:
                seen = "missing: both; no comment has either"
            warns.append((QC_LINES_RULE, f"{QC_LINES_RULE} (warning; the merge is proceeding): {name} has no comment "
                          f"carrying both a `**Filed:**` and a `**Playbook:**` line ({seen}). Post the QC comment "
                          "(templates/qc-comment.md) with both lines on the PR."))
        bad = sum(1 for b in bodies for label, text in _qc_labels(b) if label == "second read"
                  and not text.strip(" \t*\"'`<").lower().startswith(_NOT_RUN) and not _MISSED.search(text))
        if bad:
            warns.append((SECOND_READ_RULE, f"{SECOND_READ_RULE} (warning; the merge is proceeding): {name} has "
                          f"{bad} `**Second read:**` line(s) that record a read but carry no literal "
                          "`missed by the first read: <digit>`. Add the count as a numeral, or open the line with "
                          "`not triggered` / `not run` if no second read ran."))
    return warns


def _warn(texts):
    """Text put in front of the model while the call proceeds: `additionalContext` and no permissionDecision, so
    the normal permission flow still decides the call."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(texts)}}


def _record(rule, ev, warn=False):
    """Append one line to the gate-firings ledger: the rule tag, never the command. Any error is swallowed,
    so the hook's decision is the same with an unwritable ledger. A warning's line carries `"warn": true`; a deny's
    and a bypass's carry no such field."""
    try:
        # repo: the main checkout, as hooks/call-cap.py writes it (the first `worktree` line git prints), so the
        # two ledgers join and a pruned worktree's path is never recorded; null when git cannot say
        try:
            first = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=ev.get("cwd") or os.getcwd(),
                                   capture_output=True, text=True, timeout=5).stdout.splitlines()[:1]
        except Exception:
            first = []
        top = first[0][len("worktree "):].strip() if first and first[0].startswith("worktree ") else ""
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "gate": "merge-guards", "rule": rule,
                "repo": top or None, "agent_type": ev.get("agent_type")}
        if warn:
            line["warn"] = True
        os.makedirs(os.path.dirname(FIRINGS_LEDGER), exist_ok=True)
        with open(FIRINGS_LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass


def main():
    try:
        ev = json.load(sys.stdin)
        if ev.get("tool_name") != "Bash":
            return
        cmd = (ev.get("tool_input") or {}).get("command") or ""
        d, bypassed = _decide(cmd, ev.get("cwd"))
        if d:
            _record(d["hookSpecificOutput"]["permissionDecisionReason"].split(":")[0], ev)
            print(json.dumps(d))
            return
        if bypassed:
            _record("CI-DOWN BYPASS", ev)
        warns = _qc_warnings(cmd, ev.get("cwd"))
        for rule, _ in warns:
            _record(rule, ev, warn=True)
        if warns:
            print(json.dumps(_warn([t for _, t in warns])))
    except Exception:
        return


def _self_test():
    import tempfile
    me = os.path.abspath(__file__)
    # Every run writes its ledger lines into this test's own folder, never the real ledger. HOME does not move
    # it on Windows (expanduser reads USERPROFILE), so the path itself is overridden.
    tmp = tempfile.mkdtemp()
    scratch_ledger = os.path.join(tmp, "scratch.jsonl")

    def run(cmd, states=None, tool="Bash"):
        env = dict(os.environ, MERGE_GUARDS_PR_STATES=json.dumps(states or {}), MERGE_GUARDS_PR_COMMENTS="{}",
                   CLAUDE_GATE_FIRINGS_LEDGER=scratch_ledger)
        ev = {"tool_name": tool, "tool_input": {"command": cmd}}
        p = subprocess.run([sys.executable, me], input=json.dumps(ev), capture_output=True, text=True, env=env)
        assert p.returncode == 0, p.stderr
        if not p.stdout.strip():
            return None
        hso = json.loads(p.stdout)["hookSpecificOutput"]
        # every run here has a failed comments read (the seam is {}), so anything printed must be a deny
        assert "permissionDecisionReason" in hso, ("printed something that is not a deny", cmd, hso)
        return hso["permissionDecisionReason"].split(":")[0]

    # rule 1: each case is caught by this rule alone
    assert run("gh pr merge 5 --squash") == "MERGE NEEDS CHECKS"
    assert run("gh pr merge --squash") == "MERGE NEEDS CHECKS"
    assert run("gh -R o/r pr merge 5") == "MERGE NEEDS CHECKS"
    assert run("gh pr checks 5 --watch; gh pr merge 5") == "MERGE NEEDS CHECKS"
    assert run("gh pr checks 5 --watch || gh pr merge 5") == "MERGE NEEDS CHECKS"
    assert run("gh pr checks 5 --watch --fail-fast && gh pr merge 5 --squash") is None
    assert run("gh pr checks 5 --watch 2>&1 && echo ok && gh pr merge 5") is None
    assert run("gh pr merge 5 --auto --squash") is None
    # the watched PR must be the merged one
    assert run("gh pr checks 5 --watch && gh pr merge 6 --squash") == "MERGE NEEDS CHECKS"
    assert run("gh pr checks 5 --watch && gh pr merge") == "MERGE NEEDS CHECKS"
    assert run("gh pr checks --watch && gh pr merge 5") == "MERGE NEEDS CHECKS"
    assert run("gh pr checks --watch && gh pr merge --squash") is None
    assert run("gh pr checks 5 --watch && gh -R o/r pr merge 5") == "MERGE NEEDS CHECKS"
    assert run("gh -R o/r pr checks 5 --watch && gh -R o/r pr merge 5") is None
    assert run("gh pr checks 5 --watch && gh pr checks 6 --watch && gh pr merge 5") is None
    assert run("gh pr checks 5 --watch && gh pr merge 5 --body Q --squash") is None
    # a flag that takes a value, written before the PR number: its value is not the number
    assert run("gh pr checks 5 --watch && gh pr merge --squash --body Q 5") is None
    assert run("gh pr checks 5 --watch && gh pr merge -b Q 5") is None
    assert run("gh pr checks 5 --watch && gh pr merge --body Q 6") == "MERGE NEEDS CHECKS"
    for flag in ("--body", "-b", "--body-file", "-F", "--subject", "-t", "--match-head-commit", "--author-email",
                 "-A"):  # listed here, not read from the set: dropping one from the set must go red
        assert run(f"gh pr checks 5 --watch && gh pr merge {flag} Q 5") is None, flag
    assert run("gh pr checks 5 --interval 10 --watch && gh pr merge 5") is None
    assert run("gh pr checks --interval 10 5 --watch && gh pr merge 5") is None
    assert run("gh pr checks https://github.com/o/r/pull/5 --watch && gh pr merge 5") is None
    assert run("gh pr checks 5 --watch && gh pr merge 6 --auto") is None
    assert run(f"{CI_DOWN}=1 gh pr merge 5") is None
    assert run('echo "gh pr merge 5"') is None
    assert run('git commit -m "then gh pr merge 5"') is None
    assert run("cat <<'EOF'\nnotes\ngh pr merge 5\nEOF") is None
    assert run('git commit -m "ship it && gh pr merge 5"') is None
    assert run("git push origin 'a;git push --force origin HEAD'") is None
    assert run("gh pr view 5 --json state") is None
    # rule 2
    merged = {"feat/x": ["MERGED"]}
    assert run("git push origin feat/x", merged) == "NO PUSH TO A MERGED BRANCH"
    assert run("git push -u origin abc123:feat/x", merged) == "NO PUSH TO A MERGED BRANCH"
    assert run("git push origin feat/x", {"feat/x": ["MERGED", "OPEN"]}) is None
    assert run("git push origin feat/x", {"feat/x": []}) is None
    assert run("git push origin --delete feat/x", merged) is None
    assert run("git push origin :feat/x", merged) is None
    assert run('git commit -m "git push origin feat/x"', merged) is None
    # rule 3
    assert run("git push --force-with-lease origin HEAD:feat/x") == "FORCE-PUSH AN EXPLICIT SHA"
    assert run("git push -f origin HEAD") == "FORCE-PUSH AN EXPLICIT SHA"
    assert run("git push origin +HEAD:feat/x") == "FORCE-PUSH AN EXPLICIT SHA"
    assert run("git push --force-with-lease origin abc123:feat/x") is None
    assert run("git push -u origin HEAD") is None
    # not Bash, and bad input, stay silent
    assert run("gh pr merge 5", tool="Edit") is None
    p = subprocess.run([sys.executable, me], input="not json", capture_output=True, text=True,
                       env=dict(os.environ, CLAUDE_GATE_FIRINGS_LEDGER=scratch_ledger))
    assert p.returncode == 0 and not p.stdout.strip()

    # the ledger: one line per refusal, one per CI-down bypass, none for an allow; never the command text
    def fire(cmd, ledger, states=None, cwd=None, comments=None):
        env = dict(os.environ, MERGE_GUARDS_PR_STATES=json.dumps(states or {}),
                   MERGE_GUARDS_PR_COMMENTS=json.dumps(comments or {}), CLAUDE_GATE_FIRINGS_LEDGER=ledger)
        ev = {"tool_name": "Bash", "tool_input": {"command": cmd}, "agent_type": "builder"}
        if cwd:
            ev["cwd"] = cwd
        p = subprocess.run([sys.executable, me], input=json.dumps(ev), capture_output=True, text=True, env=env)
        assert p.returncode == 0 and not p.stderr, p.stderr
        try:
            with open(ledger, encoding="utf-8") as f:
                lines = [json.loads(x) for x in f.read().splitlines()]
        except (FileNotFoundError, NotADirectoryError):  # the second is what Linux raises for a path under a file
            lines = []
        return p.stdout, lines

    def expect(cmd, states=None):  # the hook's stdout as decided in this process, with no ledger in the path
        os.environ["MERGE_GUARDS_PR_STATES"] = json.dumps(states or {})
        try:
            return json.dumps(decide(cmd)) + "\n"
        finally:
            del os.environ["MERGE_GUARDS_PR_STATES"]

    not_git = tempfile.mkdtemp()
    cases = [("gh pr merge 5 --squash", None, "MERGE NEEDS CHECKS"),
             ("git push -f origin HEAD", None, "FORCE-PUSH AN EXPLICIT SHA"),
             ("git push origin feat/x", {"feat/x": ["MERGED"]}, "NO PUSH TO A MERGED BRANCH")]
    for k, (cmd, states, rule) in enumerate(cases):
        out, lines = fire(cmd, os.path.join(tmp, f"deny{k}", "gate-firings.jsonl"), states, cwd=not_git)
        assert out == expect(cmd, states), out  # the deny itself is unchanged by the ledger
        assert len(lines) == 1, lines
        assert lines[0]["gate"] == "merge-guards" and lines[0]["rule"] == rule, lines
        assert lines[0]["agent_type"] == "builder" and lines[0]["repo"] is None, lines  # not a git repo: null, never the cwd
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", lines[0]["ts"]), lines
        assert sorted(lines[0]) == ["agent_type", "gate", "repo", "rule", "ts"], lines
    # the repo is the main checkout, also for an event in a worktree of it
    git = lambda d, *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, capture_output=True, text=True)
    main, wt = os.path.join(tmp, "mainrepo"), os.path.join(tmp, "wt")
    os.makedirs(main)
    git(main, "init", "-q"), git(main, "commit", "-q", "--allow-empty", "-m", "x"), git(main, "worktree", "add", "-q", wt)
    top = git(main, "worktree", "list", "--porcelain").stdout.splitlines()[0][len("worktree "):]
    for k, d in enumerate((main, wt)):
        _, lines = fire("gh pr merge 5", os.path.join(tmp, f"top{k}.jsonl"), cwd=d)
        assert top and os.path.isdir(wt) and lines and lines[0]["repo"] == top, (d, top, lines)
    # an allow writes nothing and prints nothing at all
    allow = os.path.join(tmp, "allow.jsonl")
    for cmd in ("gh pr checks 5 --watch && gh pr merge 5", "gh pr merge 5 --auto", "git push -u origin HEAD", "ls"):
        out, lines = fire(cmd, allow)
        assert out == "" and lines == [], (cmd, out, lines)
    # the CI-down escape hatch lets the merge through and is logged
    out, lines = fire(f"{CI_DOWN}=1 gh pr merge 5", os.path.join(tmp, "bypass.jsonl"))
    assert out == "" and len(lines) == 1 and lines[0]["rule"] == "CI-DOWN BYPASS", (out, lines)
    # ... but not when the merge was watched anyway, nor when a later rule denies the line
    out, lines = fire(f"gh pr checks 5 --watch && {CI_DOWN}=1 gh pr merge 5", os.path.join(tmp, "bp2.jsonl"))
    assert out == "" and lines == [], lines
    out, lines = fire(f"{CI_DOWN}=1 gh pr merge 5 && git push -f origin HEAD", os.path.join(tmp, "bp3.jsonl"))
    assert [x["rule"] for x in lines] == ["FORCE-PUSH AN EXPLICIT SHA"], lines
    # an unwritable ledger (its parent is a file) changes nothing about the decision
    blocker = os.path.join(tmp, "blocker")
    with open(blocker, "w") as f:
        f.write("x")
    dead = os.path.join(blocker, "sub", "gate-firings.jsonl")
    for cmd, states in (("gh pr merge 5", None), ("git push origin feat/x", {"feat/x": ["MERGED"]}),
                        ("git push --force-with-lease origin HEAD:feat/x", None)):
        out, _ = fire(cmd, dead, states)
        assert out == expect(cmd, states) and out.strip(), (cmd, out)
    for cmd in (f"{CI_DOWN}=1 gh pr merge 5", "gh pr merge 5 --auto"):
        out, _ = fire(cmd, dead)
        assert out == "", (cmd, out)
    assert not os.path.exists(os.path.join(blocker, "sub"))

    # rule 4: an allowed merge whose PR's QC comment is short of a line is warned about, never denied
    ok_merge = "gh pr checks 5 --watch && gh pr merge 5 --squash"
    full = "## QC read\n\n**Filed:** none.\n**Playbook:** none proposed.\n**Second read:** not triggered\n"
    n = [0]

    def warned(cmd, comments, rules):
        """Fire with these comments; assert the call is not denied and exactly `rules` were warned and logged."""
        n[0] += 1
        out, lines = fire(cmd, os.path.join(tmp, f"warn{n[0]}.jsonl"), comments=comments)
        if not rules:
            assert out == "" and lines == [], (cmd, comments, out, lines)
            return ""
        assert out.strip(), ("expected a warning, got no output", cmd, comments, lines)
        hso = json.loads(out)["hookSpecificOutput"]
        assert sorted(hso) == ["additionalContext", "hookEventName"], hso  # no permissionDecision: the call proceeds
        assert hso["hookEventName"] == "PreToolUse", hso
        assert [x["rule"] for x in lines] == rules, lines
        for x in lines:
            assert x["warn"] is True and x["gate"] == "merge-guards", x
            assert sorted(x) == ["agent_type", "gate", "repo", "rule", "ts", "warn"], x
        for r in rules:
            assert r + " (warning; the merge is proceeding)" in hso["additionalContext"], hso
        return hso["additionalContext"]

    # a complete QC comment: silent
    warned(ok_merge, {"5": ["LGTM", full]}, [])
    # no QC comment at all: warned, both lines named
    text = warned(ok_merge, {"5": []}, ["QC COMMENT LINES MISSING"])
    assert "PR #5" in text and "missing: both" in text, text
    text = warned(ok_merge, {"5": ["looks good"]}, ["QC COMMENT LINES MISSING"])
    # Filed without Playbook: warned, and the missing one is named
    text = warned(ok_merge, {"5": ["**Filed:** none.\n**Second read:** not run.\n"]}, ["QC COMMENT LINES MISSING"])
    assert "missing: `Playbook:`" in text, text
    text = warned(ok_merge, {"5": ["**Playbook:** none proposed.\n"]}, ["QC COMMENT LINES MISSING"])
    assert "missing: `Filed:`" in text, text
    # both lines, but in separate comments: one comment must carry both
    text = warned(ok_merge, {"5": ["**Filed:** none.\n", "**Playbook:** none proposed.\n"]},
                  ["QC COMMENT LINES MISSING"])
    assert "separate comments" in text, text
    # plain (unbolded) labels and a list marker read as labels, as bin/trials reads them
    warned(ok_merge, {"5": ["- Filed: none.\nPlaybook: none proposed.\n"]}, [])
    # a Second read that ran: the count must be a literal digit
    ran = "**Filed:** none.\n**Playbook:** none proposed.\n**Second read:** opus, head abc, "
    text = warned(ok_merge, {"5": [ran + "it missed one finding.\n"]}, ["SECOND READ COUNT MISSING"])
    assert "missed by the first read: <digit>" in text and "1 `**Second read:**` line(s)" in text, text
    warned(ok_merge, {"5": [ran + "**missed by the first read: 0**\n"]}, [])
    warned(ok_merge, {"5": [ran + "found x2: 1,\nmissed by the first read: 2\n"]}, [])  # wrapped onto the next line
    warned(ok_merge, {"5": [ran + "found x2: 1\n\nmissed by the first read: 2\n"]},  # a blank line ends the label
           ["SECOND READ COUNT MISSING"])
    # "not triggered" / "not run" need no count
    warned(ok_merge, {"5": [full.replace("not triggered", '"not triggered"')]}, [])
    warned(ok_merge, {"5": [full.replace("not triggered", "not run. A test-only change.")]}, [])
    # both warnings at once, in order, one ledger line each
    text = warned(ok_merge, {"5": ["**Second read:** opus, head abc, none missed\n"]},
                  ["QC COMMENT LINES MISSING", "SECOND READ COUNT MISSING"])
    # the PR is resolved as rule 1 resolves it: the number, else the current branch's PR; --auto and CI-down merges
    # are allowed merges too
    text = warned("gh pr checks --watch && gh pr merge --squash", {"": []}, ["QC COMMENT LINES MISSING"])
    assert "the current branch's PR" in text, text
    warned("gh pr merge 5 --auto", {"5": []}, ["QC COMMENT LINES MISSING"])
    warned("gh pr merge 5 --auto", {"6": []}, [])  # PR 6's comments are not PR 5's
    out, lines = fire(f"{CI_DOWN}=1 gh pr merge 5", os.path.join(tmp, "warn-bypass.jsonl"), comments={"5": []})
    assert [x["rule"] for x in lines] == ["CI-DOWN BYPASS", "QC COMMENT LINES MISSING"], lines
    assert "warn" not in lines[0] and lines[1]["warn"] is True, lines
    # gh cannot answer (the PR is not in the seam): silent, exit 0
    warned(ok_merge, {}, [])
    # ... and one PR that cannot be read does not silence another merged on the same line
    warned("gh pr merge 6 --auto && gh pr merge 5 --auto", {"5": []}, ["QC COMMENT LINES MISSING"])
    # ... nor can it when gh is not on the PATH at all, with no seam: silent, exit 0
    env = dict(os.environ, PATH=tempfile.mkdtemp(), CLAUDE_GATE_FIRINGS_LEDGER=os.path.join(tmp, "nogh.jsonl"))
    env.pop("MERGE_GUARDS_PR_COMMENTS", None)
    p = subprocess.run([sys.executable, me], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": ok_merge}}),
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout == "" and not p.stderr, (p.returncode, p.stdout, p.stderr)
    assert not os.path.exists(os.path.join(tmp, "nogh.jsonl"))
    # a denied merge keeps its deny, and gets no warning and no warning line
    out, lines = fire("gh pr merge 5 --squash", os.path.join(tmp, "warn-deny.jsonl"), comments={"5": []})
    assert out == expect("gh pr merge 5 --squash") and "additionalContext" not in out, out
    assert [x["rule"] for x in lines] == ["MERGE NEEDS CHECKS"], lines
    # a command that is not a merge prints nothing at all, whatever the comments say
    for cmd in ("ls", "gh pr view 5 --json comments", 'echo "gh pr merge 5"'):
        out, lines = fire(cmd, os.path.join(tmp, "warn-none.jsonl"), comments={"5": [], "": []})
        assert out == "" and lines == [], (cmd, out, lines)
    print("self-test OK: merge needs a chained --watch (or --auto, or the CI-down prefix); quoted words and "
          "heredocs are not commands; a push to a merged branch is denied unless a PR is open; a forced push of "
          "HEAD is denied, a plain one is not; other tools and bad input stay silent; each deny and each CI-down "
          "bypass writes one ledger line, an allow none, and an unwritable ledger changes no decision; an allowed merge "
          "whose PR lacks a comment with Filed: and Playbook:, or has a run Second read with no digit, is warned "
          "(never denied) and logged with warn: true, and a PR whose comments cannot be read gets nothing")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    else:
        main()
