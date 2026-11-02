#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试用的假课程网站：python tests/fixture_site.py [端口，默认 9941]

  /course/            静态课程页：直链、download?id=3（Content-Disposition 中文名）、预览器参数、需登录的文件
  /course/spa.html    Vue 风格单页应用：空 <div id=app>，文件列表来自 /api/materials，
                      另有一个“下载全部答案”按钮（fetch → blob 下载，静态解析完全看不到）
  /course/list.html   正文很多的静态壳子，课件表格由内联脚本 fetch('/api/list') 填进去
  /login              统一身份认证：提交后种 cookie sid=ok，并把 token 写进 localStorage（?auto=1 自动提交）
  /portal/#/course/42 要登录的单页应用：hash 路由，文件列表接口和文件下载都只认 Authorization: Bearer <token>，
                      光有 cookie 不行；令牌失效时文件接口学国内平台：HTTP 200 + {"code":401,"msg":"登录已过期…"}
  /v/                 下载校验：扩展名是 PDF / DOCX，实际回的是 JSON 错误、乱码、网页、JSON 里套真实地址
  /r/notes.pdf        支持 Range + If-Range 的文件，版本可切换（ETag 跟着变）；/r/sloppy.pdf 不认 If-Range
  全站相关（课程页上都没有链接，只有 sitemap 里列着）：
  /robots.txt         Sitemap: 指向 /sitemap_index.xml → /sitemaps/pages.xml.gz（gzip、CDATA、站外地址）+ /sitemaps/files.txt（纯文本）；
                      /admin/set {"robots_sitemap":0} 去掉声明，这时只有默认位置 /sitemap.xml
  /hidden/            孤立网页，只在 sitemap 里出现
  /pager/list.html    Element UI 风格分页：列表接口只给 id，链接在前端拼；“下一页”是没字的图标按钮，最后一页变灰；
                      另有轮播图的“Next slide”箭头和“更多”菜单（都不该点），每页一个“下载本页答案”按钮
  /feed/              静态网页 + JS “加载更多”按钮（外链脚本，只能靠按钮认出是动态页面），点两次加载完
  /plist/             静态分页列表，4 页（/plist/ → list_2.html …）：每页只链到相邻页码和“下一页 ›”，
                      条目在详情页 /plist/item/1a.html 里才有文件链接；第 1 页另有一个页码样子的“7”链到别的目录（不算翻页）
  /cal/?m=0           翻不完的日历：只有一个“›”链到下个月

