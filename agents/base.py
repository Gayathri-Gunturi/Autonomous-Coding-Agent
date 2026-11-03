"""Shared base class for agents."""
from utils.code_parser import ParseError


class BaseAgent:
    name = "Agent"

    def __init__(self, llm):
        self.llm = llm

    def _ask(self, parser, prompt, *, system, purpose, json_mode=False, temperature=0.2, attempts=2):
        """Call the LLM and parse the reply; on a format error retry once with a reminder."""
        last_error = None
        for _ in range(attempts):
            raw = self.llm.generate(prompt, system=system, json_mode=json_mode,
                                    temperature=temperature, purpose=purpose)
            try:
                return parser(raw)
            except ParseError as exc:
                last_error = exc
                prompt += (f"\n\nIMPORTANT: your previous reply could not be parsed ({exc}). "
                           "Follow the required output format exactly.")
        raise last_error
