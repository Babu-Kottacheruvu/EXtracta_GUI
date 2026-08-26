import fitz  # PyMuPDF
import xml.etree.ElementTree as ET
from xml.dom import minidom
import os
import re
import sys
from math_extractor import get_latex_from_image


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


def spans_text_reliable(spans):
    """False when PyMuPDF couldn't map enough glyphs to real Unicode
    codepoints (shows up as U+FFFD), meaning the extracted text itself
    can't be trusted -- e.g. an embedded custom math font with a broken
    ToUnicode CMap. In that case image OCR is the only option left."""
    text = "".join(s["text"] for s in spans)
    if not text.strip():
        return False
    unmapped = text.count('�')
    return unmapped / max(len(text), 1) < 0.15


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
    """Build a LaTeX string directly from PDF text spans instead of
    rasterizing the line and running it through OCR.

    PyMuPDF already reports each span's exact text, font size, vertical
    origin, and an explicit superscript flag. For the extremely common case
    of a Word/Google-Docs-generated PDF -- where "superscript"/"subscript"
    is really just a smaller, vertically-shifted run of the same font --
    that is strictly more reliable ground truth than asking a vision model
    (PaddleOCR-VL) to re-read a picture of text we already have perfectly.
    This keeps math conversion fully offline/free and sidesteps OCR's
    accuracy limits entirely for this common case. The resulting LaTeX is
    stored in a JATS <tex-math> element; MathML generation from it is a
    separate, later step.
    """
    text_spans = [s for s in spans if s["text"].strip()]
    if not text_spans:
        return None

    sizes = [s["size"] for s in text_spans]
    base_size = max(set(sizes), key=sizes.count)
    normal_ys = [s["origin"][1] for s in text_spans if s["size"] >= base_size * 0.85]
    baseline_y = sum(normal_ys) / len(normal_ys) if normal_ys else text_spans[0]["origin"][1]

    tokens = []  # list of (kind, text) where kind is "normal" / "sup" / "sub"
    for s in text_spans:
        smaller = s["size"] < base_size * 0.85
        raised = s["origin"][1] < baseline_y - 0.5
        lowered = s["origin"][1] > baseline_y + 0.5
        if (s["flags"] & 1) or (smaller and raised):
            kind = "sup"
        elif smaller and lowered:
            kind = "sub"
        else:
            kind = "normal"
        for tok in _MATH_TOKEN_RE.findall(s["text"]):
            tokens.append((kind, tok))

    if not tokens:
        return None

    def latex_token(text):
        return _UNICODE_TO_LATEX.get(text, text)

    parts = []
    i, n = 0, len(tokens)
    while i < n:
        kind, tok = tokens[i]
        if kind == "normal" or not parts:
            # A sup/sub run with nothing preceding it on this line has no
            # base to attach to -- keep it as plain text rather than losing it.
            parts.append(latex_token(tok))
            i += 1
            continue

        base = parts.pop()
        run = []
        while i < n and tokens[i][0] == kind:
            run.append(latex_token(tokens[i][1]))
            i += 1
        marker = "^" if kind == "sup" else "_"
        parts.append(f"{base}{marker}{{{_join_latex_parts(run)}}}")

    latex = _fix_sqrt_grouping(_join_latex_parts(parts))
    return latex if latex else None


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

DEFAULT_OPTIONS = {
    "ocr_math": True,          # run image OCR (PaddleOCR-VL) to convert equations to MathML
    "detect_tables": True,     # detect and emit <table-wrap> for structured tables
    "strip_header_footer": True,  # drop repeating page headers/footers
}


