"""Helpers for reliably extracting code / JSON from LLM replies."""
import json
import re


class ParseError(ValueError):
    """Raised when an LLM reply does not follow the requested output format."""


_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\r?\n(.*?)```", re.DOTALL)


def extract_tag(text: str, tag: str):
    """Return the text inside <tag>...</tag>, or None when the tag is absent or unterminated."""
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text or "", re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else None


def strip_fences(text: str) -> str:
    """Return the largest fenced block in text, or the stripped text if there is none."""
    blocks = _FENCE.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip("\n")
    return (text or "").strip("\n")


def extract_code(text: str, tag: str = "solution") -> str:
    """Extract code from <tag> (preferred) or from a fenced block (fallback)."""
    block = extract_tag(text, tag)
    if block is None:
        if not _FENCE.search(text or ""):
            raise ParseError(f"Expected <{tag}> block with code in the reply")
        block = text
    code = strip_fences(block)
    if not code.strip():
        raise ParseError(f"<{tag}> block was empty")
    return code.rstrip() + "\n"


def extract_json(text: str) -> dict:
    """Parse a JSON object from a reply that may be wrapped in fences or prose."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = strip_fences(raw)
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ParseError("Reply was not a valid JSON object")
