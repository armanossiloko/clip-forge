// ClipForge front-end: plain JavaScript, no build step.
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (s) => { s = Math.max(0, +s || 0); const m = Math.floor(s / 60); return `${m}:${(s - m * 60).toFixed(1).padStart(4, "0")}`; };
// "83.5", "1:23.5" or "1:01:23.5" → seconds (NaN if it can't be read)
const parseTime = (v) => { v = String(v).trim(); if (!v) return NaN; return v.split(":").reduce((acc, p) => acc * 60 + (p.trim() === "" ? NaN : +p), 0); };

const state = { jobs: [], selected: null, viewJobId: null };

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const res = await fetch(path, init);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), isErr ? 6000 : 2600);
}

// ---------------------------------------------------------------- health
const FIX_NATIVE = {
  ffmpeg: "Install ffmpeg: open PowerShell and run  winget install Gyan.FFmpeg  then restart ClipForge.",
  claude: "Install Claude Code: in PowerShell run  irm https://claude.ai/install.ps1 | iex  then run  claude  once to log in, and restart ClipForge.",
  js: "YouTube downloads need Deno: in PowerShell run  winget install DenoLand.Deno  then restart ClipForge.",
};
const FIX_DOCKER = {
  ffmpeg: "ffmpeg is missing from the Docker image. Rebuild it:  docker compose up -d --build",
  claude: "Claude Code is missing from the Docker image. Rebuild it:  docker compose up -d --build",
  claude_login: "Claude has no login. On your PC run  claude setup-token, put the token in .env as CLAUDE_CODE_OAUTH_TOKEN=..., then run  docker compose up -d",
  js: "The YouTube runtime (Node) is missing from the image. Rebuild it:  docker compose up -d --build",
};
let FIX = FIX_NATIVE;
async function loadHealth() {
  try {
    const h = await api("/api/health");
    state.docker = !!h.docker;
    FIX = h.docker ? FIX_DOCKER : FIX_NATIVE;
    const claudeOk = h.claude && (!h.docker || h.claude_login);
    const items = [
      ["ffmpeg", "ffmpeg", h.ffmpeg, "Installed"],
      [h.claude ? "claude_login" : "claude", h.claude && h.docker && !h.claude_login ? "Claude login" : "Claude Code", claudeOk, h.claude_version || "Installed"],
      ["js", "YouTube runtime", !!h.js_runtime, h.js_runtime ? `Using ${h.js_runtime}` : ""],
    ];
    if (h.docker) $("#urlInput").placeholder = "Paste a YouTube link, or the name of a file in your videos folder";
    $("#health").innerHTML = items.map(([k, label, ok, okTip]) =>
      `<span class="pill ${ok ? "" : "bad"}" data-k="${k}" title="${esc(ok ? okTip : FIX[k] || "")}">${label}${ok ? "" : " missing"}</span>`).join("")
      + (h.docker ? `<span class="pill docker" title="Running inside Docker">Docker</span>` : "");
    $("#health").querySelectorAll(".pill.bad").forEach((p) => (p.onclick = () => toast(FIX[p.dataset.k] || "", true)));
  } catch {}
}

// ---------------------------------------------------------------- "what to make" (brief) fields
const MODES = [
  ["top", "Top moments", "Claude picks the strongest self-contained moments."],
  ["funny", "Funniest bits", "Jokes, punchlines and banter, from the setup to the laugh."],
  ["takes", "Hot takes", "Bold opinions that get people arguing in the comments."],
  ["tips", "Tips & how-tos", "Complete, practical tips a viewer can use."],
  ["stories", "Stories", "Anecdotes with a setup and a payoff."],
  ["quotes", "Short quotes", "Short, punchy lines that work on their own."],
  ["cuts", "Cuts (part of the video → reels)", "No picking: the part you choose (or the whole video) becomes reels as it is, split between sentences when it's longer than the max length. Claude only writes the titles."],
  ["custom", "Custom: tell Claude what to find", "Describe what you want, and Claude finds the moments that match."],
];
const QUOTE_LEN = [8, 20];

