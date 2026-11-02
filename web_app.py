#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_app.py —— study_crawler 的网页界面（在本机运行）

  python web_app.py                                  # 打开 http://127.0.0.1:9940
  python web_app.py --out ~/Downloads/学习资料 --port 8000 --no-browser

流程：（要登录的平台先点「在浏览器里登录」，在弹出的真浏览器里自己登录）→
输入网址 → 扫描（只列出能下载的文件，不下载；动态网页用无头浏览器渲染，顺带嗅探页面调用的接口）→ 勾选 →
  * 「下载到本机」：后台下载到输出目录，断点续传 / 重试 / 去重 / 限速 / robots.txt 都沿用 study_crawler
  * 每个文件的「↓」：经本机转发给浏览器直接保存，自动带上 Cookie 和 Referer（绕开防盗链）
  * 「打包 ZIP」：把已下载的文件边打包边传给浏览器

只监听 127.0.0.1，并校验 Host 头，防止别的网站借你的浏览器调用这个服务。
依赖：pip install flask requests beautifulsoup4 lxml playwright && python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import study_crawler as sc  # noqa: E402
from js_render import LoginSession, Renderer  # noqa: E402
from js_render import check as js_check  # noqa: E402

HERE = Path(__file__).resolve().parent
STATIC = HERE / "web"
log = sc.log

app = Flask(__name__, static_folder=None)
CFG = {"out": Path.home() / "Downloads" / "StudyCrawler"}
JOBS: dict[str, "Job"] = {}
JOBS_LOCK = threading.Lock()
DL_LOCK = threading.Lock()  # 下载一次只跑一批：共用输出目录里的断点状态库和 .parts
RENDERER = Renderer()
LOGIN = {"session": None}  # 同一时间只开一个登录窗口
MAX_JOBS = 30
# 浏览器里直接打开（预览）安全的类型；其余一律当附件下载，避免别人的网页以本机服务的身份运行脚本
INLINE_OK = re.compile(r"^(application/pdf|image/(png|jpe?g|gif|webp|bmp)|text/plain|video/.*|audio/.*)$")


# ───────────────────────── 任务 ─────────────────────────

