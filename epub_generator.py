import os
import uuid
import zipfile
import datetime
import xml.etree.ElementTree as ET

XHTML_NS = "http://www.w3.org/1999/xhtml"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
XLINK_NS = "http://www.w3.org/1999/xlink"
EPUB_OPS_NS = "http://www.idpf.org/2007/ops"

# The source XML declares a real xmlns:mml on its root, so ElementTree parses
# every mml:* element back as Clark-notation ({MATHML_NS}mi, ...) rather than
# a literal "mml:mi" string. Registering the prefix here means those parsed
# elements come back out of ET.tostring() as clean "mml:"-prefixed markup
# instead of an auto-generated "ns0:" alias.
ET.register_namespace("mml", MATHML_NS)

CSS = """\
body { font-family: Georgia, 'Times New Roman', serif; line-height: 1.5; margin: 1em; color: #222; }
h1, h2, h3, h4, h5, h6 { font-family: Helvetica, Arial, sans-serif; line-height: 1.25; }
p { margin: 0.6em 0; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; }
th, td { border: 1px solid #999; padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: #eee; }
.table-caption { font-weight: bold; margin-bottom: 0.3em; }
.image-placeholder {
  border: 2px dashed #999;
  background: #f4f4f4;
  color: #555;
  padding: 1.5em;
  margin: 1em 0;
  text-align: center;
  font-style: italic;
}
.tex-fallback {
  background: #f0f0f0;
  color: #a00;
  font-family: 'Courier New', monospace;
  padding: 0 0.3em;
}
"""


def _text_of(elem):
    return (elem.text or "").strip() if elem is not None else ""


def _append_inline(src, dest, mathml_seen):
    # Carries over src's leading text plus every recognized inline child
    # (bold/italic/styled-content/inline-formula), preserving JATS's
    # text/tail mixed-content order so a formula sitting mid-sentence stays
    # mid-sentence rather than being hoisted before or after the prose.
    dest.text = src.text
    for child in list(src):
        tag = child.tag
        if tag == "bold":
            dest_child = ET.SubElement(dest, "b")
            _append_inline(child, dest_child, mathml_seen)
        elif tag == "italic":
            dest_child = ET.SubElement(dest, "i")
            _append_inline(child, dest_child, mathml_seen)
        elif tag == "styled-content":
            dest_child = ET.SubElement(dest, "span")
            style = child.get("style")
            if style:
                dest_child.set("style", style)
            _append_inline(child, dest_child, mathml_seen)
        elif tag == "inline-formula":
            dest_child = _convert_inline_formula(child, mathml_seen)
            dest.append(dest_child)
        else:
            # Unknown inline element (shouldn't occur for this app's output)
            # -- keep its text rather than silently dropping content.
            dest_child = ET.SubElement(dest, "span")
            _append_inline(child, dest_child, mathml_seen)
        dest_child.tail = child.tail


def _convert_inline_formula(formula, mathml_seen):
    span = ET.Element("span")
    span.set("class", "formula")
    mml_math = formula.find("{%s}math" % MATHML_NS)
    if mml_math is not None:
        span.append(mml_math)
        mathml_seen.append(True)
    else:
        tex_math = formula.find("tex-math")
        code = ET.SubElement(span, "code")
        code.set("class", "tex-fallback")
        code.text = _text_of(tex_math) or (tex_math.text if tex_math is not None else "")
    return span


def _convert_table_wrap(elem, mathml_seen):
    wrapper = ET.Element("div")
    wrapper.set("class", "table-wrap")

    caption_src = elem.find("title")
    if caption_src is not None:
        caption = ET.SubElement(wrapper, "p")
        caption.set("class", "table-caption")
        _append_inline(caption_src, caption, mathml_seen)

    table_src = elem.find("table")
    if table_src is None:
        return wrapper

    table_dest = ET.SubElement(wrapper, "table")

    def copy_cell_attrs(src, dest):
        for name in ("style", "colspan", "rowspan"):
            value = src.get(name)
            if value is not None:
                dest.set(name, value)

    thead_src = table_src.find("thead")
    if thead_src is not None:
        thead_dest = ET.SubElement(table_dest, "thead")
        for tr_src in thead_src.findall("tr"):
            tr_dest = ET.SubElement(thead_dest, "tr")
            for th_src in tr_src.findall("th"):
                th_dest = ET.SubElement(tr_dest, "th")
                copy_cell_attrs(th_src, th_dest)
                _append_inline(th_src, th_dest, mathml_seen)

    tbody_src = table_src.find("tbody")
    if tbody_src is not None:
        tbody_dest = ET.SubElement(table_dest, "tbody")
        for tr_src in tbody_src.findall("tr"):
            tr_dest = ET.SubElement(tbody_dest, "tr")
            for td_src in tr_src.findall("td"):
                td_dest = ET.SubElement(tr_dest, "td")
                copy_cell_attrs(td_src, td_dest)
                _append_inline(td_src, td_dest, mathml_seen)

    return wrapper


