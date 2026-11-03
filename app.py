"""Flask application: REST API + dashboard for the Autonomous Coding Agent."""
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

from werkzeug.exceptions import HTTPException
from flask import Flask, Response, jsonify, render_template, request, session

from agents import Orchestrator, TaskState, TaskStore
from config import Config
from services.code_executor import PythonExecutor
from services.gemini_service import GeminiService
from services.git_service import commit_approved

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def ok(data=None, status=200):
    return jsonify({"ok": True, "data": data}), status


def fail(code: str, message: str, status: int):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


def create_app(config=Config, llm=None, executor=None) -> Flask:
    app = Flask(__name__)
    app.config.update(SECRET_KEY=config.SECRET_KEY, MAX_CONTENT_LENGTH=256 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    llm = llm or GeminiService()
    executor = executor or PythonExecutor()
    orchestrator = Orchestrator(llm, executor, config)
    store = TaskStore()
    pool = ThreadPoolExecutor(max_workers=config.MAX_CONCURRENT_TASKS)
    app.extensions["aca"] = {"store": store, "orchestrator": orchestrator, "llm": llm}

    # ---- helpers -----------------------------------------------------
    def sid() -> str:
        if "sid" not in session:
            session["sid"] = uuid.uuid4().hex
        return session["sid"]

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def find(task_id):
        return store.get(task_id, sid())

    def validate_code(code):
        if not isinstance(code, str) or not code.strip():
            return "code must be a non-empty string"
        if len(code) > config.MAX_CODE_CHARS:
            return f"code exceeds {config.MAX_CODE_CHARS} characters"
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
        return resp

    # ---- pages -------------------------------------------------------
    @app.get("/")
    def index():
        sid()
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return ok({"gemini_configured": llm.configured, "model": getattr(llm, "model", "unknown"),
                   "languages": config.SUPPORTED_LANGUAGES,
                   "default_max_iterations": config.DEFAULT_MAX_ITERATIONS,
                   "max_iterations_cap": config.MAX_ITERATIONS_CAP,
                   "execution_timeout": config.EXECUTION_TIMEOUT})

    # ---- agent API ---------------------------------------------------
    @app.post("/api/agent/run")
    def run_agent():
        data = body()
        task = data.get("task")
        language = str(data.get("language", "python")).lower()
        if not isinstance(task, str) or len(task.strip()) < 10:
            return fail("invalid_task", "Describe the coding task in at least 10 characters.", 400)
        if len(task) > config.MAX_TASK_CHARS:
            return fail("invalid_task", f"Task is too long (max {config.MAX_TASK_CHARS} characters).", 400)
        if language not in config.SUPPORTED_LANGUAGES:
            return fail("unsupported_language", f"Language '{language}' is not supported yet.", 400)
        try:
            max_iter = int(data.get("max_iterations", config.DEFAULT_MAX_ITERATIONS))
        except (TypeError, ValueError):
            return fail("invalid_iterations", "max_iterations must be an integer.", 400)
        if not 1 <= max_iter <= config.MAX_ITERATIONS_CAP:
            return fail("invalid_iterations",
                        f"max_iterations must be between 1 and {config.MAX_ITERATIONS_CAP}.", 400)
        if not llm.configured:
            return fail("missing_api_key", "GEMINI_API_KEY is not configured. Add it to your .env "
                        "file (see .env.example) and restart the server.", 503)
        if any(not t.finished for t in store.for_session(sid())):
            return fail("task_in_progress", "A task is already running. Wait for it or reset it.", 429)
        state = TaskState(task.strip(), language, max_iter, sid())
        store.add(state)
        pool.submit(orchestrator.run, state)
        return ok({"task_id": state.id, "status": state.status}, 202)

    @app.post("/api/agent/execute")
    def execute():
        data = body()
        err = validate_code(data.get("code"))
        if err:
            return fail("invalid_code", err, 400)
        task_id = data.get("task_id")
        if task_id:
            state = find(task_id)
            if not state:
                return fail("not_found", "Task not found.", 404)
            if not state.finished:
                return fail("task_running", "Wait for the agent to finish before running edited code.", 409)
            return ok(orchestrator.execute_code(state, data["code"]))
        result = executor.run_code(data["code"], config.EXECUTION_TIMEOUT)
        return ok({"execution": result.to_dict(), "test_report": None, "passed": result.ok})

    @app.post("/api/agent/reset")
    def reset():
        task_id = body().get("task_id")
        targets = [find(task_id)] if task_id else store.for_session(sid())
        removed = 0
        for state in targets:
            if state:
                state.cancel_event.set()
                store.delete(state.id)
                removed += 1
        return ok({"reset": removed})

    @app.get("/api/agent/status/<task_id>")
    def status(task_id):
        state = find(task_id)
        return ok(state.to_dict()) if state else fail("not_found", "Task not found.", 404)

    @app.get("/api/agent/history/<task_id>")
    def history(task_id):
        state = find(task_id)
        if not state:
            return fail("not_found", "Task not found.", 404)
        return ok({"task_id": state.id, "task": state.task, "status": state.status,
                   "iterations": state.to_dict(include_history=True)["history"]})

    @app.get("/api/agent/tasks")
    def tasks():
        return ok([t.summary() for t in store.for_session(sid())])

    @app.get("/api/agent/download/<task_id>")
    def download(task_id):
        state = find(task_id)
        if not state:
            return fail("not_found", "Task not found.", 404)
        which = request.args.get("file", "solution")
        if which not in ("solution", "tests"):
            return fail("invalid_file", "file must be 'solution' or 'tests'.", 400)
        content = state.code if which == "solution" else state.tests
        if not content:
            return fail("no_content", "Nothing to download yet.", 404)
        slug = re.sub(r"[^a-z0-9]+", "_", state.title().lower()).strip("_")[:40] or "solution"
        filename = f"{slug}.py" if which == "solution" else f"test_{slug}.py"
        return Response(content, mimetype="text/x-python",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/agent/approve")
    def approve():
        data = body()
        state = find(data.get("task_id"))
        if not state:
            return fail("not_found", "Task not found.", 404)
        if not state.finished:
            return fail("task_running", "The agent is still running.", 409)
        if "code" in data:
            err = validate_code(data["code"])
            if err:
                return fail("invalid_code", err, 400)
            state.code = data["code"]
        folder = orchestrator._persist(state)
        state.approved = True
        state.log("User", "Approved", "Code approved by the user. Nothing is deployed or pushed automatically.")
        if data.get("git_commit") is True:
            state.commit_info = commit_approved(folder, f"Approved: {state.title()}")
        return ok({"approved": True, "saved_to": f"generated_code/{state.id}/",
                   "commit_info": state.commit_info})

    # ---- error handlers ----------------------------------------------
    @app.errorhandler(404)
    def _404(_):
        return fail("not_found", "Resource not found.", 404)

    @app.errorhandler(405)
    def _405(_):
        return fail("method_not_allowed", "Method not allowed.", 405)

    @app.errorhandler(413)
    def _413(_):
        return fail("payload_too_large", "Request body is too large.", 413)

    @app.errorhandler(Exception)
    def _500(exc):
        if isinstance(exc, HTTPException):
            return fail(exc.name.lower().replace(" ", "_"), exc.description, exc.code or 500)
        logging.getLogger(__name__).exception("Unhandled error")
        return fail("internal_error", "Internal server error.", 500)

    return app


if __name__ == "__main__":
    application = create_app()
    if not Config.GEMINI_API_KEY:
        print("WARNING: GEMINI_API_KEY is not set. Copy .env.example to .env and add your key.")
    application.run(host=Config.HOST, port=Config.PORT, debug=Config.DEBUG, threaded=True)