class Job:
    """一次扫描，以及之后从扫描结果里发起的下载。所有变化追加到 events，前端用 SSE 按序回放。"""

    def __init__(self, params, a, seeds, cookie_file=None):
        self.id = uuid.uuid4().hex[:12]
        self.params, self.a, self.seeds = params, a, seeds
        self.cookie_file = cookie_file
        self.url = seeds[0]
        self.created = time.time()
        self.status = "running"  # running | done | stopped | error
        self.title = ""
        self.events, self.cond = [], threading.Condition()
        self.files, self.by_url = {}, {}
        self.pages = Counter()
        self.scanner = None
        self.downloader = None
        self.dl_gen = 0  # 每次“停止下载”加一，排队中的批次据此作废
        self.auth_paused = ""  # 下载时发现登录失效：之后排队的批次不再跑，等用户重新登录后继续
        self.apis = set()

    def emit(self, type_, **kw):
        with self.cond:
            kw.update(type=type_, seq=len(self.events), t=round(time.time(), 3))
            self.events.append(kw)
            self.cond.notify_all()

    def summary(self):
        done = sum(1 for f in list(self.files.values()) if f["dl"].get("status") == "done")
        return {"id": self.id, "url": self.url, "title": self.title, "status": self.status,
                "created": self.created, "files": len(self.files), "downloaded": done,
                "pages": self.pages["ok"], "js": self.a.js}

    # 扫描 / 下载线程回调

    def on_result(self, res, out, scanning):
        t = res.task
        if scanning and res.status == "found":
            self._add_file(t, res)
        elif t.kind == "page" and not scanning:
            pass
        elif t.kind == "page":
            if res.status in ("done", "failed", "skipped"):
                ok = res.status == "done"
                self.pages["ok" if ok else "bad"] += 1
                if t.depth == 0 and res.title and not self.title:
                    self.title = res.title
                self.emit("page", url=t.url, title=res.title, depth=t.depth, ok=ok, note=res.note,
                          links=len(res.links))
        elif not scanning:
            if res.status in ("done", "dup") and res.path:
                note = "本地已有同名同内容文件" if res.note == "已存在" else res.note
                self.file_done(t.url, res.path, res.size, out, note)
            elif res.status == "interrupted":
                self.set_dl(t.url, status="stopped")
            elif res.status == "auth":
                self.set_dl(t.url, status="auth", note=res.note)
            elif res.status in ("failed", "skipped"):
                self.set_dl(t.url, status=res.status, note=res.note)

    def _add_file(self, t, res):
        if t.url in self.by_url:
            return
        name = res.path or "file"
        fid = sc.sha1(t.url)[:12]
        f = {"fid": fid, "url": t.url, "name": name, "ext": sc.name_ext(name), "size": res.size or 0,
             # 网页任务最后发现是文件：下载时按“疑似下载链接”处理，万一又回网页也不会顺着爬下去
             "kind": "probe" if t.kind == "page" else t.kind,
             "referer": t.referer or "", "folder": t.folder, "anchor": t.anchor, "depth": t.depth,
             "note": res.note, "dl": {}}
        self.files[fid], self.by_url[t.url] = f, fid
        self.emit("file", file={k: v for k, v in f.items() if k != "dl"})

    def set_dl(self, url, **dl):
        fid = self.by_url.get(url)
        if fid:
            self.files[fid]["dl"] = dl
            self.emit("dl", fid=fid, **dl)

    def file_done(self, url, rel, size, out, note=""):
        full = (out / rel).resolve()
        self.set_dl(url, status="done", path=rel, abs=str(full), size=size, note=note)


class WebCrawler(sc.Crawler):
    def __init__(self, a, seeds, job, renderer=None, tasks=None):
        super().__init__(a, seeds, renderer=renderer)
        self.job, self.tasks = job, tasks
        self.thread_prefix = f"{job.id}-w"
        self.log_file = None if a.dry_run else "crawl.log"
        self.ticks = {}

    def _seed(self):
        if self.tasks is None:
            return super()._seed()
        todo = []
        for t in self.tasks:
            row = self.db.lookup(t.url)
            if row and row[0] in ("done", "dup") and row[1] and (self.out / row[1]).is_file():
                self.job.file_done(t.url, row[1], row[2] or 0, self.out, "之前下载过")
                continue
            self.db.requeue(t)
            todo.append(t)
        return todo

    def _on_result(self, res):
        super()._on_result(res)
        if res.status == "interrupted" and self.auth_lost:  # 因为登录失效被叫停的，跟着一起等重新登录
            res = dataclasses.replace(res, status="auth", note=self.auth_lost)
        self.job.on_result(res, self.out, self.a.dry_run)

    def _progress(self, t, done, total):
        now = time.monotonic()
        if now - self.ticks.get(t.url, 0.0) < 0.25:
            return
        self.ticks[t.url] = now
        self.job.set_dl(t.url, status="running", done=done, total=total)

    def _on_sniffed(self, host, names):
        super()._on_sniffed(host, names)
        self.job.emit("auth", host=host, headers=names)  # 只报名字，不把值发给前端

    def _on_apis(self, page, apis):
        super()._on_apis(page, apis)
        for a in apis:
            key = (a["method"], a["url"])
            if key not in self.job.apis:
                self.job.apis.add(key)
                self.job.emit("api", page=page, **a)

    def _report(self, elapsed):
        if not self.a.dry_run:
            super()._report(elapsed)
        s = self.stats
        self.job.emit("summary", phase="scan" if self.a.dry_run else "download", elapsed=round(elapsed, 1),
                      pages=s["pages"], files=s["files"] or len(self.found), bytes=s["bytes"],
                      existed=s["existed"], dup=s["dup"], failed=s["failed"],
                      capped=s["capped"], max_pages=self.a.max_pages)


