"""Stage 7 of the math pipeline: LaTeX Validator.

Cheap, deterministic structural checks -- not a full parse (latex_to_mathml.py
does that) -- run before spending time on MathML generation. Every issue
found here feeds the confidence engine and, when severe enough, triggers the
LLM repair engine instead of accepting the deterministic conversion as-is.
"""

import re

_COMMAND_RE = re.compile(r"\\([A-Za-z]+)")
_FRAC_RE = re.compile(r"\\frac\b")
_SQRT_RE = re.compile(r"\\sqrt\b")

# Commands this app's deterministic pipeline (pdf_to_xml.py's _UNICODE_TO_LATEX
# plus latex_to_mathml.py's structural/symbol commands) actually knows how to
# handle. An unrecognized command isn't necessarily wrong -- it's just a
# signal the deterministic MathML step will fall back to treating it as a
# plain identifier, which lowers confidence rather than failing outright.
KNOWN_COMMANDS = {
    "frac", "sqrt", "sum", "prod", "int", "oint", "iint", "iiint",
    "left", "right", "begin", "end",
    "hat", "bar", "vec", "dot", "ddot", "tilde", "check", "breve",
    "widehat", "widetilde", "overline", "underline",
    "mathbf", "mathrm", "mathit", "mathcal", "text",
    "times", "div", "pm", "mp", "cdot", "cdots", "ldots", "vdots", "ddots",
    "neq", "approx", "equiv", "leq", "geq", "ll", "gg", "sim", "simeq", "cong",
    "propto", "infty", "partial", "nabla",
    "in", "notin", "subset", "supset", "subseteq", "supseteq",
    "cup", "cap", "wedge", "vee", "oplus", "otimes", "odot", "oslash",
    "emptyset", "forall", "exists", "nexists",
    "angle", "perp", "parallel", "top", "bot",
    "langle", "rangle", "lceil", "rceil", "lfloor", "rfloor",
    "sin", "cos", "tan", "cot", "sec", "csc",
    "sinh", "cosh", "tanh", "coth",
    "log", "ln", "exp", "lim", "max", "min", "sup", "inf",
    "det", "dim", "ker", "deg", "gcd", "arg",
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta",
    "eta", "theta", "vartheta", "iota", "kappa", "lambda", "mu", "nu",
    "xi", "pi", "varpi", "rho", "varrho", "sigma", "varsigma", "tau",
    "upsilon", "phi", "varphi", "chi", "psi", "omega",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon",
    "Phi", "Psi", "Omega",
    "hbar", "ell", "Re", "Im", "aleph", "wp",
    "quad", "qquad",
}


def validate_latex(latex):
    """Returns (ok, issues). `ok` is False only for structural breakage
    (unbalanced braces/delimiters) that would make parsing unreliable;
    everything else is a soft issue that lowers confidence but doesn't
    block the deterministic converter from trying.
    """
    issues = []
    ok = True

    depth = 0
    for ch in latex:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                issues.append("unmatched closing brace")
                ok = False
                break
    else:
        if depth > 0:
            issues.append(f"{depth} unclosed brace(s)")
            ok = False

    left_count = len(re.findall(r"\\left\b", latex))
    right_count = len(re.findall(r"\\right\b", latex))
    if left_count != right_count:
        issues.append(f"\\left/\\right mismatch ({left_count} vs {right_count})")

    begin_envs = re.findall(r"\\begin\{([a-zA-Z*]+)\}", latex)
    end_envs = re.findall(r"\\end\{([a-zA-Z*]+)\}", latex)
    if begin_envs != end_envs:
        issues.append(f"\\begin/\\end environment mismatch ({begin_envs} vs {end_envs})")
        ok = False

    for m in _COMMAND_RE.finditer(latex):
        name = m.group(1)
        if name not in KNOWN_COMMANDS:
            issues.append(f"unrecognized command \\{name}")

    if re.search(r"[\^_]\s*$", latex):
        issues.append("dangling ^/_ with no argument")

    for m in _FRAC_RE.finditer(latex):
        rest = latex[m.end():].lstrip()
        if not rest:
            issues.append("\\frac with no arguments")

    for m in _SQRT_RE.finditer(latex):
        rest = latex[m.end():]
        rest = re.sub(r"^\[[^\]]*\]", "", rest).lstrip()
        if not rest:
            issues.append("\\sqrt with no argument")

    return ok, issues
