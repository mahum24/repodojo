# RepoDojo

**Read any codebase. Find the bug. Prove you didn't just trust the AI.**

Software interviews are changing. Google has confirmed it is piloting an AI-assisted **code-comprehension round**: candidates get an unfamiliar multi-file codebase and an AI assistant, then have to find bugs, implement and optimise. Interviewers score how they navigate code they didn't write and their **AI fluency**: whether they ask good questions and *verify* the assistant instead of trusting it. New hires face the same problem on day one, in a real codebase.

LeetCode doesn't train any of that. RepoDojo does, on any real GitHub repo. It combines three tools in one loop:

### 1. Learn: the repo becomes a course
RepoDojo downloads the repo and indexes it statically (Python `ast`: functions, classes, imports and the import graph). **Nemotron 3 Super** writes a 5-step guided tour in reading order: architecture, entry points, how a call flows through the code, then the tests. Each step has clickable symbols that jump to the exact line, a "look for" hint and a quick quiz. Every file and symbol the model mentions is checked against the index; anything that doesn't exist is dropped.

### 2. Practise: a real bug, proven by the tests
- **Nemotron 3 Super** plants one subtle, realistic bug (off-by-one, inverted condition, wrong comparison) in a function the test suite covers.
- The repo's own tests run against it. The bug is only accepted if it breaks at least one test but not most of them; otherwise RepoDojo tries again. Tests that already failed before the change are recorded and ignored.
- **Nemotron 3 Nano** turns the failing tests into a user-style GitHub issue that describes the symptom without revealing where the bug is.
- The candidate works in a three-panel interview (files, editor, AI assistant) with a timer and a "Run tests" button.
- The assistant (Nemotron 3 Nano) is **deliberately wrong once**, as real AI tools can be: its second answer contains one plausible but false claim. Candidates are told up front and can flag any answer. Catching the wrong answer is part of the score.

### 3. Ship: blast radius and hidden tests
On submit:
- **The full test suite** runs against the fix.
- **Blast radius:** code works out which functions changed and every file that imports them, directly or indirectly.
- **Hidden edge-case tests:** Nemotron 3 Super writes edge-case tests for the bugged function *against the original code*. Only tests that pass on the original are kept, and then they run against the candidate's fix.
- **Scorecard:** **Nemotron 3 Ultra** (reasoning on) scores four dimensions (code navigation, AI fluency, debugging approach and fix quality), citing specific moments from the session: files opened, questions asked, test runs, the flag, and the diff.

## NVIDIA Nemotron on Nebius Token Factory

| Step | Model | Reasoning |
|---|---|---|
| Repo tour | `nvidia/nemotron-3-super-120b-a12b` | off |
| Plant the bug | `nvidia/nemotron-3-super-120b-a12b` | off |
| Bug ticket | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | off |
| Interview assistant | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | off |
| Hidden edge tests | `nvidia/nemotron-3-super-120b-a12b` | off |
| Scorecard | `nvidia/Nemotron-3-Ultra-550b-a55b` | **on** |

The scorecard screen shows a routing table with the model, latency, tokens and reasoning tokens for every call.

## Tests run in isolation

Running a stranger's repo means running a stranger's code. RepoDojo's test runner sits behind one interface (`app/runner.py`, `make_runner()`) so the backend can be swapped:

- **Today:** a subprocess runner with a timeout, which reports per-test results via JUnit XML. The hosted demo only allows curated repos (`dbader/schedule`, `pytoolz/toolz`); set `ALLOW_ANY_REPO=1` to try others locally.
- **Next:** **Nebius Token Factory Sandboxes**, which give an isolated VM per run with branchable filesystem state. That's a natural fit: the baseline, the bugged version and each candidate fix can each be a branch of one prepared image. The runner already packages the repo plus overrides as a tarball for upload (`runner.tarball`). Sandboxes are in beta and need per-project activation, which is requested.

## Models propose, tests decide

| What a model proposes | What decides |
|---|---|
| Tour references | Must exist in the AST index |
| A bug | Must break 1 to N tests that passed before |
| Hidden tests | Must pass on the original code |
| A fix | Must pass the full suite and the hidden tests |
| The assistant's trap | Logged server-side, so the scorecard knows whether it was caught |

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # NEBIUS_API_KEY (and optionally NEBIUS_PROJECT_ID for Sandboxes)
uvicorn app.main:app --port 8000
```

## License

MIT
