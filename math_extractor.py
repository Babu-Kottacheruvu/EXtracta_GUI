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

    HAS_PADDLEOCR_VL = True
except ImportError:
    HAS_PADDLEOCR_VL = False

# Initialize once -- PaddleOCR-VL is a ~0.9B parameter vision-language model,
# expensive to load.
_pipeline = None

_LATEX_SEGMENT_RE = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", re.DOTALL)


def get_latex_from_image(img_bytes):
    """OCR a cropped equation image into raw LaTeX (no MathML conversion --
    that is a separate, later step). Used as a fallback for math that isn't
    real extractable PDF text (e.g. a genuinely embedded equation image, or
    a custom math font PyMuPDF can't decode)."""
    if not HAS_PADDLEOCR_VL:
        return "% LaTeX extraction requires paddleocr"

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
        # A <tex-math> element should hold pure LaTeX, so pull out just the
        # math segment(s) and drop any surrounding label/prose text.
        math_segments = [
            (m.group(1) if m.group(1) is not None else m.group(2)).strip()
            for m in _LATEX_SEGMENT_RE.finditer(content)
        ]
        if math_segments:
            return " ".join(math_segments)

        # No $...$ delimiters found -- fall back to the raw recognized text
        # rather than returning nothing.
        return content

    except Exception as e:
        return f"% LaTeX extraction failed: {e}"
