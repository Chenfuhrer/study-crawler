#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
js_render.py —— 用无头 Chromium（Playwright）打开动态网页，把结果交给 study_crawler 解析

收集的东西
  * JS 执行完之后的 DOM，含各个 iframe 里的链接、data-url / data-file 之类的属性
  * 加载过程中的网络响应：URL、方法、状态码、Content-Type、Content-Disposition，
    以及 JSON 接口的响应体（Vue / React 页面的文件列表通常就在这里）
  * 自动点“加载更多 / 下一页”（分页条里的“>”、图标按钮也认），每点一次收集一遍链接，
    按钮变灰、点了没变化、或者到了次数 / 时间上限就停；有真实地址的翻页链接留给爬虫当普通网页去抓
  * 可选：依次点击写着“下载 / 附件 / download”的按钮（翻页后的每一页也点），记录由此触发的下载、弹窗和跳转。
    确认框一律自动取消；带“删除 / 提交 / 支付 / 退出”等字样的按钮不点
  * 页面 JS 发请求时带的 Authorization / token 请求头（按主机记），下载文件时原样带上

LoginSession：弹出一个看得见的浏览器让你自己登录（统一身份认证、扫码、验证码都行），
把 cookie 和 localStorage（很多平台的 token 存在这里）存成 Playwright storage_state 文件，
之后每次渲染都用它新建 context，requests 也装上其中的 cookie。

Playwright 的同步 API 只能在创建它的线程里用，所以浏览器操作都放在一个专用线程里，
爬虫的工作线程通过队列提交任务并等结果。浏览器只启动一次（空闲 5 分钟自动关闭），
每个网页用一个新的 context，互不串 cookie。图片、字体、音视频只记地址不加载。

依赖
  pip install playwright && python -m playwright install chromium
  （没装 Playwright 自带的 Chromium 时，会依次尝试本机的 Google Chrome、Microsoft Edge）
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DOWNLOAD_WORDS = "下载|附件|导出|download|attachment"
DANGER_WORDS = ("删除|移除|清空|退出|注销|登出|提交|上传|支付|付款|购买|"
                "delete|remove|logout|log out|sign out|submit|upload|pay|buy|purchase")
MORE_WORDS = "(?:点击)?(?:加载|查看|显示|展开)更多|load more|show more|view more|more results"
NEXT_WORDS = "下一页|下页|后一页|下一頁|next page"
BLOCK_TYPES = {"image", "media", "font"}
MAX_JSON_BYTES = 2 << 20
MAX_JSON_COUNT = 100  # 翻页时每一页的列表接口都要留着
MAX_JSON_TOTAL = 32 << 20
MAX_CLICKS = 15
CLICK_BUDGET = 25.0  # 点下载按钮（含翻页后的每一页）最多花多少秒
MAX_EXPAND = 30  # 最多点多少次“加载更多 / 下一页”
EXPAND_BUDGET = 45.0  # 翻页阶段最多花多少秒
# 前端框架常用来放登录凭证的请求头
AUTH_HEADER = re.compile(r"^(authorization|token|access-?token|x-(auth|access|csrf|xsrf)-token|x-token|satoken|blade-auth)$", re.I)

# 在每个 frame 里执行：收集 JS 生成之后的链接
COLLECT_JS = r"""() => {
  const out = [];
  const add = (u, t) => { if (u && typeof u === 'string') out.push([u, String(t || '').trim().slice(0, 200)]); };
  for (const el of document.querySelectorAll('a[href], area[href]'))
    add(el.href, el.getAttribute('download') || el.innerText || el.title);
  for (const el of document.querySelectorAll('iframe[src], frame[src], embed[src], source[src], video[src], audio[src]'))
    add(el.src, '');
  for (const el of document.querySelectorAll('object[data]')) add(el.data, '');
  for (const el of document.querySelectorAll('[data-href], [data-url], [data-src], [data-file], [data-download]'))
    for (const k of ['href', 'url', 'src', 'file', 'download'])
      if (el.dataset[k]) { try { add(new URL(el.dataset[k], document.baseURI).href, el.innerText); } catch (e) {} }
  return out;
}"""

