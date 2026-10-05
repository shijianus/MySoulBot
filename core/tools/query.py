"""现实世界查询：中文搜索、天气、行情、汇率、车次、热榜、本机出口 IP。

这些能力都是**只读外部查询**，输出是给模型消化的材料，不是最终台词——模型决定怎么说。
所以这里返回的是紧凑的带标签事实，而不是整页正文，也不替角色说话。

为什么另起一模块而不改 `web.py`：原来的 `web_search` 指向 html.duckduckgo.com，
这台机器上网络根本不可达（Errno 101）。这里换成实测能通的免费源：
Bing 中文搜索、wttr.in、东方财富、er-api、12306、知乎/B站热榜、ipip。

三条硬规矩：
- 诚实降级：源挂了或没解析出东西，就 `ToolResult.failure` 说一句人话，绝不编一个数。
- 多源兜底：凡是我给了两个源的地方（搜索、天气），先试下一个再失败。
- 防御性解析：上游改了 JSON 结构只能变成 failure，不能抛出未捕获异常。全程 `.get()` + try。

HTTP 一律走 `webio.fetch`（含 SSRF 守卫与 web_* 配置）。唯一例外是中国天气网：
它缺 `Referer` 必 403，而 `fetch()` 没有塞自定义头的入口，于是就近复用 webio 的守卫补一个极小的带头请求。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from html import unescape
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener

from core.tools.base import Tool, ToolContext, ToolParam, ToolResult
from core.tools.webio import FetchError, GuardedRedirect, _assert_public_host, fetch

# 有些源对非浏览器 UA 直接变脸（B 站风控），搜索/行情反而无所谓。
_UA_BROWSER: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TAG: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")
_WS: Final[re.Pattern[str]] = re.compile(r"\s+")
_CJK: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]")

_STOCK_URL: Final[str] = (
    "https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields={fields}"
)
_STOCK_FIELDS: Final[str] = "f43,f57,f58,f169,f170,f46,f44,f45,f60,f47,f48,f50,f51,f52"
# 腾讯这条是 `~` 分隔的定长字段表，GBK 正文，免密钥。它才是主源：
# 东财 push2 的边缘节点会按客户端指纹整片掐 Python 的连接（实测 curl 200、http.client 直接
# RemoteDisconnected），拿它兜底而不是当家，不然「能看行情」这句话一半时间是空的。
_TENCENT_URL: Final[str] = "https://qt.gtimg.cn/q={symbols}"
_FX_URL: Final[str] = "https://open.er-api.com/v6/latest/CNY"
_ZHIHU_URL: Final[str] = "https://api.zhihu.com/topstory/hot-lists/total?limit={limit}"
_BILI_URL: Final[str] = "https://api.bilibili.com/x/web-interface/wbi/search/square?limit={limit}"
_IP_URL: Final[str] = "https://myip.ipip.net/json"

# 常见城市 → 中国天气网城市码（wttr.in 挂了时的兜底源；它只认城市码不认城市名）
_CITY_CODES: Final[dict[str, str]] = {
    "北京": "101010100", "上海": "101020100", "广州": "101280101", "深圳": "101280601",
    "杭州": "101210101", "南京": "101190101", "成都": "101270101", "武汉": "101200101",
    "西安": "101110101", "重庆": "101040100", "天津": "101030100", "苏州": "101190401",
    "长沙": "101250101", "郑州": "101180101", "青岛": "101120201", "沈阳": "101070101",
}
# 指数名 → 东财 secid：首位 1=沪、0=深
_INDEX_CODES: Final[dict[str, str]] = {
    "上证指数": "1.000001", "沪指": "1.000001", "深证成指": "0.399001", "深成指": "0.399001",
    "创业板指": "0.399006", "创业板": "0.399006", "沪深300": "1.000300", "沪深三百": "1.000300",
    "上证50": "1.000016", "中证500": "1.000905", "科创50": "1.000688",
}


# 连接级失败的指纹：这类（对端直接掐线、DNS、超时）重试可能救回来；
# HTTP 状态码或空页就不必白等第二轮。
_RETRYABLE: Final[re.Pattern[str]] = re.compile(r"打不开|连不上|Remote|Errno|timed out|超时")


# ------------------------------------------------------------------ 通用取数
def _timeout(ctx: ToolContext, cap: float) -> float:
    """单请求超时：听 settings.web_timeout，但再短也要有个封顶，
    免得一个死源把整轮对话拖住。"""
    base = getattr(ctx.settings, "web_timeout", cap) or cap
    return min(float(base), cap)


def _fetch(ctx: ToolContext, url: str, *, ua: str | None = None, cap: float = 8.0, attempts: int = 1):
    """同步阻塞抓取——调用方必须用 asyncio.to_thread 把它挪出事件循环（见各 run）。"""
    s = ctx.settings
    agent = ua or "Mozilla/5.0 (MySoulBot; personal assistant)"
    last: FetchError | None = None
    for attempt in range(max(1, attempts)):
        try:
            return fetch(
                url,
                timeout=_timeout(ctx, cap),
                max_bytes=s.web_max_bytes,
                allow_private=s.web_allow_private,
                user_agent=agent,
            )
        except FetchError as exc:
            last = exc
            if _RETRYABLE.search(str(exc)) is None or attempt + 1 >= attempts:
                raise
            # 递增退避：东财这类源掐线是成片的，隔太久等于白等，隔太短等于再撞一次
            time.sleep(0.3 * (attempt + 1))
    # 循环最后一轮必定 raise；这里只是给类型一个可达出口
    raise last if last is not None else FetchError("抓取失败")


def _fetch_json(ctx: ToolContext, url: str, *, ua: str | None = None, cap: float = 8.0, attempts: int = 1) -> Any:  # noqa: ANN401
    page = _fetch(ctx, url, ua=ua, cap=cap, attempts=attempts)
    try:
        return json.loads(page.text)
    except (ValueError, TypeError) as exc:
        raise FetchError("这个源没回合法的 JSON") from exc


def _fetch_referer_text(ctx: ToolContext, url: str, referer: str, *, cap: float = 8.0) -> str:
    """只为缺 Referer 就 403 的源（中国天气网）补一个头；SSRF 守卫沿用 webio 的，别另起炉灶。
    同样阻塞，调用方负责丢进 to_thread。"""
    s = ctx.settings
    allow = bool(s.web_allow_private)
    if not allow:
        _assert_public_host(urlparse(url).hostname or "")
    request = Request(  # noqa: S310 - 上面已限定 scheme，且经 webio 守卫
        url,
        headers={
            "User-Agent": _UA_BROWSER,
            "Referer": referer,  # 这源少了它必 403，实测
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    opener = build_opener(GuardedRedirect(allow))
    try:
        with opener.open(request, timeout=_timeout(ctx, cap)) as response:
            return response.read(s.web_max_bytes).decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise FetchError(f"这个源回 {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise FetchError(f"连不上这个源：{exc}") from exc


# ------------------------------------------------------------------ 数值/文本
def _num(value: object) -> float | None:
    """把上游给的 '23' / 23 / 125862 之类收成 float；停牌、'-'、空一律 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"-", "--", "—"}:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _scaled(value: object, digits: int = 2, suffix: str = "") -> str:
    """东财的价格字段是 ×100 的整数，还原成可读值；取不到就摆个 '—'，绝不补 0。
    价格固定小数位，别把 3.80 打成 3.8。"""
    number = _num(value)
    if number is None:
        return "—"
    return f"{number / 100.0:,.{digits}f}{suffix}"


