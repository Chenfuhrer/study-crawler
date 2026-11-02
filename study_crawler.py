#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
study_crawler.py —— 学习资料批量下载爬虫

从课程主页 / 资料索引页出发，按深度广度优先爬取，自动识别并下载 PDF、PPT、Word、
Excel、压缩包、电子书、ipynb 等资料文件。

特性
  * 多线程并发 + 按主机限速（默认同一主机每秒 1 个请求），遵守 robots.txt（含 Crawl-delay），
    遇到 429 自动降速
  * 断点续传：中断后再跑同一条命令即可继续；下到一半的大文件用 HTTP Range + If-Range 接着下
    （服务器上的文件更新过就整个重下，不会把新旧两个版本拼在一起）
  * 下载校验：按文件头（magic bytes）确认真是 PDF / Office / 压缩包…；接口回的 {"code":500,"msg":…}、
    错误网页、登录页不会被存成文件，失败原因里写明对方说了什么
  * 登录失效（401、跳到登录页、接口说“请重新登录”）时暂停整批、保留进度，退出码 3；重新登录后再跑即可接着下
  * 自动重试：超时 / 断连 / 429 / 5xx 指数退避，尊重 Retry-After
  * 识别没有扩展名的下载链接（download.php?id=3），从 Content-Disposition 取真实文件名，
    修复中文文件名乱码；也能从 onclick / <script> / 预览器参数（viewer.html?file=x.pdf）里挖链接
  * SHA-256 内容去重、文件名清洗、同名自动编号
  * 支持浏览器导出的 cookies.txt，下载需要登录才能看的课程资料（cookie / 自定义请求头只发给目标站点）
  * 正则包含 / 排除、爬取范围控制、文件大小上限、dry-run 预览
  * -d all 不限层数（全站）；--sitemap 读网站的 sitemap（robots.txt 里声明的，没有就试 /sitemap.xml；
    sitemap 索引、.gz、纯文本格式都行），导航里没有链接、只在 sitemap 里列出的网页和文件也能找到
  * 翻页链接不算层：“下一页 / 下页 / next page / ›”，以及跟当前页像同一个列表的页码（list_2.html、?page=3），
    翻到的页面沿用当前页的层数，-d 1 也能把分页列表翻完（同一条翻页链连续 100 页之后照常算层）
  * --js：用无头浏览器渲染 Vue/React/AJAX 动态页面，收集 JS 生成的链接、接口 JSON 里的文件地址、
    网络请求里的文件；会自动点“加载更多 / 下一页”，把后面几页的列表也收进来；
    --js-click 还会点一遍页面上的“下载”按钮，记录触发的下载（见 js_render.py）
  * --login：弹出真浏览器让你自己登录（统一身份认证、扫码都行），cookie + localStorage 按站点保存，
    之后扫描和下载自动带上；渲染时嗅探到的 Authorization / token 请求头也会在下载时带上（只发给原主机），
    并随登录状态存到 ~/.study_crawler/tokens/（0600），重启后照样能用；--logout 一起删除
  * 保留 #/course/42 这类单页应用的 hash 路由，每个路由当成一个网页去渲染
  * 输出 manifest.csv（下载清单）、failed.txt（失败清单）、crawl.log（详细日志）
  * 网页界面：python web_app.py（输入网址 → 列出文件 → 勾选下载）

依赖
  pip install requests beautifulsoup4 lxml      # lxml 可选，装了解析更快
  pip install playwright && python -m playwright install chromium   # 仅 --js 需要

示例
  # 只下载课程页上直接链接的资料（不进入子页面）
  python study_crawler.py https://example.edu/course/ds/ -d 0

  # 爬 2 层，只要 pdf 和 pptx，且 URL / 链接文字 / 文件名里含“课件”或“习题”
  python study_crawler.py https://example.edu/course/ds/ -d 2 --ext pdf,pptx --include '课件|习题'

  # 在默认类型之外再加 mp4；用浏览器导出的 cookies.txt 访问需要登录的页面
  python study_crawler.py https://lms.example.edu/course/123 --ext +mp4 --cookies cookies.txt

  # 先预览会下载哪些文件
  python study_crawler.py https://example.edu/course/ds/ --dry-run

  # 整个网站：不限层数 + 读 sitemap + 不限网页数（大网站很慢，先 --dry-run 看看规模）
  python study_crawler.py https://example.edu/ -d all --sitemap --max-pages 0 --js auto --dry-run

  # 资料列表是 JS 动态加载的：auto = 页面看起来是动态的才开浏览器；on = 每个网页都渲染
  python study_crawler.py https://spa.example.edu/#/course/42 --js auto --js-click

  # 要登录的课程平台：先在弹出的浏览器里登录，然后从登录后停留的页面开始爬；以后不用再登录
  python study_crawler.py https://lms.example.edu/ --login --js auto
  python study_crawler.py https://lms.example.edu/#/course/42 --js auto     # 自动用保存的登录状态
  python study_crawler.py https://lms.example.edu/ --logout                  # 删除保存的登录状态
"""
from __future__ import annotations

import argparse
import csv
import email.utils
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import zlib
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, unquote_to_bytes, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

try:
    import requests
    import urllib3
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("缺少依赖，请先运行：pip install requests beautifulsoup4 lxml")

try:
    import lxml  # noqa: F401

    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

VERSION = "1.0"
BOT_NAME = "StudyCrawler"
DEFAULT_UA = f"Mozilla/5.0 (compatible; {BOT_NAME}/{VERSION}; personal study use)"

DEFAULT_EXTS = {
    # 文档
    "pdf", "doc", "docx", "ppt", "pptx", "pps", "ppsx", "xls", "xlsx", "odt", "odp", "ods", "rtf",
    # 电子书
    "epub", "mobi", "azw3", "djvu", "chm",
    # 压缩包
    "zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz",
    # 代码 / 数据
    "ipynb", "csv", "mat",
}
# 这些扩展名按“网页”对待：会被请求并解析其中的链接
PAGE_EXTS = {"", "html", "htm", "shtml", "xhtml", "php", "asp", "aspx", "jsp", "jspx", "do", "action", "cgi", "pl"}
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# mimetypes 不全或给出冷门扩展名时的兜底
MIME_EXT = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/x-rar-compressed": "rar",
    "application/vnd.rar": "rar",
    "application/x-7z-compressed": "7z",
    "application/epub+zip": "epub",
    "application/gzip": "gz",
    "application/x-gzip": "gz",
    "text/csv": "csv",
    "text/plain": "txt",
}
# 超出深度 / 范围的链接，只要像下载链接，仍会请求一次看看是不是文件（不解析网页）
DOWNLOAD_HINT = re.compile(r"(?i)download|attach|getfile|fileid|下载|附件")
# 没有信息量的链接文字，不拿来当文件名
MEANINGLESS_ANCHOR = re.compile(r"(?i)\s*(点击)?(下载|预览|查看|打开|附件|链接|这里|此处)\s*|\s*(download|click here|here|link|view)\s*")
LINK_ATTRS = (("a", "href"), ("area", "href"), ("iframe", "src"), ("frame", "src"),
              ("embed", "src"), ("object", "data"), ("source", "src"))
# 只有明确要脚本 / 样式 / 图片 / 音视频时才收集这些标签，免得 --ext '*' 把网页零件全下下来
RESOURCE_ATTRS = (("script", "src"), ("link", "href"), ("img", "src"), ("video", "src"), ("audio", "src"))
RESOURCE_EXTS = {"js", "mjs", "css", "wasm", "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico",
                 "mp4", "webm", "mkv", "mov", "mp3", "m4a", "ogg", "wav", "flac"}
# --js auto：内联脚本在拉数据，或者引用了前端框架 / 打包产物，说明页面内容多半是 JS 生成的
AJAX_HINT = re.compile(r"fetch\(|\$\.(?:ajax|get|post|getJSON)\(|XMLHttpRequest|axios")
SPA_SCRIPT = re.compile(r"(?i)vue|react|angular|svelte|chunk|bundle|app\.[0-9a-f]{6,}")
SPA_MOUNT = re.compile(r"^(app|root|__next|__nuxt)$")
# 没有真实地址的“加载更多 / 下一页”按钮：后面的列表要靠 JS 点出来
PAGER_TEXT = re.compile(r"(?i)\s*(?:(?:点击)?(?:加载|查看|显示|展开)更多|load\s+more|show\s+more|下一页|下页|next\s+page)\s*[.…>›»]*\s*")
# 翻页链接：文字是“下一页 / next / ›”，或者是页码（地址还得跟当前页像同一个列表）。翻到的页面不算新的一层
NEXT_LINK = re.compile(r"(?i)\s*(?:下一页|下页|后一页|下一頁|next(?:\s+page)?|[>›»]{1,2})\s*[>›»]*\s*")
PAGE_NUM = re.compile(r"\s*[\[【(（]?\s*(?:第\s*)?\d{1,4}\s*页?\s*[\]】)）]?\s*")
PAGE_SEQ = re.compile(r"[_-]?\d+")
MAX_PAGE_HOPS = 100  # 同一条翻页链最多连续这么多页不算层（防日历的“›”这种翻不完的），之后照常 +1
# -d all：不限层数
DEPTH_ALL = 1_000_000
# sitemap：最多读多少个文件 / 收多少个网址；单个文件上限按 sitemap 规范（解压后 50MB）
MAX_SITEMAPS = 50
MAX_SITEMAP_URLS = 100_000
MAX_SITEMAP_BYTES = 50 << 20
SITEMAP_ROOT = re.compile(r"<(?:[\w-]+:)?(urlset|sitemapindex)\b", re.I)
SITEMAP_LOC = re.compile(r"<(?:[\w-]+:)?(?:loc|content_loc)>\s*(?:<!\[CDATA\[)?\s*([^<\]]+?)\s*(?:\]\]>)?\s*</", re.I)
# 接口 JSON 里，这些键的值通常是下载地址 / 文件名
JSON_URL_KEYS = {"url", "href", "link", "src", "path", "file", "fileurl", "file_url", "filepath", "file_path",
                 "download", "downloadurl", "download_url", "attachment", "attachmenturl", "previewurl"}
JSON_NAME_KEYS = {"name", "filename", "file_name", "title", "originalname", "original_name", "displayname", "label"}

# 下载校验：先看文件开头（magic bytes）。扩展名说是 PDF，开头却是 {"code":401} 或登录页，就不能当文件存下来
HEAD_BYTES = 64 * 1024
EXT_FORMAT = {
    "pdf": "pdf", "rar": "rar", "7z": "7z", "gz": "gz", "tgz": "gz", "bz2": "bz2", "xz": "xz", "tar": "tar",
    "djvu": "djvu", "chm": "chm", "rtf": "rtf", "png": "png", "jpg": "jpg", "jpeg": "jpg", "gif": "gif",
    "mobi": "mobi", "azw3": "mobi", "azw": "mobi", "mkv": "ebml", "webm": "ebml", "ogg": "ogg", "flac": "flac",
    "mp3": "mp3", "webp": "riff", "wav": "riff", "avi": "riff",
    **dict.fromkeys(("mp4", "m4a", "m4v", "mov", "3gp"), "mp4"),
    **dict.fromkeys(("zip", "docx", "pptx", "xlsx", "ppsx", "epub", "odt", "odp", "ods", "jar", "xmind"), "zip"),
    **dict.fromkeys(("doc", "xls", "ppt", "pps", "wps", "et", "dps", "msg"), "ole"),
}
MAGIC = (  # (格式, 偏移, 开头)
    ("zip", 0, b"PK\x03\x04"), ("zip", 0, b"PK\x05\x06"), ("zip", 0, b"PK\x07\x08"),
    ("ole", 0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"), ("rar", 0, b"Rar!\x1a\x07"), ("7z", 0, b"7z\xbc\xaf\x27\x1c"),
    ("gz", 0, b"\x1f\x8b"), ("bz2", 0, b"BZh"), ("xz", 0, b"\xfd7zXZ\x00"), ("tar", 257, b"ustar"),
    ("djvu", 0, b"AT&TFORM"), ("chm", 0, b"ITSF"), ("rtf", 0, b"{\\rtf"), ("png", 0, b"\x89PNG\r\n\x1a\n"),
    ("jpg", 0, b"\xff\xd8\xff"), ("gif", 0, b"GIF8"), ("mobi", 60, b"BOOKMOBI"), ("ebml", 0, b"\x1a\x45\xdf\xa3"),
    ("ogg", 0, b"OggS"), ("flac", 0, b"fLaC"), ("mp3", 0, b"ID3"), ("riff", 0, b"RIFF"), ("mp4", 4, b"ftyp"),
)
MARKUP_EXTS = PAGE_EXTS | {"xml", "svg", "mht", "mhtml"}
JSON_EXTS = {"json", "ipynb", "geojson", "har"}
LOOKS_HTML = re.compile(rb"\s*(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html|head|body|meta|title|script|form|div)\b", re.I | re.S)
LOGIN_FORM = re.compile(r"""(?i)type\s*=\s*["']?password|<title>[^<]*(?:登录|登入|统一身份|认证|sign\s*in|log\s*in)""")
LOGIN_URL = re.compile(r"(?i)(?:^|[/.?=&_-])(?:login|logon|signin|sign-in|sso|cas|passport|authserver|oauth2?|uaa|idp)(?:$|[/.?=&_-])")
# 接口出错时的提示里出现这些词，说明是登录失效，不是这个文件本身有问题（“无权限”不算：重新登录也没用）
AUTH_HINT = re.compile(r"(?i)未登录|没有登录|(?:登录|会话|令牌|凭证|token|session)\s*(?:已|已经)?(?:过期|失效|超时|无效)|请(?:先|重新)登录"
                       r"|unauthori[sz]ed|not\s+logged|login\s+required|(?:token|session)\s+(?:expired|invalid)|身份(?:验证|认证)失败")