function briefHTML() {
  return `
    <label class="bf-label">What to make
      <select data-b="mode">${MODES.map(([v, l]) => `<option value="${v}">${esc(l)}</option>`).join("")}</select></label>
    <p class="bf-help"></p>
    <textarea data-b="instructions" rows="2"></textarea>
    <details class="bf-more">
      <summary></summary>
      <div class="bf-grid">
        <label data-for="count">Reels <input type="number" data-b="num_clips" min="1" max="30"></label>
        <label data-for="min">Min length (s) <input type="number" data-b="min_seconds" min="3" max="180"></label>
        <label>Max length (s) <input type="number" data-b="max_seconds" min="5" max="180"></label>
        <label>From <input type="text" data-b="range_start" placeholder="start"></label>
        <label>To <input type="text" data-b="range_end" placeholder="end"></label>
      </div>
      <p class="bf-note">From / To: only use this part of the video, e.g. 2:00 to 5:30. Leave empty for the whole video.</p>
    </details>`;
}

// Build the fields inside `root`, filled from `b` (a job's brief, or Settings for a new video).
function setupBrief(root, b, defaults) {
  root.innerHTML = briefHTML();
  const f = (k) => root.querySelector(`[data-b=${k}]`);
  const set = (k, v) => { f(k).value = v ?? ""; };
  set("mode", b.mode || "top");
  set("instructions", b.instructions || "");
  set("num_clips", b.num_clips ?? defaults.num_clips);
  set("min_seconds", b.min_seconds ?? defaults.min_seconds);
  set("max_seconds", b.max_seconds ?? defaults.max_seconds);
  set("range_start", b.range ? fmtShort(b.range[0]) : "");
  set("range_end", b.range && b.range[1] != null ? fmtShort(b.range[1]) : "");
  if (b.range) root.querySelector("details").open = true;
  let prevMode = f("mode").value;
  const sync = () => {
    const mode = f("mode").value;
    if (mode !== prevMode) {  // short quotes want short clips; switching back restores the defaults
      if (mode === "quotes") { set("min_seconds", QUOTE_LEN[0]); set("max_seconds", QUOTE_LEN[1]); }
      else if (prevMode === "quotes") { set("min_seconds", defaults.min_seconds); set("max_seconds", defaults.max_seconds); }
      prevMode = mode;
    }
    const cuts = mode === "cuts";
    root.querySelector(".bf-help").textContent = MODES.find(([v]) => v === mode)[2];
    f("instructions").placeholder = mode === "custom"
      ? "e.g. Every moment where she talks about food in space"
      : cuts ? "Anything for the titles? (optional) e.g. Write them in Bosnian"
      : "Anything specific? (optional) e.g. Only moments about Mars";
    root.querySelector("[data-for=count]").hidden = cuts;
    root.querySelector("[data-for=min]").hidden = cuts;
    const from = f("range_start").value.trim(), to = f("range_end").value.trim();
    const part = from || to ? `${from || "start"}–${to || "end"}` : "whole video";
    root.querySelector("summary").textContent = cuts
      ? `Reels up to ${f("max_seconds").value}s · ${part}`
      : `${f("num_clips").value} reels · ${f("min_seconds").value}–${f("max_seconds").value}s · ${part}`;
  };
  root.querySelectorAll("[data-b]").forEach((el) => el.addEventListener("input", sync));
  sync();
}

const fmtShort = (s) => fmt(s).replace(/\.0$/, "");

// Read the fields back. Throws an Error with a friendly message if something can't be read.
function readBrief(root) {
  const v = (k) => root.querySelector(`[data-b=${k}]`).value.trim();
  const time = (k, label) => {
    const raw = v(k);
    if (!raw) return null;
    const t = parseTime(raw);
    if (!isFinite(t) || t < 0) throw new Error(`${label} must look like 2:30 or 150`);
    return t;
  };
  const b = { mode: v("mode"), instructions: v("instructions"),
    num_clips: +v("num_clips"), min_seconds: +v("min_seconds"), max_seconds: +v("max_seconds") };
  const from = time("range_start", "From"), to = time("range_end", "To");
  if (from != null) b.range_start = from;
  if (to != null) b.range_end = to;
  if (b.mode === "custom" && !b.instructions) throw new Error("Tell Claude what to look for in the box under the mode.");
  return b;
}

