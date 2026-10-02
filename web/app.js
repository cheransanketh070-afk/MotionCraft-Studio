// MotionCraft Studio UI — no framework, no inline code, all DOM text via textContent.
const $ = (id) => document.getElementById(id);
const IDEAS = [
  "Cinematic luxury ad for Aurora Perfume, golden light, slow orbit, 20% off, order now",
  "Neon energy drink for Volt Cola, electric night vibe, buy 1 get 1, shop now",
  "Clean minimal studio ad for a smart speaker, floating product, learn more",
  "Fresh organic water bottle for HydroPeak, summer, macro reveal, free shipping",
];
const state = { cfg: null, key: sessionStorage.getItem("mc_key") || "", file: null, jobId: null, timer: null,
  duration: 5, aspect: "9:16", quality: "draft", audio: "true" };
const LS = "mc_library_v1";
const lib = () => { try { return JSON.parse(localStorage.getItem(LS) || "[]"); } catch { return []; } };
const saveLib = (a) => { try { localStorage.setItem(LS, JSON.stringify(a.slice(0, 40))); } catch {} };

function headers(json) {
  const h = {};
  if (state.key) h.Authorization = "Bearer " + state.key;
  if (json) h["Content-Type"] = "application/json";
  return h;
}
async function api(path, opt = {}) {
  const r = await fetch(path, { ...opt, headers: { ...headers(), ...(opt.headers || {}) } });
  let data = {};
  try { data = await r.json(); } catch {}
  if (r.status === 401) { askKey(); }
  if (!r.ok) throw new Error(data.error || "Request failed (" + r.status + ")");
  return data;
}
const say = (t, err = false) => { const m = $("msg"); m.textContent = t; m.classList.toggle("err", err); };

function segment(id, options, key, fmt) {
  const el = $(id); el.textContent = "";
  options.forEach(([val, label]) => {
    const b = document.createElement("button");
    b.type = "button"; b.setAttribute("role", "radio"); b.textContent = label;
    b.setAttribute("aria-checked", String(String(state[key]) === String(val)));
    b.addEventListener("click", () => {
      state[key] = val;
      el.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      if (key === "aspect") $("screen").dataset.aspect = val;
    });
    el.appendChild(b);
  });
}

function setFile(f) {
  if (f && !/^image\/(png|jpeg|webp)$/.test(f.type)) return say("Please choose a PNG, JPEG or WebP image.", true);
  if (f && f.size > state.cfg.max_upload_mb * 1048576) return say("Image is larger than " + state.cfg.max_upload_mb + " MB.", true);
  state.file = f || null;
  $("drop-empty").hidden = !!f; $("drop-has").hidden = !f;
  if (f) { const u = URL.createObjectURL(f); $("thumb").src = u; $("fname").textContent = f.name || "pasted image"; }
  say("");
}

function formData() {
  const fd = new FormData();
  fd.append("prompt", $("prompt").value);
  fd.append("duration", state.duration); fd.append("aspect", state.aspect);
  fd.append("quality", state.quality); fd.append("audio", state.audio);
  fd.append("seed", $("f-seed").value || "0");
  [["product_name", "f-product"], ["headline", "f-headline"], ["offer", "f-offer"], ["cta", "f-cta"]]
    .forEach(([k, id]) => { if ($(id).value.trim()) fd.append(k, $(id).value.trim()); });
  if (state.file) fd.append("image", state.file);
  return fd;
}

function showPlan(p) {
  $("plan").hidden = false;
  const chips = $("plan-chips"); chips.textContent = "";
  [["Style", p.style], ["Music", p.bpm + " BPM " + p.scale], ["Headline", p.headline], ["Product", p.product_name],
   ["Offer", p.offer], ["Button", p.cta]].filter((x) => x[1]).forEach(([k, v]) => {
    const s = document.createElement("span"); const b = document.createElement("b"); b.textContent = v;
    s.append(k + ": ", b); chips.appendChild(s);
  });
  const ol = $("plan-shots"); ol.textContent = "";
  p.shots.forEach((s, i) => {
    const li = document.createElement("li");
    li.textContent = "Shot " + (i + 1) + " · " + s.start + "–" + s.end + "s · " + s.role + " · camera " + s.motion +
      " · text: " + s.texts.map((t) => t.key).join(", ");
    ol.appendChild(li);
  });
}

function busy(on) { $("busy").hidden = !on; $("go").disabled = on; if (on) { $("empty").hidden = true; $("video").hidden = true; $("result-actions").hidden = true; } }

function show(job) {
  state.jobId = job.id;
  if (job.aspect) { $("screen").dataset.aspect = job.aspect; }
  if (job.status === "queued" || job.status === "running") {
    busy(true);
    $("stage").textContent = job.status === "queued" ? "Waiting in queue" + (job.queue_position ? " (#" + job.queue_position + ")" : "") + "…" : job.stage + "…";
    $("pct").textContent = Math.round((job.progress || 0) * 100) + "%";
    $("bar").style.width = (job.progress || 0) * 100 + "%";
    return;
  }
  busy(false);
  if (job.status === "done") {
    const v = $("video"); v.src = job.video_url; v.hidden = false; $("empty").hidden = true;
    $("dl").href = job.video_url + "?download=1"; $("result-actions").hidden = false;
    v.play().catch(() => {});
    if (job.plan) showPlan(job.plan);
    say("Done — " + job.width + "×" + job.height + ", " + job.duration + "s MP4.");
  } else {
    $("empty").hidden = false;
    say(job.error || "Generation " + job.status + ".", true);
  }
  renderLibrary();
}