def _convert_fig(elem):
    div = ET.Element("div")
    div.set("class", "image-placeholder")
    graphic = elem.find("graphic")
    href = None
    if graphic is not None:
        href = graphic.get("{%s}href" % XLINK_NS) or graphic.get("xlink:href") or graphic.get("href")
    div.text = "[Image placeholder: %s]" % (href or "unknown")
    return div


def _convert_block(elem, dest_parent, heading_level, mathml_seen):
    tag = elem.tag
    if tag == "p":
        p = ET.SubElement(dest_parent, "p")
        _append_inline(elem, p, mathml_seen)
    elif tag == "table-wrap":
        dest_parent.append(_convert_table_wrap(elem, mathml_seen))
    elif tag == "fig":
        dest_parent.append(_convert_fig(elem))
    elif tag == "sec":
        title_src = elem.find("title")
        heading = ET.SubElement(dest_parent, "h%d" % min(heading_level, 6))
        heading.text = _text_of(title_src) or "Subsection"
        for child in list(elem):
            if child.tag == "title":
                continue
            _convert_block(child, dest_parent, heading_level + 1, mathml_seen)
    # Any other/unexpected top-level tag is skipped -- this app's own
    # pdf_to_xml.py only ever emits sec/p/table-wrap/fig at this level.


def _split_into_chapters(body):
    # One chapter per top-level <sec>; anything appearing in <body> before
    # the first <sec> (a title page or lead-in paragraph) becomes its own
    # leading chapter so it isn't lost.
    chapters = []
    pending = []

    def flush(title):
        if pending:
            chapters.append({"title": title, "children": list(pending)})
            pending.clear()

    for child in list(body) if body is not None else []:
        if child.tag == "sec":
            flush(None)
            title_src = child.find("title")
            title = _text_of(title_src) or None
            children = [c for c in list(child) if c.tag != "title"]
            chapters.append({"title": title, "children": children})
        else:
            pending.append(child)
    flush(None)

    if not chapters:
        chapters.append({"title": None, "children": []})

    return chapters


def _chapter_label(chapter, index, doc_title):
    if chapter["title"]:
        return chapter["title"]
    if index == 0:
        return doc_title
    return "Section %d" % (index + 1)


def _build_chapter_xhtml(label, children):
    mathml_seen = []

    html = ET.Element("html")
    html.set("xmlns", XHTML_NS)
    html.set("lang", "en")

    head = ET.SubElement(html, "head")
    meta_charset = ET.SubElement(head, "meta")
    meta_charset.set("charset", "utf-8")
    title_el = ET.SubElement(head, "title")
    title_el.text = label
    link = ET.SubElement(head, "link")
    link.set("rel", "stylesheet")
    link.set("href", "style.css")
    link.set("type", "text/css")

    body = ET.SubElement(html, "body")
    h1 = ET.SubElement(body, "h1")
    h1.text = label

    for child in children:
        _convert_block(child, body, 2, mathml_seen)

    xhtml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(html, encoding="unicode")
    return xhtml_str, bool(mathml_seen)