def _signed(value: object, suffix: str = "") -> str:
    """带正负号的涨跌：方向写在符号里，就不必再叠一个「涨/跌」字。"""
    number = _num(value)
    if number is None:
        return "—"
    return f"{number / 100.0:+,.2f}{suffix}"


def _plain(value: object, digits: int = 2, suffix: str = "") -> str:
    number = _num(value)
    if number is None:
        return "—"
    text = f"{number:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text + suffix


def _clean(fragment: str) -> str:
    return _WS.sub(" ", _TAG.sub("", unescape(fragment or ""))).strip()


def _clamp(value: object, low: int, high: int, default: int) -> int:
    number = _num(value)
    if number is None:
        return default
    return max(low, min(int(number), high))


# ------------------------------------------------------------------ 工具
class WebSearchCN(Tool):
    name = "web_search_cn"
    description = (
        "中文网页搜索（Bing 中文），返回前几条的标题、摘要和链接。想核实说法、查最新消息、"
        "找某个具体页面就从这里开始。拿到的是线索，你自己消化后用自己的话说。"
    )
    hint = "能查资料（中文源）"
    params = (
        ToolParam("query", "string", "搜索词，自然语言即可"),
        ToolParam("limit", "integer", "最多几条，默认 5，最多 8", required=False),
    )
    primary_arg = "query"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult.failure("没给搜索词", say="（要查什么，先给个词。）")
        limit = _clamp(args.get("limit"), 1, 8, 5)
        encoded = quote(query, safe="")
        # 主源 + 兜底源：cn.bing 挂了还有 www.bing，两个都是同一套 HTML 结构
        sources = (
            f"https://cn.bing.com/search?q={encoded}&setlang=zh-hans",
            f"https://www.bing.com/search?q={encoded}&mkt=zh-CN",
        )
        last_error = ""
        for url in sources:
            try:
                page = await asyncio.to_thread(_fetch, ctx, url, cap=8.0)
            except FetchError as exc:
                last_error = str(exc)
                continue
            hits = _parse_bing(page.html, limit)
            if hits:
                body = "\n".join(
                    f"{i}. {title}\n   {snippet or '（这条没有摘要）'}\n   {link}"
                    for i, (title, snippet, link) in enumerate(hits, 1)
                )
                return ToolResult.success(
                    f"【搜「{query}」看到 {len(hits)} 条，消化后用自己的话讲给他，别整段搬】\n{body}",
                    meta={"hits": len(hits)},
                )
        if last_error:
            return ToolResult.failure(last_error, say="（这条搜索线没打通，就说没查到。）")
        return ToolResult.failure(
            "搜索没返回结果", say="（搜了一圈没搜着，我只能说不知道。）"
        )