# 给“下载”按钮编号，返回个数。已经有真实 href 的链接不算（静态解析就能拿到）
MARK_JS = r"""([words, danger, max]) => {
  const ok = new RegExp(words, 'i'), bad = new RegExp(danger, 'i');
  const sel = 'button, [role=button], a:not([href]), a[href^="javascript:" i], a[href="#"], [onclick], input[type=button]';
  let n = 0;
  for (const el of document.querySelectorAll('[data-sc-click]')) el.removeAttribute('data-sc-click');
  for (const el of document.querySelectorAll(sel)) {
    if (el.hasAttribute('data-sc-done')) continue;  // “加载更多”之后再点一轮：前面点过的不再点
    const t = (el.innerText || el.value || el.title || el.getAttribute('aria-label') || '').trim();
    if (!t || t.length > 40 || !ok.test(t) || bad.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    el.setAttribute('data-sc-click', n++);
    el.setAttribute('data-sc-done', '');
    if (n >= max) break;
  }
  return n;
}"""

# 找一个“加载更多”（优先）或“下一页”按钮，标上 data-sc-more，返回 "more:文字" / "next:文字"，没有返回 null。
# 有真实地址的链接不点（爬虫会当网页去抓）；单独的“next / >”和没字的图标按钮只在分页条里认，免得点到轮播图的箭头
FIND_MORE_JS = r"""([more, next, danger]) => {
  const okMore = new RegExp('^(?:' + more + ')\\s*[.…>›»]*$', 'i');
  const okNext = new RegExp('^(?:' + next + ')\\s*[>›»]*$', 'i');
  const arrow = /^(?:next|[>›»]{1,2})$/i;
  const bad = new RegExp(danger, 'i');
  const pager = '[class*=pagination], [class*=pager], [class*=paging], [class*=pagenav], [class*=page-nav], [class*=laypage]';
  const nextCls = /(?:^|[\s_-])(?:btn-next|next-btn|page-next|next-page|nextpage|next)(?:$|[\s_-])/i;
  const cls = (el) => (typeof el.className === 'string' ? el.className : el.getAttribute('class') || '');
  const disabled = (el) => {
    for (let e = el, i = 0; e && i < 3; e = e.parentElement, i++)
      if (e.disabled || e.getAttribute('aria-disabled') === 'true' || /(?:^|[\s_-])disabled(?:$|\s)/i.test(cls(e))) return true;
    return false;
  };
  const realLink = (el) => {
    const a = el.closest('a[href]');
    const h = a ? a.getAttribute('href').trim() : '';
    return (!!h && !/^(?:#|javascript:)/i.test(h)) || /^#[\/!]/.test(h);
  };
  for (const el of document.querySelectorAll('[data-sc-more]')) el.removeAttribute('data-sc-more');
  let pick = null, kind = '';
  for (const el of document.querySelectorAll('button, [role=button], a, [onclick], input[type=button], li, span')) {
    if (realLink(el)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const t = (el.innerText || el.value || '').trim();
    const label = (el.getAttribute('aria-label') || el.title || '').trim();
    if (t.length > 20 || bad.test(t) || bad.test(label)) continue;
    let k = '';
    if (okMore.test(t)) k = 'more';
    else if (okNext.test(t) || okNext.test(label)) k = 'next';
    else if (el.closest(pager) && (arrow.test(t) || arrow.test(label) || (!t && nextCls.test(cls(el))))) k = 'next';
    if (!k || disabled(el)) continue;
    if (!pick || (k === 'more' && kind !== 'more')) { pick = el; kind = k; }
    if (kind === 'more') break;
  }
  if (!pick) return null;
  pick.setAttribute('data-sc-more', '');
  return kind + ':' + (pick.innerText || pick.value || pick.getAttribute('aria-label') || pick.title || '').trim().slice(0, 20);
}"""

# 页面内容指纹：点完“加载更多 / 下一页”有没有变化
SIG_JS = r"""() => {
  let s = location.href + '\n' + (document.body ? document.body.innerText : '');
  for (const a of document.querySelectorAll('a[href]')) s += '\n' + a.href;
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return s.length + ':' + h;
}"""