测试用的管理接口（真网站没有）：POST /admin/expire 让令牌作废；POST /admin/set {"notes":2} 切换版本；GET /admin/log
"""
import gzip
import hashlib
import re
import secrets
import sys
import time
from urllib.parse import quote

from flask import Flask, Response, make_response, request

app = Flask(__name__)

NAV = "".join(f'<li><a href="/course/week{i}.html">第 {i} 周 课程安排与阅读材料</a></li>' for i in range(1, 7))
INTRO = "本课程介绍数据结构的基本概念、线性表、栈与队列、树与二叉树、图以及查找和排序算法。" * 4


def blob(name, size):
    seed = hashlib.sha256(name.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


SIZES = {"ch1.pdf": 300_000, "ch1-copy.pdf": 300_000, "作业1.docx": 40_000, "讲义.pdf": 120_000,
         "ch2.pptx": 900_000, "期末复习.pdf": 260_000, "report.docx": 30_000, "大文件.zip": 6_000_000,
         "secret.pdf": 50_000, "app.js": 2_000}
CTYPE = {"pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation", "zip": "application/zip"}


MAGIC = {"pdf": b"%PDF-1.4\n", "docx": b"PK\x03\x04", "pptx": b"PK\x03\x04", "zip": b"PK\x03\x04"}


def file_response(name, data, cd_name=None, slow=False):
    ext = name.rsplit(".", 1)[-1]
    sig = MAGIC.get((cd_name or name).rsplit(".", 1)[-1], b"")
    data = sig + data[len(sig):]  # 像真文件一样有文件头（下载时会校验），长度不变
    headers = {"Content-Length": str(len(data))}
    if cd_name:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(cd_name)}"

    def gen():
        for i in range(0, len(data), 64 * 1024):
            if slow:
                time.sleep(0.04)
            yield data[i:i + 64 * 1024]

    return Response(gen(), mimetype=CTYPE.get(ext, "application/octet-stream"), headers=headers)


SITE = {"robots_sitemap": 1}


@app.get("/robots.txt")
def robots():
    sm = f"Sitemap: {request.host_url}sitemap_index.xml\n" if SITE["robots_sitemap"] else ""
    return Response("User-agent: *\nAllow: /\n" + sm, mimetype="text/plain")


@app.get("/course/")
def course():
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>数据结构（2026 秋）</title></head><body>
<h1>数据结构</h1><p>{INTRO}</p><ul>{NAV}</ul>
<h2>课件与资料</h2><ul>
<li><a href="files/ch1.pdf">第1章 绪论</a></li>
<li><a href="files/ch1-copy.pdf">第1章 绪论（备份链接）</a></li>
<li><a href="files/作业1.docx">作业1</a></li>
<li><a href="/download?id=3">下载</a></li>
<li><a href="viewer.html?file=/course/files/讲义.pdf">在线预览讲义</a></li>
<li><a href="files/大文件.zip">实验代码合集</a></li>
<li><a href="login-only/secret.pdf">往年试题（需登录）</a></li>
<li><a href="spa.html">资料库（动态页面）</a></li>
<li><a href="list.html">课件列表</a></li>
</ul></body></html>"""


@app.get("/course/week<int:i>.html")
def week(i):
    return f"<!doctype html><meta charset=utf-8><title>第 {i} 周</title><p>{INTRO}</p><a href='/course/'>返回</a>"


@app.get("/course/viewer.html")
def viewer():
    return "<!doctype html><meta charset=utf-8><title>预览</title><p>PDF 预览器</p>"


@app.get("/course/files/<name>")
def files(name):
    if name not in SIZES:
        return "not found", 404
    data = blob("ch1.pdf" if name == "ch1-copy.pdf" else name, SIZES[name])
    return file_response(name, data, slow=name.endswith(".zip"))


@app.get("/download")
def download():
    return file_response("x.pdf", blob("实验指导书", 80_000), cd_name="实验指导书.pdf")


@app.get("/course/login-only/secret.pdf")
def secret():
    if request.cookies.get("sid") != "ok":
        return "<!doctype html><meta charset=utf-8><title>请登录</title><form>用户名<input></form>"
    return file_response("secret.pdf", blob("secret.pdf", SIZES["secret.pdf"]))


@app.get("/course/spa.html")
def spa():
    return """<!doctype html><html><head><meta charset="utf-8"><title>资料库</title></head>
<body><div id="app"></div><script src="app.bundle.js"></script></body></html>"""


@app.get("/course/app.bundle.js")
def spa_js():
    js = r"""
fetch('/api/materials').then(r => r.json()).then(d => {
  const app = document.getElementById('app');
  app.innerHTML = '<h2>资料库</h2><ul id="m"></ul><button id="ans">下载全部答案</button>' +
                  '<span class="dl" data-file="/course/files/期末复习.pdf">期末复习</span>' +
                  '<button id="del" onclick="alert(1)">删除下载记录</button>';
  for (const it of d.data) {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.textContent = it.title || it.name;
    a.href = 'javascript:void(0)';
    a.onclick = () => { location.href = it.downloadUrl || it.url; };
    li.appendChild(a); document.getElementById('m').appendChild(li);
  }
  document.getElementById('ans').onclick = () =>
    fetch('/api/answer').then(r => r.blob()).then(b => {
      const u = URL.createObjectURL(b), x = document.createElement('a');
      x.href = u; x.download = '答案.zip'; document.body.appendChild(x); x.click();
    });
});
"""
    return Response(js, mimetype="application/javascript")