class WeatherNow(Tool):
    name = "weather_now"
    description = (
        "按城市名查实时天气和明天的预报。主源 wttr.in（任意城市），挂了再回落中国天气网"
        "（只有常见城市码，且拿不到次日预报）。给的是实况材料，怎么说由你定。"
    )
    hint = "能报天气"
    params = (
        ToolParam("city", "string", "城市名，中文即可，如 北京"),
        ToolParam("city_code", "string", "中国天气网城市码，可留空（如 101010100）", required=False),
    )
    primary_arg = "city"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        city = str(args.get("city", "")).strip()
        if not city:
            return ToolResult.failure("没给城市", say="（要说哪个城市的天气，先报个地名。）")
        wttr_error = ""
        try:
            data = await asyncio.to_thread(
                _fetch_json, ctx, f"https://wttr.in/{quote(city, safe='')}?format=j1&lang=zh", cap=9.0
            )
            return self._from_wttr(city, data)
        except FetchError as exc:
            wttr_error = str(exc)

        code = str(args.get("city_code", "")).strip() or _CITY_CODES.get(city)
        if not code:
            return ToolResult.failure(
                f"wttr 不可用（{wttr_error}），且没有「{city}」的国内城市码可兜底",
                say="（这条天气线没查通，就说没看着，别猜。）",
            )
        try:
            body = await asyncio.to_thread(
                _fetch_referer_text,
                ctx,
                f"https://d1.weather.com.cn/sk_2d/{code}.html?_={int(time.time() * 1000)}",
                "https://www.weather.com.cn/",
                cap=8.0,
            )
            return self._from_weathercn(city, body)
        except FetchError as exc:
            return ToolResult.failure(
                f"两个天气源都不行：wttr（{wttr_error}）；中国天气网（{exc}）",
                say="（天气实在没查到，就说没查到。）",
            )

    @staticmethod
    def _from_wttr(city: str, data: Any) -> ToolResult:
        if not isinstance(data, dict):
            raise FetchError("wttr 回了个不是对象的东西")
        conditions = data.get("current_condition")
        if not isinstance(conditions, list) or not conditions or not isinstance(conditions[0], dict):
            raise FetchError("wttr 没给实况字段")
        current = conditions[0]
        temp = _plain(current.get("temp_C"), 0, "°C")
        feels = _plain(current.get("FeelsLikeC"), 0, "°C")
        humidity = _plain(current.get("humidity"), 0, "%")
        wind = _plain(current.get("windspeedKmph"), 0, " km/h")
        direction = str(current.get("winddir16Point", "") or "").strip()
        cond = _wttr_condition(current)

        lines = [
            f"【{city} · 实况】（源 wttr.in，观测 {current.get('observation_time', '?')} UTC）",
            f"当前：{temp}（体感 {feels}）｜{cond or '晴雨未知'}",
            f"湿度：{humidity}　风：{(direction + ' ' + wind).strip() or '—'}",
        ]
        days = data.get("weather")
        if isinstance(days, list) and len(days) >= 2 and isinstance(days[1], dict):
            tomorrow = days[1]
            lines.append(
                "明日："
                f"{tomorrow.get('date', '?')} "
                f"{_plain(tomorrow.get('mintempC'), 0)}~{_plain(tomorrow.get('maxtempC'), 0)}°C"
            )
        else:
            lines.append("明日：wttr 没给到次日预报")
        return ToolResult.success("\n".join(lines), meta={"source": "wttr.in"})

    @staticmethod
    def _from_weathercn(city: str, body: str) -> ToolResult:
        match = re.search(r"dataSK\s*=\s*(\{.*\})", body, re.S)
        if not match:
            raise FetchError("中国天气网返回的不再是 dataSK 结构")
        try:
            payload = json.loads(match.group(1))
        except (ValueError, TypeError) as exc:
            raise FetchError("中国天气网的 dataSK 解析不了") from exc
        if not isinstance(payload, dict):
            raise FetchError("中国天气网回了个不是对象的东西")
        lines = [
            f"【{payload.get('cityname') or city} · 实况】（源 中国天气网，更新 {payload.get('time', '?')}）",
            f"当前：{_plain(payload.get('temp'), 1, '°C')}｜天气：{payload.get('weather', '未知')}",
            f"湿度：{payload.get('SD', '—')}　风：{(str(payload.get('WD', '')) + ' ' + str(payload.get('WS', ''))).strip() or '—'}",
            f"气压：{_plain(payload.get('qy'), 0, ' hPa')}　能见度：{payload.get('njd', '—')}　空气质量：{payload.get('aqi', '—')}",
            "次日预报：这个源只有实况，没有次日",
        ]
        return ToolResult.success("\n".join(lines), meta={"source": "weather.com.cn"})


