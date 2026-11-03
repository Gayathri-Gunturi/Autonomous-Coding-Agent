"""Debugger Agent: analyses failures and returns corrected code."""
import json
from dataclasses import dataclass
from typing import Optional

from agents.base import BaseAgent
from utils.code_parser import ParseError, extract_code, extract_tag

DEBUGGER_SYSTEM = (
    "You are a senior debugging engineer. Find the root cause of failures from stack traces and "
    "failing tests, then return corrected code. Fix the implementation; only change the tests if "
    "they contradict the specification. Use the exact tag format requested.")


@dataclass
class DebugResult:
    analysis: str
    code: str
    tests: Optional[str] = None  # set only when the tests themselves were wrong


def _clip(text: str, limit: int = 5000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def build_debug_prompt(spec, code, tests, execution, report, iteration, max_iterations) -> str:
    failing = [c for c in report.get("cases", []) if c["status"] in ("failed", "error")]
    failing_text = "\n\n".join(f"[{c['status'].upper()}] {c['name']}\n{c['message']}" for c in failing[:8])
    return f"""Debugging attempt {iteration} of {max_iterations}.

SPECIFICATION:
{json.dumps(spec, indent=2)}

CURRENT solution.py:
```python
{code}
```

CURRENT test_solution.py:
```python
{tests}
```

EXECUTION OF solution.py (exit_code={execution.get('exit_code')}, timed_out={execution.get('timed_out')}):
STDOUT:
{_clip(execution.get('stdout'), 1500)}
STDERR:
{_clip(execution.get('stderr'), 3000)}

TEST RESULTS: total={report.get('total')} passed={report.get('passed_count')} failed={report.get('failed')} errors={report.get('errors')}
{('HARNESS PROBLEM: ' + report['harness_error']) if report.get('harness_error') else ''}
{_clip(failing_text)}

Reply in exactly this format:
<analysis>root cause in 2-4 sentences</analysis>
<solution>
```python
...the full corrected solution.py...
```
</solution>
<tests>include ONLY if the tests contradict the specification: the full corrected test_solution.py; otherwise omit this tag</tests>
"""


class DebuggerAgent(BaseAgent):
    name = "Debugger"

    def fix(self, spec, code, tests, execution: dict, report: dict, iteration: int,
            max_iterations: int) -> DebugResult:
        def parse(raw: str) -> DebugResult:
            analysis = (extract_tag(raw, "analysis") or "").strip()
            if not analysis:
                raise ParseError("Missing <analysis> block")
            new_code = extract_code(raw, "solution")
            new_tests = None
            if extract_tag(raw, "tests") is not None:
                try:
                    new_tests = extract_code(raw, "tests")
                except ParseError:
                    new_tests = None
            return DebugResult(analysis, new_code, new_tests)

        return self._ask(parse, build_debug_prompt(spec, code, tests, execution, report,
                                                    iteration, max_iterations),
                         system=DEBUGGER_SYSTEM, purpose="debugger.fix", temperature=0.2)
