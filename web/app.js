"use strict";

// ───────────────────────── 常量 / 工具 ─────────────────────────

const KINDS = [
  { id: "pdf", label: "PDF", color: "#c4382b", on: true, exts: ["pdf"] },
  { id: "word", label: "Word", color: "#2f5fd0", on: true, exts: ["doc", "docx", "odt", "rtf", "wps"] },
  { id: "ppt", label: "PPT", color: "#d0661a", on: true, exts: ["ppt", "pptx", "pps", "ppsx", "odp", "key", "dps"] },
  { id: "sheet", label: "表格", color: "#1f8a4c", on: true, exts: ["xls", "xlsx", "ods", "csv", "et"] },
  { id: "text", label: "文本", color: "#6b7280", on: true, exts: ["txt", "md"] },
  { id: "book", label: "电子书", color: "#0f8a8a", on: true, exts: ["epub", "mobi", "azw3", "djvu", "chm", "caj"] },
  { id: "zip", label: "压缩包", color: "#7b4bd6", on: true, exts: ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"] },
  { id: "code", label: "代码/数据", color: "#55657a", on: true, exts: ["ipynb", "mat", "py", "m", "c", "cpp", "h", "java"] },
  { id: "media", label: "音视频", color: "#c0306f", on: false, exts: ["mp4", "mkv", "webm", "mov", "avi", "flv", "mp3", "m4a", "wav", "flac", "ogg"] },
  { id: "image", label: "图片", color: "#1188b0", on: false, exts: ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"] },
  { id: "web", label: "JS/CSS", color: "#a3770c", on: false, exts: ["js", "mjs", "css", "wasm"] },
];
const OTHER = { id: "other", label: "其他", color: "#7a766c", exts: [] };
const EXT_KIND = new Map(KINDS.flatMap((k) => k.exts.map((e) => [e, k])));
const kindOf = (ext) => EXT_KIND.get((ext || "").toLowerCase()) || OTHER;
const PREVIEW = new Set(["pdf", "txt", "md", "csv", "py", "m", "c", "cpp", "h", "java", "jpg", "jpeg", "png", "gif", "webp", "bmp", "mp4", "webm", "mp3", "m4a", "wav", "ogg"]);

const DEFAULT_OPTS = {
  depth: 1, js: "auto", kinds: KINDS.filter((k) => k.on).map((k) => k.id), all: false,
  include: "", exclude: "", scope: "host", extra: "", max_pages: 300, delay: 1, max_size: "", proxy: "",
  js_click: false, probe_size: false, insecure: false,
};
const SAVED_KEYS = Object.keys(DEFAULT_OPTS); // Cookie / 请求头不落盘
const depthOf = (v) => (v === "all" ? v : Number(v)); // "all" = 全站：不限层数 + 读 sitemap
const nextDepth = (d) => (d === "all" ? null : d >= 3 ? "all" : d + 1);
// 扫描因为达到“最多扫描网页数”停下：还有多少网页没扫
const cappedNote = (s) => (s?.capped ? `达到最多扫描网页数 ${s.max_pages}，还有 ${s.capped} 个网页没扫` : "");

const ICON = {
  get: '<path d="M12 4v11m0 0l-4.5-4.5M12 15l4.5-4.5M5 19h14"/>',
  view: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>',
};
const icon = (n) => `<svg class="i" viewBox="0 0 24 24">${ICON[n]}</svg>`;

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const decode = (u) => { try { return decodeURI(u); } catch { return u; } };
const shortUrl = (u) => decode(u).replace(/^https?:\/\//, "");

function human(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n} B` : `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
}
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.round(s)} 秒` : s < 3600 ? `${Math.round(s / 60)} 分钟` : s < 86400 ? `${Math.round(s / 3600)} 小时` : `${Math.round(s / 86400)} 天`;
}
function clock(sec) {
  sec = Math.max(0, Math.round(sec));
  return sec < 60 ? `${sec} 秒` : `${Math.floor(sec / 60)} 分 ${sec % 60} 秒`;
}
function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem(key) || "null");
    localStorage.setItem(key, JSON.stringify(val));
  } catch { return null; }
}

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `请求失败（HTTP ${r.status}）`);
  return data;
}

function toast(msg, kind = "", action) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="t-msg">${esc(msg)}</span>`;
  if (action) {
    const b = document.createElement("button");
    b.className = "link";
    b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.appendChild(b);
  }
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), action ? 9000 : 4500);
}

// ───────────────────────── 状态 ─────────────────────────

const S = {
  cfg: { out: "", js: true },
  opts: { ...DEFAULT_OPTS, ...(pick(store("sc.opts") || {}, SAVED_KEYS)) },
  job: null, // { id, url, status, title, js, started, ended }
  files: new Map(),
  order: [],
  pages: [],
  logs: 0,
  sel: new Set(),
  q: "",
  kind: "all",
  group: store("sc.group") || "page",
  sort: "found",
  es: null,
  openedAt: 0,
  dl: { running: false, batch: new Set(), authMsg: "" },
  zipWait: null,
  lastClick: null,
  scanSummary: null,
  apis: [],
  auth: new Map(),
};
function pick(o, keys) { return Object.fromEntries(keys.filter((k) => k in o).map((k) => [k, o[k]])); }
function saveOpts() { store("sc.opts", pick(S.opts, SAVED_KEYS)); }

// ───────────────────────── 扫描参数 ─────────────────────────

function buildParams(url) {
  const o = S.opts;
  let ext = "*";
  if (!o.all) {
    const set = new Set();
    for (const k of KINDS) if (o.kinds.includes(k.id)) k.exts.forEach((e) => set.add(e));
    o.extra.split(/[\s,，;；]+/).filter(Boolean).forEach((e) => set.add(e.replace(/^\*?\./, "").toLowerCase()));
    ext = [...set].join(",");
  }
  let cookie = $("#oCookie").value.trim().replace(/^cookie:\s*/i, "");
  const netscape = cookie.split("\n").some((l) => l.split("\t").length >= 7);
  return {
    url, depth: o.depth, js: S.cfg.js ? o.js : "off", js_click: o.js_click, ext,
    include: o.include, exclude: o.exclude, scope: o.scope, max_pages: o.max_pages, delay: o.delay,
    max_size: o.max_size, probe_size: o.probe_size, insecure: o.insecure, proxy: o.proxy,
    headers: $("#oHeaders").value, cookie: netscape ? "" : cookie, cookies_txt: netscape ? cookie : "",
  };
}