class StockQuote(Tool):
    name = "stock_quote"
    description = (
        "查 A 股个股或宽基指数行情。code 可给 6 位数（如 600519，自动判沪深）、指数名"
        "（如 上证指数、创业板指）或显式 secid（如 1.600519）。价格类字段是东财 ×100 的整数，"
        "我已还原。停牌或代码不存在会明说，不编数。"
    )
    hint = "能看 A 股行情"
    params = (ToolParam("code", "string", "6 位代码 / 指数名 / 显式 secid，如 600519、上证指数、0.300750"),)
    primary_arg = "code"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        token = str(args.get("code", "")).strip()
        if not token:
            return ToolResult.failure("没给代码", say="（要查哪只，先给我个代码或指数名。）")
        secid = _resolve_secid(token)
        if not secid:
            return ToolResult.failure(
                f"认不出「{token}」", say="（这个代码/指数名我不认，别硬猜。）"
            )
        # 主源腾讯，兜底东财：两边都拿不到才算查不到，任何一边给数都不编
        tencent_error = ""
        try:
            page = await asyncio.to_thread(
                _fetch, ctx, _TENCENT_URL.format(symbols=_to_tencent_symbol(secid)),
                ua=_UA_BROWSER, cap=8.0, attempts=2,
            )
            row = _parse_tencent(page.text)
            if row:
                return ToolResult.success(_render_quote(row, secid), meta={"source": "qt.gtimg.cn"})
            tencent_error = "腾讯没回这个代码的数据"
        except FetchError as exc:
            tencent_error = str(exc)

        try:
            data = await asyncio.to_thread(
                _fetch_json, ctx, _STOCK_URL.format(secid=secid, fields=_STOCK_FIELDS),
                ua=_UA_BROWSER, cap=8.0, attempts=3,
            )
        except FetchError as exc:
            return ToolResult.failure(
                f"两个行情源都没打通：腾讯（{tencent_error}）；东财（{exc}）",
                say="（行情源没打通，就说没看着。）",
            )
        quote_obj = data.get("data") if isinstance(data, dict) else None
        if not isinstance(quote_obj, dict) or not quote_obj:
            return ToolResult.failure(
                f"东财没有 {secid} 的数据（代码可能不存在或已退市）",
                say="（这只查不到行情，就说没查到。）",
            )
        price = _scaled(quote_obj.get("f43"))
        if price == "—":
            return ToolResult.failure(
                f"{quote_obj.get('f58', secid)} 没有最新价（多半停牌了）",
                say="（这只现在没有报价，可能是停牌，就说没行情。）",
            )
        name = str(quote_obj.get("f58", "") or "").strip() or secid
        code = str(quote_obj.get("f57", "") or "").strip()
        market = "沪" if secid.startswith("1.") else "深"
        amount = _num(quote_obj.get("f48"))
        limit_up = _num(quote_obj.get("f51"))
        limit_dn = _num(quote_obj.get("f52"))
        # 指数没有涨跌停（东财回填 0），只有个股才有；全 0 就当不存在，别摆个假数字
        limit_seg = ""
        if (limit_up or 0) > 0 or (limit_dn or 0) > 0:
            limit_seg = f"涨停 {_scaled(quote_obj.get('f51'))}　跌停 {_scaled(quote_obj.get('f52'))}　"
        lines = [
            f"【{name}（{code}）· {market}市】",
            f"最新 {price}　较昨收 {_signed(quote_obj.get('f169'))}（{_signed(quote_obj.get('f170'), '%')}）",
            f"今开 {_scaled(quote_obj.get('f46'))}　最高 {_scaled(quote_obj.get('f44'))}　"
            f"最低 {_scaled(quote_obj.get('f45'))}　昨收 {_scaled(quote_obj.get('f60'))}",
            f"{limit_seg}成交量 {_plain(quote_obj.get('f47'), 0, ' 手')}　"
            f"成交额 {f'{amount / 1e8:,.2f} 亿' if amount is not None else '—'}",
            "注：价格字段按东财 ×100 缩放已还原。",
        ]
        return ToolResult.success("\n".join(lines), meta={"secid": secid})


