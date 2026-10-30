"""RepoDojo sessions: learn a repo, fix a planted bug, get scored like the new interview.

Model routing (all on Nebius Token Factory):
- Super: repo tour, bug forging, hidden edge-case tests (structured generation)
- Nano: bug ticket and the in-interview AI assistant (fast, chatty)
- Ultra (reasoning on): the scorecard
Tests decide everything that can be decided by running code.
"""
import difflib
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field

from . import llm, repo as repolib, runner as runlib

RUNNER = None
SESSIONS: dict[str, "Session"] = {}
LAST: dict[str, dict] = {}  # slug -> last tour/bug/ticket, for rehearsing the same exercise
TRAP_TURN = 2  # the assistant's 2nd answer contains one plausible but wrong claim


def get_runner():
    global RUNNER
    if RUNNER is None:
        RUNNER = runlib.make_runner()
    return RUNNER


@dataclass
class Session:
    id: str
    repo: repolib.Repo
    baseline_failed: set = field(default_factory=set)
    tour: dict = field(default_factory=dict)
    bug: dict = field(default_factory=dict)        # ground truth, never sent to the browser
    bugged: dict = field(default_factory=dict)     # path -> source with the bug applied
    ticket: dict = field(default_factory=dict)
    chat: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    started: float = field(default_factory=time.time)

    def source(self, path):
        return self.bugged.get(path, self.repo.files.get(path, ""))


def event(step, status, **payload):
    return {"step": step, "status": status, **payload}


def new_failures(result, baseline):
    return [t for t in result["failed"] if t not in baseline]


# ---------------------------------------------------------------- session setup

def start(repo_input: str, replay: bool = False):
    llm.USAGE.clear()
    slug = repolib.parse_slug(repo_input)
    if repolib.CURATED and slug not in repolib.CURATED and not _allow_any():
        raise ValueError("The hosted demo runs curated repos only: " + ", ".join(repolib.CURATED))

    yield event("fetch", "running")
    repo = repolib.load(slug)
    s = Session(id=uuid.uuid4().hex[:12], repo=repo)
    SESSIONS[s.id] = s
    yield event("fetch", "done", result={"files": len(repo.files), "functions": len(repo.funcs)})

    yield event("baseline", "running", runner=get_runner().name)
    base = get_runner().run(repo.root, {}, repo.tests_dir)
    if base.get("error"):
        raise RuntimeError(f"The test suite didn't run: {base['error'][:300]}")
    s.baseline_failed = set(base["failed"])
    yield event("baseline", "done", result={"passed": len(base["passed"]), "failed": len(base["failed"]),
                                            "seconds": base.get("seconds"), "runner": base["runner"]})

    if replay and slug in LAST:
        s.tour, s.bug, s.bugged, s.ticket = (LAST[slug][k] for k in ("tour", "bug", "bugged", "ticket"))
        for step in ("tour", "forge", "ticket"):
            yield event(step, "done", result={"replayed": True})
        yield event("ready", "done", result=public_state(s))
        return

    yield event("tour", "running", tier="super")
    s.tour = make_tour(repo)
    yield event("tour", "done", tier="super", result={"steps": len(s.tour["steps"])})

    yield event("forge", "running", tier="super")
    attempts = forge_bug(s)
    yield event("forge", "done", tier="super", result={"attempts": attempts})

    yield event("ticket", "running", tier="nano")
    s.ticket = write_ticket(s)
    yield event("ticket", "done", tier="nano")
    LAST[slug] = {"tour": s.tour, "bug": s.bug, "bugged": s.bugged, "ticket": s.ticket}

    yield event("ready", "done", result=public_state(s))


def _allow_any():
    import os
    return os.getenv("ALLOW_ANY_REPO") == "1"


def public_state(s: Session) -> dict:
    tree = sorted(list(s.repo.files) + [p for p in s.repo.other if p.count("/") < 2][:40])
    return {"session": s.id, "repo": s.repo.slug, "tree": tree, "tour": s.tour, "ticket": s.ticket,
            "runner": get_runner().name, "tests": len(s.runs), "usage": list(llm.USAGE)}