class JobLogRouter(logging.Handler):
    """线程名以任务 id 开头的日志，转成那个任务的 log 事件。"""

    def emit(self, record):
        job = JOBS.get(record.threadName.split("-", 1)[0])
        if job is not None:
            try:
                job.emit("log", level=record.levelname.lower(), msg=record.getMessage())
            except Exception:
                self.handleError(record)


# ───────────────────────── 参数 ─────────────────────────

def keywords_regex(s):
    """“课件 习题,实验” → 课件|习题|实验（按普通文字匹配，不用懂正则）。"""
    words = [w for w in re.split(r"[\s,，;；|]+", s or "") if w]
    return "|".join(map(re.escape, words))


def parse_url(url):
    url = str(url or "").strip()
    if not url:
        raise ValueError("请输入网址")
    full = url if "://" in url else "https://" + url
    host = urlsplit(full).hostname or ""
    if not ("." in host or host in ("localhost",) or ":" in host):
        raise ValueError(f"“{url}”看起来不是网址，例如：https://example.edu/course/")
    seed = sc.normalize_url(full, "https://invalid/")
    if not seed:
        raise ValueError(f"无效的网址：{url}")
    return seed


def build_args(params, *, download=False, cookie_file=None):
    """把网页表单翻译成命令行参数，交给 study_crawler 自己的 argparse 校验。"""
    full = parse_url(params.get("url"))
    argv = [full, "-o", str(CFG["out"]),
            "--delay", str(float(params.get("delay", 1.0))),
            "--timeout", str(float(params.get("timeout", 30)))]
    if params.get("scope") == "prefix":
        argv += ["--scope", "prefix"]
    if params.get("cookie", "").strip():
        argv += ["--cookie", params["cookie"].strip()]
    if cookie_file:
        argv += ["--cookies", cookie_file]
    for line in str(params.get("headers", "")).splitlines():
        if line.strip():
            argv += ["-H", line.strip()]
    if params.get("insecure"):
        argv.append("--insecure")
    if params.get("proxy", "").strip():
        argv += ["--proxy", params["proxy"].strip()]
    if str(params.get("max_size", "")).strip():
        argv += ["--max-size", str(params["max_size"]).strip()]
    if download:  # 用户亲手勾的，不再按类型 / 关键词过滤
        argv += ["--ext", "*", "--layout", params.get("layout", "page")]
    else:
        js = params.get("js", "auto") if js_check() is None else "off"
        argv += ["--js", js] + (["--js-click"] if params.get("js_click") and js != "off" else [])
        depth = params.get("depth", 1)
        whole = str(depth).lower() == "all"  # 全站：不限层数，并读 sitemap
        argv += ["--dry-run", "-d", "all" if whole else str(int(depth)),
                 "--max-pages", str(int(params.get("max_pages", 300))),
                 "--ext", str(params.get("ext") or "*")] + (["--sitemap"] if whole else [])
        for key in ("include", "exclude"):
            rx = keywords_regex(params.get(key, ""))
            if rx:
                argv += [f"--{key}", rx]
        if params.get("probe_size"):
            argv.append("--probe-size")

    parser = sc.build_parser()

    def fail(msg):
        raise ValueError(msg)

    parser.error = fail
    a = parser.parse_args(argv)
    return a, [full]


