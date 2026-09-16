"""Self-contained token-savings dashboard on the stdlib HTTP server.

No web framework dependency: ``ThreadingHTTPServer`` serves one HTML page
(vanilla JS, no CDN) plus a small JSON API. ``gryphon savings --serve``
starts it on localhost.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .savings_log import read_entries, summarize

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>gryphon savings</title>
<style>
  :root { --bg:#0f1115; --card:#181c24; --fg:#e6e8ec; --mut:#8b93a3; --acc:#5ee0a0; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  main { max-width:980px; margin:0 auto; padding:32px 20px 64px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--mut); margin-bottom:24px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
           gap:12px; margin-bottom:28px; }
  .card { background:var(--card); border:1px solid #242a36; border-radius:10px;
          padding:16px; }
  .card .num { font-size:26px; color:var(--acc); font-weight:600; }
  .card .lbl { color:var(--mut); font-size:12px; text-transform:uppercase;
               letter-spacing:.06em; }
  section { background:var(--card); border:1px solid #242a36; border-radius:10px;
            padding:18px; margin-bottom:20px; }
  h2 { font-size:14px; margin:0 0 14px; color:var(--mut);
       text-transform:uppercase; letter-spacing:.06em; }
  #chart { display:flex; align-items:flex-end; gap:4px; height:140px; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #242a36; }
  th { color:var(--mut); font-weight:500; }
  td.r,th.r { text-align:right; }
  .kind-measure { color:#7cc4ff; }
  .kind-tool_call { color:var(--mut); }
  form { display:grid; grid-template-columns:2fr 1fr 1fr auto; gap:8px;
         align-items:end; }
  input { background:#0f1115; border:1px solid #2a3140; color:var(--fg);
          border-radius:6px; padding:8px 10px; font:inherit; }
  button { background:var(--acc); color:#0f1115; border:0; border-radius:6px;
           padding:9px 16px; font:inherit; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:wait; }
  #measure-result { margin-top:12px; white-space:pre-wrap; color:var(--acc); }
  .err { color:#ff7b72; }
  .empty { color:var(--mut); padding:12px 0; }
</style>
</head>
<body>
<main>
  <h1>gryphon savings</h1>
  <div class="sub">estimated tokens saved by the knowledge graph</div>

  <div class="cards" id="cards"></div>

  <section>
    <h2>saved per day</h2>
    <div id="chart-labels" style="display:flex;gap:16px;margin-bottom:8px;font-size:11px;">
      <span><span style="display:inline-block;width:10px;height:10px;background:var(--acc);border-radius:2px;margin-right:4px;"></span>file reads</span>
      <span><span style="display:inline-block;width:10px;height:10px;background:#7cc4ff;border-radius:2px;margin-right:4px;"></span>search + analysis</span>
    </div>
    <div id="chart"></div>
  </section>

  <section>
    <h2>measure a diff</h2>
    <form id="mform">
      <input name="repo_root" placeholder="repo path (blank = auto-detect)" />
      <input name="base" placeholder="base ref" value="HEAD~1" />
      <input name="head" placeholder="head ref (optional)" />
      <button type="submit">measure</button>
    </form>
    <div id="measure-result"></div>
  </section>

  <section>
    <h2>recent entries</h2>
    <table id="entries">
      <thead><tr>
        <th>when</th><th>kind</th><th>tool / ref</th><th>repo</th>
        <th class="r">baseline</th><th class="r">returned</th>
        <th class="r">saved</th><th class="r">%</th>
        <th class="r">cf saved</th><th class="r">cf %</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </section>
</main>
<script>
const fmt = n => n == null ? "-" : n.toLocaleString("en-US");
const shortRepo = p => (p || "").split(/[\\\\/]/).filter(Boolean).pop() || p;
const shortTime = ts => (ts || "").replace("T", " ").slice(5, 16);

async function load() {
  const [sum, entries] = await Promise.all([
    fetch("/api/summary").then(r => r.json()),
    fetch("/api/entries?limit=100").then(r => r.json()),
  ]);

  const hasCf = sum.cf_total_saved > 0;
  const mainSaved = hasCf ? sum.cf_total_saved : sum.total_saved_tokens;
  const mainPct = hasCf ? sum.cf_total_saved_percent : sum.total_saved_percent;
  const mainLabel = hasCf ? "tokens saved (counterfactual)" : "tokens saved";
  document.getElementById("cards").innerHTML = [
    [fmt(mainSaved), mainLabel],
    [mainPct + "%", "avg reduction"],
    [fmt(hasCf ? sum.cf_total_baseline : sum.total_baseline_tokens), "naive baseline"],
    [fmt(sum.count), "entries"],
  ].map(([n, l]) =>
    `<div class="card"><div class="num">${n}</div><div class="lbl">${l}</div></div>`
  ).join("");

  const cfDays = sum.cf_by_day || {};
  const fileDays = sum.by_day || {};
  const allDayKeys = [...new Set([...Object.keys(fileDays), ...Object.keys(cfDays)])].sort();
  const dayData = allDayKeys.map(d => ({
    day: d,
    file: fileDays[d] || 0,
    cf: Math.max(0, (cfDays[d] || 0) - (fileDays[d] || 0)),
  }));
  const maxDay = Math.max(1, ...dayData.map(d => d.file + d.cf));
  document.getElementById("chart").innerHTML = dayData.length
    ? dayData.map(d => {
        const filePct = d.file / maxDay * 100;
        const cfPct = d.cf / maxDay * 100;
        const total = d.file + d.cf;
        return `<div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:stretch;height:100%;position:relative;gap:0" class="bar-col">
          <div style="height:${Math.max(0, cfPct)}%;background:#7cc4ff;border-radius:3px 3px 0 0;min-height:${d.cf ? 1 : 0}px;opacity:.85"></div>
          <div style="height:${Math.max(0, filePct)}%;background:var(--acc);border-radius:${d.cf ? '0' : '3px 3px'} 0 0;min-height:${d.file ? 1 : 0}px;opacity:.85"></div>
          <span style="position:absolute;bottom:100%;left:50%;transform:translateX(-50%);font-size:10px;color:var(--mut);white-space:nowrap;display:none;padding-bottom:2px">${d.day.slice(5)} · ${fmt(total)}</span>
        </div>`;
      }).join("")
    : '<div class="empty">no data yet</div>';

  // Hover for stacked bars
  document.querySelectorAll(".bar-col").forEach(col => {
    col.addEventListener("mouseenter", () => col.querySelector("span").style.display = "block");
    col.addEventListener("mouseleave", () => col.querySelector("span").style.display = "none");
  });

  const tbody = document.querySelector("#entries tbody");
  tbody.innerHTML = entries.length ? entries.map(e => {
    const ex = e.extra || {};
    const cfSaved = ex.total_saved_tokens;
    const cfPct = ex.total_saved_percent;
    return `<tr>
      <td>${shortTime(e.ts)}</td>
      <td class="kind-${e.kind}">${e.kind || ""}</td>
      <td>${[e.tool, e.ref].filter(Boolean).join(" · ")}</td>
      <td title="${e.repo || ""}">${shortRepo(e.repo)}</td>
      <td class="r">${fmt(e.baseline_tokens)}</td>
      <td class="r">${fmt(e.returned_tokens)}</td>
      <td class="r">${fmt(e.saved_tokens)}</td>
      <td class="r">${e.saved_percent ?? ""}%</td>
      <td class="r">${cfSaved != null ? fmt(cfSaved) : "-"}</td>
      <td class="r">${cfPct != null ? cfPct + "%" : "-"}</td>
    </tr>`;
  }).join("")
    : '<tr><td colspan="10" class="empty">no entries logged yet</td></tr>';
}

document.getElementById("mform").addEventListener("submit", async ev => {
  ev.preventDefault();
  const btn = ev.target.querySelector("button");
  const out = document.getElementById("measure-result");
  btn.disabled = true; out.textContent = "measuring…"; out.className = "";
  const fd = new FormData(ev.target);
  const body = {
    repo_root: fd.get("repo_root") || null,
    base: fd.get("base") || "HEAD~1",
    head: fd.get("head") || null,
  };
  try {
    const r = await fetch("/api/measure", {
      method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify(body),
    });
    const res = await r.json();
    if (res.status !== "ok") throw new Error(res.error || "measure failed");
    const cf = res.counterfactual || {};
    const cfLine = cf.total_counterfactual
      ? `\\ncounterfactual baseline: ${fmt(cf.total_counterfactual)} ` +
        `(search ${fmt(cf.search_trace_tokens)} + analysis ${fmt(cf.analysis_tokens)} + precision ${fmt(cf.precision_tokens)} + files ${fmt(cf.file_tokens)})` +
        `\\ncounterfactual saved: ${fmt(res.counterfactual_saved)} (~${res.counterfactual_saved_percent}%)`
      : "";
    out.textContent =
      `saved ${fmt(res.saved_tokens)} tokens (~${res.saved_percent}%) [file reads only]\\n` +
      `baseline ${fmt(res.baseline_tokens)} = ` +
      `${res.changed_files.length} changed (${fmt(res.changed_tokens)}) + ` +
      `${res.impacted_files.length} impacted (${fmt(res.impacted_tokens)})\\n` +
      `graph response: ${fmt(res.graph_tokens)} · ` +
      (res.verified ? "tiktoken verified" : "chars/4 estimate") +
      cfLine;
    load();
  } catch (e) {
    out.textContent = String(e.message || e);
    out.className = "err";
  } finally {
    btn.disabled = false;
  }
});

load();
</script>
</body>
</html>
"""


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, default=str).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server_version = "gryphon-savings/1.0"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, _json_bytes(payload), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        route, qs = parsed.path, parse_qs(parsed.query)
        if route == "/":
            self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route == "/api/summary":
            self._json(summarize(read_entries()))
        elif route == "/api/entries":
            limit = int(qs.get("limit", ["100"])[0])
            self._json(list(reversed(read_entries()))[:limit])
        elif route == "/api/repos":
            repos = sorted({e.get("repo", "") for e in read_entries()} - {""})
            self._json(repos)
        else:
            self._json({"error": "not found"}, code=404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        if parsed.path != "/api/measure":
            self._json({"error": "not found"}, code=404)
            return
        try:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"status": "error", "error": "invalid JSON body"}, 400)
            return
        try:
            from .measure import measure_savings

            result = measure_savings(
                body.get("repo_root") or None,
                changed_files=body.get("files") or None,
                base=body.get("base") or "HEAD~1",
                head=body.get("head") or None,
                ref=body.get("ref") or None,
            )
            self._json(result)
        except Exception as exc:  # noqa: BLE001 - surface as JSON error
            logger.warning("measure failed: %s", exc)
            self._json({"status": "error", "error": str(exc)}, code=500)

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.debug("dashboard: " + fmt, *args)


def serve_dashboard(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the savings dashboard; blocks until Ctrl+C."""
    server = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    print(f"gryphon savings dashboard → {url}  (Ctrl+C to stop)")
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - browser is best-effort
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
