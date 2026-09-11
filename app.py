import os
import time
import uuid
import threading
import traceback

import fitz
from flask import Flask, request, jsonify, send_file, Response, render_template
from lxml import etree

from pdf_to_xml import extract_pdf_to_xml
from config import get_openrouter_api_key, set_openrouter_api_key
from epub_generator import generate_epub

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 150 * 1024 * 1024  # 150MB

FILES = {}  # file_id -> {path, filename, size, page_count}
JOBS = {}  # job_id -> {status, logs, file_id, xml_path, error, options}
JOBS_LOCK = threading.Lock()


def _rehydrate_files_from_disk():
    """Restore a FILES entry for every PDF already sitting in UPLOAD_DIR from
    a previous run of this process, for the same reason
    _rehydrate_jobs_from_disk restores JOBS: a page left open across a
    server restart can still ask to convert a file_id it uploaded earlier,
    and without this that 404s with "Unknown file_id" even though the PDF
    itself is untouched on disk. The original filename the user uploaded it
    under isn't recoverable (uploads are stored as "<file_id>.pdf", not
    under their original name), so a rehydrated entry falls back to that.
    """
    if not os.path.isdir(UPLOAD_DIR):
        return
    for name in os.listdir(UPLOAD_DIR):
        if not name.endswith(".pdf"):
            continue
        file_id = name[: -len(".pdf")]
        path = os.path.join(UPLOAD_DIR, name)
        try:
            doc = fitz.open(path)
            page_count = len(doc)
            doc.close()
        except Exception:
            continue
        FILES[file_id] = {
            "path": path,
            "filename": name,
            "size": os.path.getsize(path),
            "page_count": page_count,
        }


def _rehydrate_jobs_from_disk():
    """Restore a "done" JOBS entry for every XML file already sitting in
    OUTPUT_DIR from a previous run of this process.

    JOBS is purely in-memory, but extract_pdf_to_xml's output files persist
    on disk under their job_id -- so a server restart (e.g. after a backend
    code change) doesn't lose any actual conversion result, only the
    in-memory record pointing to it. Without this, a page left open across
    that restart still holds a job_id from before, and every job-status
    check (download, XML view, EPUB export, ...) 404s with "Result not
    ready" even though the finished XML is right there on disk. The
    original upload's filename can't be recovered this way (that lived only
    in FILES, keyed by a different id with no on-disk trace), so a
    rehydrated job's download falls back to naming the file after its job_id
    -- functional, just less friendly than the original PDF's name.
    """
    if not os.path.isdir(OUTPUT_DIR):
        return
    for name in os.listdir(OUTPUT_DIR):
        if not name.endswith(".xml"):
            continue
        job_id = name[: -len(".xml")]
        JOBS[job_id] = {
            "status": "done",
            "logs": [],
            "file_id": None,
            "xml_path": os.path.join(OUTPUT_DIR, name),
            "error": None,
            "options": {},
        }


_rehydrate_files_from_disk()
_rehydrate_jobs_from_disk()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    file_id = uuid.uuid4().hex
    path = os.path.join(UPLOAD_DIR, f"{file_id}.pdf")
    f.save(path)

    try:
        doc = fitz.open(path)
        page_count = len(doc)
        doc.close()
    except Exception as e:
        os.remove(path)
        return jsonify({"error": f"Could not open PDF: {e}"}), 400

    FILES[file_id] = {
        "path": path,
        "filename": f.filename,
        "size": os.path.getsize(path),
        "page_count": page_count,
    }
    return jsonify(
        {
            "file_id": file_id,
            "filename": f.filename,
            "size": FILES[file_id]["size"],
            "page_count": page_count,
        }
    )


@app.route("/api/preview/<file_id>/<int:page_num>")
def preview(file_id, page_num):
    info = FILES.get(file_id)
    if not info:
        return jsonify({"error": "Unknown file"}), 404

    zoom = max(0.3, min(float(request.args.get("zoom", 1.5)), 4.0))
    doc = fitz.open(info["path"])
    try:
        if page_num < 1 or page_num > len(doc):
            return jsonify({"error": "Page out of range"}), 404
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img_bytes = pix.tobytes("png")
    finally:
        doc.close()
    return Response(img_bytes, mimetype="image/png")


@app.route("/api/thumbnail/<file_id>/<int:page_num>")
def thumbnail(file_id, page_num):
    info = FILES.get(file_id)
    if not info:
        return jsonify({"error": "Unknown file"}), 404

    doc = fitz.open(info["path"])
    try:
        if page_num < 1 or page_num > len(doc):
            return jsonify({"error": "Page out of range"}), 404
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(0.3, 0.3))
        img_bytes = pix.tobytes("png")
    finally:
        doc.close()
    return Response(img_bytes, mimetype="image/png")