def cookie_file_for(params):
    """粘贴的是 Netscape 格式 cookies.txt 就落到临时文件；否则当 k=v; k2=v2 字符串处理。"""
    text = str(params.get("cookies_txt", "")).strip()
    if not text:
        return None
    fd, path = tempfile.mkstemp(prefix="sc-cookies-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text if text.startswith("# Netscape") else "# Netscape HTTP Cookie File\n" + text)
        f.write("\n")
    return path


# ───────────────────────── 后台线程 ─────────────────────────

def run_scan(job):
    c = WebCrawler(job.a, job.seeds, job, renderer=RENDERER if job.a.js != "off" else None)
    job.scanner = c
    try:
        c.run()
        job.status = "stopped" if c.stop.is_set() else "done"
    except BaseException as e:  # noqa: BLE001  任何异常都要让前端知道
        job.status = "error"
        job.emit("log", level="error", msg=f"扫描出错：{type(e).__name__}: {e}")
        log.debug("扫描出错", exc_info=True)
    job.emit("status", phase="scan", status=job.status)


def run_download(job, fids, gen, after_login=False):
    with DL_LOCK:
        if gen != job.dl_gen:
            for fid in fids:
                job.set_dl(job.files[fid]["url"], status="stopped")
            return
        if job.auth_paused and not after_login:  # 前一批刚发现登录失效，这批不用再撞一次
            for fid in fids:
                job.set_dl(job.files[fid]["url"], status="auth", note=job.auth_paused)
            job.emit("authlost", msg=job.auth_paused, count=len(fids))
            job.emit("dlrun", status="idle", out=str(CFG["out"]))
            return
        job.auth_paused = ""
        a, _ = build_args(job.params, download=True, cookie_file=job.cookie_file)
        tasks = []
        for fid in fids:
            f = job.files[fid]
            tasks.append(sc.Task(f["url"], f["kind"], 1, f["referer"] or None, f["folder"], f["anchor"]))
        c = WebCrawler(a, job.seeds, job, tasks=tasks)
        c.pause_on_auth = not after_login  # 刚重新登录过还失败：多半是没权限，记失败，别再让人登录一遍
        job.downloader = c
        job.emit("dlrun", status="running", count=len(tasks), out=str(c.out))
        try:
            if after_login:
                refresh_tokens(job, c, tasks)
            elif job.scanner is not None:  # 扫描时嗅探到的 Authorization 等请求头，下载接口多半也要
                for host, hs in job.scanner.sniffed.items():
                    c.sniffed.setdefault(host, {}).update(hs)
            c.run()
        except BaseException as e:  # noqa: BLE001
            job.emit("log", level="error", msg=f"下载出错：{type(e).__name__}: {e}")
            log.debug("下载出错", exc_info=True)
        finally:
            job.downloader = None
            if c.auth_lost:
                job.auth_paused = c.auth_lost
            for fid in fids:  # 没有结果的（被停止 / 出错 / 登录失效被叫停）统一标记
                if job.files[fid]["dl"].get("status") in ("queued", "running"):
                    job.set_dl(job.files[fid]["url"], **({"status": "auth", "note": c.auth_lost} if c.auth_lost
                                                         else {"status": "stopped"}))
            if c.auth_lost:
                n = sum(1 for fid in fids if job.files[fid]["dl"].get("status") == "auth")
                job.emit("authlost", msg=c.auth_lost, count=n)
            job.emit("dlrun", status="idle", out=str(c.out))


def refresh_tokens(job, c, tasks):
    """重新登录后，旧的 Authorization 令牌已作废：用新登录状态把来源页再渲染一遍，重新嗅探。"""
    if job.scanner is not None:
        job.scanner.refresh_login()  # 单个文件「↓」转发也换上新登录状态
    if js_check() is not None or not c.login_state:
        return
    page = next((t.referer for t in tasks if t.referer), None) or job.url
    job.emit("log", level="info", msg=f"重新登录后刷新接口令牌：渲染 {page}")
    c.renderer = RENDERER
    try:
        c._render_page(page)
    finally:
        c.renderer = None
    if job.scanner is not None:
        for host, hs in c.sniffed.items():
            job.scanner.sniffed.setdefault(host, {}).update(hs)


def evict_old_jobs():
    with JOBS_LOCK:
        if len(JOBS) <= MAX_JOBS:
            return
        for job in sorted(JOBS.values(), key=lambda j: j.created)[: len(JOBS) - MAX_JOBS]:
            if job.status != "running" and job.downloader is None:
                JOBS.pop(job.id, None)
                if job.cookie_file:
                    Path(job.cookie_file).unlink(missing_ok=True)


# ───────────────────────── 安全 / 工具 ─────────────────────────

@app.before_request
def guard():
    host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        abort(403)  # 防 DNS rebinding：别的域名解析到 127.0.0.1 也不行
    if request.method == "POST" and not request.is_json:
        abort(415)  # 跨站表单 POST 发不了 application/json（会先触发 CORS 预检被拦）


def get_job(jid):
    job = JOBS.get(jid)
    if job is None:
        abort(404, "任务不存在（服务重启过？请重新扫描）")
    return job


def disposition(name, inline=False):
    fallback = re.sub(r'[^\x20-\x7e]|["\\]', "_", name) or "file"
    return f"{'inline' if inline else 'attachment'}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def error_page(status, msg):
    """单个文件下载走隐藏 iframe：出错时返回这个小网页，前端读出文字弹提示。"""
    body = (f"<!doctype html><meta charset=utf-8><title>下载失败</title>"
            f"<body data-sc-error style='font:15px/1.6 system-ui;padding:2em;max-width:40em'>"
            f"<h3>下载失败</h3><p id=msg>{msg}</p></body>")
    return Response(body, status=status, mimetype="text/html")


@app.errorhandler(400)
@app.errorhandler(404)
def json_error(e):
    if request.path.startswith("/api/"):
        return jsonify(error=getattr(e, "description", str(e))), e.code
    return e


# ───────────────────────── 页面 / 配置 ─────────────────────────

@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html", max_age=0)


@app.get("/static/<path:name>")
def static_files(name):
    return send_from_directory(STATIC, name, max_age=0)


@app.get("/api/config")
def get_config():
    return {"out": str(CFG["out"]), "js": js_check() is None, "js_msg": js_check() or "",
            "engine": RENDERER.engine, "version": sc.VERSION, "default_exts": sorted(sc.DEFAULT_EXTS)}


@app.post("/api/config")
def set_config():
    out = str((request.get_json() or {}).get("out", "")).strip()
    if not out:
        abort(400, "请填写保存位置")
    p = Path(out).expanduser().resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        abort(400, f"无法创建目录：{e}")
    CFG["out"] = p
    return get_config()


# ───────────────────────── 扫描 ─────────────────────────

@app.post("/api/scan")
def start_scan():
    params = request.get_json() or {}
    cookie_file = cookie_file_for(params)
    try:
        a, seeds = build_args(params, cookie_file=cookie_file)
    except ValueError as e:
        if cookie_file:
            Path(cookie_file).unlink(missing_ok=True)
        abort(400, str(e))
    job = Job(params, a, seeds, cookie_file)
    with JOBS_LOCK:
        JOBS[job.id] = job
    job.emit("meta", url=job.url, params={k: v for k, v in params.items() if k not in ("cookie", "cookies_txt", "headers")},
             login=sc.login_state_path(job.url).is_file(),
             js=a.js)
    threading.Thread(target=run_scan, args=(job,), name=f"{job.id}-scan", daemon=True).start()
    evict_old_jobs()
    return {"id": job.id}


@app.get("/api/jobs")
def list_jobs():
    return jsonify([j.summary() for j in sorted(JOBS.values(), key=lambda j: j.created, reverse=True)])


@app.get("/api/jobs/<jid>")
def job_info(jid):
    return get_job(jid).summary()


@app.get("/api/jobs/<jid>/events")
def job_events(jid):
    job = get_job(jid)
    since = request.headers.get("Last-Event-ID") or request.args.get("since") or "0"
    i = int(since) if since.isdigit() else 0

    def stream():
        nonlocal i
        yield "retry: 2000\n\n"
        while True:
            with job.cond:
                if i >= len(job.events):
                    job.cond.wait(timeout=15)
                batch = job.events[i:]
            if not batch:
                yield ": ping\n\n"
                continue
            out = []
            for ev in batch:
                out.append(f"id: {ev['seq'] + 1}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n")
            i += len(batch)
            yield "".join(out)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/jobs/<jid>/stop")
def stop_job(jid):
    job = get_job(jid)
    what = (request.get_json() or {}).get("what", "all")
    if what in ("scan", "all") and job.scanner is not None:
        job.scanner.stop.set()
    if what in ("download", "all"):
        job.dl_gen += 1
        if job.downloader is not None:
            job.downloader.stop.set()
    return {"ok": True}


# ───────────────────────── 下载 ─────────────────────────

@app.post("/api/jobs/<jid>/download")
def start_download(jid):
    job = get_job(jid)
    data = request.get_json() or {}
    fids = [fid for fid in data.get("fids", []) if fid in job.files]
    after_login = bool(data.get("after_login"))
    picked = []
    for fid in fids:
        f = job.files[fid]
        st = f["dl"].get("status")
        if st in ("queued", "running"):
            continue
        if st == "done" and f["dl"].get("abs") and os.path.isfile(f["dl"]["abs"]):
            continue
        job.set_dl(f["url"], status="queued")
        picked.append(fid)
    if picked:
        threading.Thread(target=run_download, args=(job, picked, job.dl_gen, after_login),
                         name=f"{job.id}-dl{uuid.uuid4().hex[:4]}", daemon=True).start()
    return {"queued": len(picked), "out": str(CFG["out"])}


@app.get("/api/jobs/<jid>/files/<fid>")
def get_file(jid, fid):
    """已下载的从磁盘发；没下载的经本机转发（带 Cookie / Referer），不落盘。?inline=1 预览。"""
    job = get_job(jid)
    f = job.files.get(fid) or abort(404)
    inline = request.args.get("inline") == "1"
    p = f["dl"].get("abs")
    if p and os.path.isfile(p):
        mime = mimetypes.guess_type(p)[0] or "application/octet-stream"
        inline = inline and bool(INLINE_OK.match(mime))
        resp = send_file(p, mimetype=mime, conditional=True)
        resp.headers["Content-Disposition"] = disposition(Path(p).name, inline)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
    return proxy_file(job, f, inline)


def proxy_file(job, f, inline):
    c = job.scanner
    if c is None:
        return error_page(404, "扫描任务还没准备好")
    t = sc.Task(f["url"], f["kind"], 1, f["referer"] or None, f["folder"], f["anchor"])
    if not c.robots.allowed(t.url):
        return error_page(403, "robots.txt 禁止抓取这个文件")
    c.refresh_login()  # 期间在界面里重新登录过的话，用新的
    s = c._make_session()  # 每个请求一个会话，不跟爬虫线程抢
    c.throttle.wait(urlsplit(t.url).netloc, threading.Event())
    try:
        r = s.get(t.url, headers=c._headers(t), stream=True, timeout=(10, c.a.timeout))
    except requests.RequestException as e:
        return error_page(502, f"连接对方服务器失败：{sc.short(e, 120)}")
    try:
        if r.status_code >= 400:
            with r:
                denied = c._check_denied(r) if r.status_code in (401, 403) else ""
            return error_page(502, f"对方服务器返回 HTTP {r.status_code}{denied}")
        ctype = r.headers.get("Content-Type", "")
        cd_name = sc.filename_from_cd(r.headers.get("Content-Disposition"))
        if not cd_name and sc.mime_of(ctype) in ("text/html", "application/xhtml+xml"):
            with r:
                c._handle_page(sc.Task(t.url, "file", 1), r, ctype)  # 登录页会抛 AuthLost
            return error_page(502, "对方返回的是网页而不是文件，链接可能失效了，或者需要登录")
        name = c._pick_name(t, r, ctype, cd_name)
        chunks = r.iter_content(1 << 16)
        head = sc.read_head(chunks)  # 先看开头：是错误提示就别让浏览器存成文件
        cl = r.headers.get("Content-Length", "")
        problem = c._check_content(t, r, name, cd_name, head, int(cl) if cl.isdigit() else None)
    except sc.AuthLost as e:
        r.close()
        if not c._has_auth():
            return error_page(502, f"需要登录（{e}）：点上方「在浏览器里登录」，登录后再试")
        return error_page(502, f"登录已失效（{e}）：点上方「重新登录」，在弹出的浏览器里登录后再试")
    except requests.RequestException as e:
        r.close()
        return error_page(502, f"读取对方服务器失败：{sc.short(e, 120)}")
    if problem:
        r.close()
        return error_page(502, problem)
    mime = sc.mime_of(ctype) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if mime == "application/octet-stream":
        mime = mimetypes.guess_type(name)[0] or mime
    inline = inline and bool(INLINE_OK.match(mime))
    headers = {"Content-Disposition": disposition(name, inline), "Cache-Control": "no-store",
               "X-Content-Type-Options": "nosniff"}
    cl = r.headers.get("Content-Length", "")
    if cl.isdigit() and r.headers.get("Content-Encoding", "identity").lower() in ("", "identity"):
        headers["Content-Length"] = cl

    def gen():
        with r:
            yield head
            yield from chunks

    return Response(gen(), mimetype=mime, headers=headers, direct_passthrough=True)


class _ZipSink(io.RawIOBase):
    """zipfile 写到这里，生成器边写边取走，ZIP 不用先落盘。"""

    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)

    def take(self):
        out = bytes(self.buf)
        self.buf.clear()
        return out


