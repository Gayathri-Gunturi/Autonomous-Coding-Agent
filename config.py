"""Central configuration. All values can be overridden through environment variables / .env."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


class Config:
    BASE_DIR = BASE_DIR

    # Gemini
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    GEMINI_TIMEOUT_SECONDS = _int("GEMINI_TIMEOUT_SECONDS", 90, 10, 600)
    GEMINI_MAX_RETRIES = _int("GEMINI_MAX_RETRIES", 3, 1, 6)

    # Agent loop
    DEFAULT_MAX_ITERATIONS = _int("MAX_ITERATIONS", 3, 1, 5)
    MAX_ITERATIONS_CAP = 5

    # Execution limits
    EXECUTION_TIMEOUT = _int("EXECUTION_TIMEOUT", 10, 1, 120)
    MAX_OUTPUT_BYTES = _int("MAX_OUTPUT_BYTES", 20000, 1000, 1_000_000)
    MAX_MEMORY_MB = _int("MAX_MEMORY_MB", 512, 64, 4096)  # enforced on POSIX only

    # Request limits
    MAX_TASK_CHARS = 4000
    MAX_CODE_CHARS = 60000
    MAX_CONCURRENT_TASKS = _int("MAX_CONCURRENT_TASKS", 2, 1, 8)

    # Storage / server
    GENERATED_DIR = Path(os.getenv("GENERATED_DIR", BASE_DIR / "generated_code"))
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = _int("PORT", 5000, 1, 65535)
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

    SUPPORTED_LANGUAGES = {"python": "Python"}
