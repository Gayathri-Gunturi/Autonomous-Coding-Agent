# Autonomous Coding Agent

An AI-powered coding system that takes a plain-English task, then **plans, writes, executes, tests, debugs and
re-tests** Python code using a multi-agent workflow powered by Google Gemini. A human reviews, edits and approves
the final result. Built with Flask and a dependency-free HTML/CSS/JS dashboard.

## Features

- **Planner Agent** – converts the request into a structured spec (requirements, constraints, edge cases, public API) and an implementation plan
- **Coder Agent** – generates a complete `solution.py` that follows the spec and plan
- **Test Agent** – writes `unittest` tests *from the spec (not from the code)* and runs them through a JSON-reporting harness
- **Debugger Agent** – reads stack traces and failing tests, explains the root cause and returns fixed code (3 iterations by default, configurable 1-5)
- **Orchestrator** – stateful workflow, iteration limit, cancellation, per-stage status, structured logs
- **Human-in-the-loop** – edit the code in the browser, re-run it against the tests, download it and explicitly approve it. Nothing is deployed or pushed automatically; an optional *local* `git commit` happens only on approval
- **Session history** – every attempt (code, tests, errors, debugger analysis) can be inspected
- **Dashboard** – dark theme, stage tracker, CodeMirror editor, test panel, live agent logs, responsive layout

## Architecture and workflow

```
User task -> Requirement analysis -> Plan -> Code generation -> Code execution -> Test generation -> Test execution
                                                                                                         |
     all tests pass -> Final review (human approves) <-------------------------------------------------+
     tests fail     -> Debugger (root cause + fix) -> re-execute -> re-test -> (repeat up to N times) --+
```

| Layer | Modules |
|---|---|
| Agents | `agents/planner_agent.py`, `coder_agent.py`, `tester_agent.py`, `debugger_agent.py`, `orchestrator.py` (+ `state.py`, `base.py`) |
| LLM | `services/gemini_service.py` (retries, error translation, missing-key message) |
| Execution | `services/code_executor.py` (`BaseExecutor` interface, `PythonExecutor`), `services/test_runner.py` |
| Optional | `services/git_service.py` (local commit only, never pushes) |
| Parsing | `utils/code_parser.py` (tag / fence / JSON extraction) |
| Web | `app.py` (Flask app factory and REST API), `templates/`, `static/` |

The agent loop runs in a background thread; the browser polls `GET /api/agent/status/<id>` once per second.

## Technology stack

Python 3.10+, Flask, `google-genai` (official Gemini SDK), `python-dotenv`, `unittest` / `pytest`, vanilla JS and CodeMirror 5 (loaded from cdnjs; the editor falls back to a plain textarea if the CDN is unreachable).

## Directory structure

```
autonomous-coding-agent/
├── app.py  config.py  requirements.txt  .env.example  README.md
├── agents/     planner_agent.py coder_agent.py tester_agent.py debugger_agent.py orchestrator.py state.py base.py
├── services/   gemini_service.py code_executor.py test_runner.py git_service.py
├── utils/      code_parser.py
├── templates/  index.html
├── static/     css/style.css  js/script.js
├── generated_code/   (per-task output, git-ignored)
├── docs/screenshots/
└── tests/      test_agents.py
```

## Installation

```bash
cd autonomous-coding-agent

# virtual environment
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

## API key configuration

1. Create a key at <https://aistudio.google.com/apikey>.
2. Copy `.env.example` to `.env` (Windows: `copy .env.example .env`) and set `GEMINI_API_KEY=...`.
3. Optionally set `GEMINI_MODEL` (default `gemini-2.5-flash`), `MAX_ITERATIONS`, `EXECUTION_TIMEOUT`, etc.

The key is read server-side only. It is never sent to the browser or passed to generated code. If it is missing, the UI shows a banner and `POST /api/agent/run` returns a clear `503 missing_api_key` error.

## Running

```bash
python app.py                  # open http://127.0.0.1:5000
python -m pytest tests -q      # offline tests, no API key needed
```

## Example tasks

- Write `is_palindrome(text)` that ignores case, spaces and punctuation, and `longest_palindromic_substring(text)`.
- Implement an `LRUCache` class with O(1) `get` / `put`, returning -1 for missing keys.
- Write `parse_csv_line(line)` supporting quoted fields, embedded commas and escaped quotes.
- Write `evaluate(expression)` for arithmetic with parentheses and unary minus, without `eval`.

The same four are available as one-click chips in the UI.

## REST API

Responses are `{"ok": true, "data": ...}` or `{"ok": false, "error": {"code", "message"}}`. Tasks are scoped to the browser session cookie.

| Endpoint | Purpose |
|---|---|
| `GET /` | Dashboard |
| `POST /api/agent/run` | Body `{task, language, max_iterations?}`; starts a task, returns `202 {task_id}` |
| `GET /api/agent/status/<id>` | Stages, logs, spec, plan, code, tests, results |
| `GET /api/agent/history/<id>` | Every iteration (code, analysis, results) |
| `POST /api/agent/execute` | Body `{code, task_id?}`; runs edited code (and the task's tests when `task_id` is given) |
| `POST /api/agent/approve` | Body `{task_id, code?, git_commit?}`; human approval, saves to `generated_code/<id>/` |
| `POST /api/agent/reset` | Body `{task_id?}`; cancels and removes a task (or all of the session's tasks) |
| `GET /api/agent/download/<id>` | Download source; `?file=tests` for the test file |
| `GET /api/agent/tasks`, `GET /api/health` | Session task list; configuration and health |

## Screenshots

Capture the dashboard after running the app (idle view, a passing run, a self-corrected run), save the images in `docs/screenshots/` and link them here:

```
![Dashboard](docs/screenshots/dashboard.png)
```

## Limitations and security considerations

**Subprocess execution is not a fully secure sandbox.** What it does:

- runs generated code in a separate process (never inside Flask), in a throw-away temp directory, with stdin closed
- scrubs the environment, so `GEMINI_API_KEY` and other secrets are not inherited
- enforces a wall-clock timeout (the process tree is killed) and caps captured output
- on Linux/macOS also applies CPU-time, memory and file-size `rlimit`s (not available on Windows)
- applies a *heuristic* static scan that rejects obvious patterns (`.env`, `subprocess`, `socket`, `ctypes`, `os.system`, ...). A determined attacker can bypass it
- hides sandbox and interpreter paths in anything returned to the UI

What it does **not** do: block network access, stop reads of any file the server's OS user can read, or contain a hostile program. Run the app only on a machine and account you are comfortable with, keep it bound to `127.0.0.1`, and do not expose it publicly. For real isolation, implement `BaseExecutor` with Docker (`--network none`, read-only filesystem, CPU/memory limits, non-root user) and pass it to `create_app(executor=...)`.

Other limitations: Python only (the language selector and executor interface are structured for more); task state is kept in memory and is lost on restart; LLM-written tests can themselves be wrong (the debugger may revise them and says so in the logs, so always review); output quality depends on the model.

## Future enhancements

Docker executor, additional languages (JavaScript, Java), streaming logs over SSE, persistent history (SQLite), diff view between iterations, user-supplied tests, authentication and per-user quotas.
