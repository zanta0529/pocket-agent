import logging
import re
from urllib.parse import urlparse

import httpx

log = logging.getLogger("pocket-agent")

WEB_TOOLS = [
    {
        "type": "function",
        "name": "search_web",
        "description": (
            "Perform a lightweight web search using DuckDuckGo to find information, documentation, or solutions."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query."}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "read_url_content",
        "description": (
            "Fetch the content of a specific URL and extract its text. Useful for reading documentation or articles."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The absolute URL to fetch."}},
            "required": ["url"],
        },
    },
]


def is_web_tool(name: str) -> bool:
    return any(t["name"] == name for t in WEB_TOOLS)


def _strip_html(html_str: str) -> str:
    """簡易移除 HTML 標籤並抽取純文字 (避免 bs4 依賴)"""
    from html.parser import HTMLParser

    class MLStripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self.reset()
            self.strict = False
            self.convert_charrefs = True
            self.text = []
            self.in_style_script = False

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style"):
                self.in_style_script = True

        def handle_endtag(self, tag):
            if tag in ("script", "style"):
                self.in_style_script = False

        def handle_data(self, d):
            if not self.in_style_script:
                self.text.append(d)

        def get_data(self):
            return "".join(self.text)

    try:
        s = MLStripper()
        s.feed(html_str)
        text = s.get_data()
        # 整理連續空白
        return re.sub(r"\n\s*\n", "\n\n", text).strip()
    except Exception:
        # Fallback regex fallback
        return re.sub(r"<[^>]+>", " ", html_str)


def call_web_tool(name: str, args: dict) -> str:
    """執行輕量級網路工具。"""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }

        if name == "search_web":
            query = args.get("query", "")
            if not query:
                return "[ERROR] 缺少必填參數 'query'"

            # DuckDuckGo HTML 介面
            url = "https://html.duckduckgo.com/html/"
            resp = httpx.post(url, data={"q": query}, headers=headers, follow_redirects=True, timeout=15.0)
            resp.raise_for_status()

            # 手動解析 DuckDuckGo 結果 (提取 .result__snippet 和 .result__url)
            html = resp.text
            results = []

            # 使用 Regex 解析基本內容 (因無法依賴 bs4)
            snippets = re.findall(r'<a class="result__snippet[^>]*>(.*?)</a>', html, re.DOTALL)
            urls = re.findall(r'<a class="result__url" href="([^"]+)">', html)

            for i in range(min(len(snippets), 5, len(urls))):
                text = _strip_html(snippets[i]).strip()
                link = urls[i].strip()
                # 處理 DDG 的跳轉連結
                if link.startswith("//duckduckgo.com/l/?"):
                    import urllib.parse

                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
                    if "uddg" in parsed:
                        link = parsed["uddg"][0]
                results.append(f"{i + 1}. {text}\n   URL: {link}")

            if not results:
                return "搜尋無結果，或遭到搜尋引擎阻擋。"
            return "搜尋結果 (DuckDuckGo)：\n\n" + "\n\n".join(results)

        elif name == "read_url_content":
            url = args.get("url", "")
            if not url:
                return "[ERROR] 缺少必填參數 'url'"

            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return "[ERROR] 僅支援 http / https 網址"

            resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=15.0)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                text = _strip_html(resp.text)
            elif "application/json" in content_type:
                text = resp.text
            else:
                # 純文字或其他
                text = resp.text

            # 限制長度避免爆 Context
            max_len = 15000
            if len(text) > max_len:
                return text[:max_len] + f"\n\n... (內容過長，已截斷前 {max_len} 字元)"
            return text

        return f"[ERROR] 未知的工具名稱: {name}"

    except Exception as e:
        log.info(f"Web Tool {name} 執行失敗: {e}")
        return f"[ERROR] 網路操作失敗: {e}"