@app.get("/api/materials")
def materials():
    return {"code": 0, "data": [{"title": "第3章 栈与队列", "downloadUrl": "/dl/7"},
                                {"name": "实验报告模板.docx", "url": "/course/files/report.docx"}]}


@app.get("/dl/<int:i>")
def dl(i):
    return file_response("x.pdf", blob(f"dl{i}", 150_000), cd_name="第3章 栈与队列.pdf")


@app.get("/api/answer")
def answer():
    return file_response("a.zip", blob("answer", 64_000), cd_name="答案.zip")


@app.get("/course/list.html")
def list_page():
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>课件列表</title></head><body>
<h1>课件列表</h1><p>{INTRO}</p><ul>{NAV}</ul><table id="files"></table>
<script>
fetch('/api/list').then(r => r.json()).then(rows => {{
  document.getElementById('files').innerHTML = rows.map(r =>
    '<tr><td><a href="' + r.path + '">' + r.label + '</a></td></tr>').join('');
}});
</script></body></html>"""


@app.get("/api/list")
def api_list():
    return [{"label": "第2章 线性表", "path": "/course/files/ch2.pptx"}]


# ── 全站：sitemap / 翻页 / 加载更多 ──

def urlset(urls):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")


@app.get("/sitemap_index.xml")
def sitemap_index():
    h = request.host_url
    return Response('<?xml version="1.0" encoding="UTF-8"?>\n<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<sitemap><loc>{h}sitemaps/pages.xml.gz</loc></sitemap>"
                    f"<sitemap><loc>{h}sitemaps/files.txt</loc></sitemap></sitemapindex>", mimetype="application/xml")


@app.get("/sitemaps/pages.xml.gz")
def sitemap_pages():  # 不带 Content-Encoding 的 .gz：得自己解压
    h = request.host_url
    xml = urlset([f"{h}course/", f"{h}hidden/", f"<![CDATA[{h}hidden/deep.html]]>", f"{h}pager/list.html", f"{h}feed/",
                  "http://other.invalid/away.pdf"])
    return Response(gzip.compress(xml.encode()), mimetype="application/x-gzip")


@app.get("/sitemaps/files.txt")
def sitemap_text():
    return Response(f"{request.host_url}hidden/files/syllabus.pdf\n", mimetype="text/plain")


@app.get("/sitemap.xml")  # robots.txt 没声明时的默认位置
def sitemap_default():
    return Response(urlset([f"{request.host_url}hidden/files/plan.pdf"]), mimetype="application/xml")


@app.get("/hidden/")
def hidden():
    return "<!doctype html><meta charset=utf-8><title>往届资料</title><a href='files/notes.pdf'>往届笔记</a>"


@app.get("/hidden/deep.html")
def hidden_deep():
    return "<!doctype html><meta charset=utf-8><title>深处</title><a href='/hidden/files/deep.pdf'>深处的讲义</a>"


@app.get("/hidden/files/<name>")
@app.get("/pager/files/<name>")
@app.get("/feed/files/<name>")
@app.get("/plist/files/<name>")
@app.get("/elsewhere/files/<name>")
def extra_files(name):
    return file_response(name, blob(name, 30_000))


PLIST_PAGES = 4


def plist_url(n):
    return "/plist/" if n == 1 else f"/plist/list_{n}.html"


def plist_page(n):
    items = "".join(f'<li><a href="/plist/item/{n}{c}.html">第{n}页 条目{c.upper()}</a></li>' for c in "ab")
    nums = " ".join(f'<a href="{plist_url(m)}">{m}</a>' for m in (n - 1, n + 1) if 1 <= m <= PLIST_PAGES)
    prev = f'<a href="{plist_url(n - 1)}">‹ 上一页</a>' if n > 1 else ""
    nxt = f'<a href="{plist_url(n + 1)}">下一页 ›</a>' if n < PLIST_PAGES else ""
    extra = '<p>相关：<a href="/elsewhere/7.html">7</a></p>' if n == 1 else ""
    return (f"<!doctype html><meta charset=utf-8><title>课件列表（第 {n} 页）</title><ul>{items}</ul>"
            f"<div class=pager>{prev} {nums} {nxt}</div>{extra}")


@app.get("/plist/")
def plist_first():
    return plist_page(1)


@app.get("/plist/list_<int:n>.html")
def plist_more(n):
    return plist_page(n) if 2 <= n <= PLIST_PAGES else ("not found", 404)


@app.get("/plist/item/<name>.html")
def plist_item(name):
    return f"<!doctype html><meta charset=utf-8><title>条目 {name}</title><a href='/plist/files/{name}.pdf'>下载讲义</a>"


@app.get("/elsewhere/7.html")
def elsewhere():
    return "<!doctype html><meta charset=utf-8><title>别处</title><a href='/elsewhere/deeper.html'>更深一层</a>"


@app.get("/elsewhere/deeper.html")
def elsewhere_deeper():
    return "<!doctype html><meta charset=utf-8><title>更深</title><a href='/elsewhere/files/deeper.pdf'>更深处的讲义</a>"


@app.get("/cal/")
def calendar():
    m = int(request.args.get("m", 0))
    return f"<!doctype html><meta charset=utf-8><title>日历 {m}</title><p>第 {m} 个月</p><a href='/cal/?m={m + 1}'>›</a>"


PAGER = {p: [{"id": f"p{p}-{c}", "title": f"第{p}页 资料{c.upper()}"} for c in "ab"] for p in (1, 2, 3)}
HITS = {"pager": [], "feed": []}


@app.get("/pager/list.html")
def pager_page():
    return """<!doctype html><html><head><meta charset="utf-8"><title>分页课件</title></head>
