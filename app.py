"""Flask 后端:提供页面、抓取接口和统一身份认证登录。

启动:
    python app.py
然后浏览器打开 http://127.0.0.1:5000
"""

from __future__ import annotations

import urllib.parse

from flask import Flask, jsonify, render_template, request

from browser import BrowserFetcher, get_worker
from crawler import (
    CATEGORY_NEWS,
    CATEGORY_NOTICE,
    CATEGORY_OTHER,
    CATEGORY_PERSONAL,
    crawl_site,
    extract_detail,
    find_columns,
    fetch,
)

app = Flask(__name__)


def normalize_url(raw: str) -> str:
    """补全协议,拒绝非 http(s) 地址。"""
    url = (raw or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        return ""
    return url


def payload() -> dict:
    return request.get_json(silent=True) or {}


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/crawl")
def crawl():
    data = payload()
    url = normalize_url(data.get("url", ""))
    if not url:
        return jsonify({"error": "网址无效,请填写完整域名,例如 https://www.example.edu.cn"}), 400

    use_login = bool(data.get("use_login"))
    fetcher = BrowserFetcher() if use_login else None

    result = crawl_site(url, fetcher=fetcher, personal=use_login)
    result["categories"] = [CATEGORY_PERSONAL, CATEGORY_NEWS, CATEGORY_NOTICE, CATEGORY_OTHER]
    result["used_login"] = use_login
    return jsonify(result)


@app.post("/detail")
def detail():
    data = payload()
    url = normalize_url(data.get("url", ""))
    if not url:
        return jsonify({"error": "网址无效"}), 400
    fetcher = BrowserFetcher() if data.get("use_login") else None
    return jsonify(extract_detail(url, fetcher))


@app.post("/discover")
def discover():
    """登录后,列出当前页面上能识别到的系统入口,供用户挑选。"""
    data = payload()
    url = normalize_url(data.get("url", "")) or "https://ehall.dhu.edu.cn/"
    try:
        fetcher = BrowserFetcher()
        soup, base = fetch(url, fetcher)
    except Exception as exc:  # noqa: BLE001 - 浏览器抓取的各种失败统一返回给前端
        return jsonify({"error": f"读取页面失败:{exc}"}), 502

    columns = find_columns(soup, base, personal=True)
    return jsonify({"url": base, "columns": columns})


# ---------- 统一身份认证 ----------


@app.post("/login/start")
def login_start():
    try:
        return jsonify(get_worker().start_login())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"打开登录窗口失败:{exc}"}), 502


@app.get("/login/status")
def login_status():
    try:
        return jsonify(get_worker().status())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"logged_in": False, "error": str(exc)})


@app.post("/probe")
def probe():
    """诊断用:在登录浏览器里打开给定地址,看跳转到哪、页面标题是什么。"""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "缺少 url"}), 400
    url = normalize_url(url)
    try:
        return jsonify(get_worker().probe(url))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


@app.post("/logout")
def logout():
    try:
        return jsonify(get_worker(reset=True).close())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 502


if __name__ == "__main__":
    print("学校信息抓取工具已启动,浏览器打开 http://127.0.0.1:5000")
    # threaded=True:登录浏览器要长时间运行,不能被其他请求堵住
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