SCROLL_JS = """() => {
  window.scrollBy(0, window.innerHeight || 800);
  const d = document.scrollingElement || document.documentElement;
  return d.scrollHeight - d.scrollTop - d.clientHeight;
}"""


class RenderError(Exception):
    pass


@dataclass
class RenderResult:
    url: str
    html: str = ""
    title: str = ""
    links: list = field(default_factory=list)      # [(url, 文字)]：DOM 里的链接
    network: list = field(default_factory=list)    # [(url, method, status, resource_type, content_type, content_disposition)]
    json: list = field(default_factory=list)       # [(url, 响应文本)]
    downloads: list = field(default_factory=list)  # [(url, 建议文件名)]
    opened: list = field(default_factory=list)     # 点击后弹出的窗口 / 跳转到的地址
    auth: dict = field(default_factory=dict)       # 主机 → {请求头: 值}，页面 JS 发 XHR / fetch 时带的凭证
    clicked: int = 0
    expanded: int = 0                              # 点了几次“加载更多 / 下一页”


def check():
    """可用返回 None，否则返回给人看的提示。"""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return "JS 渲染需要 Playwright：pip install playwright && python -m playwright install chromium"
    return None


def _first_line(e, n=200):
    s = str(e).strip().splitlines()
    return (s[0] if s else type(e).__name__)[:n]


