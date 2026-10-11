"""通用学校网站爬虫。

输入一个学校官网地址,自动识别新闻/通知类栏目,抓取列表和正文。
不依赖任何具体学校的页面结构,尽量靠通用启发式规则。

命令行自测:
    python crawler.py https://www.example.edu.cn
"""

from __future__ import annotations

import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:  # trafilatura 缺失时降级用内置正文提取
    trafilatura = None

try:
    from tld import get_fld
except ImportError:
    get_fld = None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

TIMEOUT = 15          # 单个请求超时(秒)
MAX_COLUMNS = 12      # 最多抓取的栏目数,防止超时
MAX_WORKERS = 5       # 并发抓栏目数

CATEGORY_NEWS = "新闻动态"
CATEGORY_NOTICE = "通知公告"
CATEGORY_PERSONAL = "个人相关"
CATEGORY_OTHER = "其他"

# 栏目名关键词分类
NEWS_WORDS = ("新闻", "要闻", "动态", "快讯", "头条", "资讯", "媒体", "综合新闻")
NOTICE_WORDS = ("通知", "公告", "公示", "通告")

# 用户明确不要的内容:招标采购类,一律过滤
EXCLUDE_WORDS = (
    "招标", "投标", "中标", "采购", "询价", "竞争性磋商", "磋商", "成交", "竞价",
    "比选", "资格预审", "供应商", "招租", "出让", "拍卖", "资产处置", "流标",
    "废标", "标段", "评标",
)

# 登录后才看得到的个人相关栏目(选课、放假、成绩、奖助学金等)
PERSONAL_WORDS = (
    "通知", "公告", "教务", "教学", "选课", "课程", "考试", "成绩", "学籍",
    "放假", "假期", "校历", "邮箱", "邮件", "账号", "密码", "一卡通", "校园卡",
    "奖助", "奖学金", "助学金", "评优", "留学", "交换", "交流项目", "学业警告",
    "培养", "学位", "毕业", "评教", "消息", "提醒", "公告栏",
)

# 明显不是内容栏目的链接文本
NOISE_WORDS = {
    "english", "english version", "网站地图", "联系我们", "联系", "登录", "登陆",
    "map", "留言", "rss", "订阅", "首页", "返回首页", "更多", "more", ">>",
    "设为首页", "加入收藏", "打印", "关闭", "后台", "管理", "招生", "就业",
    "图书馆", "注册", "忘记密码",
}

# 日期正则:2024-09-01 / 2024/09/01 / 2024.09.01 / 2024年9月1日
DATE_RE = re.compile(r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)")


