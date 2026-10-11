# school-news · 校园信息聚合

输入一个学校官网地址,自动识别站内的新闻 / 通知 / 公告栏目,抓取列表并按类别整理,点标题直接看正文。可以登录统一身份认证,访问登录后才看得到的校内系统。

**招标采购类内容一律过滤掉**(招标、投标、中标、采购、询价、磋商、成交、竞价、比选、招租、拍卖等),只留跟个人相关的信息。

School news & notice aggregator. Give it a university homepage URL; it discovers the news/notice sections, crawls them, categorizes the results, and opens full text on click. Optionally logs into the campus unified identity system (CAS) to reach pages behind the login wall. Tender/procurement notices are filtered out by default.

Windows · Python 3.13 · Flask + Playwright · [MIT License](LICENSE)

---

## 先说清楚:能用在哪,不能用在哪

这个项目**只有一处是学校专属的**:统一身份认证入口写的是东华大学的 `cas.dhu.edu.cn`。除此之外的栏目发现、列表提取、正文抽取、招标过滤全是通用的,任何学校官网都能用。

| 场景 | 要做什么 | 能不能用 |
|---|---|---|
| 抓公开新闻/通知 | 直接填网址 | ✅ 任何学校官网 |
| 抓东华校内系统(服务大厅、教务、邮箱) | 先连学校 SSLVPN 客户端,再登录 | ⚠️ 见下方[东华的网络结构](#东华的网络结构问题出在网络不在代码) |
| 抓其他学校的内网系统 | 改 `browser.py` 里的 `LOGIN_URL` | ⚠️ 各校认证方式和内网策略不同,要自己适配 |

**这不是一个绕过登录墙的工具。** 它不破解验证码、不猜密码、不伪造身份,只是把一个你已经有权访问的浏览器会话拿来复用。见[隐私与合规](#隐私与合规)。

> 截图占位:建议放三张 —— ① 抓取结果分类展示 ② 顶部登录条(已登录绿点) ③ 登录浏览器窗口停在统一身份认证页。放到 `docs/` 目录后用 `![说明](docs/xxx.png)` 引用。

---

## 快速开始

Windows 上双击 `start.bat`。首次运行会自动装依赖(从仓库内的 `wheels/` 离线安装),之后自动打开 `http://127.0.0.1:5000`。关掉命令行窗口即停止。

手动跑:

```bash
pip install -r requirements.txt
python app.py
```

macOS / Linux 同理,只是没有 `start.bat`,直接 `python app.py`。

登录功能需要本机装有 **Chrome**(没装则依次退回 Edge、Playwright 自带浏览器)。想用 Playwright 自带浏览器就执行:

```bash
python -m playwright install chromium
```

---

## 用法

### 一、抓公开内容

输入框填网址,点「开始抓取」。例如:

```
www.pku.edu.cn
www.hzau.edu.cn
news.dhu.edu.cn
```

已验证过:华中科技大学、华中农业大学、北京大学、四川大学、东华大学。

### 二、抓东华校内系统(需要 SSLVPN + 登录)

1. **先连上学校的 SSLVPN 客户端**(iSecSP,接入点 `vpn.dhu.edu.cn` / `vvpn.dhu.edu.cn`,登录要过企业微信里的二次认证验证码)。这步工具替不了你。
2. 点页面顶部「打开登录窗口」。
3. 弹出的 Chrome 窗口停在统一身份认证页。**在窗口里自己输账号密码。**
4. 登录成功后网页状态点变绿、显示「已登录」。
5. 勾上「使用登录态」,填校内地址(如 `ehall.dhu.edu.cn`),点「开始抓取」。

登录态存在 `browser_profile/` 目录,下次启动仍然有效,不用反复登录。过期时状态点变回灰色,再点一次「打开登录窗口」重新登录即可。

---

## 东华的网络结构:问题出在网络,不在代码

这是本项目踩坑最多的地方,直接给实测结论(2026-10-11,校外网络,浏览器已持有**有效的**统一身份认证登录态):

| 地址 | 结果 |
|---|---|
| `cas.dhu.edu.cn` | ✅ 正常,登录后停在自助服务台 `cas.dhu.edu.cn/selfcare/` |
| `webproxy.dhu.edu.cn` 根 / `/login` / `/login?cas_login=true` | ❌ `访问被拒绝` |
| `webvpn.dhu.edu.cn` 根 / `/login` / `/portal` | ❌ 整个域名都拒绝 |
| `ehall.dhu.edu.cn` | ❌ 跳 `webproxy.dhu.edu.cn/https/<编码>/`,同样被拒 |
| `jwc.dhu.edu.cn` 教务处 | ❌ 校外直连不通 |

**结论:WebVPN 这条路校外已经走不通了**,拿着有效登录态连它的登录页都出不来。东华信息办现行的方案是 SSLVPN 客户端,那是整机隧道,连上之后浏览器访问校内地址就通了 —— 本工具用 Playwright 开的 Chrome 会自然复用这条隧道。

所以校内系统抓不到时,先确认 SSLVPN 连上没有,再去怀疑代码。

另外:不要自己手拼 `webproxy.dhu.edu.cn/https/<编码>/...` 这种重写地址,实测校外一律被拒。

> 如果哪天 WebVPN 恢复了,改 `browser.py` 顶部的 `LOGIN_URL` 即可,其余逻辑不用动。

---

## 原理

1. 抓首页,从所有链接里挑出名字含「新闻/要闻/动态」「通知/公告」「选课/放假/成绩…」的作为栏目。
2. 栏目名或标题命中招标采购关键词的直接丢弃。
3. 子域名算同一所学校(`news.xxx.edu.cn` 和 `www.xxx.edu.cn` 都认,按可注册域名 `tld` 比较)。
4. 并发抓每个栏目页,提取列表标题和日期;带日期的条目够多时,不带日期的链接按导航丢弃。
5. 按日期倒序合并,同一标题同一天只留一条。
6. 点标题时再单独抓一次正文,用 trafilatura 提取。

公开站走 `requests`(快);勾了「使用登录态」走 Playwright 真浏览器,能等 JS 渲染完。

Playwright 的同步 API 绑定创建它的线程、不能跨线程调用,所以 `browser.py` 把浏览器关在一个专用线程里,外部通过 `queue.Queue` 投递任务、用 `threading.Event` 等结果。

---

## 已知限制

- **纯前端渲染(SPA)的站点,公开抓取抓不到**,会提示「未识别到栏目」。勾「使用登录态」可以绕过。
- 教务系统若是老式 iframe 或弹窗结构,列表提取可能不准,需要针对该页调选择器。
- 不翻页,每个栏目最多取前 40 条(`crawler.py` 的 `crawl_site(max_items_per_column=...)`)。
- 登录态过期无法自动续期,要手动重登一次(通常能撑几天)。
- 登录态抓取是串行的(共用一个浏览器窗口),栏目多时比较慢,可能要一分钟。
- 登录窗口只点一次。已停在登录页时再点会重新导航,冲掉你正在输的内容。
- 一切只在本机。

---

## 排错

### 看某个地址到底跳到了哪

```bash
curl -sX POST http://127.0.0.1:5000/probe \
  -H "Content-Type: application/json" \
  -d '{"url":"https://ehall.dhu.edu.cn"}'
```

会在登录浏览器里打开这个地址,回传最终 URL、页面标题和可见文字。判断「是被拒绝还是没登录」看它最快。前端没做这个按钮,纯排错用。

### 命令行自测

```bash
python crawler.py https://news.dhu.edu.cn
python crawler.py https://www.dhu.edu.cn --personal
```

打印识别到的栏目、抓到的标题和每条失败原因。`--personal` 按登录后的门户页处理,额外识别「选课/放假/成绩」这类个人栏目。

### 改了代码但行为没变

Windows 上停掉后台服务**不会杀掉真正的 python 进程**,它还占着 5000 端口,于是你以为在跑新代码、其实请求被旧进程接走了。先杀干净再起:

```powershell
Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

### 装不上依赖

有人会遇到 pip 被本机系统代理拦掉(返回 403),报错却伪装成「找不到某个包」,很容易误判。仓库里的 `wheels/` 已放好全部依赖,`start.bat` 用 `--no-index` 从本地装,不受代理影响。

`wheels/` 缺文件时(换了机器或换了 Python 版本):

```bash
python download_wheels.py
```

它显式禁用代理直连清华镜像。提示缺哪个包就把包名加进 `download_wheels.py` 顶部的 `PACKAGES`;某个包太新装不上时(比如 playwright 要 `pyee<14`),在 `VERSION_CAP` 里给它加个上限。

---

## 隐私与合规

**工具不接触你的密码。** 你在浏览器窗口里输的账号密码只提交给学校自己的登录页;本项目不读输入框、不写配置文件、不记日志,只复用浏览器自带的登录态。

**`browser_profile/` 目录里是真实的登录 Cookie。** 它等同于你的账号凭据 —— 已经加进 `.gitignore`,提交前请再确认一次 `git status` 里没有它。想清除就点页面上的「退出登录」,或直接删掉这个目录。

本项目按「抓取公开信息 + 复用本人账号会话」设计,请遵守:

- 只抓你有权访问的页面,不要拿它去探测别人看不到的东西。
- 不要用来批量刷站、压测,或做成公开服务对外提供。
- 使用前看清学校的信息安全规定和相关法律法规,后果自负。

作者不对任何滥用行为负责。

---

## 文件说明

| 文件 | 作用 |
|---|---|
| `start.bat` | Windows 一键启动 |
| `app.py` | Flask 后端:抓取、正文、登录、诊断接口 |
| `crawler.py` | 爬虫核心,过滤规则和抓取器抽象;可单独命令行跑 |
| `browser.py` | 登录态浏览器(Playwright),跑在独立线程里 |
| `templates/index.html` | 前端页面,单文件无构建 |
| `download_wheels.py` | 离线依赖下载脚本(应急用) |
| `wheels/` | 离线依赖包 |
| `browser_profile/` | 登录态,首次登录后自动生成,**已被 git 忽略** |

改抓取规模看 `crawler.py` 顶部:`TIMEOUT`(单请求超时)、`MAX_COLUMNS`(最多栏目数)、`MAX_WORKERS`(并发数)。

---

## License

[MIT](LICENSE)