async function startScan(url) {
  url = (url ?? $("#url").value).trim();
  if (!url) { $("#url").focus(); return; }
  if (!S.opts.all && !S.opts.kinds.length && !S.opts.extra.trim()) {
    toast("至少选一种文件类型", "bad");
    return;
  }
  $("#url").value = url;
  $("#scanBtn").disabled = true;
  try {
    const { id } = await api("/api/scan", buildParams(url));
    rememberUrl(url);
    $("#more").hidden = true; // 收起选项，让结果露出来
    $("#moreBtn").setAttribute("aria-expanded", "false");
    location.hash = `#/job/${id}`; // hashchange → openJob
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    $("#scanBtn").disabled = false;
  }
}

function rememberUrl(url) {
  const list = (store("sc.urls") || []).filter((u) => u !== url);
  list.unshift(url);
  store("sc.urls", list.slice(0, 8));
}

// ───────────────────────── 任务 / 事件流 ─────────────────────────

function resetJob() {
  if (S.es) S.es.close();
  Object.assign(S, {
    job: null, files: new Map(), order: [], pages: [], logs: 0, sel: new Set(), q: "", kind: "all",
    es: null, dl: { running: false, batch: new Set(), authMsg: "" }, zipWait: null, lastClick: null, scanSummary: null,
    apis: [], auth: new Map(),
  });
  $("#log").textContent = "";
  $("#pages").textContent = "";
  $("#apis").textContent = "";
  $("#auth").hidden = true;
  $("#q").value = "";
}

async function openJob(id) {
  resetJob();
  let info;
  try {
    info = await api(`/api/jobs/${id}`);
  } catch {
    toast("这个扫描任务已失效（服务重启过），请重新扫描", "bad");
    history.replaceState(null, "", "/");
    showIdle();
    return;
  }
  S.job = { id, url: info.url, status: info.status, title: info.title, js: info.js, started: info.created * 1000, ended: 0 };
  S.openedAt = Date.now() / 1000;
  $("#url").value = decode(info.url);
  refreshLogin();
  $("#intro").hidden = true;
  $("#status").hidden = false;
  $("#results").hidden = false;
  render();
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = (m) => handle(JSON.parse(m.data));
  es.onerror = async () => {
    if (es.readyState === EventSource.CLOSED) return;
    try { await api(`/api/jobs/${id}`); } catch { es.close(); }
  };
  S.es = es;
}

function showIdle() {
  resetJob();
  $("#intro").hidden = false;
  $("#status").hidden = true;
  $("#results").hidden = true;
  $("#selbar").hidden = true;
  loadRecent();
}

function handle(ev) {
  const live = ev.t >= S.openedAt - 1; // 回放的历史事件不弹提示
  const j = S.job;
  switch (ev.type) {
    case "meta":
      j.js = ev.js;
      j.login = ev.login;
      break;
    case "api":
      S.apis.push(ev);
      addApi(ev);
      break;
    case "auth":
      S.auth.set(ev.host, ev.headers);
      renderAuth();
      break;
    case "log":
      addLog(ev);
      break;
    case "page":
      S.pages.push(ev);
      if (ev.depth === 0 && ev.title && !j.title) j.title = ev.title;
      addPage(ev);
      renderStatus();
      break;
    case "file":
      if (!S.files.has(ev.file.fid)) {
        S.files.set(ev.file.fid, { ...ev.file, dl: {} });
        S.order.push(ev.file.fid);
        schedule();
      }
      break;
    case "dl": {
      const f = S.files.get(ev.fid);
      if (!f) break;
      const was = f.dl.status;
      f.dl = ev;
      if (ev.status === "queued") S.dl.batch.add(ev.fid);
      if (ev.status === "running" && was === "running" && patchProgress(f)) { renderDlStrip(); break; }
      schedule();
      checkZip();
      break;
    }
    case "dlrun":
      S.dl.running = ev.status === "running";
      if (ev.status === "idle") dlFinished(live);
      schedule();
      break;
    case "authlost":
      S.dl.authMsg = ev.msg;
      schedule();
      break;
    case "summary":
      if (ev.phase === "scan") S.scanSummary = ev;
      break;
    case "status":
      j.status = ev.status;
      j.ended = ev.t * 1000;
      if (live && ev.status === "done") {
        const cap = cappedNote(S.scanSummary);
        toast(`扫描完成：找到 ${S.files.size} 个文件` + (cap ? `（${cap}，可在更多选项里调大）` : ""), cap ? "" : "ok");
      }
      if (live && ev.status === "error") toast("扫描出错，详见运行日志", "bad");
      if (live) refreshLogin(); // 扫描时可能嗅探到了新令牌
      schedule();
      loadRecent();
      break;
  }
}

function dlFinished(live) {
  const st = [...S.dl.batch].map((fid) => S.files.get(fid)?.dl.status);
  if (st.some((s) => s === "queued" || s === "running")) return; // 还有下一批排着
  const ok = st.filter((s) => s === "done").length;
  const bad = st.filter((s) => s === "failed").length;
  const paused = st.filter((s) => s === "auth").length;
  const rest = st.length - ok - bad - paused;
  S.dl.batch = new Set();
  if (!live || !st.length) return;
  if (paused) {
    toast(`下载已暂停：登录失效，${paused} 个文件等你重新登录后接着下` + (ok ? `（已保存 ${ok} 个）` : ""), "bad",
      { label: "重新登录", run: relogin });
    return;
  }
  const msg = `下载结束：保存 ${ok} 个` + (bad ? `，失败 ${bad} 个` : "") + (rest ? `，跳过 / 停止 ${rest} 个` : "");
  toast(msg, bad ? "bad" : "ok", { label: "打开文件夹", run: () => api("/api/reveal", {}) });
}

