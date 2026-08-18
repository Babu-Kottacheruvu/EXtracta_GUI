import fitz  # PyMuPDF
import xml.etree.ElementTree as ET
from xml.dom import minidom
import os
import re
import sys
from math_extractor import get_mathml_from_image

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
    """Simple heuristic to detect headers and footers based on y-coordinates"""
    y0 = block_bbox[1]
    y1 = block_bbox[3]
    height = page_rect.height
    
    # Top 7% and bottom 7% are considered headers/footers
    if y0 <= height * 0.07 or y1 >= height * 0.93:
        return True
    return False


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

    bbox = table.bbox
    page_coverage = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / max(page_rect.get_area(), 1)
    all_cells = [cell for row in rows for cell in row if cell]
    total_text = sum(len(cell) for cell in all_cells)
    longest_cell = max((len(cell) for cell in all_cells), default=0)

    # Full-page grids with a single narrative cell are common in templates.
    if page_coverage > 0.75 and (longest_cell > 300 or longest_cell > total_text * 0.70):
        return False
    return True

DEFAULT_OPTIONS = {
    "ocr_math": True,          # run image OCR (pix2tex) to convert equations to MathML
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
            last_elem = None
            for span in spans:
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
            if opts["strip_header_footer"] and is_header_or_footer(bbox, page_rect):
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
                # An external header is not part of table.rows.  Internal headers
                # are already the first row and must not be emitted twice.
                if table.header and table.header.cells and table.header.external:
                    thead = ET.SubElement(table_el, "thead")
                    tr = ET.SubElement(thead, "tr")
                    for cell_bbox in table.header.cells:
                        if cell_bbox is None: continue
                        th = ET.SubElement(tr, "th", {"style": cell_style + " text-align:left;"})
                        cell_dict = page.get_text("dict", clip=cell_bbox)
                        for b in cell_dict.get("blocks", []):
                            if b.get("type") == 0:
                                for l in b.get("lines", []):
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
                                    for l in b.get("lines", []):
                                        process_spans(td, l.get("spans", []))
                continue

            block = item
            bbox = block["bbox"]
            if block["type"] == 0:  # Text block
                block_text = ""
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        block_text += span["text"] + " "
                
                block_text = block_text.strip()
                if not block_text:
                    continue

                # Math block detection heuristic
                def is_math_block(text, spans):
                    if not spans or not text:
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
                    return False

                # Reconstruct full block text and collect spans to check for math / headings
                all_spans = []
                for line in block.get("lines", []):
                    all_spans.extend(line.get("spans", []))

                total_chars = sum(len(s["text"]) for s in all_spans) or 1
                bold_chars = sum(len(s["text"]) for s in all_spans if s["flags"] & 16)
                bold_ratio = bold_chars / total_chars

                # A real heading is either ALL CAPS, or a short line that is *entirely*
                # (or almost entirely) bold. A numbered clause like "4.3.12 The bidder's
                # solution shall be..." only has its number in bold, so bold_ratio stays
                # low and it correctly falls through to the normal paragraph branch below,
                # where process_spans keeps the bold number and normal prose as separate
                # runs inside the same <p>.
                is_heading = len(block_text) < 100 and (
                    block_text.isupper()
                    or (bold_ratio > 0.8 and re.match(r'^\d+(\.\d+)*[\.\s]', block_text))
                )

                if is_heading:
                    current_sec = ET.SubElement(body, "sec")
                    title = ET.SubElement(current_sec, "title")
                    title.text = sanitize_xml_text(block_text)
                elif opts["ocr_math"] and is_math_block(block_text, all_spans):
                    parent = current_sec if current_sec is not None else body
                    disp_formula = ET.SubElement(parent, "disp-formula")

                    # Render block as image
                    pix = page.get_pixmap(clip=bbox)
                    img_bytes = pix.tobytes("png")

                    mathml = get_mathml_from_image(img_bytes)
                    try:
                        math_elem = ET.fromstring(mathml)
                        disp_formula.append(math_elem)
                    except:
                        disp_formula.text = mathml

                    if log_callback:
                        log_callback("Detected math text block, rendered and converted.")
                else:
                    # Append paragraph
                    parent = current_sec if current_sec is not None else body
                    p = ET.SubElement(parent, "p")
                    
                    for line in block.get("lines", []):
                        process_spans(p, line.get("spans", []))
                    
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
                    disp_formula = ET.SubElement(parent, "disp-formula")
                    mathml = get_mathml_from_image(img_bytes)
                    
                    # Embed the MathML XML directly using a dirty hack since we have it as string
                    # Alternatively, parse it and append
                    try:
                        math_elem = ET.fromstring(mathml)
                        disp_formula.append(math_elem)
                    except:
                        disp_formula.text = mathml
                        
                    if log_callback:
                        log_callback("Detected and converted math equation.")
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