class ExchangeRate(Tool):
    name = "exchange_rate"
    description = (
        "以人民币为基准查汇率（er-api 免费源）。给目标币种代码（如 USD、JPY、EUR）和可选金额，"
        "两个方向的换算都给。源每天更新一次，我会带上更新时间。认不出的币种直接说。"
    )
    hint = "能换算汇率"
    params = (
        ToolParam("code", "string", "目标币种 ISO 代码，如 USD"),
        ToolParam("amount", "number", "换算金额，默认 1", required=False),
    )
    primary_arg = "code"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        code = str(args.get("code", "")).strip().upper()
        if not code:
            return ToolResult.failure("没给币种", say="（换成哪种货币，先给个币种代码。）")
        amount = _num(args.get("amount"))
        if amount is None:
            amount = 1.0
        try:
            data = await asyncio.to_thread(_fetch_json, ctx, _FX_URL, cap=10.0)
        except FetchError as exc:
            return ToolResult.failure(str(exc), say="（汇率源没打通，就说没查到。）")
        rates = data.get("rates") if isinstance(data, dict) else None
        if not isinstance(rates, dict) or not rates:
            return ToolResult.failure("汇率源没回 rates 字段", say="（这个汇率查询没成，就说没查到。）")
        rate = _num(rates.get(code))
        if rate is None or rate == 0:
            return ToolResult.failure(
                f"源里没有 {code}", say="（这种货币的汇率我没查到，别编。）"
            )
        per_other = 1.0 / rate
        updated = str(data.get("time_last_update_utc", "") or "").strip()
        lines = [
            f"【汇率 · 人民币基准】（更新 {updated or '时间未知'}）",
            f"1 CNY = {rate:.4f} {code}　｜　1 {code} = {per_other:.4f} CNY",
            f"{_plain(amount)} 元 = {amount * rate:,.2f} {code}",
            f"{_plain(amount)} {code} = {amount * per_other:,.2f} 元",
        ]
        return ToolResult.success("\n".join(lines), meta={"code": code})


