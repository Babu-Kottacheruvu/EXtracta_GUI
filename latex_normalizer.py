"""Stage 6 of the math pipeline: LaTeX Normalizer.

pdf_to_xml.py's build_latex_from_spans() already produces reasonable LaTeX
for the common case; this cleans up the rough edges that are safe to fix
mechanically (stray whitespace introduced by token-joining, empty groups
left behind by a dropped glyph) without touching anything that could change
the expression's meaning -- that's the validator/repair engine's job, not
this one.
"""

import re

# Only safe to drop the space when what follows can't extend a TeX control
# word's name (a backslash or "{" always terminates it; a bare letter does
# NOT -- "\alpha x" and "\alphax" are different control sequences).
_SPACE_AFTER_BACKSLASH_RE = re.compile(r"\\([A-Za-z]+)\s+(?=[\\{])")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_EMPTY_GROUP_RE = re.compile(r"\{\s*\}")
_SPACE_BEFORE_SCRIPT_RE = re.compile(r"\s+([\^_])")
_SPACE_AFTER_SCRIPT_RE = re.compile(r"([\^_])\s+")


def normalize_latex(latex):
    if not latex:
        return latex

    text = latex.strip()
    text = _SPACE_AFTER_BACKSLASH_RE.sub(r"\\\1", text)
    text = _SPACE_BEFORE_SCRIPT_RE.sub(r"\1", text)
    text = _SPACE_AFTER_SCRIPT_RE.sub(r"\1", text)
    text = _EMPTY_GROUP_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    return text.strip()
