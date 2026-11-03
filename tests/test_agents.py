"""Offline test-suite: a scripted FakeLLM stands in for Gemini so everything else is real
(parser, subprocess executor, unittest harness, orchestrator loop, Flask API)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from agents.orchestrator import Orchestrator  # noqa: E402
from agents.state import TaskState  # noqa: E402
from config import Config  # noqa: E402
from services.code_executor import PythonExecutor, scan_policy  # noqa: E402
from services.gemini_service import GeminiError, GeminiService  # noqa: E402
from services.test_runner import TestRunner  # noqa: E402
from utils.code_parser import ParseError, extract_code, extract_json, extract_tag  # noqa: E402

SPEC = ('{"title":"Add numbers","summary":"add two ints","functional_requirements":["add a and b"],'
        '"constraints":[],"edge_cases":["negatives"],'
        '"public_api":[{"name":"add","signature":"add(a, b)","description":"sum"}],'
        '"demo_behavior":"prints 5"}')
PLAN = '{"steps":[{"step":1,"title":"Implement","details":"return a+b"}]}'
BUGGY = "def add(a, b):\n    return a - b\n\nif __name__ == '__main__':\n    print(add(2, 3))\n"
FIXED = "def add(a, b):\n    return a + b\n\nif __name__ == '__main__':\n    print(add(2, 3))\n"
TESTS = ("import unittest\nfrom solution import add\n\nclass T(unittest.TestCase):\n"
         "    def test_pos(self): self.assertEqual(add(2, 3), 5)\n"
         "    def test_neg(self): self.assertEqual(add(-1, -1), -2)\n")


def wrap(tag, code):
    return f"<{tag}>\n```python\n{code}```\n</{tag}>"


class FakeLLM:
    configured = True
    model = "fake"

    def __init__(self, fix=True):
        self.fix, self.calls = fix, []

    def generate(self, prompt, system=None, json_mode=False, temperature=0.2, purpose=""):
        self.calls.append(purpose)
        if purpose == "planner.analyze":
            return SPEC
        if purpose == "planner.plan":
            return PLAN
        if purpose == "coder.generate":
            return wrap("solution", BUGGY)
        if purpose == "tester.generate":
            return wrap("tests", TESTS)
        if purpose == "debugger.fix":
            return "<analysis>add subtracts instead of adding</analysis>" + wrap("solution", FIXED if self.fix else BUGGY)
        raise AssertionError(purpose)


class ParserTests(unittest.TestCase):
    def test_extract_code_variants(self):
        self.assertEqual(extract_code("<solution>\n```python\nx = 1\n```\n</solution>"), "x = 1\n")
        self.assertEqual(extract_code("blah\n```python\ny = 2\n```"), "y = 2\n")
        with self.assertRaises(ParseError):
            extract_code("no code here")
        with self.assertRaises(ParseError):
            extract_code("<solution>unterminated")

    def test_extract_json(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Here: {"a": 2} done'), {"a": 2})
        with self.assertRaises(ParseError):
            extract_json("[1,2]")

    def test_extract_tag(self):
        self.assertEqual(extract_tag("<a>x</a>", "a"), "x")
        self.assertIsNone(extract_tag("<a>x", "a"))


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.ex = PythonExecutor(timeout=3, max_output_bytes=2000)

    def test_success_and_stderr(self):
        r = self.ex.run_code("print('hello')")
        self.assertTrue(r.ok)
        self.assertIn("hello", r.stdout)
        r = self.ex.run_code("raise ValueError('boom')")
        self.assertFalse(r.ok)
        self.assertIn("ValueError: boom", r.stderr)

    def test_timeout(self):
        start = time.time()
        r = self.ex.run_code("while True: pass")
        self.assertTrue(r.timed_out)
        self.assertLess(time.time() - start, 10)

    def test_output_cap(self):
        r = self.ex.run_code("print('x' * 100000)")
        self.assertTrue(r.truncated)
        self.assertLess(len(r.stdout), 2200)

    def test_env_is_scrubbed(self):
        os.environ["ACA_TEST_SECRET"] = "s3cret"
        try:
            r = self.ex.run_code("import os\nprint(os.environ.get('ACA_TEST_SECRET'))")
        finally:
            del os.environ["ACA_TEST_SECRET"]
        self.assertIn("None", r.stdout)
        self.assertNotIn("s3cret", r.stdout + r.stderr)

    def test_policy_blocks(self):
        for bad in ("open('.env').read()", "import subprocess", "import socket", "print('GEMINI_API_KEY')"):
            r = self.ex.run_code(bad)
            self.assertTrue(r.blocked_reason, bad)
            self.assertFalse(r.ok)
        self.assertIsNone(scan_policy({"a.py": "import os\nprint(os.environ)"}))

    def test_paths_hidden_and_temp_cleaned(self):
        r = self.ex.run_code("import os\nprint(os.getcwd())\nraise SystemExit(2)")
        self.assertIn("<sandbox>", r.stdout)
        self.assertEqual(r.exit_code, 2)
        self.assertEqual([p for p in os.listdir(tempfile.gettempdir()) if p.startswith("aca_")], [])

    def test_test_runner_counts(self):
        rep = TestRunner(self.ex, 5).run(BUGGY, TESTS)
        self.assertEqual((rep.total, rep.passed_count, rep.failed), (2, 0, 2))
        self.assertFalse(rep.passed)
        rep = TestRunner(self.ex, 5).run(FIXED, TESTS)
        self.assertTrue(rep.passed)
        rep = TestRunner(self.ex, 5).run(FIXED, "import unittest\nfrom solution import nope\n")
        self.assertFalse(rep.passed)  # import error surfaces as an errored test


class GeminiTests(unittest.TestCase):
    def test_missing_key_message(self):
        svc = GeminiService(api_key="")
        self.assertFalse(svc.configured)
        with self.assertRaises(GeminiError) as ctx:
            svc.generate("hi")
        self.assertIn("GEMINI_API_KEY", str(ctx.exception))
        self.assertEqual(ctx.exception.status, 503)


class OrchestratorTests(unittest.TestCase):
    def make(self, fix=True, max_iter=3):
        cfg = type("C", (Config,), {"GENERATED_DIR": Path(tempfile.mkdtemp())})
        orch = Orchestrator(FakeLLM(fix), PythonExecutor(timeout=5), cfg)
        state = TaskState("write an add function please", "python", max_iter, "s1")
        orch.run(state)
        return orch, state

    def test_self_correction(self):
        orch, st = self.make(fix=True)
        self.assertEqual(st.status, "passed")
        self.assertEqual(st.debug_attempts, 1)
        self.assertEqual(len(st.history), 2)
        self.assertEqual(st.history[1]["analysis"], "add subtracts instead of adding")
        self.assertEqual(st.stages["review"]["status"], "completed")
        self.assertIn("return a + b", st.code)
        self.assertTrue((Path(orch.config.GENERATED_DIR) / st.id / "solution.py").exists())

    def test_iteration_limit(self):
        _, st = self.make(fix=False, max_iter=2)
        self.assertEqual(st.status, "failed")
        self.assertEqual(st.debug_attempts, 2)
        self.assertEqual(len(st.history), 3)
        self.assertEqual(st.stages["debugging"]["status"], "failed")

    def test_manual_execute(self):
        orch, st = self.make(fix=False, max_iter=1)
        res = orch.execute_code(st, FIXED)
        self.assertTrue(res["passed"])
        self.assertEqual(st.status, "passed")

    def test_llm_failure_marks_error(self):
        class Boom(FakeLLM):
            def generate(self, *a, **k):
                raise GeminiError("rate limited", 429)
        orch = Orchestrator(Boom(), PythonExecutor(timeout=5),
                            type("C", (Config,), {"GENERATED_DIR": Path(tempfile.mkdtemp())}))
        st = TaskState("write an add function please", "python", 3, "s")
        orch.run(st)
        self.assertEqual(st.status, "error")
        self.assertEqual(st.stages["analysis"]["status"], "failed")


class ApiTests(unittest.TestCase):
    def setUp(self):
        cfg = type("C", (Config,), {"GENERATED_DIR": Path(tempfile.mkdtemp())})
        self.app = create_app(cfg, llm=FakeLLM(True), executor=PythonExecutor(timeout=5))
        self.c = self.app.test_client()

    def wait(self, tid):
        for _ in range(100):
            d = self.c.get(f"/api/agent/status/{tid}").get_json()["data"]
            if d["status"] in ("passed", "failed", "error", "cancelled"):
                return d
            time.sleep(0.2)
        self.fail("timeout")

    def test_pages_and_health(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        h = self.c.get("/api/health").get_json()["data"]
        self.assertTrue(h["gemini_configured"])
        self.assertEqual(self.c.get("/api/nope").status_code, 404)

    def test_validation(self):
        r = self.c.post("/api/agent/run", json={"task": "short"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/agent/run", json={"task": "a valid long task", "language": "cobol"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/agent/run", json={"task": "a valid long task", "max_iterations": 99})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post("/api/agent/run", data="notjson").status_code, 400)
        self.assertEqual(self.c.post("/api/agent/execute", json={"code": ""}).status_code, 400)

    def test_missing_key(self):
        app = create_app(llm=GeminiService(api_key=""), executor=PythonExecutor(timeout=5))
        r = app.test_client().post("/api/agent/run", json={"task": "write something useful"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()["error"]["code"], "missing_api_key")

    def test_full_flow(self):
        r = self.c.post("/api/agent/run", json={"task": "write an add function please"})
        self.assertEqual(r.status_code, 202)
        tid = r.get_json()["data"]["task_id"]
        d = self.wait(tid)
        self.assertEqual(d["status"], "passed")
        self.assertEqual(d["test_report"]["passed_count"], 2)
        self.assertEqual([s["status"] for s in d["stages"]],
                         ["completed"] * 3 + ["completed", "completed", "completed", "completed"])
        hist = self.c.get(f"/api/agent/history/{tid}").get_json()["data"]
        self.assertEqual(len(hist["iterations"]), 2)
        dl = self.c.get(f"/api/agent/download/{tid}")
        self.assertIn("attachment", dl.headers["Content-Disposition"])
        self.assertIn("a + b", dl.get_data(as_text=True))
        self.assertEqual(self.c.get(f"/api/agent/download/{tid}?file=x").status_code, 400)
        ex = self.c.post("/api/agent/execute", json={"task_id": tid, "code": BUGGY}).get_json()["data"]
        self.assertFalse(ex["passed"])
        ap = self.c.post("/api/agent/approve", json={"task_id": tid, "code": FIXED}).get_json()["data"]
        self.assertTrue(ap["approved"])
        self.assertNotIn(str(Path(tempfile.gettempdir())), str(ap))
        self.assertEqual(len(self.c.get("/api/agent/tasks").get_json()["data"]), 1)
        self.assertEqual(self.c.post("/api/agent/reset", json={"task_id": tid}).get_json()["data"]["reset"], 1)
        self.assertEqual(self.c.get(f"/api/agent/status/{tid}").status_code, 404)

    def test_sessions_are_isolated(self):
        tid = self.c.post("/api/agent/run", json={"task": "write an add function please"}).get_json()["data"]["task_id"]
        self.wait(tid)
        other = self.app.test_client()
        self.assertEqual(other.get(f"/api/agent/status/{tid}").status_code, 404)

    def test_standalone_execute(self):
        r = self.c.post("/api/agent/execute", json={"code": "print(1+1)"}).get_json()["data"]
        self.assertTrue(r["passed"])
        self.assertIn("2", r["execution"]["stdout"])


if __name__ == "__main__":
    unittest.main()
