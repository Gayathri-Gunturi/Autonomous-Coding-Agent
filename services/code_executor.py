"""Best-effort subprocess execution of untrusted (LLM-generated) code.

SECURITY NOTE: this is NOT a fully secure sandbox. It gives you process isolation, a
throw-away working directory, a scrubbed environment, wall-clock timeouts, output caps,
a heuristic static policy scan and (POSIX only) CPU/memory/file-size rlimits. It does not
block network access or reads of arbitrary files the OS user can read. For stronger
isolation, implement ``BaseExecutor`` with Docker (see README) and pass it to ``create_app``.
"""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional

from config import Config

BLOCKED_PATTERNS = [
    (r"\.env\b", "access to .env files"),
    (r"GEMINI_API_KEY", "access to application secrets"),
    (r"\bsubprocess\b", "spawning subprocesses"),
    (r"\bos\.(system|popen|exec\w*|spawn\w*|fork\w*)\b", "spawning processes through os"),
    (r"\bsocket\b", "raw network sockets"),
    (r"\bctypes\b", "native code through ctypes"),
    (r"\bshutil\.rmtree\b", "recursive deletion"),
]


def scan_policy(files: Dict[str, str]) -> Optional[str]:
    """Heuristic static scan. Returns a reason string if a file should be blocked."""
    for name, content in files.items():
        if name.startswith("_aca_"):
            continue
        for pattern, reason in BLOCKED_PATTERNS:
            if re.search(pattern, content):
                return f"{reason} (found in {name})"
    return None


@dataclass
class ExecutionResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    duration: float = 0.0
    truncated: bool = False
    blocked_reason: Optional[str] = None
    files: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.blocked_reason

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "stdout": self.stdout, "stderr": self.stderr,
            "exit_code": self.exit_code, "timed_out": self.timed_out,
            "duration": round(self.duration, 3), "truncated": self.truncated,
            "blocked_reason": self.blocked_reason,
        }


class BaseExecutor(ABC):
    """Interface so a Docker-based executor can replace the subprocess one later."""
    language = "python"

    @abstractmethod
    def run_files(self, files: Dict[str, str], entry: str, timeout: Optional[int] = None,
                  collect: Iterable[str] = ()) -> ExecutionResult: ...

    @abstractmethod
    def run_code(self, code: str, timeout: Optional[int] = None) -> ExecutionResult: ...


_BOOTSTRAP = '''
import sys, runpy
def _limit(name, value):
    try:
        import resource
        res = getattr(resource, name)
        resource.setrlimit(res, (value, value))
    except Exception:
        pass
_limit("RLIMIT_CPU", int(sys.argv[2]))
_limit("RLIMIT_FSIZE", 10 * 1024 * 1024)
_limit("RLIMIT_AS", int(sys.argv[3]) * 1024 * 1024)
entry = sys.argv[1]
sys.argv = [entry]
runpy.run_path(entry, run_name="__main__")
'''


class PythonExecutor(BaseExecutor):
    language = "python"

    def __init__(self, timeout=None, max_output_bytes=None, max_memory_mb=None):
        self.timeout = timeout or Config.EXECUTION_TIMEOUT
        self.max_output = max_output_bytes or Config.MAX_OUTPUT_BYTES
        self.max_memory_mb = max_memory_mb or Config.MAX_MEMORY_MB

    # -- public API -------------------------------------------------------
    def run_code(self, code, timeout=None):
        return self.run_files({"solution.py": code}, "solution.py", timeout)

    def run_files(self, files, entry, timeout=None, collect=()):
        timeout = timeout or self.timeout
        for name in list(files) + [entry]:
            if os.path.basename(name) != name or name.startswith("."):
                raise ValueError("Invalid file name")
        reason = scan_policy(files)
        if reason:
            return ExecutionResult(
                blocked_reason=reason,
                stderr=f"Blocked by security policy: {reason}. Rewrite the code without it.")

        with tempfile.TemporaryDirectory(prefix="aca_work_", ignore_cleanup_errors=True) as work, \
             tempfile.TemporaryDirectory(prefix="aca_io_", ignore_cleanup_errors=True) as io_dir:
            root, io_root = Path(work), Path(io_dir)
            for name, content in files.items():
                (root / name).write_text(content, encoding="utf-8")
            (root / "_aca_bootstrap.py").write_text(_BOOTSTRAP, encoding="utf-8")
            out_path, err_path = io_root / "stdout.txt", io_root / "stderr.txt"
            cmd = [sys.executable, "-B", "_aca_bootstrap.py", entry,
                   str(timeout + 1), str(self.max_memory_mb)]
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                           | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                kwargs["start_new_session"] = True

            timed_out, exit_code = False, None
            start = time.monotonic()
            with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
                proc = subprocess.Popen(cmd, cwd=str(root), stdin=subprocess.DEVNULL,
                                        stdout=fo, stderr=fe, env=self._env(root), **kwargs)
                try:
                    exit_code = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._kill(proc)
            duration = time.monotonic() - start

            stdout, t1 = self._read_capped(out_path)
            stderr, t2 = self._read_capped(err_path)
            if timed_out:
                stderr += f"\n[Execution timed out after {timeout}s and was terminated]"
            collected = {}
            for name in collect:
                path = root / name
                if path.is_file() and path.stat().st_size < 1_000_000:
                    collected[name] = self._sanitize(
                        path.read_text(encoding="utf-8", errors="replace"), root, io_root)
            result = ExecutionResult(
                stdout=self._sanitize(stdout, root, io_root), stderr=self._sanitize(stderr, root, io_root),
                exit_code=None if timed_out else exit_code, timed_out=timed_out,
                duration=duration, truncated=t1 or t2, files=collected)
            return result

    # -- internals --------------------------------------------------------
    @staticmethod
    def _env(root: Path) -> dict:
        """Minimal environment: no API keys or other secrets are inherited."""
        keep = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
        env = {k: os.environ[k] for k in keep if k in os.environ}
        env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONUNBUFFERED="1", HOME=str(root), USERPROFILE=str(root))
        return env

    @staticmethod
    def _kill(proc):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10)
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass

    def _read_capped(self, path: Path):
        size = path.stat().st_size if path.exists() else 0
        with open(path, "rb") as fh:
            data = fh.read(self.max_output)
        text = data.decode("utf-8", errors="replace")
        if size > self.max_output:
            text += f"\n[output truncated at {self.max_output} bytes]"
        return text, size > self.max_output

    @staticmethod
    def _sanitize_text(text: str) -> str:
        """Scrub interpreter paths from text that did not pass through run_files."""
        for prefix in {sys.prefix, sys.base_prefix}:
            text = text.replace(prefix, "<python>").replace(prefix.replace("\\", "/"), "<python>")
        return text

    @staticmethod
    def _sanitize(text: str, *roots: Path) -> str:
        """Hide server-internal paths from anything shown to the user."""
        for p in roots:
            for variant in {str(p), str(p).replace("\\", "/"), str(p.resolve())}:
                text = text.replace(variant, "<sandbox>")
        for prefix in {sys.prefix, sys.base_prefix}:
            text = text.replace(prefix, "<python>").replace(prefix.replace("\\", "/"), "<python>")
        return text
