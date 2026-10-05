"""文本转向量。两种后端协议都支持，配置决定走哪条。

为什么做成可插拔而不是直接写死 Cohere：**这台机器现在到不了
`api.cohere.com`**——它对任何路径都回同一份 403 裸 HTML（root、乱造的路径、
真端点的响应体 md5 完全一致），那是网络层封锁，不是密钥问题，
所以这个 key 在这里既验证不了也用不了。写死就等于交一套跑不起来的代码。

于是：
- **配好了就用真的。** `EMBED_PROVIDER=cohere` 走 `/v2/embed`，
  `=openai` 走任何 OpenAI 兼容网关的 `/embeddings`。哪天这台机器有了通路
  （换网络、走代理、或网关上了 embedding 通道），改两行配置就生效，不用改代码。
- **没通路也不空转。** 退回本地确定性向量（字符 n-gram 哈希），
  中文词法相似度是真能用的一类近似，检索质量比「只取最近 N 条」好，
  而且**同一句话永远得到同一个向量**，索引不会今天建完明天对不上。
- **降级要说得出来。** `backend` 属性明写现在用的是哪一种，
  面板和日志都读得到。假装在跑向量检索而不说自己在退化，是最省事的撒谎方式。

密钥只从环境变量/配置读，绝不写进任何进版本库的文件。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import struct
import urllib.request
from typing import Any, Final

from config import Settings
from core.tools.webio import FetchError, _assert_public_host
from urllib.parse import urlparse

logger: Final = logging.getLogger("mysoulbot.embed")

# 本地兜底的维度：够装下常用中文 n-gram 的稀疏投影，又不至于把索引撑肥
_LOCAL_DIM: Final[int] = 384
_COHERE_BATCH: Final[int] = 96
_OPENAI_BATCH: Final[int] = 64
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


class EmbedError(RuntimeError):
    """向量化失败。消息已经是一句能直接说出去的话。"""


def _features(text: str) -> list[str]:
    """中文取单字 + 相邻二字，拉丁文取小写词。

    不用分词器：多一个依赖就多一个装不上的理由，而检索要的是「像不像」，
    不是「词性对不对」。二字组合已经能把「不想动」和「动一下」分开。
    """
    body = (text or "").lower()
    marks = _TOKEN_RE.findall(body)
    feats: list[str] = []
    for token in marks:
        if len(token) == 1:
            feats.append(token)
            continue
        feats.append(token)
        feats.extend(token[i:i + 2] for i in range(len(token) - 1))
    # 跨 token 的二元组：中文里「不想/动」这种接缝最容易丢信息
    for left, right in zip(marks, marks[1:]):
        feats.append(f"{left}_{right}")
    return feats


def embed_local(text: str, dim: int = _LOCAL_DIM) -> list[float]:
    """确定性本地向量：特征哈希 + L2 归一。没有随机种子，没有状态。"""
    vec = [0.0] * dim
    for feat in _features(text):
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        slot = struct.unpack("<Q", digest)[0]
        index = slot % dim
        sign = 1.0 if (slot >> 63) & 1 else -1.0
        vec[index] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


def cosine(left: list[float], right: list[float]) -> float:
    """点积即余弦——前提是两个向量都已归一，所以这里不再各自开方。

    维度不一致就是索引和查询用了不同后端（换过配置没重建），
    那种情况下返回 0 比抛异常好：检索退化，对话不该停。
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def _post_json(url: str, payload: dict[str, Any], key: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - scheme 在下面查过
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise EmbedError(f"只走 http/https，这个地址不行：{url[:60]}")
    _assert_public_host(parsed.hostname or "")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read(4_000_000)
    except Exception as exc:  # noqa: BLE001 - 上游怎么坏都收敛成一句人话
        raise EmbedError(f"向量端点没打通：{str(exc)[:90]}") from exc
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # 裸 HTML 就是被网络层挡了（Cohere 在这里正是这个表现），说清楚别让人以为是 key 错
        head = raw[:80].decode("utf-8", errors="replace").lstrip().lower()
        if head.startswith(("<!doctype", "<html")):
            raise EmbedError("向量端点回了一段 HTML 而不是 JSON——多半是被网络层挡了，不是密钥问题") from exc
        raise EmbedError("向量端点回了不是 JSON 的东西") from exc
    if not isinstance(data, dict):
        raise EmbedError("向量端点回了个不是对象的东西")
    return data


class Embedder:
    """按配置选后端。`backend` 说的是**现在真在用的那一条**。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._failed = ""
        # 远程后端**真的通过一次**才算数。只凭配置就报 "cohere"，
        # 等于在一个到不了 Cohere 的网络上谎称自己在跑真向量——
        # 这一层存在的意义就是说真话，不能在这儿打折扣。
        self._remote_ok = False

    # ------------------------------------------------------------ 配置
    @property
    def configured(self) -> tuple[str, str, str, str]:
        """(provider, base_url, api_key, model)。key 只在这条内部流转，不外传。"""
        provider = (self._settings.embed_provider or "off").strip().lower()
        base = (self._settings.embed_base_url or "").strip().rstrip("/")
        key = (self._settings.embed_api_key or "").strip()
        model = (self._settings.embed_model or "").strip()
        return provider, base, key, model if provider != "off" else ""

    @property
    def usable(self) -> bool:
        provider, base, key, model = self.configured
        return bool(provider in ("cohere", "openai") and base and key and model)

    @property
    def backend(self) -> str:
        """现在**真在干活**的那一条，不是配置里写的那一条。"""
        if not self.usable or self._failed or not self._remote_ok:
            return "local"
        return self.configured[0]

    @property
    def configured_backend(self) -> str:
        """配置里想要的那一条（off 表示没打算用远程）。"""
        return self.configured[0] if self.usable else "off"

    @property
    def degraded_because(self) -> str:
        """退化成 local 的原因。没退化就空串——面板与日志要说得清现在是谁在干活。"""
        if not self.usable:
            return "没配 EMBED_PROVIDER/EMBED_BASE_URL/EMBED_API_KEY/EMBED_MODEL，或 provider=off"
        if self._failed:
            return self._failed
        if not self._remote_ok:
            return (f"配了 {self.configured[0]} 但还没成功打通过一次，暂时用本地向量"
                    "（这台机器多半到不了那个端点）")
        return ""

    # ------------------------------------------------------------ 出向量
    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """一批文本换成一批向量。远程失败就整批退到本地，不半真半假。

        `query=True` 只影响 Cohere：它要 `input_type=search_query`，
        和入库时的 `search_document` 用同一批向量算相似度会掉召回。
        """
        items = [str(text or "") for text in texts]
        if not items:
            return []
        if not self.usable or self._failed:
            return [embed_local(text) for text in items]
        provider, base, key, model = self.configured
        timeout = float(self._settings.embed_timeout)
        try:
            rows = (self._cohere(base, key, model, items, query, timeout) if provider == "cohere"
                    else self._openai(base, key, model, items, timeout))
            self._remote_ok = True   # 真收到向量了，这才敢报自己是那条后端
            return rows
        except EmbedError as exc:
            # 记一次原因就不再重试：每条记忆都撞一次墙，回合就废在等待上了
            self._failed = str(exc)
            logger.warning("向量化退回本地：%s", exc)
            return [embed_local(text) for text in items]

    def _cohere(self, base: str, key: str, model: str, items: list[str],
                query: bool, timeout: float) -> list[list[float]]:
        url = f"{base}/embed" if base.endswith("/v2") else f"{base}/v2/embed"
        out: list[list[float]] = []
        for start in range(0, len(items), _COHERE_BATCH):
            chunk = items[start:start + _COHERE_BATCH]
            payload = {
                "model": model,
                "texts": chunk,
                "input_type": "search_query" if query else "search_document",
                "embedding_types": ["float"],
            }
            data = _post_json(url, payload, key, timeout)
            block = data.get("embeddings")
            # v2 按类型分组返回 {"float": [[...]]}；有的部署直接给二维数组
            rows = block.get("float") if isinstance(block, dict) else block
            if not isinstance(rows, list) or len(rows) != len(chunk):
                raise EmbedError("Cohere 回的向量条数跟送进去的对不上")
            out.extend([float(v) for v in row] for row in rows)
        return out

    def _openai(self, base: str, key: str, model: str, items: list[str],
                timeout: float) -> list[list[float]]:
        url = f"{base}/embeddings" if not base.endswith("/embeddings") else base
        out: list[list[float]] = []
        for start in range(0, len(items), _OPENAI_BATCH):
            chunk = items[start:start + _OPENAI_BATCH]
            data = _post_json(url, {"model": model, "input": chunk}, key, timeout)
            rows = data.get("data")
            if not isinstance(rows, list) or len(rows) != len(chunk):
                raise EmbedError("网关回的向量条数跟送进去的对不上")
            ordered = sorted(rows, key=lambda r: r.get("index", 0) if isinstance(r, dict) else 0)
            out.extend([float(v) for v in row["embedding"]] for row in ordered)
        return out


def build_embedder(settings: Settings) -> Embedder:
    return Embedder(settings)