let briefDefaults = { num_clips: 5, min_seconds: 20, max_seconds: 60 };
async function loadBriefDefaults() {
  try { const s = await api("/api/settings"); briefDefaults = { num_clips: s.num_clips, min_seconds: s.min_seconds, max_seconds: s.max_seconds }; } catch {}
  let keep = {};
  try { keep = readBrief($("#newBrief")); } catch {}  // keep what's typed in, except the numbers
  setupBrief($("#newBrief"), { mode: keep.mode, instructions: keep.instructions }, briefDefaults);
}
setupBrief($("#newBrief"), {}, briefDefaults);

// ---------------------------------------------------------------- new jobs
$("#urlForm").onsubmit = async (e) => {
  e.preventDefault();
  const v = $("#urlInput").value.trim();
  if (!v) return;
  let brief;
  try { brief = readBrief($("#newBrief")); } catch (err) { return toast(err.message, true); }
  const body = /^https?:\/\//i.test(v) ? { url: v, brief } : { path: v, brief };
  try {
    const job = await api("/api/jobs", { json: body });
    $("#urlInput").value = "";
    state.selected = job.id;
    await refresh();
  } catch (err) { toast(err.message, true); }
};

const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
$("#fileInput").onchange = (e) => e.target.files[0] && upload(e.target.files[0]);

function upload(file) {
  let brief;
  try { brief = readBrief($("#newBrief")); } catch (err) { $("#fileInput").value = ""; return toast(err.message, true); }
  const fd = new FormData();
  fd.append("file", file);
  fd.append("brief", JSON.stringify(brief));
  const xhr = new XMLHttpRequest();
  const status = $("#uploadStatus");
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) status.textContent = `Uploading ${file.name}… ${Math.round(e.loaded / e.total * 100)}%`; };
  xhr.onload = async () => {
    status.textContent = "";
    if (xhr.status >= 200 && xhr.status < 300) {
      state.selected = JSON.parse(xhr.responseText).id;
      await refresh();
    } else {
      let m = xhr.statusText; try { m = JSON.parse(xhr.responseText).detail; } catch {}
      toast(m, true);
    }
  };
  xhr.onerror = () => { status.textContent = ""; toast("Upload failed", true); };
  xhr.open("POST", "/api/jobs/upload");
  xhr.send(fd);
  $("#fileInput").value = "";
}

// ---------------------------------------------------------------- job list
function jobTitle(j) { return j.source.title || j.source.value || "Untitled"; }

function renderJobList() {
  const ul = $("#jobList");
  if (!state.jobs.length) { ul.innerHTML = `<li class="s" style="color:var(--muted);padding:6px 4px">Nothing yet.</li>`; return; }
  ul.innerHTML = state.jobs.map((j) => {
    const prog = j.status === "running" ? `<div class="mini"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div>` : "";
    const n = j.clips.length, exported = j.clips.filter((c) => c.export_state === "exported").length;
    const sub = j.status === "error" ? "Failed: " + (j.error || "").split("\n")[0]
      : j.status === "done" ? `${n} reel${n === 1 ? "" : "s"}${exported ? ` · ${exported} generated` : ""}${j.video ? " · " + fmt(j.video.duration) : ""}` : j.message;
    return `<li class="job-item ${j.id === state.selected ? "active" : ""}" data-id="${j.id}">
      <span class="t">${esc(jobTitle(j))}</span><span class="dot ${j.status}"></span>
      <span class="s">${esc(sub)}</span>${prog}</li>`;
  }).join("");
  ul.querySelectorAll(".job-item").forEach((li) => (li.onclick = () => { state.selected = li.dataset.id; renderAll(); }));
}

// ---------------------------------------------------------------- job view
const STAGES = [["downloading", "Get video"], ["transcribing", "Transcribe"], ["analyzing", "Claude finds the reels"]];
const ASK_EXAMPLES = ["Give me 3 more reels like the best one", "Make reel 1 shorter and end on the punchline",
  "Turn 1:00 to 1:45 into a reel", "Delete the weakest reel"];

