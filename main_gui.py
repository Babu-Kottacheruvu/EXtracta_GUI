"""Desktop client for the PDF to XML Converter.

This process does NOT run the Flask app or the conversion pipeline itself --
it is a thin wrapper that opens the hosted server (see SERVER_URL below) in a
native window via pywebview, and handles native Save-As dialogs for
downloads (WebView2 doesn't support browser-style downloads the way a real
browser does, so those go through the Api class below instead).

Because the UI, the PDF/OCR pipeline, and the OpenRouter calls all live on
the server, this app REQUIRES an internet connection to do anything useful --
that's intentional: it's what keeps the installed .exe small and lets the
pipeline be fixed/upgraded on the server without anyone reinstalling the app.
"""

import os
import re
import sys
import time
import tkinter as tk
from tkinter import messagebox

import requests
import webview

# Where the hosted backend lives. Overridable without rebuilding the .exe:
#   1. the EXTRACTA_SERVER_URL environment variable, or
#   2. a "server_url.txt" file dropped next to the .exe (or this script),
#      containing just the URL.
# Falls back to the deployed Render URL below -- replace this with your own
# once you've deployed (see render.yaml / Procfile).
DEFAULT_SERVER_URL = "https://extracta.onrender.com"


def _app_dir():
    # sys.executable is the .exe itself once frozen by PyInstaller; the
    # source .py file's own path otherwise.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _server_url():
    env_url = os.environ.get("EXTRACTA_SERVER_URL")
    if env_url:
        return env_url.strip().rstrip("/")
    config_path = os.path.join(_app_dir(), "server_url.txt")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            url = f.read().strip()
            if url:
                return url.rstrip("/")
    return DEFAULT_SERVER_URL.rstrip("/")


SERVER_URL = _server_url()


def _check_connection():
    """Confirm the server is reachable before opening the window.

    Render's free plan spins a sleeping instance back up on the first
    request, which can take 30-50s -- so this tries a quick probe first and,
    only if that fails, a second, more patient one before giving up (rather
    than misreporting a cold start as "no internet").
    """
    for timeout in (8, 45):
        try:
            resp = requests.get(SERVER_URL, timeout=timeout)
            if resp.status_code < 500:
                return True
        except requests.RequestException:
            time.sleep(1)
    return False


def _fail_with_message(message):
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("PDF to XML Converter", message)
    root.destroy()


def _filename_from_response(resp, fallback):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r'filename="?([^";]+)"?', cd)
    return m.group(1) if m else fallback


class Api:
    """Exposed to the page as window.pywebview.api.

    Each save_* method re-fetches the finished result from the server (the
    client holds no job/file state of its own) and writes it to a path the
    user picks via a native Save As dialog.
    """

    def _save_via(self, url_path, force_ext, file_types, transform=None, binary=False):
        window = webview.windows[0]
        try:
            resp = requests.get(f"{SERVER_URL}{url_path}", timeout=120)
        except requests.RequestException as e:
            return {"ok": False, "error": f"Could not reach server: {e}"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"Server error ({resp.status_code})"}

        suggested = _filename_from_response(resp, f"output.{force_ext}")
        base, _ = os.path.splitext(suggested)
        suggested = f"{base}.{force_ext}"

        dest = window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=os.path.expanduser("~"),
            save_filename=suggested,
            file_types=file_types,
        )
        if not dest:
            return {"ok": False, "cancelled": True}
        dest_path = dest[0] if isinstance(dest, (list, tuple)) else dest

        if binary:
            with open(dest_path, "wb") as out:
                out.write(resp.content)
        else:
            content = resp.text
            if transform:
                content = transform(content)
            with open(dest_path, "w", encoding="utf-8") as out:
                out.write(content)
        return {"ok": True, "path": dest_path}

    def save_xml(self, job_id):
        return self._save_via(
            f"/api/jobs/{job_id}/download", "xml",
            ("XML Files (*.xml)", "All files (*.*)"),
        )

    def save_text(self, job_id):
        def strip_tags(xml_text):
            text = re.sub(r"<[^>]+>", " ", xml_text)
            return re.sub(r"\s+", " ", text).strip()

        return self._save_via(
            f"/api/jobs/{job_id}/download", "txt",
            ("Text Files (*.txt)", "All files (*.*)"),
            transform=strip_tags,
        )

    def save_epub(self, job_id):
        return self._save_via(
            f"/api/jobs/{job_id}/epub", "epub",
            ("EPUB Files (*.epub)", "All files (*.*)"),
            binary=True,
        )


def main():
    if not _check_connection():
        _fail_with_message(
            "PDF to XML Converter needs an internet connection to reach:\n\n"
            f"{SERVER_URL}\n\n"
            "Please check your connection and try again."
        )
        sys.exit(1)

    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.create_window(
        "PDF to XML Converter",
        SERVER_URL,
        width=1440,
        height=900,
        min_size=(1100, 700),
        js_api=Api(),
    )
    webview.start()


if __name__ == "__main__":
    main()
