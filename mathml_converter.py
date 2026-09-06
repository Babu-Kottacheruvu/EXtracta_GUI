"""Stage 12 of the math pipeline: LLM Repair Engine, routed through OpenRouter.

This is no longer the primary LaTeX -> MathML path -- latex_to_mathml.py's
deterministic parser is (stage 8, free, offline, instant). This module only
gets called for a formula the validator/confidence engine (stages 7 and 13)
flagged as shaky, and its job is narrow: given the LaTeX the deterministic
pipeline extracted and what's wrong with it, return corrected canonical
LaTeX -- never MathML. pdf_to_xml.py re-runs the deterministic converter on
whatever comes back. Needs a user-supplied OpenRouter API key (see config.py
/ the Settings UI); silently skipped otherwise, since a repair engine that
can't run is just "keep the unrepaired result", not a failure.
"""

import json
import re
import time
import urllib.error
import urllib.request

from config import get_openrouter_api_key

OPENROUTER_MODEL = "openai/gpt-4o-mini"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

_REPAIR_PROMPT = """You are a mathematical expression repair engine.

Your task is NOT to freely rewrite mathematics.

Input:
1. Extracted LaTeX (built directly from the source PDF's text layer)
2. A list of structural issues found in that LaTeX, if any
3. Previous candidate LaTeX, if available

Rules:

1. Preserve the mathematical meaning exactly.
2. Do not invent symbols that are not supported by the source.
3. Do not change variable names.
4. Do not change superscripts into multiplication.
5. Do not change subscripts into normal text.
6. Preserve fractions as fractions.
7. Preserve square roots as square roots.
8. Preserve integrals and their limits.
9. Preserve summation/product limits.
10. Preserve matrices and aligned structures.
11. Preserve Greek letters.
12. Preserve accents, hats, bars and vector notation.
13. Preserve differential operators.
14. Preserve parentheses and delimiters.
15. Preserve equation grouping.
16. Treat the entire input as ONE mathematical expression.
17. Do not split one formula into multiple independent formulas.
18. Return canonical LaTeX only.
19. If the input is already correct, return it unchanged.
20. Never output MathML.

Before returning the answer, verify:
- balanced braces
- balanced delimiters
- valid LaTeX commands
- correct subscript/superscript structure
- fraction structure
- root structure
- integral limits
- summation limits
- matrix structure

Return ONLY this JSON, no explanation, no markdown fences:

{{
  "latex": "...",
  "confidence": 0.0,
  "issues": [],
  "needs_human_review": false
}}

Extracted LaTeX:
{latex}

Structural issues found:
{issues}

Previous candidate LaTeX:
{previous}
"""

# pdf_to_xml.py only calls this for formulas the confidence engine already
# flagged (a small minority of a document's total), so retries only need to
# ride out a genuinely transient blip, not a sustained multi-minute outage.
_MAX_RETRIES = 4
_BACKOFF_SECONDS = (3, 6, 12, 24)
_TRANSIENT_BACKOFF_SECONDS = (1, 2, 4)  # for 5xx server errors, not quota


def _call_openrouter(prompt, api_key, log_callback=None):
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode("utf-8")

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "http://127.0.0.1:8642",
            "X-Title": "EXtracta",
        },
    )

    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            if attempt == _MAX_RETRIES - 1:
                raise
            if e.code == 429:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(retry_after) if retry_after else _BACKOFF_SECONDS[attempt]
                except ValueError:
                    wait = _BACKOFF_SECONDS[attempt]
                reason = "rate-limited"
            elif 500 <= e.code < 600 and attempt < len(_TRANSIENT_BACKOFF_SECONDS):
                wait = _TRANSIENT_BACKOFF_SECONDS[attempt]
                reason = f"got a server error ({e.code})"
            else:
                raise
            if log_callback:
                log_callback(
                    f"Repair engine {reason}, retrying in {wait:.0f}s "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES})..."
                )
            time.sleep(wait)


def _parse_response(text):
    match = _JSON_FENCE_RE.search(text)
    candidate = match.group(1) if match else text
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        result = json.loads(candidate[start:end + 1])
    except ValueError:
        return None
    if not isinstance(result, dict) or "latex" not in result:
        return None
    return result


def repair_latex(latex, issues=None, previous=None, log_callback=None):
    """Ask the LLM to repair `latex` given the structural `issues` the
    validator found (and, on a second pass, the `previous` candidate that
    still wasn't good enough). Returns the parsed repair-engine JSON dict
    ({"latex", "confidence", "issues", "needs_human_review"}), or None if no
    API key is configured or the repair attempt failed for any reason --
    callers should treat that as "keep the unrepaired result", not a fatal
    error.
    """
    api_key = get_openrouter_api_key()
    if not api_key or not latex:
        return None

    prompt = _REPAIR_PROMPT.format(
        latex=latex,
        issues=json.dumps(issues or []),
        previous=previous or "(none)",
    )

    for attempt in range(2):
        try:
            text = _call_openrouter(prompt, api_key, log_callback=log_callback)
            result = _parse_response(text)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, IndexError, ValueError) as e:
            if log_callback:
                log_callback(f"Repair engine failed for '{latex}': {e}")
            return None
        if result is not None:
            return result
        if attempt == 0 and log_callback:
            log_callback(f"Repair engine returned no usable JSON for '{latex}', retrying once...")
    return None
