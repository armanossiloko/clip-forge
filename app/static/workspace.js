// ClipForge workspace: the video page. The original video, a live phone preview of the selected reel,
// a timeline over the whole video with every reel on it, and the selected reel's details.
// Reels are drafts (start, end, text) until they're generated (rendered to a file and downloaded); nothing is rendered while editing.
// Uses $, esc, fmt, fmtShort, parseTime, api, toast, state, refresh, applyJob, openBig from app.js.

const TICK_STEPS = [0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
const FILM_H = 56;
// what the creator sees (the code calls generating a file "export")
const STATE_LABEL = { draft: "Draft", exporting: "Generating…", exported: "Generated", changed: "Changed since generated", error: "Generating failed" };
const clampN = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
// libass sizes a font by its whole line height (OS/2 winAscent + winDescent): for Poppins Bold 1000 / (1135 + 627).
// So an ASS font size of 92 is a CSS font-size of 92 × 0.568. (The hook banner is 72 % of the caption size.)
const ASS_EM = 0.568;

const ws = {
  root: null, jobId: null, job: null, selected: null,
  dur: 0, view: [0, 1], drag: null, raf: 0, stopAt: null, seekWanted: null,
  local: null,            // {id, start, end} while the selected reel's times are being changed
  segs: [], words: [], wordStarts: [], wordEnds: [], chunks: [], chunksKey: "",
  settings: {}, film: null, filmTimer: 0,
  plan: null, planKey: "", planTimer: 0, planFor: null,
  saveTimers: {}, usingProxy: false, small: null, needDraw: true,
  autoDownload: new Set(), zipWhenDone: false,  // reels to download as soon as they're generated
  writing: new Set(),  // reels Claude is writing post text for

  // ------------------------------------------------------------ lifecycle
  mount(root, job) {
    this.unmount();
    this.root = root;
    this.jobId = job.id;
    this.job = job;
    this.selected = null;
    this.local = null;
    this.mark = null; this.markShown = false; this.mkKey = "";
    this.film = null;
    this.plan = null; this.planKey = "";
    this.dur = job.video?.duration || 0;
    root.innerHTML = WS_HTML;
    this.small = document.createElement("canvas");
    this.small.width = 36; this.small.height = 64;
    wireWorkspace();
    if (job.video) this.start(job);
  },

  start(job) {  // the video is downloaded: load it, the transcript, thumbnails, settings
    this.started = true;
    this.dur = job.video.duration;
    this.setView(0, this.dur);
    this.setSource(job);
    this.loadExtras(job.id);
    cancelAnimationFrame(this.raf);
    this.raf = requestAnimationFrame(wsTick);
  },

  unmount() {
    cancelAnimationFrame(this.raf);
    clearTimeout(this.filmTimer);
    clearTimeout(this.planTimer);
    Object.values(this.saveTimers).forEach(clearTimeout);
    this.saveTimers = {};
    const v = this.root && $("#wsVideo", this.root);
    if (v) { v.pause(); v.removeAttribute("src"); v.load(); }
    this.root = null; this.jobId = null; this.started = false;
  },

  async loadExtras(jobId) {
    try { this.settings = await api("/api/settings"); } catch {}
    try {
      const t = await api(`/api/jobs/${jobId}/transcript`);
      if (this.jobId !== jobId) return;
      this.segs = t.segments || [];
      this.words = this.segs.flatMap((s) => s.words || []);
      this.wordStarts = this.words.map((w) => w.start).sort((a, b) => a - b);
      this.wordEnds = this.words.map((w) => w.end).sort((a, b) => a - b);
      this.chunksKey = "";
    } catch { this.transcriptMissing = true; }
    this.loadFilm(jobId);
    this.render();
  },

  // timeline thumbnails appear while the one-time scan runs: check for more every few seconds until it's done
  async loadFilm(jobId) {
    clearTimeout(this.filmTimer);
    try {
      const f = await api(`/api/jobs/${jobId}/thumbs`);
      if (this.jobId !== jobId) return;
      if (f.status === "ready" && (!this.film || f.available !== this.film.available)) { this.film = f; this.render(); }
      if (f.complete) return;
    } catch {}
    this.filmTimer = setTimeout(() => this.loadFilm(jobId), 5000);
  },

  // called after every poll
  update(job) {
    if (!this.root || job.id !== this.jobId) return;
    // a reel finished generating: download it if that's what was asked, and say so either way
    const before = new Map((this.job?.clips || []).map((c) => [c.id, c.export_state]));
    for (const c of job.clips) {
      if (before.get(c.id) !== "exporting" || c.export_state === "exporting") continue;
      const wanted = this.autoDownload.delete(c.id);
      if (c.export_state === "exported") {
        if (wanted) downloadFile(clipDownloadUrl(job.id, c));
        toast(wanted ? `Reel #${c.index} is ready and downloading.` : `Reel #${c.index} is ready.`);
      } else if (c.export_state === "error") toast(`Reel #${c.index} couldn't be generated: ${c.error || "unknown error"}`, true);
    }
    // "Generate all": when the last one is done, download them together
    if (this.zipWhenDone && !job.clips.some((c) => c.export_state === "exporting")) {
      this.zipWhenDone = false;
      if (job.clips.some((c) => c.video_url)) { downloadFile(`/api/jobs/${job.id}/zip?v=${Date.now()}`); toast("All reels are ready. Downloading them as one zip."); }
    }
    this.job = job;
    if (job.video && !this.started) this.start(job);
    if (!job.has_transcript && this.started) this.transcriptMissing = true;
    if (job.has_transcript && this.transcriptMissing) { this.transcriptMissing = false; this.loadExtras(job.id); }
    if (job.proxy === "ready" && !this.usingProxy && this.started) {
      const v = $("#wsVideo"), t = v.currentTime;
      this.setSource(job);
      v.addEventListener("loadedmetadata", () => { v.currentTime = t; }, { once: true });
    }
    // keep a selection; the first time, start at the first reel (but keep the whole video in view)
    if (!job.clips.find((c) => c.id === this.selected)) {
      const first = !this.selected && job.clips[0];
      this.select(job.clips[0]?.id || null, false);
      if (first && this.started) this.seek(job.clips[0].start);
    }
    // the scan just finished: framing plans are now instant and exact
    if (job.prep?.status === "done" && this.planKey && !this.planKey.endsWith("|scan")) this.planKey = "";
    this.render();
  },

  // ------------------------------------------------------------ data helpers
  clips() { return this.job?.clips || []; },
  clip(id = this.selected) { return this.clips().find((c) => c.id === id) || null; },
  // times of a reel, including unsaved changes while dragging
  range(c) { return this.local && this.local.id === c.id ? [this.local.start, this.local.end] : [c.start, c.end]; },

  select(id, seek = true) {
    if (this.selected === id) return;
    this.selected = id;
    this.local = null;
    this.plan = null; this.planKey = "";
    this.chunksKey = "";
    this.fillDetail(true);
    const c = this.clip();
    if (c && seek) {
      const v = $("#wsVideo");
      v.pause();
      this.seek(c.start);
      const [v0, v1] = this.view;
      if (c.start < v0 || c.end > v1 || v1 - v0 > (c.end - c.start) * 8) this.fitReel();
    }
    this.render();
  },

  // ------------------------------------------------------------ source video
  setSource(job) {
    const useProxy = job.proxy === "ready";
    this.usingProxy = useProxy;
    const src = useProxy ? `/media/${job.id}/proxy` : `/media/${job.id}/source`;
    const v = $("#wsVideo");
    if (!v.src.endsWith(src)) { v.src = src; v.load(); }
    // the editing copy is made by the background scan anyway; only mention it if the original can not play
    this.msg(job.proxy === "building" && this.sourceFailed ? "Your browser can’t play this video’s format. Making a copy it can play (only once)…" : "");
  },
  msg(text) { const m = $("#wsMsg"); if (m) { m.textContent = text; m.hidden = !text; } },
  seek(t) { this.seekWanted = clampN(t, 0, this.dur); this.needDraw = true; },

  // ------------------------------------------------------------ timeline geometry
  width() { return $("#wsTl")?.clientWidth || 1; },
  pps() { return this.width() / (this.view[1] - this.view[0]); },
  x(t) { return (t - this.view[0]) * this.pps(); },
  t(x) { return this.view[0] + x / this.pps(); },
  setView(v0, v1) {
    const w = clampN(v1 - v0, Math.min(3, this.dur || 3), this.dur || 3);
    v0 = clampN(v0, 0, Math.max(0, this.dur - w));
    this.view = [v0, v0 + w];
    this.scheduleRender();
  },
  fitReel() {
    const c = this.clip();
    if (!c) return this.setView(0, this.dur);
    const [s, e] = this.range(c), pad = Math.max(6, (e - s) * 0.5);
    this.setView(s - pad, e + pad);
  },
  zoom(f, at) {
    const [v0, v1] = this.view;
    at = at ?? (v0 + v1) / 2;
    const w = clampN((v1 - v0) * f, Math.min(3, this.dur), this.dur), r = (at - v0) / (v1 - v0);
    this.setView(at - r * w, at - r * w + w);
  },
  snap(t, kind, off) {  // nearest word edge within a few pixels; Alt turns it off
    const list = kind === "end" ? this.wordEnds : this.wordStarts;
    if (off || !list.length) return t;
    let lo = 0, hi = list.length - 1;
    while (lo < hi) { const m = (lo + hi) >> 1; if (list[m] < t) lo = m + 1; else hi = m; }
    const best = [list[lo], list[lo - 1]].filter((x) => x !== undefined).reduce((a, b) => (Math.abs(b - t) < Math.abs(a - t) ? b : a));
    if (Math.abs(best - t) > Math.min(0.4, 10 / this.pps())) return t;
    return kind === "end" ? Math.min(this.dur, best + 0.1) : Math.max(0, best - 0.05);
  },

  // ------------------------------------------------------------ changing the selected reel
  setTimes(s, e, keepLen = false) {
    const c = this.clip();
    if (!c) return;
    const [cs, ce] = this.range(c), len = ce - cs;
    if (keepLen) { s = clampN(s, 0, this.dur - len); e = s + len; }
    s = clampN(s, 0, this.dur - 1);
    e = clampN(e, s + 1, this.dur);
    this.local = { id: c.id, start: Math.round(s * 100) / 100, end: Math.round(e * 100) / 100 };
    this.chunksKey = "";
    this.scheduleRender();
  },
  commitTimes() {  // after a drag / I / O / typed time: save (still a draft; no rendering)
    const c = this.clip();
    if (!c || !this.local || this.local.id !== c.id) return;
    const { start, end } = this.local;
    if (Math.abs(start - c.start) < 0.005 && Math.abs(end - c.end) < 0.005) { this.local = null; return; }
    this.save(c.id, { start, end }, true);
  },
  async save(id, fields, isTimes = false) {
    try {
      const j = await api(`/api/jobs/${this.jobId}/clips/${id}`, { method: "PATCH", json: fields });
      if (isTimes && this.local?.id === id) this.local = null;
      applyJob(j);
      this.flash("Saved");
    } catch (e) {
      if (isTimes) this.local = null;
      toast(e.message, true);
      this.render();
    }
  },
  saveSoon(field, value) {  // text fields: save 600 ms after typing stops
    const id = this.selected;
    clearTimeout(this.saveTimers[field]);
    this.saveTimers[field] = setTimeout(() => { delete this.saveTimers[field]; this.save(id, { [field]: value }); }, 600);
    if (field === "hook") this.render();
  },
  flash(text) { const el = $("#wsSaved"); if (!el) return; el.textContent = text; el.classList.add("show"); clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), 1200); },
  setIn(t) {
    const c = this.clip(); if (!c) return toast("Select a reel first. To make a new one, use Mark start (I) and Mark end (O)", true);
    const [s, e] = this.range(c), len = e - s;
    if (t > e - 1) this.setTimes(t, t + len); else this.setTimes(t, e);
    this.commitTimes();
  },
  setOut(t) {
    const c = this.clip(); if (!c) return toast("Select a reel first. To make a new one, use Mark start (I) and Mark end (O)", true);
    const [s] = this.range(c);
    if (t < s + 1) return toast("The end must be at least 1 second after the start", true);
    this.setTimes(s, t);
    this.commitTimes();
  },
  // a reel made by hand: from `s` to `e`, or 30 s from the playhead
  async newReel(s, e = null) {
    if (e === null) {
      const len = Math.min(+(this.job?.brief?.max_seconds || 30), 30);
      s = clampN(s, 0, Math.max(0, this.dur - 1));
      e = Math.min(this.dur, s + len);
    }
    // title it after its first words, so it's easy to recognise in the list
    const words = this.words.filter((w) => w.start >= s && w.end <= e).slice(0, 7).map((w) => w.word.trim());
    const title = words.length ? words.join(" ").replace(/[,.;:!?]+$/, "") + (words.length === 7 ? "…" : "") : "New reel";
    try {
      const j = await api(`/api/jobs/${this.jobId}/clips`, { json: { start: s, end: e, title } });
      applyJob(j);
      this.select(j.new_clip_id, false);
      const c = this.clip();
      toast(`Reel${c ? " #" + c.index : ""} added (${(e - s).toFixed(1)} s). Drag its edges to fine-tune, then press Generate.`);
    } catch (e2) { toast(e2.message, true); }
  },

  // Mark start (I) / Mark end (O): make a new reel by hand, Steam-style
  markStart(t) {
    t = clampN(this.snap(t, "start", false), 0, this.dur);
    this.mark = { start: Math.round(t * 100) / 100 };
    this.render();
    toast("Start marked. Play or scrub to where the reel should end, then press Mark end (O). Esc cancels.");
  },
  async markEnd(t) {
    if (!this.mark) return toast("Mark the start first: press Mark start (I) where the reel should begin", true);
    let s = this.mark.start, e = this.snap(t, "end", false);
    if (e < s) [s, e] = [this.snap(t, "start", false), this.snap(this.mark.start, "end", false)];
    if (e - s < 1) return toast("A reel must be at least 1 second long: move further from the start mark", true);
    this.mark = null;
    $("#wsVideo").pause();
    await this.newReel(Math.max(0, s), Math.min(this.dur, e));
  },
  cancelMark() { if (this.mark) { this.mark = null; this.render(); } },

  // ------------------------------------------------------------ drawing
  render() {
    if (!this.root || !this.started) return;
    this.tlW = this.width();  // cached for the per-frame playhead
    this.ovW = $("#wsOv").clientWidth || 1;
    this.phoneH = $("#wsPhone").clientHeight || 1;
    this.phKey = "";
    this.renderTimeline();
    this.renderOverview();
    this.renderList();
    this.fillDetail(false);
    this.renderStatus();
    this.ensurePlan();
    this.needDraw = true;
  },

  // redraw at most once per frame (dragging fires many more events than that)
  scheduleRender() {
    if (this.renderQueued) return;
    this.renderQueued = true;
    requestAnimationFrame(() => { this.renderQueued = false; this.render(); });
  },

  renderTimeline() {
    const W = this.width(), [v0, v1] = this.view, p = this.pps();
    // ruler, thumbnails and sentences only change when the visible part of the video changes
    const viewKey = `${v0.toFixed(3)}|${v1.toFixed(3)}|${W}|${this.film?.version || 0}|${this.film?.available || 0}|${this.segs.length}`;
    if (viewKey !== this.viewKey) { this.viewKey = viewKey; this.renderBackdrop(W, v0, v1, p); }
    this.renderReelsLayer(v0, v1, p);
    this.drawPlayhead();
  },

  renderBackdrop(W, v0, v1, p) {
    const step = TICK_STEPS.find((s) => s * p >= 80) || 3600;
    let ticks = "";
    for (let t = Math.ceil(v0 / step) * step; t <= v1; t += step) {
      ticks += `<span class="tk" style="left:${this.x(t).toFixed(1)}px">${step < 1 ? fmt(t) : fmtShort(t)}</span>`;
    }
    $("#wsRuler").innerHTML = ticks;

    // thumbnails: one per slot, the frame nearest the slot's middle (only those the scan has made so far)
    const film = $("#wsFilm");
    if (this.film && this.film.available > 0) {
      const f = this.film, tw = Math.round(FILM_H * f.tile_w / f.tile_h), n = Math.ceil(W / tw) + 1;
      while (film.children.length < n) film.appendChild(document.createElement("i"));
      while (film.children.length > n) film.lastChild.remove();
      [...film.children].forEach((el, k) => {
        const t = v0 + (k * tw + tw / 2) / p, idx = Math.max(0, Math.round(t / f.interval));
        const ok = t <= this.dur && idx < f.available;
        const url = ok ? `${f.base}${String(idx + 1).padStart(5, "0")}.jpg?v=${f.version}` : "";
        if (el.dataset.url !== url) {
          el.dataset.url = url;
          el.style.backgroundImage = url ? `url(${url})` : "none";
        }
        el.style.left = `${k * tw}px`;
        el.style.width = `${tw}px`;
      });
      film.classList.toggle("loading", !f.complete);
    } else { film.innerHTML = ""; film.classList.add("loading"); }

    // sentences: only when zoomed in enough to read them (a long video has well over a thousand)
    let segs = "";
    if (p >= 4) {
      for (const sg of this.segs) {
        if (sg.end < v0 || sg.start > v1) continue;
        const w = Math.max(2, (sg.end - sg.start) * p);
        segs += `<span class="sg" data-t="${sg.start}" style="left:${this.x(sg.start).toFixed(1)}px;width:${w.toFixed(1)}px" title="${esc(fmt(sg.start) + "  " + sg.text)}">${w > 28 ? esc(sg.text) : ""}</span>`;
      }
    } else if (this.segs.length) segs = `<span class="sg-hint">Zoom in to see what's said</span>`;
    $("#wsSegs").innerHTML = segs;
  },

  renderReelsLayer(v0, v1, p) {
    let reels = "";
    for (const c of this.clips()) {
      const [s, e] = this.range(c);
      if (e < v0 || s > v1) continue;
      const sel = c.id === this.selected;
      const w = Math.max(3, (e - s) * p);
      reels += `<div class="rl ${sel ? "sel" : ""} st-${c.export_state}" data-id="${c.id}" style="left:${this.x(s).toFixed(1)}px;width:${w.toFixed(1)}px">`
        + `<span class="rl-label">${w > 34 ? `#${c.index} ${esc(c.title)}` : `#${c.index}`}</span>`
        + (c.peak != null && c.peak > s && c.peak < e
          ? `<span class="rl-peak" style="left:${((c.peak - s) * p).toFixed(1)}px" title="Peak ${fmt(c.peak)}: the moment this reel builds up to"></span>` : "")
        + (sel ? `<i class="tl-h" data-h="start"></i><i class="tl-h" data-h="end"></i><span class="rl-len">${(e - s).toFixed(1)}s</span>` : "")
        + `</div>`;
    }
    const layer = $("#wsReelsLayer");
    if (layer._html !== reels) { layer.innerHTML = reels; layer._html = reels; }
  },

  renderOverview() {
    const ov = $("#wsOv"), op = (ov.clientWidth || 1) / Math.max(this.dur, 1);
    $("#wsOvClips").innerHTML = this.clips().map((c) => {
      const [s, e] = this.range(c);
      return `<span class="${c.id === this.selected ? "sel" : ""} st-${c.export_state}" data-id="${c.id}" style="left:${(s * op).toFixed(1)}px;width:${Math.max(3, (e - s) * op).toFixed(1)}px" title="#${c.index} ${esc(c.title)}"></span>`;
    }).join("");
    const vb = $("#wsOvView");
    vb.style.left = `${this.view[0] * op}px`;
    vb.style.width = `${Math.max(4, (this.view[1] - this.view[0]) * op)}px`;
  },

  // called every frame: only touch the page when something visibly changed
  drawPlayhead() {
    const v = $("#wsVideo"); if (!v) return;
    const t = v.currentTime || 0;
    const W = this.tlW || (this.tlW = this.width()), ow = this.ovW || (this.ovW = $("#wsOv").clientWidth || 1);
    const x = Math.round((t - this.view[0]) * W / (this.view[1] - this.view[0]) * 2) / 2, ox = Math.round(t * ow / Math.max(this.dur, 1) * 2) / 2;
    const key = `${x}|${ox}`;
    if (key !== this.phKey) {
      this.phKey = key;
      const ph = $("#wsPlayhead");
      ph.style.transform = `translateX(${x}px)`;
      ph.hidden = x < -2 || x > W + 2;
      $("#wsOvPlayhead").style.transform = `translateX(${ox}px)`;
    }
    const now = `${fmt(t)} / ${fmt(this.dur)}`;
    if (now !== this.nowText) { this.nowText = now; $("#wsNow").textContent = now; }
    // a marked start: the marker, and the new reel growing from it to the playhead
    const mk = $("#wsMark"), gh = $("#wsGhost");
    if (!mk) return;
    if (this.mark) {
      const sc = W / (this.view[1] - this.view[0]);
      const mx = (this.mark.start - this.view[0]) * sc;
      const a = Math.min(this.mark.start, t), b = Math.max(this.mark.start, t);
      const mkKey = `${mx.toFixed(1)}|${a.toFixed(2)}|${b.toFixed(2)}|${W}`;
      if (mkKey !== this.mkKey) {
        this.mkKey = mkKey;
        mk.hidden = mx < -2 || mx > W + 2;
        mk.style.transform = `translateX(${mx}px)`;
        gh.hidden = false;
        gh.style.left = `${(a - this.view[0]) * sc}px`;
        gh.style.width = `${(b - a) * sc}px`;
        gh.dataset.len = `${(b - a).toFixed(1)}s`;
        $("#wsOvMark").style.transform = `translateX(${this.mark.start * ow / Math.max(this.dur, 1)}px)`;
      }
    } else if (this.mkKey) { this.mkKey = ""; mk.hidden = true; gh.hidden = true; }
    if (!!this.mark !== this.markShown) {  // only when a mark appears or goes away
      this.markShown = !!this.mark;
      $("#wsOvMark").hidden = !this.mark;
      $("#wsMarkEnd").classList.toggle("primary", !!this.mark);
      $("#wsMarkCancel").hidden = !this.mark;
    }
  },

  renderList() {
    const list = $("#wsReels");
    const items = this.clips().map((c) => {
      const [s, e] = this.range(c);
      return `<button class="rc ${c.id === this.selected ? "sel" : ""}" data-id="${c.id}">`
        + `<span class="rc-n">#${c.index}</span><span class="rc-t">${esc(c.title || "Untitled")}</span>`
        + `<span class="rc-m">${fmtShort(s)}–${fmtShort(e)} · ${(e - s).toFixed(0)}s</span>`
        + `<span class="chip-st st-${c.export_state}">${c.export_state === "exporting" ? exportLabel(c) : STATE_LABEL[c.export_state] || ""}</span></button>`;
    }).join("");
    const html = items + `<button class="rc rc-new" id="wsNewReel" title="A new reel starting at the playhead (N)">+ New reel at the playhead</button>`;
    if (list._html !== html) { list.innerHTML = html; list._html = html; }
    $("#wsReelsTitle").textContent = this.clips().length ? `${this.clips().length} reel${this.clips().length === 1 ? "" : "s"}` : "No reels yet";
  },

  renderStatus() {
    const job = this.job, note = $("#wsPrep");
    const p = job?.prep;
    note.hidden = !(p && (p.status === "queued" || p.status === "running"));
    if (!note.hidden) {
      const eta = p.eta ? ` · about ${p.eta >= 90 ? Math.round(p.eta / 60) + " min" : p.eta + " s"} left` : "";
      note.textContent = `Preparing this video (once): finding faces and camera cuts, and making a light copy for smooth editing. ${Math.round((p.progress || 0) * 100)}%${eta}. Until it finishes, the phone shows the whole frame; generated files always use the exact framing.`;
    }
  },

  // ------------------------------------------------------------ details panel of the selected reel
  fillDetail(force) {
    const box = $("#wsDetail"); if (!box) return;
    const c = this.clip();
    box.classList.toggle("empty", !c);
    if (!c) return;
    const [s, e] = this.range(c);
    const set = (id, v) => { const el = $("#" + id); if (force || (document.activeElement !== el && !this.saveTimers[el.dataset.field])) { if (el.value !== v) el.value = v; } };
    set("wdTitle", c.title || "");
    set("wdHook", c.hook || "");
    set("wdCaption", c.caption || "");
    set("wdTags", (c.hashtags || []).join(" "));
    set("wdCrop", c.crop_mode || "");
    if (force || document.activeElement !== $("#wdStart")) $("#wdStart").value = fmt(s);
    if (force || document.activeElement !== $("#wdEnd")) $("#wdEnd").value = fmt(e);
    $("#wdDur").textContent = `${(e - s).toFixed(1)}s`;
    $("#wdNum").textContent = `Reel #${c.index}`;
    const st = $("#wdState");
    st.className = `chip-st st-${c.export_state}`;
    st.textContent = STATE_LABEL[c.export_state] || "";
    $("#wdReason").textContent = c.reason || "";
    $("#wdMade").textContent = c.made_by_text || "";
    $("#wdErr").textContent = c.error || "";
    // one button: Generate (make the file, then download it) / Generating… / Download again / Generate again
    const ex = $("#wdExport");
    const generating = c.export_state === "exporting";
    ex.textContent = generating ? exportLabel(c)
      : { draft: "⬇ Generate", changed: "⬇ Generate again", exported: "⬇ Download again", error: "⬇ Try again" }[c.export_state] || "⬇ Generate";
    ex.title = c.export_state === "exported" ? "Download the file you already generated"
      : "Make the 1080×1920 video file of this reel and download it";
    ex.disabled = generating;
    ex.classList.toggle("busy", generating);
    ex.classList.toggle("primary", !generating);
    // while generating, the file on disk is still the previous version: don't offer it
    $("#wdWatch").hidden = !c.video_url || generating;
    const writing = ws.writing.has(c.id), has = hasPostText(c);
    const copy = $("#wdCopy");
    copy.textContent = writing ? "✨ Writing…" : has ? "Copy post text" : "✨ Write post text";
    copy.title = has ? "Copy the title, caption and hashtags" : "Let Claude write a title, caption and hashtags from what's said in this reel";
    copy.disabled = writing;
    $("#wdRewrite").hidden = !has || writing;
    $("#wdChanged").hidden = c.export_state !== "changed";
    $("#wdReset").hidden = !c.suggested;
    const i = this.clips().indexOf(c);
    $("#wdPrev").disabled = i <= 0;
    $("#wdNext").disabled = i >= this.clips().length - 1;
  },

  // ------------------------------------------------------------ live phone preview
  ensurePlan() {
    const c = this.clip();
    if (!c || !this.job?.video) { this.plan = null; return; }
    if (this.drag) return;  // wait until the drag ends
    const [s, e] = this.range(c);
    const mode = c.crop_mode || "";
    const scanned = this.job.prep?.status === "done";
    const key = `${c.id}|${s}|${e}|${mode}|${scanned ? "scan" : "quick"}`;
    if (key === this.planKey) return;
    this.planKey = key;
    clearTimeout(this.planTimer);
    const jobId = this.jobId;
    this.planTimer = setTimeout(async () => {
      $("#pvBusy").hidden = false;
      try {
        const plan = await api(`/api/jobs/${jobId}/framing?start=${s}&end=${e}${mode ? `&mode=${mode}` : ""}`);
        if (this.planKey !== key) return;
        this.plan = { ...plan, at: s };
        this.needDraw = true;
      } catch (err) {
        if (this.planKey === key) this.plan = null;
      } finally { if (this.planKey === key) $("#pvBusy").hidden = true; }
    }, scanned ? 0 : 250);
  },

  buildChunks(c) {
    const [s, e] = this.range(c);
    const key = `${c.id}|${s}|${e}|${this.settings.caption_words}`;
    if (key === this.chunksKey) return;
    this.chunksKey = key;
    const maxW = Math.max(1, +(this.settings.caption_words || 3));
    const words = this.words.filter((w) => w.end > s && w.start < e);
    const chunks = [];
    let cur = [];
    for (const w of words) {
      if (cur.length) {
        const gap = w.start - cur[cur.length - 1].end, long = w.end - cur[0].start > 1.8;
        if (cur.length >= maxW || gap > 0.6 || long || /[.?!,]$/.test(cur[cur.length - 1].word.trim())) { chunks.push(cur); cur = []; }
      }
      cur.push(w);
    }
    if (cur.length) chunks.push(cur);
    this.chunks = chunks.map((ch, i) => {
      const next = chunks[i + 1] ? chunks[i + 1][0].start : e;
      let end = ch[ch.length - 1].end;
      if (next - end < 0.4) end = next;
      return { words: ch, start: ch[0].start, end };
    });
  },

  drawPhone(t) {
    const cv = $("#wsCanvas"), v = $("#wsVideo");
    if (!cv || !v || v.readyState < 2) return;
    const ctx = cv.getContext("2d"), W = cv.width, H = cv.height;
    const vw = v.videoWidth, vh = v.videoHeight;
    if (!vw || !vh) return;
    const c = this.clip();
    const [s, e] = c ? this.range(c) : [0, 0];
    const inside = c && t >= s - 0.01 && t < e;
    const plan = this.plan && inside ? this.plan : null;
    let layout = "fit", cropX = null;
    if (plan) {
      const rel = t - plan.at;
      const shot = plan.shots.find((sh) => rel >= sh.t0 && rel < sh.t1) || plan.shots[plan.shots.length - 1];
      if (shot && shot.layout === "crop" && plan.keys.length) { layout = "crop"; cropX = keyX(plan.keys, rel); }
    }
    if (layout === "crop") {
      const sx = vw / plan.src_w, sy = vh / plan.src_h;
      ctx.drawImage(v, cropX * sx, 0, plan.crop_w * sx, plan.crop_h * sy, 0, 0, W, H);
    } else {
      // blurred background: draw tiny, then stretch (cheap blur), slightly darker; whole frame on top
      const sc = this.small, sctx = sc.getContext("2d"), k = Math.max(sc.width / vw, sc.height / vh);
      sctx.drawImage(v, (sc.width - vw * k) / 2, (sc.height - vh * k) / 2, vw * k, vh * k);
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = "high";
      ctx.drawImage(sc, 0, 0, W, H);
      ctx.fillStyle = "rgba(0,0,0,0.08)";
      ctx.fillRect(0, 0, W, H);
      const fh = W * vh / vw;
      if (fh <= H) ctx.drawImage(v, 0, (H - fh) / 2, W, fh);
      else { const fw = H * vw / vh; ctx.drawImage(v, (W - fw) / 2, 0, fw, H); }
    }
    this.drawCaptions(t, c, inside);
  },

  drawCaptions(t, c, inside) {
    const frame = $("#wsPhone"), cap = $("#pvCap"), hook = $("#pvHook"), out = $("#pvOutside");
    const H = this.phoneH || frame.clientHeight || 1, k = H / 1920;
    const st = this.settings, size = +(st.caption_size || 92);
    const upper = st.caption_uppercase !== false, hl = st.caption_highlight || "#FFD400";
    const show = (el, on) => { if (el.hidden === on) el.hidden = !on; };  // only touch the page on a change
    show(out, !!c && !inside);
    if (c && !inside) {
      const msg = `Outside reel #${c.index}. Press ▶ Play reel to watch it.`;
      if (out.textContent !== msg) out.textContent = msg;
    }
    if (!c || !inside) { show(cap, false); show(hook, false); return; }
    // sizes and colours only change with the phone size or Settings
    const styleKey = `${H}|${size}|${hl}|${st.caption_color}|${st.caption_position}`;
    if (styleKey !== this.capStyleKey) {
      this.capStyleKey = styleKey;
      cap.style.fontSize = `${size * k * ASS_EM}px`;
      cap.style.color = st.caption_color || "#fff";
      cap.style.top = `${(+(st.caption_position ?? 0.7)) * 100}%`;
      cap.style.setProperty("--o", `${Math.max(1, 7 * k)}px`);
      hook.style.fontSize = `${size * 0.72 * k * ASS_EM}px`;
      hook.style.background = hl;
      hook.style.top = `${230 * k}px`;
      hook.style.padding = `${18 * k * 0.6}px ${18 * k}px`;
    }
    this.buildChunks(c);
    const [s] = this.range(c);
    const ch = st.captions !== false ? this.chunks.find((x) => t >= x.start && t < x.end) : null;
    if (ch) {
      const cur = [...ch.words].reverse().find((w) => w.start <= t) || ch.words[0];
      // each word its own box with the same gap as the export; the current one grows and pushes the others aside
      const html = ch.words.map((w) => {
        const txt = esc(upper ? w.word.toUpperCase() : w.word);
        return w === cur ? `<b class="w on" style="color:${esc(hl)}">${txt}</b>` : `<span class="w">${txt}</span>`;
      }).join("");
      if (cap._html !== html) { cap.innerHTML = html; cap._html = html; }
    }
    show(cap, !!ch);
    const hookText = (c.id === this.selected ? $("#wdHook").value : c.hook) || "";
    const showHook = !!hookText && t < s + 3;
    show(hook, showHook);
    if (showHook) {
      const txt = upper ? hookText.toUpperCase() : hookText;
      if (hook.textContent !== txt) hook.textContent = txt;
    }
  },
};

