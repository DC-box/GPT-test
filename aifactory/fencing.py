"""Remove a wrapping markdown code fence without touching interior backticks."""

from __future__ import annotations

import re

# A fence that wraps the entire model reply. The body is greedy so a closing
# fence is the last one, and any ``` inside the program stays in the body.
_WRAPPED = re.compile(
    r"^```(?:python|py)?[ \t]*\r?\n(.*)\r?\n```[ \t]*$",
    re.DOTALL | re.IGNORECASE,
)

# Prose before/after a single trailing fence. Still greedy in the body so
# interior fence lines are kept; used only when the reply itself ends with ```.
_PROSE_WRAPPED = re.compile(
    r"```(?:python|py)?[ \t]*\r?\n(.*)\r?\n```[ \t]*$",
    re.DOTALL | re.IGNORECASE,
)


def strip_markdown_fences(text: str) -> str:
    """Strip one outer ```python / ``` fence from model output.

    Unlike ``str.replace("```python", "").replace("```", "")``, this leaves
    triple-backtick sequences that appear inside the generated program.
    """
    if text is None:
        return ""
    stripped = str(text).strip()
    if not stripped:
        return ""

    wrapped = _WRAPPED.match(stripped)
    if wrapped:
        body = wrapped.group(1)
    elif stripped.endswith("```") and "```" in stripped:
        prose = _PROSE_WRAPPED.search(stripped)
        body = prose.group(1) if prose else stripped
    else:
        body = stripped

    if body.endswith("\n"):
        return body
    return body + "\n"