class Renderer:
    """线程安全：任何线程都可以调用 render()，实际渲染串行地在专用线程里做。"""

    def __init__(self, headless=True, idle_close=300.0):
        self.headless = headless
        self.idle_close = idle_close
        self.q = queue.Queue()
        self.thread = None
        self.lock = threading.Lock()
        self.engine = ""  # 实际用上的浏览器

    def render(self, url, *, cookies=(), headers=None, header_hosts=(), user_agent=None, proxy=None,
               insecure=False, timeout=30.0, click=False, stop=None, storage_state=None):
        err = check()
        if err:
            raise RenderError(err)
        self._ensure()
        fut = Future()
        self.q.put((fut, dict(url=url, cookies=list(cookies), headers=dict(headers or {}),
                              header_hosts=set(header_hosts), user_agent=user_agent, proxy=proxy,
                              insecure=insecure, timeout=float(timeout), click=click, storage_state=storage_state)))
        deadline = time.monotonic() + timeout * 2 + CLICK_BUDGET + EXPAND_BUDGET + 60  # 前面可能还排着别的网页
        while True:
            try:
                return fut.result(timeout=0.5)
            except TimeoutError:
                stopped = stop is not None and stop.is_set()
                if stopped or time.monotonic() > deadline:
                    fut.cancel()
                    raise RenderError("已停止" if stopped else "渲染超时") from None

    def close(self):
        with self.lock:
            t, self.thread = self.thread, None
            if t and t.is_alive():
                self.q.put(None)
        if t:
            t.join(timeout=15)

    def _ensure(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="js-render", daemon=True)
                self.thread.start()

    # ── 以下都在渲染线程里 ──

    def _loop(self):
        try:
            from playwright.sync_api import sync_playwright

            pw = sync_playwright().start()
        except Exception as e:
            self._fail_pending(f"启动 Playwright 失败：{_first_line(e)}")
            return
        browser = None
        try:
            while True:
                try:
                    item = self.q.get(timeout=self.idle_close)
                except queue.Empty:
                    if browser:  # 空闲太久就关掉浏览器，省内存；下次用到再开
                        browser.close()
                        browser = None
                    continue
                if item is None:
                    break
                fut, kw = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    if browser is None or not browser.is_connected():
                        browser = self._launch(pw)
                    fut.set_result(self._render(browser, **kw))
                except RenderError as e:
                    fut.set_exception(e)
                except Exception as e:
                    fut.set_exception(RenderError(f"{type(e).__name__}: {_first_line(e)}"))
        finally:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
            pw.stop()

    def _fail_pending(self, msg):
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                return
            if item and item[0].set_running_or_notify_cancel():
                item[0].set_exception(RenderError(msg))

    def _launch(self, pw):
        b, self.engine = launch(pw, self.headless)
        return b

    def _render(self, browser, url, cookies, headers, header_hosts, user_agent, proxy, insecure, timeout, click,
                storage_state):
        from playwright.sync_api import Error as PWError

        ms = int(timeout * 1000)
        opts = {"ignore_https_errors": insecure, "accept_downloads": True, "locale": "zh-CN"}
        if user_agent:
            opts["user_agent"] = user_agent
        if proxy:
            opts["proxy"] = {"server": proxy}
        if storage_state:  # 登录后保存的 cookie + localStorage
            opts["storage_state"] = storage_state
        ctx = browser.new_context(**opts)
        res = RenderResult(url)
        responses, blocked, downloads, popups = [], [], [], []
        try:
            if cookies:
                try:
                    ctx.add_cookies(cookies)
                except PWError:
                    pass

            def route(r):
                req = r.request
                try:
                    if req.resource_type in ("xhr", "fetch"):
                        for k, v in req.headers.items():
                            if AUTH_HEADER.match(k) and v:
                                res.auth.setdefault(urlsplit(req.url).netloc, {})[k] = v
                    if req.resource_type in BLOCK_TYPES:
                        blocked.append(req.url)
                        r.abort()
                    elif headers and (urlsplit(req.url).hostname or "") in header_hosts:
                        r.continue_(headers={**req.headers, **headers})  # 自定义请求头只发给目标站点
                    else:
                        r.continue_()
                except PWError:
                    pass

            # 回调必须是普通函数：Playwright 会往回调对象上挂属性，list.append 这种内置方法不行
            def on_download(d):
                downloads.append(d)

            def on_response(r):
                responses.append(r)

            def on_popup(p):
                popups.append(p)
                p.on("download", on_download)

            ctx.route("**/*", route)
            ctx.on("response", on_response)
            page = ctx.new_page()
            page.on("download", on_download)
            page.on("popup", on_popup)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=ms)
            except PWError as e:
                if "Download is starting" in str(e):  # 根本不是网页
                    res.downloads.append((url, ""))
                    return res
                raise RenderError(f"打开网页失败：{_first_line(e)}") from None
            self._settle(page, 8000)
            self._scroll(page)
            click_deadline = time.monotonic() + CLICK_BUDGET
            if click:
                self._click_downloads(page, ms, res, click_deadline)
            self._expand(page, ms, res, click, click_deadline)
            res.url, res.title, res.html = page.url, page.title(), page.content()
            self._collect(page, res)
            res.links = list(dict.fromkeys(res.links))  # 翻页时每一页都收集过一遍，去重
            for p in popups:
                if p.url and p.url != "about:blank":
                    res.opened.append(p.url)
            for d in downloads:
                res.downloads.append((d.url, d.suggested_filename))
                try:
                    d.cancel()
                except PWError:
                    pass
            json_bytes = 0
            for resp in responses:
                try:
                    req, h = resp.request, resp.headers
                    ctype = h.get("content-type", "")
                    res.network.append((resp.url, req.method, resp.status, req.resource_type,
                                        ctype, h.get("content-disposition", "")))
                    if ("json" in ctype.lower() and resp.status < 400 and len(res.json) < MAX_JSON_COUNT
                            and int(h.get("content-length") or 0) <= MAX_JSON_BYTES):
                        body = resp.body()
                        if len(body) <= MAX_JSON_BYTES and json_bytes + len(body) <= MAX_JSON_TOTAL:
                            res.json.append((resp.url, body.decode("utf-8", "replace")))
                            json_bytes += len(body)
                except PWError:
                    pass
            res.network += [(u, "GET", 0, "blocked", "", "") for u in blocked]
            return res
        finally:
            try:
                ctx.close()
            except PWError:
                pass

    @staticmethod
    def _settle(page, ms):
        from playwright.sync_api import Error as PWError

        try:
            page.wait_for_load_state("networkidle", timeout=ms)
        except PWError:
            pass  # 有长连接 / 轮询的页面永远等不到 networkidle，到点就算

    def _scroll(self, page):
        """滚到底，触发懒加载 / 无限滚动的列表。"""
        from playwright.sync_api import Error as PWError

        try:
            for _ in range(8):
                left = page.evaluate(SCROLL_JS)
                page.wait_for_timeout(300)
                if left <= 0:
                    break
        except PWError:
            return
        self._settle(page, 3000)

    @staticmethod
    def _collect(page, res):
        from playwright.sync_api import Error as PWError

        for fr in page.frames:
            try:
                res.links += [tuple(x) for x in fr.evaluate(COLLECT_JS)]
            except PWError:
                pass

    def _expand(self, page, ms, res, click, click_deadline):
        """反复点“加载更多 / 下一页”，每点一次收集一遍链接（点了“下一页”，上一页的列表就被换掉了）。
        找不到按钮、按钮变灰、点了页面没变化、或者到了次数 / 时间上限就停。"""
        from playwright.sync_api import Error as PWError

        deadline = time.monotonic() + EXPAND_BUDGET
        while res.expanded < MAX_EXPAND and time.monotonic() < deadline:
            try:
                found = page.evaluate(FIND_MORE_JS, [MORE_WORDS, NEXT_WORDS, DANGER_WORDS])
                if not found:
                    return
                if not res.expanded:
                    self._collect(page, res)  # 第一页的链接
                before = page.evaluate(SIG_JS)
                page.evaluate("() => { window.__scSameDoc = true; }")
                page.locator("[data-sc-more]").first.click(timeout=3000)
            except PWError:
                return
            self._settle(page, 5000)
            try:
                same_doc = page.evaluate("() => window.__scSameDoc === true")  # pushState 换地址不算跳走
            except PWError:
                same_doc = False
            if not same_doc:  # 整页跳到了下一页：记下地址交给爬虫当翻页链接（不算新的一层），这里不再接着翻
                res.links.append((page.url, "下一页"))
                return
            try:
                after = page.evaluate(SIG_JS)
                if after == before:  # 可能还在加载，再等一下
                    page.wait_for_timeout(1500)
                    after = page.evaluate(SIG_JS)
            except PWError:
                return
            if after == before:  # 点了没反应：到最后一页了，或者这不是翻页按钮
                return
            res.expanded += 1
            self._collect(page, res)
            if click and self._click_downloads(page, ms, res, click_deadline):
                return  # 点下载按钮时跳走、重新打开了原页面：翻页状态没了，不再接着翻

    def _click_downloads(self, page, ms, res, deadline):
        """依次点“下载”按钮，点过的不再点。返回 True 表示中途跳走后重新打开了原页面（翻页状态没了）。"""
        from playwright.sync_api import Error as PWError

        start, reset = page.url, False
        if time.monotonic() > deadline:
            return False
        try:
            n = page.evaluate(MARK_JS, [DOWNLOAD_WORDS, DANGER_WORDS, MAX_CLICKS])
        except PWError:
            return False
        for i in range(n):
            if time.monotonic() > deadline:
                break
            try:
                loc = page.locator(f'[data-sc-click="{i}"]')
                if not loc.count():
                    continue
                loc.first.click(timeout=3000)
                res.clicked += 1
                page.wait_for_timeout(1200)
            except PWError:
                continue
            if page.url.split("#")[0] != start.split("#")[0]:  # 点完跳走了：记下地址，回到原页面继续
                res.opened.append(page.url)
                reset = True
                try:
                    page.goto(start, wait_until="domcontentloaded", timeout=ms)
                    self._settle(page, 5000)
                    page.evaluate(MARK_JS, [DOWNLOAD_WORDS, DANGER_WORDS, MAX_CLICKS])
                except PWError:
                    break
        if n:
            self._settle(page, 3000)
        return reset


