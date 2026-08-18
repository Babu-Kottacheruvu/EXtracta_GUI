(function () {
  "use strict";

  const state = {
    fileId: null,
    filename: "",
    pageCount: 0,
    currentPage: 1,
    zoomPercent: 100,
    jobId: null,
    jobPollTimer: null,
    seenLogCount: 0,
    xmlText: "",
    layoutMode: "auto",
  };

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // ---------------- Sidebar navigation (Dashboard/Convert real, rest placeholder) ----------------
  const dashboardView = $("#dashboardView");
  const placeholderView = $("#placeholderView");
  const pageTitle = $("#pageTitle");
  const placeholderTitle = $("#placeholderTitle");

  $$(".nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      $$(".nav-item").forEach((i) => i.classList.remove("active"));
      item.classList.add("active");
      const view = item.dataset.view;
      const label = item.textContent.trim();
      if (view === "dashboard") {
        dashboardView.hidden = false;
        placeholderView.hidden = true;
        pageTitle.textContent = "PDF to XML Converter";
      } else {
        dashboardView.hidden = true;
        placeholderView.hidden = false;
        pageTitle.textContent = label;
        placeholderTitle.textContent = label;
      }
    });
  });

  $("#hamburgerBtn").addEventListener("click", () => {
    document.querySelector(".app-shell").classList.toggle("sidebar-collapsed");
  });

  // ---------------- Upload ----------------
  const dropzone = $("#dropzone");
  const fileInput = $("#fileInput");
  const browseBtn = $("#browseBtn");
  const fileRow = $("#fileRow");
  const fileNameEl = $("#fileName");
  const fileSizeEl = $("#fileSize");
  const uploadError = $("#uploadError");
  const convertBtn = $("#convertBtn");

  browseBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    fileInput.click();
  });
  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files[0]) uploadFile(fileInput.files[0]);
  });

  ["dragenter", "dragover"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
    })
  );
  dropzone.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / (1024 * 1024)).toFixed(2) + " MB";
  }

  function showUploadError(msg) {
    uploadError.hidden = false;
    uploadError.textContent = msg;
  }

  async function uploadFile(file) {
    uploadError.hidden = true;
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      showUploadError("Only PDF files are supported.");
      return;
    }

    const form = new FormData();
    form.append("file", file);

    dropzone.querySelector(".dz-text").textContent = "Uploading...";
    try {
      const res = await fetch("/api/upload", { method: "POST", body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Upload failed");

      state.fileId = data.file_id;
      state.filename = data.filename;
      state.pageCount = data.page_count;
      state.currentPage = 1;

      fileNameEl.textContent = data.filename;
      fileSizeEl.textContent = `${formatSize(data.size)} • ${data.page_count} page${data.page_count === 1 ? "" : "s"}`;
      fileRow.hidden = false;
      convertBtn.disabled = false;

      resetConversionOutputs();
      $("#pageTotal").textContent = state.pageCount;
      loadPreview(1);
      loadThumbnails();
    } catch (err) {
      showUploadError(err.message);
    } finally {
      dropzone.querySelector(".dz-text").textContent = "Drag & drop your PDF file here";
    }
  }

  function resetConversionOutputs() {
    state.jobId = null;
    state.seenLogCount = 0;
    if (state.jobPollTimer) clearInterval(state.jobPollTimer);
    $("#downloadBtn").disabled = true;
    $("#exportBtn").disabled = true;
    $("#codeContent").textContent = "// XML output will appear here after conversion";
    $("#logsPanel").innerHTML = '<div class="log-line muted">Logs will appear here during conversion...</div>';
    $("#validationPanel").innerHTML =
      '<div class="empty-state"><svg class="icon empty-icon"><use href="#i-check-circle"/></svg><div>Run a conversion to see validation results</div></div>';
    $("#convertBtnLabel").textContent = "Convert to XML";
    $("#convertHint").textContent = "Estimated time: ~30 seconds";
  }

  // ---------------- PDF preview ----------------
  const pdfPageWrap = $("#pdfPageWrap");
  const pageInput = $("#pageInput");
  const zoomSelect = $("#zoomSelect");

  function actualZoom() {
    return 1.5 * (state.zoomPercent / 100);
  }

  function loadPreview(page) {
    if (!state.fileId) return;
    page = Math.max(1, Math.min(page, state.pageCount));
    state.currentPage = page;
    pageInput.value = page;
    const url = `/api/preview/${state.fileId}/${page}?zoom=${actualZoom()}`;
    pdfPageWrap.innerHTML = `<img src="${url}" alt="Page ${page}">`;
    highlightActiveThumb();
  }

  $("#firstPageBtn").addEventListener("click", () => loadPreview(1));
  $("#prevPageBtn").addEventListener("click", () => loadPreview(state.currentPage - 1));
  $("#nextPageBtn").addEventListener("click", () => loadPreview(state.currentPage + 1));
  $("#lastPageBtn").addEventListener("click", () => loadPreview(state.pageCount));
  pageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const n = parseInt(pageInput.value, 10);
      if (!isNaN(n)) loadPreview(n);
    }
  });
  pageInput.addEventListener("blur", () => {
    const n = parseInt(pageInput.value, 10);
    if (!isNaN(n)) loadPreview(n);
  });
  zoomSelect.addEventListener("change", () => {
    state.zoomPercent = parseInt(zoomSelect.value, 10);
    if (state.fileId) loadPreview(state.currentPage);
  });

  $("#fullscreenBtn").addEventListener("click", () => {
    $(".right-col").classList.toggle("is-fullscreen");
  });
  $("#searchBtn").addEventListener("click", () => {
    // Text search within the rendered page image is out of scope; focus the page
    // input as the closest useful action for now.
    pageInput.focus();
  });

  // ---------------- Thumbnails ----------------
  const thumbStrip = $("#thumbStrip");

  function loadThumbnails() {
    thumbStrip.innerHTML = "";
    for (let p = 1; p <= state.pageCount; p++) {
      const div = document.createElement("div");
      div.className = "thumb" + (p === state.currentPage ? " active" : "");
      div.dataset.page = p;
      div.innerHTML = `<img loading="lazy" src="/api/thumbnail/${state.fileId}/${p}" alt="Page ${p} thumbnail"><span class="thumb-num">${p}</span>`;
      div.addEventListener("click", () => loadPreview(p));
      thumbStrip.appendChild(div);
    }
  }

  function highlightActiveThumb() {
    $$(".thumb").forEach((t) => t.classList.toggle("active", parseInt(t.dataset.page, 10) === state.currentPage));
    const active = thumbStrip.querySelector(".thumb.active");
    if (active) active.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }

  $("#thumbLeftBtn").addEventListener("click", () => (thumbStrip.scrollLeft -= 200));
  $("#thumbRightBtn").addEventListener("click", () => (thumbStrip.scrollLeft += 200));

  // ---------------- Conversion options ----------------
  $$(".toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      $$(".toggle-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.layoutMode = btn.dataset.layout;
    });
  });

  const moreOptionsToggle = $("#moreOptionsToggle");
  const moreOptions = $("#moreOptions");
  moreOptionsToggle.addEventListener("click", () => {
    const open = moreOptions.hidden;
    moreOptions.hidden = !open;
    moreOptionsToggle.classList.toggle("open", open);
  });

  // ---------------- Tabs ----------------
  const viewer = $("#viewer");
  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      viewer.dataset.mode = tab.dataset.tab;
    });
  });

  // ---------------- XML syntax highlighting ----------------
  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function highlightXml(xml) {
    const lines = xml.split("\n");
    return lines
      .map((line, i) => {
        let escaped = escapeHtml(line);

        // Comments
        if (/^\s*<!--/.test(line)) {
          return `<span class="ln">${i + 1}</span><span class="comment">${escaped}</span>`;
        }

        // Tag with optional attributes: <tag attr="value" attr2='value2'>
        escaped = escaped.replace(
          /(&lt;\/?)([a-zA-Z0-9_:\-]+)((?:\s+[a-zA-Z0-9_:\-]+=(?:&quot;[^&]*&quot;|"[^"]*"|'[^']*'))*)(\s*\/?&gt;)/g,
          (m, open, name, attrs, close) => {
            let attrHtml = attrs.replace(
              /([a-zA-Z0-9_:\-]+)(=)("([^"]*)"|'([^']*)')/g,
              (am, aname, eq, full) => {
                return `<span class="attr-name">${aname}</span><span class="tag-punc">${eq}</span><span class="attr-value">${full}</span>`;
              }
            );
            return `<span class="tag-punc">${open}</span><span class="tag-name">${name}</span>${attrHtml}<span class="tag-punc">${close}</span>`;
          }
        );

        return `<span class="ln">${i + 1}</span><span class="text-node">${escaped}</span>`;
      })
      .join("\n");
  }

  const codeContent = $("#codeContent");
  function renderXml(xml) {
    state.xmlText = xml;
    codeContent.innerHTML = highlightXml(xml);
  }

  $("#copyXmlBtn").addEventListener("click", async () => {
    if (!state.xmlText) return;
    try {
      await navigator.clipboard.writeText(state.xmlText);
      flashIconBtn($("#copyXmlBtn"));
    } catch (e) {
      /* clipboard permissions denied; nothing to fall back to reliably */
    }
  });

  function flashIconBtn(btn) {
    btn.style.color = "#4ade80";
    setTimeout(() => (btn.style.color = ""), 700);
  }

  $("#downloadXmlIconBtn").addEventListener("click", () => downloadXml());
  $("#downloadBtn").addEventListener("click", () => downloadXml());

  // Inside the desktop shell (main_gui.py), pywebview injects window.pywebview.api.
  // WebView2 doesn't support browser-style downloads (attachment responses, <a
  // download> blobs), so in that case we go through a native Save As dialog
  // instead. Plain-browser usage (running app.py directly) keeps the old path.
  function inDesktopShell() {
    return !!(window.pywebview && window.pywebview.api);
  }

  function showHint(msg, revertMs) {
    const el = $("#convertHint");
    const prev = el.textContent;
    el.textContent = msg;
    if (revertMs) setTimeout(() => { if (el.textContent === msg) el.textContent = prev; }, revertMs);
  }

  function downloadXml() {
    if (!state.jobId) return;
    if (inDesktopShell()) {
      window.pywebview.api.save_xml(state.jobId).then((res) => {
        if (res && res.ok) showHint(`Saved to ${res.path}`, 4000);
        else if (res && !res.cancelled) showHint("Save failed: " + (res.error || "unknown error"), 4000);
      });
      return;
    }
    window.location.href = `/api/jobs/${state.jobId}/download`;
  }

  // Export dropdown
  const exportBtn = $("#exportBtn");
  const exportMenu = $("#exportMenu");
  exportBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (exportBtn.disabled) return;
    exportMenu.hidden = !exportMenu.hidden;
  });
  document.addEventListener("click", () => (exportMenu.hidden = true));
  exportMenu.addEventListener("click", (e) => e.stopPropagation());
  $$("#exportMenu button").forEach((b) => {
    b.addEventListener("click", () => {
      exportMenu.hidden = true;
      if (b.dataset.export === "xml") {
        downloadXml();
        return;
      }

      if (inDesktopShell()) {
        window.pywebview.api.save_text(state.jobId).then((res) => {
          if (res && res.ok) showHint(`Saved to ${res.path}`, 4000);
          else if (res && !res.cancelled) showHint("Save failed: " + (res.error || "unknown error"), 4000);
        });
        return;
      }

      const text = state.xmlText.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
      const blob = new Blob([text], { type: "text/plain" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      const base = (state.filename || "output").replace(/\.pdf$/i, "");
      a.download = `${base}.txt`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  });

  // ---------------- Conversion ----------------
  convertBtn.addEventListener("click", startConversion);

  function currentOptions() {
    return {
      file_id: state.fileId,
      ocr_math: $("#ocrEngine").value === "auto",
      detect_tables: $("#optTables").checked,
      strip_header_footer: $("#optHeaders").checked,
    };
  }

  async function startConversion() {
    if (!state.fileId) return;
    resetConversionOutputs();
    convertBtn.disabled = true;
    $("#convertBtnLabel").textContent = "Converting...";
    $("#convertHint").textContent = "Starting conversion...";

    try {
      const res = await fetch("/api/convert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(currentOptions()),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to start conversion");
      state.jobId = data.job_id;
      state.seenLogCount = 0;
      pollJob();
      state.jobPollTimer = setInterval(pollJob, 900);
    } catch (err) {
      $("#convertHint").textContent = "Error: " + err.message;
      convertBtn.disabled = false;
      $("#convertBtnLabel").textContent = "Convert to XML";
    }
  }

  const logsPanel = $("#logsPanel");

  function appendLogs(logs) {
    if (state.seenLogCount === 0) logsPanel.innerHTML = "";
    for (let i = state.seenLogCount; i < logs.length; i++) {
      const entry = logs[i];
      const div = document.createElement("div");
      div.className = "log-line" + (/error/i.test(entry.msg) ? " error" : "");
      const t = new Date(entry.t * 1000);
      const ts = t.toLocaleTimeString();
      div.innerHTML = `<span class="ts">${ts}</span>${escapeHtml(entry.msg)}`;
      logsPanel.appendChild(div);
    }
    state.seenLogCount = logs.length;
    logsPanel.scrollTop = logsPanel.scrollHeight;
    if (logs.length) $("#convertHint").textContent = logs[logs.length - 1].msg;
  }

  async function pollJob() {
    if (!state.jobId) return;
    try {
      const res = await fetch(`/api/jobs/${state.jobId}`);
      const data = await res.json();
      appendLogs(data.logs || []);

      if (data.status === "done") {
        clearInterval(state.jobPollTimer);
        await finishJob(true);
      } else if (data.status === "error") {
        clearInterval(state.jobPollTimer);
        await finishJob(false, data.error);
      }
    } catch (err) {
      clearInterval(state.jobPollTimer);
      $("#convertHint").textContent = "Error while checking job status.";
      convertBtn.disabled = false;
      $("#convertBtnLabel").textContent = "Convert to XML";
    }
  }

  async function finishJob(success, errorMsg) {
    convertBtn.disabled = false;
    if (!success) {
      $("#convertBtnLabel").textContent = "Convert to XML";
      $("#convertHint").textContent = "Failed: " + (errorMsg || "unknown error");
      return;
    }

    $("#convertBtnLabel").textContent = "Convert Again";
    $("#convertHint").textContent = "Conversion complete.";
    $("#downloadBtn").disabled = false;
    $("#exportBtn").disabled = false;

    const [xmlRes, valRes] = await Promise.all([
      fetch(`/api/jobs/${state.jobId}/xml`),
      fetch(`/api/jobs/${state.jobId}/validation`),
    ]);
    const xmlData = await xmlRes.json();
    const valData = await valRes.json();

    if (xmlData.xml) renderXml(xmlData.xml);
    renderValidation(valData);
  }

  function renderValidation(data) {
    const panel = $("#validationPanel");
    const ok = data.well_formed;
    const statsEntries = [
      ["sections", "Sections"],
      ["paragraphs", "Paragraphs"],
      ["tables", "Tables"],
      ["formulas", "Formulas"],
      ["bold_runs", "Bold Runs"],
      ["figures", "Figures"],
    ];

    let html = `<div class="validation-summary ${ok ? "ok" : "fail"}">
      <svg class="icon"><use href="#${ok ? "i-check-circle" : "i-x-circle"}"/></svg>
      ${ok ? "XML is well-formed and valid JATS structure" : "XML failed validation"}
    </div>`;

    if (ok) {
      html += `<div class="validation-stats">${statsEntries
        .map(
          ([key, label]) =>
            `<div class="stat-tile"><div class="stat-num">${data.stats[key] ?? 0}</div><div class="stat-label">${label}</div></div>`
        )
        .join("")}</div>`;
    }

    if (data.errors && data.errors.length) {
      html += `<div class="validation-errors">${data.errors.map((e) => `<div>${escapeHtml(e)}</div>`).join("")}</div>`;
    }

    panel.innerHTML = html;
  }
})();