class TrainSearch(Tool):
    name = "train_query"
    description = (
        "按车次号查 12306 的运行区间（在哪个站始发、终到哪、全程几站）。date 可选，YYYYMMDD，"
        "默认今天；12306 只给未来约一个月的排班。查到的是车底与区间，具体每站时刻不在这个接口里。"
    )
    hint = "能查火车车次"
    params = (
        ToolParam("keyword", "string", "车次号，如 G102"),
        ToolParam("date", "string", "查询日期 YYYYMMDD，默认今天", required=False),
    )
    primary_arg = "keyword"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        keyword = str(args.get("keyword", "")).strip().upper()
        if not keyword:
            return ToolResult.failure("没给车次", say="（要查哪趟车，先给个车次号。）")
        date = str(args.get("date", "")).strip()
        if not re.fullmatch(r"\d{8}", date):
            date = time.strftime("%Y%m%d")
        try:
            data = await asyncio.to_thread(
                _fetch_json, ctx,
                f"https://search.12306.cn/search/v1/train/search?keyword={quote(keyword, safe='')}&date={date}",
                cap=8.0,
            )
        except FetchError as exc:
            return ToolResult.failure(str(exc), say="（12306 没打通，就说没查到。）")
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            return ToolResult.failure(
                f"12306 在 {date} 没有 {keyword}", say="（这趟车这天可能没排班，就说没查到。）"
            )
        exact = [r for r in rows if isinstance(r, dict) and str(r.get("station_train_code", "")).upper() == keyword]
        picked = exact or rows
        lines = [f"【{keyword} · {date}】（源 12306）"]
        for row in picked[:3]:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"{row.get('station_train_code', '?')} "
                f"{row.get('from_station', '?')} → {row.get('to_station', '?')}　"
                f"全程 {row.get('total_num', '?')} 站　车底 {row.get('train_no', '?')}"
            )
        if len(rows) > len(exact):
            lines.append(f"另有 {len(rows) - len(exact)} 个相近车次（前缀匹配），以上是最接近的。")
        return ToolResult.success("\n".join(lines), meta={"rows": len(picked)})


class HotList(Tool):
    name = "hot_list"
    description = (
        "把知乎热榜和 B 站热搜各取一截合在一起，看今天大家在聊什么。两路各自独立，一路挂了不影响另一路"
        "（我会写明哪路没取到）。给的是话题清单，你挑着说。"
    )
    hint = "能看热榜热搜"
    params = (ToolParam("limit", "integer", "每路最多几条，默认 8，最多 15", required=False),)
    primary_arg = "limit"

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        limit = _clamp(args.get("limit"), 1, 15, 8)
        sections: list[str] = []
        failed: list[str] = []

        # 两路并发，互不拖累：一路慢/挂了不影响另一路
        zhihu, bili = await asyncio.gather(self._zhihu(ctx, limit), self._bilibili(ctx, limit))
        for label, outcome in (("知乎", zhihu), ("B站", bili)):
            if isinstance(outcome, str):
                sections.append(outcome)
            else:
                failed.append(f"{label}：{outcome}")

        if not sections:
            return ToolResult.failure(
                "；".join(failed) or "两路热榜都是空的",
                say="（今天的热榜没刷出来，就说没看着。）",
            )
        body = "\n\n".join(sections)
        if failed:
            body += "\n\n（部分来源没取到：" + "；".join(failed) + "）"
        return ToolResult.success("【今日热榜，挑能接的话说】\n" + body, meta={"sources": len(sections)})

    @staticmethod
    async def _zhihu(ctx: ToolContext, limit: int) -> str | FetchError:
        try:
            data = await asyncio.to_thread(_fetch_json, ctx, _ZHIHU_URL.format(limit=limit), cap=8.0)
        except FetchError as exc:
            return exc
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return FetchError("热榜结构变了，没拿到 data 列表")
        lines: list[str] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            target = item.get("target") if isinstance(item.get("target"), dict) else {}
            title = _first_title(target)
            if not title:
                continue
            heat = _clean(str(item.get("detail_text", "") or ""))
            lines.append(f"- {title}" + (f"（{heat}）" if heat else ""))
        if not lines:
            return FetchError("知乎一条都没解析出来")
        return "知乎热榜：\n" + "\n".join(lines)

    @staticmethod
    async def _bilibili(ctx: ToolContext, limit: int) -> str | FetchError:
        try:
            data = await asyncio.to_thread(_fetch_json, ctx, _BILI_URL.format(limit=limit), ua=_UA_BROWSER, cap=8.0)
        except FetchError as exc:
            return exc
        trending = (((data.get("data") or {}) if isinstance(data, dict) else {}).get("trending") or {})
        items = trending.get("list")
        if not isinstance(items, list):
            return FetchError("B站热搜结构变了")
        lines: list[str] = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            name = _clean(str(item.get("show_name", "") or item.get("keyword", "") or ""))
            if not name:
                continue
            heat = _plain(item.get("heat_score"), 0)
            lines.append(f"- {name}" + (f"（{heat}）" if heat != "—" else ""))
        if not lines:
            return FetchError("B站一条都没解析出来")
        return "B 站热搜：\n" + "\n".join(lines)


