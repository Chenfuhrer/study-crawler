#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载能力测试（命令行内核，不经网页界面）：
  * 下载校验：文件头（magic bytes）、接口 JSON 错误、错误网页、登录页
  * 断点续传：If-Range，文件更新后不会把新旧两个版本拼在一起；服务器不认 If-Range 时自己对 ETag
  * 登录失效：有登录凭据时暂停整批（退出码 3，任务保持待处理）；本来就没登录只记失败

  python tests/test_download.py
"""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import requests

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="sc-dl-test-"))
os.environ["STUDY_CRAWLER_HOME"] = str(TMP / "home")  # 必须在 import study_crawler 之前
sys.path.insert(0, str(HERE.parent))
import study_crawler as sc  # noqa: E402

sc.log.setLevel("DEBUG")
OK = True


def check(cond, msg):
    global OK
    print(("  ✓ " if cond else "  ✗ ") + msg)
    OK &= bool(cond)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def crawl(url, out, *extra):
    a = sc.build_parser().parse_args([url, "-o", str(out), "--delay", "0", "--retries", "2", *extra])
    return sc.Crawler(a, [url]).run()


def rows(out):
    with sqlite3.connect(out / ".study_crawler.db") as db:
        return {u.rsplit("/", 1)[-1]: (st, note or "") for u, st, note in db.execute("SELECT url,status,note FROM urls")}


def content_checks():
    print("文件头 / 接口错误识别（单元）")
    check(sc.sniff_format(b"%PDF-1.7\n") == "pdf" and sc.sniff_format(b"\r\n%PDF-1.4") == "pdf", "PDF，允许前面有空行")
    check(sc.sniff_format(b"PK\x03\x04rest") == "zip" and sc.sniff_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") == "ole",
          "ZIP（docx/pptx）与 OLE（doc/ppt）")
    check(sc.sniff_format(b"\0\0\0\x18ftypmp42") == "mp4" and sc.sniff_format(b'{"code":1}') == "", "MP4；JSON 认不出格式")
    check(sc.api_error({"code": 401, "msg": "未登录"}) == ("未登录", True), "code=401 → 登录失效")
    check(sc.api_error({"success": False, "message": "token已过期"}) == ("token已过期", True), "提示里写着过期 → 登录失效")
    check(sc.api_error({"code": 403, "msg": "无权限下载"}) == ("无权限下载", False), "无权限不算登录失效")
    check(sc.api_error({"code": 0, "data": []}) is None and sc.api_error({"status": "published", "items": []}) is None,
          "code=0、status 是文字 → 不是错误")

    seed = "https://lms.example.edu/course/"
    c = sc.Crawler(sc.build_parser().parse_args([seed, "-o", str(TMP / "unit"), "--dry-run"]), [seed])
    r = SimpleNamespace(url="https://lms.example.edu/f", history=[])

    def cc(name, head, url="https://lms.example.edu/f", cd=None):
        t = sc.Task(url, "file", 1)
        head = head.encode() if isinstance(head, str) else head
        try:
            return c._check_content(t, r, name, cd, head, len(head))
        except sc.AuthLost as e:
            return f"AUTH:{e}"

    check(cc("a.pdf", '{"code":500,"msg":"文件不存在"}') == "接口返回了错误：文件不存在", "PDF 实际是 JSON 错误")
    check(cc("a.pdf", '{"code":401,"msg":"请重新登录"}').startswith("AUTH:"), "PDF 实际是 JSON 401 → 登录失效")
    check(cc("a.pdf", b"\x00junk" * 10).startswith("内容不是有效的 PDF"), "PDF 文件头不对")
    check(cc("a.pdf", b"").startswith("服务器返回了空文件"), "空文件")
    check(cc("a.doc", b"PK\x03\x04....") == "" and cc("a.docx", b"PK\x03\x04....") == "", "doc 实际是 docx 也放行")
    check(cc("a.txt", b"<!DOCTYPE html><html><title>404</title>") == "拿到的是网页而不是文件（链接可能失效了）", "txt 实际是网页")
    check(cc("a.pdf", "<html><head><title>统一身份认证</title><input type=password>").startswith("AUTH:"),
          "PDF 实际是登录页 → 登录失效")
    check(cc("a.csv", "name,score\n张三,90\n") == "", "普通文本文件放行")
    check(cc("data.json", b'{"status":"published","items":[1]}', url="https://x/data.json") == "", "本来就要 JSON 文件：正常数据放行")
    check(cc("data.json", b'{"code":0,"data":[]}', url="https://x/data.json") == "", "本来就要 JSON 文件：code=0 放行")
    got = cc("课件.pdf", b'{"code":0,"data":{"url":"/files/ch1.pdf"}}', cd="课件.pdf")
    check(got.startswith("接口返回了 JSON 数据而不是文件") and "/files/ch1.pdf" in got, f"JSON 里套真实地址：{got}")
    check(cc("x", b'{"rows":[]}') != "", "没有扩展名、内容是 JSON 数据 → 不当文件")


def main():
    port = free_port()
    site = subprocess.Popen([sys.executable, str(HERE / "fixture_site.py"), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    origin = f"http://127.0.0.1:{port}"
    try:
        for _ in range(75):
            try:
                requests.get(origin + "/robots.txt", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)
        content_checks()

        print("下载校验（端到端）")
        out = TMP / "verify"
        crawl(origin + "/v/", out, "-d", "0")
        rs = rows(out)
        check(rs["real.pdf"][0] == "done", "真 PDF 下载成功")
        check(rs["fake.pdf"] == ("failed", "接口返回了错误：文件不存在或已被删除"), f"JSON 错误：{rs['fake.pdf']}")
        check(rs["broken.pdf"][0] == "failed" and "不是有效的 PDF" in rs["broken.pdf"][1], f"乱码：{rs['broken.pdf']}")
        check(rs["errpage.docx"][0] == "failed" and "网页" in rs["errpage.docx"][1], f"错误网页：{rs['errpage.docx']}")
        check(rs["getfile?id=9"][0] == "failed" and "/course/files/ch1.pdf" in rs["getfile?id=9"][1],
              f"JSON 里套地址：{rs['getfile?id=9']}")
        check(rs["expired.pdf"][0] == "failed" and rs["expired.pdf"][1].startswith("需要登录"),
              f"没登录过 → 只记失败、不暂停：{rs['expired.pdf']}")
        saved = sorted(p.name for p in out.rglob("*.pdf")) + sorted(p.name for p in out.rglob("*.docx"))
        check(saved == ["real.pdf"], f"错误内容没有存成文件：{saved}")
        check(not list((out / ".parts").glob("*")), "没有留下 .part / .meta")

        print("登录失效 → 暂停")
        out = TMP / "pause"
        rc = crawl(origin + "/v/expired.pdf", out, "--cookie", "sid=old")
        st = rows(out)["expired.pdf"]
        check(rc == 3 and st[0] == "pending", f"有凭据：退出码 {rc}，任务 {st[0]}（保持待处理，重新登录后接着下）")

        print("断点续传 If-Range")
        url = origin + "/r/notes.pdf"
        log = lambda: requests.get(origin + "/admin/log").json()["log"]  # noqa: E731

        def resume_case(name, u, part_bytes, meta, ver=None):
            out = TMP / name
            parts = out / ".parts"
            parts.mkdir(parents=True)
            part = parts / (sc.sha1(u)[:20] + ".part")
            part.write_bytes(part_bytes)
            if meta:
                part.with_suffix(".meta").write_text(json.dumps(meta))
            if ver:
                requests.post(origin + "/admin/set", json=ver)
            n0 = len(log())
            rc = crawl(u, out)
            got = next(out.rglob(u.rsplit("/", 1)[-1]), None)
            return rc, got.read_bytes() if got else b"", [tuple(x) for x in log()[n0:]]

        v1 = requests.get(url).content
        meta1 = {"url": url, "etag": '"notes-v1"', "modified": "", "total": len(v1)}
        rc, data, lg = resume_case("same", url, v1[:50_000], meta1)
        check(data == v1 and lg == [("notes", "bytes=50000-", '"notes-v1"', 206)], f"文件没变：带 If-Range 续传 206 {lg}")

        rc, data, lg = resume_case("changed", url, v1[:50_000], meta1, {"notes": 2})
        v2 = requests.get(url).content
        check(v2 != v1 and len(v2) == len(v1), "fixture：两个版本等长不同内容")
        check(data == v2 and [s for *_, s in lg] == [200], f"文件更新了：服务器按 If-Range 回 200，整个重下 {lg}")

        surl = origin + "/r/sloppy.pdf"
        s1 = requests.get(surl).content
        smeta = {"url": surl, "etag": '"sloppy-v1"', "modified": "", "total": len(s1)}
        rc, data, lg = resume_case("sloppy", surl, s1[:50_000], smeta, {"sloppy": 2})
        s2 = requests.get(surl).content
        check(data == s2 and [s for *_, s in lg] == [206, 200],
              f"服务器不认 If-Range：发现 ETag 变了，丢掉旧的从头下 {[s for *_, s in lg]}")

        requests.post(origin + "/admin/set", json={"notes": 1})
        rc, data, lg = resume_case("junk", url, b"<!doctype html><title>x</title><p>err</p>", None)
        check(data == v1 and [s for *_, s in lg] == [206, 200], f"旧版本留下的 .part 是网页：丢掉重下 {[s for *_, s in lg]}")
        check(not list((TMP / "junk" / ".parts").glob("*")), "完成后 .part / .meta 都清理了")
    finally:
        site.terminate()
    print("全部通过" if OK else "有失败项")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
