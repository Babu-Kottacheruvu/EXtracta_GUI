import io
import os
import re
import tempfile

# PaddleOCR-VL's model downloads go through huggingface_hub/httpx, whose SSL
# verification fails on this network for otherwise-reachable hosts (missing
# intermediate certificate that Windows' native trust store resolves via AIA
# chasing but Python's bundled certifi does not). truststore patches the ssl
# module to use the OS trust store instead, which fixes it.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

# Skip PaddleX's own connectivity pre-check to the model hosters: it uses a
# stricter probe that reports failure even when the actual download (via the
# fix above) succeeds fine.
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

try:
    from paddleocr import PaddleOCRVL
    import latex2mathml.converter

    HAS_PADDLEOCR_VL = True
except ImportError:
    HAS_PADDLEOCR_VL = False

# Initialize once -- PaddleOCR-VL is a ~0.9B parameter vision-language model,
# expensive to load.
_pipeline = None

_LATEX_SEGMENT_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.DOTALL)


def _latex_to_mathml_full(latex):
    """A complete, standalone <mml:math> element for `latex`."""
    mathml = latex2mathml.converter.convert(latex)
    mathml = mathml.replace('<?xml version="1.0" encoding="UTF-8"?>', "").strip()
    mathml = mathml.replace(
        'xmlns="http://www.w3.org/1998/Math/MathML"',
        'xmlns:mml="http://www.w3.org/1998/Math/MathML"',
    )
    mathml = re.sub(r"<(/?)(m[a-z]+)", r"<\1mml:\2", mathml)
    return mathml


def _latex_to_mathml_inner(latex):
    """Same as _latex_to_mathml_full but with the outer <mml:math> wrapper
    stripped, for splicing into a larger combined element."""
    full = _latex_to_mathml_full(latex)
    inner = re.sub(r"^<mml:math[^>]*>", "", full)
    inner = re.sub(r"</mml:math>\s*$", "", inner)
    return inner


def _escape_xml_text(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_mathml_from_image(img_bytes):
    if not HAS_PADDLEOCR_VL:
        return '<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mtext>Math extraction requires paddleocr</mml:mtext></mml:math>'

    global _pipeline
    try:
        if _pipeline is None:
            _pipeline = PaddleOCRVL()

        # predict() accepts a file path; a temp file avoids any ambiguity
        # around which in-memory array/PIL formats it expects.
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(img_bytes)
                tmp_path = tf.name
            results = list(_pipeline.predict(tmp_path))
        finally:
            if tmp_path:
                os.unlink(tmp_path)

        if not results:
            raise ValueError("no output from PaddleOCR-VL")

        blocks = results[0].json.get("res", {}).get("parsing_res_list", [])
        content = " ".join(
            (b.get("block_content") or b.get("content") or "").strip()
            for b in blocks
            if (b.get("block_content") or b.get("content"))
        ).strip()
        if not content:
            raise ValueError("empty content from PaddleOCR-VL")

        # PaddleOCR-VL returns Markdown with inline/display LaTeX delimited by
        # $...$ / $$...$$ (e.g. "6.  $ x = (-b \pm \sqrt{b^2-4ac}) / 2a $").
        # Split into alternating plain-text and math segments so a leading
        # clause number stays as text rather than being force-fed through the
        # LaTeX converter.
        parts = []
        last = 0
        for m in _LATEX_SEGMENT_RE.finditer(content):
            if m.start() > last:
                text = content[last : m.start()].strip()
                if text:
                    parts.append(("text", text))
            latex = m.group(1) if m.group(1) is not None else m.group(2)
            parts.append(("math", latex.strip()))
            last = m.end()
        if last < len(content):
            text = content[last:].strip()
            if text:
                parts.append(("text", text))
        if not parts:
            parts = [("text", content)]

        if len(parts) == 1 and parts[0][0] == "math":
            return _latex_to_mathml_full(parts[0][1])

        inner = ""
        for kind, value in parts:
            if kind == "text":
                inner += f"<mml:mtext>{_escape_xml_text(value)}</mml:mtext>"
            else:
                inner += _latex_to_mathml_inner(value)
        return f'<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mrow>{inner}</mml:mrow></mml:math>'

    except Exception as e:
        err_msg = _escape_xml_text(str(e))
        return f'<mml:math xmlns:mml="http://www.w3.org/1998/Math/MathML"><mml:mtext>Math extraction failed: {err_msg}</mml:mtext></mml:math>'