# ---------------------------------------------------------------- learn: the repo tour

def make_tour(repo: repolib.Repo) -> dict:
    tour = llm.chat_json("super", [
        {"role": "system", "content": "You are a senior engineer onboarding a new teammate onto an unfamiliar codebase. "
                                      "Teach it step by step, in reading order. Only mention files and functions that appear in the outline. Reply with JSON only."},
        {"role": "user", "content": f"""{repolib.outline(repo)}

Write a 5-step guided tour that gets someone productive in this codebase fast:
architecture first, then the main entry points, then how a typical call flows through the code, then the tests.
Return JSON:
{{"summary": "2 sentences: what this project does and how it's organised",
  "steps": [{{"title": "short",
             "files": ["paths from the outline"],
             "symbols": ["function or Class.method names from the outline"],
             "explain": "3-5 sentences, concrete, naming the symbols",
             "look_for": "one thing to notice when reading this code",
             "quiz": {{"q": "a question answerable from this step", "options": ["4 options"], "answer": 0, "why": "one sentence"}}}}]}}"""},
    ], step="Build repo tour", max_tokens=4000, temperature=0.3)
    # Keep only references that really exist.
    names = {f.qualname for f in repo.funcs} | {f.name for f in repo.funcs} | set(repo.classes)
    steps = []
    for st in tour.get("steps", [])[:6]:
        st["files"] = [f for f in st.get("files", []) if f in repo.files]
        st["symbols"] = [x for x in st.get("symbols", []) if x in names or x.split(".")[-1] in names]
        st["symbols"] = [{"name": x, **_locate(repo, x)} for x in st["symbols"]]
        q = st.get("quiz") or {}
        if not (isinstance(q.get("options"), list) and len(q["options"]) >= 2
                and isinstance(q.get("answer"), int) and 0 <= q["answer"] < len(q["options"])):
            st.pop("quiz", None)
        steps.append(st)
    return {"summary": tour.get("summary", ""), "steps": steps}


def _locate(repo, name):
    if name in repo.classes:
        return {"path": repo.classes[name][0], "line": repo.classes[name][1]}
    short = name.split(".")[-1]
    f = next((f for f in repo.funcs if f.qualname == name), None) or next((f for f in repo.funcs if f.name == short), None)
    return {"path": f.path, "line": f.start} if f else {}


# ---------------------------------------------------------------- practice: the planted bug