// ───────────────────────── 渲染 ─────────────────────────

let pending = false;
function schedule() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => { pending = false; render(); });
}

function render() {
  renderStatus();
  renderKindFilter();
  renderList();
  renderDlStrip();
  renderAuthStrip();
  renderSelbar();
}

let tick = null;
function renderStatus() {
  const j = S.job;
  if (!j) return;
  const box = $("#status");
  box.className = `card status ${j.status}`;
  const host = shortUrl(j.url);
  $("#stTitle").textContent = {
    running: `正在扫描 ${j.title || host} …`,
    done: `扫描完成 · ${j.title || host}`,
    stopped: `已停止 · ${j.title || host}`,
    error: `扫描出错 · ${j.title || host}`,
  }[j.status] || host;
  const ok = S.pages.filter((p) => p.ok).length;
  const bad = S.pages.length - ok;
  const end = j.status === "running" ? Date.now() : (j.ended || Date.now());
  const meta = [
    `${ok} 个网页` + (bad ? `（${bad} 个失败 / 跳过）` : ""),
    `${S.files.size} 个文件`,
    `用时 ${clock((end - j.started) / 1000)}`,
    { auto: "动态网页：自动", on: "动态网页：总是渲染", off: "未渲染 JS" }[j.js] || "",
    j.login ? "带登录状态" : "",
    j.status === "running" ? "" : cappedNote(S.scanSummary),
  ];
  $("#stMeta").textContent = meta.filter(Boolean).join(" · ");
  $("#stopScan").hidden = j.status !== "running";
  $("#rescan").hidden = j.status === "running";
  $("#pageCount").textContent = S.pages.length ? `(${S.pages.length})` : "";
  $("#apiBox").hidden = !S.apis.length && !S.auth.size;
  clearInterval(tick);
  if (j.status === "running") tick = setInterval(renderStatus, 1000);
}

function addLog(ev) {
  const pre = $("#log");
  const line = document.createElement("div");
  line.className = ev.level;
  const t = new Date(ev.t * 1000).toTimeString().slice(0, 8);
  line.textContent = `${t}  ${ev.msg}`;
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  pre.appendChild(line);
  while (pre.childElementCount > 600) pre.firstChild.remove();
  if (atBottom) pre.scrollTop = pre.scrollHeight;
  $("#logCount").textContent = `(${++S.logs})`;
}

function addPage(p) {
  const li = document.createElement("li");
  if (!p.ok) li.className = "bad";
  li.innerHTML = `<a href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">${esc(p.title || shortUrl(p.url))}</a>` +
    `<span class="tag">${p.ok ? `${p.links} 个链接` : esc(p.note || "失败")}</span>`;
  $("#pages").appendChild(li);
}

function addApi(a) {
  const li = document.createElement("li");
  const hit = a.links || a.attachment;
  if (hit) li.className = "hit";
  const what = a.links ? `${a.links} 个链接` : a.attachment ? `附件 ${a.attachment}` : esc(a.type || "");
  li.innerHTML = `<span class="m">${esc(a.method)}</span><span class="u" title="${esc(a.url)}">${esc(shortUrl(a.url))}</span>` +
    `<span class="tag">HTTP ${a.status || "?"}${what ? " · " + what : ""}</span>`;
  // 找到文件的接口排前面
  const list = $("#apis");
  if (hit) list.insertBefore(li, $("li:not(.hit)", list)); else list.appendChild(li);
  $("#apiCount").textContent = `(${S.apis.length})`;
  renderStatus();
}

function renderAuth() {
  const box = $("#auth");
  box.hidden = !S.auth.size;
  box.innerHTML = icon("key") + `<span>嗅探到页面取数据用的登录凭证：${[...S.auth].map(([h, names]) =>
    `<b>${esc(h)}</b>（${esc(names.join("、"))}）`).join("，")}。下载时会带上，只发给对应主机。</span>`;
  renderStatus();
}

function visible() {
  const q = S.q.trim().toLowerCase();
  let arr = S.order.map((id) => S.files.get(id));
  if (S.kind !== "all") arr = arr.filter((f) => kindOf(f.ext).id === S.kind);
  if (q) arr = arr.filter((f) => `${f.name} ${f.anchor} ${decode(f.url)}`.toLowerCase().includes(q));
  if (S.sort === "name") arr.sort((a, b) => a.name.localeCompare(b.name, "zh-CN", { numeric: true }));
  if (S.sort === "size") arr.sort((a, b) => (b.size || 0) - (a.size || 0));
  return arr;
}

function groups(arr) {
  if (S.group === "none") return [{ key: "", title: "", url: "", files: arr }];
  const m = new Map();
  for (const f of arr) {
    let key, title, url;
    if (S.group === "kind") {
      const k = kindOf(f.ext);
      key = k.id; title = k.label; url = "";
    } else {
      key = f.referer || "-";
      title = f.referer ? f.folder || shortUrl(f.referer) : "直接给出的文件链接";
      url = f.referer;
    }
    if (!m.has(key)) m.set(key, { key, title, url, files: [] });
    m.get(key).files.push(f);
  }
  return [...m.values()];
}

function renderKindFilter() {
  const counts = new Map();
  for (const f of S.files.values()) {
    const k = kindOf(f.ext);
    counts.set(k.id, (counts.get(k.id) || 0) + 1);
  }
  if (!counts.size) { $("#kindFilter").innerHTML = ""; return; }
  if (S.kind !== "all" && !counts.has(S.kind)) S.kind = "all";
  const chip = (id, label, n, color) =>
    `<button class="chip${id === "all" ? " all" : ""}" data-kind="${id}" aria-pressed="${S.kind === id}"` +
    (color ? ` style="--c:${color}"` : "") + `>${color ? '<span class="sw"></span>' : ""}${esc(label)} <span class="n">${n}</span></button>`;
  let html = chip("all", "全部", S.files.size);
  for (const k of [...KINDS, OTHER]) if (counts.has(k.id)) html += chip(k.id, k.label, counts.get(k.id), k.color);
  $("#kindFilter").innerHTML = html;
}