AUTH_CODES = {"401", "-401", "40100", "40101", "1001"}

log = logging.getLogger("study_crawler")


# ───────────────────────── 工具函数 ─────────────────────────

def short(e, n=150):
    s = str(e)
    return s if len(s) <= n else s[:n] + "…"


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def parse_size(s):
    m = re.fullmatch(r"\s*([\d.]+)\s*([kmgt]?)i?b?\s*", s, re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"无法识别的大小：{s}（例：500M、2G）")
    return int(float(m.group(1)) * 1024 ** " kmgt".index(m.group(2).lower() or " "))


def parse_exts(spec):
    """--ext：'pdf,pptx' 替换默认类型，'+mp4,mkv' 在默认类型上追加，'*' 表示所有非网页内容。"""
    if not spec:
        return set(DEFAULT_EXTS)
    spec = spec.strip()
    if spec == "*":
        return None
    items = {e.strip().lower().lstrip(".") for e in spec.lstrip("+").split(",") if e.strip()}
    return (set(DEFAULT_EXTS) | items) if spec.startswith("+") else items


def sha1(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def url_ext(url):
    name = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def name_ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def is_pager_link(parent, url, anchor):
    """翻页链接：文字是“下一页 / 下页 / next page / ›”；或者文字是页码，且地址跟当前页在同一个目录、
    或者只差数字（/news/ → /news/list_2.html、index.html → index_3.html、?page=2 → ?page=3）。"""
    if NEXT_LINK.fullmatch(anchor):
        return True
    if not PAGE_NUM.fullmatch(anchor):
        return False
    a, b = urlsplit(parent), urlsplit(url)
    return a.netloc == b.netloc and (a.path.rsplit("/", 1)[0] == b.path.rsplit("/", 1)[0]
                                     or PAGE_SEQ.sub("", a.path) == PAGE_SEQ.sub("", b.path))


def better_anchor(old, new):
    """同一个地址出现多次时换不换链接文字：先出现的优先，空的让位；“下一页”这类翻页文字优先（它决定层数）。"""
    if old is None:
        return True
    return bool(new) and (not old or (bool(NEXT_LINK.fullmatch(new)) and not NEXT_LINK.fullmatch(old)))


def normalize_url(href, base):
    """拼成绝对地址，去掉 #片段和默认端口，非 ASCII 字符统一百分号编码，方便去重。
    单页应用的 hash 路由（#/course/42、#!/x）是另一个页面，保留。"""
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "data:")) or (
            href.startswith("#") and not href.startswith(("#/", "#!"))):
        return None
    try:
        p = urlsplit(urljoin(base, href))
        port = p.port
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    host = p.hostname.lower()
    if ":" in host:  # IPv6
        host = f"[{host}]"
    if port and port != {"http": 80, "https": 443}[p.scheme]:
        host = f"{host}:{port}"
    path = quote(p.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(p.query, safe="/%:@!$&'()*+,;=-._~?")
    frag = quote(p.fragment, safe="/%:@!$&'()*+,;=-._~?") if p.fragment.startswith(("/", "!")) else ""
    return urlunsplit((p.scheme, host, path, query, frag))


# 浏览器登录后的状态（cookie + localStorage，Playwright storage_state 格式），按站点存
LOGIN_DIR = Path(os.environ.get("STUDY_CRAWLER_HOME") or Path.home() / ".study_crawler") / "login"


TOKEN_DIR = LOGIN_DIR.parent / "tokens"


def login_state_path(url):
    return LOGIN_DIR / (safe_name(urlsplit(url).netloc) + ".json")


def token_path(url):
    """渲染时嗅探到的 Authorization 等请求头，跟着登录状态按站点存一份（重启后下载接口还要用）。"""
    return TOKEN_DIR / (safe_name(urlsplit(url).netloc) + ".json")


def forget_login(url, tokens_only=False):
    """退出登录删两份；重新登录只删旧令牌（新令牌要等页面再调接口时重新嗅探）。"""
    for p in (token_path(url),) if tokens_only else (login_state_path(url), token_path(url)):
        p.unlink(missing_ok=True)


def write_private(path, obj):
    """原子写 JSON，只给自己读写（0600，目录 0700）：里面有 cookie / 令牌。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def sniff_format(head):
    """按文件开头认格式，认不出返回 ""。PDF 允许前面有少量垃圾字节（规范允许，很多生成器这么干）。"""
    if b"%PDF-" in head[:1024]:
        return "pdf"
    for fmt, off, sig in MAGIC:
        if head[off:off + len(sig)] == sig:
            return fmt
    if head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    return ""


def api_error(obj):
    """接口常见的出错格式 {"code":401,"msg":"未登录"} / {"success":false,"message":…}。
    像出错返回 (提示（可能为空）, 是否像登录失效)，否则 None。"""
    if not isinstance(obj, dict):
        return None
    low = {str(k).lower(): v for k, v in obj.items()}
    msg = next((str(low[k]).strip() for k in ("msg", "message", "errmsg", "errormsg", "error_msg", "error_description",
                                              "detail", "tip", "error")
                if isinstance(low.get(k), (str, int)) and not isinstance(low.get(k), bool) and str(low[k]).strip()), "")
    code = next((str(low[k]).strip() for k in ("code", "errcode", "error_code", "statuscode", "retcode", "status")
                 if isinstance(low.get(k), (str, int)) and not isinstance(low.get(k), bool)
                 and (k != "status" or str(low[k]).lstrip("-").isdigit())), None)  # "status":"published" 不是错误码
    bad = (low.get("success") is False or low.get("ok") is False or low.get("error") not in (None, "", False, 0, {}, [])
           or (code is not None and code.lower() not in ("0", "200", "ok", "success", "true", "")))
    if not bad:
        return None
    return msg, code in AUTH_CODES or bool(AUTH_HINT.search(msg))


def looks_login(text, url=""):
    """登录页：有密码框 / 标题写着登录，或者地址就是 login、cas、sso 这类。"""
    p = urlsplit(url)
    return bool(LOGIN_FORM.search(text) or (url and LOGIN_URL.search(p.netloc + p.path)))


def _chain(first, rest):
    if first:
        yield first
    yield from rest


def read_head(chunks, n=HEAD_BYTES):
    buf = bytearray()
    for c in chunks:
        buf += c
        if len(buf) >= n:
            break
    return bytes(buf)


def ago(sec):
    sec = max(0, int(sec))
    for unit, n in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if sec >= n:
            return f"{sec // n} {unit}"
    return f"{sec} 秒"


_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def safe_name(name, max_bytes=200, default="file"):
    """清洗文件名：去掉非法字符，按 UTF-8 字节数截断（macOS/Linux 单个文件名上限 255 字节），保留扩展名。"""
    name = re.sub(r"\s+", " ", _BAD_CHARS.sub("_", name or "")).strip().strip(".")
    if not name:
        return default
    if len(name.encode("utf-8")) > max_bytes:
        stem, dot, ext = name.rpartition(".")
        if not dot or len(ext) > 10:
            stem, ext = name, ""
        ext = "." + ext if ext else ""
        stem = stem.encode("utf-8")[: max_bytes - len(ext.encode("utf-8"))].decode("utf-8", "ignore")
        name = stem.rstrip() + ext
    return name


def _decode_bytes(b):
    for enc in ("utf-8", "gb18030"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("latin-1")


def fix_header_text(s):
    """HTTP 头按 latin-1 解码；服务器直接塞 UTF-8 / GBK 字节时会变乱码，这里还原。"""
    try:
        return _decode_bytes(s.encode("latin-1"))
    except UnicodeEncodeError:
        return s


def filename_from_cd(cd):
    """从 Content-Disposition 取文件名，支持 RFC 5987 的 filename*=UTF-8''... 和各种不规范写法。"""
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*([\w-]*)'[^']*'([^;]+)", cd, re.I)
    if m:
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8", errors="strict")
        except (LookupError, UnicodeDecodeError):
            pass
    m = re.search(r'filename\s*=\s*"((?:[^"\\]|\\.)*)"', cd, re.I) or re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if not m:
        return None
    raw = m.group(1).strip()
    name = _decode_bytes(unquote_to_bytes(raw)) if re.search(r"%[0-9A-Fa-f]{2}", raw) else fix_header_text(raw)
    return name.replace("\\", "/").rsplit("/", 1)[-1] or None


def mime_of(ctype):
    return (ctype or "").split(";")[0].strip().lower()


def ext_from_mime(ctype):
    mime = mime_of(ctype)
    if mime in MIME_EXT:
        return MIME_EXT[mime]
    if not mime or mime == "application/octet-stream":
        return ""
    ext = mimetypes.guess_extension(mime)
    return ext.lstrip(".") if ext else ""


def charset_of(ctype):
    m = re.search(r"charset=[\"']?([\w-]+)", ctype or "", re.I)
    return m.group(1) if m else None


def retry_after(r):
    v = r.headers.get("Retry-After")
    if not v:
        return None
    try:
        return max(0.0, min(float(v), 300.0))
    except ValueError:
        try:
            return max(0.0, min(email.utils.parsedate_to_datetime(v).timestamp() - time.time(), 300.0))
        except (TypeError, ValueError):
            return None


def build_file_regex(exts):
    """在 onclick / <script> 等原始文本里找形如 '/files/x.pdf' 的字符串。
    download="x.pdf" / title= / alt= 里的是文件名不是地址，跳过。"""
    if not exts:
        return None
    alt = "|".join(sorted(map(re.escape, exts), key=len, reverse=True))
    return re.compile(
        r"""(?<!download=)(?<!title=)(?<!alt=)["'(]\s*((?:https?:)?[^\s"'()<>?#]*\.(?:%s)(?:\?[^\s"'()<>#]*)?)(?:#[^\s"'()<>]*)?\s*["')]""" % alt,
        re.I,
    )


# ───────────────────────── 数据结构 ─────────────────────────

@dataclass
class Task:
    url: str
    kind: str  # page = 网页（解析链接）；file = 按扩展名判定的文件；probe = 疑似下载链接
    depth: int
    referer: str | None = None
    folder: str = ""  # 来源页面标题，--layout page 时作为子目录
    anchor: str = ""  # 链接文字


@dataclass
class Result:
    task: Task
    status: str  # done | skipped | dup | failed | found | interrupted | auth（登录失效，暂停等重新登录）
    note: str = ""
    path: str | None = None
    size: int = 0
    sha256: str | None = None
    title: str = ""
    links: list = field(default_factory=list)  # [(url, anchor)]


class Retryable(Exception):
    def __init__(self, msg, wait=None):
        super().__init__(msg)
        self.wait = wait


class AuthLost(Exception):
    """拿到的是登录页 / 401 / “请重新登录”之类：不是这个文件的问题，换个文件也一样。"""


class HostThrottle:
    """同一主机的相邻请求至少间隔 delay 秒，多线程共享。"""

    def __init__(self, delay):
        self.delay = delay
        self.min_gap = {}
        self.next_slot = {}
        self.lock = threading.Lock()

    def set_min(self, host, gap):
        with self.lock:
            self.min_gap[host] = max(gap, self.min_gap.get(host, 0.0))

    def slow_down(self, host):
        with self.lock:
            gap = max(self.delay, self.min_gap.get(host, 0.0))
            self.min_gap[host] = gap = min(max(gap * 2, 1.0), 60.0)
        log.info("%s 返回 429，降速到每 %.0f 秒一个请求", host, gap)

    def wait(self, host, stop):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot.get(host, 0.0))
            self.next_slot[host] = slot + max(self.delay, self.min_gap.get(host, 0.0))
        if slot > now:
            stop.wait(slot - now)