<body><div id="app"></div><script src="/pager/pager.js"></script></body></html>"""


@app.get("/pager/pager.js")
def pager_js():
    js = r"""
let p = 1, slide = 1;
const app = document.getElementById('app');
function load() {
  fetch('/api/pager?p=' + p).then(r => r.json()).then(d => {
    app.innerHTML = '<h2>课件（第 ' + p + ' 页）</h2><ul>' + d.rows.map(x =>
      '<li><a href="/pager/files/' + x.id + '.pdf">' + x.title + '</a></li>').join('') + '</ul>' +
      '<button class="ans">下载本页答案</button>' +
      '<div class="el-pagination"><button class="btn-prev"' + (p === 1 ? ' disabled' : '') + '>‹</button>' +
      '<span class="el-pager">' + p + ' / ' + d.pages + '</span>' +
      '<button class="btn-next"' + (p >= d.pages ? ' disabled' : '') + '><i class="el-icon-arrow-right"></i></button></div>' +
      '<div class="carousel"><button class="swiper-button-next" aria-label="Next slide"></button><span id="slide">' + slide + '</span></div>' +
      '<button id="menu">更多</button><div id="menuBox"></div>';
    app.querySelector('.btn-next').onclick = () => { p++; load(); };
    app.querySelector('.btn-prev').onclick = () => { p--; load(); };
    app.querySelector('.swiper-button-next').onclick = () => { document.getElementById('slide').textContent = ++slide; };
    document.getElementById('menu').onclick = () => {
      const b = document.getElementById('menuBox'); b.textContent = b.textContent ? '' : '设置 帮助';
    };
    app.querySelector('.ans').onclick = () => fetch('/api/pager/answer?p=' + p).then(r => r.blob()).then(b => {
      const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = 'x.pdf';
      document.body.appendChild(a); a.click();
    });
  });
}
load();
"""
    return Response(js, mimetype="application/javascript")


@app.get("/api/pager")
def api_pager():  # 只给 id 和标题：文件地址在前端拼出来，接口 JSON 里看不到
    p = int(request.args.get("p", 1))
    HITS["pager"].append(p)
    return {"code": 0, "pages": len(PAGER), "rows": PAGER.get(p, [])}


@app.get("/api/pager/answer")
def pager_answer():
    name = f"第{int(request.args.get('p', 1))}页答案.pdf"
    return file_response("x.pdf", blob(name, 20_000), cd_name=name)


@app.get("/feed/")
def feed():
    first = "".join(f'<li><a href="/feed/files/feed{i}.pdf">动态 {i}</a></li>' for i in (1, 2))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>资料动态</title></head><body>
<h1>资料动态</h1><p>{INTRO}</p><ul>{NAV}</ul><ul id="feed">{first}</ul>
<button id="more">加载更多</button><script src="/feed/feed.js"></script></body></html>"""