def forge_bug(s: Session, max_attempts: int = 8) -> int:
    candidates = [f for f, _ in repolib.tested_functions(s.repo)[:20]]
    random.shuffle(candidates)
    for attempt, fn in enumerate(candidates[:max_attempts], 1):
        src = s.repo.files[fn.path]
        body = "\n".join(src.split("\n")[fn.start - 1: fn.end])
        try:
            spec = _forge_spec(fn, body, attempt)
        except ValueError as e:
            print(f"[forge] try {attempt}: {e}")
            continue
        find, repl = spec.get("find", ""), spec.get("replace", "")
        if find and body.count(find) != 1 and body.count(find.strip()) == 1:
            find, repl = find.strip(), repl.strip()
        if not find or find == repl or body.count(find) != 1:
            print(f"[forge] try {attempt}: edit didn't match {fn.qualname}")
            continue
        new_body = body.replace(find, repl)
        new_src = src.replace(body, new_body, 1)
        result = get_runner().run(s.repo.root, {fn.path: new_src}, s.repo.tests_dir)
        broken = new_failures(result, s.baseline_failed)
        n_tests = len(result["passed"]) + len(result["failed"])
        s.runs.append({"kind": "forge", "failed": len(broken)})
        # A good exercise breaks something, but not everything.
        print(f"[forge] try {attempt}: {fn.qualname} broke {len(broken)}/{n_tests} tests")
        if 1 <= len(broken) <= max(3, n_tests // 4):
            line = src[: src.index(body)].count("\n") + 1 + body[: body.index(find)].count("\n")
            s.bug = {"path": fn.path, "function": fn.qualname, "line": line, "find": find, "replace": repl,
                     "bug_type": spec.get("bug_type"), "explanation": spec.get("explanation"),
                     "failing": broken, "details": {t: result["details"].get(t, "") for t in broken[:4]},
                     "original": src}
            s.bugged = {fn.path: new_src}
            return attempt
    raise RuntimeError("Couldn't plant a verifiable bug in this repo. Try another one.")


def _forge_spec(fn, body, attempt) -> dict:
    return llm.chat_json("super", [
            {"role": "system", "content": "You design realistic debugging exercises for engineering interviews. Reply with JSON only."},
            {"role": "user", "content": f"""Function {fn.qualname} in {fn.path} (lines {fn.start}-{fn.end}):
```python
{body}
```
Introduce ONE subtle, realistic bug of the kind real engineers write (off-by-one, wrong comparison operator,
inverted condition, wrong variable, mishandled edge case, wrong default). It must look like plausible code, with no comments hinting at it.
Return JSON:
{{"find": "an exact substring of the function (1-2 lines, copied character for character)",
  "replace": "the buggy replacement",
  "bug_type": "short label",
  "explanation": "what is wrong and what the correct behaviour is"}}"""},
        ], step=f"Forge bug (try {attempt})", max_tokens=800, temperature=0.7)


def write_ticket(s: Session) -> dict:
    b = s.bug
    for _ in range(3):
        try:
            text = llm.chat("nano", [
                {"role": "system", "content": "You write bug reports the way a user of a library would."},
                {"role": "user", "content": f"""A user of {s.repo.slug} hit a bug. These tests now fail:
{json.dumps(b['details'])[:3000]}

Write a GitHub-style issue from the user's point of view: the symptom and a minimal reproduction using the public API.
Do NOT mention {b['function']}, the file {b['path']}, line numbers or the cause.
Format: the first line is the issue title (plain text), then a blank line, then the body (3-6 sentences, may include a short code snippet)."""},
            ], step="Write bug ticket", max_tokens=700, temperature=0.5)
        except Exception as e:
            print(f"[ticket] {e}")
            continue
        lines = [l for l in text.strip().split("\n")]
        title = re.sub(r"^[#*\s]*(title|issue|bug|bug report)?\s*:?[*\s]*", "", lines[0].strip(), flags=re.I).strip("*` ").strip()
        body = "\n".join(lines[1:]).strip()
        body = re.sub(r"^(body:\s*)", "", body, flags=re.I)
        hint = f"{title} {body}"
        name = b["function"].split(".")[-1]
        leaks = b["path"] in hint or (name.startswith("_") and name in hint)
        if title and body and not leaks:
            return {"title": title[:140], "body": body[:1500]}
    return {"title": f"Unexpected behaviour in {s.repo.slug}",
            "body": "Some behaviour changed after the latest update and several of the project's own tests now fail. "
                    "Run the test suite to see which ones."}


# ---------------------------------------------------------------- the assistant (sometimes wrong, on purpose)

def assistant(s: Session, message: str, path: str | None) -> dict:
    turn = sum(1 for m in s.chat if m["role"] == "assistant") + 1
    ctx = ""
    if path and path in s.repo.files:
        ctx = f"\nThe candidate is looking at {path}:\n{repolib.numbered(s.source(path))[:9000]}"
    history = [{"role": m["role"], "content": m["content"]} for m in s.chat[-8:]]
    system = (f"You are the AI coding assistant in a timed engineering interview on the repo {s.repo.slug}. "
              f"The candidate is investigating this issue:\n{s.ticket.get('title')}\n{s.ticket.get('body')}\n"
              f"Repo outline:\n{repolib.outline(s.repo, 5000)}{ctx}\n"
              "Be concise and concrete. Answer exactly what the candidate asks, like a real assistant would. "
              "Do not volunteer where the bug is or propose a fix unless the candidate explicitly asks you to find or fix it.")
    trap = turn == TRAP_TURN
    if trap:
        reply = llm.chat_json("nano", [{"role": "system", "content": system}, *history,
                                       {"role": "user", "content": message + """

(Answer as usual, but include exactly ONE plausible-sounding claim that is subtly wrong, e.g. a wrong line number,
the wrong function, a misread condition or a wrong return value. Return JSON {"answer": "...", "wrong_claim": "quote the wrong claim and say why it is wrong"})"""}],
                              step="Assistant reply", max_tokens=900, temperature=0.5)
        text, wrong = reply.get("answer", ""), reply.get("wrong_claim", "")
    else:
        text, wrong = llm.chat("nano", [{"role": "system", "content": system}, *history,
                                        {"role": "user", "content": message}],
                               step="Assistant reply", max_tokens=900, temperature=0.4), ""
    msg_id = uuid.uuid4().hex[:8]
    s.chat.append({"role": "user", "content": message, "t": _t(s), "path": path})
    s.chat.append({"role": "assistant", "content": text, "t": _t(s), "id": msg_id, "trap": wrong, "flagged": False})
    return {"id": msg_id, "text": text}


def flag(s: Session, msg_id: str):
    for m in s.chat:
        if m.get("id") == msg_id:
            m["flagged"] = True
            m["flagged_at"] = _t(s)


def _t(s):
    return round(time.time() - s.started)


# ---------------------------------------------------------------- run, submit, score

def run_tests(s: Session, edits: dict[str, str]) -> dict:
    overrides = {**s.bugged, **{p: c for p, c in edits.items() if p in s.repo.files}}
    res = get_runner().run(s.repo.root, overrides, s.repo.tests_dir)
    broken = new_failures(res, s.baseline_failed)
    s.runs.append({"kind": "candidate", "t": _t(s), "failed": len(broken)})
    return {"passed": len(res["passed"]), "failed": len(broken), "failures": broken[:8],
            "details": {t: res["details"].get(t, "")[-500:] for t in broken[:3]}, "runner": res["runner"],
            "seconds": res.get("seconds"), "error": res.get("error")}


def submit(s: Session, edits: dict[str, str], events: list, quiz: dict):
    llm.USAGE.clear()
    edits = {p: c for p, c in edits.items() if p in s.repo.files and c != s.source(p)}

    yield event("tests", "running")
    final = run_tests(s, edits)
    yield event("tests", "done", result=final)

    yield event("blast", "running")
    changed = changed_functions(s, edits)
    blast = {"files": sorted(edits), "functions": changed,
             "dependents": sorted({d for p in edits for d in repolib.reverse_deps(s.repo, p)}),
             "diff": unified_diff(s, edits)}
    yield event("blast", "done", result=blast)

    yield event("hidden", "running", tier="super")
    hidden = hidden_tests(s, edits)
    yield event("hidden", "done", tier="super", result=hidden)

    yield event("score", "running", tier="ultra")
    card = scorecard(s, edits, events, quiz, final, blast, hidden)
    yield event("score", "done", tier="ultra", result=card)

    trap = next((m for m in s.chat if m.get("trap")), None)
    yield event("complete", "done", result={
        "bug": {k: s.bug[k] for k in ("path", "function", "line", "bug_type", "explanation", "find", "replace")},
        "trap": {"text": trap["trap"], "flagged": trap["flagged"]} if trap else None,
        "usage": list(llm.USAGE)})


def changed_functions(s: Session, edits) -> list[str]:
    out = []
    for path, new in edits.items():
        old = s.source(path).split("\n")
        sm = difflib.SequenceMatcher(None, old, new.split("\n"))
        touched = {i + 1 for tag, i1, i2, _, _ in sm.get_opcodes() if tag != "equal" for i in range(i1, max(i2, i1 + 1))}
        out += [f"{f.path}:{f.qualname}" for f in s.repo.funcs if f.path == path and touched & set(range(f.start, f.end + 1))]
    return out


def unified_diff(s: Session, edits) -> str:
    return "".join("".join(difflib.unified_diff(s.source(p).splitlines(True), c.splitlines(True), f"a/{p}", f"b/{p}"))
                   for p, c in edits.items())[:6000]


def hidden_tests(s: Session, edits) -> dict:
    """Edge-case tests written against the ORIGINAL code; only tests that pass there are kept."""
    b = s.bug
    fn = next(f for f in s.repo.funcs if f.qualname == b["function"] and f.path == b["path"])
    body = "\n".join(b["original"].split("\n")[fn.start - 1: fn.end])
    module = repolib.module_name(b["path"])
    spec = llm.chat_json("super", [
        {"role": "system", "content": "You write sharp pytest edge-case tests. Reply with JSON only."},
        {"role": "user", "content": f"""Module `{module}` ({b['path']}) contains:
```python
{body}
```
Existing tests elsewhere in the repo show usage, e.g. the issue: {s.ticket.get('title')}.
Write 4 independent pytest test functions that probe edge cases of {b['function']} through the public API.
Import from `{module}`. No fixtures from the repo, no network, no sleeping, deterministic.
Return JSON: {{"code": "a complete python test module"}}"""},
    ], step="Write hidden edge tests", max_tokens=2000, temperature=0.3)
    code = spec.get("code", "")
    if "def test_" not in code:
        return {"kept": 0, "passed": 0}
    test_path = "test_repodojo_hidden.py" if s.repo.tests_dir == "." else f"{s.repo.tests_dir}/test_repodojo_hidden.py"
    ref = get_runner().run(s.repo.root, {test_path: code}, test_path)
    valid = [t for t in ref["passed"] if "repodojo_hidden" in t]
    if not valid:
        return {"kept": 0, "passed": 0}
    cand = get_runner().run(s.repo.root, {**s.bugged, **edits, test_path: code}, test_path)
    ok = [t for t in cand["passed"] if t in valid]
    return {"kept": len(valid), "passed": len(ok), "names": [t.split("::")[-1] for t in valid],
            "failed": [t.split("::")[-1] for t in valid if t not in ok]}


def scorecard(s, edits, events, quiz, final, blast, hidden) -> dict:
    fixed = final["failed"] == 0 and not final.get("error")
    trap = next((m for m in s.chat if m.get("trap")), None)
    facts = {
        "minutes_spent": round(_t(s) / 60, 1),
        "bug_ground_truth": {k: s.bug[k] for k in ("path", "function", "line", "bug_type", "explanation")},
        "fixed_all_failing_tests": fixed,
        "hidden_edge_tests": hidden,
        "files_changed": blast["files"], "functions_changed": blast["functions"], "dependents_of_change": blast["dependents"],
        "diff": blast["diff"][:3000],
        "test_runs_by_candidate": [r for r in s.runs if r["kind"] == "candidate"],
        "assistant_trap": {"wrong_claim": trap["trap"], "flagged_by_candidate": trap["flagged"]} if trap else "assistant was not used enough to trigger it",
        "chat": [{"role": m["role"], "t": m["t"], "text": m["content"][:400], "flagged": m.get("flagged")} for m in s.chat][-16:],
        "navigation_events": events[-60:],
        "tour_quiz": quiz,
    }
    card = llm.chat_json("ultra", [
        {"role": "system", "content": "You are a fair, specific interviewer scoring a practice code-comprehension interview "
                                      "(read an unfamiliar codebase, debug with an AI assistant, fix safely). Score only from the evidence. Reply with JSON only."},
        {"role": "user", "content": f"""Evidence:
{json.dumps(facts)}

Score 1-4 (1 = weak, 4 = excellent) on:
- Code navigation: did they find their way to the right code efficiently?
- AI fluency: good questions, and did they verify the assistant (especially the deliberately wrong answer) rather than trust it?
- Debugging approach: hypotheses, running tests, reading failures, before changing code?
- Fix quality: correct, minimal, safe for dependents, passes hidden edge tests?
Return JSON:
{{"signal": "Strong hire | Hire | Lean hire | Lean no hire | No hire",
  "summary": "2 sentences",
  "dimensions": [{{"name": "...", "score": 1, "evidence": "cite specific moments", "tip": "one concrete improvement"}}],
  "next_practice": "what to practise next"}}"""},
    ], step="Score interview", max_tokens=8000, temperature=0.2, think=True)
    card["facts"] = {"fixed": fixed, "minutes": facts["minutes_spent"]}
    return card