def _run_job(job_id, file_id, options):
    info = FILES[file_id]
    out_path = os.path.join(OUTPUT_DIR, f"{job_id}.xml")

    def log_cb(msg):
        with JOBS_LOCK:
            JOBS[job_id]["logs"].append({"t": time.time(), "msg": msg})

    with JOBS_LOCK:
        JOBS[job_id]["status"] = "running"

    try:
        extract_pdf_to_xml(info["path"], out_path, log_callback=log_cb, options=options)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["xml_path"] = out_path
    except Exception as e:
        traceback.print_exc()
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["error"] = str(e)
        log_cb(f"ERROR: {e}")


@app.route("/api/convert", methods=["POST"])
def convert():
    data = request.get_json(force=True, silent=True) or {}
    file_id = data.get("file_id")
    if not file_id or file_id not in FILES:
        return jsonify({"error": "Unknown file_id"}), 400

    options = {
        "ocr_math": bool(data.get("ocr_math", True)),
        "detect_tables": bool(data.get("detect_tables", True)),
        "strip_header_footer": bool(data.get("strip_header_footer", True)),
        "latex_to_mathml": bool(data.get("latex_to_mathml", False)),
    }

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "status": "queued",
        "logs": [],
        "file_id": file_id,
        "xml_path": None,
        "error": None,
        "options": options,
    }

    threading.Thread(target=_run_job, args=(job_id, file_id, options), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/jobs/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    return jsonify({"status": job["status"], "logs": job["logs"], "error": job["error"]})


@app.route("/api/jobs/<job_id>/xml")
def job_xml(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Result not ready"}), 404
    with open(job["xml_path"], "r", encoding="utf-8") as f:
        return jsonify({"xml": f.read()})


@app.route("/api/jobs/<job_id>/validation")
def job_validation(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Result not ready"}), 404

    result = {"well_formed": False, "errors": [], "stats": {}}
    try:
        parser = etree.XMLParser(dtd_validation=False, load_dtd=False, no_network=True, resolve_entities=False)
        tree = etree.parse(job["xml_path"], parser)
        root = tree.getroot()
        result["well_formed"] = True
        result["stats"] = {
            "sections": len(root.findall(".//sec")),
            "paragraphs": len(root.findall(".//p")),
            "tables": len(root.findall(".//table-wrap")),
            "formulas": len(root.findall(".//inline-formula")),
            "mathml_formulas": len(root.findall(".//{http://www.w3.org/1998/Math/MathML}math")),
            "bold_runs": len(root.findall(".//bold")),
            "figures": len(root.findall(".//fig")),
        }
    except etree.XMLSyntaxError as e:
        result["errors"].append(str(e))

    return jsonify(result)


@app.route("/api/jobs/<job_id>/download")
def job_download(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Result not ready"}), 404
    info = FILES.get(job["file_id"])
    base_name = os.path.splitext(info["filename"])[0] if info else job_id
    return send_file(job["xml_path"], as_attachment=True, download_name=f"{base_name}.xml", mimetype="application/xml")


@app.route("/api/jobs/<job_id>/epub")
def job_epub(job_id):
    job = JOBS.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Result not ready"}), 404
    info = FILES.get(job["file_id"])
    base_name = os.path.splitext(info["filename"])[0] if info else job_id

    epub_path = os.path.join(OUTPUT_DIR, f"{job_id}.epub")
    if not os.path.exists(epub_path):
        generate_epub(job["xml_path"], epub_path, title=base_name)

    return send_file(epub_path, as_attachment=True, download_name=f"{base_name}.epub", mimetype="application/epub+zip")


@app.route("/api/settings/openrouter-api-key", methods=["GET", "POST", "DELETE"])
def openrouter_api_key_setting():
    # The raw key is never sent back to the browser once saved -- only
    # whether one is configured, plus a masked hint so the user can tell
    # which key is active without it being readable from the page/devtools.
    if request.method == "GET":
        key = get_openrouter_api_key()
        return jsonify({"configured": bool(key), "masked": _mask_key(key)})

    if request.method == "DELETE":
        set_openrouter_api_key(None)
        return jsonify({"configured": False, "masked": None})

    data = request.get_json(force=True, silent=True) or {}
    key = (data.get("api_key") or "").strip()
    if not key:
        return jsonify({"error": "No API key provided"}), 400
    set_openrouter_api_key(key)
    return jsonify({"configured": True, "masked": _mask_key(key)})


def _mask_key(key):
    if not key:
        return None
    return f"{'*' * max(len(key) - 4, 0)}{key[-4:]}"


if __name__ == "__main__":
    # Note: port 8642 avoids colliding with an unrelated project's dev server on port 5000.
    app.run(debug=True, threaded=True, port=8642)