function renderJob() {
  const job = state.jobs.find((j) => j.id === state.selected);
  $("#emptyState").hidden = !!job;
  const view = $("#jobView");
  view.hidden = !job;
  if (!job) { state.viewJobId = null; ws.unmount(); return; }

  if (state.viewJobId !== job.id) {
    state.viewJobId = job.id;
    view.innerHTML = `
      <div class="job-head">
        <div><h1 id="jvTitle"></h1><div class="sub" id="jvSub"></div></div>
        <div class="actions">
          <button class="btn" id="jvReanalyze" title="Replace the draft reels with a new set (generated reels are kept)">New set…</button>
          <button class="btn" id="jvFolder" title="Where the generated files are kept">Open folder</button>
          <button class="btn primary" id="jvExportAll"></button>
          <button class="btn ghost danger" id="jvDelete" title="Delete this video and everything made from it">Delete video</button>
        </div>
      </div>
      <div class="stepper" id="jvSteps"></div>
      <div class="status-line" id="jvStatus"></div>
      <div id="jvError"></div>
      <section class="ws" id="ws"></section>
      <section class="ask" id="jvAsk" hidden>
        <div class="ask-head"><h2>Ask ClipForge</h2><span class="hint">Claude sees the whole transcript and every reel. Ask for more reels, changes, or anything about the video. Changes stay drafts until you press Generate.</span></div>
        <div class="chat" id="jvChat"></div>
        <form class="ask-form" id="jvAskForm">
          <textarea id="jvAskInput" rows="2" placeholder="e.g. Give me 3 more reels about food in space   (Enter sends, Shift+Enter for a new line)"></textarea>
          <button class="btn primary" id="jvAskSend">Send</button>
        </form>
        <div class="chips">${ASK_EXAMPLES.map((t) => `<button type="button" class="chip">${esc(t)}</button>`).join("")}</div>
      </section>`;
    $("#jvDelete").onclick = async () => {
      if (!confirm("Delete this video, all its reels and every generated file?")) return;
      await api(`/api/jobs/${job.id}`, { method: "DELETE" });
      state.selected = null; await refresh();
    };
    $("#jvFolder").onclick = () => api(`/api/jobs/${job.id}/open-folder`, { method: "POST" })
      .then((r) => toast(r.docker ? `Your generated reels are in the ClipForge folder under ${r.path}` : "Opened " + r.path))
      .catch((e) => toast(e.message, true));
    $("#jvReanalyze").onclick = () => openBriefDialog(state.jobs.find((j) => j.id === job.id) || job);
    // Generate all: make every reel that needs it, then download them together as one zip.
    // When they're all made already, the same button just downloads the zip.
    $("#jvExportAll").onclick = async () => {
      const j = state.jobs.find((x) => x.id === job.id) || job;
      const todo = j.clips.filter((c) => ["draft", "changed", "error"].includes(c.export_state)).length;
      if (!todo) return downloadFile(`/api/jobs/${job.id}/zip?v=${Date.now()}`);
      try {
        ws.zipWhenDone = true;
        applyJob(await api(`/api/jobs/${job.id}/render-all`, { method: "POST" }));
        toast(`Generating ${todo} reel${todo === 1 ? "" : "s"}. They download as one zip when they're all ready.`);
      } catch (e) { ws.zipWhenDone = false; toast(e.message, true); }
    };
    $("#jvAskForm").onsubmit = (e) => { e.preventDefault(); sendAsk(job.id); };
    $("#jvAskInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendAsk(job.id); }
    });
    $("#jvAsk").querySelectorAll(".chip").forEach((c) => (c.onclick = () => { const i = $("#jvAskInput"); i.value = c.textContent; i.focus(); }));
    state.chatKey = null;
    ws.mount($("#ws"), job);
  }

  $("#jvTitle").textContent = jobTitle(job);
  const v = job.video;
  const src = job.source.type === "url" ? `<a href="${esc(job.source.value)}" target="_blank" rel="noreferrer">${esc(job.source.value)}</a>` : esc(job.source.type === "file" ? "Uploaded file" : job.source.value);
  $("#jvSub").innerHTML = `${src}${v ? ` · ${fmt(v.duration)} · ${v.width}×${v.height}` : ""}`
    + (job.brief_label ? `<div class="brief-label">${esc(job.brief_label)}${job.brief?.instructions ? ` · “${esc(job.brief.instructions)}”` : ""}</div>` : "");
  const working = job.status === "running" || job.status === "queued";
  $("#jvReanalyze").disabled = working || !job.has_transcript;
  const todo = job.clips.filter((c) => c.export_state === "draft" || c.export_state === "changed" || c.export_state === "error").length;
  const exporting = job.clips.filter((c) => c.export_state === "exporting").length;
  const made = job.clips.filter((c) => c.video_url).length;
  $("#jvExportAll").textContent = exporting ? `Generating ${exporting}…` : todo ? `⬇ Generate all (${todo})` : made ? "⬇ Download all (zip)" : "⬇ Generate all";
  $("#jvExportAll").title = todo ? "Make the video file of every reel that needs it, then download them all as one zip" : "Download every generated reel as one zip";
  $("#jvExportAll").disabled = !!exporting || (!todo && !made);

  // stepper: only while the video is being prepared (or something failed)
  const finished = job.status === "done" || job.stage === "asking";
  const idx = finished ? STAGES.length : STAGES.findIndex(([k]) => k === job.stage);
  const cuts = job.brief?.mode === "cuts";
  $("#jvSteps").hidden = finished;
  $("#jvSteps").innerHTML = STAGES.map(([k, label], i) => {
    if (k === "analyzing" && cuts) label = "Split into reels";
    let cls = i < idx ? "done" : i === idx ? (job.status === "error" ? "failed" : "active") : "";
    let sub = i < idx ? "Done" : i === idx ? (job.status === "error" ? "Failed" : `${Math.round((job.progress || 0) * 100)}%`) : "Waiting";
    if (i === 0 && job.source.type !== "url" && i < idx) sub = "Local file";
    const bar = i === idx && job.status === "running" ? `<span class="bar" style="width:${(job.progress || 0) * 100}%"></span>` : "";
    return `<div class="step ${cls}"><b>${label}</b>${sub}${bar}</div>`;
  }).join("");
  $("#jvStatus").textContent = working && job.stage !== "asking" ? job.message || "" : "";
  renderAsk(job, working);

  $("#jvError").innerHTML = job.status === "error"
    ? `<div class="error-box">${esc(job.error)}<br><button class="btn" id="jvRetry">Retry</button></div>` : "";
  if (job.status === "error") $("#jvRetry").onclick = async () => { await api(`/api/jobs/${job.id}/retry`, { method: "POST" }); await refresh(); };

  ws.update(job);
}

