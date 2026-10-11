"""从清华镜像下载依赖 wheel 到 wheels/,供离线安装使用。

本机的 Windows 系统代理会让 pip 收到 403,所以这里显式禁用代理直连。
缺少某个包时,把包名加进下面的 PACKAGES 列表再运行一次:

    python download_wheels.py

下载完成后用离线模式安装:

    python -m pip install --no-index --find-links wheels -r requirements.txt
"""

from __future__ import annotations

import pathlib
import re
import urllib.request
from html.parser import HTMLParser

INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple/{}/"
OUT = pathlib.Path(__file__).with_name("wheels")

PACKAGES = [
    "flask", "werkzeug", "jinja2", "itsdangerous", "click", "blinker", "markupsafe",
    "requests", "urllib3", "idna", "certifi", "charset-normalizer",
    "beautifulsoup4", "soupsieve", "typing-extensions",
    "trafilatura", "courlan", "htmldate", "justext", "tld", "python-dateutil", "six", "lxml",
    "babel", "dateparser", "pytz", "regex", "tzlocal", "lxml-html-clean", "tzdata",
    "playwright", "greenlet", "pyee",
]

# 预发布版本:rc1 / dev0 / alpha / beta / 7.0.0a3 / 1.2b1
PRERELEASE = re.compile(r"(rc\d*|dev\d*|alpha\d*|beta\d*|(?<=\d)[ab]\d+)", re.I)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for key, val in attrs:
                if key == "href":
                    self.hrefs.append(val)


def opener() -> urllib.request.OpenerDirector:
    # ProxyHandler({}) 显式禁用代理,不走注册表里的 Clash 代理
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    op.addheaders = [("User-Agent", "curl/8.5.0")]
    return op


# 个别包要锁大版本:playwright 依赖 pyee<14,取最新会装不上
VERSION_CAP = {"pyee": (13, 999)}


def pick(hrefs: list[str], cap: tuple | None = None) -> str | None:
    """挑出适合 Windows + CPython 3.13 的最高版本 wheel。"""
    candidates = []
    for href in hrefs:
        name = href.rsplit("/", 1)[-1].split("#")[0]
        if not name.endswith(".whl"):
            continue
        parts = name[:-4].split("-")
        if len(parts) < 5:
            continue
        version, tag = parts[1], "-".join(parts[2:])
        if PRERELEASE.search(version):
            continue
        # 纯 Python 包是 none-any;带二进制的要 win_amd64(cp313 或 abi3 或 py3-none)
        ok = tag.endswith("none-any") or (
            "win_amd64" in tag and ("cp313" in tag or "abi3" in tag or "py3" in tag)
        )
        if ok and cap and version_key(version) > cap:
            continue
        if ok:
            candidates.append((href, name, version))
    if not candidates:
        return None
    # 索引顺序不保证按版本递增,必须自己按版本号排序
    candidates.sort(key=lambda c: version_key(c[2]))
    return candidates[-1][0]


def version_key(version: str) -> tuple:
    """把版本号拆成整数元组用于比较,2.32.5 > 2.9.2。"""
    return tuple(int(n) for n in re.findall(r"\d+", version))


def main() -> None:
    OUT.mkdir(exist_ok=True)
    op = opener()
    for pkg in PACKAGES:
        url = INDEX.format(pkg)
        try:
            html = op.open(url, timeout=60).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            print(f"[跳过] {pkg}: 索引获取失败 {exc}")
            continue
        parser = LinkParser()
        parser.feed(html)
        href = pick(parser.hrefs, cap=VERSION_CAP.get(pkg))
        if not href:
            print(f"[跳过] {pkg}: 无匹配 wheel")
            continue
        file_url = urllib.parse.urljoin(url, href)
        name = file_url.rsplit("/", 1)[-1].split("#")[0]
        target = OUT / name
        if target.exists():
            print(f"[已存在] {name}")
            continue
        try:
            data = op.open(file_url, timeout=120).read()
            target.write_bytes(data)
            print(f"[下载] {name} ({len(data) // 1024} KB)")
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] {name}: {exc}")


if __name__ == "__main__":
    import urllib.parse  # noqa: F401

    main()
