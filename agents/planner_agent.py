"""Planner Agent: natural language -> structured specification -> implementation plan."""
import json

from agents.base import BaseAgent
from utils.code_parser import ParseError, extract_json

ANALYSIS_SYSTEM = (
    "You are a senior software architect. Turn a user's coding request into a precise, "
    "testable specification. Reply with a single JSON object only.")
PLAN_SYSTEM = (
    "You are a senior software architect. Produce a short, ordered implementation plan. "
    "Reply with a single JSON object only.")


def build_analysis_prompt(task: str, language: str) -> str:
    return f"""Analyze this coding task and return a JSON object with exactly these keys:
- "title": short title (max 8 words)
- "language": "{language}"
- "summary": one-paragraph restatement of the task
- "functional_requirements": list of strings
- "constraints": list of strings (performance, libraries, I/O rules, etc.)
- "edge_cases": list of strings that tests should cover
- "public_api": list of objects {{"name", "signature", "description"}} for every function/class
  that must be importable from a module named solution.py (this is the contract that both the
  implementation and the unit tests must follow; choose clear names if the user did not)
- "demo_behavior": one sentence describing what running `python solution.py` should print

Rules: Python 3.10+, standard library only unless the task clearly needs more; no input(), no
network access, no file access outside the working directory.

TASK:
{task}
"""


def build_plan_prompt(spec: dict) -> str:
    return f"""Given this specification, return JSON {{"steps": [{{"step": 1, "title": "...", "details": "..."}}]}}
with 3-7 concrete implementation steps (data structures, algorithm, edge-case handling, demo).

SPECIFICATION:
{json.dumps(spec, indent=2)}
"""


def _str_list(value):
    if isinstance(value, str):
        value = [value]
    return [str(v) for v in value] if isinstance(value, list) else []


def normalize_spec(data: dict, task: str, language: str) -> dict:
    api = []
    for item in data.get("public_api") or []:
        if isinstance(item, dict) and item.get("name"):
            api.append({"name": str(item["name"]), "signature": str(item.get("signature", "")),
                        "description": str(item.get("description", ""))})
        elif isinstance(item, str):
            api.append({"name": item, "signature": item, "description": ""})
    if not api:
        raise ParseError("Specification had no public_api entries")
    return {
        "title": str(data.get("title") or task[:60]).strip()[:80],
        "language": language,
        "summary": str(data.get("summary") or task),
        "functional_requirements": _str_list(data.get("functional_requirements")),
        "constraints": _str_list(data.get("constraints")),
        "edge_cases": _str_list(data.get("edge_cases")),
        "public_api": api,
        "demo_behavior": str(data.get("demo_behavior") or "Prints a short demo and exits."),
    }


def normalize_plan(data: dict) -> list:
    steps = []
    for i, item in enumerate(data.get("steps") or [], 1):
        if isinstance(item, dict):
            steps.append({"step": i, "title": str(item.get("title", f"Step {i}")),
                          "details": str(item.get("details", ""))})
        else:
            steps.append({"step": i, "title": str(item), "details": ""})
    if not steps:
        raise ParseError("Plan had no steps")
    return steps


class PlannerAgent(BaseAgent):
    name = "Planner"

    def analyze(self, task: str, language: str) -> dict:
        return self._ask(lambda raw: normalize_spec(extract_json(raw), task, language),
                         build_analysis_prompt(task, language), system=ANALYSIS_SYSTEM,
                         json_mode=True, purpose="planner.analyze")

    def plan(self, spec: dict) -> list:
        return self._ask(lambda raw: normalize_plan(extract_json(raw)),
                         build_plan_prompt(spec), system=PLAN_SYSTEM,
                         json_mode=True, purpose="planner.plan")