class HostIpInfo(Tool):
    name = "egress_ip"
    description = (
        "查这台机器自己的出口公网 IP 和归属地（ipip.net）。只能报本机出口，"
        "查不了别人给的一个 IP 归到哪——别用它去定位他人。"
    )
    hint = "能看本机出口 IP"
    params: tuple[ToolParam, ...] = ()

    def available(self, ctx: ToolContext) -> bool:
        return ctx.settings.tools_enabled and ctx.settings.web_enabled

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:  # noqa: ARG002 - 无参
        try:
            data = await asyncio.to_thread(_fetch_json, ctx, _IP_URL, cap=8.0)
        except FetchError as exc:
            return ToolResult.failure(str(exc), say="（出口 IP 这条没查通，就说没查到。）")
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return ToolResult.failure("ipip 没回 data 字段", say="（这个查询没成，就说没查到。）")
        ip = _clean(str(payload.get("ip", "") or ""))
        if not ip:
            return ToolResult.failure("ipip 没给 IP", say="（没读到出口 IP，就说没查到。）")
        location = payload.get("location")
        region = " ".join(str(x) for x in location if x) if isinstance(location, list) else ""
        lines = [
            "【本机出口 IP】（源 ipip.net）",
            f"IP：{ip}",
            f"归属：{region or '（没给归属）'}",
            "说明：这只是这台机器自己的出口地址；给你另一个 IP 想查它归属，这个源做不到。",
        ]
        return ToolResult.success("\n".join(lines), meta={"ip": ip})


# ------------------------------------------------------------------ 解析辅助
def _parse_bing(markup: str, limit: int) -> list[tuple[str, str, str]]:
    """Bing 中文结果页：每条包在 class=\"b_algo\" 的 <li> 里，标题在 h2>a，摘要在 b_caption>p。"""
    if not markup:
        return []
    hits: list[tuple[str, str, str]] = []
    for block in re.split(r'class="b_algo"', markup)[1:]:
        if len(hits) >= limit:
            break
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S | re.I)
        if not link:
            continue
        title = _clean(link.group(2))
        if not title:
            continue
        url = _clean(link.group(1))
        caption = re.search(r'class="b_caption"[^>]*>.*?<p[^>]*>(.*?)</p>', block, re.S | re.I)
        snippet = _clean(caption.group(1)) if caption else ""
        snippet = snippet.strip("·").strip()  # 摘要常以「日期 ·」开头
        hits.append((title, snippet, url))
    return hits


def _wttr_condition(current: dict[str, Any]) -> str:
    """优先取中文描述，lang_zh 有时原样给英文，那就退回 weatherDesc。"""
    for key in ("lang_zh", "weatherDesc"):
        block = current.get(key)
        if isinstance(block, list) and block and isinstance(block[0], dict):
            value = _clean(str(block[0].get("value", "") or ""))
            if value and (key == "weatherDesc" or _CJK.search(value)):
                return value
    return ""