// x of the crop window at clip-relative time t; the same rule as crop.keys_to_expr (hold, 0.6 s glide, or jump)
function exportLabel(c) {
  return c.status === "pending" ? "Waiting…" : `Generating… ${Math.round((c.progress || 0) * 100)}%`;
}

function hasPostText(c) { return !!((c.caption || "").trim() || (c.hashtags || []).length); }

async function writePostText(c) {
  const jobId = ws.jobId;
  ws.writing.add(c.id);
  ws.fillDetail(false);
  try {
    applyJob(await api(`/api/jobs/${jobId}/clips/${c.id}/write-text`, { method: "POST" }));
    if (ws.selected === c.id) ws.fillDetail(true);  // show the new text in the fields
    toast(`Post text written for reel #${c.index}. Edit it if you like, then Copy post text.`);
  } catch (e) { toast(e.message, true); }
  finally { ws.writing.delete(c.id); ws.fillDetail(false); }
}

// the link changes with every new version of the file, so a browser can never hand back an older one
function clipDownloadUrl(jobId, c) { return `/api/jobs/${jobId}/clips/${c.id}/download?v=${Math.round(c.rendered_at || 0)}`; }

function downloadFile(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function keyX(keys, t) {
  for (let i = 1; i < keys.length; i++) {
    const [t1, x1, cut] = keys[i], [t0, x0] = keys[i - 1];
    if (t < t1) {
      const ts = Math.max(t0, t1 - 0.6);
      if (cut || t1 - ts < 0.05 || t < ts) return x0;
      return x0 + (x1 - x0) * (t - ts) / (t1 - ts);
    }
  }
  return keys[keys.length - 1][1];
}

function wsTick() {
  if (!ws.root) return;
  const v = $("#wsVideo");
  if (ws.seekWanted !== null && !v.seeking) { v.currentTime = ws.seekWanted; ws.seekWanted = null; }
  const t = v.currentTime || 0;
  if (ws.stopAt !== null && !v.paused && t >= ws.stopAt) { v.pause(); v.currentTime = ws.stopAt - 0.02; ws.stopAt = null; }
  if (!v.paused && !ws.drag) {  // keep the playhead in view
    const [v0, v1] = ws.view, w = v1 - v0;
    if (t > v1 || t < v0) ws.setView(t - w * 0.1, t + w * 0.9);
  }
  if (!v.paused || ws.needDraw) { ws.drawPhone(t); ws.needDraw = false; }
  ws.drawPlayhead();
  const label = v.paused ? "▶ Play" : "❚❚ Pause";
  if (ws.playLabel !== label) { ws.playLabel = label; $("#wsPlay").textContent = label; }
  ws.raf = requestAnimationFrame(wsTick);
}

function wsPlayReel() {
  const c = ws.clip(); if (!c) return;
  const v = $("#wsVideo"), [s, e] = ws.range(c);
  v.currentTime = s;
  ws.stopAt = e;
  v.play().catch(() => {});
}
function wsTogglePlay() {
  const v = $("#wsVideo");
  if (!v.paused) return v.pause();
  const c = ws.clip();
  if (c) {
    const [s, e] = ws.range(c);
    if (v.currentTime >= s - 0.05 && v.currentTime < e - 0.05) ws.stopAt = e;  // inside the reel: stop at its end
    else ws.stopAt = null;
  }
  v.play().catch(() => {});
}

const WS_HTML = `
  <div class="ws-top">
    <div class="ws-src">
      <div class="ws-stage" id="wsStage" title="Click to play or pause (Space)">
        <video id="wsVideo" playsinline preload="auto"></video>
        <span class="ws-tag">Original video</span>
        <div class="ws-msg" id="wsMsg" hidden></div>
      </div>
    </div>
    <div class="ws-phone-col">
      <div class="ws-phone" id="wsPhone">
        <canvas id="wsCanvas" width="540" height="960"></canvas>
        <div class="pv-hook" id="pvHook" hidden></div>
        <div class="pv-cap" id="pvCap" hidden></div>
        <div class="pv-out" id="pvOutside" hidden></div>
        <div class="pv-busy" id="pvBusy" hidden><div class="spinner"></div></div>
      </div>
      <span class="ws-tag2">Live preview · nothing is rendered until you press Generate</span>
    </div>
    <div class="ws-detail" id="wsDetail">
      <div class="wd-empty">Select a reel on the timeline or in the list below, or press <b>N</b> to start a new one at the playhead.</div>
      <div class="wd-body">
        <div class="wd-head"><b id="wdNum"></b><span id="wdState"></span><span class="wd-saved" id="wsSaved"></span>
          <span class="wd-nav"><button class="btn small" id="wdPrev" title="Previous reel">‹</button><button class="btn small" id="wdNext" title="Next reel">›</button></span></div>
        <input type="text" id="wdTitle" data-field="title" class="title" placeholder="Title">
        <div class="wd-times">
          <label>Start<span class="time"><button class="btn" data-nudge="start:-0.5">−</button><input type="text" id="wdStart"><button class="btn" data-nudge="start:0.5">+</button></span></label>
          <label>End<span class="time"><button class="btn" data-nudge="end:-0.5">−</button><input type="text" id="wdEnd"><button class="btn" data-nudge="end:0.5">+</button></span></label>
          <span class="wd-dur" id="wdDur"></span>
        </div>
        <label>Hook (on screen for 3 s)<input type="text" id="wdHook" data-field="hook" placeholder="No hook"></label>
        <label>Caption<textarea id="wdCaption" data-field="caption" rows="3"></textarea></label>
        <label>Hashtags<input type="text" id="wdTags" data-field="hashtags"></label>
        <label>Framing<select id="wdCrop" data-field="crop_mode">
          <option value="">Default (from Settings)</option><option value="smart">Smart (per shot)</option><option value="track">Always follow the face</option>
          <option value="center">Center crop</option><option value="fit">Always the whole frame</option></select></label>
        <div class="reason" id="wdReason"></div>
        <div class="made-by" id="wdMade"></div>
        <div class="clip-err" id="wdErr"></div>
        <div class="wd-note" id="wdChanged" hidden>You changed this reel after generating it. Generate again to get a file with your changes.</div>
        <div class="wd-actions">
          <button class="btn primary" id="wdExport"></button>
          <button class="btn small" id="wdWatch" hidden title="Watch the generated file">▶ Watch file</button>
          <button class="btn small" id="wdCopy"></button>
          <button class="btn small ghost" id="wdRewrite" title="Let Claude write a new title, caption and hashtags" hidden>✨ Rewrite</button>
          <button class="btn small ghost" id="wdReset" title="Back to the times and text Claude suggested">Reset</button>
          <button class="btn small ghost danger" id="wdDelete">Delete</button>
        </div>
      </div>
    </div>
  </div>
  <div class="ws-bar">
    <div class="grp">
      <button class="btn" id="wsToStart" title="Go to the reel's start (Home)">⏮</button>
      <button class="btn" id="wsPlay" title="Play / pause (Space)">▶ Play</button>
      <button class="btn" id="wsPlayReel" title="Play the selected reel from its start">▶ Play reel</button>
      <button class="btn" id="wsToEnd" title="Go to the reel's end (End)">⏭</button>
      <span class="mono ws-now" id="wsNow"></span>
    </div>
    <div class="grp" title="Make a new reel by hand">
      <span class="grp-label">New reel</span>
      <button class="btn" id="wsMarkStart" title="Mark where a new reel starts (I)">⟦ Mark start</button>
      <button class="btn" id="wsMarkEnd" title="Mark where it ends: this creates the reel (O)">Mark end ⟧</button>
      <button class="btn ghost" id="wsMarkCancel" title="Cancel the start mark (Esc)" hidden>✕</button>
    </div>
    <div class="grp" title="Change the selected reel">
      <span class="grp-label">Selected reel</span>
      <button class="btn" id="wsSetIn" title="Move the selected reel's start to the playhead (Shift+I)">Start here</button>
      <button class="btn" id="wsSetOut" title="Move the selected reel's end to the playhead (Shift+O)">End here</button>
    </div>
    <div class="grp right">
      <button class="btn" id="wsZoomOut" title="Zoom out (−)">−</button>
      <button class="btn" id="wsZoomIn" title="Zoom in (+)">+</button>
      <button class="btn" id="wsFit" title="Zoom to the selected reel">Fit reel</button>
      <button class="btn" id="wsAll" title="Show the whole video">Whole video</button>
    </div>
  </div>
  <div class="tl" id="wsTl">
    <div class="tl-ruler" id="wsRuler"></div>
    <div class="tl-film" id="wsFilm"></div>
    <div class="tl-segs" id="wsSegs"></div>
    <div class="tl-reels" id="wsReelsLayer"></div>
    <div class="tl-ghost" id="wsGhost" hidden></div>
    <div class="tl-mark" id="wsMark" hidden></div>
    <div class="tl-playhead" id="wsPlayhead"></div>
    <div class="tl-tip" id="wsTip" hidden></div>
  </div>
  <div class="tl-overview" id="wsOv" title="The whole video. Click or drag to move around; click a reel to select it.">
    <div class="ov-clips" id="wsOvClips"></div><div class="ov-view" id="wsOvView"></div><div class="ov-mark" id="wsOvMark" hidden></div><div class="ov-playhead" id="wsOvPlayhead"></div>
  </div>
  <div class="ws-prep" id="wsPrep" hidden></div>
  <div class="ws-reels-head"><h2 id="wsReelsTitle"></h2>
    <span class="hint">Make your own: <b>I</b> marks a start, <b>O</b> marks the end and creates the reel. Click a reel to edit it: drag its edges on
    the timeline (they snap to words; hold Alt to stop that) or drag the middle to move it.
    Keys: Space play · I / O new reel · Shift+I / Shift+O move the selected reel's start / end · N 30 s reel · Esc cancel · ← → step (Shift: finer) · + − zoom.</span></div>
  <div class="ws-reels" id="wsReels"></div>`;

function wireWorkspace() {
  const v = $("#wsVideo");
  v.addEventListener("seeked", () => { ws.needDraw = true; });
  v.addEventListener("loadeddata", () => { ws.needDraw = true; });
  v.addEventListener("error", async () => {
    if (!ws.root || ws.usingProxy || !v.getAttribute("src")) return;
    ws.sourceFailed = true;
    ws.msg("Your browser can't play this video's format. Making a playable copy (only once; a few minutes for long videos)…");
    try { await api(`/api/jobs/${ws.jobId}/proxy`, { method: "POST" }); refresh(); } catch (e) { ws.msg(e.message); }
  });
  $("#wsStage").onclick = wsTogglePlay;
  $("#wsPlay").onclick = wsTogglePlay;
  $("#wsPlayReel").onclick = wsPlayReel;
  $("#wsToStart").onclick = () => { const c = ws.clip(); v.pause(); ws.seek(c ? ws.range(c)[0] : 0); };
  $("#wsToEnd").onclick = () => { const c = ws.clip(); v.pause(); ws.seek(c ? ws.range(c)[1] - 0.04 : ws.dur); };
  $("#wsMarkStart").onclick = () => ws.markStart(v.currentTime);
  $("#wsMarkEnd").onclick = () => ws.markEnd(v.currentTime);
  $("#wsMarkCancel").onclick = () => ws.cancelMark();
  $("#wsSetIn").onclick = () => ws.setIn(v.currentTime);
  $("#wsSetOut").onclick = () => ws.setOut(v.currentTime);
  $("#wsZoomIn").onclick = () => ws.zoom(0.5, v.currentTime);
  $("#wsZoomOut").onclick = () => ws.zoom(2, v.currentTime);
  $("#wsFit").onclick = () => ws.fitReel();
  $("#wsAll").onclick = () => ws.setView(0, ws.dur);

  // timeline: drag edges / move the selected reel, click another reel to select it, elsewhere scrubs
  const tl = $("#wsTl");
  tl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || !ws.started) return;
    const r = tl.getBoundingClientRect(), t = ws.t(e.clientX - r.left);
    const reel = e.target.closest(".rl"), h = e.target.closest(".tl-h");
    if (h) ws.drag = { kind: h.dataset.h };
    else if (reel && reel.dataset.id !== ws.selected) { ws.select(reel.dataset.id, false); ws.drag = { kind: "move", offset: t - ws.range(ws.clip())[0], moved: false }; }
    else if (reel) ws.drag = { kind: "move", offset: t - ws.range(ws.clip())[0], moved: false };
    else { ws.drag = { kind: "scrub" }; ws.seek(t); }
    v.pause();
    tl.setPointerCapture(e.pointerId);
    tl.classList.add("dragging");
    e.preventDefault();
  });
  tl.addEventListener("pointermove", (e) => {
    const r = tl.getBoundingClientRect(), x = e.clientX - r.left, t = clampN(ws.t(x), 0, ws.dur), tip = $("#wsTip");
    tip.hidden = false;
    tip.style.left = `${x}px`;
    const d = ws.drag;
    if (!d) { tip.textContent = fmt(t); return; }
    const c = ws.clip();
    if (d.kind === "start") { ws.setTimes(ws.snap(t, "start", e.altKey), ws.range(c)[1]); ws.seek(ws.range(c)[0]); }
    else if (d.kind === "end") { ws.setTimes(ws.range(c)[0], ws.snap(t, "end", e.altKey)); ws.seek(ws.range(c)[1] - 0.04); }
    else if (d.kind === "move") {
      if (!d.moved && Math.abs(t - d.offset - ws.range(c)[0]) * ws.pps() < 4) return;  // a click, not a drag
      d.moved = true;
      ws.setTimes(ws.snap(t - d.offset, "start", e.altKey), 0, true);
      ws.seek(ws.range(c)[0]);
    } else ws.seek(t);
    const [s2, e2] = c ? ws.range(c) : [0, 0];
    tip.textContent = d.kind === "start" ? `Start ${fmt(s2)}` : d.kind === "end" ? `End ${fmt(e2)}` : d.kind === "move" ? `${fmt(s2)} → ${fmt(e2)}` : fmt(t);
  });
  const end = () => {
    const d = ws.drag; ws.drag = null;
    tl.classList.remove("dragging");
    if (d && d.kind === "move" && !d.moved) { const c = ws.clip(); if (c) ws.seek(ws.range(c)[0]); }
    if (d && d.kind !== "scrub") ws.commitTimes();
    ws.render();
  };
  tl.addEventListener("pointerup", end);
  tl.addEventListener("pointercancel", end);
  tl.addEventListener("pointerleave", () => { if (!ws.drag) $("#wsTip").hidden = true; });
  tl.addEventListener("dblclick", (e) => {  // double-click a sentence: start the selected reel there
    const sg = e.target.closest(".sg");
    if (sg && ws.clip()) ws.setIn(+sg.dataset.t - 0.05);
  });
  tl.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = tl.getBoundingClientRect();
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const dt = (e.shiftKey ? e.deltaY : e.deltaX) / ws.pps();
      ws.setView(ws.view[0] + dt, ws.view[1] + dt);
    } else ws.zoom(Math.exp(e.deltaY * 0.0015), ws.t(e.clientX - r.left));
  }, { passive: false });

  // overview of the whole video
  const ov = $("#wsOv");
  ov.addEventListener("pointerdown", (e) => {
    if (!ws.started) return;
    const reel = e.target.closest("[data-id]");
    if (reel) return ws.select(reel.dataset.id);
    const move = (ev) => {
      const r = ov.getBoundingClientRect(), t = clampN((ev.clientX - r.left) / r.width * ws.dur, 0, ws.dur), w = ws.view[1] - ws.view[0];
      ws.setView(t - w / 2, t + w / 2);
    };
    move(e);
    ov.setPointerCapture(e.pointerId);
    ov.onpointermove = move;
    ov.onpointerup = () => { ov.onpointermove = null; };
  });

  // reel list
  $("#wsReels").addEventListener("click", (e) => {
    if (e.target.closest("#wsNewReel")) return ws.newReel($("#wsVideo").currentTime);
    const b = e.target.closest(".rc[data-id]");
    if (b) ws.select(b.dataset.id);
  });

  // details panel: saves as you type (still a draft)
  ["wdTitle", "wdHook", "wdCaption", "wdTags"].forEach((id) => {
    const el = $("#" + id);
    el.addEventListener("input", () => ws.saveSoon(el.dataset.field, el.value));
  });
  $("#wdCrop").addEventListener("change", (e) => ws.save(ws.selected, { crop_mode: e.target.value || "default" }));
  ["wdStart", "wdEnd"].forEach((id) => $("#" + id).addEventListener("change", (e) => {
    const t = parseTime(e.target.value);
    if (!isFinite(t)) { toast("Times must look like 1:23.5", true); return ws.fillDetail(true); }
    if (id === "wdStart") { ws.setIn(t); ws.seek(t); } else { ws.setOut(t); ws.seek(t - 0.04); }
  }));
  ws.root.querySelectorAll("[data-nudge]").forEach((b) => (b.onclick = () => {
    const c = ws.clip(); if (!c) return;
    const [f, d] = b.dataset.nudge.split(":"), [s, e] = ws.range(c);
    if (f === "start") { ws.setTimes(s + +d, e); ws.seek(ws.range(c)[0]); } else { ws.setTimes(s, e + +d); ws.seek(ws.range(c)[1] - 0.04); }
    ws.commitTimes();
  }));
  $("#wdPrev").onclick = () => { const cs = ws.clips(), i = cs.indexOf(ws.clip()); if (i > 0) ws.select(cs[i - 1].id); };
  $("#wdNext").onclick = () => { const cs = ws.clips(), i = cs.indexOf(ws.clip()); if (i < cs.length - 1) ws.select(cs[i + 1].id); };
  $("#wdExport").onclick = async () => {
    const c = ws.clip(); if (!c) return;
    if (c.export_state === "exported") return downloadFile(clipDownloadUrl(ws.jobId, c));  // already made: just download it
    try {
      ws.autoDownload.add(c.id);  // download it as soon as it's made
      applyJob(await api(`/api/jobs/${ws.jobId}/clips/${c.id}/render`, { method: "POST" }));
      toast(`Generating reel #${c.index}. It downloads by itself when it's ready.`);
    } catch (e) { ws.autoDownload.delete(c.id); toast(e.message, true); }
  };
  $("#wdWatch").onclick = () => { const c = ws.clip(); if (c?.video_url) openBig(c.video_url, c.thumb_url || ""); };
  // no post text yet (e.g. a reel made by hand): Claude writes it; otherwise copy it
  $("#wdCopy").onclick = async () => {
    const c = ws.clip(); if (!c) return;
    if (!hasPostText(c)) return writePostText(c);
    try { await navigator.clipboard.writeText(`${c.title}\n\n${c.caption}\n\n${(c.hashtags || []).join(" ")}`); toast("Post text copied"); }
    catch { toast("Could not access the clipboard", true); }
  };
  $("#wdRewrite").onclick = () => { const c = ws.clip(); if (c) writePostText(c); };
  $("#wdReset").onclick = () => {
    const c = ws.clip(); if (!c?.suggested) return;
    if (!confirm("Go back to the times and text that were first suggested for this reel?")) return;
    const sg = c.suggested;
    ws.save(c.id, { start: sg.start, end: sg.end, title: sg.title, hook: sg.hook, caption: sg.caption, hashtags: sg.hashtags });
    ws.fillDetail(true);
  };
  $("#wdDelete").onclick = async () => {
    const c = ws.clip(); if (!c) return;
    const what = c.video_url ? "this reel and its generated file" : "this draft reel";
    if (!confirm(`Delete ${what}?`)) return;
    try { applyJob(await api(`/api/jobs/${ws.jobId}/clips/${c.id}`, { method: "DELETE" })); } catch (e) { toast(e.message, true); }
  };
}