def _decode(resp: requests.Response) -> str:
    """按 响应头 charset -> apparent_encoding -> utf-8 -> gbk 顺序解码。"""
    if resp.encoding and resp.encoding.lower() not in ("iso-8859-1", "ascii"):
        try:
            return resp.content.decode(resp.encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    enc = resp.apparent_encoding
    if enc:
        try:
            return resp.content.decode(enc, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return resp.text


class FetchError(Exception):
    """抓取失败。两种抓取器都抛这个,调用方只需处理一种异常。"""


class RequestsFetcher:
    """直接 HTTP 抓取。公开站用这个,可并发,快。"""

    def __init__(self, timeout: int = TIMEOUT) -> None:
        self.timeout = timeout

    def fetch(self, url: str) -> tuple[BeautifulSoup, str]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc
        return BeautifulSoup(_decode(resp), "lxml"), resp.url


_default_fetcher = RequestsFetcher()


def fetch(url: str, fetcher=None) -> tuple[BeautifulSoup, str]:
    """抓取页面,返回 (soup, 最终地址)。失败抛 FetchError。"""
    return (fetcher or _default_fetcher).fetch(url)


def _host(url: str) -> str:
    """取主机名:去掉端口,去掉开头的 www.。"""
    host = urllib.parse.urlparse(url).netloc.split(":")[0].lower()
    return host[4:] if host.startswith("www.") else host


def _root_domain(url: str) -> str:
    """取可注册域名,子域名视为同站(www.x.edu.cn 与 news.x.edu.cn 同校)。"""
    if get_fld is not None:
        try:
            fld = get_fld(url, fail_silently=True)
            if fld:
                return fld
        except Exception:  # noqa: BLE001 - tld 数据缺失时退回主机名比较
            pass
    return _host(url)


def _same_site(url: str, base: str) -> bool:
    """同校判断:允许新闻站挂在 news.xxx.edu.cn 这类子域名上。"""
    if not urllib.parse.urlparse(url).netloc:
        return True
    return _root_domain(url) == _root_domain(base)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_excluded(text: str) -> bool:
    """招标采购类内容,用户明确不要。"""
    return any(w in text for w in EXCLUDE_WORDS)


def _classify(text: str, personal: bool = False) -> str:
    if is_excluded(text):
        return CATEGORY_OTHER
    if any(w in text for w in NOTICE_WORDS):
        return CATEGORY_NOTICE
    if personal and any(w in text for w in PERSONAL_WORDS):
        return CATEGORY_PERSONAL
    if any(w in text for w in NEWS_WORDS):
        return CATEGORY_NEWS
    return CATEGORY_OTHER


def find_columns(soup: BeautifulSoup, base_url: str, personal: bool = False) -> list[dict]:
    """从页面所有链接里挑出新闻/通知类栏目入口。

    personal=True 时用于登录后的门户页,额外认「选课/放假/成绩」这类个人栏目,
    但仍会剔除招标采购。
    """
    seen: set[str] = set()
    columns: list[dict] = []

    for a in soup.find_all("a", href=True):
        text = _clean(a.get_text(" ", strip=True))
        if not text or len(text) > 24:
            continue
        if text.lower() in NOISE_WORDS:
            continue
        if is_excluded(text):
            continue
        # 栏目名必须命中关键词,否则跳过
        category = _classify(text, personal=personal)
        if category == CATEGORY_OTHER:
            continue

        href = urllib.parse.urljoin(base_url, a["href"].strip())
        if not href.startswith(("http://", "https://")):
            continue
        if not _same_site(href, base_url):
            continue
        # 页内锚点(#go5 之类)本身不是独立栏目,但常常指向首页上的新闻区块,
        # 所以回落到抓首页本身,由 extract_list 去噪。
        if href.split("#")[0].rstrip("/") == base_url.split("#")[0].rstrip("/"):
            href = href.split("#")[0] or base_url
        key = href.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        columns.append({"name": text, "category": category, "url": href})

    # 个人相关最优先,其次通知,再新闻;同类按名称稳定排序,结果可复现
    order = {CATEGORY_PERSONAL: 0, CATEGORY_NEWS: 1, CATEGORY_NOTICE: 2, CATEGORY_OTHER: 3}
    columns.sort(key=lambda c: (order[c["category"]], c["name"]))
    return columns[:MAX_COLUMNS]


TRAILING_DATE_RE = re.compile(
    r"""[\s（(\[【]*(
        20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?
      | \d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?
    )\s*[)）\]】]*\s*$""",
    re.VERBOSE,
)


LEADING_DATE_RE = re.compile(
    r"""^\s*[（(\[【]*(
        20\d{2}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?
      | \d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?
    )\s*[)）\]】]*\s*""",
    re.VERBOSE,
)


def _strip_trailing_date(title: str) -> str:
    """列表标题常把日期一起包在 <a> 里,去掉头部和尾部的日期。"""
    return LEADING_DATE_RE.sub("", TRAILING_DATE_RE.sub("", title)).strip()


def _find_date(container) -> str:
    """在列表项容器里找日期,返回 YYYY-MM-DD,找不到返回空串。"""
    text = container.get_text(" ", strip=True) if container else ""
    if not text:
        return ""
    m = DATE_RE.search(text)
    if m:
        y, mo, d = m.groups()
        try:
            return datetime(int(y), int(mo), int(d)).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    m = SHORT_DATE_RE.search(text)
    if m:
        mo, d = m.groups()
        try:  # 无年份的短日期,按当前年份补全;若比今天晚则视为去年
            today = datetime.now()
            dt = datetime(today.year, int(mo), int(d))
            if dt.date() > today.date():
                dt = datetime(today.year - 1, int(mo), int(d))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def extract_list(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """从栏目页提取列表项 {title, url, date}。"""
    items: list[dict] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        title = _strip_trailing_date(_clean(a.get_text(" ", strip=True)))
        # 标题长度过滤:太短是导航,太长多半不是列表标题
        if not (5 <= len(title) <= 100):
            continue
        if title.lower() in NOISE_WORDS:
            continue
        if is_excluded(title):
            continue
        # 必须是空链接文本的锚点,排除纯图标链接
        href = urllib.parse.urljoin(base_url, a["href"].strip())
        if not href.startswith(("http://", "https://")):
            continue
        if href.rstrip("/") == base_url.rstrip("/"):
            continue

        container = a.find_parent(["li", "tr", "dd", "div"]) or a.parent
        date = _find_date(container)

        key = href.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        items.append({"title": title, "url": href, "date": date})

    # 有日期的按日期倒序在前,无日期的保持原页面顺序排在后面
    dated = [i for i in items if i["date"]]
    undated = [i for i in items if not i["date"]]
    dated.sort(key=lambda i: i["date"], reverse=True)

    # 页面上已有一批带日期的条目时,说明这是真正的列表页,
    # 剩下来的无日期链接基本是导航/侧栏,直接丢弃。
    if len(dated) >= 5:
        return dated
    return dated + undated


def extract_detail(url: str, fetcher=None) -> dict:
    """抓取正文,返回 {title, text, url}。失败返回 text 为空。"""
    try:
        soup, final_url = fetch(url, fetcher)
    except FetchError as exc:
        return {"title": "", "text": "", "url": url, "error": str(exc)}

    title = ""
    if soup.title and soup.title.string:
        title = _clean(soup.title.string)
    h1 = soup.find("h1")
    if h1 and _clean(h1.get_text()):
        title = _clean(h1.get_text())

    text = ""
    if trafilatura is not None:
        try:
            text = trafilatura.extract(soup.prettify(), include_comments=False) or ""
        except Exception:  # noqa: BLE001 - 第三方库内部异常,降级处理
            text = ""

    if not text:
        text = _fallback_text(soup)

    return {"title": title, "text": text.strip(), "url": final_url}


def _fallback_text(soup: BeautifulSoup) -> str:
    """trafilatura 不可用时的兜底:剥掉脚本/导航,取正文最多的文本块。"""
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "iframe"]):
        tag.decompose()
    best = ""
    for block in soup.find_all(["article", "div", "section", "td"]):
        t = block.get_text("\n", strip=True)
        if len(t) > len(best):
            best = t
    if not best:
        best = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", best)


def crawl_site(
    url: str,
    max_items_per_column: int = 40,
    fetcher=None,
    personal: bool = False,
) -> dict:
    """完整流程:首页 -> 找栏目 -> 并发抓列表 -> 分类聚合。

    fetcher 为 None 时用 RequestsFetcher 抓公开站;传 BrowserFetcher 则走登录态。
    personal=True 时额外识别选课/放假/成绩这类个人栏目。
    """
    fetcher = fetcher or _default_fetcher
    result = {
        "site": url,
        "columns": [],
        "items": [],
        "errors": [],
        "counts": {CATEGORY_PERSONAL: 0, CATEGORY_NEWS: 0, CATEGORY_NOTICE: 0, CATEGORY_OTHER: 0},
    }

    try:
        home_soup, base_url = fetch(url, fetcher)
    except FetchError as exc:
        result["errors"].append(f"打开首页失败: {exc}")
        return result

    result["site"] = base_url
    columns = find_columns(home_soup, base_url, personal=personal)

    if not columns:
        result["errors"].append(
            "未识别到新闻/通知栏目。该站可能是纯前端渲染(SPA),或不含常规新闻栏目,"
            "或栏目全被招标采购过滤规则挡掉了。"
        )
        return result

    result["columns"] = columns
    aggregated: list[dict] = []
    seen_urls: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()

    def work(col: dict) -> tuple[dict, list[dict]]:
        soup, _ = fetch(col["url"], fetcher)
        return col, extract_list(soup, col["url"])[:max_items_per_column]

    # 浏览器抓取器同时只能跑一个标签页队列,并发只会互相拖慢
    workers = 1 if fetcher is not _default_fetcher else MAX_WORKERS

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, col): col for col in columns}
        for fut in as_completed(futures):
            col = futures[fut]
            try:
                _, items = fut.result()
            except FetchError as exc:
                result["errors"].append(f"栏目「{col['name']}」抓取失败: {exc}")
                continue
            for it in items:
                key = it["url"].split("#")[0]
                # 同一标题同一天在多个栏目重复出现时只保留一条
                title_key = (it["title"], it["date"])
                if key in seen_urls or title_key in seen_titles:
                    continue
                seen_urls.add(key)
                seen_titles.add(title_key)
                it["category"] = col["category"]
                it["column"] = col["name"]
                aggregated.append(it)

    aggregated.sort(key=lambda i: i["date"] or "0000-00-00", reverse=True)
    for it in aggregated:
        result["counts"][it["category"]] = result["counts"].get(it["category"], 0) + 1

    result["items"] = aggregated
    return result


def _main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    personal = "--personal" in sys.argv
    if not args:
        print("用法: python crawler.py <学校网址> [--personal]")
        print("  --personal  按登录后的门户页处理,额外识别个人相关栏目")
        raise SystemExit(1)

    data = crawl_site(args[0], personal=personal)
    print(f"站点: {data['site']}")
    print(f"识别栏目 {len(data['columns'])} 个,抓到条目 {len(data['items'])} 条")
    for c in data["columns"]:
        print(f"  [{c['category']}] {c['name']} -> {c['url']}")
    for err in data["errors"]:
        print(f"  警告: {err}")
    print("-" * 60)
    for it in data["items"][:30]:
        print(f"{it['date'] or '          '}  [{it['category']}] {it['title']}")


if __name__ == "__main__":
    _main()
