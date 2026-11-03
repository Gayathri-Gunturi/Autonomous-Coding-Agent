"""Optional LOCAL git commit of user-approved code. Never pushes anywhere."""
import shutil
import subprocess
from pathlib import Path


def commit_approved(directory: Path, message: str) -> dict:
    if not shutil.which("git"):
        return {"committed": False, "reason": "git is not installed on the server"}
    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Autonomous Coding Agent",
             "-c", "user.email=agent@localhost", *args],
            cwd=str(directory), capture_output=True, text=True, timeout=20)
    try:
        if not (directory / ".git").exists():
            git("init")
        git("add", "-A")
        res = git("commit", "-m", message[:200])
        if res.returncode != 0:
            return {"committed": False, "reason": (res.stdout + res.stderr).strip()[:200] or "nothing to commit"}
        return {"committed": True, "reason": "Committed locally (not pushed)."}
    except Exception as exc:  # noqa: BLE001
        return {"committed": False, "reason": f"git failed: {type(exc).__name__}"}
