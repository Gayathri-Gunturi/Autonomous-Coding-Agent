"""Orchestrator Agent: drives the workflow, tracks state and enforces the iteration limit."""
import logging
from pathlib import Path

from agents.coder_agent import CoderAgent
from agents.debugger_agent import DebuggerAgent
from agents.planner_agent import PlannerAgent
from agents.state import TaskState
from agents.tester_agent import TesterAgent
from config import Config
from services.code_executor import BaseExecutor
from services.gemini_service import GeminiError
from services.test_runner import TestRunner
from utils.code_parser import ParseError

log = logging.getLogger(__name__)


class Cancelled(Exception):
    pass


class Orchestrator:
    name = "Orchestrator"

    def __init__(self, llm, executor: BaseExecutor, config=Config):
        self.config = config
        self.executor = executor
        self.planner = PlannerAgent(llm)
        self.coder = CoderAgent(llm)
        self.tester = TesterAgent(llm, TestRunner(executor, config.EXECUTION_TIMEOUT))
        self.debugger = DebuggerAgent(llm)

    # ------------------------------------------------------------------
    def run(self, state: TaskState) -> None:
        """Entry point executed in a worker thread. Never raises."""
        try:
            self._run(state)
        except Cancelled:
            state.status = "cancelled"
            state.log(self.name, "Cancelled", "Task cancelled by user.", "warn")
        except GeminiError as exc:
            self._fail(state, str(exc))
        except ParseError as exc:
            self._fail(state, f"The model returned an unexpected format: {exc}. Please try again.")
        except Exception:  # noqa: BLE001
            log.exception("Unexpected orchestrator failure")
            self._fail(state, "Unexpected internal error while running the agent. See server logs.")
        finally:
            try:
                self._persist(state)
            except Exception:  # noqa: BLE001
                log.exception("Could not persist generated files")

    def _fail(self, state: TaskState, message: str):
        with state.lock():
            for stage in state.stages.values():
                if stage["status"] == "running":
                    stage["status"] = "failed"
            state.status = "error"
            state.error = message
        state.log(self.name, "Failed", message, "error")

    def _check(self, state: TaskState):
        if state.cancel_event.is_set():
            raise Cancelled()

    # ------------------------------------------------------------------
    def _run(self, state: TaskState) -> None:
        state.status = "running"
        state.log(self.name, "Started", f"Task accepted (max {state.max_iterations} debugging iterations).")

        # 1. Requirement analysis
        self._check(state)
        state.set_stage("analysis", "running")
        state.log(self.planner.name, "Analyzing requirements", "Converting the request into a specification.")
        state.spec = self.planner.analyze(state.task, state.language)
        state.set_stage("analysis", "completed", state.spec["title"])
        state.log(self.planner.name, "Specification ready",
                  f"{len(state.spec['functional_requirements'])} requirements, "
                  f"{len(state.spec['edge_cases'])} edge cases, {len(state.spec['public_api'])} API items.")

        # 2. Planning
        self._check(state)
        state.set_stage("planning", "running")
        state.log(self.planner.name, "Planning", "Creating step-by-step implementation plan.")
        state.plan = self.planner.plan(state.spec)
        state.set_stage("planning", "completed", f"{len(state.plan)} steps")

        # 3. Code generation
        self._check(state)
        state.set_stage("codegen", "running")
        state.log(self.coder.name, "Generating code", "Writing solution.py from the plan.")
        state.code = self.coder.generate(state.task, state.spec, state.plan)
        state.set_stage("codegen", "completed", f"{len(state.code.splitlines())} lines")

        # 4-6. Execute -> test -> (debug -> re-execute ...) loop
        attempt = 0
        pending_analysis = None
        while True:
            self._check(state)
            label = "initial run" if attempt == 0 else f"re-run after fix {attempt}"
            state.set_stage("execution", "running", label)
            state.log(self.executor.__class__.__name__, "Executing code", f"Running solution.py ({label}).")
            execution = self.executor.run_code(state.code, self.config.EXECUTION_TIMEOUT)
            state.execution = execution.to_dict()
            state.set_stage("execution", "completed" if execution.ok else "failed",
                            "exit code 0" if execution.ok else self._exec_detail(execution))
            state.log("Executor", "Execution finished", self._exec_detail(execution) if not execution.ok
                      else f"Exited cleanly in {execution.duration:.2f}s.", "info" if execution.ok else "warn")

            self._check(state)
            state.set_stage("testing", "running", label)
            if not state.tests:
                state.log(self.tester.name, "Generating tests", "Writing unit tests from the specification.")
                state.tests = self.tester.generate_tests(state.spec)
            state.log(self.tester.name, "Running tests", "Executing test suite against solution.py.")
            report = self.tester.run_tests(state.code, state.tests)
            state.test_report = report.to_dict()
            passed = execution.ok and report.passed
            detail = (f"{report.passed_count}/{report.total} passed" if report.total
                      else (report.harness_error or "no tests ran"))
            state.set_stage("testing", "completed" if report.passed else "failed", detail)
            state.log(self.tester.name, "Test results", detail, "info" if report.passed else "warn")

            state.add_history({
                "iteration": attempt, "kind": "initial" if attempt == 0 else "debug",
                "analysis": pending_analysis, "code": state.code, "tests": state.tests,
                "execution": state.execution, "test_report": state.test_report, "passed": passed,
            })
            if passed or attempt >= state.max_iterations:
                break

            # Debugging
            attempt += 1
            state.debug_attempts = attempt
            self._check(state)
            state.set_stage("debugging", "running", f"attempt {attempt}/{state.max_iterations}")
            state.log(self.debugger.name, "Analyzing failure",
                      f"Debugging attempt {attempt}/{state.max_iterations}.", "warn")
            fix = self.debugger.fix(state.spec, state.code, state.tests, state.execution,
                                    state.test_report, attempt, state.max_iterations)
            pending_analysis = fix.analysis
            state.code = fix.code
            if fix.tests:
                state.tests = fix.tests
                state.log(self.debugger.name, "Tests corrected",
                          "The debugger judged the tests inconsistent with the specification and revised them.", "warn")
            state.set_stage("debugging", "completed", f"fix {attempt} applied")
            state.log(self.debugger.name, "Root cause", fix.analysis)

        # 7. Final review
        with state.lock():
            if attempt == 0 and passed:
                state.stages["debugging"].update(status="skipped", detail="not needed")
            elif not passed:
                state.stages["debugging"].update(status="failed", detail="iteration limit reached")
        if passed:
            state.set_stage("review", "completed", "awaiting your approval")
            state.status = "passed"
            state.log(self.name, "Finished", "All tests passed. Review the code and approve it.")
        else:
            state.set_stage("review", "failed", "limit reached; review latest code and errors")
            state.status = "failed"
            state.log(self.name, "Finished",
                      f"Stopped after {attempt} debugging iteration(s) without passing all tests. "
                      "Showing latest code, errors and debugging report.", "error")

    @staticmethod
    def _exec_detail(execution) -> str:
        if execution.blocked_reason:
            return f"blocked: {execution.blocked_reason}"
        if execution.timed_out:
            return "timed out"
        last = [l for l in execution.stderr.strip().splitlines() if l.strip()]
        return (last[-1] if last else f"exit code {execution.exit_code}")[:160]

    # ------------------------------------------------------------------
    def execute_code(self, state: TaskState, code: str) -> dict:
        """Run user-edited code (and the task's tests, if any) and record it in history."""
        execution = self.executor.run_code(code, self.config.EXECUTION_TIMEOUT)
        report = self.tester.run_tests(code, state.tests) if state.tests else None
        passed = execution.ok and bool(report and report.passed)
        with state.lock():
            if code != state.code:
                state.approved = False
            state.code = code
            state.execution = execution.to_dict()
            if report:
                state.test_report = report.to_dict()
            if state.finished:
                state.status = "passed" if passed else "failed"
            state.add_history({
                "iteration": len(state.history), "kind": "manual", "analysis": None, "code": code,
                "tests": state.tests, "execution": state.execution,
                "test_report": state.test_report, "passed": passed})
        state.log("User", "Manual run", "Edited code executed" + (" — all tests passed." if passed else "."),
                  "info" if passed else "warn")
        self._persist(state)
        return {"execution": state.execution, "test_report": state.test_report, "passed": passed}

    def _persist(self, state: TaskState) -> Path:
        """Save the latest code under generated_code/<task_id>/ (never auto-deployed)."""
        folder = Path(self.config.GENERATED_DIR) / state.id
        folder.mkdir(parents=True, exist_ok=True)
        if state.code:
            (folder / "solution.py").write_text(state.code, encoding="utf-8")
        if state.tests:
            (folder / "test_solution.py").write_text(state.tests, encoding="utf-8")
        return folder
