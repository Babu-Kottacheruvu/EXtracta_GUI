// Vercel build script for the standalone frontend copy.
//
// There is only ONE source of truth for the UI: server/templates/index.html
// (rendered by Flask when the backend is opened directly) plus
// server/static/{app.js,style.css,vendor/...} (served by Flask too). This
// script does NOT duplicate any of that -- it takes the same index.html
// Flask uses, and:
//   1. rewrites its Flask url_for(...) asset references into absolute URLs
//      pointing at the live Render backend's own /static/ files (so CSS/JS
//      keep loading straight from Render -- editing app.js/style.css there
//      automatically reaches this Vercel copy too, no re-deploy needed here)
//   2. injects window.EXTRACTA_API_BASE so app.js's fetch calls (see
//      server/static/app.js) target that same backend instead of same-origin
//
// RENDER_URL must be set as an environment variable on the Vercel project
// (Project Settings -> Environment Variables) to your real backend URL,
// e.g. https://extracta.onrender.com -- no trailing slash.
const fs = require("fs");
const path = require("path");

const RENDER_URL = (process.env.RENDER_URL || "").replace(/\/+$/, "");
if (!RENDER_URL) {
  console.error(
    "RENDER_URL environment variable is not set. Add it in the Vercel " +
    "project's Environment Variables (e.g. https://extracta.onrender.com) and redeploy."
  );
  process.exit(1);
}

const srcHtml = path.join(__dirname, "..", "server", "templates", "index.html");
let html = fs.readFileSync(srcHtml, "utf8");

html = html.replace(
  /\{\{\s*url_for\(\s*'static'\s*,\s*filename=['"]([^'"]+)['"]\s*\)\s*\}\}/g,
  (_, file) => `${RENDER_URL}/static/${file}`
);

html = html.replace(
  "<head>",
  `<head>\n<script>window.EXTRACTA_API_BASE = ${JSON.stringify(RENDER_URL)};</script>`
);

const outDir = path.join(__dirname, "public");
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, "index.html"), html);

console.log(`Built web/public/index.html pointing at ${RENDER_URL}`);
