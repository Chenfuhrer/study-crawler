#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试：起假课程网站 + 网页界面后端，走一遍 扫描 → 下载 → 转发 → ZIP → 浏览器登录 → 带 token 的单页应用
→ 令牌持久化 → 令牌过期时暂停 → 重新登录后继续。
登录窗口用无头模式（SC_LOGIN_HEADLESS=1），登录页 ?auto=1 会自己提交表单，相当于“用户登录了”。
登录状态写到临时目录（STUDY_CRAWLER_HOME），不碰 ~/.study_crawler。

  python tests/test_web.py
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
EXTS = "pdf,doc,docx,ppt,pptx,xls,xlsx,zip,rar,7z,txt"  # 跟网页界面默认勾选的类型一样，只是短一点
ROOT = HERE.parent


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(url, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            requests.get(url, timeout=1)
            return
        except requests.RequestException:
            time.sleep(0.2)
    raise RuntimeError(f"{url} 没起来")


def events(base, jid, stop, timeout=120):
    """读 SSE，直到 stop(ev) 为真；返回读到的全部事件。"""
    out = []
    with requests.get(f"{base}/api/jobs/{jid}/events", stream=True, timeout=timeout) as r:
        t0 = time.time()
        for line in r.iter_lines(decode_unicode=True):
            if time.time() - t0 > timeout:
                raise TimeoutError("等事件超时")
            if line and line.startswith("data: "):
                ev = json.loads(line[6:])
                out.append(ev)
                if stop(ev):
                    return out
    return out


def scan(base, **params):
    r = requests.post(f"{base}/api/scan", json=params)
    assert r.ok, r.text
    jid = r.json()["id"]
    evs = events(base, jid, lambda e: e["type"] == "status")
    files = {e["file"]["name"]: e["file"] for e in evs if e["type"] == "file"}
    return jid, files, evs


def dl(base, jid, since, **body):
    """发起下载，读事件直到这一批结束（dlrun idle）。返回 (每个文件最后的 dl 事件, 下一个 since, 事件)。"""
    r = requests.post(f"{base}/api/jobs/{jid}/download", json=body)
    assert r.ok and r.json()["queued"], r.text
    final, evs = {}, []
    with requests.get(f"{base}/api/jobs/{jid}/events?since={since}", stream=True, timeout=180) as resp:
        for line in resp.iter_lines(decode_unicode=True):
            if line and line.startswith("data: "):
                ev = json.loads(line[6:])
                evs.append(ev)
                if ev["type"] == "dl":
                    final[ev["fid"]] = ev
                if ev["type"] == "dlrun" and ev["status"] == "idle":
                    break
    return final, evs[-1]["seq"] + 1, evs


def login(base, origin, check):
    """无头登录：登录页 ?auto=1 自己提交，等跳到门户后点「我已登录」。"""
    requests.post(f"{base}/api/login", json={"url": f"{origin}/login?auto=1"})
    info = {}
    for _ in range(80):
        info = requests.get(f"{base}/api/login").json()
        if "/portal/" in info["session"]["url"]:
            break
        time.sleep(0.25)
    r = requests.post(f"{base}/api/login/finish", json={})
    check(r.ok and r.json()["saved"], f"登录并保存：{r.json().get('saved')}")
    return r.json()


def main():
    site_port, app_port = free_port(), free_port()
    out = Path(tempfile.mkdtemp(prefix="sc-web-test-"))
    site = subprocess.Popen([sys.executable, str(HERE / "fixture_site.py"), str(site_port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = dict(os.environ, SC_LOGIN_HEADLESS="1", STUDY_CRAWLER_HOME=str(out.parent / (out.name + "-home")))
    web = subprocess.Popen([sys.executable, str(ROOT / "web_app.py"), "--port", str(app_port), "--out", str(out),
                            "--no-browser"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    base, course = f"http://127.0.0.1:{app_port}", f"http://127.0.0.1:{site_port}/course/"
    ok = True

    def check(cond, msg):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + msg)
        ok &= bool(cond)

    try:
        wait_up(f"http://127.0.0.1:{site_port}/robots.txt")
        wait_up(f"{base}/api/config")

        print("安全")
        check(requests.get(f"{base}/api/config", headers={"Host": "evil.example"}).status_code == 403, "非本机 Host 被拒绝")
        check(requests.post(f"{base}/api/scan", data={"url": course}).status_code == 415, "非 JSON 的 POST 被拒绝")
        r = requests.post(f"{base}/api/scan", json={"url": "网站"})
        check(r.status_code == 400 and "不是网址" in r.json()["error"], f"无效输入给出提示：{r.json().get('error')}")

        print("静态扫描（js=off）")
        t0 = time.time()
        _, static, _ = scan(base, url=course, depth=1, js="off", delay=0, ext=EXTS)
        check(len(static) == 7, f"找到 {len(static)} 个文件（{time.time() - t0:.1f}s）：{sorted(static)}")

        print("动态扫描（js=auto + 点击下载按钮）")
        t0 = time.time()
        jid, files, evs = scan(base, url=course, depth=1, js="auto", js_click=True, delay=0, ext=EXTS)
        names = set(files)
        print(f"    {len(files)} 个文件（{time.time() - t0:.1f}s）：{sorted(names)}")
        for want in ("第3章 栈与队列.pdf", "答案.zip", "期末复习.pdf", "ch2.pptx", "report.docx"):
            check(want in names, f"JS 才能发现：{want}")
        check(not any(n.startswith("实验报告模板") for n in names), "JSON 里的 name 字段没被当成地址")
        check(any(e["type"] == "log" for e in evs), "日志通过 SSE 推送")
        pages = [e for e in evs if e["type"] == "page"]
        check(len(pages) == 10, f"扫描了 {len(pages)} 个网页")

        print("全站扫描（深度 = 全站：不限层数 + 读 sitemap + 自动翻页）")
        t0 = time.time()
        _, whole, evs = scan(base, url=course, depth="all", js="auto", js_click=True, delay=0, ext=EXTS)
        print(f"    {len(whole)} 个文件（{time.time() - t0:.1f}s）")
        for want in ("notes.pdf", "deep.pdf", "syllabus.pdf", "p3-b.pdf", "feed6.pdf", "第3页答案.pdf"):
            check(want in whole, f"全站才能发现：{want}")
        check(names <= set(whole), "1 层能找到的，全站都找到了")
        summ = next(e for e in evs if e["type"] == "summary")
        check(summ["capped"] == 0, f"没到网页数上限：{summ}")
        _, _, evs = scan(base, url=course, depth="all", js="off", delay=0, ext=EXTS, max_pages=3)
        summ = next(e for e in evs if e["type"] == "summary")
        check(summ["capped"] > 0 and summ["max_pages"] == 3, f"到了网页数上限，告诉前端还剩 {summ['capped']} 个没扫")

        print("转发下载（不落盘）")
        r = requests.get(f"{base}/api/jobs/{jid}/files/{files['实验指导书.pdf']['fid']}")
        check(r.ok and r.headers["Content-Type"] == "application/pdf" and len(r.content) == 80_000,
              f"Content-Disposition 文件名：{r.headers.get('Content-Disposition')}")
        check("filename*=UTF-8''%E5%AE%9E%E9%AA%8C" in r.headers.get("Content-Disposition", ""), "中文文件名按 RFC 5987 编码")
        r = requests.get(f"{base}/api/jobs/{jid}/files/{files['secret.pdf']['fid']}")
        check(r.status_code == 502 and "登录" in r.text, "需要登录的文件给出提示")
        r = requests.get(f"{base}/api/jobs/{jid}/files/{files['ch1.pdf']['fid']}?inline=1")
        check(r.headers["Content-Disposition"].startswith("inline"), "PDF 可以内联预览")

        print("批量下载到本机")
        fids = [f["fid"] for f in files.values()]
        r = requests.post(f"{base}/api/jobs/{jid}/download", json={"fids": fids})
        check(r.json()["queued"] == len(fids), f"排队 {r.json()['queued']} 个")
        since = max(e["seq"] for e in evs) + 1
        evs2 = []
        with requests.get(f"{base}/api/jobs/{jid}/events?since={since}", stream=True, timeout=120) as resp:
            for line in resp.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    ev = json.loads(line[6:])
                    evs2.append(ev)
                    if ev["type"] == "dlrun" and ev["status"] == "idle":
                        break
        final = {}
        for e in evs2:
            if e["type"] == "dl":
                final[e["fid"]] = e
        by_name = {f["name"]: final.get(f["fid"], {}) for f in files.values()}
        done = [n for n, d in by_name.items() if d.get("status") == "done"]
        check(len(done) == len(files) - 1, f"成功 {len(done)} / {len(files)}")
        check(by_name["secret.pdf"].get("status") == "failed", f"secret.pdf 失败：{by_name['secret.pdf'].get('note')}")
        dup_notes = [by_name[n].get("note", "") for n in ("ch1.pdf", "ch1-copy.pdf")]
        check(sum("内容相同" in n for n in dup_notes) == 1, f"两个同内容 PDF 只留一份：{dup_notes}")
        check(any(e["type"] == "dl" and e["status"] == "running" for e in evs2), "有下载进度事件")
        on_disk = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and not p.name.startswith("."))
        print("    " + "\n    ".join(on_disk))
        check((out / "manifest.csv").exists(), "写了 manifest.csv")

        print("再次下载：识别已下载")
        r = requests.post(f"{base}/api/jobs/{jid}/download", json={"fids": fids})
        check(r.json()["queued"] == 1, f"只重新排队失败的那 1 个（实际 {r.json()['queued']}）")

        print("ZIP")
        r = requests.get(f"{base}/api/jobs/{jid}/zip")
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        check(zf.testzip() is None and len(zf.namelist()) == len(done) - 1,
              f"ZIP 有效，{len(zf.namelist())} 个文件（重复的只打一份）")
        zp = out / "t.zip"
        zp.write_bytes(r.content)
        res = subprocess.run(["unzip", "-tq", str(zp)], capture_output=True, text=True)
        check(res.returncode == 0, f"unzip -t：{res.stdout.strip() or res.stderr.strip()}")
        zp.unlink()

        print("单个文件转发：先校验内容")
        jv, vfiles, _ = scan(base, url=f"http://127.0.0.1:{site_port}/v/", depth=0, js="off", delay=0, ext=EXTS)
        r = requests.get(f"{base}/api/jobs/{jv}/files/{vfiles['fake.pdf']['fid']}")
        check(r.status_code == 502 and "文件不存在或已被删除" in r.text, "接口 JSON 错误不会被存成 fake.pdf")
        r = requests.get(f"{base}/api/jobs/{jv}/files/{vfiles['expired.pdf']['fid']}")
        check(r.status_code == 502 and "需要登录" in r.text, "没登录过：提示「在浏览器里登录」")
        r = requests.get(f"{base}/api/jobs/{jv}/files/{vfiles['real.pdf']['fid']}")
        check(r.ok and r.content.startswith(b"%PDF-") and len(r.content) == 30_009, f"真 PDF 完整转发：{len(r.content)} 字节")

        print("浏览器登录 + 带 token 的单页应用")
        origin = f"http://127.0.0.1:{site_port}"
        portal = f"{origin}/portal/#/course/42"
        _, before, _ = scan(base, url=portal, depth=0, js="auto", delay=0, ext=EXTS)
        check(not before, f"未登录：找到 {len(before)} 个文件")
        r = requests.post(f"{base}/api/login", json={"url": f"{origin}/login?auto=1"})
        check(r.ok and r.json()["session"]["status"] in ("starting", "open"), f"打开登录窗口：{r.json()['session']}")
        info = {}
        for _ in range(80):
            info = requests.get(f"{base}/api/login").json()
            if "/portal/" in info["session"]["url"]:
                break
            time.sleep(0.25)
        check(info["session"]["url"].endswith("/portal/#/course/42"), f"跟踪到登录后停留的页面：{info['session']['url']}")
        r = requests.post(f"{base}/api/login/finish", json={})
        info = r.json()
        check(r.ok and info["saved"], f"保存登录状态：{info.get('saved')}")
        state_file = Path(info["saved"]["path"])
        state = json.loads(state_file.read_text(encoding="utf-8"))
        check(any(c["name"] == "sid" for c in state["cookies"]), "cookie 已保存")
        check(any(i["name"] == "token" for o in state["origins"] for i in o["localStorage"]), "localStorage 里的 token 已保存")
        check(state_file.stat().st_mode & 0o777 == 0o600, f"状态文件权限 {oct(state_file.stat().st_mode & 0o777)}")

        # delay=0.5：下载时排在后面的文件还在等限速，暂停时也得一起标成「等重新登录」
        jid2, after, evs = scan(base, url=info["session"]["url"], depth=0, js="auto", delay=0.5, ext=EXTS)
        want = {"第1讲 信号的分类.pdf", "期中试卷.pdf", "实验三 卷积.docx"}
        check(set(after) == want, f"登录后找到：{sorted(after)}")
        meta = next(e for e in evs if e["type"] == "meta")
        check(meta.get("login") is True, "扫描标记为带登录状态")
        auth = [e for e in evs if e["type"] == "auth"]
        check(auth and "authorization" in auth[0]["headers"] and "T0KEN" not in json.dumps(evs, ensure_ascii=False),
              f"嗅探到请求头（只报名字，不把值发给前端）：{auth and auth[0]['headers']}")
        apis = [e for e in evs if e["type"] == "api"]
        check(any(a["url"].endswith("/api/portal/course/42/files") and a["links"] == 3 for a in apis),
              f"列出页面调用的接口：{[(a['url'].rsplit('/', 3)[-3:], a['links']) for a in apis]}")
        f = after["期中试卷.pdf"]
        r = requests.get(f"{base}/api/jobs/{jid2}/files/{f['fid']}")
        check(r.ok and r.headers["Content-Type"] == "application/pdf", f"转发下载带上嗅探到的 token：HTTP {r.status_code}")
        requests.post(f"{base}/api/jobs/{jid2}/download", json={"fids": [x["fid"] for x in after.values()]})
        since = max(e["seq"] for e in evs) + 1
        final = {}
        with requests.get(f"{base}/api/jobs/{jid2}/events?since={since}", stream=True, timeout=120) as resp:
            for line in resp.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["type"] == "dl":
                        final[ev["fid"]] = ev
                    if ev["type"] == "dlrun" and ev["status"] == "idle":
                        last_seq = ev["seq"]
                        break
        check(all(final.get(x["fid"], {}).get("status") == "done" for x in after.values()),
              f"批量下载带 token 的文件：{[(d['status'], d.get('note', '')) for d in final.values()]}")

        print("令牌持久化")
        home = Path(env["STUDY_CRAWLER_HOME"])
        tok_file = next((home / "tokens").glob("*.json"), None)
        check(tok_file and "T0KEN-42" in tok_file.read_text(encoding="utf-8")
              and tok_file.stat().st_mode & 0o777 == 0o600, f"嗅探到的令牌随登录状态保存（0600）：{tok_file}")
        r = requests.get(f"{base}/api/login", params={"url": portal})
        check(r.json()["saved"]["tokens"] == ["authorization"] and "T0KEN" not in r.text, "界面只拿到令牌名字，不拿值")
        cli_out = out.parent / (out.name + "-cli")
        res = subprocess.run([sys.executable, str(ROOT / "study_crawler.py"), f"{origin}/api/portal/file/2", "-o", str(cli_out),
                              "--delay", "0", "-q"], env=env, capture_output=True, text=True, timeout=60)
        got = list(cli_out.rglob("期中试卷.pdf"))
        check(res.returncode == 0 and got, f"新进程（重启后）直接下要令牌的文件：rc={res.returncode} {res.stderr.strip()[-200:]}")

        print("登录失效 → 暂停 → 重新登录 → 继续")
        fids2 = [x["fid"] for x in after.values()]
        for d in final.values():
            Path(d["abs"]).unlink()  # 删掉已下载的，下面重新下
        requests.post(f"{origin}/admin/expire")  # 服务器那边令牌过期
        since = last_seq + 1  # 跳过前面那一批下载的事件
        res2, since, evs3 = dl(base, jid2, since, fids=fids2)
        lost = [e for e in evs3 if e["type"] == "authlost"]
        check(all(res2[f]["status"] == "auth" for f in fids2) and lost and lost[0]["count"] == 3
              and "登录已失效" in lost[0]["msg"], f"整批暂停等重新登录：{[res2[f]['status'] for f in fids2]} {lost and lost[0]['msg']}")
        res2, since, evs3 = dl(base, jid2, since, fids=fids2[:1])
        check(res2[fids2[0]]["status"] == "auth" and any(e["type"] == "authlost" for e in evs3), "暂停期间新的一批直接挂起，不再撞服务器")
        res2, since, evs3 = dl(base, jid2, since, fids=fids2, after_login=True)
        check(all(res2[f]["status"] == "failed" and "重新登录后仍然" in res2[f]["note"] for f in fids2),
              f"没重新登录就点继续：记失败、不再无限暂停：{res2[fids2[0]].get('note')}")
        login(base, origin, check)
        check(not tok_file.exists(), "重新登录后旧令牌作废（文件删除）")
        res2, since, evs3 = dl(base, jid2, since, fids=fids2, after_login=True)
        check(all(res2[f]["status"] == "done" for f in fids2),
              f"重新登录后继续下载：{[(res2[f]['status'], res2[f].get('note', '')) for f in fids2]}")
        check(any("刷新接口令牌" in e.get("msg", "") for e in evs3) and tok_file.exists()
              and "T0KEN-42" not in tok_file.read_text(encoding="utf-8"), "重新渲染嗅探到新令牌并保存")

        r = requests.post(f"{base}/api/logout", json={"url": portal})
        check(r.ok and not r.json()["saved"] and not state_file.exists(), "退出登录删除状态文件")
        check(not tok_file.exists(), "退出登录同时删除令牌")
    finally:
        web.terminate()
        site.terminate()
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
