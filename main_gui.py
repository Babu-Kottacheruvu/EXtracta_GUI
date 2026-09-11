"""Desktop launcher for the PDF to XML Converter.

Runs the existing Flask app (app.py) in a background thread and displays it
in a native desktop window via pywebview, instead of opening a browser tab.
The dashboard UI (templates/static) and the conversion pipeline
(pdf_to_xml.py) are unchanged.

WebView2 (the engine pywebview uses on Windows) does not handle browser-style
downloads (Content-Disposition attachments, <a download> blob links) the way
a real browser does, so "Download XML" needs a native Save As dialog instead.
The Api class below is exposed to the page as window.pywebview.api and does
that directly against the in-memory job/file state from app.py.
"""

import os
import re
import socket
import threading
import time

import webview

from app import app, FILES, JOBS, OUTPUT_DIR
from epub_generator import generate_epub

HOST = "127.0.0.1"


def _free_port(host):
    """Reserve an OS-assigned free port for THIS process's own server.

    A previous version used a fixed port (8642) and just polled whether it
    was open before creating the window. That's broken if a stale
    main_gui.py process from an earlier run is still holding that port: the
    poll sees the port open (because the OLD process is answering on it),
    so this process's window ends up pointing at someone else's Flask
    server -- conversions run there and populate ITS JOBS dict, but the
    save_xml/save_text/save_epub calls below run inside THIS process and
    check THIS process's own, unrelated (empty) JOBS import, so every
    download fails with "Result not ready" even though the conversion
    genuinely finished. Binding our own OS-assigned port sidesteps the
    whole class of bug: this process only ever talks to a server it itself
    started.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def run_server(port):
    app.run(host=HOST, port=port, debug=False, use_reloader=False, threaded=True)


def _base_name(job):
    info = FILES.get(job["file_id"])
    if info:
        return os.path.splitext(info["filename"])[0]
    return job.get("xml_path") and os.path.splitext(os.path.basename(job["xml_path"]))[0] or "output"


class Api:
    def save_xml(self, job_id):
        job = JOBS.get(job_id)
        if not job or job.get("status") != "done":
            return {"ok": False, "error": "Result not ready"}

        window = webview.windows[0]
        dest = window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=os.path.expanduser("~"),
            save_filename=f"{_base_name(job)}.xml",
            file_types=("XML Files (*.xml)", "All files (*.*)"),
        )
        if not dest:
            return {"ok": False, "cancelled": True}
        dest_path = dest[0] if isinstance(dest, (list, tuple)) else dest

        with open(job["xml_path"], "r", encoding="utf-8") as src:
            content = src.read()
        with open(dest_path, "w", encoding="utf-8") as out:
            out.write(content)
        return {"ok": True, "path": dest_path}

    def save_text(self, job_id):
        job = JOBS.get(job_id)
        if not job or job.get("status") != "done":
            return {"ok": False, "error": "Result not ready"}

        window = webview.windows[0]
        dest = window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=os.path.expanduser("~"),
            save_filename=f"{_base_name(job)}.txt",
            file_types=("Text Files (*.txt)", "All files (*.*)"),
        )
        if not dest:
            return {"ok": False, "cancelled": True}
        dest_path = dest[0] if isinstance(dest, (list, tuple)) else dest

        with open(job["xml_path"], "r", encoding="utf-8") as src:
            xml_text = src.read()
        text = re.sub(r"<[^>]+>", " ", xml_text)
        text = re.sub(r"\s+", " ", text).strip()
        with open(dest_path, "w", encoding="utf-8") as out:
            out.write(text)
        return {"ok": True, "path": dest_path}

    def save_epub(self, job_id):
        job = JOBS.get(job_id)
        if not job or job.get("status") != "done":
            return {"ok": False, "error": "Result not ready"}

        window = webview.windows[0]
        dest = window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=os.path.expanduser("~"),
            save_filename=f"{_base_name(job)}.epub",
            file_types=("EPUB Files (*.epub)", "All files (*.*)"),
        )
        if not dest:
            return {"ok": False, "cancelled": True}
        dest_path = dest[0] if isinstance(dest, (list, tuple)) else dest

        epub_path = os.path.join(OUTPUT_DIR, f"{job_id}.epub")
        if not os.path.exists(epub_path):
            generate_epub(job["xml_path"], epub_path, title=_base_name(job))

        with open(epub_path, "rb") as src:
            content = src.read()
        with open(dest_path, "wb") as out:
            out.write(content)
        return {"ok": True, "path": dest_path}


def _port_open(host, port, timeout=0.3):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def main():
    port = _free_port(HOST)
    server_thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    server_thread.start()

    for _ in range(100):
        if _port_open(HOST, port):
            break
        time.sleep(0.1)

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.create_window(
        "PDF to XML Converter",
        f"http://{HOST}:{port}",
        width=1440,
        height=900,
        min_size=(1100, 700),
        js_api=Api(),
    )
    webview.start()


if __name__ == "__main__":
    main()
