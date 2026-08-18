import os
import time
import uuid
import threading
import traceback

import fitz
from flask import Flask, request, jsonify, send_file, Response, render_template
from lxml import etree

from pdf_to_xml import extract_pdf_to_xml

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
            "formulas": len(root.findall(".//disp-formula")),
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


if __name__ == "__main__":
    # Note: port 8642 avoids colliding with an unrelated project's dev server on port 5000.
    app.run(debug=True, threaded=True, port=8642)
