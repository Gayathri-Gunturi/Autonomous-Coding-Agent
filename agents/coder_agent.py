"""Coder Agent: specification + plan -> complete source code."""
import json

from agents.base import BaseAgent
from utils.code_parser import extract_code

CODER_SYSTEM = (
    "You are an expert Python engineer. Write complete, correct, readable code with docstrings. "
    "Reply with the code inside <solution></solution> tags (a ```python fence inside is fine) "
    "and nothing else of importance outside the tags.")


def build_code_prompt(task: str, spec: dict, plan: list) -> str:
    return f"""Implement the following as a single Python module that will be saved as solution.py.

ORIGINAL TASK:
{task}

SPECIFICATION:
{json.dumps(spec, indent=2)}

IMPLEMENTATION PLAN:
{json.dumps(plan, indent=2)}

Requirements:
- Expose every item in public_api at module level with exactly the specified names/signatures.
- Include all imports, type hints where useful, and docstrings.
- Standard library only unless the specification demands otherwise.
- Never read input(), use the network, read environment variables, or touch files outside the cwd.
- Add a main() demo guarded by `if __name__ == "__main__":` that prints a few sample results,
  finishes within a second, and exits with code 0.

Return: <solution>
```python
...code...
```
</solution>
"""


class CoderAgent(BaseAgent):
    name = "Coder"

    def generate(self, task: str, spec: dict, plan: list) -> str:
        return self._ask(lambda raw: extract_code(raw, "solution"),
                         build_code_prompt(task, spec, plan), system=CODER_SYSTEM,
                         purpose="coder.generate", temperature=0.3)
