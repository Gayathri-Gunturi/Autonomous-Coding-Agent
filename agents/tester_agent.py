"""Test Agent: writes unit tests from the SPEC (not the code) and executes them."""
import json

from agents.base import BaseAgent
from services.test_runner import TestReport, TestRunner
from utils.code_parser import extract_code

TESTER_SYSTEM = (
    "You are a meticulous QA engineer. Write deterministic unittest suites that verify behaviour "
    "described in a specification. Reply with the test file inside <tests></tests> tags.")


def build_test_prompt(spec: dict) -> str:
    return f"""Write a Python unittest module saved as test_solution.py for a module named solution.py.

SPECIFICATION:
{json.dumps(spec, indent=2)}

Rules:
- `import unittest` and import only names listed in public_api from solution.
- 6-12 focused test methods across one or more unittest.TestCase classes, covering normal cases,
  every listed edge case, and invalid input handling ONLY where the specification defines it.
- Assert only behaviour stated in the specification; do not guess unspecified behaviour.
- Deterministic: no randomness, timing, network, or file access. Do not call main().
- End with `if __name__ == "__main__": unittest.main()`.

Return: <tests>
```python
...tests...
```
</tests>
"""


class TesterAgent(BaseAgent):
    name = "Tester"

    def __init__(self, llm, runner: TestRunner):
        super().__init__(llm)
        self.runner = runner

    def generate_tests(self, spec: dict) -> str:
        return self._ask(lambda raw: extract_code(raw, "tests"), build_test_prompt(spec),
                         system=TESTER_SYSTEM, purpose="tester.generate")

    def run_tests(self, code: str, tests: str) -> TestReport:
        return self.runner.run(code, tests)
