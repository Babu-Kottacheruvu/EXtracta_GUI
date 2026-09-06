import fitz  # PyMuPDF
import xml.etree.ElementTree as ET
from xml.dom import minidom
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from latex_normalizer import normalize_latex
from latex_validator import validate_latex
from latex_to_mathml import latex_to_mathml_element as deterministic_latex_to_mathml
from confidence_engine import score as score_confidence
from mathml_converter import repair_latex


def dedupe_overlapping_spans(spans):
    """Some PDF generators fake a bold/heavy weight by stacking the same run
    of text several times at a sub-pixel offset instead of embedding a real
    bold font. Left alone, that comes through as literal repeated text.
    Collapse spans down to one per (text, ~1pt-rounded position) group,
    keeping first-seen order -- genuinely distinct repeated text (e.g. the
    same word in two different table cells) sits at a clearly different
    position and is left untouched. Set-based (not just adjacent-pair
    comparison) since PyMuPDF doesn't always return the stacked copies next
    to each other.
    """
    seen = set()
    deduped = []
    for span in spans:
        key = (span["text"], round(span["origin"][0]), round(span["origin"][1]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(span)
    return deduped


def _bbox_overlap_ratio(b1, b2):
    """Intersection area as a fraction of the smaller of the two boxes (a
    containment-style ratio, not IoU)."""
    ix0, iy0 = max(b1[0], b2[0]), max(b1[1], b2[1])
    ix1, iy1 = min(b1[2], b2[2]), min(b1[3], b2[3])
    inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
    area1 = max(0, b1[2] - b1[0]) * max(0, b1[3] - b1[1])
    area2 = max(0, b2[2] - b2[0]) * max(0, b2[3] - b2[1])
    smaller = min(area1, area2) or 1
    return inter / smaller


def dedupe_overlapping_lines(lines):
    """Same fake-bold problem as dedupe_overlapping_spans, but for a messier
    real-world case: a decorative heading rendered with a 4-direction
    shadow/emboss effect (the same glyphs stacked at NE/NW/SE/SW pixel
    offsets) plus one "real" center copy. PyMuPDF often reports the shadow
    copies as smaller, more fragmented lines than the center copy (e.g. a
    shadow line containing only "W" next to a center line containing
    "WORLD"), so exact text matching misses it. Instead: when two lines'
    bounding boxes substantially overlap and one line's text is wholly
    contained in the other's, keep only the more complete line.
    """
    kept = []
    for line in lines:
        text = "".join(s["text"] for s in line.get("spans", []))
        norm = re.sub(r"\s+", "", text)
        bbox = line.get("bbox")
        if not norm or not bbox:
            kept.append(line)
            continue

        absorbed = False
        for k in kept:
            k_text = "".join(s["text"] for s in k.get("spans", []))
            k_norm = re.sub(r"\s+", "", k_text)
            k_bbox = k.get("bbox")
            if not k_norm or not k_bbox:
                continue
            if _bbox_overlap_ratio(bbox, k_bbox) <= 0.5:
                continue
            if norm in k_norm:
                absorbed = True
                break
            if k_norm in norm:
                kept.remove(k)
                break
        if not absorbed:
            kept.append(line)
    return kept

_MATH_TOKEN_RE = re.compile(r'\d+\.?\d*|[A-Za-zΑ-Ωα-ω]+|\s+|[^\sA-Za-z0-9]')

# Invisible formatting characters (variation selectors, zero-width space/
# joiners, word joiner, BOM) are rendering hints some PDF generators leave
# in the text layer -- e.g. a "∑" glyph followed by a variation selector.
# _MATH_TOKEN_RE's symbol catch-all would otherwise capture one as its own
# token and pass it through into the LaTeX untouched, silently corrupting a
# perfectly good command right next to it (seen as "\sum" + an invisible
# character, which downstream tools/LLMs then can't make sense of).
_INVISIBLE_CHARS_RE = re.compile(
    "[\uFE00-\uFE0F\u200B-\u200D\u2060\uFEFF]"
)


_UNICODE_TO_LATEX = {
    "×": r"\times", "÷": r"\div", "±": r"\pm", "∓": r"\mp",
    "≠": r"\neq", "≈": r"\approx", "≡": r"\equiv", "≤": r"\leq", "≥": r"\geq",
    "∝": r"\propto", "∞": r"\infty",
    "∫": r"\int", "∬": r"\iint", "∭": r"\iiint", "∮": r"\oint",
    "∇": r"\nabla", "∂": r"\partial",
    "∏": r"\prod", "∑": r"\sum",
    "√": r"\sqrt", "∛": r"\sqrt[3]", "∜": r"\sqrt[4]",
    "∠": r"\angle", "⊥": r"\perp", "∥": r"\parallel",
    "∩": r"\cap", "∪": r"\cup",
    "⊂": r"\subset", "⊃": r"\supset", "⊆": r"\subseteq", "⊇": r"\supseteq",
    "∈": r"\in", "∉": r"\notin", "∅": r"\emptyset",
    "∧": r"\wedge", "∨": r"\vee", "⊕": r"\oplus", "⊗": r"\otimes",
    "·": r"\cdot", "⋯": r"\cdots", "…": r"\ldots",
    "π": r"\pi", "θ": r"\theta", "α": r"\alpha", "β": r"\beta", "γ": r"\gamma",
    "δ": r"\delta", "λ": r"\lambda", "μ": r"\mu", "σ": r"\sigma", "ω": r"\omega",
    "Δ": r"\Delta", "Σ": r"\Sigma", "Π": r"\Pi", "Ω": r"\Omega",
}

# Computer Modern's math extension font (CMEX10 and its size variants) reuses
# ASCII code points from a text font's encoding slot to store large "stretchy"
# operator glyphs -- e.g. the big integral sign lives at the same code as the
# letter 'Z', \sum at 'X'. When a PDF's ToUnicode CMap for that font is
# missing or generic, extractors read back the literal ASCII letter instead
# of the operator it actually draws. CMEX is only ever used for these
# operator/delimiter glyphs (never ordinary text), so remapping is safe as
# long as it's scoped to spans actually set in that font.
_CMEX10_BIG_OP_MAP = {"X": "∑", "Y": "∏", "Z": "∫", "I": "∮"}

# CMEX10 also stores the *pieces* used to build large, auto-sized delimiters
# (parentheses/brackets tall enough to enclose a fraction or a stacked
# expression) at codes in the C0 control range, which come back from
# extraction as literal control characters -- invisible, and previously
# stripped outright by sanitize_xml_text as invalid XML. Checked against a
# real test document: consecutive even/odd code pairs consistently bracket
# exactly the expression that needs enclosing (0x10/0x11 around "(n/e)" in
# one formula and, reused at a different size, around "(πs/2)" in
# another; 0x12/0x13 around "(∂u/∂t + u·∇u)";
# 0x14/0x15 around a "[...]" bracketed group) -- i.e. even = opening, odd =
# closing. There's no way to recover from the code alone whether the
# original was "(" ")" or "[" "]" or "{" "}", so this renders every such
# pair as parentheses: an approximation, but a large improvement over
# dropping the grouping information entirely.
def _cmex10_remap(ch):
    if ch in _CMEX10_BIG_OP_MAP:
        return _CMEX10_BIG_OP_MAP[ch]
    code = ord(ch)
    if code < 0x20:
        return "(" if code % 2 == 0 else ")"
    return ch


def _is_cmex_font(font_name):
    return "cmex" in (font_name or "").lower()


def _join_latex_parts(parts):
    """Join LaTeX fragments, inserting a space wherever omitting one would
    change the meaning: a LaTeX control word (\\times, \\sqrt, ...) is only
    terminated by the first non-letter character, so "\\times" + "a" must
    become "\\times a" (not "\\timesa", which LaTeX would parse as an
    unrecognized command \\timesa). No space is needed before a digit,
    symbol, or another command -- those already terminate the control word
    on their own.
    """
    out = ""
    for part in parts:
        if out and re.search(r'\\[a-zA-Z]+$', out) and re.match(r'[a-zA-Z]', part):
            out += " "
        out += part
    return out


_SQRT_RE = re.compile(r'\\sqrt(\[[34]\])?([(\[])')


def _fix_sqrt_grouping(latex):
    """\\sqrt only takes the next single token as its argument unless it is
    wrapped in {}; a parenthesized (or bracketed -- PyMuPDF text sometimes
    carries "[" ... "]" around a root instead of "(" ... ")") group like
    "\\sqrt(x+y)" is invalid (it would apply only to the literal "(" and
    leave ")" dangling outside the root, and a bare "[...]" right after
    \\sqrt is parsed as the root-degree argument, not a group). Convert
    whichever bracket the source used into the {} grouping LaTeX actually
    needs, recursively for nested roots.
    """
    out = []
    i = 0
    while True:
        m = _SQRT_RE.search(latex, i)
        if not m:
            out.append(latex[i:])
            break
        out.append(latex[i:m.start()])
        open_ch = m.group(2)
        close_ch = ')' if open_ch == '(' else ']'
        depth = 1
        j = m.end()
        while j < len(latex) and depth > 0:
            if latex[j] == open_ch:
                depth += 1
            elif latex[j] == close_ch:
                depth -= 1
            j += 1
        inner = _fix_sqrt_grouping(latex[m.end():j - 1])
        out.append(f"\\sqrt{m.group(1) or ''}{{{inner}}}")
        i = j
    return "".join(out)


def build_latex_from_spans(spans):
    """Build a LaTeX string directly from PDF text spans.

    PyMuPDF already reports each span's exact text, font size, vertical
    origin, and an explicit superscript flag. For the extremely common case
    of a Word/Google-Docs-generated PDF -- where "superscript"/"subscript"
    is really just a smaller, vertically-shifted run of the same font --
    that is a complete, deterministic, and free way to build the formula's
    LaTeX with no OCR involved at all. If a particular glyph's ToUnicode
    mapping is broken (rare, but seen with some math fonts' big-operator
    symbols), that character is simply missing from the result -- OCR could
    recover it but is too slow to be worth it for a narrow edge case. The
    resulting LaTeX is stored in a JATS <tex-math> element; MathML
    generation from it is a separate, later step.
    """
    text_spans = [s for s in spans if s["text"].strip()]
    if not text_spans:
        return None

    def latex_token(text):
        return _UNICODE_TO_LATEX.get(text, text)

    # Explode spans into the fine-grained tokens _MATH_TOKEN_RE finds (one
    # span's text can hold several, e.g. "iπ" -> "i", "π"), each still
    # carrying its ORIGINAL span's size/origin/flags/font -- classification
    # below always operates on a token's own span metadata, not on
    # whatever token happens to sit next to it.
    token_metas = []
    for s in text_spans:
        is_raw = bool(s.get("_raw_latex"))
        is_cmex = _is_cmex_font(s.get("font"))
        # id(s): one multi-letter span (e.g. a "µν" subscript) explodes into
        # several tokens below, and those must all still count as the ONE
        # span they came from when weighing sizes by how often each occurs
        # -- otherwise a two-letter sub/superscript can out-vote a
        # one-letter base run in a short line and get mistaken for the
        # "normal" size instead of the other way around.
        meta = {"size": s["size"], "y": s["origin"][1], "flags": s["flags"], "cmex": is_cmex, "span_id": id(s)}
        if is_raw:
            # Already a finished LaTeX fragment from build_math_substitution
            # (a whole \frac{}{} or \sum_{}^{} folded down to one span) --
            # tokenizing it character-by-character like ordinary text would
            # tear its backslash commands and braces apart.
            token_metas.append({**meta, "text": s["text"], "raw": True})
            continue
        clean_text = _INVISIBLE_CHARS_RE.sub("", s["text"])
        if is_cmex:
            clean_text = "".join(_cmex10_remap(ch) for ch in clean_text)
        for tok in _MATH_TOKEN_RE.findall(clean_text):
            token_metas.append({**meta, "text": tok, "raw": False})

    if not token_metas:
        return None

    def build(metas):
        """Group a flat run of tokens into nested sup/sub LaTeX, recursively.

        A run of consecutive sup (or sub) tokens can itself contain a
        further level of nesting -- e.g. in "e^{-x^2}", both "x" and "2"
        read as smaller-and-raised relative to "e"'s own baseline, so a
        single non-recursive pass lumps them into one flat exponent
        "e^{-x2}" instead of recognizing that "2" is itself an exponent of
        "x", one level deeper. Re-deriving a fresh base size/baseline from
        just the tokens handed to THIS call (rather than reusing the
        outermost call's) is what lets a nested call notice that "2" is
        smaller than "x" even though both were "smaller" relative to the
        top-level base -- recursing on every sup/sub run this way handles
        arbitrary nesting depth for free, one level of the recursion per
        level of nesting.
        """
        if not metas:
            return ""

        # CMEX10's stretchy operator/delimiter glyphs (see
        # _CMEX10_BIG_OP_MAP / _cmex10_remap) report wildly
        # non-representative sizes and origins for what is visually a
        # tall, multi-line-high symbol -- using one as the "normal
        # size"/baseline reference has produced a wrong sup/sub call for
        # an unrelated nearby glyph in real test documents. A synthetic
        # span built by build_math_substitution() (its own
        # already-finished LaTeX, not a font glyph at all) is equally
        # unrepresentative. Prefer whatever ordinary tokens remain once
        # both are excluded; only fall back to the full set when a run is
        # made up entirely of one or the other.
        sizing = [m for m in metas if not m["cmex"] and not m["raw"]] or metas
        # One vote per ORIGINAL SPAN, not per exploded token -- a span's
        # text can explode into several tokens above (a two-letter
        # subscript like "µν" becomes two), and counting each of those
        # separately would let a multi-letter sub/superscript outvote a
        # single-letter base run in a short line.
        span_sizes = {m["span_id"]: m["size"] for m in sizing}
        sizes = list(span_sizes.values())
        # Ties in count are broken toward the larger size: with exactly
        # two distinct sizes each appearing once (a short two-span run),
        # the bigger glyph is conventionally the base, not the exponent.
        base_size = max(set(sizes), key=lambda sz: (sizes.count(sz), sz))
        # Same one-vote-per-span reasoning applies to the baseline average:
        # a normal-sized span that explodes into several tokens (e.g. " +
        # Λ" -> " ", "+", " ", "Λ") would otherwise contribute that many
        # copies of its own y to the mean, outweighing a shorter
        # single-token span like "G" and skewing the baseline enough to
        # misjudge a real subscript right next to it as not lowered at all.
        span_ys = {m["span_id"]: m["y"] for m in sizing if m["size"] >= base_size * 0.85}
        baseline_y = sum(span_ys.values()) / len(span_ys) if span_ys else sizing[0]["y"]

        classified = []
        for m in metas:
            smaller = m["size"] < base_size * 0.85
            raised = m["y"] < baseline_y - 0.5
            lowered = m["y"] > baseline_y + 0.5
            # PyMuPDF's own superscript flag (bit 0) is unreliable on its
            # own -- a full-size glyph that merely extends upward from its
            # baseline (e.g. a "√" radical sign) sometimes gets it set
            # too, which wrongly turned "= √π" into "=^{√π}" for one sqrt
            # in a real test document while an identical-looking sqrt
            # right next to it (flag unset) rendered fine. A true
            # superscript is by definition smaller text, so require that
            # in every case and use the flag only to confirm which
            # direction (rather than trusting it outright), matching how
            # the smaller+lowered case already works.
            if smaller and (raised or (m["flags"] & 1)):
                kind = "sup"
            elif smaller and lowered:
                kind = "sub"
            else:
                kind = "normal"
            classified.append((kind, m))

        parts = []
        i, n = 0, len(classified)
        while i < n:
            kind, m = classified[i]
            if kind == "normal" or not parts:
                # A sup/sub run with nothing preceding it has no base to
                # attach to -- keep it as plain text rather than losing it.
                parts.append(m["text"] if m["raw"] else latex_token(m["text"]))
                i += 1
                continue

            base = parts.pop()
            run = []
            while i < n and classified[i][0] == kind:
                run.append(classified[i][1])
                i += 1
            nested = build(run)
            marker = "^" if kind == "sup" else "_"
            parts.append(f"{base}{marker}{{{nested}}}")
        return _join_latex_parts(parts)

    latex = _fix_sqrt_grouping(build(token_metas))
    return latex if latex else None


_BIG_OP_UNICODE = {"∑", "∏", "∫", "∬", "∭", "∮"}


def _x_overlap_ratio(a0, a1, b0, b1):
    """Fraction of span [a0,a1)'s own width that falls inside [b0,b1)."""
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    width = (a1 - a0) or 1.0
    return inter / width


def _bbox_center_y(bbox):
    return (bbox[1] + bbox[3]) / 2.0


def _union_bbox(b1, b2):
    return (min(b1[0], b2[0]), min(b1[1], b2[1]), max(b1[2], b2[2]), max(b1[3], b2[3]))


def _spans_union_bbox(spans):
    bbox = spans[0]["bbox"]
    for s in spans[1:]:
        bbox = _union_bbox(bbox, s["bbox"])
    return bbox


def _bboxes_close(b1, b2, margin):
    """Symmetric version of the carry-continuation proximity test: does b2
    fall within b1 expanded by `margin` points in every direction (or vice
    versa -- the test is symmetric)."""
    x0, y0, x1, y1 = b1
    expanded = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
    return not (
        b2[2] < expanded[0] or b2[0] > expanded[2]
        or b2[3] < expanded[1] or b2[1] > expanded[3]
    )


_MATH_FONT_HINTS = ("math", "symbol", "cmmi", "cmsy", "cmex", "mt extra")


def _looks_mathy(block):
    """Cheap, block-level "is this substantially math" signal used only to
    decide which nearby blocks are eligible to be grouped into one reading-
    order island (see _group_by_proximity) -- a coarser, page-structure-
    level cousin of is_math_block's per-line classification. A block set
    substantially in a math font (the same signal is_math_block leans on)
    is math regardless of which particular symbols it contains, which
    matters here since a lone operator glyph or a bare limit (e.g. just
    "k=1") carries no `=`/Unicode-math-symbol density of its own."""
    spans = [s for line in block.get("lines", []) for s in line.get("spans", []) if s["text"].strip()]
    if not spans:
        return False
    total = sum(len(s["text"]) for s in spans)
    mathy = sum(
        len(s["text"]) for s in spans
        if _is_cmex_font(s.get("font")) or any(h in s.get("font", "").lower() for h in _MATH_FONT_HINTS)
    )
    return mathy / total > 0.3


def _group_by_proximity(blocks, margin):
    """Union-Find over `blocks` (each a dict with a "bbox" key), connecting
    two blocks whenever they're within `margin` points of each other (see
    _bboxes_close) AND at least one of the two looks mathy (_looks_mathy) --
    that second condition is what keeps two ordinary, merely-adjacent prose
    paragraphs from ever being grouped (and therefore reading-order-
    reshuffled) by this. Returns a list of groups, each a list of blocks;
    an isolated block with no mathy neighbor is its own group of one.
    """
    parent = {id(b): id(b) for b in blocks}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    mathy = {id(b): _looks_mathy(b) for b in blocks}
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            bi, bj = blocks[i], blocks[j]
            if not (mathy[id(bi)] or mathy[id(bj)]):
                continue
            if _bboxes_close(bi["bbox"], bj["bbox"], margin):
                union(id(bi), id(bj))

    groups = {}
    for b in blocks:
        groups.setdefault(find(id(b)), []).append(b)
    return list(groups.values())


def _block_ordering_bbox(block, span_to_cluster):
    """A block's bbox for reading-order purposes, built from only the
    spans that actually survive substitution: a dropped, non-anchor
    cluster member (e.g. a fraction's denominator, once its numerator's
    block has become the fraction's anchor) contributes nothing of its
    own, since after substitution there's no visible content left at its
    position for a sibling block to be "read after"; an anchor span
    contributes its whole cluster's bbox instead of just its own, since
    the substituted synthetic span it becomes stands in for the entire
    reconstructed construct (numerator AND denominator, operator AND
    limits, ...), reaching well past the anchor's own small bbox; an
    ordinary span just contributes its own bbox, as usual.

    Skipping dropped members matters as much as including anchors' full
    extent does: crediting a denominator's own position to its block (as a
    simpler "extend the raw bbox" version of this once did) can accidentally
    align that block's left edge with the fraction's numerator block, tying
    two blocks that must sort in a specific order for the surrounding
    formula to read correctly -- excluding it instead leaves that block's
    position resting on whatever OTHER, still-visible content it holds.
    """
    bbox = None
    for line in block.get("lines", []):
        for span in line.get("spans", []):
            cluster = span_to_cluster.get(id(span))
            if cluster is not None and id(span) != cluster["anchor_id"]:
                continue  # dropped by substitute() -- no surviving content here
            piece_bbox = cluster["bbox"] if cluster is not None else span["bbox"]
            bbox = piece_bbox if bbox is None else _union_bbox(bbox, piece_bbox)
    return bbox if bbox is not None else block["bbox"]


def _row_reading_order(blocks):
    """Order blocks (each a dict with a "bbox" key) the way a reader
    actually scans a tall, multi-piece display formula: greedily cluster
    blocks into horizontal "rows" wherever their y-ranges substantially
    overlap, then read row-by-row top-to-bottom and, within a row,
    left-to-right by x.

    This -- not a single global top-y sort -- is what a formula spliced
    across several separate PyMuPDF blocks actually needs: a big stretchy
    operator glyph (a summation/integral sign tall enough to hold its own
    stacked limits) is, geometrically, as tall as the whole row it sits in,
    so its raw bbox top sits far above where it visually belongs and a
    plain top-y sort keeps placing it before content that should precede
    it. Two blocks that are genuinely stacked (a numerator over a
    denominator, or a formula wrapped onto a second page-width line) don't
    y-overlap at all, so they still land in separate rows and keep their
    natural top-to-bottom order -- only content sharing one visual row
    gets reordered by x instead.
    """
    ordered = sorted(blocks, key=lambda b: _bbox_center_y(b["bbox"]))
    rows = []
    for b in ordered:
        bbox = b["bbox"]
        placed = False
        for row in rows:
            overlap = min(bbox[3], row["bbox"][3]) - max(bbox[1], row["bbox"][1])
            span = min(bbox[3] - bbox[1], row["bbox"][3] - row["bbox"][1]) or 1.0
            if overlap / span > 0.3:
                row["members"].append(b)
                row["bbox"] = _union_bbox(row["bbox"], bbox)
                placed = True
                break
        if not placed:
            rows.append({"bbox": bbox, "members": [b]})

    rows.sort(key=lambda r: r["bbox"][1])
    result = []
    for row in rows:
        row["members"].sort(key=lambda b: b["bbox"][0])
        result.extend(row["members"])
    return result


def _find_horizontal_bars(page, min_width=2.0, max_width=220.0):
    """A fraction bar is drawn as a thin horizontal vector stroke, not
    text -- PyMuPDF's text layer never reports it, so page.get_drawings()
    (vector paths) is the only place it shows up. A stray horizontal line
    narrower than a full-width page rule is almost certainly a division bar
    (or a \\sqrt's vinculum -- see _is_sqrt_vinculum, which tells the two
    apart)."""
    bars = []
    for d in page.get_drawings():
        for item in d.get("items", []):
            if item[0] != "l":
                continue
            p1, p2 = item[1], item[2]
            if abs(p1.y - p2.y) > 0.75:
                continue
            width = abs(p2.x - p1.x)
            if width < min_width or width > max_width:
                continue
            x0, x1 = sorted((p1.x, p2.x))
            bars.append((x0, x1, (p1.y + p2.y) / 2.0))
    return bars


def _sqrt_glyph_for_vinculum(bar_x0, bar_y, all_spans):
    """Find the literal '√' glyph a horizontal bar belongs to, if any. A
    \\sqrt sign's top bar is geometrically identical to a fraction's division
    bar -- both are thin horizontal strokes -- but must not be reconstructed
    as one. It's always drawn immediately to the right of a literal '√'
    glyph at roughly the bar's own height; a real fraction bar never is (its
    numerator sits above it, not a radical sign beside it)."""
    for s in all_spans:
        if s["text"].strip() != "√":
            continue
        sx0, sy0, sx1, sy1 = s["bbox"]
        if abs(sx1 - bar_x0) < 3 and sy0 - 3 <= bar_y <= sy1 + 3:
            return s
    return None


def _is_sqrt_vinculum(bar_x0, bar_y, all_spans):
    return _sqrt_glyph_for_vinculum(bar_x0, bar_y, all_spans) is not None


def _flatten_text_spans(blocks):
    spans = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in dedupe_overlapping_lines(block.get("lines", [])):
            for span in dedupe_overlapping_spans(line.get("spans", [])):
                if span["text"].strip():
                    spans.append(span)
    return spans


def _find_fraction_clusters(page, all_spans, min_overlap=0.6, vreach=18.0):
    """Reconstruct \\frac{num}{den} from a fraction bar's geometry.

    PyMuPDF's text layer gives no structural link between a fraction's
    numerator and denominator -- they're just two lines of text that happen
    to sit above and below empty space, often even split into separate
    "blocks" if they don't otherwise touch anything. The bar itself (see
    _find_horizontal_bars) is the only signal tying them together: gather
    whatever text overlaps it horizontally, split by which side of it each
    piece sits on, and require a healthy overlap ratio (not just any
    overlap) so an adjacent "=" or enclosing "(" that merely brushes the
    bar's edge isn't swept in as part of the fraction.
    """
    clusters = []
    for bar_x0, bar_x1, bar_y in _find_horizontal_bars(page):
        if _is_sqrt_vinculum(bar_x0, bar_y, all_spans):
            continue

        numerator, denominator = [], []
        for s in all_spans:
            sx0, sy0, sx1, sy1 = s["bbox"]
            if _x_overlap_ratio(sx0, sx1, bar_x0 - 1, bar_x1 + 1) < min_overlap:
                continue
            if sy1 < bar_y - vreach or sy0 > bar_y + vreach:
                continue
            (numerator if _bbox_center_y(s["bbox"]) < bar_y else denominator).append(s)

        if not numerator or not denominator:
            continue

        numerator.sort(key=lambda s: s["bbox"][0])
        denominator.sort(key=lambda s: s["bbox"][0])
        num_latex = build_latex_from_spans(numerator)
        den_latex = build_latex_from_spans(denominator)
        if not num_latex or not den_latex:
            continue

        # The anchor decides both where the whole \frac{}{} is spliced into
        # the surrounding line's left-to-right token stream, and what
        # size/origin the synthetic span reports to that line's own sup/sub
        # classifier. It must be a full-size numerator member: the topmost
        # member is often instead a small superscript sitting inside the
        # numerator itself (e.g. the "2" in "π^2" over "6") -- anchoring
        # there would splice the fraction in after the numerator's own base
        # character and report a small, raised size, which then gets the
        # entire fraction misread as *itself* a superscript of whatever
        # precedes it on the line.
        anchor = max(numerator, key=lambda s: (s["size"], -s["bbox"][0]))
        clusters.append({
            "latex": f"\\frac{{{num_latex}}}{{{den_latex}}}",
            "member_ids": {id(s) for s in numerator + denominator},
            "anchor_id": id(anchor),
            "size": anchor["size"],
            "origin": anchor["origin"],
            "bbox": _spans_union_bbox(numerator + denominator),
        })
    return clusters


def _find_sqrt_clusters(page, all_spans, min_overlap=0.6, vreach=18.0):
    """Reconstruct \\sqrt{radicand} from a radical sign's vinculum bar.

    A \\sqrt sign's radicand is, geometrically, exactly like a fraction's
    numerator: text sitting just under a thin horizontal bar (see
    _find_horizontal_bars / _sqrt_glyph_for_vinculum). Without this, a
    radical drawn as a lone '√' glyph next to its own text -- rather than
    the '√' Unicode character butted directly against its argument in the
    same span -- has no way to recover what it applies to, and the
    radicand is left to fall through as unrelated, ungrouped text.
    """
    clusters = []
    for bar_x0, bar_x1, bar_y in _find_horizontal_bars(page):
        sqrt_span = _sqrt_glyph_for_vinculum(bar_x0, bar_y, all_spans)
        if sqrt_span is None:
            continue

        radicand = []
        for s in all_spans:
            if s is sqrt_span:
                continue
            sx0, sy0, sx1, sy1 = s["bbox"]
            if _x_overlap_ratio(sx0, sx1, bar_x0 - 1, bar_x1 + 1) < min_overlap:
                continue
            if sy0 < bar_y - 1 or sy0 > bar_y + vreach:
                continue
            radicand.append(s)

        if not radicand:
            continue

        radicand.sort(key=lambda s: s["bbox"][0])
        rad_latex = build_latex_from_spans(radicand)
        if not rad_latex:
            continue

        clusters.append({
            "latex": f"\\sqrt{{{rad_latex}}}",
            "member_ids": {id(sqrt_span)} | {id(s) for s in radicand},
            "anchor_id": id(sqrt_span),
            "size": sqrt_span["size"],
            "origin": sqrt_span["origin"],
            "bbox": _spans_union_bbox([sqrt_span] + radicand),
        })
    return clusters


def _find_operator_limit_clusters(all_spans, touch_margin=3.0, reach=4.0):
    """Reconstruct \\sum_{..}^{..} / \\int_{..}^{..} / \\oint_{..} from a
    big-operator glyph's geometry.

    A \\sum or \\prod's limits are typeset stacked directly above/below the
    glyph (heavy horizontal overlap with it); an \\int or \\oint's are set
    to its upper-/lower-right instead (touching its right edge, with no
    overlap at all). Both conventions are checked -- PyMuPDF gives no other
    way to tell a limit apart from unrelated nearby text (an integrand, a
    following "=") than this geometry, and without it these come through as
    flat, meaningless concatenation instead of actual bounds.
    """
    clusters = []
    op_spans = [
        s for s in all_spans
        if _is_cmex_font(s.get("font")) and len(s["text"]) == 1
        and _CMEX10_BIG_OP_MAP.get(s["text"]) in _BIG_OP_UNICODE
    ]
    for op in op_spans:
        ox0, oy0, ox1, oy1 = op["bbox"]
        op_cy = _bbox_center_y(op["bbox"])
        upper, lower = [], []
        for s in all_spans:
            if s is op or not s["text"].strip():
                continue
            # A real limit is set in a visibly smaller font than the
            # operator itself -- even the \int/\oint corner convention
            # shrinks it -- which is what tells it apart from the operand
            # text immediately following the operator (e.g. the "f" that
            # starts \sum...f(x)'s summand): that text is full body size,
            # and without this check it can satisfy "touches_right" below
            # just as well as a genuine limit does, purely by sitting a
            # little above the operator's tall overall vertical center.
            if s["size"] >= op["size"] * 0.85:
                continue
            sx0, sy0, sx1, sy1 = s["bbox"]
            if sy1 < oy0 - reach or sy0 > oy1 + reach:
                continue
            touches_right = 0 <= (sx0 - ox1) <= touch_margin
            stacked = _x_overlap_ratio(sx0, sx1, ox0, ox1) > 0.5
            if not (touches_right or stacked):
                continue
            (upper if _bbox_center_y(s["bbox"]) < op_cy else lower).append(s)

        if not upper and not lower:
            continue

        upper.sort(key=lambda s: s["bbox"][0])
        lower.sort(key=lambda s: s["bbox"][0])
        upper_latex = build_latex_from_spans(upper) if upper else None
        lower_latex = build_latex_from_spans(lower) if lower else None

        op_unicode = _CMEX10_BIG_OP_MAP[op["text"]]
        latex = _UNICODE_TO_LATEX.get(op_unicode, op_unicode)
        if lower_latex:
            latex += f"_{{{lower_latex}}}"
        if upper_latex:
            latex += f"^{{{upper_latex}}}"

        clusters.append({
            "latex": latex,
            "member_ids": {id(op)} | {id(s) for s in upper + lower},
            "anchor_id": id(op),  # only the operator's own span may render this cluster
            "size": op["size"],
            "origin": op["origin"],
            "bbox": _spans_union_bbox([op] + upper + lower),
        })
    return clusters


# Unicode's dedicated Spacing Modifier Letters -- never used as ordinary
# running-prose punctuation (unlike ASCII ^ / ~ / ' / `, which are e.g. in
# "it's" or code snippets) -- are what some PDF generators draw a math
# accent as: a standalone glyph placed just above its base character
# instead of a combining mark folded into the base's own span.
_ACCENT_CHAR_TO_LATEX = {
    "ˆ": "hat",    # ˆ MODIFIER LETTER CIRCUMFLEX ACCENT
    "˜": "tilde",  # ˜ SMALL TILDE
    "˙": "dot",    # ˙ DOT ABOVE
    "˝": "ddot",   # ˝ DOUBLE ACUTE ACCENT (closest to a diaeresis glyph)
    "¯": "bar",    # ¯ MACRON
}


def _find_accent_clusters(all_spans, max_gap=3.0, min_x_overlap=0.4):
    """Reconstruct \\hat{x} / \\tilde{x} / etc. from an accent glyph drawn as
    its own standalone span sitting just above (and often overlapping) its
    base character, rather than as a combining mark on the base's own span
    -- see _ACCENT_CHAR_TO_LATEX for why this is safe to look for anywhere.
    Left alone, the accent glyph renders as stray, orphaned text next to
    the formula instead of decorating its base character.
    """
    clusters = []
    for accent in all_spans:
        text = accent["text"].strip()
        if text not in _ACCENT_CHAR_TO_LATEX:
            continue
        ax0, ay0, ax1, ay1 = accent["bbox"]

        best, best_overlap = None, 0.0
        for s in all_spans:
            if s is accent or s["text"].strip() in _ACCENT_CHAR_TO_LATEX:
                continue
            sx0, sy0, sx1, sy1 = s["bbox"]
            # The base sits at (or a little below) the accent's own row --
            # never above it -- and the accent's baseline must be raised
            # relative to the base's, matching how an accent actually sits.
            if sy0 < ay0 - 1 or sy0 - ay1 > max_gap:
                continue
            if s["origin"][1] <= accent["origin"][1]:
                continue  # base's baseline must sit lower than the accent's
            overlap = _x_overlap_ratio(ax0, ax1, sx0, sx1)
            if overlap < min_x_overlap or overlap <= best_overlap:
                continue
            best, best_overlap = s, overlap

        if best is None:
            continue
        base_text = best["text"].strip()
        if len(base_text) != 1 or not base_text.isalpha():
            continue  # only wrap a single base letter -- ambiguous otherwise

        latex_cmd = _ACCENT_CHAR_TO_LATEX[text]
        clusters.append({
            "latex": f"\\{latex_cmd}{{{base_text}}}",
            "member_ids": {id(accent), id(best)},
            "anchor_id": id(best),
            "size": best["size"],
            "origin": best["origin"],
            "bbox": _spans_union_bbox([accent, best]),
        })
    return clusters


def build_math_substitution(page, blocks):
    """Precompute this page's fraction, sqrt, big-operator/limit, and accent
    reconstructions, and return (substitute, span_to_cluster): a function
    that rewrites a line's spans accordingly, and the span-id -> cluster
    lookup itself (each cluster carries a "bbox" spanning every one of its
    members -- e.g. a fraction's numerator AND denominator together --
    used by the caller to treat a cluster's pieces, wherever they landed in
    separate PyMuPDF blocks, as one unit for reading-order purposes; see
    _block_ordering_bbox / _group_by_proximity / _row_reading_order).
    Every span belonging to one of these clusters is either dropped by
    substitute() or, at exactly one point, replaced by a single synthetic
    span carrying the cluster's finished LaTeX (flagged "_raw_latex" so
    build_latex_from_spans treats it as one already-built token instead of
    re-tokenizing it as text -- see build_latex_from_spans). Doing this
    once per page and reusing it for every line drawn from that same
    get_text('dict') call is what lets membership be checked by span
    identity (id()) instead of matching on (fragile, ambiguous)
    text/position.

    Every cluster names one fixed "anchor" member (see _find_fraction_clusters
    / _find_sqrt_clusters / _find_operator_limit_clusters /
    _find_accent_clusters) that alone renders its finished LaTeX;
    every other member is dropped, silently, wherever it's encountered. That
    per-cluster choice is fixed once, up front, rather than "whichever member
    a call to substitute() happens to reach first" -- this same page's lines
    get walked more than once (block text for heading detection, then again
    to actually build the formula), and dynamic first-wins emission would
    render the cluster on the first walk and then, correctly remembering it
    as already emitted, drop it as invisible on every walk after that.
    """
    all_spans = _flatten_text_spans(blocks)
    clusters = (
        _find_fraction_clusters(page, all_spans)
        + _find_sqrt_clusters(page, all_spans)
        + _find_operator_limit_clusters(all_spans)
        + _find_accent_clusters(all_spans)
    )

    span_to_cluster = {}
    for cluster in clusters:
        for sid in cluster["member_ids"]:
            span_to_cluster.setdefault(sid, cluster)

    def substitute(spans):
        out = []
        for s in spans:
            cluster = span_to_cluster.get(id(s))
            if cluster is None:
                out.append(s)
                continue
            if id(s) != cluster["anchor_id"]:
                continue  # a non-anchor member: folded into the anchor's synthetic span
            out.append({
                "text": cluster["latex"],
                "font": "CMEX-synthetic",
                "size": cluster["size"],
                "origin": cluster["origin"],
                "flags": 0,
                "color": s.get("color", 0),
                "bbox": s["bbox"],
                "_raw_latex": True,
            })
        return out

    return substitute, span_to_cluster


def sanitize_xml_text(text):
    """Remove control characters that are invalid in XML 1.0."""
    if not isinstance(text, str):
        return text
    # Valid XML 1.0 chars: #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]
    # We remove anything not in these ranges.
    _illegal_unichrs = [
        (0x00, 0x08), (0x0B, 0x0C), (0x0E, 0x1F), 
        (0x7F, 0x84), (0x86, 0x9F), 
        (0xFDD0, 0xFDDF), (0xFFFE, 0xFFFF)
    ]
    if sys.maxunicode >= 0x10000:  # not narrow build
        _illegal_unichrs.extend([(0x1FFFE, 0x1FFFF), (0x2FFFE, 0x2FFFF), 
                                 (0x3FFFE, 0x3FFFF), (0x4FFFE, 0x4FFFF), 
                                 (0x5FFFE, 0x5FFFF), (0x6FFFE, 0x6FFFF), 
                                 (0x7FFFE, 0x7FFFF), (0x8FFFE, 0x8FFFF), 
                                 (0x9FFFE, 0x9FFFF), (0xAFFFE, 0xAFFFF), 
                                 (0xBFFFE, 0xBFFFF), (0xCFFFE, 0xCFFFF), 
                                 (0xDFFFE, 0xDFFFF), (0xEFFFE, 0xEFFFF), 
                                 (0xFFFFE, 0xFFFFF), (0x10FFFE, 0x10FFFF)])

    illegal_ranges = [f"{chr(low)}-{chr(high)}" for (low, high) in _illegal_unichrs]
    illegal_xml_chars_RE = re.compile(f"[{''.join(illegal_ranges)}]")
    return illegal_xml_chars_RE.sub("", text)


_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")


def _encode_non_ascii_as_ncr(xml_text):
    """Re-encode every non-ASCII character in the final, already-serialized
    XML string as an uppercase-hex numeric character reference (&#xHHHH;)
    -- Greek letters, math italic letters, curly quotes, arrows, dashes,
    everything. This is the convention real publisher JATS deliverables
    (e.g. ACM's own) use throughout, prose and MathML content alike, rather
    than shipping literal UTF-8 for anything past ASCII: the file's bytes
    stay plain ASCII no matter what a downstream tool's own encoding
    handling does with the rest of the pipeline it travels through, and a
    specific character can be found by grepping the raw XML for its code
    point instead of pasting the glyph itself. Applied once to the fully
    assembled string rather than at each text-insertion site so it can't
    miss a spot and so it runs exactly once (no risk of a "&amp;" produced
    by an earlier XML-escaping pass being mistaken for un-encoded text --
    that string is pure ASCII already and this regex never touches it).
    """
    # Zero-padded to a minimum of 4 hex digits (matching the \uXXXX
    # convention) for anything in the Basic Multilingual Plane -- e.g.
    # U+00A9 becomes &#x00A9;, not the equally-valid-but-nonstandard
    # &#xA9; -- and left unpadded past 4 digits for an astral character
    # (e.g. a mathematical italic letter) that genuinely needs more.
    return _NON_ASCII_RE.sub(lambda m: f"&#x{ord(m.group()):04X};", xml_text)


def is_header_or_footer(block_bbox, page_rect):
    """Position-only margin-zone check, used for image blocks (logos etc.)
    where there's no text to confirm repetition against. Real body figures
    are rarely small enough to fit entirely inside this band, so a position
    check alone is reasonably safe for images."""
    y0 = block_bbox[1]
    y1 = block_bbox[3]
    height = page_rect.height

    # Top 12% and bottom 12% are considered header/footer margin.
    if y0 <= height * 0.12 or y1 >= height * 0.88:
        return True
    return False


def _header_footer_key(text, bbox, page_rect):
    """Normalized (zone, text) key for a block sitting in the header/footer
    margin. Digit runs are collapsed so a page-numbered footer like "Page 5
    of 26" matches "Page 6 of 26" on the next page as the same running
    footer."""
    height = page_rect.height
    y0, y1 = bbox[1], bbox[3]
    if y0 <= height * 0.12:
        zone = "top"
    elif y1 >= height * 0.88:
        zone = "bottom"
    else:
        return None

    normalized = re.sub(r'\d+', '#', text.strip().lower())
    normalized = re.sub(r'\s+', ' ', normalized)
    return (zone, normalized) if normalized else None


def find_repeating_header_footer_keys(doc):
    """Two-pass detection of running headers/footers: a block in the page
    margin whose (digit-normalized) text repeats across a good fraction of
    pages is treated as a real running header/footer; a one-off block that
    merely happens to sit in that margin (e.g. a title on a page with a
    small top margin) is left alone. A single fixed percentage-band cutoff
    can't tell these apart on its own, since it only looks at position.
    """
    if len(doc) < 2:
        return set()

    key_page_counts = {}
    for page in doc:
        page_rect = page.rect
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            text = "".join(
                span["text"] for line in dedupe_overlapping_lines(block.get("lines", [])) for span in line.get("spans", [])
            )
            key = _header_footer_key(text, block["bbox"], page_rect)
            if key:
                key_page_counts[key] = key_page_counts.get(key, 0) + 1

    min_repeats = max(2, len(doc) // 3)
    return {k for k, count in key_page_counts.items() if count >= min_repeats}


def _cell_texts(table):
    """Return the text in each detected cell without relying on table.extract()."""
    return [[(cell or "").strip() for cell in row] for row in table.extract()]


def is_structured_table(table, page_rect):
    """Distinguish data tables from borders / layout grids used to position prose.

    PDF table detectors operate on ruling lines, so a page-sized frame with a
    couple of merged cells can look like a table.  Such a grid is a layout
    container, not document data, and must not suppress the text blocks inside
    it.  A real table needs several populated rows and at least two populated
    columns.  Tables dominated by one long prose cell are treated as layout.
    """
    rows = _cell_texts(table)
    if len(rows) < 3:
        return False

    column_count = max((len(row) for row in rows), default=0)
    if column_count < 2:
        return False

    populated_rows = [row for row in rows if sum(bool(cell) for cell in row) >= 2]
    if len(populated_rows) < 3:
        return False

    populated_columns = sum(
        any(column < len(row) and row[column] for row in populated_rows)
        for column in range(column_count)
    )
    if populated_columns < 2:
        return False

    all_cells = [cell for row in rows for cell in row if cell]
    total_text = sum(len(cell) for cell in all_cells)
    longest_cell = max((len(cell) for cell in all_cells), default=0)

    # Real tabular data cells are short phrases/values. A single cell holding
    # this much running prose means the "table" is actually a layout grid --
    # e.g. rule lines from a drop-cap box, a chapter banner, or a two-column
    # page's column gutter -- wrapping ordinary body text, not real tabular
    # data. This check must NOT be gated on how much of the page the detected
    # grid covers: a false-positive grid over "only" 60% of a page is just as
    # much a layout artifact as one over 90%.
    if longest_cell > 300 or (total_text > 0 and longest_cell > total_text * 0.5):
        return False
    return True

def _attach_mathml(inline_formula, latex, opts, pending_mathml):
    """Queue this formula's LaTeX for the deterministic MathML pipeline
    (stages 6-13: normalize, validate, deterministic convert, and -- only if
    that wasn't good enough and the user opted in -- an LLM repair pass)
    instead of running it right here. A document can have 50-100+ formulas;
    resolving them one at a time, in the middle of the extraction walk,
    means any repair calls take as long as that many sequential network
    round-trips. Queuing them and resolving the whole batch concurrently
    afterwards (see _resolve_pending_mathml) turns minutes into seconds.
    The deterministic conversion itself is free and always runs -- opting in
    only controls whether shaky results get an LLM repair attempt on top.
    """
    pending_mathml.append((inline_formula, latex, bool(opts.get("latex_to_mathml"))))


def _resolve_one(item, log_callback=None):
    inline_formula, latex, repair_enabled = item

    normalized = normalize_latex(latex)
    ok, issues = validate_latex(normalized)
    mathml_el = deterministic_latex_to_mathml(normalized)
    confidence, needs_review = score_confidence(ok, issues)

    if repair_enabled and (needs_review or mathml_el is None):
        repair = repair_latex(normalized, issues=issues, log_callback=log_callback)
        if repair and repair.get("latex"):
            repaired = normalize_latex(repair["latex"])
            repaired_mathml = deterministic_latex_to_mathml(repaired)
            if repaired_mathml is not None:
                normalized = repaired
                mathml_el = repaired_mathml

    return inline_formula, normalized, mathml_el


def _resolve_pending_mathml(pending_mathml, log_callback=None):
    """Resolve every queued formula's LaTeX -> MathML concurrently: normalize,
    validate, deterministically convert, and (only for formulas that still
    look shaky and only if the user opted in) send it through the LLM repair
    engine. Runs once, after the whole document has been walked, so formulas
    found on page 1 and page 20 get resolved in the same batch instead of
    one-by-one. <math> is attached as a sibling when conversion
    succeeded -- at that point it's the only math representation kept:
    <tex-math> is dropped rather than left sitting next to it, since a JATS
    viewer with no real MathML template support (most naive XSLT-based
    ones) renders <tex-math> as plain visible text alongside its own
    garbled attempt at the MathML, showing the same formula twice, once as
    a raw, unrendered LaTeX string. Only when MathML conversion didn't
    succeed does <tex-math> remain, as the sole representation of that
    formula.
    """
    if not pending_mathml:
        return

    if log_callback:
        log_callback(f"Resolving MathML for {len(pending_mathml)} formula(s)...")

    def _convert(item):
        return _resolve_one(item, log_callback=log_callback)

    converted = 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        for inline_formula, normalized, mathml_el in executor.map(_convert, pending_mathml):
            tex_math = inline_formula.find("tex-math")
            if mathml_el is not None:
                inline_formula.append(mathml_el)
                converted += 1
                if tex_math is not None:
                    inline_formula.remove(tex_math)
            elif tex_math is not None and normalized:
                tex_math.text = sanitize_xml_text(normalized)

    if log_callback:
        log_callback(f"Converted {converted}/{len(pending_mathml)} formula(s) to MathML.")


DEFAULT_OPTIONS = {
    "ocr_math": True,          # detect math lines and build LaTeX directly from PDF text (no OCR)
    "detect_tables": True,     # detect and emit <table-wrap> for structured tables
    "strip_header_footer": True,  # drop repeating page headers/footers
    "latex_to_mathml": False,  # also send low-confidence formulas to an LLM (via OpenRouter) for repair; needs an API key. Deterministic MathML conversion always runs regardless of this flag.
}


def extract_pdf_to_xml(pdf_path, output_xml_path, log_callback=None, options=None):
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update(options)

    if log_callback:
        log_callback(f"Starting extraction for: {pdf_path}")

    pending_mathml = []  # (inline_formula element, latex) queued for one concurrent batch at the end

    doc = fitz.open(pdf_path)

    header_footer_keys = set()
    if opts["strip_header_footer"]:
        header_footer_keys = find_repeating_header_footer_keys(doc)
        if log_callback and header_footer_keys:
            log_callback(f"Detected {len(header_footer_keys)} repeating header/footer line(s) to strip.")

    # Create XML Root based on the JATS template
    root = ET.Element("article", {
        "article-type": "proceedings",
        "xmlns:xlink": "http://www.w3.org/1999/xlink",
        "xmlns:mml": "http://www.w3.org/1998/Math/MathML",
        "xmlns:oasis": "http://www.niso.org/standards/z39-96/ns/oasis-exchange/table"
    })
    
    front = ET.SubElement(root, "front")
    article_meta = ET.SubElement(front, "article-meta")
    title_group = ET.SubElement(article_meta, "title-group")
    article_title = ET.SubElement(title_group, "article-title")
    article_title.text = sanitize_xml_text(os.path.basename(pdf_path))
    
    body = ET.SubElement(root, "body")

    current_sec = None

    def finalize_formula(formula_el, parts):
        combined = " ".join(part for part in parts if part)
        formula_el.find("tex-math").text = combined
        _attach_mathml(formula_el, combined, opts, pending_mathml)

    # A display equation's pieces -- e.g. a big integral glyph on the left
    # of the line with its integrand to the right, or bounds/an exponent
    # stacked above/below -- sometimes come back from PyMuPDF as separate
    # blocks, not just separate lines within one block, and the split can
    # run in either direction (side-by-side on one baseline, or stacked
    # vertically). When a block ends with a (possibly still growing)
    # formula, carry_* holds it open instead of finalizing it, so an
    # immediately following block can continue the same inline-formula if
    # it too starts with math AND its bounding box is genuinely close to
    # the carried one (checked by expanding the carried box by a small
    # margin and testing for overlap, which catches both directions).
    # That geometry check matters -- without it, this would just as happily
    # glue together unrelated short "math-looking" blocks that happen to be
    # adjacent (chart axis-tick labels, a list of numbered examples,
    # scattered diagram node labels), which is worse than the fragmentation
    # it fixes. Anything that isn't a clean, nearby continuation (a table,
    # a heading, prose, a page boundary, or too far away) flushes it as-is.
    carry_p = carry_el = carry_bbox = None
    carry_parts = []

    _FORMULA_PROXIMITY = 20  # points

    def _continues_carry(next_bbox):
        if carry_bbox is None:
            return False
        return _bboxes_close(carry_bbox, next_bbox, _FORMULA_PROXIMITY)

    def flush_carry():
        nonlocal carry_p, carry_el, carry_parts, carry_bbox
        if carry_el is not None:
            finalize_formula(carry_el, carry_parts)
        carry_p = carry_el = carry_bbox = None
        carry_parts = []

    for page_num in range(len(doc)):
        if log_callback:
            log_callback(f"Processing page {page_num + 1}/{len(doc)}...")
            
        page = doc[page_num]
        page_rect = page.rect

        # Get tables
        tables = page.find_tables() if opts["detect_tables"] else None
        table_bboxes = []
        
        def hex_color(color_int):
            if color_int is None or color_int < 0:
                return None
            r, g, b = (color_int >> 16) & 0xFF, (color_int >> 8) & 0xFF, color_int & 0xFF
            # This converter does not capture cell/box fill colors, only text
            # color. A PDF often sets header/label text to white against a
            # colored fill (e.g. a table header band); without that fill,
            # white (or near-white) text would render invisibly against a
            # plain background, so fall back to the default (black) text
            # color instead of reproducing an illegible color.
            if r > 235 and g > 235 and b > 235:
                return None
            return f"#{color_int:06x}"
        
        def process_spans(parent, spans):
            # process_spans is called once per line, with multiple lines
            # accumulating into the same <p> (see the per-line loop below).
            # last_elem must therefore pick up wherever the previous line's
            # call left off -- an element's .text only ever renders before
            # ALL of its children, so if a later line's leading plain-text
            # run were written to parent.text instead of the last existing
            # child's .tail, it would render before every earlier line's
            # italic/bold runs, scrambling the paragraph's word order.
            last_elem = list(parent)[-1] if len(parent) else None
            for span in dedupe_overlapping_spans(spans):
                text = sanitize_xml_text(span["text"])
                if not text:
                    continue
                is_italic = bool(span["flags"] & 2)
                is_bold = bool(span["flags"] & 16)
                color = span["color"]
                hcolor = hex_color(color) if color != 0 else None
                styles = []
                if hcolor:
                    styles.append(f"color:{hcolor}")
                if is_italic or is_bold or styles:
                    outer_elem = ET.Element("styled-content") if styles else None
                    if outer_elem is not None:
                        outer_elem.set("style", ";".join(styles))
                    content_parent = outer_elem if outer_elem is not None else parent
                    if is_bold:
                        content_parent = ET.SubElement(content_parent, "bold")
                    if is_italic:
                        content_parent = ET.SubElement(content_parent, "italic")
                    content_parent.text = text
                    if outer_elem is None:
                        # A bare bold / italic element has already been
                        # appended to its parent by SubElement.
                        last_elem = content_parent
                        continue
                    parent.append(outer_elem)
                    last_elem = outer_elem
                else:
                    if last_elem is None:
                        parent.text = (parent.text or "") + text
                    else:
                        last_elem.tail = (last_elem.tail or "") + text
                        
        def intersect(b1, b2):
            return max(0, min(b1[2], b2[2]) - max(b1[0], b2[0])) * max(0, min(b1[3], b2[3]) - max(b1[1], b2[1])) > 0
            
        # Collect structured tables (skipping layout grids) without rendering
        # them yet -- each table's top y-coordinate is needed so it can be
        # interleaved with the surrounding paragraphs in reading order below,
        # instead of every table on the page being dumped before its text.
        page_tables = []
        for table in (tables.tables if tables is not None else []):
            if not is_structured_table(table, page_rect):
                if log_callback:
                    log_callback("Ignored a layout grid; extracting its contents as normal text.")
                continue
            table_bboxes.append(table.bbox)
            page_tables.append(table)

        # Get text blocks and image blocks
        blocks = page.get_text("dict")["blocks"]

        # Fractions and big-operator limits (sum/integral/oint bounds) are
        # invisible to a line-by-line reading of this same blocks list --
        # see build_math_substitution -- so they're detected once up front
        # and folded into single synthetic spans wherever their pieces
        # would otherwise have been read as unrelated, meaningless text.
        substitute_math_spans, span_to_cluster = build_math_substitution(page, blocks)

        page_blocks = []
        for block in blocks:
            bbox = block["bbox"]
            if opts["strip_header_footer"]:
                if block.get("type") == 0:
                    # Text: only strip margin text confirmed to repeat across
                    # pages, so a one-off heading that merely starts close to
                    # the top margin isn't mistaken for a running header.
                    text = "".join(
                        span["text"] for line in dedupe_overlapping_lines(block.get("lines", [])) for span in line.get("spans", [])
                    )
                    key = _header_footer_key(text, bbox, page_rect)
                    if key and key in header_footer_keys:
                        continue
                elif is_header_or_footer(bbox, page_rect):
                    # Images (logos etc.): position alone is a reasonably
                    # safe signal since real body figures rarely fit
                    # entirely inside the margin band.
                    continue

            # Skip if block is in any table
            in_table = False
            for t_bbox in table_bboxes:
                if intersect(bbox, t_bbox):
                    in_table = True
                    break
            if in_table:
                continue

            page_blocks.append(block)

        def _block_sort_top(block):
            # A block's raw bbox top is the min across every span in it, so
            # a single small superscript (e.g. the "2" in a formula's
            # "pi^2") can pull the whole block's sort position a few points
            # above a visually-lower block that starts with only
            # normal-sized text -- exactly the ambiguous case two blocks of
            # one multi-block display formula (e.g. a fraction's numerator
            # and, a few points further down, its "denominator = next
            # fraction's numerator" line) land in. Ignore spans well below
            # the block's own dominant size when computing its sort key;
            # same-row content (no such outlier) sorts identically to
            # before.
            spans = [s for line in block.get("lines", []) for s in line.get("spans", []) if s["text"].strip()]
            if not spans:
                return block["bbox"][1]
            sizes = [s["size"] for s in spans]
            dominant = max(set(sizes), key=sizes.count)
            normal_tops = [s["bbox"][1] for s in spans if s["size"] >= dominant * 0.85]
            top = min(normal_tops) if normal_tops else block["bbox"][1]
            # Two spans meant to sit at the same nominal height often differ
            # by a hair (e.g. 275.8661804... vs 275.8661499...) from
            # font-metric rounding -- real enough to break exact equality,
            # too small to mean anything. Round it away so a genuine tie
            # falls through to stable (original list) order instead of
            # being decided by that noise.
            return round(top, 1)

        # A tall, stretchy CMEX glyph (a summation/integral sign holding its
        # own stacked limits) reports a bbox reaching far above where it
        # visually belongs, which _block_sort_top's single scalar can't
        # tell apart from genuinely-earlier content -- that misordering is
        # what fragments and scrambles a formula split across several
        # PyMuPDF blocks. Group blocks that are geometrically close AND at
        # least one of which looks like math into "formula islands" (see
        # _group_by_proximity) and re-derive each island's internal order
        # via row-then-column reading order (_row_reading_order) instead of
        # trusting _block_sort_top for every member individually. Ordinary
        # prose is untouched -- two non-mathy blocks are never grouped, so
        # their relative order never changes. Image blocks are left out of
        # grouping entirely; nothing about their own placement is broken by
        # the misordering this fixes. Grouping and row-ordering both work
        # off each block's ordering bbox (its own bbox extended to cover
        # any cluster whose anchor it holds -- see _block_ordering_bbox),
        # not its raw bbox, via a shallow-copied proxy so the real block
        # object (looked up afterwards through "_original_block") is what
        # ultimately gets keyed into block_order_override.
        block_order_override = {}
        text_blocks_for_grouping = []
        for b in page_blocks:
            if b.get("type") != 0:
                continue
            proxy = dict(b)
            proxy["bbox"] = _block_ordering_bbox(b, span_to_cluster)
            proxy["_original_block"] = b
            text_blocks_for_grouping.append(proxy)

        for group in _group_by_proximity(text_blocks_for_grouping, _FORMULA_PROXIMITY):
            if len(group) < 2 or not any(_looks_mathy(b) for b in group):
                continue
            anchor = min(_block_sort_top(b["_original_block"]) for b in group)
            for idx, member in enumerate(_row_reading_order(group)):
                block_order_override[id(member["_original_block"])] = (anchor, idx)

        def _block_sort_key(block):
            return block_order_override.get(id(block), (_block_sort_top(block), 0))

        # Merge tables and text/image blocks into a single top-to-bottom
        # reading order (sorted by top y-coordinate) so a table renders
        # exactly where it sits between paragraphs.
        items = [((round(t.bbox[1], 1), 0), "table", t) for t in page_tables]
        items += [(_block_sort_key(b), "block", b) for b in page_blocks]
        items.sort(key=lambda entry: entry[0])

        for _, kind, item in items:
            if kind == "table":
                flush_carry()
                table = item
                parent = current_sec if current_sec is not None else body
                table_wrap = ET.SubElement(parent, "table-wrap")
                # JATS's HTML-compatible table model permits standard HTML
                # table attributes (cellpadding/cellspacing/width/style) to
                # pass straight through to whatever renders the XML. Without
                # them, cells default to zero padding and the table looks
                # cramped -- these give every JATS/HTML viewer some breathing
                # room by default, not just this app's own preview.
                table_el = ET.SubElement(table_wrap, "table", {
                    "border": "1",
                    "cellpadding": "8",
                    "cellspacing": "0",
                    "style": "width:100%; border-collapse:collapse;",
                })
                cell_style = "padding:8px 12px;"
                # Only trust an INTERNAL header: it's part of the table's own
                # detected row/ruling structure, so it's reliable. An
                # "external" header is just PyMuPDF's guess at whatever text
                # sits closest above the table -- for a sparse grid (e.g. a
                # diagram laid out as a table of mostly-empty cells), that is
                # often the tail of the preceding paragraph, chopped into
                # nonsense column fragments. That text is not part of
                # table_bboxes either, so it is already correctly emitted as
                # its own <p> elsewhere; rendering it again here would only
                # duplicate it, and corrupted.
                if table.header and table.header.cells and not table.header.external:
                    thead = ET.SubElement(table_el, "thead")
                    tr = ET.SubElement(thead, "tr")
                    for cell_bbox in table.header.cells:
                        if cell_bbox is None: continue
                        th = ET.SubElement(tr, "th", {"style": cell_style + " text-align:left;"})
                        cell_dict = page.get_text("dict", clip=cell_bbox)
                        for b in cell_dict.get("blocks", []):
                            if b.get("type") == 0:
                                for l in dedupe_overlapping_lines(b.get("lines", [])):
                                    process_spans(th, l.get("spans", []))

                tbody = ET.SubElement(table_el, "tbody")
                for row in table.rows:
                    tr = ET.SubElement(tbody, "tr")
                    for cell_bbox in row.cells:
                        td = ET.SubElement(tr, "td", {"style": cell_style})
                        if cell_bbox is not None:
                            cell_dict = page.get_text("dict", clip=cell_bbox)
                            for b in cell_dict.get("blocks", []):
                                if b.get("type") == 0:
                                    for l in dedupe_overlapping_lines(b.get("lines", [])):
                                        process_spans(td, l.get("spans", []))
                continue

            block = item
            bbox = block["bbox"]
            if block["type"] == 0:  # Text block
                block_text = ""
                for line in dedupe_overlapping_lines(block.get("lines", [])):
                    for span in substitute_math_spans(dedupe_overlapping_spans(line.get("spans", []))):
                        block_text += span["text"] + " "

                block_text = block_text.strip()
                if not block_text:
                    continue

                # Math detection heuristic. Applied per-line (see below) so a
                # stack of independent short equations doesn't get diluted by
                # being averaged together as one block.
                def is_math_block(text, spans):
                    if not spans or not text:
                        return False

                    # Algorithmic pseudocode ("do if a −< s[m]", "return s[m]
                    # and n − m") mixes structural keywords with math-italic
                    # variables that are typically set in the very math fonts
                    # checked below, so the font-ratio and short-line checks
                    # both misfire and classify these as pure math. That is
                    # doubly bad: it not only routes plain prose into a
                    # <disp-formula>, but build_latex_from_spans's tokenizer
                    # drops whitespace between tokens, so "do if" glues into
                    # "doif". Treat a line containing any of these reserved
                    # words as prose so it goes through normal paragraph
                    # extraction, which preserves real spacing.
                    pseudocode_keywords = {
                        "do", "if", "then", "else", "for", "while", "return", "case", "let"
                    }
                    words_lower = set(w.lower() for w in re.findall(r"[A-Za-z]+", text))
                    if words_lower & pseudocode_keywords:
                        return False

                    math_fonts = ['Math', 'Symbol', 'CMMI', 'CMSY', 'CMEX', 'Cambria Math', 'MT Extra']
                    total_chars = sum(len(s["text"]) for s in spans) or 1
                    math_font_chars = sum(
                        len(s["text"]) for s in spans
                        if any(mf.lower() in s["font"].lower() for mf in math_fonts)
                    )
                    # Only treat as math if a real portion of the block is set in a math font,
                    # not just a single stray glyph (e.g. a bullet or symbol font used for icons).
                    if math_font_chars / total_chars > 0.3:
                        return True

                    math_chars = set('=+≠≈≡≤≥±∓×÷∝∞∫∬∭∮∇∂∆∏∑√∛∜∠∡∢⊥∥∩∪⊂⊃⊆⊇∈∉∅∧∨⊕⊗')
                    math_char_count = sum(1 for c in text if c in math_chars)
                    # U+FFFD shows up when PyMuPDF can't map a glyph to a Unicode codepoint,
                    # which is a strong signal of an embedded math font that '?' is not.
                    unmapped_count = text.count('�')

                    # Ratio-based only: ordinary prose that happens to contain a couple of
                    # "=", "+", "×" characters (e.g. "T+1 settlement") must not qualify.
                    if math_char_count / len(text) > 0.08 or unmapped_count / len(text) > 0.1:
                        return True

                    # Catches equations built almost entirely from single-letter
                    # variables, digits and ASCII operators (e.g. "P(A | B) =
                    # P(B | A)P(A) / P(B)"), which have too few Unicode math
                    # symbols to trip the ratio check above. Real prose sentences
                    # are made of actual multi-letter words, so requiring at most
                    # one word longer than 3 letters (function names like "sin"/
                    # "log" are exactly 3) keeps this from firing on a sentence
                    # that merely contains a stray "=".
                    if len(text) < 150 and ('=' in text or math_char_count > 0):
                        words = re.findall(r'[A-Za-z]+', text)
                        long_words = [w for w in words if len(w) > 3]
                        if len(long_words) <= 1:
                            return True
                    return False

                # Reconstruct full block text and collect spans to check for headings
                all_spans = []
                for line in dedupe_overlapping_lines(block.get("lines", [])):
                    all_spans.extend(substitute_math_spans(dedupe_overlapping_spans(line.get("spans", []))))

                total_chars = sum(len(s["text"]) for s in all_spans) or 1
                bold_chars = sum(len(s["text"]) for s in all_spans if s["flags"] & 16)
                bold_ratio = bold_chars / total_chars

                # A real heading is either ALL CAPS, or a short line that is *entirely*
                # (or almost entirely) bold. A numbered clause like "4.3.12 The bidder's
                # solution shall be..." only has its number in bold, so bold_ratio stays
                # low and it correctly falls through to the normal paragraph branch below,
                # where process_spans keeps the bold number and normal prose as separate
                # runs inside the same <p>. The isupper() check requires a real run of 2+
                # uppercase letters so a formula built from lone uppercase variables (e.g.
                # "P(A | B) = P(B | A)P(A) / P(B)") -- which is trivially "all uppercase"
                # since every cased character in it happens to be a capital letter --
                # doesn't get mistaken for a heading.
                is_heading = len(block_text) < 100 and (
                    (block_text.isupper() and re.search(r'[A-Z]{2,}', block_text))
                    or (bold_ratio > 0.8 and re.match(r'^\d+(\.\d+)*[\.\s]', block_text))
                )

                if is_heading:
                    flush_carry()
                    current_sec = ET.SubElement(body, "sec")
                    title = ET.SubElement(current_sec, "title")
                    title.text = sanitize_xml_text(block_text)
                    continue

                # Walk the block line by line instead of dumping every line into
                # one paragraph. A line detected as math becomes an
                # <inline-formula> inside the same, still-open <p> instead of
                # closing it -- unlike a <disp-formula> (a block element),
                # this keeps a run of pseudocode/prose lines that happen to
                # contain a formula in one continuous paragraph rather than
                # fragmenting it into many small out-of-order-looking pieces.
                parent = current_sec if current_sec is not None else body
                current_p = None
                prev_line_text = ""
                active_formula = None
                active_parts = []
                active_bbox = None

                def ensure_space(p, prev_text, next_text):
                    # PyMuPDF line boundaries don't carry the space a
                    # justified paragraph visually has at the wrap point, so
                    # without this two wrapped lines glue together as one
                    # word (e.g. "of xs(longest"). Skip it after a hyphen (a
                    # broken word, not a real word boundary) or around
                    # bracket/quote pairs, where no space belongs either.
                    if not (
                        prev_text
                        and prev_text[-1] not in "-([{“‘"
                        and next_text[:1] not in ").,;:!?’”»-"
                    ):
                        return
                    last_elem = list(p)[-1] if len(p) else None
                    if last_elem is not None:
                        last_elem.tail = (last_elem.tail or "") + " "
                    else:
                        p.text = (p.text or "") + " "

                for line in dedupe_overlapping_lines(block.get("lines", [])):
                    line_spans = substitute_math_spans(dedupe_overlapping_spans(line.get("spans", [])))
                    line_text = "".join(s["text"] for s in line_spans).strip()
                    if not line_text:
                        continue

                    is_math_line = opts["ocr_math"] and is_math_block(line_text, line_spans)
                    latex = sanitize_xml_text(build_latex_from_spans(line_spans)) if is_math_line else None

                    if current_p is None:
                        if latex and carry_el is not None and _continues_carry(bbox):
                            # This block opens with math, sits right under a
                            # still-open formula left dangling by the
                            # previous block, and overlaps it horizontally
                            # -- e.g. an integral sign in one block, its
                            # bounds and integrand in the next. Continue it
                            # seamlessly instead of starting a new one.
                            current_p, active_formula, active_parts = carry_p, carry_el, carry_parts
                            # The carry's OWN bbox, not just this new block's,
                            # since a formula's full extent-so-far is what the
                            # *next* continuation should be checked against --
                            # matching only against the most recently attached
                            # piece missed a further piece that was near the
                            # formula overall but a little too far from that
                            # one piece specifically (e.g. a big integral sign
                            # near the formula's start, but just outside reach
                            # of a fraction attached after it).
                            active_bbox = _union_bbox(carry_bbox, bbox)
                            carry_p = carry_el = carry_bbox = None
                            carry_parts = []
                        else:
                            flush_carry()
                            current_p = ET.SubElement(parent, "p")

                    if latex:
                        # Built directly from the PDF's own text -- exact,
                        # deterministic, and free. If a glyph's ToUnicode
                        # mapping was broken (rare, but seen with some math
                        # fonts' big-operator symbols), that character is
                        # simply missing from the result rather than
                        # attempted via OCR, which is slow and not always
                        # reliable either. The LaTeX goes into a JATS
                        # <tex-math> element; MathML generation from it is a
                        # separate, later step (see finalize_formula). A run
                        # of consecutive math lines joins into one
                        # inline-formula rather than fragmenting into several.
                        if active_formula is None:
                            if prev_line_text:
                                ensure_space(current_p, prev_line_text, line_text)
                            active_formula = ET.SubElement(current_p, "inline-formula")
                            ET.SubElement(active_formula, "tex-math")
                            active_parts = [latex]
                            active_bbox = bbox
                        else:
                            active_parts.append(latex)
                        if log_callback:
                            log_callback("Detected math line, built LaTeX directly from PDF text.")
                    else:
                        # Either not math, or math but nothing usable could
                        # be built from it (e.g. a line made up entirely of
                        # broken-font glyphs) -- fall through to plain text
                        # rather than losing the line.
                        if active_formula is not None:
                            finalize_formula(active_formula, active_parts)
                            active_formula = None
                            active_parts = []
                            active_bbox = None
                        if prev_line_text:
                            ensure_space(current_p, prev_line_text, line_text)
                        process_spans(current_p, line_spans)

                    prev_line_text = line_text

                if active_formula is not None:
                    # This formula is the last thing the block added -- hold
                    # off finalizing so an immediately following, nearby
                    # math block can continue it (e.g. a numbered label +
                    # integral sign in one block, its bounds and integrand
                    # in the next). flush_carry() finalizes it as-is the
                    # moment anything else intervenes, or nothing turns out
                    # to be close enough underneath it (_continues_carry).
                    carry_p, carry_el, carry_parts, carry_bbox = current_p, active_formula, active_parts, active_bbox

            elif block["type"] == 1:  # Image block
                # A genuinely embedded equation image (no text layer at all)
                # has no LaTeX to offer without OCR, which this tool
                # deliberately doesn't use (slow, and not reliable enough to
                # be worth the wait -- see build_latex_from_spans for the
                # text-layer path that handles the vast majority of
                # formulas). Represent it the same as any other image: a
                # <fig> placeholder the user can match up with the PDF.
                flush_carry()
                parent = current_sec if current_sec is not None else body
                fig = ET.SubElement(parent, "fig")
                graphic = ET.SubElement(fig, "graphic")
                graphic.set("xlink:href", f"image_p{page_num+1}_{int(bbox[0])}_{int(bbox[1])}.jpg")
                if log_callback:
                    log_callback("Found image, inserted placeholder.")

        flush_carry()  # don't let a formula continuation span a page break

    _resolve_pending_mathml(pending_mathml, log_callback)

    # Pretty print and save
    xmlstr = minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(indent="  ")

    # The xmlstr is already valid XML, we do not want to unescape &lt; and &gt; as that breaks XML parsing.

    # Numeric-character-reference-encode every non-ASCII symbol (prose
    # punctuation, Greek letters, MathML content alike) -- see
    # _encode_non_ascii_as_ncr for why. Done last, on the fully assembled
    # string, so it's unconditional and exhaustive.
    xmlstr = _encode_non_ascii_as_ncr(xmlstr)

    with open(output_xml_path, "w", encoding="utf-8") as f:
        # Add JATS doctype
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<!DOCTYPE article PUBLIC "-//NLM//DTD JATS (Z39.96) Journal Publishing DTD with OASIS Tables v1.0 20120330//EN" "JATS-journalpublishing-oasis-article1.dtd">\n')
        f.write(xmlstr.split('?>', 1)[-1].strip())
        
    if log_callback:
        log_callback(f"Successfully saved XML to: {output_xml_path}")

if __name__ == "__main__":
    # Test
    pass