function poll(id) {
  clearInterval(state.timer);
  const tick = async () => {
    try {
      const j = await api("/api/v1/generations/" + id);
      if (state.jobId === id) show(j);
      if (!["queued", "running"].includes(j.status)) { clearInterval(state.timer); }
    } catch (e) { clearInterval(state.timer); busy(false); say(e.message, true); }
  };
  tick(); state.timer = setInterval(tick, 1000);
}

async function generate() {
  say("");
  if ($("prompt").value.trim().length < 3) return say("Describe your ad first.", true);
  try {
    busy(true); $("stage").textContent = "Submitting…";
    const j = await api("/api/v1/generations", { method: "POST", body: formData() });
    const a = lib().filter((x) => x.id !== j.id); a.unshift({ id: j.id, aspect: j.aspect, duration: j.duration, prompt: j.prompt });
    saveLib(a); renderLibrary(); poll(j.id);
  } catch (e) { busy(false); $("empty").hidden = false; say(e.message, true); }
}

async function preview() {
  try { say(""); showPlan(await api("/api/v1/plan", { method: "POST", body: formData() })); }
  catch (e) { say(e.message, true); }
}

async function renderLibrary() {
  const g = $("gallery"); const items = lib(); g.textContent = "";
  if (!items.length) { const p = document.createElement("p"); p.style.color = "var(--mute)"; p.textContent = "Nothing yet — your videos will show up here."; g.appendChild(p); return; }
  const keep = [];
  await Promise.all(items.map(async (it) => {
    try { it.job = await api("/api/v1/generations/" + it.id); keep.push(it); } catch { /* expired */ }
  }));
  const alive = items.filter((x) => keep.includes(x));
  if (alive.length !== items.length) saveLib(alive.map(({ id, aspect, duration, prompt }) => ({ id, aspect, duration, prompt })));
  g.textContent = "";
  alive.forEach((it) => {
    const b = document.createElement("button"); b.type = "button";
    b.className = "card" + (it.aspect === "16:9" ? " land" : "") + (it.id === state.jobId ? " sel" : "");
    b.title = it.prompt || "";
    if (it.job.status === "done") { const im = document.createElement("img"); im.alt = it.prompt || "video"; im.loading = "lazy"; im.src = it.job.thumb_url; b.appendChild(im); }
    else { const d = document.createElement("div"); d.className = "ph"; d.textContent = it.job.status; b.appendChild(d); }
    const t = document.createElement("span"); t.className = "tag"; t.textContent = it.duration + "s · " + it.aspect; b.appendChild(t);
    b.addEventListener("click", () => { state.jobId = it.id; poll(it.id); window.scrollTo({ top: 0, behavior: "smooth" }); });
    g.appendChild(b);
  });
}

function askKey() { const d = $("key-dialog"); if (!d.open) d.showModal(); }

async function init() {
  state.cfg = await (await fetch("/api/v1/config")).json();
  $("brand").textContent = state.cfg.brand; document.title = state.cfg.brand + " Studio — AI ad video generator";
  if (state.cfg.auth_required) { $("btn-key").hidden = false; if (!state.key) askKey(); }
  segment("seg-duration", state.cfg.durations.map((d) => [d, d + "s"]), "duration");
  segment("seg-aspect", [["9:16", "9:16 Vertical"], ["16:9", "16:9 Wide"]], "aspect");
  const ql = { draft: "Draft 540p", hd: "HD 720p", fhd: "Full HD" };
  segment("seg-quality", state.cfg.qualities.map((q) => [q, ql[q]]), "quality");
  segment("seg-audio", [["true", "Music on"], ["false", "Silent"]], "audio");
  IDEAS.forEach((t) => { const b = document.createElement("button"); b.type = "button"; b.textContent = t.split(",")[0]; b.title = t;
    b.addEventListener("click", () => { $("prompt").value = t; $("prompt").focus(); }); $("ideas").appendChild(b); });
  const drop = $("drop"), file = $("file");
  drop.addEventListener("click", (e) => { if (e.target.id !== "rm") file.click(); });
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
  file.addEventListener("change", () => setFile(file.files[0]));
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));
  document.addEventListener("paste", (e) => { const f = [...(e.clipboardData?.files || [])].find((x) => x.type.startsWith("image/")); if (f) setFile(f); });
  $("rm").addEventListener("click", (e) => { e.stopPropagation(); file.value = ""; setFile(null); });
  $("shuffle").addEventListener("click", () => { $("f-seed").value = Math.floor(Math.random() * 1e9); });
  $("go").addEventListener("click", generate); $("preview").addEventListener("click", preview);
  $("btn-key").addEventListener("click", askKey);
  $("key-dialog").addEventListener("close", () => { const v = $("key-input").value.trim(); if ($("key-dialog").returnValue === "ok" && v) { state.key = v; sessionStorage.setItem("mc_key", v); renderLibrary(); } $("key-input").value = ""; });
  $("remix").addEventListener("click", () => { $("f-seed").value = Math.floor(Math.random() * 1e9); generate(); });
  $("del").addEventListener("click", async () => {
    if (!state.jobId) return;
    try { await api("/api/v1/generations/" + state.jobId, { method: "DELETE" }); } catch {}
    saveLib(lib().filter((x) => x.id !== state.jobId)); state.jobId = null;
    $("video").hidden = true; $("video").removeAttribute("src"); $("result-actions").hidden = true; $("empty").hidden = false; renderLibrary();
  });
  renderLibrary();
}
init().catch(() => say("Could not reach the server.", true));