class Robots:
    """robots.txt 缓存。规则与 urllib.robotparser 一致：401/403 视为全站禁止，其余 4xx/5xx 视为允许。"""

    def __init__(self, crawler):
        self.c = crawler
        self.cache = {}
        self.lock = threading.Lock()

    def allowed(self, url):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self.lock:
            rp = self.cache.get(origin)
            if rp is None:
                rp = self.cache[origin] = self._load(origin, p.netloc)
        return rp.can_fetch(BOT_NAME, url)

    def sitemaps(self, origin):
        """robots.txt 里用 Sitemap: 声明的地址（没有就是空列表）。"""
        self.allowed(origin + "/")
        return list(self.cache[origin].site_maps() or [])

    def _load(self, origin, host):
        rp = RobotFileParser(origin + "/robots.txt")
        self.c.throttle.wait(host, self.c.stop)
        try:
            r = self.c.session.get(origin + "/robots.txt", timeout=min(15.0, self.c.a.timeout))
        except requests.RequestException as e:
            log.debug("robots.txt 获取失败，按允许处理：%s（%s）", origin, short(e))
            rp.allow_all = True
            return rp
        if r.status_code in (401, 403):
            log.warning("%s/robots.txt 返回 %d，按规范视为全站禁止抓取", origin, r.status_code)
            rp.disallow_all = True
        elif r.status_code >= 400:
            rp.allow_all = True
        else:
            rp.parse(r.text.splitlines())
            delay = rp.crawl_delay(BOT_NAME)
            if delay:
                self.c.throttle.set_min(host, min(float(delay), 60.0))
                log.info("%s 要求 Crawl-delay=%s 秒，已遵守", host, delay)
        return rp


class State:
    """SQLite 记录每个 URL 的状态，用于断点续爬、去重和导出清单。只在主线程访问。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS urls(
        url TEXT PRIMARY KEY, kind TEXT NOT NULL, depth INTEGER NOT NULL,
        referer TEXT, folder TEXT DEFAULT '', anchor TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', note TEXT, path TEXT,
        size INTEGER, sha256 TEXT, title TEXT, updated REAL);
    CREATE INDEX IF NOT EXISTS urls_status ON urls(status);
    CREATE INDEX IF NOT EXISTS urls_sha ON urls(sha256);
    """

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(self.SCHEMA)

    def add(self, t):
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO urls(url,kind,depth,referer,folder,anchor,updated) VALUES(?,?,?,?,?,?,?)",
            (t.url, t.kind, t.depth, t.referer, t.folder, t.anchor, time.time()),
        )
        return cur.rowcount == 1

    def add_seed(self, t):
        # 起始网页每次都重新抓（课程页可能新增了资料）；起始文件下载过就不再下
        self.conn.execute(
            "INSERT INTO urls(url,kind,depth,updated) VALUES(?,?,0,?) "
            "ON CONFLICT(url) DO UPDATE SET status='pending', depth=0 WHERE excluded.kind='page'",
            (t.url, t.kind, time.time()),
        )

    def requeue(self, t):
        """网页界面勾选下载：没有就新增，有就重新排队，来源信息以这次为准。"""
        self.conn.execute(
            "INSERT INTO urls(url,kind,depth,referer,folder,anchor,updated) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET status='pending', kind=excluded.kind, referer=excluded.referer, "
            "folder=excluded.folder, anchor=excluded.anchor",
            (t.url, t.kind, t.depth, t.referer, t.folder, t.anchor, time.time()),
        )

    def lookup(self, url):
        return self.conn.execute("SELECT status,path,size FROM urls WHERE url=?", (url,)).fetchone()

    def reset(self, where):
        return self.conn.execute(f"UPDATE urls SET status='pending' WHERE {where}").rowcount

    def pending(self):
        rows = self.conn.execute(
            "SELECT url,kind,depth,referer,folder,anchor FROM urls WHERE status='pending' ORDER BY rowid"
        )
        return [Task(*row) for row in rows]

    def finish(self, res):
        self.conn.execute(
            "UPDATE urls SET status=?,note=?,path=?,size=?,sha256=?,title=?,updated=? WHERE url=?",
            (res.status, res.note, res.path, res.size, res.sha256, res.title, time.time(), res.task.url),
        )

    def path_by_sha(self, sha, url):
        row = self.conn.execute(
            "SELECT path FROM urls WHERE sha256=? AND url<>? AND status='done' LIMIT 1", (sha, url)
        ).fetchone()
        return row[0] if row else None

    def count(self, where):
        return self.conn.execute(f"SELECT COUNT(*) FROM urls WHERE {where}").fetchone()[0]

    def export(self, manifest, failed):
        rows = self.conn.execute(
            "SELECT status,path,size,sha256,url,referer,note FROM urls "
            "WHERE sha256 IS NOT NULL OR (kind='file' AND status<>'done') ORDER BY path, url"
        ).fetchall()
        with open(manifest, "w", newline="", encoding="utf-8-sig") as f:  # 带 BOM，Excel 打开中文不乱码
            w = csv.writer(f)
            w.writerow(["状态", "文件", "大小(字节)", "SHA256", "URL", "来源页面", "备注"])
            w.writerows(rows)
        bad = self.conn.execute("SELECT url,note FROM urls WHERE status='failed' ORDER BY rowid").fetchall()
        if bad:
            failed.write_text("".join(f"{u}\t{n}\n" for u, n in bad), encoding="utf-8")
        elif failed.exists():
            failed.unlink()


# ───────────────────────── 爬虫主体 ─────────────────────────