def launch(pw, headless):
    """依次试 Playwright 自带的 Chromium、本机 Chrome、Edge。看得见的登录窗口优先用本机 Chrome（少被当成机器人）。"""
    errors = []
    for channel in ((None, "chrome", "msedge") if headless else ("chrome", None, "msedge")):
        try:
            return pw.chromium.launch(headless=headless, channel=channel), channel or "chromium"
        except Exception as e:
            errors.append(f"{channel or 'chromium'}: {_first_line(e, 100)}")
    raise RenderError("找不到可用的浏览器，请运行 python -m playwright install chromium（" + "；".join(errors) + "）")


def save_state(path, state):
    """登录状态里有会话 cookie，只给自己读写（0600）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


class LoginSession:
    """弹出看得见的浏览器让用户自己登录。finish() 或用户关掉窗口时，把 cookie + localStorage 存到 state_path。

    状态 status：starting → open → saved / cancelled / error。current_url 是用户最后停留的页面（用来接着扫描）。
    """

    def __init__(self, url, state_path, *, proxy=None, insecure=False, headless=None):
        self.url, self.path = url, Path(state_path)
        self.proxy, self.insecure = proxy, insecure
        # 自动化测试里没人点窗口，用环境变量换成无头
        self.headless = os.environ.get("SC_LOGIN_HEADLESS") == "1" if headless is None else headless
        self.status, self.error, self.engine = "starting", "", ""
        self.current_url, self.cookies, self.saved_at = url, 0, None
        self.cmd = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="login-browser", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def alive(self):
        return self.thread.is_alive()

    def finish(self, timeout=30):
        """保存并关闭浏览器。"""
        if self.alive():
            self.cmd.put("save")
            self.thread.join(timeout)
        return self.status

    def cancel(self, timeout=15):
        """不保存，关闭浏览器（之前保存过的登录状态不动）。"""
        if self.alive():
            self.cmd.put("cancel")
            self.thread.join(timeout)
        return self.status

    def info(self):
        return {"status": self.status, "url": self.current_url, "cookies": self.cookies,
                "error": self.error, "engine": self.engine, "saved_at": self.saved_at}

    def _run(self):
        err = check()
        if err:
            self.status, self.error = "error", err
            return
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as pw:
                browser, self.engine = launch(pw, self.headless)
                opts = {"ignore_https_errors": self.insecure, "locale": "zh-CN"}
                if not self.headless:
                    opts["no_viewport"] = True  # 窗口多大页面就多大
                if self.proxy:
                    opts["proxy"] = {"server": self.proxy}
                if self.path.is_file():  # 在上次的登录状态上继续，常常不用再输密码
                    opts["storage_state"] = str(self.path)
                ctx = browser.new_context(**opts)

                def on_nav(frame):
                    if frame.parent_frame is None and frame.url.startswith("http"):
                        self.current_url = frame.url

                def on_page(p):
                    p.on("framenavigated", on_nav)

                ctx.on("page", on_page)
                page = ctx.new_page()
                try:
                    page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
                except PWError as e:
                    self.error = f"打开网页失败：{_first_line(e)}（可以在窗口里手动输入网址）"
                self.status = "open"
                state, last = None, 0.0
                while True:
                    try:
                        cmd = self.cmd.get(timeout=0.5)
                    except queue.Empty:
                        cmd = None
                    if not browser.is_connected() or not [p for p in ctx.pages if not p.is_closed()]:
                        cmd = cmd or "save"  # 用户把窗口关了：用最后一次拿到的状态
                    if cmd is None and time.monotonic() - last > 2:
                        state, last = self._snapshot(ctx) or state, time.monotonic()
                    if cmd == "cancel":
                        self.status = "cancelled"
                        break
                    if cmd == "save":
                        state = self._snapshot(ctx) or state
                        if state is None:
                            raise RenderError("浏览器已经关了，没拿到登录状态")
                        save_state(self.path, state)
                        self.cookies, self.saved_at, self.status = len(state.get("cookies", [])), time.time(), "saved"
                        break
                try:
                    browser.close()
                except PWError:
                    pass
        except Exception as e:  # noqa: BLE001  任何问题都要让界面 / 命令行知道
            self.status, self.error = "error", f"{type(e).__name__}: {_first_line(e)}"

    @staticmethod
    def _snapshot(ctx):
        from playwright.sync_api import Error as PWError

        try:
            return ctx.storage_state()
        except PWError:
            return None
