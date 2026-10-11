"""带登录态的浏览器抓取。

东华大学的校内系统(服务大厅、教务、邮箱)都在统一身份认证后面,而且校外访问
还要经过 WebVPN(webproxy.dhu.edu.cn)。所以必须用真实浏览器:

- 用户在弹出的浏览器窗口里自己输账号密码,过验证码和短信。
- 本模块**不接触密码**,不保存账号信息,只用浏览器自带的登录态。
- 登录态存在 browser_profile/ 目录,下次启动直接复用。

Playwright 的同步 API 绑定创建它的线程,不能跨线程调用。这里用一个专用线程
跑所有浏览器操作,外部通过队列投递任务、等待结果。
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from bs4 import BeautifulSoup

from crawler import FetchError

# 东华大学统一身份认证入口。
# 注意:不能用 WebVPN 根地址 https://webproxy.dhu.edu.cn/ 当入口,
# 校外访问它只会返回「对不起,您没有权限访问该资源。」。cas.dhu.edu.cn 才是正门。
LOGIN_URL = "https://cas.dhu.edu.cn/"

PROFILE_DIR = Path(__file__).with_name("browser_profile")

# 登录页特征:URL 里带这些片段说明还没登录
LOGIN_URL_MARKERS = ("identity/login", "/cas/login", "/login?", "/login/")
# 登录后页面的 URL 特征(自助服务台、门户)
PORTAL_URL_MARKERS = ("/selfcare", "snack_router", "/portal", "/personal")
# 页面里出现这些词说明已经进去了
LOGGED_IN_MARKERS = ("退出", "注销", "登出", "个人中心", "我的应用", "服务大厅")
# 出现这些词说明被挡在门外(WebVPN 拒绝了未认证访问)
DENIED_MARKERS = ("没有权限访问该资源", "访问被拒绝", "请先登录", "登录超时")


class _Job:
    """投递给浏览器线程的一个任务。"""

    def __init__(self, fn) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result = None
        self.error: BaseException | None = None


class BrowserWorker:
    """把 Playwright 关在自己的线程里,外部只能通过 submit() 调它。"""

    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._context = None
        self._page = None

    # ---------- 线程管理 ----------

    def _ensure_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="browser-worker", daemon=True)
        self._thread.start()

    def submit(self, fn, timeout: float = 120):
        """把 fn 丢到浏览器线程执行,阻塞等结果。"""
        self._ensure_thread()
        job = _Job(fn)
        self._queue.put(job)
        if not job.done.wait(timeout):
            raise TimeoutError("浏览器操作超时")
        if job.error is not None:
            raise job.error
        return job.result

    def _loop(self) -> None:
        """浏览器线程的主循环。Playwright 对象只在这个线程里被碰。"""
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            while True:
                job = self._queue.get()
                if job.fn is None:  # 退出信号
                    break
                try:
                    job.result = job.fn()
                except BaseException as exc:  # noqa: BLE001 - 统一回传给调用方
                    job.error = exc
                finally:
                    job.done.set()
        finally:
            self._teardown()
            self._pw.stop()

    def _teardown(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # noqa: BLE001 - 关浏览器失败不影响退出
            pass
        self._context = None
        self._page = None

    # ---------- 浏览器同步操作(只在浏览器线程里跑) ----------

    def _alive(self) -> bool:
        """上下文还在不在。用户手动关掉浏览器窗口后,这个对象不会自动变 None,
        必须主动探一下,否则后面所有操作都会报 Target page ... has been closed。"""
        if self._context is None:
            return False
        try:
            self._context.pages  # 访问已关闭的上下文会抛异常
            return True
        except Exception:  # noqa: BLE001
            self._teardown()
            return False

    def _ensure_browser(self):
        """首次调用时开浏览器。用本机已装的 Chrome,不下载 Chromium。"""
        if self._alive():
            return self._context

        PROFILE_DIR.mkdir(exist_ok=True)
        last_error = None
        # 依次尝试本机 Chrome、Edge,最后才是 Playwright 自带浏览器
        for channel in ("chrome", "msedge", None):
            try:
                kwargs = {
                    "user_data_dir": str(PROFILE_DIR),
                    "headless": self.headless,
                    "no_viewport": True,
                    "args": ["--start-maximized"],
                }
                if channel:
                    kwargs["channel"] = channel
                self._context = self._pw.chromium.launch_persistent_context(**kwargs)
                break
            except Exception as exc:  # noqa: BLE001 - 换下一个浏览器继续试
                last_error = exc
        if self._context is None:
            raise FetchError(
                f"打不开浏览器(Chrome/Edge 都没成功):{last_error}。"
                "请确认本机装了 Chrome 或 Edge。"
            )

        self._context.set_default_timeout(30000)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._context

    def _goto(self, url: str, timeout: int = 60000):
        """打开页面。浏览器中途被关掉时重建一次再试,避免留下死上下文。"""
        self._ensure_browser()
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception:
            self._teardown()
            self._ensure_browser()
            self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return self._page

    def _settle(self, page) -> None:
        """等一下跳转和 JS 渲染。首屏常常还要再跳一次(比如 webproxy 转到登录页)。"""
        try:
            page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            return
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:  # noqa: BLE001 - 一直有请求在跑就算了,不阻塞
            pass

    def _logged_in_sync(self) -> tuple[bool, str]:
        """判登录态。先看否定证据,再看肯定证据,都没有就是未登录。

        踩过的两个坑:
        1. 不能拿正文里有没有「统一身份认证」判未登录。登录成功后 CAS 把人
           送到自助服务台(cas.dhu.edu.cn/selfcare/),那页 <title> 仍是
           「统一身份认证系统」,照这条否会一直显示未登录。
        2. 不能"没有登录页特征就算登录"。刚开浏览器时第一页是 about:blank,
           它既不是登录页也没有密码框,会被误判成已登录,于是不再导航。
           所以必须要有肯定证据,并且先排除空白页。
        """
        if not self._alive() or self._page is None:
            return False, ""
        try:
            url = self._page.url
        except Exception:  # noqa: BLE001 - 页面可能已关
            return False, ""

        low = url.lower()
        # 1. 空白页 / 浏览器内部页,还没导航到任何地方
        if not low or low == "about:blank" or low.startswith(("chrome://", "edge://")):
            return False, url
        # 2. 还挂在登录路由上
        if any(m in low for m in LOGIN_URL_MARKERS):
            return False, url
        # 3. 页面上还有**可见**的密码框。必须带 :visible——登录后的自助
        #    服务台里也有隐藏的改密表单,不加会把已登录误判成未登录。
        try:
            if self._page.query_selector("input[type=password]:visible"):
                return False, url
        except Exception:  # noqa: BLE001
            return False, url

        # 4. 肯定证据:URL 特征
        if any(m in low for m in PORTAL_URL_MARKERS):
            return True, url

        try:
            body = self._page.content()
        except Exception:  # noqa: BLE001
            return False, url
        # 5. 被 WebVPN 挡在门外
        if any(m in body for m in DENIED_MARKERS):
            return False, url
        # 6. 肯定证据:页面上的登录后元素
        if any(m in body for m in LOGGED_IN_MARKERS):
            return True, url
        return False, url

    # ---------- 对外接口 ----------

    def start_login(self) -> dict:
        """打开登录窗口并跳到统一身份认证页。用户自己输密码。"""

        def job():
            self._ensure_browser()
            # 已经开着且不是登录页时,只把窗口顶到前面,不要重新导航,
            # 否则用户正在输的账号密码会被冲掉。
            logged_in, _ = self._logged_in_sync()
            page = self._page if logged_in else self._goto(LOGIN_URL)
            try:  # 把窗口顶到最前面,否则可能开在别的窗口后面看不见
                page.bring_to_front()
            except Exception:  # noqa: BLE001
                pass
            self._settle(page)
            logged_in, url = self._logged_in_sync()
            return {"logged_in": logged_in, "url": url, "title": self._title()}

        return self.submit(job, timeout=150)

    def _title(self) -> str:
        try:
            return self._page.title()
        except Exception:  # noqa: BLE001
            return ""

    def status(self) -> dict:
        """查当前登录态。没开过浏览器就直接返回未登录,不启动新窗口。"""
        if not self._alive():
            return {"logged_in": False, "url": "", "running": False}

        def job():
            logged_in, url = self._logged_in_sync()
            return {"logged_in": logged_in, "url": url, "running": True, "title": self._title()}

        return self.submit(job, timeout=30)

    def fetch(self, url: str) -> tuple[BeautifulSoup, str]:
        """在登录态下打开页面,返回 (soup, 最终地址)。"""

        def job():
            page = self._goto(url, timeout=45000)
            self._settle(page)
            return page.content(), page.url

        try:
            html, final_url = self.submit(job, timeout=120)
        except FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - Playwright 异常统一转成 FetchError
            raise FetchError(str(exc)) from exc

        return BeautifulSoup(html, "lxml"), final_url

    def probe(self, url: str) -> dict:
        """诊断用:在登录浏览器里打开一个地址,回传最终 URL、标题和可见文字。
        排查 WebVPN/CAS 跳转链时用,比翻日志快。"""
        def job():
            page = self._goto(url, timeout=45000)
            self._settle(page)
            try:
                text = page.inner_text("body")[:1000]
            except Exception:  # noqa: BLE001
                text = ""
            return {"url": page.url, "title": self._title(), "text": text}

        return self.submit(job, timeout=150)

    def close(self) -> dict:
        """关掉浏览器和线程。"""
        if self._thread is None or not self._thread.is_alive():
            return {"closed": True, "was_running": False}
        self.submit(lambda: None, timeout=10)  # 确认线程还活着
        self._queue.put(_Job(None))
        self._thread.join(timeout=20)
        self._thread = None
        return {"closed": True, "was_running": True}


# 全局单例:Flask 多线程共享同一个浏览器
_worker: BrowserWorker | None = None
_lock = threading.Lock()


def get_worker(reset: bool = False) -> BrowserWorker:
    global _worker
    with _lock:
        if reset and _worker is not None:
            try:
                _worker.close()
            except Exception:  # noqa: BLE001
                pass
            _worker = None
        if _worker is None:
            _worker = BrowserWorker()
        return _worker


class BrowserFetcher:
    """给 crawler.crawl_site 用的抓取器,签名与 RequestsFetcher 一致。"""

    def __init__(self, worker: BrowserWorker | None = None) -> None:
        self.worker = worker or get_worker()

    def fetch(self, url: str) -> tuple[BeautifulSoup, str]:
        return self.worker.fetch(url)
