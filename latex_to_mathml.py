"""Stage 8 of the math pipeline: deterministic LaTeX -> MathML.

A small hand-written tokenizer + recursive-descent parser that covers the
LaTeX constructs pdf_to_xml.py's build_latex_from_spans() actually produces
(fractions, roots, sub/superscripts, big operators with limits, matrices,
accents, Greek letters, common symbols) and turns them directly into MathML
-- no network call, no LLM, no cost. This is the primary conversion path;
the LLM repair engine (mathml_converter.py) only gets involved when the
validator/confidence engine decide this wasn't good enough.

Every element is built with a plain, unprefixed tag name (not ElementTree's
{namespace-uri}tag Clark notation), and the root <math> carries a literal
xmlns="..." attribute instead of relying on an ancestor's xmlns:mml. That
makes each formula's <math>...</math> a fully self-contained, valid MathML
document on its own -- copyable out of the surrounding JATS <article> (whose
own xmlns:mml declaration this deliberately doesn't need) into any other
document without losing its namespace.

Degrades gracefully by design: malformed or unsupported input never raises
out of latex_to_mathml_element() -- it just returns the best tree it could
build, or None. A wrong/missing symbol here is a confidence-engine problem,
not a crash.
"""

import re
import xml.etree.ElementTree as ET

_TOKEN_RE = re.compile(
    r"(?P<WS>\s+)"
    r"|(?P<ROWSEP>\\\\)"
    r"|(?P<CMD>\\[A-Za-z]+)"
    r"|(?P<CMDSYM>\\.)"
    r"|(?P<LBRACE>\{)"
    r"|(?P<RBRACE>\})"
    r"|(?P<LBRACK>\[)"
    r"|(?P<RBRACK>\])"
    r"|(?P<CARET>\^)"
    r"|(?P<UNDERSCORE>_)"
    r"|(?P<AMP>&)"
    r"|(?P<NUMBER>\d+(?:\.\d+)?)"
    r"|(?P<LETTERS>[A-Za-z]+)"
    r"|(?P<OTHER>.)",
    re.DOTALL,
)

_DELIM_CHARS = set("()[]|.")

_ACCENTS = {
    "hat": "^", "widehat": "^", "bar": "ˉ", "overline": "ˉ",
    "vec": "→", "dot": "˙", "ddot": "¨", "tilde": "~", "widetilde": "~",
    "check": "ˇ", "breve": "˘",
}

_VARIANTS = {"mathbf": "bold", "mathrm": "normal", "mathit": "italic", "mathcal": "script"}

# \int and friends show their limits as ordinary sub/superscripts even in
# display style; \sum/\prod/\bigcup/etc. conventionally stack them above and
# below the symbol instead -- munder/mover/munderover vs. msub/msup/msubsup.
_BIG_SYMBOLS = {
    "sum": "∑", "prod": "∏", "coprod": "∐",
    "int": "∫", "oint": "∮", "iint": "∬", "iiint": "∭", "oiint": "∯",
    "bigcup": "⋃", "bigcap": "⋂", "bigvee": "⋁", "bigwedge": "⋀",
    "bigoplus": "⨁", "bigotimes": "⨂", "bigodot": "⨀", "biguplus": "⨄",
}
_UNDER_OVER_NAMES = {
    "sum", "prod", "coprod", "bigcup", "bigcap", "bigvee", "bigwedge",
    "bigoplus", "bigotimes", "bigodot", "biguplus",
}

_LIMIT_FUNCTIONS = {"lim", "max", "min", "sup", "inf"}
_FUNCTION_NAMES = {
    "sin", "cos", "tan", "cot", "sec", "csc",
    "sinh", "cosh", "tanh", "coth",
    "log", "ln", "exp", "det", "dim", "ker", "deg", "gcd", "arg",
}

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "ϕ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
    "hbar": "ℏ", "ell": "ℓ", "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "wp": "℘",
}

_SYMBOLS = {
    "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "cdot": "·", "cdots": "⋯", "ldots": "…", "vdots": "⋮", "ddots": "⋱",
    "neq": "≠", "approx": "≈", "equiv": "≡", "leq": "≤", "geq": "≥",
    "ll": "≪", "gg": "≫", "sim": "∼", "simeq": "≃", "cong": "≅",
    "propto": "∝", "infty": "∞", "partial": "∂", "nabla": "∇",
    "in": "∈", "notin": "∉", "subset": "⊂", "supset": "⊃",
    "subseteq": "⊆", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "wedge": "∧", "vee": "∨", "oplus": "⊕", "otimes": "⊗",
    "odot": "⊙", "oslash": "⊘", "emptyset": "∅", "forall": "∀",
    "exists": "∃", "nexists": "∄", "angle": "∠", "perp": "⊥",
    "parallel": "∥", "top": "⊤", "bot": "⊥",
    "langle": "⟨", "rangle": "⟩", "lceil": "⌈", "rceil": "⌉",
    "lfloor": "⌊", "rfloor": "⌋",
}

