"""Runs generated unittest suites inside the executor and returns structured results."""
import json
from dataclasses import dataclass, field
from typing import List, Optional

from config import Config
from services.code_executor import BaseExecutor

HARNESS = '''
import json, time, unittest

class Collector(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.records = []
    def _add(self, test, status, message=""):
        self.records.append({"name": test.id(), "status": status, "message": message})
    def addSuccess(self, test):
        super().addSuccess(test); self._add(test, "passed")
    def addFailure(self, test, err):
        super().addFailure(test, err); self._add(test, "failed", self._exc_info_to_string(err, test))
    def addError(self, test, err):
        super().addError(test, err); self._add(test, "error", self._exc_info_to_string(err, test))
    def addSkip(self, test, reason):
        super().addSkip(test, reason); self._add(test, "skipped", str(reason))
    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test); self._add(test, "failed", "Unexpected success")

start = time.time()
result = Collector()
try:
    suite = unittest.defaultTestLoader.loadTestsFromName("test_solution")
    suite.run(result)
finally:
    with open("_aca_results.json", "w", encoding="utf-8") as fh:
        json.dump({"records": result.records, "duration": round(time.time() - start, 3)}, fh)
'''


@dataclass
class TestReport:
    __test__ = False  # keep pytest from collecting this class
    total: int = 0
    passed_count: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    cases: List[dict] = field(default_factory=list)
    harness_error: Optional[str] = None
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        return (self.total > 0 and self.failed == 0 and self.errors == 0
                and not self.harness_error)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed, "total": self.total, "passed_count": self.passed_count,
            "failed": self.failed, "errors": self.errors, "skipped": self.skipped,
            "duration": self.duration, "cases": self.cases,
            "harness_error": self.harness_error, "stdout": self.stdout, "stderr": self.stderr,
        }


class TestRunner:
    __test__ = False

    def __init__(self, executor: BaseExecutor, timeout: Optional[int] = None):
        self.executor = executor
        self.timeout = timeout or Config.EXECUTION_TIMEOUT

    def run(self, code: str, tests: str) -> TestReport:
        files = {"solution.py": code, "test_solution.py": tests, "_aca_harness.py": HARNESS}
        res = self.executor.run_files(files, "_aca_harness.py", self.timeout,
                                      collect=["_aca_results.json"])
        report = TestReport(duration=round(res.duration, 3), stdout=res.stdout, stderr=res.stderr)
        if res.blocked_reason:
            report.harness_error = f"Blocked by security policy: {res.blocked_reason}"
            return report
        if res.timed_out:
            report.harness_error = f"Test run timed out after {self.timeout}s (possible infinite loop)."
        raw = res.files.get("_aca_results.json")
        if raw is None:
            report.harness_error = report.harness_error or "Test harness produced no results (see stderr)."
            return report
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            report.harness_error = "Test harness results were unreadable."
            return report
        for rec in data.get("records", []):
            status = rec.get("status", "error")
            name = str(rec.get("name", "?")).replace("test_solution.", "", 1)
            report.cases.append({"name": name, "status": status,
                                 "message": rec.get("message", "")})
            report.total += 1
            if status == "passed":
                report.passed_count += 1
            elif status == "failed":
                report.failed += 1
            elif status == "skipped":
                report.skipped += 1
            else:
                report.errors += 1
        report.duration = float(data.get("duration", report.duration))
        return report