function statusCell(f) {
  const d = f.dl || {};
  switch (d.status) {
    case "queued": return '<span class="pill">排队中</span>';
    case "running": {
      const pct = d.total ? Math.min(100, (d.done / d.total) * 100) : 0;
      return `<div class="prog"><i style="width:${d.total ? pct.toFixed(1) : 100}%;${d.total ? "" : "opacity:.35"}"></i></div>` +
        `<span class="pct">${d.total ? `${pct.toFixed(0)}%` : human(d.done)}</span>`;
    }
    case "done":
      return `<button class="pill ok" data-act="reveal" title="${esc((d.note ? d.note + "\n" : "") + "已保存：" + (d.path || ""))}">${icon("check")}${d.note ? "已有" : "已保存"}</button>`;
    case "failed": return `<span class="pill bad" title="${esc(d.note)}">失败：${esc(shortNote(d.note))}</span>`;
    case "skipped": return `<span class="pill" title="${esc(d.note)}">跳过：${esc(shortNote(d.note))}</span>`;
    case "stopped": return '<span class="pill">已停止</span>';
    case "auth": return `<span class="pill warn" title="${esc(d.note)}">等重新登录</span>`;
    default: return "";
  }
}
function shortNote(n) {
  n = n || "";
  if (/登录已失效/.test(n)) return "登录失效";
  if (/需要登录|登录页/.test(n)) return "需要登录";
  if (/网页而不是文件/.test(n)) return "回的是网页";
  if (/接口返回了错误/.test(n)) return "接口报错";
  if (/JSON 数据而不是文件/.test(n)) return "回的是 JSON";
  if (/不是有效的|空文件/.test(n)) return "内容不对";
  if (/HTTP (\d+)/.test(n)) return n.match(/HTTP \d+/)[0];
  if (/robots/.test(n)) return "robots 禁止";
  if (/过大/.test(n)) return "文件过大";
  return n.length > 10 ? n.slice(0, 10) + "…" : n;
}

function rowHtml(f) {
  const k = kindOf(f.ext);
  const sel = S.sel.has(f.fid);
  const anchor = f.anchor && f.anchor !== f.name && f.anchor.length < 120 ? `<span class="anchor">“${esc(f.anchor)}”</span>` : "";
  const note = f.note && !f.dl.status ? `<span class="note" title="${esc(f.note)}">⚠ ${esc(shortNote(f.note))}</span>` : "";
  return `<div class="row${sel ? " sel" : ""}" data-fid="${f.fid}">
  <label class="ck"><input type="checkbox"${sel ? " checked" : ""} aria-label="选择 ${esc(f.name)}"></label>
  <span class="badge" style="--c:${k.color}">${esc((f.ext || "?").toUpperCase().slice(0, 5))}</span>
  <div class="nm"><div class="name" title="${esc(f.name)}">${esc(f.name)}</div>
    <div class="sub">${note}${anchor}<span class="u" title="${esc(decode(f.url))}">${esc(shortUrl(f.url))}</span></div></div>
  <div class="size">${human(f.dl.total || f.dl.size || f.size)}</div>
  <div class="st">${statusCell(f)}</div>
  <div class="acts">
    <button class="ib go" data-act="get" title="直接保存到浏览器（经本机转发，带 Cookie / Referer）">${icon("get")}</button>
    ${PREVIEW.has(f.ext) ? `<button class="ib" data-act="view" title="在新标签页预览">${icon("view")}</button>` : '<span class="ib" aria-hidden="true"></span>'}
    <button class="ib" data-act="copy" title="复制链接">${icon("copy")}</button>
  </div>
</div>`;
}

let shown = [];
function renderList() {
  const arr = visible();
  shown = arr.map((f) => f.fid);
  const list = $("#list");
  let html = "";
  for (const g of groups(arr)) {
    if (g.title) {
      const allSel = g.files.every((f) => S.sel.has(f.fid));
      html += `<div class="group-h" data-group="${esc(g.key)}">
  <label class="ck"><input type="checkbox"${allSel ? " checked" : ""} aria-label="选择这一组"></label>
  <span class="gt">${esc(g.title)}</span>
  ${g.url ? `<a class="gu" href="${esc(g.url)}" target="_blank" rel="noopener noreferrer">${esc(shortUrl(g.url))}</a>` : '<span class="gu"></span>'}
  <span class="gn">${g.files.length} 个</span></div>`;
    }
    html += g.files.map(rowHtml).join("");
  }
  list.innerHTML = html;
  const all = $("#selAll");
  const nSel = shown.filter((id) => S.sel.has(id)).length;
  all.checked = shown.length > 0 && nSel === shown.length;
  all.indeterminate = nSel > 0 && nSel < shown.length;
  renderEmpty(arr);
}

function renderEmpty(arr) {
  const box = $("#empty");
  const j = S.job;
  if (arr.length || !j) { box.hidden = true; return; }
  box.hidden = false;
  if (S.files.size) {
    box.innerHTML = `<h3>没有符合筛选条件的文件</h3><div class="row-btns"><button class="btn sm" data-empty="clear">清除筛选</button></div>`;
  } else if (j.status === "running") {
    box.innerHTML = `<p class="muted">正在找文件…找到的会立刻出现在这里。</p>`;
  } else {
    const deeper = nextDepth(S.opts.depth);
    const tips = [
      cappedNote(S.scanSummary) ? `${cappedNote(S.scanSummary)}：在<b>更多选项</b>里调大「最多扫描网页数」（0 = 不限）` : "",
      deeper === "all" ? "资料可能在更深的子页面里：把<b>深度</b>设为「全站」（还会读网站的 sitemap.xml）"
        : deeper ? "资料可能在子页面里：把<b>深度</b>调大一层" : "",
      j.js !== "on" && S.cfg.js ? "页面可能是 JS 动态加载的：把<b>动态网页</b>设为「总是」，并勾选「点击下载按钮」" : "",
      j.login ? "" : "资料要登录才能看到：点上方<b>在浏览器里登录</b>，登录后再扫描",
      "网站的 robots.txt 可能禁止抓取：展开上面的<b>运行日志</b>看看",
      S.opts.all ? "" : "文件类型可能没选上：试试类型里的「全部」",
    ].filter(Boolean);
    box.innerHTML = `<h3>没找到可下载的文件</h3><ul>${tips.map((t) => `<li>${t}</li>`).join("")}</ul>
      <div class="row-btns">
        ${deeper !== null ? `<button class="btn sm" data-empty="deeper">${deeper === "all" ? "扫描全站" : "深度 +1 重新扫描"}</button>` : ""}
        ${j.js !== "on" && S.cfg.js ? '<button class="btn sm primary" data-empty="js">用浏览器渲染重新扫描</button>' : ""}
      </div>`;
  }
}