// Put a job the server just sent back into the state and redraw (no need to wait for the next poll).
function applyJob(j) {
  if (!j || !j.id) return;
  const i = state.jobs.findIndex((x) => x.id === j.id);
  if (i >= 0) state.jobs[i] = j; else state.jobs.unshift(j);
  renderAll();
}

// ---------------------------------------------------------------- Ask ClipForge
function renderAsk(job, working) {
  $("#jvAsk").hidden = !job.has_transcript;
  if (!job.has_transcript) return;
  const asking = job.stage === "asking" && working;
  $("#jvAskSend").disabled = working;
  $("#jvAskInput").disabled = working;
  $("#jvAskSend").textContent = asking ? "Working…" : "Send";
  const chat = job.chat || [];
  const key = `${chat.length}|${asking}`;
  if (state.chatKey === key) return;  // only redraw when something changed (keeps the scroll position)
  state.chatKey = key;
  const box = $("#jvChat");
  box.innerHTML = chat.map((m) => {
    if (m.role === "you") return `<div class="msg you">${esc(m.text)}</div>`;
    if (m.role === "note") return `<div class="msg note">${esc(m.text)}</div>`;
    if (m.role === "error") return `<div class="msg err"><b>Claude couldn't do that.</b> ${esc(m.text)}</div>`;
    const changes = m.changes && m.changes !== m.text ? `<div class="changes">${esc(m.changes)}</div>` : "";
    return `<div class="msg claude"><span class="who" title="${esc(m.model || "")}">${esc(m.label || "Claude")}</span>${esc(m.text)}${changes}</div>`;
  }).join("") + (asking ? `<div class="msg claude pending"><div class="spinner"></div>Claude is working on it…</div>` : "");
  box.hidden = !box.innerHTML;
  box.scrollTop = box.scrollHeight;
}

