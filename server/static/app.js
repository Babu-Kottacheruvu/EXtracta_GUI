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

  // ---------------- Theme toggle ----------------
  // The <head> inline script already applied the stored theme before paint
  // (avoids a flash of the wrong theme); this just wires up the button.
  $("#themeToggleBtn").addEventListener("click", () => {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const next = isDark ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("theme", next);
    } catch (e) {}
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
    $("#renderedContent").innerHTML =
      '<div class="empty-state"><svg class="icon empty-icon"><use href="#i-sigma"/></svg><div>Run a conversion to see the document with formulas rendered as real math notation</div></div>';
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

  // ---------------- OpenRouter API key (LaTeX -> MathML via LLM) ----------------
  const optMathml = $("#optMathml");
  const apiKeyRow = $("#apiKeyRow");
  const openrouterApiKeyInput = $("#openrouterApiKeyInput");
  const saveApiKeyBtn = $("#saveApiKeyBtn");
  const apiKeyStatus = $("#apiKeyStatus");
  let apiKeyConfigured = false;

  function setApiKeyStatus(text, kind) {
    apiKeyStatus.textContent = text;
    apiKeyStatus.className = "api-key-status" + (kind ? " " + kind : "");
  }

  async function refreshApiKeyStatus() {
    try {
      const res = await fetch("/api/settings/openrouter-api-key");
      const data = await res.json();
      apiKeyConfigured = !!data.configured;
      if (apiKeyConfigured) {
        openrouterApiKeyInput.placeholder = `Saved (${data.masked})`;
        setApiKeyStatus("Key saved -- used automatically on every conversion. Paste a new one here only to replace it.", "ok");
      } else {
        setApiKeyStatus("No API key saved yet. Paste one below and click Save -- you won't need to re-enter it after that.");
      }
    } catch (e) {
      setApiKeyStatus("Could not check API key status.", "error");
    } finally {
      // The "Saved (****)" placeholder is just a hint, not a live value --
      // an empty field means "keep the existing key," so Save should be
      // disabled until the user actually types a new one (otherwise a stray
      // click reads as an alarming "Enter a key first" error).
      saveApiKeyBtn.disabled = !openrouterApiKeyInput.value.trim();
    }
  }

  openrouterApiKeyInput.addEventListener("input", () => {
    saveApiKeyBtn.disabled = !openrouterApiKeyInput.value.trim();
  });

  optMathml.addEventListener("change", () => {
    apiKeyRow.hidden = !optMathml.checked;
  });

  saveApiKeyBtn.addEventListener("click", async () => {
    const key = openrouterApiKeyInput.value.trim();
    if (!key) return;
    saveApiKeyBtn.disabled = true;
    try {
      const res = await fetch("/api/settings/openrouter-api-key", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: key }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to save key");
      apiKeyConfigured = true;
      openrouterApiKeyInput.value = "";
      openrouterApiKeyInput.placeholder = `Saved (${data.masked})`;
      setApiKeyStatus("API key saved.", "ok");
      showToast("API key saved successfully", "success");
    } catch (err) {
      setApiKeyStatus("Error: " + err.message, "error");
      showToast("Failed to save API key", "error");
    } finally {
      saveApiKeyBtn.disabled = !openrouterApiKeyInput.value.trim();
    }
  });

  refreshApiKeyStatus();

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

  // escapeHtml alone isn't safe inside a double-quoted HTML attribute (it
  // doesn't touch '"'), which highlightXml's own attribute-coloring regex
  // relies on -- adding quote-escaping there would silently stop XML
  // attribute values (all quoted) from getting their color spans. Formula
  // source text is MathML/LaTeX, which quotes its own attributes
  // (display="inline") and must not break out of data-copy="..." below, so
  // it gets this stricter escaping instead, kept separate from escapeHtml.
  function escapeAttr(s) {
    return escapeHtml(s).replace(/"/g, "&quot;");
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

  function showToast(message, type = "success", duration = 4000) {
    let container = document.querySelector(".toast-container");
    if (!container) {
      container = document.createElement("div");
      container.className = "toast-container";
      document.body.appendChild(container);
    }
    const toast = document.createElement("div");
    toast.className = `toast ${type}`;
    const iconId = type === "error" ? "i-x-circle" : "i-check-circle";
    toast.innerHTML = `<svg class="icon"><use href="#${iconId}"/></svg><span></span>`;
    toast.querySelector("span").textContent = message;
    container.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("show"));
    setTimeout(() => {
      toast.classList.remove("show");
      setTimeout(() => toast.remove(), 250);
    }, duration);
  }

  function downloadXml() {
    if (!state.jobId) return;
    if (inDesktopShell()) {
      window.pywebview.api.save_xml(state.jobId).then((res) => {
        if (res && res.ok) {
          showHint(`Saved to ${res.path}`, 4000);
          showToast("File downloaded successfully", "success");
        } else if (res && !res.cancelled) {
          showHint("Save failed: " + (res.error || "unknown error"), 4000);
          showToast("Download failed: " + (res.error || "unknown error"), "error");
        }
      });
      return;
    }
    window.location.href = `/api/jobs/${state.jobId}/download`;
    showToast("File downloaded successfully", "success");
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

      if (b.dataset.export === "epub") {
        if (inDesktopShell()) {
          window.pywebview.api.save_epub(state.jobId).then((res) => {
            if (res && res.ok) {
              showHint(`Saved to ${res.path}`, 4000);
              showToast("File downloaded successfully", "success");
            } else if (res && !res.cancelled) {
              showHint("Save failed: " + (res.error || "unknown error"), 4000);
              showToast("Download failed: " + (res.error || "unknown error"), "error");
            }
          });
          return;
        }
        window.location.href = `/api/jobs/${state.jobId}/epub`;
        showToast("File downloaded successfully", "success");
        return;
      }

      if (inDesktopShell()) {
        window.pywebview.api.save_text(state.jobId).then((res) => {
          if (res && res.ok) {
            showHint(`Saved to ${res.path}`, 4000);
            showToast("File downloaded successfully", "success");
          } else if (res && !res.cancelled) {
            showHint("Save failed: " + (res.error || "unknown error"), 4000);
            showToast("Download failed: " + (res.error || "unknown error"), "error");
          }
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
      showToast("File downloaded successfully", "success");
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
      latex_to_mathml: optMathml.checked,
    };
  }

  async function startConversion() {
    if (!state.fileId) return;
    if (optMathml.checked && !apiKeyConfigured) {
      showToast("Save an OpenRouter API key first, or uncheck AI repair", "error");
      moreOptions.hidden = false;
      moreOptionsToggle.classList.add("open");
      apiKeyRow.hidden = false;
      openrouterApiKeyInput.focus();
      return;
    }
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
      showToast("Conversion failed: " + (errorMsg || "unknown error"), "error");
      return;
    }

    $("#convertBtnLabel").textContent = "Convert Again";
    $("#convertHint").textContent = "Conversion complete.";
    $("#downloadBtn").disabled = false;
    $("#exportBtn").disabled = false;
    showToast("File converted successfully", "success");

    const [xmlRes, valRes] = await Promise.all([
      fetch(`/api/jobs/${state.jobId}/xml`),
      fetch(`/api/jobs/${state.jobId}/validation`),
    ]);
    const xmlData = await xmlRes.json();
    const valData = await valRes.json();

    if (xmlData.xml) {
      renderXml(xmlData.xml);
      renderJatsPreview(xmlData.xml);
    }
    renderValidation(valData);
  }

  // ---------------- Rendered math preview ----------------
  // Turns the JATS XML into HTML so MathJax can typeset the formulas for a
  // real visual check, instead of the flat <tex-math>/<math> text a raw XML
  // view or a generic JATS-HTML viewer shows. tex-math is wrapped in \( \)
  // so MathJax's TeX input processes it; each formula's self-namespaced
  // <math xmlns="..."> is passed through as literal markup for MathJax's
  // native MathML input.
  function jatsNodeToHtml(node) {
    let out = "";
    for (const child of node.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        out += escapeHtml(child.nodeValue);
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;

      const tag = child.tagName.toLowerCase();
      switch (tag) {
        case "title":
          continue; // handled by the parent sec/table-wrap below
        case "sec": {
          const titleEl = child.querySelector(":scope > title");
          const depth = Math.min(6, (Number(child.getAttribute("sec-level")) || 2) + 1);
          const level = Math.max(2, depth);
          if (titleEl) out += `<h${level}>${escapeHtml(titleEl.textContent)}</h${level}>`;
          out += jatsNodeToHtml(child);
          break;
        }
        case "p":
          out += `<p>${jatsNodeToHtml(child)}</p>`;
          break;
        case "bold":
          out += `<b>${jatsNodeToHtml(child)}</b>`;
          break;
        case "italic":
          out += `<i>${jatsNodeToHtml(child)}</i>`;
          break;
        case "styled-content":
          out += `<span>${jatsNodeToHtml(child)}</span>`;
          break;
        case "inline-formula": {
          // Namespace-qualified lookup, not querySelector("math") -- each
          // formula's <math> carries a real xmlns (or, for an older
          // conversion, inherits one via an ancestor's xmlns:mml), so it's
          // genuinely in the MathML namespace once parsed, and relying on
          // querySelector's namespace-agnostic local-name matching for
          // that isn't guaranteed the same way across engines.
          const mmlMatches = child.getElementsByTagNameNS("http://www.w3.org/1998/Math/MathML", "math");
          const mml = mmlMatches.length ? mmlMatches[0] : null;
          const tex = child.querySelector("tex-math");
          let rendered = "";
          let sourceCode = "";
          let sourceLabel = "";
          if (mml) {
            // Each formula's <math> now carries its own xmlns directly, so
            // this is normally already a self-contained, valid snippet as
            // -is. Still guarded for an older conversion whose <math> was
            // only ever namespaced via an ancestor's xmlns:mml: stripping
            // that "mml:" prefix would otherwise leave a bare, unnamespaced
            // <math> that a strict XML/XSLT consumer (or a JATS/EPUB
            // round-trip) won't recognize as MathML at all, so re-add
            // xmlns here -- but only when the tag doesn't already have one,
            // to avoid emitting a duplicate attribute.
            let mmlMarkup = mml.outerHTML.replace(/<(\/?)mml:/gi, "<$1");
            const openTagEnd = mmlMarkup.indexOf(">") + 1;
            if (!/\bxmlns\s*=/.test(mmlMarkup.slice(0, openTagEnd))) {
              mmlMarkup = mmlMarkup.replace(/^<math(?=[\s>])/, '<math xmlns="http://www.w3.org/1998/Math/MathML"');
            }
            rendered = mmlMarkup;
            // mml.outerHTML reproduces the source XML's pretty-printing
            // verbatim -- every indent and line break between elements is a
            // real whitespace text node -- so the "source" pill would
            // otherwise sprawl across many lines instead of reading as one
            // compact, copiable snippet.
            sourceCode = mmlMarkup.replace(/\s+/g, " ").trim();
            sourceLabel = "MathML";
          } else if (tex) {
            rendered = `\\(${tex.textContent}\\)`;
            sourceCode = tex.textContent.replace(/\s+/g, " ").trim();
            sourceLabel = "LaTeX";
          }
          if (rendered) {
            out += `<span class="formula-wrap">${rendered}`;
            if (sourceCode) {
              out += `<code class="formula-code" title="${sourceLabel} source">${escapeHtml(sourceCode)}</code>`;
              out += `<button type="button" class="formula-copy-btn" data-copy="${escapeAttr(sourceCode)}" title="Copy ${sourceLabel} source"><svg class="icon"><use href="#i-copy"/></svg></button>`;
            }
            out += `</span>`;
          }
          break;
        }
        case "table-wrap": {
          const titleEl = child.querySelector(":scope > title");
          if (titleEl) out += `<div class="table-caption"><b>${escapeHtml(titleEl.textContent)}</b></div>`;
          out += jatsNodeToHtml(child);
          break;
        }
        case "table":
        case "thead":
        case "tbody":
        case "tr":
          out += `<${tag}>${jatsNodeToHtml(child)}</${tag}>`;
          break;
        case "th":
        case "td": {
          const attrs = [];
          if (child.hasAttribute("colspan")) attrs.push(`colspan="${child.getAttribute("colspan")}"`);
          if (child.hasAttribute("rowspan")) attrs.push(`rowspan="${child.getAttribute("rowspan")}"`);
          out += `<${tag} ${attrs.join(" ")}>${jatsNodeToHtml(child)}</${tag}>`;
          break;
        }
        case "fig": {
          const graphic = child.querySelector("graphic");
          const href = graphic ? graphic.getAttributeNS("http://www.w3.org/1999/xlink", "href") || graphic.getAttribute("xlink:href") : "";
          out += `<div class="rendered-fig">Image placeholder${href ? `: ${escapeHtml(href)}` : ""}</div>`;
          break;
        }
        default:
          out += jatsNodeToHtml(child);
      }
    }
    return out;
  }

  function renderJatsPreview(xmlText) {
    const container = $("#renderedContent");
    let doc;
    try {
      doc = new DOMParser().parseFromString(xmlText, "application/xml");
      const parseError = doc.querySelector("parsererror");
      if (parseError) throw new Error(parseError.textContent);
    } catch (err) {
      container.innerHTML = `<div class="parse-error">Could not render preview: ${escapeHtml(err.message)}</div>`;
      return;
    }

    const body = doc.querySelector("body") || doc.documentElement;
    container.innerHTML = jatsNodeToHtml(body) || "<p>(No content)</p>";
    typesetMath(container);
  }

  // One delegated listener for every formula's copy button, rather than
  // one per button -- the buttons are rebuilt from scratch on every
  // renderJatsPreview() call, so anything bound directly to them would need
  // rebinding right after (easy to forget, and it's the container that's
  // stable across re-renders, not its contents).
  $("#renderedContent").addEventListener("click", async (e) => {
    const btn = e.target.closest(".formula-copy-btn");
    if (!btn) return;
    try {
      await navigator.clipboard.writeText(btn.dataset.copy || "");
      flashIconBtn(btn);
    } catch (err) {
      /* clipboard permissions denied; nothing to fall back to reliably */
    }
  });

  // The MathJax script tag is deferred, and even once it executes it still
  // has async startup work (building the output jax, etc.) before
  // typesetPromise exists -- calling renderJatsPreview() right after a fast
  // conversion can easily race that, silently skipping typesetting and
  // leaving raw "\(...\)" text on screen. Wait on MathJax.startup.promise
  // when typesetPromise isn't there yet instead of just giving up.
  function typesetMath(container) {
    if (!window.MathJax) return;
    const run = () => window.MathJax.typesetPromise([container]).catch(() => {});
    if (window.MathJax.typesetPromise) {
      run();
    } else if (window.MathJax.startup && window.MathJax.startup.promise) {
      window.MathJax.startup.promise.then(run).catch(() => {});
    }
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