function patchProgress(f) {
  const row = $(`.row[data-fid="${f.fid}"]`);
  const bar = row && $(".prog i", row);
  if (!bar) return false;
  const d = f.dl;
  if (d.total) bar.style.width = `${Math.min(100, (d.done / d.total) * 100).toFixed(1)}%`;
  $(".pct", row).textContent = d.total ? `${Math.min(100, (d.done / d.total) * 100).toFixed(0)}%` : human(d.done);
  return true;
}

function renderDlStrip() {
  const batch = [...S.dl.batch].map((id) => S.files.get(id)).filter(Boolean);
  const active = batch.some((f) => f.dl.status === "queued" || f.dl.status === "running");
  $("#dlStrip").hidden = !active;
  if (!active) return;
  const total = batch.length;
  const fin = batch.filter((f) => !["queued", "running"].includes(f.dl.status)).length;
  let bytes = 0, known = 0;
  for (const f of batch) {
    const d = f.dl;
    if (d.status === "running") { bytes += d.done || 0; known += d.total || f.size || 0; }
    else if (d.status === "done") { bytes += d.size || 0; known += d.size || 0; }
    else known += f.size || 0;
  }
  $("#dlTitle").textContent = `正在下载 ${fin} / ${total}`;
  $("#dlMeta").textContent = bytes ? ` · ${human(bytes)}${known > bytes ? " / " + human(known) : ""}` : "";
  const pct = known ? (bytes / known) * 100 : (fin / total) * 100;
  $("#dlBar").style.width = `${Math.min(100, pct).toFixed(1)}%`;
}

function authFids() { return S.order.filter((id) => S.files.get(id)?.dl.status === "auth"); }

// 重新登录的窗口开着：横条上直接给「取消 / 我已登录」，不用滚回顶部找登录栏
const reloginOpen = () => L.resume && L.resume === S.job?.id && ["starting", "open"].includes(L.info?.session?.status);

function renderAuthStrip() {
  const n = authFids().length;
  $("#authStrip").hidden = !n || S.dl.running;
  if (!n) return;
  const open = reloginOpen();
  $("#authStrip b").textContent = open ? "请在弹出的浏览器窗口里登录" : "登录已失效，下载已暂停";
  $("#authMsg").textContent = `${n} 个文件保留了进度，` + (open ? "登录好了点右边继续" : "等你重新登录后接着下") +
    (S.dl.authMsg && !open ? ` · ${S.dl.authMsg.replace(/^登录已失效：/, "")}` : "");
  $("#authGo").textContent = open ? "取消" : "直接继续";
  $("#authGo").title = open ? "关掉登录窗口" : "已经在别处重新登录过，直接接着下";
  $("#authLogin").textContent = open ? "我已登录，继续下载" : "重新登录";
  if (!open) $("#authLogin").disabled = false; // 点「我已登录」时 loginAction 会先禁用它
}

// 下载时发现登录失效：重新登录（登录完自动继续，不重新扫描）
function relogin() {
  if (!S.job) return;
  $("#url").value = decode(S.job.url);
  L.resume = S.job.id;
  loginAction("start");
}

async function resumeAfterLogin() {
  L.resume = null;
  const fids = authFids();
  if (!fids.length) return;
  try {
    const r = await api(`/api/jobs/${S.job.id}/download`, { fids, after_login: true });
    S.dl.authMsg = "";
    toast(`继续下载 ${r.queued} 个文件`, "ok");
  } catch (e) { toast(e.message, "bad"); }
}

function renderSelbar() {
  const ids = [...S.sel].filter((id) => S.files.has(id));
  $("#selbar").hidden = !ids.length;
  if (!ids.length) return;
  let size = 0, unknown = 0, done = 0;
  for (const id of ids) {
    const f = S.files.get(id);
    const s = f.dl.size || f.size;
    if (s) size += s; else unknown++;
    if (f.dl.status === "done") done++;
  }
  $("#selCount").textContent = ids.length;
  $("#selSize").textContent = (size ? `约 ${human(size)}` : "") + (unknown ? `${size ? "，" : ""}${unknown} 个大小未知` : "");
  $("#actDl").disabled = done === ids.length;
  $("#actDl").lastChild.textContent = done === ids.length ? "已全部下载" : done ? `下载其余 ${ids.length - done} 个` : "下载到本机";
}

// ───────────────────────── 动作 ─────────────────────────

function selected() { return [...S.sel].filter((id) => S.files.has(id)); }

async function downloadSelected(fids = selected()) {
  if (!fids.length) return;
  try {
    const r = await api(`/api/jobs/${S.job.id}/download`, { fids });
    if (r.queued) toast(`开始下载 ${r.queued} 个文件 → ${r.out}`);
    else if (!S.zipWait) toast("所选文件都已下载过", "ok", { label: "打开文件夹", run: () => api("/api/reveal", {}) });
  } catch (e) { toast(e.message, "bad"); }
}