@app.get("/feed/feed.js")
def feed_js():
    js = r"""
let p = 1;
document.getElementById('more').onclick = () => {
  p++;
  fetch('/api/feed?p=' + p).then(r => r.json()).then(d => {
    for (const id of d.ids) {
      const li = document.createElement('li');
      li.innerHTML = '<a href="/feed/files/' + id + '.pdf">动态</a>';
      document.getElementById('feed').appendChild(li);
    }
    if (!d.more) document.getElementById('more').remove();
  });
};
"""
    return Response(js, mimetype="application/javascript")


@app.get("/api/feed")
def api_feed():
    p = int(request.args.get("p", 1))
    HITS["feed"].append(p)
    return {"ids": [f"feed{2 * p - 1}", f"feed{2 * p}"], "more": p < 3}


STATE = {"token": "T0KEN-42"}
VER = {"notes": 1, "sloppy": 1}
RLOG = []  # Range 请求记录：(路径, Range, If-Range, 状态码)
PORTAL_FILES = {1: "第1讲 信号的分类.pdf", 2: "期中试卷.pdf", 3: "实验三 卷积.docx"}


def authorized():
    return request.headers.get("Authorization") == f"Bearer {STATE['token']}"


@app.get("/login")
def login_form():
    auto = "<script>setTimeout(() => document.forms[0].submit(), 300)</script>" if request.args.get("auto") else ""
    return f"""<!doctype html><meta charset=utf-8><title>统一身份认证</title>
<form method=post action=/login>学号 <input name=u value=2025001> 密码 <input name=p type=password value=x>
<button>登录</button></form>{auto}"""


@app.post("/login")
def login_submit():
    resp = make_response(f"""<!doctype html><meta charset=utf-8><title>登录成功</title><script>
localStorage.setItem('token', '{STATE["token"]}'); location.replace('/portal/#/course/42');</script>""")
    resp.set_cookie("sid", "ok")
    return resp


@app.get("/portal/")
def portal():
    return """<!doctype html><html><head><meta charset="utf-8"><title>教学平台</title></head>
<body><div id="app"></div><script src="/portal/portal.js"></script></body></html>"""


@app.get("/portal/portal.js")
def portal_js():
    js = r"""
function route() {
  const app = document.getElementById('app');
  const token = localStorage.getItem('token');
  if (!token) { app.innerHTML = '<p>请先 <a href="/login">登录</a></p>'; return; }
  const m = location.hash.match(/^#\/course\/(\d+)/);
  if (!m) { app.innerHTML = '<h2>我的课程</h2><a href="#/course/42">信号与系统</a>'; return; }
  const auth = { headers: { Authorization: 'Bearer ' + token } };
  fetch('/api/portal/course/' + m[1] + '/files', auth).then(r => r.json()).then(d => {
    app.innerHTML = '<h2>' + d.course + '</h2><ul>' + d.files.map(f =>
      '<li>' + f.fileName + ' <button data-id="' + f.id + '">下载</button></li>').join('') + '</ul>';
    for (const b of app.querySelectorAll('button'))
      b.onclick = () => fetch('/api/portal/file/' + b.dataset.id, auth).then(r => r.blob()).then(x => {
        const a = document.createElement('a'); a.href = URL.createObjectURL(x); a.download = 'f'; a.click();
      });
  });
}
window.addEventListener('hashchange', route);
route();
"""
    return Response(js, mimetype="application/javascript")