async function sendAsk(jobId) {
  const input = $("#jvAskInput");
  const message = input.value.trim();
  if (!message || input.disabled) return;
  try {
    await api(`/api/jobs/${jobId}/ask`, { json: { message } });
    input.value = "";
    await refresh();
  } catch (e) { toast(e.message, true); }
}

// ---------------------------------------------------------------- New set… dialog
function openBriefDialog(job) {
  const dlg = $("#briefDlg");
  setupBrief($("#dlgBrief"), job.brief || {}, briefDefaults);
  dlg.returnValue = "";  // closing with Esc keeps the old value, which could be "go" from last time
  dlg.onclose = async () => {
    if (dlg.returnValue !== "go") return;
    let brief;
    try { brief = readBrief($("#dlgBrief")); } catch (e) { return toast(e.message, true); }
    try {
      applyJob(await api(`/api/jobs/${job.id}/reanalyze`, { json: brief }));
    } catch (e) { toast(e.message, true); }
  };
  dlg.showModal();
}

// ---------------------------------------------------------------- big player (exported reels)
// Shows an exported reel at full screen height.
function openBig(url, poster = "") {
  const big = $("#bigPlayer");
  big.src = url;
  big.poster = poster;
  $("#bigDlg").showModal();
  big.play().catch(() => {});
}
$("#bigClose").onclick = () => $("#bigDlg").close();
$("#bigDlg").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });  // backdrop click
$("#bigDlg").addEventListener("close", () => { const v = $("#bigPlayer"); v.pause(); v.removeAttribute("src"); v.load(); });

// ---------------------------------------------------------------- settings
async function openSettings() {
  const s = await api("/api/settings");
  const form = $("#settingsForm");
  for (const [k, v] of Object.entries(s)) {
    const input = form.elements[k];
    if (!input) continue;
    if (input.type === "checkbox") input.checked = !!v; else input.value = v;
  }
  $("#settingsDlg").showModal();
}
$("#settingsBtn").onclick = openSettings;
$("#claudeTest").onclick = async () => {
  const out = $("#claudeTestResult");
  out.textContent = "Asking Claude…"; out.className = "test-result";
  try {
    const r = await api("/api/claude-test", { method: "POST" });
    out.textContent = r.ok ? `Claude is connected and answering${r.model_label ? ` (${r.model_label})` : ""}.` : r.message;
    out.className = "test-result " + (r.ok ? "ok" : "bad");
  } catch (e) { out.textContent = e.message; out.className = "test-result bad"; }
};
$("#settingsDlg").addEventListener("close", async () => {
  if ($("#settingsDlg").returnValue !== "save") return;
  const form = $("#settingsForm"), data = {};
  for (const input of form.elements) {
    if (!input.name) continue;
    data[input.name] = input.type === "checkbox" ? input.checked : input.value;
  }
  try { await api("/api/settings", { json: data }); toast("Settings saved"); loadBriefDefaults(); } catch (e) { toast(e.message, true); }
});

// ---------------------------------------------------------------- polling
function renderAll() { renderJobList(); renderJob(); }

async function refresh() {
  try {
    state.jobs = await api("/api/jobs");
    if (!state.selected && state.jobs.length) state.selected = state.jobs[0].id;
    if (state.selected && !state.jobs.find((j) => j.id === state.selected)) state.selected = state.jobs[0]?.id || null;
    renderAll();
  } catch (e) { console.warn(e); }
}

function busy() {
  // the background scan alone does not need fast polling: its progress note updates every few seconds anyway
  return state.jobs.some((j) => j.status === "running" || j.status === "queued" ||
    j.clips.some((c) => c.status === "pending" || c.status === "rendering"));
}

(async function loop() {
  await refresh();
  setTimeout(loop, busy() ? 1500 : 6000);
})();
loadHealth();
loadBriefDefaults();
setInterval(loadHealth, 30000);