function zipSelected() {
  const fids = selected();
  if (!fids.length) return;
  const todo = fids.filter((id) => S.files.get(id).dl.status !== "done");
  if (!todo.length) return fetchZip(fids);
  S.zipWait = new Set(fids);
  toast(`先下载 ${todo.length} 个文件到本机，完成后自动打包`);
  downloadSelected(todo);
}
function checkZip() {
  if (!S.zipWait) return;
  const st = [...S.zipWait].map((id) => S.files.get(id)?.dl.status);
  if (st.some((s) => !s || s === "queued" || s === "running")) return;
  const fids = [...S.zipWait];
  S.zipWait = null;
  if (fids.some((id) => S.files.get(id).dl.status === "done")) fetchZip(fids);
  else toast("所选文件都没下载成功，无法打包", "bad");
}
function fetchZip(fids) {
  frameDownload(`/api/jobs/${S.job.id}/zip?fids=${fids.join(",")}`);
  toast("正在打包 ZIP，浏览器会开始下载", "ok");
}

// 隐藏 iframe 下载：成功时浏览器直接存文件；出错时服务端回一个小网页，读出原因提示
function frameDownload(url) {
  const ifr = document.createElement("iframe");
  ifr.hidden = true;
  ifr.onload = () => {
    try {
      const doc = ifr.contentDocument;
      if (doc?.body?.hasAttribute("data-sc-error")) toast(doc.getElementById("msg").textContent, "bad");
    } catch { /* 跨域或已开始下载 */ }
  };
  ifr.src = url;
  document.body.appendChild(ifr);
  setTimeout(() => ifr.remove(), 5 * 60 * 1000);
}

async function copyText(text, what) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(`已复制${what}`, "ok");
}

function exportCsv() {
  const rows = [["文件名", "类型", "大小(字节)", "链接", "链接文字", "来源网页", "状态", "本地路径"]];
  for (const id of selected()) {
    const f = S.files.get(id);
    rows.push([f.name, f.ext, f.dl.size || f.size || "", decode(f.url), f.anchor, f.referer, f.dl.status || "", f.dl.abs || ""]);
  }
  const csv = "﻿" + rows.map((r) => r.map((c) => `"${String(c ?? "").replace(/"/g, '""')}"`).join(",")).join("\r\n");
  const a = Object.assign(document.createElement("a"), {
    href: URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" })),
    download: `文件清单_${(S.job.title || "scan").slice(0, 30)}.csv`,
  });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function toggle(fid, on) {
  if (on ?? !S.sel.has(fid)) S.sel.add(fid); else S.sel.delete(fid);
}

// ───────────────────────── 选项面板 ─────────────────────────

function bindSeg(el, key, cast = (v) => v) {
  const paint = () => $$("button", el).forEach((b) => b.setAttribute("aria-checked", String(cast(b.dataset.v) === S.opts[key])));
  el.setAttribute("role", "radiogroup");
  $$("button", el).forEach((b) => {
    b.setAttribute("role", "radio");
    b.onclick = () => { S.opts[key] = cast(b.dataset.v); saveOpts(); paint(); paintMoreCount(); };
  });
  paint();
  return paint;
}

function renderKindChips() {
  const o = S.opts;
  let html = KINDS.map((k) =>
    `<button type="button" class="chip" data-k="${k.id}" style="--c:${k.color}" aria-pressed="${!o.all && o.kinds.includes(k.id)}"` +
    ` title="${k.exts.join(", ")}"><span class="sw"></span>${k.label}</button>`).join("");
  html += `<button type="button" class="chip all" data-k="*" aria-pressed="${o.all}" title="下载所有不是网页的内容">全部</button>`;
  $("#kindChips").innerHTML = html;
}

const FIELDS = [
  ["#oInclude", "include"], ["#oExclude", "exclude"], ["#oExtra", "extra"], ["#oMaxPages", "max_pages", Number],
  ["#oDelay", "delay", Number], ["#oMaxSize", "max_size"], ["#oProxy", "proxy"],
  ["#oClick", "js_click", null, "checked"], ["#oProbe", "probe_size", null, "checked"], ["#oInsecure", "insecure", null, "checked"],
];
function paintFields() {
  for (const [sel, key, , prop = "value"] of FIELDS) $(sel)[prop] = S.opts[key];
}
function paintMoreCount() {
  const o = S.opts, d = DEFAULT_OPTS;
  let n = ["include", "exclude", "scope", "extra", "max_pages", "delay", "max_size", "proxy", "js_click", "probe_size", "insecure"]
    .filter((k) => String(o[k]) !== String(d[k])).length;
  if ($("#oCookie").value.trim()) n++;
  if ($("#oHeaders").value.trim()) n++;
  $("#moreCount").hidden = !n;
  $("#moreCount").textContent = n;
}

let paintSegs = [];
function initOptions() {
  paintSegs = [
    bindSeg($("#segDepth"), "depth", depthOf),
    bindSeg($("#segJs"), "js"),
    bindSeg($("#segScope"), "scope"),
  ];
  renderKindChips();
  paintFields();
  for (const [sel, key, cast, prop = "value"] of FIELDS) {
    $(sel).addEventListener(prop === "checked" ? "change" : "input", (e) => {
      const v = e.target[prop];
      S.opts[key] = cast ? cast(v) : v;
      saveOpts();
      paintMoreCount();
    });
  }
  ["#oCookie", "#oHeaders"].forEach((s) => $(s).addEventListener("input", paintMoreCount));
  $("#kindChips").onclick = (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    const o = S.opts;
    if (b.dataset.k === "*") o.all = !o.all;
    else {
      if (o.all) { o.all = false; o.kinds = []; }
      o.kinds = o.kinds.includes(b.dataset.k) ? o.kinds.filter((k) => k !== b.dataset.k) : [...o.kinds, b.dataset.k];
    }
    saveOpts();
    renderKindChips();
  };
  $("#moreBtn").onclick = () => {
    const p = $("#more");
    p.hidden = !p.hidden;
    $("#moreBtn").setAttribute("aria-expanded", String(!p.hidden));
  };
  $("#resetOpts").onclick = () => {
    S.opts = { ...DEFAULT_OPTS };
    saveOpts();
    $("#oCookie").value = $("#oHeaders").value = "";
    paintSegs.forEach((p) => p());
    renderKindChips();
    paintFields();
    paintMoreCount();
  };
  $("#cookieFileBtn").onclick = () => $("#cookieFile").click();
  $("#cookieFile").onchange = async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    $("#oCookie").value = await file.text();
    paintMoreCount();
    toast(`已导入 ${file.name}`, "ok");
    e.target.value = "";
  };
  paintMoreCount();
}