def _build_nav_xhtml(doc_title, chapter_files):
    html = ET.Element("html")
    html.set("xmlns", XHTML_NS)
    html.set("xmlns:epub", EPUB_OPS_NS)
    html.set("lang", "en")

    head = ET.SubElement(html, "head")
    title_el = ET.SubElement(head, "title")
    title_el.text = doc_title
    link = ET.SubElement(head, "link")
    link.set("rel", "stylesheet")
    link.set("href", "style.css")
    link.set("type", "text/css")

    body = ET.SubElement(html, "body")
    nav = ET.SubElement(body, "nav")
    nav.set("epub:type", "toc")
    nav.set("id", "toc")
    h1 = ET.SubElement(nav, "h1")
    h1.text = doc_title
    ol = ET.SubElement(nav, "ol")
    for href, label in chapter_files:
        li = ET.SubElement(ol, "li")
        a = ET.SubElement(li, "a")
        a.set("href", href)
        a.text = label

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(html, encoding="unicode")


def _build_opf(doc_title, chapter_entries):
    # chapter_entries: list of (item_id, href, has_mathml)
    package = ET.Element("package")
    package.set("xmlns", "http://www.idpf.org/2007/opf")
    package.set("version", "3.0")
    package.set("unique-identifier", "pub-id")

    metadata = ET.SubElement(package, "metadata")
    metadata.set("xmlns:dc", "http://purl.org/dc/elements/1.1/")
    dc_title = ET.SubElement(metadata, "dc:title")
    dc_title.text = doc_title
    dc_lang = ET.SubElement(metadata, "dc:language")
    dc_lang.text = "en"
    dc_id = ET.SubElement(metadata, "dc:identifier")
    dc_id.set("id", "pub-id")
    dc_id.text = "urn:uuid:%s" % uuid.uuid4()
    meta_modified = ET.SubElement(metadata, "meta")
    meta_modified.set("property", "dcterms:modified")
    meta_modified.text = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = ET.SubElement(package, "manifest")
    nav_item = ET.SubElement(manifest, "item")
    nav_item.set("id", "nav")
    nav_item.set("href", "nav.xhtml")
    nav_item.set("media-type", "application/xhtml+xml")
    nav_item.set("properties", "nav")

    css_item = ET.SubElement(manifest, "item")
    css_item.set("id", "css")
    css_item.set("href", "style.css")
    css_item.set("media-type", "text/css")

    for item_id, href, has_mathml in chapter_entries:
        item = ET.SubElement(manifest, "item")
        item.set("id", item_id)
        item.set("href", href)
        item.set("media-type", "application/xhtml+xml")
        if has_mathml:
            item.set("properties", "mathml")

    spine = ET.SubElement(package, "spine")
    for item_id, _href, _has_mathml in chapter_entries:
        itemref = ET.SubElement(spine, "itemref")
        itemref.set("idref", item_id)

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(package, encoding="unicode")


_CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def generate_epub(xml_path, output_epub_path, title=None):
    tree = ET.parse(xml_path)
    root = tree.getroot()
    body = root.find("body")

    article_title = root.find(".//article-title")
    doc_title = title or _text_of(article_title) or os.path.splitext(os.path.basename(xml_path))[0]

    chapters = _split_into_chapters(body)

    chapter_files = []  # (href, label) for the nav doc
    chapter_entries = []  # (item_id, href, has_mathml) for the manifest
    chapter_bodies = []  # (href, xhtml_str)

    for index, chapter in enumerate(chapters):
        label = _chapter_label(chapter, index, doc_title)
        href = "chap%d.xhtml" % (index + 1)
        item_id = "chap%d" % (index + 1)
        xhtml_str, has_mathml = _build_chapter_xhtml(label, chapter["children"])
        chapter_files.append((href, label))
        chapter_entries.append((item_id, href, has_mathml))
        chapter_bodies.append((href, xhtml_str))

    nav_xhtml = _build_nav_xhtml(doc_title, chapter_files)
    opf_xml = _build_opf(doc_title, chapter_entries)

    with zipfile.ZipFile(output_epub_path, "w", zipfile.ZIP_DEFLATED) as zf:
        mimetype_info = zipfile.ZipInfo("mimetype")
        mimetype_info.compress_type = zipfile.ZIP_STORED
        zf.writestr(mimetype_info, "application/epub+zip")

        zf.writestr("META-INF/container.xml", _CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", opf_xml)
        zf.writestr("OEBPS/nav.xhtml", nav_xhtml)
        zf.writestr("OEBPS/style.css", CSS)
        for href, xhtml_str in chapter_bodies:
            zf.writestr("OEBPS/%s" % href, xhtml_str)
