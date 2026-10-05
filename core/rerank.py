"""精排（rerank）：向量负责「别漏」，精排负责「谁最相关」。

为什么要两段。向量检索是**各自独立**编码再比距离——它没见过「问题」和「候选」
放在一起长什么样，所以快、能扫全库，但排序粗。精排是把问题和一条候选**拼在一起**
送进模型打分：准得多，但每条都要算一次，没法拿去扫一万条。
于是自然的分工就是：向量先粗筛出 20 条，精排在这 20 条里挑出 6 条。

这不是理论上的漂亮话，是这台机器上量出来的：
本地词法向量把「今天不想动」和「懒得动弹」「今晚吃米饭」打成**一样的分**
（实测都是 0.126，字面只共享一个「动」字）；
而 bge-reranker 面对「他猫叫什么名字」，把「他养了一只叫豆豆的猫」打到
**0.9806**，其余两条 0.0003 / 0.0001。差的就是这一截。

和嵌入层一样的两条纪律：

1. **只报真打通过的后端。** 配置里写了不等于能用；没成功过一次就报 `off`，
   并说清为什么。配置读起来和成功读起来一模一样，这层不较真就等于撒谎。
2. **精排失败绝不拖垮检索。** 退回向量原序，`status` 里写明这次是退化的。
   精排是锦上添花，把它做成必经之路就是给自己加了个故障点。
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any, Final
from urllib.error import HTTPError
from urllib.parse import urlparse

from config import Settings
from core.tools.webio import _assert_public_host

logger: Final = logging.getLogger("mysoulbot.rerank")

__all__ = ["Reranker", "RerankError"]

_MAX_DOC_CHARS: Final[int] = 900  # 单条候选送进去的上限，超长截断而不是丢掉
_BATCH: Final[int] = 32


class RerankError(RuntimeError):
    """精排失败。消息已经是一句能看懂为什么失败的话。"""


def _truncate(text: str) -> str:
    body = (text or "").strip()
    return body[:_MAX_DOC_CHARS]


def _post(url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - scheme 在下面查过
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise RerankError(f"只走 http/https：{url[:60]}")
    _assert_public_host(parsed.hostname or "")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(2_000_000)
    except HTTPError as exc:
        # 把上游错误体里的原因捞出来：只报 401 会让人去猜是密钥、配额还是模型名
        detail = ""
        try:
            detail = exc.read(600).decode("utf-8", errors="replace").strip()
        except Exception:  # noqa: BLE001 - 读不到就退回状态码
            detail = ""
        try:
            hint = json.loads(detail)
            detail = str(hint.get("message") or hint.get("error") or "")[:120]
        except (json.JSONDecodeError, AttributeError):
            detail = detail[:120]
        raise RerankError(f"精排端点回 {exc.code}{'：' + detail if detail else ''}") from exc
    except Exception as exc:  # noqa: BLE001 - 上游怎么坏都收敛成一句人话
        raise RerankError(f"精排端点没打通：{str(exc)[:90]}") from exc
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        head = raw[:80].decode("utf-8", errors="replace").lstrip().lower()
        if head.startswith(("<!doctype", "<html")):
            raise RerankError("精排端点回了 HTML 而不是 JSON——多半被网络层挡了，不是密钥问题") from exc
        raise RerankError("精排端点回了不是 JSON 的东西") from exc
    if not isinstance(data, dict):
        raise RerankError("精排端点回了个不是对象的东西")
    return data


class Reranker:
    """按配置选精排后端。`backend` 说的是**现在真在干活**的那一条。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._failed = ""
        self._remote_ok = False

    # ------------------------------------------------------------ 配置
    @property
    def usable(self) -> bool:
        s = self._settings
        provider = (s.rerank_provider or "off").strip().lower()
        return bool(s.rerank_enabled and provider in ("siliconflow", "cohere")
                    and (s.rerank_base_url or "").strip() and (s.rerank_api_key or "").strip()
                    and (s.rerank_model or "").strip())

    @property
    def configured_backend(self) -> str:
        return (self._settings.rerank_provider or "off").strip().lower() if self.usable else "off"

    @property
    def backend(self) -> str:
        """真在干活的那条。没成功打通过一次，就还是 off。"""
        if not self.usable or self._failed or not self._remote_ok:
            return "off"
        return self.configured_backend

    @property
    def degraded_because(self) -> str:
        if not self._settings.rerank_enabled:
            return "RERANK_ENABLED=false，精排整体关掉，检索退回向量原序"
        if not self.usable:
            return "没配 RERANK_PROVIDER/BASE_URL/API_KEY/MODEL，或 provider=off"
        if self._failed:
            return self._failed
        if not self._remote_ok:
            return f"配了 {self.configured_backend} 但还没成功打通过一次，暂时用向量原序"
        return ""

    # ------------------------------------------------------------ 打分
    def rerank(self, query: str, documents: list[str], *, top_n: int = 6) -> list[tuple[int, float]]:
        """返回 [(原下标, 相关性分数)]，按分数从高到低。

        任何失败都返回**空列表**而不是抛：调用方据此退回向量序。
        精排是加分项，做成必经之路就是给自己加了个故障点。
        """
        body = (query or "").strip()
        docs = [_truncate(text) for text in documents]
        if not body or not docs or top_n <= 0:
            return []
        # 只挡「配了但已知坏了」；没打通过一次仍然要真试一把，
        # 否则 _remote_ok 永远是 False，就永远不会有第一次
        if not self.usable or self._failed:
            return []
        s = self._settings
        base = (s.rerank_base_url or "").strip().rstrip("/")
        url = f"{base}/rerank" if not base.endswith("/rerank") else base
        want = min(max(1, top_n), len(docs))
        try:
            data = _post(url, {
                "model": (s.rerank_model or "").strip(),
                "query": body[:_MAX_DOC_CHARS],
                "documents": docs,
                "top_n": want,
                "return_documents": False,   # 我们只要分数和原下标，把文本再抄一遍是白付流量
            }, (s.rerank_api_key or "").strip(), float(s.rerank_timeout))
        except RerankError as exc:
            # 记一次原因就不再重试：每条记忆都撞一次墙，回合就废在等待上了
            self._failed = str(exc)
            logger.warning("精排不可用，退回向量序：%s", exc)
            return []
        rows = data.get("results")
        if not isinstance(rows, list) or not rows:
            self._failed = "精排端点没回 results 列表（结构变了）"
            logger.warning("%s", self._failed)
            return []
        scored: list[tuple[int, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            index = row.get("index")
            score = row.get("relevance_score", row.get("score"))
            if not isinstance(index, int) or index < 0 or index >= len(docs):
                continue  # 越界下标直接丢：宁可少一条，不能把不相干的排进来
            try:
                scored.append((index, float(score)))
            except (TypeError, ValueError):
                continue
        if not scored:
            self._failed = "精排结果一条都没解析出下标与分数"
            return []
        self._remote_ok = True   # 真收到分数了，这才敢报自己是那条后端
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:want]