def _first_title(target: dict[str, Any]) -> str:
    """知乎热榜在不同版本里标题散在 title / title_area.text / title_anchor.text / excerpt。"""
    direct = _clean(str(target.get("title", "") or ""))
    if direct:
        return direct
    for key in ("title_area", "title_anchor"):
        area = target.get(key)
        if isinstance(area, dict):
            text = _clean(str(area.get("text", "") or ""))
            if text:
                return text
    return _clean(str(target.get("excerpt", "") or ""))


def _to_tencent_symbol(secid: str) -> str:
    """东财 secid `1.600519` → 腾讯 `sh600519`（1=沪、0=深）。"""
    market, _, code = secid.partition(".")
    return f"{'sh' if market == '1' else 'sz'}{code}"


def _parse_tencent(body: str) -> dict[str, str] | None:
    """腾讯行情是 `v_sh600519="1~名称~代码~现价~…"` 的一行，`~` 分隔、下标固定。

    只认下标不认顺序说明：字段表是公开的老格式，位置比名字可靠。
    拿不到现价那一格就当没数据，不拿 0 充数。
    """
    for chunk in (body or "").split(";"):
        line = chunk.strip()
        if "=" not in line:
            continue
        _, _, value = line.partition("=")
        fields = value.strip().strip('"').split("~")
        if len(fields) < 44 or not fields[3].strip():
            continue
        price = _num(fields[3])
        if price is None:
            continue
        return {
            "name": fields[1].strip(), "code": fields[2].strip(), "price": fields[3].strip(),
            "prev_close": fields[4].strip(), "open": fields[5].strip(),
            "volume": fields[6].strip(), "change": fields[31].strip(),
            "pct": fields[32].strip(), "high": fields[33].strip(), "low": fields[34].strip(),
            "amount_wan": fields[37].strip(), "stamp": fields[30].strip(),
        }
    return None


def _render_quote(row: dict[str, str], secid: str) -> str:
    market = "沪" if secid.startswith("1.") else "深"
    try:
        amount_text = f"{float(row['amount_wan']) / 1e4:,.2f} 亿"  # 万元 → 亿元
    except (KeyError, TypeError, ValueError):
        amount_text = "—"
    stamp = row.get("stamp", "")
    when = (f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}"
            if len(stamp) >= 12 else "时间未知")
    return "\n".join([
        f"【{row['name'] or secid}（{row['code']}）· {market}市】（源 腾讯行情，{when}）",
        f"最新 {row['price']}　较昨收 {row['change']}（{row['pct']}%）",
        f"今开 {row['open']}　最高 {row['high']}　最低 {row['low']}　昨收 {row['prev_close']}",
        f"成交量 {_plain(_num(row.get('volume')), 0, ' 手')}　成交额 {amount_text}",
    ])


def _resolve_secid(token: str) -> str:
    """指数名 / 6 位数 / 显式 secid / 带 SH·SZ 前缀，统一折成东财的 secid。"""
    text = token.strip()
    if re.fullmatch(r"[01]\.\d{6}", text):
        return text
    named = _INDEX_CODES.get(text) or _INDEX_CODES.get(text.upper())
    if named:
        return named
    # 去掉常见市场前缀，只留数字
    digits = re.sub(r"\D", "", re.sub(r"(?i)^(sh|sz)", "", text))
    if len(digits) == 6:
        # 沪(1)：60x 股票 / 68x 科创 / 5xx 基金 / 9xx B股；其余（0/1/2/3 开头）归深(0)
        market = "1" if digits[0] in "569" else "0"
        return f"{market}.{digits}"
    return ""


QUERY_TOOLS: Final[tuple[type[Tool], ...]] = (
    # WebSearchCN 不进这张表：`core/tools/web.py` 的 `web_search` 就是它的子类，
    # 再注册一份会出现两个一模一样的搜索工具，模型挑花眼
    WeatherNow,
    StockQuote,
    ExchangeRate,
    TrainSearch,
    HotList,
    HostIpInfo,
)