@app.get("/api/portal/course/<int:cid>/files")
def portal_files(cid):
    if not authorized():
        return {"code": 401, "msg": "未登录"}, 401
    return {"course": "信号与系统", "files": [
        {"id": i, "fileName": n, "downloadUrl": f"/api/portal/file/{i}"} for i, n in PORTAL_FILES.items()]}


@app.get("/api/portal/file/<int:fid>")
def portal_file(fid):
    if not authorized():  # 很多国内平台：HTTP 200，错误写在 JSON 里
        return {"code": 401, "msg": "登录已过期，请重新登录", "data": None}
    name = PORTAL_FILES.get(fid)
    if not name:
        return "not found", 404
    return file_response(name, blob(name, 90_000 + fid * 1000), cd_name=name)


# ── 下载校验 ──

@app.get("/v/")
def verify_index():
    return """<!doctype html><meta charset=utf-8><title>校验</title><ul>
<li><a href="real.pdf">真 PDF</a></li><li><a href="fake.pdf">JSON 错误</a></li>
<li><a href="broken.pdf">乱码</a></li><li><a href="errpage.docx">错误网页</a></li>
<li><a href="getfile?id=9">课件下载</a></li><li><a href="expired.pdf">令牌过期</a></li></ul>"""


@app.get("/v/<name>")
def verify_file(name):
    pdf = {"Content-Type": "application/pdf"}
    if name == "real.pdf":
        return Response(b"%PDF-1.4\n" + blob("real", 30_000), headers=pdf)
    if name == "fake.pdf":
        return Response('{"code":500,"msg":"文件不存在或已被删除"}', headers=pdf)
    if name == "broken.pdf":
        return Response(b"\x00\x13garbage" + blob("broken", 5_000), headers=pdf)
    if name == "errpage.docx":
        return Response("<!doctype html><meta charset=utf-8><title>出错了</title><p>系统繁忙，请稍后再试</p>",
                        mimetype="application/octet-stream")
    if name == "getfile":  # 下载接口回的是 JSON（真实地址在里面），却带着附件文件名
        return Response('{"code":0,"data":{"url":"/course/files/ch1.pdf"}}', mimetype="application/json",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote('课件.pdf')}"})
    if name == "expired.pdf":
        return Response('{"code":"401","message":"token已过期"}', headers=pdf)
    return "not found", 404


# ── 断点续传：Range / If-Range ──

@app.get("/r/<name>.pdf")
def ranged(name):
    if name not in VER:
        return "not found", 404
    data = b"%PDF-1.4\n" + blob(f"{name}-v{VER[name]}", 200_000)  # 两个版本一样长：只靠大小发现不了
    etag = f'"{name}-v{VER[name]}"'
    rng, if_range = request.headers.get("Range", ""), request.headers.get("If-Range", "")
    m = re.match(r"bytes=(\d+)-$", rng)
    honor = m and (name == "sloppy" or not if_range or if_range == etag)  # sloppy：不认 If-Range，照样续传
    if honor:
        start = int(m.group(1))
        resp = Response(data[start:], 206, mimetype="application/pdf",
                        headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
    else:
        resp = Response(data, mimetype="application/pdf")
    resp.headers["ETag"], resp.headers["Accept-Ranges"] = etag, "bytes"
    RLOG.append((name, rng, if_range, resp.status_code))
    return resp


@app.post("/admin/expire")
def admin_expire():
    STATE["token"] = "T0KEN-" + secrets.token_hex(4)
    return {"ok": True}


@app.post("/admin/set")
def admin_set():
    body = request.get_json() or {}
    VER.update({k: int(v) for k, v in body.items() if k in VER})
    SITE.update({k: int(v) for k, v in body.items() if k in SITE})
    return {**VER, **SITE}


@app.get("/admin/log")
def admin_log():
    return {"log": RLOG, **HITS}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 9941, threaded=True)
