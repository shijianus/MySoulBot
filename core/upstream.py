"""上游路由池：一个回合该打哪一家、打不通怎么**在同一回合内**当场换一家。

为什么要这个：网关的反应时间是抽签型的——同一个请求，这次 9 秒出字、那次 53 秒，
再慢一点就是「想了 3800 字一个字都没落」。用户在 QQ 上等的是「她回不回话」，
不是「哪条线路排队」。所以这里做的事只有三件：

1. **分档选路**：短话与长话可以走不同的模型（快与深本来就该分开）。
2. **按实测速度均衡**：同一优先级里谁的首字快就先打谁，用 EWMA 平滑，不看瞬时运气。
3. **同步回退**：连不上、报错、看门狗判它这趟白等——都算这一家不行，
   立刻换下一家接着答**同一句话**。不许把「换一家」做成让用户重发一次。

冷却（ejection）是被打挂之后静默一段时间，到期自动放行一次试探（半开）：
线路是会恢复的，永久拉黑反而会把好的那条一直关在外面。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Final

TIER_QUICK: Final[str] = "quick"
TIER_FULL: Final[str] = "full"
TIERS: Final[tuple[str, ...]] = (TIER_QUICK, TIER_FULL)

_EWMA_ALPHA: Final[float] = 0.4   # 首字秒数的平滑系数：0.4 大约是「最近两趟说话算数」
_DATA_URI: Final[str] = "data:"   # api_key 可以写成 data:<路径>，密钥就不必进 .env 明文行


@dataclass(frozen=True)
class Route:
    """一条能打的路：哪家网关、哪个模型、给哪些档位用、优先级多少。"""

    name: str
    base_url: str
    model: str
    api_key: str = ""
    tiers: tuple[str, ...] = TIERS
    priority: int = 100
    vision_model: str = ""   # 带图的回合交给这条线时用哪个模型；空 = 就用 model
    timeout: float = 0.0     # 0 = 跟全局 REQUEST_TIMEOUT

    def client_id(self) -> str:
        return f"{self.base_url}|{self.api_key}"

    def model_for(self, *, has_images: bool) -> str:
        if has_images and self.vision_model:
            return self.vision_model
        return self.model


@dataclass
class Health:
    """这条线最近怎么样：成、败、白等、首字多快、是否在冷却、最后一次的错。"""

    ok: int = 0
    fail: int = 0
    stalled: int = 0
    streak: int = 0
    ewma: float | None = None
    ejected_until: float = 0.0
    last_error: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "fail": self.fail,
            "stalled": self.stalled,
            "first_visible_avg": round(self.ewma, 2) if self.ewma is not None else None,
            "cooling": round(max(0.0, self.ejected_until - time.monotonic()), 1),
            "last_error": self.last_error,
        }


class UpstreamPool:
    def __init__(self, settings: Any) -> None:  # noqa: ANN401 - Settings 由调用方给
        self._settings = settings
        self._routes: list[Route] = []
        self._health: dict[str, Health] = {}
        self.reload()

    # ------------------------------------------------------------ 配置
    def reload(self) -> None:
        self._routes = parse_routes(self._settings.routes, self._settings)
        known = {route.name for route in self._routes}
        self._health = {name: self._health[name] for name in self._health if name in known}
        for name in known:
            self._health.setdefault(name, Health())

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

    def route(self, name: str) -> Route | None:
        for candidate in self._routes:
            if candidate.name == name:
                return candidate
        return None

    def _index(self, name: str) -> int:
        for position, candidate in enumerate(self._routes):
            if candidate.name == name:
                return position
        return len(self._routes)

    # ------------------------------------------------------------ 选路
    def candidates(self, tier: str, *, tried: tuple[str, ...] = ()) -> list[Route]:
        """这一档能打的线，按「优先级 → 最近实测首字」排；打过的和冷却中的往后放。"""
        now = time.monotonic()
        strict = bool(self._settings.route_strict_order)
        fresh, cooling = [], []
        for route in self._routes:
            if route.name in tried or tier not in route.tiers:
                continue
            health = self._health.get(route.name) or Health()
            (cooling if health.ejected_until > now else fresh).append(route)

        def order(route: Route) -> tuple[int, float]:
            health = self._health.get(route.name) or Health()
            if strict:
                return (route.priority, 0.0)
            if health.ewma is None:
                # 没测过的线先按池里的位次发一到两次：不然冷启动时永远只打第一条，
                # 另外几条的实测速度一辈子攒不出来
                return (route.priority, float(self._index(route.name)))
            return (route.priority, health.ewma)

        fresh.sort(key=order)
        cooling.sort(key=order)
        return fresh + cooling   # 全在冷却时也要有得打：宁可再试，不许「没有线路」

    def pick(self, tier: str, *, tried: tuple[str, ...] = ()) -> Route | None:
        options = self.candidates(tier, tried=tried)
        return options[0] if options else None

    # ------------------------------------------------------------ 记账
    def report_ok(self, name: str, first_visible_seconds: float) -> None:
        health = self._health.setdefault(name, Health())
        health.ok += 1
        health.streak = 0
        health.ejected_until = 0.0
        health.last_error = ""
        sample = max(0.0, first_visible_seconds)
        health.ewma = sample if health.ewma is None else (
            _EWMA_ALPHA * sample + (1 - _EWMA_ALPHA) * health.ewma)

    def report_bad(self, name: str, reason: str, *, stalled: bool = False) -> None:
        """白等与打不通都要记一笔：连够阈值就冷却一段时间。

        白等也算坏，而且不该等满整轮超时才知道——上游卡住时「换一家」本身就是答案。
        """
        health = self._health.setdefault(name, Health())
        if stalled:
            health.stalled += 1
        else:
            health.fail += 1
        health.streak += 1
        health.last_error = reason[:160]
        threshold = max(1, int(self._settings.route_fail_threshold))
        if health.streak >= threshold and health.ejected_until <= time.monotonic():
            health.ejected_until = time.monotonic() + float(self._settings.route_cooldown_seconds)

    def view(self) -> dict[str, Any]:
        return {
            "routes": [
                {"name": route.name, "model": route.model, "base_url": route.base_url,
                 "tiers": list(route.tiers), "priority": route.priority,
                 **(self._health.get(route.name) or Health()).view()}
                for route in self._routes
            ],
        }


def parse_routes(raw: str, settings: Any) -> list[Route]:  # noqa: ANN401
    """把 ROUTES 那段 JSON 解开；留空就是「只有一条线」——老行为，一条都不变。"""
    text = (raw or "").strip()
    if not text or text in ("[]", "{}"):
        return [Route(name="default", base_url=settings.base_url, model=settings.model,
                      api_key=settings.api_key, vision_model=settings.vision_model)]
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"ROUTES 不是合法 JSON：{exc}") from exc
    if isinstance(loaded, dict):
        loaded = [loaded]
    rows: list[Route] = []
    for index, item in enumerate(loaded):
        if not isinstance(item, dict):
            raise ValueError(f"ROUTES 第 {index + 1} 项不是对象")
        name = str(item.get("name") or f"route{index + 1}")
        tiers = item.get("tiers") or list(TIERS)
        if isinstance(tiers, str):
            tiers = [part.strip() for part in tiers.split(",") if part.strip()]
        unknown = [tier for tier in tiers if tier not in TIERS]
        if unknown:
            raise ValueError(f"ROUTES {name} 的档位不认识：{unknown}")
        rows.append(Route(
            name=name,
            base_url=str(item.get("base_url") or settings.base_url).rstrip("/"),
            model=str(item.get("model") or settings.model),
            api_key=_resolve_key(str(item.get("api_key") or ""), settings),
            tiers=tuple(tiers),
            priority=int(item.get("priority") or 100),
            vision_model=str(item.get("vision_model") or ""),
            timeout=float(item.get("timeout") or 0.0),
        ))
    if not rows:
        raise ValueError("ROUTES 解出来是空的：要么别配，要么至少给一条线")
    return rows


def _resolve_key(raw: str, settings: Any) -> str:  # noqa: ANN401
    """密钥三种写法：直接给、`env:变量名`、`data:文件路径`。后两种让明文密钥不进配置文件。"""
    value = raw.strip()
    if not value:
        return str(settings.api_key)
    if value.startswith("env:"):
        import os

        return os.environ.get(value[4:], "") or str(settings.api_key)
    if value.startswith(_DATA_URI):
        from pathlib import Path

        path = Path(value[len(_DATA_URI):]).expanduser()
        return path.read_text(encoding="utf-8").strip() if path.is_file() else str(settings.api_key)
    return value
