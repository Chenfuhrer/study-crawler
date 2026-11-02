#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全站能力测试（命令行内核，不经网页界面）：
  * -d all 不限层数；到了 --max-pages 上限，报告还有多少网页没抓
  * --sitemap：robots.txt 声明的 sitemap 索引 → .gz / 纯文本；没声明时用 /sitemap.xml；站外地址不要；受 --scope 限制
  * JS 渲染时自动点“下一页”（没字的图标按钮，最后一页变灰）和“加载更多”，不点轮播图箭头和“更多”菜单；
    --js-click 时翻到的每一页都点“下载”按钮；--js auto 能靠“加载更多”按钮认出动态页面
  * 翻页链接（“下一页 / next / ›”、同一个列表的页码）沿用当前页的层数；链到别处的数字不算；翻不完的链有上限

  python tests/test_fullsite.py
"""
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="sc-full-test-"))
os.environ["STUDY_CRAWLER_HOME"] = str(TMP / "home")  # 必须在 import study_crawler 之前
sys.path.insert(0, str(HERE.parent))
import study_crawler as sc  # noqa: E402

OK = True
LOGS = []
HIDDEN = {"notes.pdf", "deep.pdf", "syllabus.pdf"}  # 课程页上没有链接，只在 sitemap 里


class Grab(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


sc.log.setLevel("DEBUG")
sc.log.addHandler(Grab())


def check(cond, msg):
    global OK
    print(("  ✓ " if cond else "  ✗ ") + msg)
    OK &= bool(cond)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scan(url, *extra):
    """dry-run 扫描，返回 (爬虫, 找到的文件名, 这次的日志)。"""
    n0 = len(LOGS)
    a = sc.build_parser().parse_args([url, "-o", str(TMP / "out"), "--dry-run", "--delay", "0", *extra])
    c = sc.Crawler(a, [url])
    c.run()
    return c, {name for _, name, _ in c.found}, LOGS[n0:]


def pager_checks():
    print("翻页链接识别（单元）")
    yes = [("https://x/news/", "https://x/news/list_2.html", "2"),
           ("https://x/news/index.html", "https://x/news/index_3.html", "[3]"),
           ("https://x/list?page=2", "https://x/list?page=3", "第3页"),
           ("https://x/page/2/", "https://x/page/3/", "3"),
           ("https://x/a/", "https://y/whatever", "下一页 ›"),
           ("https://x/a/", "https://x/b/", "Next »"),
           ("https://x/a/", "https://x/a/?p=2", "›"),
           ("https://x/a/", "https://x/a/?p=2", "next page")]
    no = [("https://x/a/", "https://x/b/9.html", "9"),
          ("https://x/a/", "https://y/a/2.html", "2"),
          ("https://x/a/", "https://x/a/12345.html", "12345"),
          ("https://x/a/", "https://x/a/b.html", "下一章"),
          ("https://x/a/", "https://x/a/?p=1", "‹ 上一页"),
          ("https://x/a/", "https://x/a/c.html", "Next month")]
    bad = [c[2] for c in yes if not sc.is_pager_link(*c)]
    check(not bad, f"下一页 / Next » / › / 同一个列表的页码 → 翻页链接 {bad or ''}")
    bad = [c[2] for c in no if sc.is_pager_link(*c)]
    check(not bad, f"别的目录 / 别的站点的数字、上一页、下一章、Next month → 不是 {bad or ''}")

    seed = "https://lms.example.edu/p/"
    c = sc.Crawler(sc.build_parser().parse_args([seed, "-o", str(TMP / "unit"), "--dry-run"]), [seed])
    _, links, _ = c._parse_page('<a href="list_2.html">2</a> <a href="list_2.html">下一页</a>'
                                '<a href="x.pdf">讲义</a> <a href="x.pdf">下载</a>'.encode(), "utf-8", seed)
    check(links[seed + "list_2.html"] == "下一页" and links[seed + "x.pdf"] == "讲义",
          f"同一地址：“下一页”顶替页码，其他还是先出现的优先 {links}")


def main():
    pager_checks()
    port = free_port()
    site = subprocess.Popen([sys.executable, str(HERE / "fixture_site.py"), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    origin = f"http://127.0.0.1:{port}"
    course = origin + "/course/"
    hits = lambda k: requests.get(origin + "/admin/log").json()[k]  # noqa: E731
    try:
        for _ in range(75):
            try:
                requests.get(origin + "/robots.txt", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)

        print("深度参数")
        p = sc.build_parser()
        check(p.parse_args(["x", "-d", "all"]).depth == sc.DEPTH_ALL and p.parse_args(["x", "-d", "2"]).depth == 2,
              "-d all = 不限层数，数字照旧")

        print("没有 --sitemap")
        _, names, _ = scan(course, "-d", "all")
        check(names and not names & HIDDEN, f"不限层数也找不到孤立网页里的文件：{sorted(names & HIDDEN)}")

        print("--sitemap：robots.txt 声明的索引 → .gz + 纯文本")
        _, names, logs = scan(course, "-d", "all", "--sitemap")
        check(HIDDEN <= names, f"只在 sitemap 里的网页 / 文件都找到了：{sorted(HIDDEN & names)}")
        check("away.pdf" not in names, "sitemap 里的站外地址不要")
        check("plan.pdf" not in names, "robots.txt 声明了 sitemap，就不再去猜 /sitemap.xml")
        line = next((m for m in logs if m.startswith("sitemap：")), "")
        check("读了 3 个，列出 7 个网址，其中 5 个" in line, f"索引 + 2 个子 sitemap，起始页不重复加：{line}")

        print("sitemap 里的网址算第 1 层")
        _, names, _ = scan(course, "-d", "0", "--sitemap")
        check("syllabus.pdf" in names and "notes.pdf" not in names, "-d 0：只要 sitemap 里直接列出的文件，不进网页")

        print("--scope prefix")
        _, names, _ = scan(course, "-d", "all", "--sitemap", "--scope", "prefix")
        check(not names & HIDDEN, f"起始目录外的 sitemap 网址不要：{sorted(names & HIDDEN)}")

        print("robots.txt 没声明 sitemap")
        requests.post(origin + "/admin/set", json={"robots_sitemap": 0})
        _, names, _ = scan(course, "-d", "all", "--sitemap")
        requests.post(origin + "/admin/set", json={"robots_sitemap": 1})
        check("plan.pdf" in names and not names & HIDDEN, f"改读默认位置 /sitemap.xml：{sorted(names & (HIDDEN | {'plan.pdf'}))}")

        print("网页数上限")
        c, _, logs = scan(course, "-d", "all", "--sitemap", "--max-pages", "3", "-w", "1")
        warn = next((m for m in logs if m.startswith("已达到最多抓取网页数")), "")
        check(c.stats["pages"] == 3 and c.stats["capped"] > 0 and f"还有 {c.stats['capped']} 个" in warn,
              f"抓了 {c.stats['pages']} 个就停，并报告剩下的：{warn}")
        c, _, logs = scan(course, "-d", "all", "--sitemap")
        check(not c.stats["capped"] and not any(m.startswith("已达到") for m in logs), "没到上限不报")

        print("JS：自动点“下一页”")
        n0 = len(hits("pager"))
        _, names, logs = scan(origin + "/pager/list.html", "-d", "0", "--js", "on")
        want = {f"p{p}-{x}.pdf" for p in (1, 2, 3) for x in "ab"}
        check(want <= names, f"三页的课件都找到了（链接只在每一页的 DOM 里）：{sorted(want & names)}")
        check(hits("pager")[n0:] == [1, 2, 3], f"翻到第 3 页（按钮变灰）就停：{hits('pager')[n0:]}")
        check(any("翻页 / 加载更多 2 次" in m for m in logs),
              f"只翻了 2 次，没点轮播图箭头和“更多”菜单：{[m for m in logs if '翻页' in m]}")

        print("JS：--js-click 时每一页都点“下载”按钮")
        _, names, _ = scan(origin + "/pager/list.html", "-d", "0", "--js", "on", "--js-click")
        answers = {f"第{p}页答案.pdf" for p in (1, 2, 3)}
        check(answers <= names and want <= names, f"三页的答案都点出来了：{sorted(answers & names)}")

        print("JS：静态网页上的“加载更多”")
        n0 = len(hits("feed"))
        _, names, logs = scan(origin + "/feed/", "-d", "0", "--js", "auto")
        want = {f"feed{i}.pdf" for i in range(1, 7)}
        check(want <= names, f"--js auto 认出动态页面，点出后面两批：{sorted(want & names)}")
        check(hits("feed")[n0:] == [2, 3], f"按钮消失就停：{hits('feed')[n0:]}")

        print("翻页链接不算层")
        _, names, _ = scan(origin + "/plist/", "-d", "1")
        want = {f"{n}{x}.pdf" for n in range(1, 5) for x in "ab"}
        check(want <= names, f"-d 1：4 页列表翻完，每页的详情页都进去了：{sorted(want & names)}")
        check("deeper.pdf" not in names, "链到别的目录的“7”照常算一层")
        c, names, _ = scan(origin + "/plist/", "-d", "0")
        check(c.stats["pages"] == 4 and not names & want, f"-d 0：只翻列表，不进详情页（抓了 {c.stats['pages']} 页）")
        old, sc.MAX_PAGE_HOPS = sc.MAX_PAGE_HOPS, 5
        try:
            c, _, _ = scan(origin + "/cal/?m=0", "-d", "0")
        finally:
            sc.MAX_PAGE_HOPS = old
        check(c.stats["pages"] == 6, f"翻不完的“›”：连续 5 页之后照常算层，停在第 6 页（抓了 {c.stats['pages']} 页）")
    finally:
        site.terminate()
    print("全部通过" if OK else "有失败项")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