// ───────────────────────── 最近 / 配置 ─────────────────────────

async function loadRecent() {
  let jobs = [];
  try { jobs = await api("/api/jobs"); } catch { /* 服务没起来 */ }
  const urls = (store("sc.urls") || []).filter((u) => !jobs.some((j) => decode(j.url) === u || j.url === u));
  const box = $("#recent");
  const cur = S.job?.id;
  const items = [
    ...jobs.filter((j) => j.id !== cur).slice(0, 6).map((j) =>
      `<button class="rc${j.status === "running" ? " live" : ""}" data-job="${j.id}" title="${esc(decode(j.url))}">` +
      `<span>${esc(j.title || shortUrl(j.url))}</span><b>${j.files}</b></button>`),
    ...urls.slice(0, 6).map((u) => `<button class="rc" data-url="${esc(u)}" title="${esc(u)}"><span>${esc(shortUrl(u))}</span></button>`),
  ];
  box.hidden = !items.length;
  box.innerHTML = items.length ? `<span class="opt-l">最近</span>${items.join("")}` : "";
}

async function loadConfig() {
  try {
    S.cfg = await api("/api/config");
  } catch (e) {
    toast("连不上本机服务：请确认 python web_app.py 还在运行", "bad");
    return;
  }
  const p = $("#outPath");
  p.textContent = "‎" + S.cfg.out.replace(/^\/Users\/[^/]+/, "~") + "‎"; // 从左边截断，LRM 防止 “/” 被 rtl 挪位
  p.title = `${S.cfg.out}\n点击更改`;
  if (!S.cfg.js) {
    $$("#segJs button").forEach((b) => { if (b.dataset.v !== "off") { b.disabled = true; b.title = S.cfg.js_msg; } });
    S.opts.js = "off";
    paintSegs[1]?.();
  }
}

// ───────────────────────── 浏览器登录 ─────────────────────────

const L = { info: null, timer: null, deb: null, resume: null }; // resume：重新登录完要接着下载的任务 id

async function refreshLogin() {
  const url = $("#url").value.trim();
  try {
    L.info = await api(`/api/login${url ? `?url=${encodeURIComponent(url)}` : ""}`);
  } catch { return; }
  renderLogin();
  clearTimeout(L.timer);
  if (["starting", "open"].includes(L.info.session?.status)) L.timer = setTimeout(refreshLogin, 1000);
}

function renderLogin() {
  const i = L.info || {};
  const s = i.session;
  let cls = "login-bar", html;
  if (s && ["starting", "open"].includes(s.status)) {
    cls += " live";
    html = `<div class="lb-txt">
      <b><span class="lb-dot"></span>${s.status === "starting" ? "正在打开浏览器…" : "浏览器已弹出，请在那个窗口里登录"}</b>
      <span class="muted">登录后可以一路点进课程资料页，再回来点「我已登录」。当前页面</span>
      <span class="lb-url" title="${esc(decode(s.url))}">${esc(shortUrl(s.url))}</span></div>
      <div class="lb-act"><button class="btn sm" data-lg="cancel">取消</button>
      <button class="btn primary sm" data-lg="finish">${L.resume && L.resume === S.job?.id ? "我已登录，继续下载" : "我已登录，扫描这一页"}</button></div>`;
  } else if (s?.status === "error") {
    cls += " err";
    html = `<div class="lb-txt">登录窗口出错：${esc(s.error)}</div><div class="lb-act"><button class="link" data-lg="start">重试</button></div>`;
  } else if (i.saved) {
    const moved = s?.status === "saved" && s.url && decode(s.url) !== $("#url").value.trim();
    cls += " ok";
    html = `${icon("check")}<div class="lb-txt"><span>已保存 <b>${esc(i.host)}</b> 的登录状态</span>
      <span class="muted">${ago(i.saved.at)}前 · ${i.saved.cookies} 个 cookie${i.saved.tokens?.length ? ` · 令牌 ${esc(i.saved.tokens.join("、"))}` : ""} · 扫描和下载自动带上</span></div>
      <div class="lb-act">${moved ? '<button class="link" data-lg="scanhere">扫描登录后停留的页面</button>' : ""}
      <button class="link" data-lg="start">重新登录</button><button class="link" data-lg="logout">退出登录</button></div>`;
  } else {
    html = `<span class="muted">资料要登录才能看到？</span><button class="link" data-lg="start">在浏览器里登录</button>
    <span class="muted hide-s">弹出一个真浏览器，统一身份认证、扫码都行；登录状态只存在本机</span>`;
  }
  // 轮询时内容没变就不动 DOM：每秒重建一次按钮的话，点击可能落空
  const box = $("#loginBar");
  if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; }
  box.className = cls;
  if (S.job) renderAuthStrip();
}

async function loginAction(act, btn) {
  const url = $("#url").value.trim();
  try {
    if (act === "start") {
      if (!url) { toast("先在上面输入课程平台的网址", "bad"); $("#url").focus(); return; }
      L.info = await api("/api/login", { url, proxy: S.opts.proxy, insecure: S.opts.insecure });
      toast("已弹出浏览器窗口（可能在别的窗口后面），请在里面登录");
    } else if (act === "finish") {
      btn.disabled = true;
      btn.textContent = "正在保存…";
      L.info = await api("/api/login/finish", {});
      toast(`登录状态已保存（${L.info.session.cookies} 个 cookie）`, "ok");
      if (L.resume && L.resume === S.job?.id) {
        $("#url").value = decode(S.job.url);
        resumeAfterLogin();
      } else {
        L.resume = null;
        const to = decode(L.info.session.url);
        $("#url").value = to;
        startScan(to);
      }
    } else if (act === "cancel") {
      L.resume = null;
      L.info = await api("/api/login/cancel", {});
    } else if (act === "logout") {
      L.info = await api("/api/logout", { url });
      toast("已删除保存的登录状态", "ok");
    } else if (act === "scanhere") {
      const to = decode(L.info.session.url);
      $("#url").value = to;
      startScan(to);
    }
  } catch (e) {
    toast(e.message, "bad");
  }
  refreshLogin();
}

