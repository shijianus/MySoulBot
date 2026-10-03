"""零依赖抓取与正文抽取。

只用标准库：本环境的 httpx 不可导入，而工具层要能在任何机器上 clone 即用。
"""

from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Final
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

BLOCKED_SCHEMES: frozenset[str] = frozenset({"file", "gopher", "data", "ftp", ""})
_SKIP_TAGS: frozenset[str] = frozenset({"script", "style", "noscript", "svg", "template", "head"})
_BLOCK_TAGS: frozenset[str] = frozenset({
    "p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "blockquote", "pre", "hr", "table",
})
_WS: re.Pattern[str] = re.compile(r"[ \t\u00a0]+")
_MANY_BLANK: re.Pattern[str] = re.compile(r"\n{3,}")
_TITLE_TAG: re.Pattern[str] = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


class FetchError(RuntimeError):
    """抓取失败，消息已经是一句可以直接说出去的话。"""


@dataclass
class Fetched:
    url: str
    status: int
    title: str
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)
    html: str = ""
    truncated: bool = False
    bytes_read: int = 0

    def render(self, max_chars: int) -> str:
        head = f"标题：{self.title or '（没有标题）'}\n地址：{self.url}\n"
        body = self.text[:max_chars]
        if len(self.text) > max_chars:
            body += "\n……（正文过长，后面略）"
        tail = ""
        if self.links:
            shown = "、".join(f"{text} <{url}>" for text, url in self.links[:12])
            tail = f"\n\n这页还能接着看：{shown}"
        return head + "\n" + body + tail


class _TextGrabber(HTMLParser):
    """把 HTML 抽成带换行的纯文本，顺带收集链接。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False
        self._link_href = ""
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            self._link_href = dict(attrs).get("href") or ""
            self._link_text = []
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link_href:
            text = _WS.sub(" ", "".join(self._link_text)).strip()
            if text:
                self.links.append((text[:60], self._link_href))
            self._link_href = ""
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)
        if self._link_href:
            self._link_text.append(data)

    def result(self) -> tuple[str, str, list[tuple[str, str]]]:
        raw = "".join(self.parts)
        raw = _WS.sub(" ", raw)
        raw = "\n".join(line.strip() for line in raw.splitlines())
        text = _MANY_BLANK.sub("\n\n", raw).strip()
        title = _WS.sub(" ", self.title).strip()
        return title, text, _dedupe_links(self.links)


def _dedupe_links(links: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for text, href in links:
        if href in seen or href.startswith(("#", "javascript:")):
            continue
        seen.add(href)
        out.append((text, href))
    return out


def html_to_text(markup: str, base_url: str = "") -> tuple[str, str, list[tuple[str, str]]]:
    parser = _TextGrabber()
    parser.feed(markup)
    title, text, links = parser.result()
    if not title:
        match = _TITLE_TAG.search(markup)
        title = _WS.sub(" ", match.group(1)).strip() if match else ""
    if base_url:
        links = [(label, urljoin(base_url, href)) for label, href in links]
    return title, text, links


def _assert_public_host(host: str) -> None:
    """挡掉回环、私有段、链路本地与云元数据地址（169.254.169.254 那一类）。"""
    if not host:
        raise FetchError("地址里没有主机名，我不去猜")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:  # 域名解析失败
        raise FetchError(f"这个地址我打不开（{host} 解析不了）") from exc
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            raise FetchError(f"目标在内网或元数据地址上（{host} → {raw}），我不去碰")


class GuardedRedirect(HTTPRedirectHandler):
    """每一跳都复检目标：302 把我往内网或元数据地址带，也不跟。

    仍然只剩 DNS 重绑定的 TOCTOU 窗口（校验解析到的 IP、urllib 再解析一次）；
    要彻底关死得换成能钉住已校验 IP 的连接层客户端。对个人终端工具，这个粒度刚好。
    """

    def __init__(self, allow_private: bool) -> None:
        super().__init__()
        self.allow_private = allow_private

    def redirect_request(  # type: ignore[override]
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Any:
        if self.allow_private:
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        parsed = urlparse(newurl)
        if parsed.scheme in BLOCKED_SCHEMES or not parsed.netloc:
            raise FetchError(f"这页想把我带到 {parsed.scheme or '没写协议'} 的地址，我不跟")
        _assert_public_host(parsed.hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = 2_000_000,
    allow_private: bool = False,
    user_agent: str = "Mozilla/5.0 (MySoulBot; personal assistant)",
    accept: str = "text/html,text/plain,application/json;q=0.8,*/*;q=0.5",
) -> Fetched:
    """同步抓取。调用方负责放进线程里跑。"""
    target = (url or "").strip()
    parsed = urlparse(target)
    if parsed.scheme in BLOCKED_SCHEMES or not parsed.netloc:
        raise FetchError(f"只允许 http/https，这个地址不行：{target[:80]}")
    if not allow_private:
        _assert_public_host(parsed.hostname or "")

    request = Request(  # noqa: S310 - 上面已限定 scheme，禁止 file/ftp 等
        target,
        headers={
            "User-Agent": user_agent,
            "Accept": accept,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    opener = build_opener(GuardedRedirect(allow_private))
    try:
        with opener.open(request, timeout=timeout) as response:
            final_url = response.geturl() or target
            status = getattr(response, "status", None) or response.code
            charset = response.headers.get_content_charset() or "utf-8"
            ctype = (response.headers.get_content_type() or "").lower()
            chunks: list[bytes] = []
            read = 0
            truncated = False
            while True:
                piece = response.read(65_536)
                if not piece:
                    break
                chunks.append(piece)
                read += len(piece)
                if read >= max_bytes:
                    truncated = True
                    break
            body = b"".join(chunks)[:max_bytes]
    except HTTPError as exc:
        raise FetchError(f"这页回我说 {exc.code}，我进不去") from exc
    except URLError as exc:
        raise FetchError(f"连不上：{exc.reason}") from exc
    except OSError as exc:
        raise FetchError(f"打不开：{exc}") from exc

    text_body = body.decode(charset, errors="replace")
    markup = ""
    if "html" in ctype or "xml" in ctype or _looks_html(text_body):
        markup = text_body
        title, plain, links = html_to_text(text_body, final_url)
    else:
        title, plain, links = "", text_body.strip(), []
    if not plain:
        raise FetchError("这页是空的，或者全是脚本，我没读到字")
    return Fetched(
        url=final_url,
        status=int(status),
        title=title,
        text=plain,
        links=links,
        html=markup,
        truncated=truncated,
        bytes_read=read,
    )


def _looks_html(body: str) -> bool:
    head = body[:600].lstrip().lower()
    return head.startswith(("<!doctype html", "<html", "<head")) or "<body" in head