_MATRIX_FENCES = {
    "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "vmatrix": ("|", "|"),
    "Bmatrix": ("{", "}"), "Vmatrix": ("‖", "‖"), "cases": ("{", None),
    "matrix": (None, None),
}


def _new(tag, text=None):
    el = ET.Element(tag)
    if text is not None:
        el.text = text
    return el


def _wrap(tag, children):
    el = ET.Element(tag)
    for c in children:
        el.append(c)
    return el


def _mi(text, variant=None):
    el = _new("mi", text)
    if variant:
        el.set("mathvariant", variant)
    return el


def _mo(text, fence=False):
    el = _new("mo", text)
    if fence:
        el.set("stretchy", "true")
        el.set("fence", "true")
    return el


def _mn(text):
    return _new("mn", text)


def _apply_variant(node, variant):
    if node.tag in ("mi", "mn"):
        node.set("mathvariant", variant)
    for child in node:
        _apply_variant(child, variant)


def _collect_text(node):
    parts = [node.text or ""]
    for child in node:
        parts.append(_collect_text(child))
    return "".join(parts)


def _tokenize(latex):
    tokens = []
    for m in _TOKEN_RE.finditer(latex):
        kind = m.lastgroup
        if kind == "WS":
            continue
        value = m.group()
        if kind == "CMD":
            value = value[1:]
        tokens.append((kind, value))
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.peek()
        if tok and tok[0] == kind:
            self.advance()

    def parse_row(self, stop_kinds=frozenset(), stop_cmds=frozenset()):
        nodes = []
        while True:
            tok = self.peek()
            if tok is None:
                break
            kind, value = tok
            if kind in stop_kinds:
                break
            if kind == "CMD" and value in stop_cmds:
                break
            node = self.parse_item()
            if node is not None:
                nodes.append(node)
        if not nodes:
            return _new("mrow")
        if len(nodes) == 1:
            return nodes[0]
        return _wrap("mrow", nodes)

    def parse_item(self):
        base, is_large = self.parse_base()
        if base is None:
            return None

        sub = sup = None
        while True:
            tok = self.peek()
            if tok and tok[0] == "UNDERSCORE" and sub is None:
                self.advance()
                sub = self.parse_arg()
            elif tok and tok[0] == "CARET" and sup is None:
                self.advance()
                sup = self.parse_arg()
            else:
                break

        if sub is None and sup is None:
            return base
        if is_large:
            if sub is not None and sup is not None:
                return _wrap("munderover", [base, sub, sup])
            if sub is not None:
                return _wrap("munder", [base, sub])
            return _wrap("mover", [base, sup])
        if sub is not None and sup is not None:
            return _wrap("msubsup", [base, sub, sup])
        if sub is not None:
            return _wrap("msub", [base, sub])
        return _wrap("msup", [base, sup])

    def parse_arg(self):
        tok = self.peek()
        if tok and tok[0] == "LBRACE":
            self.advance()
            content = self.parse_row(stop_kinds={"RBRACE"})
            self.expect("RBRACE")
            return content
        node, _ = self.parse_base()
        return node if node is not None else _new("mrow")

    def parse_base(self):
        tok = self.peek()
        if tok is None:
            return None, False
        kind, value = tok

        if kind == "CMD":
            return self._parse_command(value)
        if kind == "CMDSYM":
            self.advance()
            return self._parse_cmdsym(value)
        if kind == "LBRACE":
            self.advance()
            content = self.parse_row(stop_kinds={"RBRACE"})
            self.expect("RBRACE")
            return content, False
        if kind == "NUMBER":
            self.advance()
            return _mn(value), False
        if kind == "LETTERS":
            self.advance()
            if value in _LIMIT_FUNCTIONS:
                return _mi(value, "normal"), True
            if value in _FUNCTION_NAMES:
                return _mi(value, "normal"), False
            if len(value) == 1:
                return _mi(value), False
            # An unrecognized run of several plain letters isn't a single
            # identifier -- conventional math typesetting reads adjacent
            # letters as implicit multiplication (e.g. "dx" is d times x,
            # "ix" is i times x), so give each its own <mi> instead of one
            # opaque multi-character <mi> that renders as if it were a
            # single (undefined) variable name.
            return _wrap("mrow", [_mi(ch) for ch in value]), False
        if kind in ("AMP", "ROWSEP", "RBRACE", "RBRACK"):
            # Stray terminator where none was expected -- consume it so the
            # caller always makes progress instead of looping forever.
            self.advance()
            return None, False
        if kind == "LBRACK":
            self.advance()
            return _mo("["), False
        # kind == "OTHER"
        self.advance()
        if value.isalpha():
            return _mi(value), False
        if value.isdigit():
            return _mn(value), False
        return _mo(value), False

    def _parse_command(self, name):
        self.advance()

        if name in ("quad", "qquad"):
            return None, False

        if name == "frac":
            num = self.parse_arg()
            den = self.parse_arg()
            return _wrap("mfrac", [num, den]), False

        if name == "sqrt":
            index = None
            tok = self.peek()
            if tok and tok[0] == "LBRACK":
                self.advance()
                index = self.parse_row(stop_kinds={"RBRACK"})
                self.expect("RBRACK")
            radicand = self.parse_arg()
            if index is not None:
                return _wrap("mroot", [radicand, index]), False
            return _wrap("msqrt", [radicand]), False

        if name in _ACCENTS:
            arg = self.parse_arg()
            node = _wrap("mover", [arg, _mo(_ACCENTS[name])])
            node.set("accent", "true")
            return node, False

        if name in _VARIANTS:
            arg = self.parse_arg()
            _apply_variant(arg, _VARIANTS[name])
            return arg, False

        if name == "text":
            arg = self.parse_arg()
            return _new("mtext", _collect_text(arg)), False

        if name == "left":
            return self._parse_fenced(), False

        if name == "begin":
            return self._parse_environment(), False

        if name in _BIG_SYMBOLS:
            return _mo(_BIG_SYMBOLS[name]), name in _UNDER_OVER_NAMES

        if name in _LIMIT_FUNCTIONS:
            return _mi(name, "normal"), True

        if name in _FUNCTION_NAMES:
            return _mi(name, "normal"), False

        if name in _GREEK:
            return _mi(_GREEK[name]), False

        if name in _SYMBOLS:
            return _mo(_SYMBOLS[name]), False

        # Unrecognized command -- surface the bare name rather than
        # dropping it silently. latex_validator.py already flags this and
        # the confidence engine scores it down.
        return _mi(name), False

    def _parse_cmdsym(self, value):
        ch = value[1] if len(value) > 1 else ""
        if ch in (",", ";", "!", " ", "\\"):
            return None, False
        if ch in ("{", "}"):
            return _mo(ch), False
        if ch:
            return _mo(ch), False
        return None, False

    def _read_delim(self):
        tok = self.peek()
        if tok is None:
            return ""
        kind, value = tok
        if kind == "CMDSYM" and value in ("\\{", "\\}"):
            self.advance()
            return value[1]
        if kind in ("OTHER", "LBRACK", "RBRACK") and value in _DELIM_CHARS:
            self.advance()
            return "" if value == "." else value
        self.advance()
        return ""

    def _parse_fenced(self):
        open_delim = self._read_delim()
        content = self.parse_row(stop_cmds={"right"})
        tok = self.peek()
        if tok and tok[0] == "CMD" and tok[1] == "right":
            self.advance()
        close_delim = self._read_delim()

        children = []
        if open_delim:
            children.append(_mo(open_delim, fence=True))
        children.append(content)
        if close_delim:
            children.append(_mo(close_delim, fence=True))
        return _wrap("mrow", children)

    def _parse_environment(self):
        self.expect("LBRACE")
        env_tok = self.peek()
        env_name = env_tok[1] if env_tok and env_tok[0] == "LETTERS" else ""
        if env_tok and env_tok[0] == "LETTERS":
            self.advance()
        self.expect("RBRACE")

        rows = [[]]
        while True:
            cell = self.parse_row(stop_kinds={"AMP", "ROWSEP"}, stop_cmds={"end"})
            rows[-1].append(cell)
            tok = self.peek()
            if tok is None:
                break
            kind, _ = tok
            if kind == "AMP":
                self.advance()
                continue
            if kind == "ROWSEP":
                self.advance()
                rows.append([])
                continue
            break  # CMD "end" reached

        tok = self.peek()
        if tok and tok[0] == "CMD" and tok[1] == "end":
            self.advance()
            self.expect("LBRACE")
            if self.peek() and self.peek()[0] == "LETTERS":
                self.advance()
            self.expect("RBRACE")

        mtable = _new("mtable")
        for row in rows:
            mtr = _wrap("mtr", [_wrap("mtd", [cell]) for cell in row])
            mtable.append(mtr)

        open_delim, close_delim = _MATRIX_FENCES.get(env_name, (None, None))
        if not open_delim and not close_delim:
            return mtable
        children = []
        if open_delim:
            children.append(_mo(open_delim, fence=True))
        children.append(mtable)
        if close_delim:
            children.append(_mo(close_delim, fence=True))
        return _wrap("mrow", children)


def latex_to_mathml_element(latex):
    """Deterministically convert a LaTeX string to a <math> Element, or
    None if there's nothing to convert. Never raises -- any parse trouble on
    genuinely messy input just means a worse (but non-crashing) result, and
    it's the caller's job to decide (via the validator/confidence engine)
    whether that result is good enough to keep.
    """
    if not latex or not latex.strip():
        return None
    try:
        tokens = _tokenize(latex)
        if not tokens:
            return None
        content = _Parser(tokens).parse_row()
        math = _new("math")
        # A literal xmlns here (rather than relying on the surrounding JATS
        # <article>'s xmlns:mml) is what makes this <math>...</math> a
        # self-contained, valid MathML document on its own -- it still
        # renders correctly if copied out of the article into any other
        # document, or opened standalone.
        math.set("xmlns", "http://www.w3.org/1998/Math/MathML")
        math.set("display", "inline")
        math.append(content)
        return math
    except Exception:
        return None