class Crawler:
    def __init__(self, a, seeds, renderer=None):
        self.a = a
        self.out = Path(a.out).expanduser().resolve()
        self.parts = self.out / ".parts"
        self.stop = threading.Event()
        self.exts = parse_exts(a.ext)
        self.include = [re.compile(p, re.I) for p in a.include]
        self.exclude = [re.compile(p, re.I) for p in a.exclude]
        self.follow = [re.compile(p, re.I) for p in a.follow]
        self.seeds = seeds
        self.seed_hosts = {urlsplit(u).hostname for u in seeds}
        self.allow_domains = [d.lower().strip().lstrip(".") for d in a.allow_domain]
        self.prefixes = [u if u.endswith("/") else u.rsplit("/", 1)[0] + "/"
                         for u in (s.split("#")[0].split("?")[0] for s in seeds)]
        self.custom_headers = dict(a.header)
        self.throttle = HostThrottle(a.delay)
        self.login_stamp = self._login_stamp()
        self.login_state = self._read_login()
        self.sniffed = self._read_tokens()  # 渲染时嗅探到的 主机 → {Authorization: …}，之后访问同一主机时带上
        self.token_lock = threading.Lock()
        self.pause_on_auth = True  # 登录失效时暂停整批（False：只记失败，用于刚重新登录过还失败的情况）
        self.auth_lost = ""
        self.session = self._make_session()
        self.robots = Robots(self)
        self.file_rx = build_file_regex(self.exts)
        self.want_resources = bool(self.exts and self.exts & RESOURCE_EXTS)
        self.js = getattr(a, "js", "off")
        self.renderer, self.own_renderer = renderer, False
        if self.js != "off" and renderer is None:
            from js_render import Renderer

            self.renderer, self.own_renderer = Renderer(), True
        self.path_lock = threading.Lock()
        self.reserved = set()
        self.pages_q, self.files_q = deque(), deque()
        self.stats = Counter()
        self.found = []
        self.db = None
        self.hinted = set()  # JS 渲染时确认是下载的地址（触发了下载 / 接口里叫 downloadUrl），没扩展名也按文件处理
        self.page_hops = {}  # 网页 → 沿着翻页链接连续翻了几页（没算层的那几页）
        self.thread_prefix = "w"  # 工作线程名前缀；网页界面靠它把日志归到各自的任务
        self.log_file = "crawl.log"

    # ── 会话 / 请求头 ──

    def _make_session(self):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=max(10, self.a.workers * 2))
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({"User-Agent": self.a.user_agent, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        if self.a.cookies:
            jar = MozillaCookieJar(self.a.cookies)
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (LoadError, OSError) as e:
                sys.exit(f"读取 cookies 文件失败：{e}\n需要 Netscape 格式（浏览器扩展“Get cookies.txt LOCALLY”导出的就是）")
            for c in jar:
                if not c.expires:  # 导出工具把会话 cookie 的过期时间写成 0，cookiejar 会当成已过期不发送
                    c.expires, c.discard = None, True
                s.cookies.set_cookie(c)
            log.debug("已加载 %d 个 cookie", len(jar))
        for pair in filter(None, (p.strip() for p in (self.a.cookie or "").split(";"))):
            k, _, v = pair.partition("=")
            for host in self.seed_hosts:  # 只发给起始站点，不泄露给其他域名
                s.cookies.set(k.strip(), v.strip(), domain=host, path="/")
        for c in (self.login_state or {}).get("cookies", []):  # cookie 自带域名，只发给对应站点
            exp = c.get("expires") or -1
            s.cookies.set_cookie(requests.cookies.create_cookie(
                c["name"], c.get("value", ""), domain=c.get("domain", ""), path=c.get("path") or "/",
                secure=bool(c.get("secure")), expires=int(exp) if exp > 0 else None,
                rest={"HttpOnly": None} if c.get("httpOnly") else {}))
        if self.a.proxy:
            s.proxies = {"http": self.a.proxy, "https": self.a.proxy}
        if self.a.insecure:
            s.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return s

    def _login_files(self):
        explicit = getattr(self.a, "login_state", None)
        return [Path(explicit).expanduser()] if explicit else list(dict.fromkeys(login_state_path(u) for u in self.seeds))

    def _login_stamp(self):
        return tuple(f.stat().st_mtime if f.is_file() else 0.0 for f in self._login_files())

    def refresh_login(self):
        """网页界面里重新登录过：换上新的登录状态和令牌（之后新建的会话生效）。返回是否有变化。"""
        stamp = self._login_stamp()
        if stamp == self.login_stamp:
            return False
        self.login_stamp = stamp
        self.login_state = self._read_login()
        self.sniffed = self._read_tokens()
        return True

    def _has_auth(self):
        return bool(self.login_state or self.a.cookies or self.a.cookie or self.custom_headers
                    or any(self.sniffed.values()))

    def _read_tokens(self):
        """上次随登录状态存下的 Authorization 等请求头。没有登录状态就不用（退出登录 = 令牌一起作废）。"""
        if not self.login_state:
            return {}
        out, newest = {}, 0.0
        for f in dict.fromkeys(token_path(u) for u in self.seeds):
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
                newest = max(newest, f.stat().st_mtime)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                log.warning("令牌文件读取失败，忽略：%s（%s）", f, short(e, 80))
                continue
            for host, hs in (st.get("hosts") or {}).items():
                if isinstance(hs, dict):
                    out.setdefault(host, {}).update({str(k): str(v) for k, v in hs.items()})
        if out:
            names = sorted({k for hs in out.values() for k in hs})
            log.info("使用保存的 %s 请求头（%s前嗅探到）", "、".join(names), ago(time.time() - newest))
        return out

    def _save_tokens(self, page_url):
        """有登录状态时才落盘：令牌和登录状态同生共死，退出登录一起删。"""
        if not self.login_state:
            return
        host = urlsplit(page_url).hostname
        key = page_url if host in self.seed_hosts else self.seeds[0]
        path = token_path(key)
        with self.token_lock:
            try:
                hosts = json.loads(path.read_text(encoding="utf-8")).get("hosts") or {}
            except (OSError, ValueError):
                hosts = {}
            for h, hs in self.sniffed.items():
                hosts.setdefault(h, {}).update(hs)
            write_private(path, {"saved": time.time(), "hosts": hosts})

    def _read_login(self):
        """读保存的浏览器登录状态（--login 存的，或 --login-state 指定的）。cookie 给 requests，整份给 JS 渲染。"""
        explicit = getattr(self.a, "login_state", None)
        files = self._login_files()
        merged = {"cookies": [], "origins": []}
        for f in files:
            if not f.is_file():
                if explicit:
                    log.warning("找不到登录状态文件：%s", f)
                continue
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("登录状态文件读取失败，忽略：%s（%s）", f, short(e, 80))
                continue
            merged["cookies"] += st.get("cookies", [])
            merged["origins"] += st.get("origins", [])
            log.info("使用保存的登录状态：%s（%d 个 cookie，%s前保存）", f.name, len(st.get("cookies", [])),
                     ago(time.time() - f.stat().st_mtime))
        return merged if merged["cookies"] or merged["origins"] else None

    def _host_allowed(self, host):
        return host in self.seed_hosts or any(host == d or host.endswith("." + d) for d in self.allow_domains)

    def _headers(self, t):
        h = {}
        if t.referer:  # 同浏览器默认策略：跨站只带来源站点，不带完整路径
            r, u = urlsplit(t.referer), urlsplit(t.url)
            h["Referer"] = t.referer if (r.scheme, r.netloc) == (u.scheme, u.netloc) else f"{r.scheme}://{r.netloc}/"
        if t.kind != "page":
            h["Accept-Encoding"] = "identity"  # 二进制文件不要压缩，长度校验和断点续传才准确
        if self.custom_headers and self._host_allowed(urlsplit(t.url).hostname or ""):
            h.update(self.custom_headers)
        h.update(self.sniffed.get(urlsplit(t.url).netloc, {}))
        return h

    # ── 范围 / 过滤 ──

    def in_page_scope(self, url):
        host = urlsplit(url).hostname or ""
        if host in self.seed_hosts:
            return self.a.scope == "host" or any(url.startswith(p) for p in self.prefixes)
        return any(host == d or host.endswith("." + d) for d in self.allow_domains)

    def in_file_scope(self, url):
        return self.in_page_scope(url) if self.a.strict_files else True

    def want_ext(self, ext):
        return self.exts is None or ext in self.exts

    def want_name(self, t, name):
        if any(rx.search(name) for rx in self.exclude):
            return False
        hay = f"{unquote(t.url)} {t.anchor} {name}"
        return not self.include or any(rx.search(hay) for rx in self.include)

    def classify(self, url, anchor, depth):
        plain = unquote(url)
        if any(rx.search(plain) for rx in self.exclude):
            return None
        ext = url_ext(url)
        if ext not in PAGE_EXTS:
            if not self.want_ext(ext) or not self.in_file_scope(url):
                return None
            if self.include and not any(rx.search(plain) or rx.search(anchor) for rx in self.include):
                return None
            return "file"
        if url in self.hinted and self.in_file_scope(url):
            return "probe"
        if (depth <= self.a.depth and self.in_page_scope(url)
                and (not self.follow or any(rx.search(plain) for rx in self.follow))):
            return "page"
        anchor_has_ext = self.exts and name_ext(anchor.strip()) in self.exts
        if self.in_file_scope(url) and (DOWNLOAD_HINT.search(plain) or DOWNLOAD_HINT.search(anchor) or anchor_has_ext):
            return "probe"
        return None

    # ── 工作线程 ──

    def process(self, t):
        try:
            for attempt in range(self.a.retries + 1):
                if self.stop.is_set():
                    return Result(t, "interrupted")
                try:
                    return self._fetch_once(t)
                except AuthLost as e:
                    return self._auth_result(t, str(e))
                except Retryable as e:
                    if attempt >= self.a.retries:
                        return Result(t, "failed", f"重试 {self.a.retries} 次仍失败：{e}")
                    delay = e.wait if e.wait is not None else min(60.0, 2.0 * 2 ** attempt)
                    log.info("… %s：%s，%.0f 秒后第 %d 次重试", t.url, e, delay, attempt + 1)
                    self.stop.wait(delay)
            return Result(t, "failed", "未知错误")
        except Exception as e:  # 兜底：单个任务出错不影响整体
            log.debug("任务异常 %s", t.url, exc_info=True)
            return Result(t, "failed", f"{type(e).__name__}: {short(e)}")

    def _auth_result(self, t, why):
        """登录失效：有登录状态就暂停整批等重新登录（任务保持待处理，.part 保留）；本来就没登录，只记失败。"""
        if not self._has_auth():
            return Result(t, "failed", f"需要登录：{why}（先用 --login 或网页里的「在浏览器里登录」）")
        if self.pause_on_auth and not self.a.dry_run:
            return Result(t, "auth", f"登录已失效：{why}")
        return Result(t, "failed", f"登录已失效：{why}" + ("" if self.a.dry_run else "（重新登录后仍然这样，可能是没有这个文件的权限）"))

    @staticmethod
    def _drop_part(part):
        part.unlink(missing_ok=True)
        part.with_suffix(".meta").unlink(missing_ok=True)

    def _fetch_once(self, t):
        if not self.robots.allowed(t.url):
            return Result(t, "skipped", "robots.txt 禁止抓取")
        if self.a.dry_run and t.kind == "file":
            if getattr(self.a, "probe_size", False):
                return self._head(t)
            return Result(t, "found", path=safe_name(unquote(urlsplit(t.url).path.rsplit("/", 1)[-1])))
        headers = self._headers(t)
        part = self.parts / (sha1(t.url)[:20] + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if offset:
            headers["Range"] = f"bytes={offset}-"
            # If-Range：服务器上的文件变了就整个重发（200），不会把新旧两个版本拼在一起。弱 ETag 不能用于 Range
            meta = self._part_meta(part)
            etag = meta.get("etag", "")
            validator = etag if etag and not etag.startswith("W/") else meta.get("modified", "")
            if validator:
                headers["If-Range"] = validator
        host = urlsplit(t.url).netloc
        self.throttle.wait(host, self.stop)
        if self.stop.is_set():
            return Result(t, "interrupted")
        try:
            r = self.session.get(t.url, headers=headers, stream=True, timeout=(min(10.0, self.a.timeout), self.a.timeout))
            with r:
                if r.status_code == 416 and offset:
                    self._drop_part(part)
                    raise Retryable("续传位置失效，从头下载", wait=0)
                denied = self._check_denied(r) if r.status_code in (401, 403) else ""
                if r.status_code in RETRY_STATUS:
                    if r.status_code == 429:
                        self.throttle.slow_down(host)
                    raise Retryable(f"HTTP {r.status_code}", retry_after(r))
                if r.status_code >= 400:
                    return Result(t, "failed", f"HTTP {r.status_code}{denied}")
                ctype = r.headers.get("Content-Type", "")
                cd_name = filename_from_cd(r.headers.get("Content-Disposition"))
                if not cd_name and (mime_of(ctype) in ("text/html", "application/xhtml+xml") or (not ctype and t.kind == "page")):
                    return self._handle_page(t, r, ctype)
                return self._handle_file(t, r, ctype, cd_name, part, offset)
        except requests.exceptions.SSLError as e:
            return Result(t, "failed", f"SSL 错误（校内自签证书可加 --insecure）：{short(e)}")
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError) as e:
            raise Retryable(f"{type(e).__name__}: {short(e, 80)}") from None

    @staticmethod
    def _check_denied(r):
        """401 一律算登录失效；403 只有提示里说是登录问题才算（否则是没权限 / 防盗链）。返回附在失败原因后面的提示。"""
        msg, auth = "", r.status_code == 401
        try:
            body = read_head(r.iter_content(4096), 16 * 1024)
        except requests.RequestException:
            body = b""
        if body.lstrip()[:1] in (b"{", b"["):
            try:
                err = api_error(json.loads(body.decode("utf-8-sig", "replace")))
            except ValueError:
                err = None
            if err:
                msg, auth = err[0], auth or err[1]
        if auth:
            raise AuthLost(f"HTTP {r.status_code}" + (f"，{msg}" if msg else ""))
        return f"：{msg[:80]}" if msg else ""

    def _part_meta(self, part):
        try:
            return json.loads(part.with_suffix(".meta").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _handle_page(self, t, r, ctype):
        if t.kind != "page" or t.depth > self.a.depth:
            # 要的是文件却回了网页：看看是不是被踢回了登录页
            head = read_head(r.iter_content(1 << 16), 1 << 16).decode(charset_of(ctype) or "utf-8", "replace")
            if looks_login(head, r.url if r.history else ""):
                raise AuthLost(f"下载地址跳到了登录页 {r.url}" if r.history else "下载地址返回的是登录页")
            if t.kind == "file":
                log.warning("⚠ 期望文件却拿到网页（链接可能失效，或需要登录）：%s", t.url)
                return Result(t, "failed", "响应是网页而不是文件（链接可能失效，或需要登录）")
            return Result(t, "skipped", "响应是网页")
        if t.depth == 0 and r.url != t.url:  # 起始页跳转到别的主机 / 目录（如 http→https、加 www），范围跟着走
            final = urlsplit(r.url)
            if final.hostname not in self.seed_hosts:
                log.info("起始页重定向到 %s，加入爬取范围", final.hostname)
                self.seed_hosts.add(final.hostname)
            base = r.url.split("#")[0]
            self.prefixes.append(base if base.endswith("/") else base.split("?")[0].rsplit("/", 1)[0] + "/")
        elif not self.in_page_scope(r.url):
            return Result(t, "skipped", f"重定向到范围外：{r.url}")
        body = bytearray()
        for chunk in r.iter_content(1 << 16):
            body += chunk
            if len(body) > self.a.max_page_bytes:
                log.debug("网页超过 %s，只解析前面部分：%s", human(self.a.max_page_bytes), t.url)
                break
        body = bytes(body)
        if r.history and looks_login("", r.url) and LOGIN_FORM.search(body[:1 << 16].decode("utf-8", "replace")):
            raise AuthLost(f"网页跳到了登录页 {r.url}")  # 统一身份认证把人踢回去了：解析登录页没有意义
        title, links, soup = self._parse_page(body, charset_of(ctype), r.url)
        rendered = ""
        route = urlsplit(t.url).fragment  # #/course/42：服务器只看得到空壳，必须渲染
        if self.renderer and (self.js == "on" or route or self._looks_dynamic(soup, links)):
            r_title, r_links = self._render_page(f"{r.url.split('#')[0]}#{route}" if route else r.url)
            title = (r_title or title) if route else (title or r_title)
            before = len(links)
            for u, anchor in r_links.items():
                if better_anchor(links.get(u), anchor):
                    links[u] = anchor
            rendered = f"，JS 渲染后多出 {len(links) - before} 个"
        if self.a.save_pages:
            self._save_page(r.url, body)
        log.info("◆ [%d] %s  %s（%d 个链接%s）", t.depth, title[:40] or "(无标题)", t.url, len(links), rendered)
        return Result(t, "done", title=title, links=list(links.items()))

    def _parse_page(self, body, encoding, url):
        """返回 (标题, {链接: 链接文字}, soup)。"""
        soup = BeautifulSoup(body, HTML_PARSER, from_encoding=encoding)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        base_tag = soup.find("base", href=True)
        base = urljoin(url, base_tag["href"]) if base_tag else url

        raw = []
        for tag, attr in LINK_ATTRS + RESOURCE_ATTRS if self.want_resources else LINK_ATTRS:
            for el in soup.find_all(tag):
                if el.get(attr):
                    anchor = el.get_text(" ", strip=True) or el.get("title", "") if tag in ("a", "area") else ""
                    raw.append((el[attr], anchor[:200]))
        for m in soup.find_all("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)}):
            mm = re.search(r"url\s*=\s*['\"]?([^'\";]+)", m.get("content", ""), re.I)
            if mm:
                raw.append((mm.group(1), ""))
        if self.file_rx:  # onclick / <script> / data-* 里藏着的文件地址
            text = body.decode(soup.original_encoding or "utf-8", "replace")
            raw += [(html.unescape(m.group(1)), "") for m in self.file_rx.finditer(text)]

        links = {}
        for href, anchor in raw:
            self._add_link(links, href, anchor, base)
        return title, links, soup

    def _add_link(self, links, href, anchor, base):
        u = normalize_url(href, base)
        if not u:
            return None
        if better_anchor(links.get(u), anchor):  # 页码“2”和“下一页”指向同一页时，记成“下一页”
            links[u] = anchor
        # 预览器 / 跳转页参数里的文件：viewer.html?file=/x.pdf（'*' 时只认常见资料类型）
        q = urlsplit(u).query
        if q:
            for vals in parse_qs(q).values():
                for v in vals:
                    if url_ext(v) in (self.exts or DEFAULT_EXTS):
                        fu = normalize_url(v, u)
                        if fu:
                            links.setdefault(fu, anchor)
        return u

    # ── JS 动态页面 ──

    @staticmethod
    def _looks_dynamic(soup, links):
        """--js auto 的判断：有脚本，并且正文 / 链接少得可疑、挂载点是空的、脚本在拉数据，
        或者有没有真实地址的“加载更多 / 下一页”按钮（后面的列表只能点出来）。"""
        scripts = soup.find_all("script")
        if not scripts:
            return False
        text = soup.body.get_text(" ", strip=True) if soup.body else ""
        mount = soup.find(id=SPA_MOUNT)
        return (len(links) < 5 or len(text) < 200
                or (mount is not None and len(mount.get_text(strip=True)) < 50)
                or any(SPA_SCRIPT.search(s.get("src", "")) for s in scripts)
                or any(not s.get("src") and AJAX_HINT.search(s.string or "") for s in scripts)
                or any(PAGER_TEXT.fullmatch(el.get_text(" ", strip=True)) for el in soup.find_all(["button", "a"])
                       if el.name == "button" or not normalize_url(el.get("href", ""), "https://x/")))

    def _browser_cookies(self):
        out = []
        for c in self.session.cookies:
            if not c.domain:
                continue
            d = {"name": c.name, "value": c.value or "", "domain": c.domain, "path": c.path or "/",
                 "secure": bool(c.secure), "httpOnly": c.has_nonstandard_attr("HttpOnly")}
            if c.expires:
                d["expires"] = float(c.expires)
            out.append(d)
        return out

    def _render_page(self, url):
        """用无头浏览器再打开一次，收集 JS 生成的链接、接口 JSON 里的文件、网络请求里的文件。返回 (标题, 链接)。"""
        from js_render import RenderError

        self.throttle.wait(urlsplit(url).netloc, self.stop)
        hosts = {h for h in self.seed_hosts if h} | set(self.allow_domains)
        try:
            rr = self.renderer.render(
                url, cookies=self._browser_cookies(), headers=self.custom_headers, header_hosts=hosts,
                user_agent=self.a.user_agent, proxy=self.a.proxy, insecure=self.a.insecure,
                timeout=self.a.timeout, click=getattr(self.a, "js_click", False), stop=self.stop,
                storage_state=self.login_state)
        except RenderError as e:
            log.warning("JS 渲染失败，只用静态解析的结果：%s（%s）", url, e)
            return "", {}
        links = {}
        if rr.html:
            _, links, _ = self._parse_page(rr.html.encode("utf-8"), "utf-8", rr.url)
        for href, anchor in rr.links:
            self._add_link(links, href, anchor, rr.url)
        sure = []  # 确定是下载的
        for href, name in rr.downloads:  # blob: 下载拿不到地址，但触发它的请求在 network 里
            if not href.startswith("blob:"):
                sure.append(self._add_link(links, href, name, rr.url))
        for href in rr.opened:
            sure.append(self._add_link(links, href, "", rr.url))
        for href, method, status, _rtype, ctype, cd in rr.network:
            if method != "GET" or status >= 400:
                continue
            name = filename_from_cd(cd) or ""
            mime = mime_of(ctype)
            m_ext = MIME_EXT.get(mime, "") if mime not in ("text/plain", "text/csv") else ""
            if name:  # 接口直接回了附件
                sure.append(self._add_link(links, href, name, rr.url))
            elif self.exts is not None and url_ext(href) in self.exts:
                self._add_link(links, href, "", rr.url)
            elif m_ext and self.want_ext(m_ext):  # /api/file/9 回的是 PDF：拼个文件名，让它按疑似下载链接处理
                stem = unquote(urlsplit(href).path.rstrip("/").rsplit("/", 1)[-1]) or "file"
                self._add_link(links, href, f"{stem}.{m_ext}", rr.url)
        per_api = Counter()
        for src, text in rr.json:
            for href, anchor, is_dl in self._json_links(text):
                u = self._add_link(links, href, anchor, src)
                per_api[src] += 1
                if is_dl:
                    sure.append(u)
        self.hinted.update(u for u in sure if u)
        changed = False
        for host, hs in rr.auth.items():
            new = {k: v for k, v in hs.items() if self.sniffed.get(host, {}).get(k) != v}
            if new:
                self.sniffed.setdefault(host, {}).update(new)
                self._on_sniffed(host, sorted(new))
                changed = True
        if changed:
            self._save_tokens(url)
        apis = [{"method": m, "url": u, "status": st, "type": mime_of(ct), "links": per_api.get(u, 0),
                 "attachment": filename_from_cd(cd) or ""}
                for u, m, st, rtype, ct, cd in rr.network if rtype in ("xhr", "fetch")]
        if apis:
            self._on_apis(url, apis)
        if rr.expanded:
            log.info("  翻页 / 加载更多 %d 次：%s", rr.expanded, url)
        if rr.clicked:
            log.info("  点击了 %d 个下载按钮：%s", rr.clicked, url)
        return rr.title, links

    def _json_links(self, text):
        """接口 JSON 里的文件：url / downloadUrl 之类的键，或者值本身就是带目标扩展名的路径；同级的 name / title 当链接文字。
        返回 [(地址, 文字, 键名是否表明这是下载)]。"""
        import json

        try:
            data = json.loads(text)
        except ValueError:  # JSONP 之类，退回正则
            text = text.replace("\\/", "/")
            return [(html.unescape(m.group(1)), "", False) for m in self.file_rx.finditer(text)] if self.file_rx else []
        out = []

        exts = self.exts or DEFAULT_EXTS  # '*' 时只认常见资料类型，不然版本号之类的 "1.0" 也会被当成文件

        def looks_file(v):
            return len(v) < 2000 and not v.startswith("data:") and url_ext(v.split("?")[0]) in exts

        def walk(obj, depth):
            if depth > 30:
                return
            if isinstance(obj, dict):
                label = next((v for k, v in obj.items() if isinstance(v, str) and k.lower() in JSON_NAME_KEYS), "")
                for k, v in obj.items():
                    key = k.lower()
                    if isinstance(v, str):
                        if key in JSON_NAME_KEYS:
                            continue
                        if (("/" in v or "." in v) and key in JSON_URL_KEYS and not v.startswith("#")
                                or "/" in v and looks_file(v)):
                            out.append((v, label[:200], bool(re.search(r"download|attach", key))))
                    else:
                        walk(v, depth + 1)
            elif isinstance(obj, list):
                for v in obj:
                    if isinstance(v, str) and "/" in v and looks_file(v):
                        out.append((v, "", False))
                    else:
                        walk(v, depth + 1)

        walk(data, 0)
        return out

    def _pick_name(self, t, r, ctype, cd_name):
        if cd_name:
            return safe_name(cd_name)
        for u in (t.url, r.url):  # 优先原始链接，其次重定向后的地址
            if url_ext(u) not in PAGE_EXTS:
                return safe_name(unquote(urlsplit(u).path.rsplit("/", 1)[-1]))
        guessed = ext_from_mime(ctype)
        anchor = re.sub(r"\s+", " ", t.anchor).strip()
        a_ext = name_ext(anchor)
        if a_ext and (a_ext == guessed or (self.exts and a_ext in self.exts)):
            return safe_name(anchor)  # 链接文字本身就是文件名，如“第3章 树.pdf”
        if anchor and len(anchor) <= 60 and not MEANINGLESS_ANCHOR.fullmatch(anchor):
            stem = anchor
        else:
            p = urlsplit(t.url)
            stem = unquote(p.path.rsplit("/", 1)[-1]).rsplit(".", 1)[0] or "file"
            if p.query:
                stem += "_" + sha1(p.query)[:8]
        return safe_name(f"{stem}.{guessed}" if guessed else stem)

    def _target_dir(self, t):
        if self.a.layout == "flat":
            return self.out
        src = urlsplit(t.url)
        if self.a.layout == "site":
            segs = [safe_name(unquote(s)) for s in src.path.split("/")[1:-1] if s]
            return self.out.joinpath(safe_name(src.netloc), *segs)
        ref = urlsplit(t.referer) if t.referer else src
        d = self.out / safe_name(ref.netloc)
        return d / safe_name(t.folder[:80], max_bytes=150) if t.folder else d

    def _place(self, t, name, sha, size):
        """返回 (目标路径, 是否已存在同内容文件)。同名不同内容时自动加 (1)(2)…"""
        d = self._target_dir(t)
        stem, suffix = (name.rsplit(".", 1)[0], "." + name.rsplit(".", 1)[1]) if "." in name else (name, "")
        with self.path_lock:
            d.mkdir(parents=True, exist_ok=True)
            i = 0
            while True:
                cand = d / (name if i == 0 else f"{stem} ({i}){suffix}")
                i += 1
                if cand in self.reserved:
                    continue
                if not cand.exists():
                    self.reserved.add(cand)
                    return cand, False
                if cand.stat().st_size == size and sha256_file(cand) == sha:
                    return cand, True

    def _handle_file(self, t, r, ctype, cd_name, part, offset):
        name = self._pick_name(t, r, ctype, cd_name)
        ext = name_ext(name)
        if not self.want_ext(ext):
            return Result(t, "skipped", f"不需要的类型：{ext or mime_of(ctype) or '未知'}")
        if not self.want_name(t, name):
            return Result(t, "skipped", "未通过 --include/--exclude 过滤")
        encoded = r.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity")
        meta = self._part_meta(part) if offset else {}
        etag, modified = r.headers.get("ETag", ""), r.headers.get("Last-Modified", "")
        if r.status_code == 206:
            m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", r.headers.get("Content-Range", ""))
            if not m or int(m.group(1)) != offset:
                self._drop_part(part)
                raise Retryable("服务器返回的续传区间不对，从头下载", wait=0)
            total = int(m.group(2)) if m.group(2) != "*" else None
            # 有的服务器不认 If-Range，照样回 206：自己再对一次 ETag 和总大小
            if (meta.get("etag") and etag and etag != meta["etag"]) or (meta.get("total") and total and total != meta["total"]):
                self._drop_part(part)
                raise Retryable("服务器上的文件已经更新，从头下载", wait=0)
        else:
            if offset:
                log.info("↻ %s：服务器上的文件已更新（或不支持续传），从头下载", name)
            offset = 0  # 服务器不支持 Range、没发 Range，或 If-Range 判定文件变了
            cl = r.headers.get("Content-Length", "")
            total = int(cl) if cl.isdigit() and not encoded else None
        if self.a.max_size and total and total > self.a.max_size:
            return Result(t, "skipped", f"文件过大：{human(total)}")
        if self.a.dry_run:
            return Result(t, "found", path=name, size=total or 0)

        chunks = r.iter_content(1 << 16)
        if offset:  # 续传：校验已经下到的开头（旧版本留下的 .part 可能是错误页）
            with open(part, "rb") as f:
                head, first = f.read(HEAD_BYTES), b""
        else:  # 先看开头确认是文件，再动 .part：不能拿错误提示覆盖掉之前下了一半的内容
            head = first = read_head(chunks)
        problem = self._check_content(t, r, name, cd_name, head, None if offset else total)
        if problem:
            if offset:
                self._drop_part(part)
                raise Retryable(f"已下载的部分不对（{problem}），从头下载", wait=0)
            log.warning("✗ %s：%s", t.url, problem)
            return Result(t, "failed", problem)

        h = hashlib.sha256()
        if offset:
            with open(part, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
            log.info("↻ 续传 %s（已有 %s / %s）", name, human(offset), human(total) if total else "?")
        else:
            log.info("↓ %s%s", name, f"（{human(total)}）" if total else "")
        self.parts.mkdir(parents=True, exist_ok=True)
        if not offset:  # 记下这个版本的 ETag / Last-Modified，下次续传用 If-Range 确认没变
            part.with_suffix(".meta").write_text(json.dumps(
                {"url": t.url, "etag": etag, "modified": modified, "total": total}), encoding="utf-8")
        size, t0, too_big = offset, time.monotonic(), False
        with open(part, "ab" if offset else "wb") as f:
            for chunk in _chain(first, chunks):
                if self.stop.is_set():
                    return Result(t, "interrupted")  # 保留 .part，下次续传
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
                self._progress(t, size, total)
                if self.a.max_size and size > self.a.max_size:
                    too_big = True
                    break
        if too_big:
            self._drop_part(part)
            return Result(t, "skipped", "文件过大（超过 --max-size）")
        if total is not None and size < total:
            raise Retryable(f"下载不完整 {human(size)}/{human(total)}")
        sha = h.hexdigest()
        dest, existed = self._place(t, name, sha, size)
        if existed:
            part.unlink(missing_ok=True)
        else:
            os.replace(part, dest)
        part.with_suffix(".meta").unlink(missing_ok=True)
        rel = str(dest.relative_to(self.out))
        dt = max(time.monotonic() - t0, 1e-6)
        log.info("✓ %s  %s  %s/s%s", rel, human(size), human((size - offset) / dt), "（已存在，跳过）" if existed else "")
        return Result(t, "done", "已存在" if existed else "", path=rel, size=size, sha256=sha)

    def _check_content(self, t, r, name, cd_name, head, total):
        """看文件开头，确认下到的是文件本身，不是错误提示 / 登录页。没问题返回 ""，否则返回原因；登录失效直接抛 AuthLost。"""
        ext = name_ext(name)
        want = EXT_FORMAT.get(ext, "")
        got = sniff_format(head)
        if got:
            if want and got != want and {got, want} != {"zip", "ole"}:
                log.debug("%s 的内容实际是 %s 格式", name, got)
            return ""
        if not head:
            return "服务器返回了空文件" if want else ""
        lead = head.lstrip(b"\xef\xbb\xbf \t\r\n")[:1]
        if lead == b"<" and ext not in MARKUP_EXTS and LOOKS_HTML.match(head.lstrip(b"\xef\xbb\xbf")):
            if looks_login(head.decode("utf-8", "replace"), r.url if r.history else ""):
                raise AuthLost("下载地址返回的是登录页")
            return "拿到的是网页而不是文件（链接可能失效了）"
        if lead in (b"{", b"[") and (total is None or len(head) >= total) and len(head) < HEAD_BYTES:
            try:
                obj = json.loads(head.decode("utf-8-sig", "replace"))
            except ValueError:
                obj = None
            if obj is not None:
                declared = name_ext(cd_name or "") or next(
                    (url_ext(u) for u in (t.url, r.url) if url_ext(u) not in PAGE_EXTS), "")
                err = api_error(obj)
                if err and (err[0] or declared not in JSON_EXTS):  # 本来就要 JSON 文件时，得有出错提示才算
                    if err[1]:
                        raise AuthLost(f"接口提示「{err[0][:60] or '未登录'}」")
                    return f"接口返回了错误：{err[0][:80] or '（没有说明）'}"
                if want or declared not in JSON_EXTS | {"txt"}:
                    urls = [u for u, _, _ in self._json_links(head.decode("utf-8", "replace"))]
                    return "接口返回了 JSON 数据而不是文件" + (f"，里面的地址：{urls[0][:120]}" if urls else "")
        if want:
            return f"内容不是有效的 {ext.upper()} 文件（开头是 {head[:8].hex(' ')}）"
        return ""

    def _progress(self, t, done, total):
        """每收到一块数据调用一次（工作线程里）。命令行不用，网页界面拿来画进度条。"""

    def _on_sniffed(self, host, names):
        log.info("  嗅探到发往 %s 的 %s 请求头，之后访问这个主机时自动带上", host, "、".join(names))

    def _on_apis(self, page, apis):
        """渲染时页面调用过的接口（XHR / fetch）。"""
        for a in apis:
            useful = a["links"] or a["attachment"]
            (log.info if useful else log.debug)("  接口 %s %s → HTTP %s%s", a["method"], a["url"], a["status"],
                                                f"，{a['links']} 个链接" if a["links"] else
                                                f"，附件 {a['attachment']}" if a["attachment"] else "")

    def _head(self, t):
        """--dry-run --probe-size：发一个 HEAD 拿大小和真实文件名，不下载。"""
        name = safe_name(unquote(urlsplit(t.url).path.rsplit("/", 1)[-1]))
        self.throttle.wait(urlsplit(t.url).netloc, self.stop)
        try:
            r = self.session.head(t.url, headers=self._headers(t), allow_redirects=True,
                                  timeout=(min(10.0, self.a.timeout), self.a.timeout))
        except requests.RequestException as e:
            return Result(t, "found", f"HEAD 失败：{short(e, 60)}", path=name)
        if r.status_code >= 400:  # 不少服务器不支持 HEAD（405），照样列出来
            return Result(t, "found", f"HEAD 返回 HTTP {r.status_code}", path=name)
        ctype = r.headers.get("Content-Type", "")
        cd_name = filename_from_cd(r.headers.get("Content-Disposition"))
        note = "返回的是网页，可能需要登录" if not cd_name and mime_of(ctype) == "text/html" else ""
        cl = r.headers.get("Content-Length", "")
        return Result(t, "found", note, path=self._pick_name(t, r, ctype, cd_name), size=int(cl) if cl.isdigit() else 0)

    def _save_page(self, url, body):
        p = urlsplit(url)
        segs = [safe_name(unquote(s)) for s in p.path.split("/") if s]
        name = segs.pop() if segs and not p.path.endswith("/") else "index"
        if name_ext(name) in PAGE_EXTS:
            name = name.rsplit(".", 1)[0]
        if p.query:
            name += "_" + sha1(p.query)[:8]
        path = self.out.joinpath(safe_name(p.netloc), "_pages", *segs, safe_name(name) + ".html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    # ── 主线程：调度 / 记账 ──

    def _next_task(self):
        if self.files_q and not (self.a.max_files and self.stats["files"] >= self.a.max_files):
            return self.files_q.popleft()
        if self.pages_q and not (self.a.max_pages and self.stats["pages"] >= self.a.max_pages):
            return self.pages_q.popleft()
        return None

    def _on_result(self, res):
        t = res.task
        if res.status == "interrupted":
            return  # 保持 pending，下次继续
        if res.status == "auth":  # 登录失效：换个文件也一样，整批停下，任务保持 pending、.part 保留
            if not self.auth_lost:
                self.auth_lost = res.note
                log.warning("⏸ %s。已暂停下载，下到一半的文件会保留进度", res.note)
                self.stop.set()
            return
        if res.sha256 and res.status == "done" and not self.a.keep_dups:
            other = self.db.path_by_sha(res.sha256, t.url)
            if other and other != res.path and (self.out / other).exists():
                (self.out / res.path).unlink(missing_ok=True)
                log.info("≡ 内容与 %s 相同，已删除重复的 %s", other, res.path)
                res.status, res.note, res.path = "dup", f"与 {other} 内容相同", other
        self.db.finish(res)

        if res.status == "done":
            if res.sha256 and res.note == "已存在":
                self.stats["existed"] += 1
            elif res.sha256:
                self.stats["files"] += 1
                self.stats["bytes"] += res.size
            else:
                self.stats["pages"] += 1
        elif res.status == "failed":
            log.warning("✗ %s：%s", t.url, res.note)
        elif res.status == "skipped":
            (log.info if "robots" in res.note else log.debug)("- 跳过 %s：%s", t.url, res.note)
        elif res.status == "found":
            self.found.append((t.url, res.path, res.size))
        self.stats[res.status] += 1

        if res.links:
            p = urlsplit(t.url)
            folder = res.title or unquote(p.path.rstrip("/").rsplit("/", 1)[-1]) or p.hostname
            hops = self.page_hops.get(t.url, 0)
            for url, anchor in res.links:
                # 翻页链接翻到的还是同一个列表：沿用这一页的层数，-d 1 也能把列表翻完
                pager = hops < MAX_PAGE_HOPS and is_pager_link(t.url, url, anchor)
                depth = t.depth if pager else t.depth + 1
                kind = self.classify(url, anchor, depth)
                if not kind:
                    continue
                child = Task(url, kind, depth, t.url, folder, anchor)
                if self.db.add(child):
                    if pager and kind == "page":
                        self.page_hops[url] = hops + 1
                    (self.pages_q if kind == "page" else self.files_q).append(child)

    def _seed(self):
        """把起始任务写进状态库，返回本次要处理的任务（默认是库里所有待处理的，即断点续爬）。"""
        if self.a.retry_failed:
            log.info("重置 %d 个失败任务", self.db.reset("status='failed'"))
        if self.a.recrawl:
            log.info("重新抓取 %d 个网页", self.db.reset("kind='page'"))
        for u in self.seeds:
            self.db.add_seed(Task(u, "file" if url_ext(u) not in PAGE_EXTS else "page", 0))
        if getattr(self.a, "sitemap", False):
            try:
                self._seed_sitemaps()
            except KeyboardInterrupt:
                log.warning("收到 Ctrl+C，停止读取 sitemap")
                self.stop.set()
        return self.db.pending()

    # ── sitemap ──

    def _seed_sitemaps(self):
        """--sitemap：读起始站点的 sitemap，范围内的网址当第 1 层加入队列（已经在库里的不重复加）。
        robots.txt 里声明了就用声明的，没有就试 /sitemap.xml；sitemap 索引会接着读下去。"""
        todo, seen, listed, added = deque(), set(), 0, 0
        for origin in dict.fromkeys(f"{p.scheme}://{p.netloc}" for p in map(urlsplit, self.seeds)):
            todo += self.robots.sitemaps(origin) or [origin + "/sitemap.xml"]
        while todo and len(seen) < MAX_SITEMAPS and listed < MAX_SITEMAP_URLS and not self.stop.is_set():
            sm = todo.popleft()
            if sm in seen:
                continue
            seen.add(sm)
            is_index, locs = self._fetch_sitemap(sm)
            if is_index:
                todo += locs
                continue
            for u in locs[:MAX_SITEMAP_URLS - listed]:
                listed += 1
                kind = self.classify(u, "", 1) if self.in_page_scope(u) else None  # 跟网页一样受 --scope 限制
                if kind and self.db.add(Task(u, kind, 1, sm)):
                    added += 1
        if listed >= MAX_SITEMAP_URLS:
            log.warning("sitemap 里的网址太多，只取前 %d 个", MAX_SITEMAP_URLS)
        log.info("sitemap：读了 %d 个，列出 %d 个网址，其中 %d 个在范围内的新网址加入队列", len(seen), listed, added)

    def _fetch_sitemap(self, url):
        """返回 (是否 sitemap 索引, [网址])。认 XML（urlset / sitemapindex）、gzip 压缩、一行一个网址的纯文本。"""
        if not self.robots.allowed(url):
            log.info("- 跳过 sitemap（robots.txt 禁止）：%s", url)
            return False, []
        self.throttle.wait(urlsplit(url).netloc, self.stop)
        try:
            r = self.session.get(url, stream=True, timeout=(min(10.0, self.a.timeout), self.a.timeout))
            with r:
                if r.status_code >= 400:
                    log.info("- 没有 sitemap：%s（HTTP %d）", url, r.status_code)
                    return False, []
                body = read_head(r.iter_content(1 << 16), MAX_SITEMAP_BYTES)
        except requests.RequestException as e:
            log.info("sitemap 获取失败：%s（%s）", url, short(e))
            return False, []
        if body[:2] == b"\x1f\x8b":  # .xml.gz（服务器没按 Content-Encoding 解压的那种）
            try:
                body = zlib.decompressobj(zlib.MAX_WBITS | 16).decompress(body, MAX_SITEMAP_BYTES)
            except zlib.error as e:
                log.info("sitemap 解压失败：%s（%s）", url, e)
                return False, []
        text = body.decode("utf-8-sig", "replace")
        root = SITEMAP_ROOT.search(text[:4096])
        if root:
            raw = [html.unescape(m.group(1)) for m in SITEMAP_LOC.finditer(text)]
        elif not LOOKS_HTML.match(body):  # 纯文本 sitemap：一行一个网址
            raw = [s.strip() for s in text.splitlines() if s.strip().lower().startswith(("http://", "https://"))]
        else:
            log.info("- %s 不是 sitemap（是个网页）", url)
            return False, []
        locs = list(dict.fromkeys(filter(None, (normalize_url(u, url) for u in raw))))
        is_index = bool(root) and root.group(1).lower() == "sitemapindex"
        log.debug("sitemap %s：%d 个%s", url, len(locs), "子 sitemap" if is_index else "网址")
        return is_index, locs

    def run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        fh = None
        if self.log_file:
            fh = logging.FileHandler(self.out / self.log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
            me = threading.get_ident()  # 同一进程里可能有别的爬虫在跑（网页界面），只记自己的
            fh.addFilter(lambda rec: rec.thread == me or rec.threadName.startswith(self.thread_prefix))
            log.addHandler(fh)
        try:
            return self._run()
        finally:
            if fh:
                log.removeHandler(fh)
                fh.close()
            if self.own_renderer:
                self.renderer.close()

    def _run(self):
        db_path = self.out / ".study_crawler.db"
        if self.a.fresh and not self.a.dry_run:
            for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
                p.unlink(missing_ok=True)
            for p in self.parts.glob("*.part"):
                p.unlink()
        self.db = State(":memory:" if self.a.dry_run else db_path)
        with self.db.conn:
            tasks = self._seed()
        for t in tasks:
            (self.pages_q if t.kind == "page" else self.files_q).append(t)
        done_before = self.db.count("status='done'")
        log.info("起始 %d 个 URL，待处理 %d 个%s；输出目录 %s", len(self.seeds), len(self.pages_q) + len(self.files_q),
                 f"（之前已完成 {done_before} 个，自动跳过）" if done_before else "", self.out)
        log.info("文件类型：%s%s", "全部" if self.exts is None else ",".join(sorted(self.exts)),
                 f"；JS 渲染：{self.js}" if self.js != "off" else "")

        t0 = time.monotonic()
        interrupted = False
        ex = ThreadPoolExecutor(max_workers=self.a.workers, thread_name_prefix=self.thread_prefix)
        inflight = {}
        try:
            while not self.stop.is_set():
                while len(inflight) < self.a.workers:
                    t = self._next_task()
                    if t is None:
                        break
                    inflight[ex.submit(self.process, t)] = t
                if not inflight:
                    break
                done, _ = wait(inflight, timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    del inflight[fut]
                    with self.db.conn:
                        self._on_result(fut.result())
        except KeyboardInterrupt:
            interrupted = True
            log.warning("收到 Ctrl+C，正在停止…（再按一次强制退出；没下完的文件下次运行会续传）")
            self.stop.set()
        finally:
            try:
                ex.shutdown(wait=True, cancel_futures=True)
                for fut in inflight:
                    if fut.done() and not fut.cancelled():
                        with self.db.conn:
                            self._on_result(fut.result())
            except KeyboardInterrupt:
                os._exit(130)
            if self.pages_q and self.a.max_pages and self.stats["pages"] >= self.a.max_pages:
                self.stats["capped"] = len(self.pages_q)
                log.warning("已达到最多抓取网页数 %d，还有 %d 个网页排队没抓（调大 --max-pages，0 = 不限）",
                            self.a.max_pages, len(self.pages_q))
            self._report(time.monotonic() - t0)
        if self.auth_lost:
            return 3
        return 130 if interrupted or self.stop.is_set() else 0

    def _report(self, elapsed):
        s = self.stats
        log.info("─" * 50)
        if self.a.dry_run:
            lst = self.out / "found_files.txt"
            lst.write_text("".join(f"{u}\t{n}\n" for u, n, _ in self.found), encoding="utf-8")
            for u, n, size in self.found:
                log.info("  %s%s\n      %s", n, f"  ({human(size)})" if size else "", u)
            log.info("预览：抓取网页 %d 个，发现文件 %d 个（未下载）。列表已写入 %s", s["pages"], len(self.found), lst)
            return
        manifest, failed = self.out / "manifest.csv", self.out / "failed.txt"
        self.db.export(manifest, failed)
        log.info("本次：网页 %d，下载 %d 个文件共 %s，已存在 %d，重复 %d，跳过 %d，失败 %d，用时 %.0f 秒",
                 s["pages"], s["files"], human(s["bytes"]), s["existed"], s["dup"], s["skipped"], s["failed"], elapsed)
        left = self.db.count("status='pending'")
        if left:
            log.info("还有 %d 个任务未完成，再次运行同一条命令即可继续", left)
        if self.auth_lost:
            log.warning("登录已失效：先重新登录（命令行加 --login），再运行同一条命令即可从断点继续")
        if self.db.count("status='failed'"):
            log.info("失败清单：%s（加 --retry-failed 重试）", failed)
        log.info("下载清单：%s", manifest)


# ───────────────────────── 命令行 ─────────────────────────

def header_arg(s):
    k, sep, v = s.partition(":")
    if not sep or not k.strip():
        raise argparse.ArgumentTypeError(f"请求头格式应为 'Name: value'：{s}")
    return k.strip(), v.strip()


def depth_arg(s):
    if s.strip().lower() in ("all", "inf", "全站"):
        return DEPTH_ALL
    try:
        return int(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"深度应为整数或 all：{s}") from None


def regex_arg(s):
    try:
        re.compile(s)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"正则表达式有误：{s}（{e}）")
    return s


def build_parser():
    p = argparse.ArgumentParser(
        prog="study_crawler.py",
        description="学习资料批量下载爬虫：从课程页 / 资料页出发，自动发现并下载 PDF、PPT、Word、压缩包等。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("示例", 1)[1].join(["示例", ""]),
    )
    p.add_argument("urls", nargs="*", help="起始 URL（网页或文件直链），可以多个")
    p.add_argument("-i", "--input-file", help="从文件读取起始 URL，一行一个，# 开头为注释")
    p.add_argument("-o", "--out", default="downloads", help="输出目录（默认 ./downloads）")

    g = p.add_argument_group("爬取范围")
    g.add_argument("-d", "--depth", type=depth_arg, default=1,
                   help="网页跳转深度：0 = 只解析起始页，1 = 再进入起始页链接到的子页面……，all = 不限（默认 1）。"
                        "“下一页”、页码这类翻页链接不算一层")
    g.add_argument("--sitemap", action="store_true",
                   help="读网站的 sitemap（robots.txt 里声明的，没有就试 /sitemap.xml），列出的网址按第 1 层加入")
    g.add_argument("--scope", choices=["host", "prefix"], default="host",
                   help="网页爬取范围：host = 同主机（默认）；prefix = 只爬起始 URL 所在目录及其子目录")
    g.add_argument("--allow-domain", action="append", default=[], metavar="DOMAIN",
                   help="额外允许爬取的域名（含子域名），可重复")
    g.add_argument("--follow", action="append", default=[], type=regex_arg, metavar="REGEX",
                   help="只进入 URL 匹配该正则的网页，可重复")
    g.add_argument("--strict-files", action="store_true",
                   help="文件也必须在爬取范围内（默认允许下载外站文件，例如 CDN 上的 PDF）")
    g.add_argument("--max-pages", type=int, default=0, help="最多抓取多少个网页（0 = 不限）")

    g = p.add_argument_group("文件筛选")
    g.add_argument("--ext", metavar="LIST",
                   help="文件类型：'pdf,pptx' 替换默认列表；'+mp4,mkv' 在默认列表上追加；'*' 下载所有非网页内容。"
                        f"默认：{','.join(sorted(DEFAULT_EXTS))}")
    g.add_argument("--include", action="append", default=[], type=regex_arg, metavar="REGEX",
                   help="只下载 URL / 链接文字 / 文件名匹配该正则的文件（不区分大小写），可重复")
    g.add_argument("--exclude", action="append", default=[], type=regex_arg, metavar="REGEX",
                   help="跳过 URL 或文件名匹配该正则的链接，可重复")
    g.add_argument("--max-size", type=parse_size, metavar="SIZE", help="单个文件大小上限，如 500M、2G")
    g.add_argument("--max-files", type=int, default=0, help="最多下载多少个文件（0 = 不限）")
    g.add_argument("--keep-dups", action="store_true", help="保留内容重复的文件（默认按 SHA-256 去重）")

    g = p.add_argument_group("网络")
    g.add_argument("-w", "--workers", type=int, default=4, help="并发线程数（默认 4）")
    g.add_argument("--delay", type=float, default=1.0,
                   help="同一主机两次请求的最小间隔秒数（默认 1.0；robots.txt 的 Crawl-delay 更大时以它为准）")
    g.add_argument("--timeout", type=float, default=30.0, help="读超时秒数（默认 30）")
    g.add_argument("--retries", type=int, default=3, help="失败重试次数（默认 3）")
    g.add_argument("--cookies", metavar="FILE", help="Netscape 格式 cookies.txt（浏览器扩展导出），用于需要登录的网站")
    g.add_argument("--cookie", metavar="'k=v; k2=v2'", help="直接给 cookie 字符串（只发给起始站点）")
    g.add_argument("-H", "--header", action="append", default=[], type=header_arg, metavar="'Name: value'",
                   help="附加请求头（只发给起始站点和 --allow-domain），可重复")
    g.add_argument("--user-agent", default=DEFAULT_UA, help="User-Agent")
    g.add_argument("--proxy", help="代理，如 http://127.0.0.1:7890")
    g.add_argument("--insecure", action="store_true", help="不校验 HTTPS 证书（仅用于校内自签证书的网站）")
    g.add_argument("--max-page-bytes", type=parse_size, default=parse_size("10M"), help=argparse.SUPPRESS)

    g = p.add_argument_group("动态网页（需要 playwright）")
    g.add_argument("--js", choices=["off", "auto", "on"], default="off",
                   help="用无头浏览器渲染网页：off = 不用（默认）；auto = 页面看起来是 JS 生成的才渲染；on = 每个网页都渲染")
    g.add_argument("--js-click", action="store_true",
                   help="渲染时依次点击写着“下载 / 附件 / download”的按钮，记录触发的下载（不点删除 / 提交 / 支付之类）")
    g.add_argument("--login", action="store_true",
                   help="开爬前弹出浏览器让你登录（统一身份认证、扫码都行）；登录状态按站点存到 "
                        f"{LOGIN_DIR}，以后自动使用，并从登录后停留的页面开始爬")
    g.add_argument("--logout", action="store_true", help="删除这些站点保存的登录状态，然后退出")
    g.add_argument("--login-state", metavar="FILE", help="使用 / 保存到指定的登录状态文件（Playwright storage_state 格式）")

    g = p.add_argument_group("输出 / 运行")
    g.add_argument("--layout", choices=["page", "site", "flat"], default="page",
                   help="保存结构：page = 主机/来源页面标题/文件（默认）；site = 按 URL 路径镜像；flat = 全部放一个目录")
    g.add_argument("--save-pages", action="store_true", help="同时保存抓到的网页原文（存到 主机/_pages/，不改写链接）")
    g.add_argument("--dry-run", action="store_true", help="只列出会下载的文件，不下载、不写状态")
    g.add_argument("--probe-size", action="store_true",
                   help="配合 --dry-run：对每个文件发 HEAD 请求，列出大小和真实文件名（每个文件多一次请求）")
    g.add_argument("--fresh", action="store_true", help="清空断点状态重新开始（已下载的同内容文件仍会识别并跳过）")
    g.add_argument("--retry-failed", action="store_true", help="把上次失败的任务重新加入队列")
    g.add_argument("--recrawl", action="store_true", help="重新抓取所有网页以发现新文件（已下载的文件不重复下）")
    g.add_argument("-v", "--verbose", action="store_true", help="显示调试信息")
    g.add_argument("-q", "--quiet", action="store_true", help="只显示警告和错误")
    return p


def browser_login(url, a):
    """弹出浏览器等你登录；回车或关掉窗口就保存。返回登录后停留的页面。"""
    from js_render import LoginSession

    path = Path(a.login_state).expanduser() if a.login_state else login_state_path(url)
    s = LoginSession(url, path, proxy=a.proxy, insecure=a.insecure).start()
    print("已打开浏览器：请在弹出的窗口里登录，可以一路点进课程资料页。\n"
          "完成后回到这里按回车（或者直接关掉浏览器窗口）……", flush=True)
    entered = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.readline(), entered.set()), daemon=True).start()
    while s.alive() and not entered.is_set():
        time.sleep(0.3)
    s.finish()
    if s.status != "saved":
        sys.exit(f"登录状态没有保存：{s.error or s.status}")
    print(f"登录状态已保存：{path}（{s.cookies} 个 cookie）；当前页面：{s.current_url}", flush=True)
    forget_login(url, tokens_only=True)  # 旧令牌跟着旧登录作废，新的等渲染时重新嗅探
    forget_login(s.current_url, tokens_only=True)
    if urlsplit(s.current_url).netloc != urlsplit(url).netloc and not a.login_state:
        other = login_state_path(s.current_url)  # 登录后跳到了别的站点，那边也存一份
        other.write_bytes(path.read_bytes())
        other.chmod(0o600)
    return normalize_url(s.current_url, url)


def main(argv=None):
    parser = build_parser()
    a = parser.parse_args(argv)
    raw = list(a.urls)
    if a.input_file:
        lines = Path(a.input_file).read_text(encoding="utf-8").splitlines()
        raw += [s.strip() for s in lines if s.strip() and not s.lstrip().startswith("#")]
    seeds = []
    for u in raw:
        n = normalize_url(u if "://" in u else "https://" + u, "https://invalid/")
        if not n:
            parser.error(f"无效的 URL：{u}")
        seeds.append(n)
    if not seeds:
        parser.error("请至少给出一个起始 URL（或用 -i 指定 URL 列表文件）")
    if a.workers < 1 or a.depth < 0 or a.retries < 0 or a.delay < 0:
        parser.error("--workers 需 ≥1，--depth / --retries / --delay 需 ≥0")
    if a.logout:
        for u in seeds:
            f = Path(a.login_state).expanduser() if a.login_state else login_state_path(u)
            print(f"已删除登录状态：{f}" if f.exists() else f"没有保存过登录状态：{f}")
            f.unlink(missing_ok=True)
            forget_login(u, tokens_only=True)
        return 0
    if a.js != "off" or a.login:
        from js_render import check

        if check():
            parser.error(check())
    if a.login:
        final = browser_login(seeds[0], a)
        if final and final not in seeds:
            seeds.append(final)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if a.verbose else logging.WARNING if a.quiet else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(console)
    log.setLevel(logging.DEBUG)

    return Crawler(a, list(dict.fromkeys(seeds))).run()


if __name__ == "__main__":
    sys.exit(main())