def extract_pdf_to_xml(pdf_path, output_xml_path, log_callback=None, options=None):
    opts = dict(DEFAULT_OPTIONS)
    if options:
        opts.update(options)

    if log_callback:
        log_callback(f"Starting extraction for: {pdf_path}")

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

        # Merge tables and text/image blocks into a single top-to-bottom
        # reading order (sorted by top y-coordinate) so a table renders
        # exactly where it sits between paragraphs.
        items = [(t.bbox[1], "table", t) for t in page_tables]
        items += [(b["bbox"][1], "block", b) for b in page_blocks]
        items.sort(key=lambda entry: entry[0])

        for _, kind, item in items:
            if kind == "table":
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
                    for span in dedupe_overlapping_spans(line.get("spans", [])):
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
                    all_spans.extend(dedupe_overlapping_spans(line.get("spans", [])))

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
                    line_spans = dedupe_overlapping_spans(line.get("spans", []))
                    line_text = "".join(s["text"] for s in line_spans).strip()
                    if not line_text:
                        continue

                    if current_p is None:
                        current_p = ET.SubElement(parent, "p")
                    elif prev_line_text:
                        ensure_space(current_p, prev_line_text, line_text)

                    if opts["ocr_math"] and is_math_block(line_text, line_spans):
                        inline_formula = ET.SubElement(current_p, "inline-formula")

                        # Prefer building LaTeX directly from the PDF's own text
                        # (exact, deterministic, free) over image OCR. OCR is
                        # only a fallback for when the extracted text itself
                        # can't be trusted (e.g. an embedded math font PyMuPDF
                        # can't decode to real Unicode). Either way the result
                        # goes into a JATS <tex-math> element -- MathML
                        # generation from that LaTeX is a separate, later step.
                        latex = None
                        if spans_text_reliable(line_spans):
                            latex = build_latex_from_spans(line_spans)

                        if latex:
                            tex_math = ET.SubElement(inline_formula, "tex-math")
                            tex_math.text = latex
                            if log_callback:
                                log_callback("Detected math line, built LaTeX directly from PDF text.")
                        else:
                            # A single text line is often only ~12-14pt tall,
                            # which at 1:1 scale renders a crop only ~12-14px
                            # tall -- too little detail for OCR to read
                            # reliably. Pad the crop slightly (glyphs like
                            # roots/tall parens can extend past the reported
                            # line bbox) and render at a much higher
                            # effective resolution.
                            line_bbox = fitz.Rect(line.get("bbox", bbox))
                            pad = 3
                            crop_rect = (line_bbox + (-pad, -pad, pad, pad)) & page_rect
                            pix = page.get_pixmap(clip=crop_rect, matrix=fitz.Matrix(6, 6))
                            img_bytes = pix.tobytes("png")

                            ocr_latex = get_latex_from_image(img_bytes)
                            tex_math = ET.SubElement(inline_formula, "tex-math")
                            tex_math.text = ocr_latex

                            if log_callback:
                                log_callback("Detected math line (image OCR fallback), extracted LaTeX.")
                    else:
                        process_spans(current_p, line_spans)

                    prev_line_text = line_text

            elif block["type"] == 1:  # Image block
                parent = current_sec if current_sec is not None else body
                
                # We could try to detect if the image is a figure or a display equation.
                # Usually display equations are centered and small, while figures are larger.
                img_width = bbox[2] - bbox[0]
                img_height = bbox[3] - bbox[1]
                
                # Extract the image bytes
                img_bytes = block.get("image")
                
                # Let's say if it's very wide but short, it might be an equation
                if opts["ocr_math"] and img_width > 50 and img_height < 150 and img_bytes:
                    # inline-formula (not disp-formula) needs a text-flow
                    # parent -- an image block isn't part of any open <p>,
                    # so give it one of its own.
                    formula_p = ET.SubElement(parent, "p")
                    inline_formula = ET.SubElement(formula_p, "inline-formula")
                    ocr_latex = get_latex_from_image(img_bytes)
                    tex_math = ET.SubElement(inline_formula, "tex-math")
                    tex_math.text = ocr_latex

                    if log_callback:
                        log_callback("Detected math equation image, extracted LaTeX.")
                else:
                    fig = ET.SubElement(parent, "fig")
                    graphic = ET.SubElement(fig, "graphic")
                    # Placeholder for images
                    graphic.set("xlink:href", f"image_p{page_num+1}_{int(bbox[0])}_{int(bbox[1])}.jpg")
                    if log_callback:
                        log_callback("Found image, inserted placeholder.")

    # Pretty print and save
    xmlstr = minidom.parseString(ET.tostring(root, encoding="utf-8")).toprettyxml(indent="  ")
    
    # The xmlstr is already valid XML, we do not want to unescape &lt; and &gt; as that breaks XML parsing.
    
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
