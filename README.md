# study-crawler · 学习资料批量下载爬虫

从课程主页或资料索引页出发，按层数爬取网页，自动找到并下载 PDF、PPT、Word、Excel、压缩包、电子书、ipynb 等资料。带命令行和网页界面两种用法。

## 功能

- **礼貌爬取**：多线程并发，同一主机默认每秒 1 个请求；遵守 robots.txt（含 Crawl-delay），遇到 429 自动降速，尊重 Retry-After
- **断点续传**：中断后再跑同一条命令即可继续；大文件用 HTTP Range + If-Range 续传，服务器上的文件更新过就整个重下
- **下载校验**：按文件头（magic bytes）确认文件类型；接口报错 JSON、错误页、登录页不会被存成文件
- **登录失效处理**：遇到 401 / 跳登录页时暂停整批、保留进度（退出码 3），重新登录后接着下
- **链接发现**：识别 `download.php?id=3` 这类无扩展名链接，从 Content-Disposition 取真实文件名并修复中文乱码；从 onclick、`<script>`、预览器参数里挖链接；支持 sitemap
- **翻页不算层**：“下一页”、页码链接沿用当前层数，`-d 1` 也能翻完分页列表
- **动态页面**（`--js`）：用 Playwright 渲染 Vue/React/AJAX 页面，自动点“加载更多 / 下一页”，收集接口 JSON 和网络请求里的文件地址
- **需要登录的平台**（`--login`）：弹出真浏览器让你自己登录（统一身份认证、扫码都行），之后自动带上登录状态；凭据只发给原站点，本地文件权限 0600
- SHA-256 去重、文件名清洗、正则包含/排除、大小上限、`--dry-run` 预览
- 输出 `manifest.csv`（下载清单）、`failed.txt`（失败清单）、`crawl.log`（详细日志）

## 安装

```bash
pip install -r requirements.txt
# 只有 --js / --login 需要浏览器：
pip install playwright && python -m playwright install chromium
```

## 用法

```bash
# 只下载课程页上直接链接的资料
python study_crawler.py https://example.edu/course/ds/ -d 0

# 爬 2 层，只要 pdf 和 pptx，且名字里含“课件”或“习题”
python study_crawler.py https://example.edu/course/ds/ -d 2 --ext pdf,pptx --include '课件|习题'

# 先预览会下载哪些文件
python study_crawler.py https://example.edu/course/ds/ --dry-run

# JS 动态加载的资料列表
python study_crawler.py https://spa.example.edu/#/course/42 --js auto --js-click

# 要登录的课程平台：先登录一次，以后自动带上
python study_crawler.py https://lms.example.edu/ --login --js auto
```

完整参数见 `python study_crawler.py --help`。

### 网页界面

```bash
python web_app.py        # 打开浏览器：输入网址 → 列出文件 → 勾选下载
```

## 测试

测试用 `tests/fixture_site.py` 在本机起一个模拟站点，不访问外网：

```bash
python tests/test_download.py   # 下载校验、断点续传、登录失效
python tests/test_fullsite.py   # 全站爬取、sitemap、翻页
python tests/test_web.py        # 网页界面端到端：扫描 → 下载 → ZIP → 浏览器登录 → token 过期后续传（需要 playwright）
```

## 使用须知

只用来下载你有权访问的学习资料。默认遵守 robots.txt 和限速，请不要调高并发去压别人的服务器，也不要把需要登录才能看的资料再公开分发。