// keyboard shortcuts while a video is open (not while typing, not under a dialog)
document.addEventListener("keydown", (e) => {
  if (!ws.root || !ws.started || e.ctrlKey || e.metaKey) return;
  if (e.target.closest?.("input, textarea, select") || document.querySelector("dialog[open]")) return;
  const v = $("#wsVideo"), k = e.key, step = e.shiftKey ? 0.1 : 1;
  if (k === " ") wsTogglePlay();
  else if ((k === "i" || k === "I") && e.shiftKey) ws.setIn(v.currentTime);   // Shift+I: the selected reel starts here
  else if ((k === "o" || k === "O") && e.shiftKey) ws.setOut(v.currentTime);  // Shift+O: the selected reel ends here
  else if (k === "i" || k === "I") ws.markStart(v.currentTime);  // I: mark the start of a new reel
  else if (k === "o" || k === "O") ws.markEnd(v.currentTime);    // O: mark its end → the reel is created
  else if (k === "n" || k === "N") ws.newReel(v.currentTime);
  else if (k === "Escape" && ws.mark) ws.cancelMark();
  else if (k === "ArrowLeft") { v.pause(); ws.seek(v.currentTime - step); }
  else if (k === "ArrowRight") { v.pause(); ws.seek(v.currentTime + step); }
  else if (k === "Home") { const c = ws.clip(); v.pause(); ws.seek(c ? ws.range(c)[0] : 0); }
  else if (k === "End") { const c = ws.clip(); v.pause(); ws.seek(c ? ws.range(c)[1] - 0.04 : ws.dur); }
  else if (k === "+" || k === "=") ws.zoom(0.5, v.currentTime);
  else if (k === "-") ws.zoom(2, v.currentTime);
  else return;
  e.preventDefault();
});
window.addEventListener("resize", () => ws.render());
