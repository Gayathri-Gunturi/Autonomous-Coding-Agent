"""Thread-safe task state shared between the orchestrator thread and Flask handlers."""
import copy
import threading
import time
import uuid
from datetime import datetime, timezone

STAGES = [
    ("analysis", "Requirement Analysis"), ("planning", "Planning"),
    ("codegen", "Code Generation"), ("execution", "Code Execution"),
    ("testing", "Testing"), ("debugging", "Debugging"), ("review", "Final Review"),
]
TERMINAL = {"passed", "failed", "error", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


class TaskState:
    def __init__(self, task: str, language: str, max_iterations: int, session_id: str):
        self.id = uuid.uuid4().hex[:12]
        self.session_id = session_id
        self.task = task
        self.language = language
        self.max_iterations = max_iterations
        self.status = "queued"          # queued | running | passed | failed | error | cancelled
        self.error = None
        self.created_at = time.time()
        self.spec = None
        self.plan = None
        self.code = ""
        self.tests = ""
        self.execution = None
        self.test_report = None
        self.debug_attempts = 0
        self.approved = False
        self.commit_info = None
        self.history = []
        self.logs = []
        self.stages = {sid: {"id": sid, "label": label, "status": "pending", "detail": ""}
                       for sid, label in STAGES}
        self.cancel_event = threading.Event()
        self._lock = threading.RLock()

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def lock(self):
        return self._lock

    def log(self, agent: str, operation: str, message: str, level: str = "info"):
        with self._lock:
            self.logs.append({"time": _now(), "agent": agent, "operation": operation,
                              "level": level, "message": message})
            del self.logs[:-500]

    def set_stage(self, stage: str, status: str, detail: str = ""):
        with self._lock:
            self.stages[stage].update(status=status, detail=detail)

    def add_history(self, record: dict):
        with self._lock:
            self.history.append(record)

    def title(self) -> str:
        return (self.spec or {}).get("title") or self.task[:60]

    def summary(self) -> dict:
        with self._lock:
            return {"task_id": self.id, "title": self.title(), "task": self.task[:200],
                    "status": self.status, "approved": self.approved,
                    "iterations": len(self.history), "created_at": self.created_at}

    def to_dict(self, include_history: bool = False) -> dict:
        with self._lock:
            data = {
                "task_id": self.id, "task": self.task, "language": self.language,
                "status": self.status, "error": self.error, "title": self.title(),
                "max_iterations": self.max_iterations, "debug_attempts": self.debug_attempts,
                "approved": self.approved, "commit_info": self.commit_info,
                "spec": self.spec, "plan": self.plan, "code": self.code, "tests": self.tests,
                "execution": self.execution, "test_report": self.test_report,
                "stages": [self.stages[s] for s, _ in STAGES], "logs": list(self.logs),
                "created_at": self.created_at,
            }
            if include_history:
                data["history"] = self.history
            return copy.deepcopy(data)


class TaskStore:
    def __init__(self, max_tasks: int = 200):
        self._tasks = {}
        self._lock = threading.Lock()
        self.max_tasks = max_tasks

    def add(self, state: TaskState):
        with self._lock:
            self._tasks[state.id] = state
            while len(self._tasks) > self.max_tasks:
                oldest = min((t for t in self._tasks.values() if t.finished),
                             key=lambda t: t.created_at, default=None)
                if oldest is None:
                    break
                del self._tasks[oldest.id]

    def get(self, task_id: str, session_id: str):
        with self._lock:
            state = self._tasks.get(task_id)
        return state if state and state.session_id == session_id else None

    def for_session(self, session_id: str):
        with self._lock:
            items = [t for t in self._tasks.values() if t.session_id == session_id]
        return sorted(items, key=lambda t: t.created_at, reverse=True)

    def delete(self, task_id: str):
        with self._lock:
            self._tasks.pop(task_id, None)