// ───────────────────────── 事件绑定 ─────────────────────────

function bind() {
  $("#scanForm").onsubmit = (e) => { e.preventDefault(); startScan(); };
  $("#url").addEventListener("input", () => { clearTimeout(L.deb); L.deb = setTimeout(refreshLogin, 400); });
  $("#loginBar").onclick = (e) => {
    const b = e.target.closest("[data-lg]");
    if (b) loginAction(b.dataset.lg, b);
  };
  $("#stopScan").onclick = () => api(`/api/jobs/${S.job.id}/stop`, { what: "scan" }).catch((e) => toast(e.message, "bad"));
  $("#authLogin").onclick = (e) => (reloginOpen() ? loginAction("finish", e.currentTarget) : relogin());
  $("#authGo").onclick = () => (reloginOpen() ? loginAction("cancel") : resumeAfterLogin());
  $("#stopDl").onclick = () => api(`/api/jobs/${S.job.id}/stop`, { what: "download" }).catch((e) => toast(e.message, "bad"));
  $("#rescan").onclick = () => startScan(decode(S.job.url));

  $("#q").oninput = (e) => { S.q = e.target.value; schedule(); };
  $("#groupBy").value = S.group;
  $("#groupBy").onchange = (e) => { S.group = e.target.value; store("sc.group", S.group); schedule(); };
  $("#sortBy").onchange = (e) => { S.sort = e.target.value; schedule(); };
  $("#kindFilter").onclick = (e) => {
    const b = e.target.closest(".chip");
    if (b) { S.kind = b.dataset.kind; schedule(); }
  };
  $("#selAll").onchange = (e) => { shown.forEach((id) => toggle(id, e.target.checked)); schedule(); };

  $("#list").addEventListener("click", (e) => {
    const gh = e.target.closest(".group-h");
    if (gh) {
      if (e.target.matches("input")) {
        const key = gh.dataset.group;
        const g = groups(visible()).find((x) => x.key === key);
        g?.files.forEach((f) => toggle(f.fid, e.target.checked));
        schedule();
      }
      return;
    }
    const row = e.target.closest(".row");
    if (!row) return;
    const f = S.files.get(row.dataset.fid);
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "get") return frameDownload(`/api/jobs/${S.job.id}/files/${f.fid}`);
    if (act === "view") return window.open(`/api/jobs/${S.job.id}/files/${f.fid}?inline=1`, "_blank", "noopener");
    if (act === "copy") return copyText(f.url, "链接");
    if (act === "reveal") return api("/api/reveal", { jid: S.job.id, fid: f.fid }).catch((er) => toast(er.message, "bad"));
    if (e.target.closest("a")) return;
    // 点行 / 复选框 = 选中；按住 Shift 连选
    const on = e.target.matches("input") ? e.target.checked : !S.sel.has(f.fid);
    if (e.shiftKey && S.lastClick && shown.includes(S.lastClick)) {
      const [a, b] = [shown.indexOf(S.lastClick), shown.indexOf(f.fid)].sort((x, y) => x - y);
      shown.slice(a, b + 1).forEach((id) => toggle(id, on));
    } else toggle(f.fid, on);
    S.lastClick = f.fid;
    schedule();
  });
  $("#list").addEventListener("mousedown", (e) => { if (e.shiftKey) e.preventDefault(); }); // 连选时别选中文字

  $("#empty").onclick = (e) => {
    const a = e.target.closest("[data-empty]")?.dataset.empty;
    if (a === "clear") { S.q = ""; S.kind = "all"; $("#q").value = ""; schedule(); }
    if (a === "deeper") { S.opts.depth = nextDepth(S.opts.depth) ?? S.opts.depth; saveOpts(); paintSegs[0](); startScan(decode(S.job.url)); }
    if (a === "js") { S.opts.js = "on"; S.opts.js_click = true; saveOpts(); paintSegs[1](); paintFields(); startScan(decode(S.job.url)); }
  };

  $("#selClear").onclick = () => { S.sel.clear(); schedule(); };
  $("#actDl").onclick = () => downloadSelected();
  $("#actZip").onclick = zipSelected;
  $("#actCopy").onclick = () => copyText(selected().map((id) => S.files.get(id).url).join("\n"), ` ${S.sel.size} 个链接`);
  $("#actCsv").onclick = exportCsv;

  $("#recent").onclick = (e) => {
    const b = e.target.closest(".rc");
    if (!b) return;
    if (b.dataset.job) location.hash = `#/job/${b.dataset.job}`;
    else { $("#url").value = b.dataset.url; $("#url").focus(); }
  };

  $("#outOpen").onclick = () => api("/api/reveal", {}).catch((e) => toast(e.message, "bad"));
  $("#outPath").onclick = () => { $("#outInput").value = S.cfg.out; $("#outDlg").showModal(); $("#outInput").select(); };
  $("#outDlg").addEventListener("close", async () => {
    if ($("#outDlg").returnValue !== "ok") return;
    try {
      S.cfg = await api("/api/config", { out: $("#outInput").value });
      await loadConfig();
      toast(`保存位置已改为 ${S.cfg.out}`, "ok");
    } catch (e) { toast(e.message, "bad"); }
  });

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); $("#url").focus(); $("#url").select(); }
    if (e.key === "Escape" && !typing && S.sel.size) { S.sel.clear(); schedule(); }
  });
  window.addEventListener("hashchange", route);
}

function route() {
  const m = location.hash.match(/^#\/job\/([0-9a-f]+)/);
  if (m) { if (S.job?.id !== m[1]) openJob(m[1]).then(loadRecent); }
  else if (S.job) showIdle();
}

initOptions();
bind();
loadConfig().then(() => { route(); loadRecent(); refreshLogin(); });
if (!location.hash) $("#url").focus();