@app.get("/api/jobs/<jid>/zip")
def zip_files(jid):
    job = get_job(jid)
    want = set(filter(None, request.args.get("fids", "").split(",")))
    items, seen = [], set()
    for fid, f in list(job.files.items()):
        p = f["dl"].get("abs")
        if (not want or fid in want) and p and os.path.isfile(p) and p not in seen:
            seen.add(p)
            items.append((p, f["dl"].get("path") or Path(p).name))
    if not items:
        return error_page(404, "所选文件还没有下载到本机，先点「下载到本机」")

    def gen():
        sink = _ZipSink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as zf:
            for path, arc in items:
                zi = zipfile.ZipInfo.from_file(path, arc)
                zi.compress_type = zipfile.ZIP_DEFLATED
                with open(path, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        dst.write(block)
                        if len(sink.buf) >= 1 << 20:
                            yield sink.take()
                yield sink.take()
        yield sink.take()

    host = urlsplit(job.url).hostname or "files"
    name = f"{job.title[:30] or host}_{datetime.now():%Y%m%d-%H%M}.zip"
    return Response(gen(), mimetype="application/zip",
                    headers={"Content-Disposition": disposition(sc.safe_name(name)), "Cache-Control": "no-store"})


# ───────────────────────── 浏览器登录 ─────────────────────────

def login_info(url=None):
    s = LOGIN["session"]
    out = {"session": s.info() | {"start": s.url} if s else None, "host": "", "saved": None}
    if url:
        try:
            seed = parse_url(url)
        except ValueError:
            return out
        path = sc.login_state_path(seed)
        out["host"] = urlsplit(seed).netloc
        if path.is_file():
            try:
                n = len(json.loads(path.read_text(encoding="utf-8")).get("cookies", []))
            except (OSError, ValueError):
                n = 0
            try:
                hosts = json.loads(sc.token_path(seed).read_text(encoding="utf-8")).get("hosts") or {}
            except (OSError, ValueError):
                hosts = {}
            tokens = sorted({k for hs in hosts.values() if isinstance(hs, dict) for k in hs})  # 只给名字，不给值
            out["saved"] = {"at": path.stat().st_mtime, "cookies": n, "path": str(path), "tokens": tokens}
    return out


@app.get("/api/login")
def login_status():
    return login_info(request.args.get("url"))


@app.post("/api/login")
def login_start():
    """弹出一个看得见的浏览器，让用户自己登录。"""
    data = request.get_json() or {}
    try:
        seed = parse_url(data.get("url"))
    except ValueError as e:
        abort(400, str(e))
    if js_check():
        abort(400, js_check())
    s = LOGIN["session"]
    if s and s.alive():
        s.cancel()  # 换个站点重新登录：先关掉旧窗口
    LOGIN["session"] = LoginSession(seed, sc.login_state_path(seed), proxy=(data.get("proxy") or "").strip() or None,
                                    insecure=bool(data.get("insecure"))).start()
    return login_info(seed)


@app.post("/api/login/finish")
def login_finish():
    s = LOGIN["session"] or abort(400, "没有打开的登录窗口")
    s.finish()
    if s.status != "saved":
        abort(400, f"登录状态没有保存：{s.error or s.status}")
    for u in (s.url, s.current_url):  # 旧令牌跟着旧登录作废，新的等渲染时重新嗅探
        sc.forget_login(u, tokens_only=True)
    if urlsplit(s.current_url).netloc != urlsplit(s.url).netloc:  # 登录后跳到了别的站点，那边也存一份
        other = sc.login_state_path(s.current_url)
        other.write_bytes(sc.login_state_path(s.url).read_bytes())
        other.chmod(0o600)
    return login_info(s.current_url)


@app.post("/api/login/cancel")
def login_cancel():
    s = LOGIN["session"]
    if s:
        s.cancel()
    return login_info(s.url if s else None)


@app.post("/api/logout")
def logout():
    try:
        seed = parse_url((request.get_json() or {}).get("url"))
    except ValueError as e:
        abort(400, str(e))
    sc.forget_login(seed)
    return login_info(seed)


@app.post("/api/reveal")
def reveal():
    """在访达 / 资源管理器里显示文件或输出目录。只接受任务里已下载的文件。"""
    data = request.get_json() or {}
    target = CFG["out"]
    if data.get("jid") and data.get("fid"):
        f = get_job(data["jid"]).files.get(data["fid"]) or abort(404)
        if f["dl"].get("abs") and os.path.exists(f["dl"]["abs"]):
            target = Path(f["dl"]["abs"])
    if target == CFG["out"]:
        target.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
    elif os.name == "nt":
        cmd = ["explorer", f"/select,{target}"] if target.is_file() else ["explorer", str(target)]
    else:
        cmd = ["xdg-open", str(target if target.is_dir() else target.parent)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "path": str(target)}


# ───────────────────────── 启动 ─────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(description="study_crawler 网页界面")
    p.add_argument("--port", type=int, default=9940)
    p.add_argument("--out", help=f"默认保存位置（默认 {CFG['out']}）")
    p.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    if a.out:
        CFG["out"] = Path(a.out).expanduser().resolve()

    log.setLevel(logging.DEBUG)
    router = JobLogRouter()
    router.setLevel(logging.INFO)
    log.addHandler(router)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if a.verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    logging.getLogger("werkzeug").setLevel(logging.INFO if a.verbose else logging.WARNING)

    url = f"http://127.0.0.1:{a.port}"
    print(f"StudyCrawler 网页界面：{url}  （保存到 {CFG['out']}，Ctrl+C 退出）", flush=True)
    if js_check():
        print("提示：" + js_check(), flush=True)
    if not a.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        app.run(host="127.0.0.1", port=a.port, threaded=True, use_reloader=False)
    finally:
        if LOGIN["session"]:
            LOGIN["session"].cancel(timeout=5)
        RENDERER.close()


if __name__ == "__main__":
    main()
