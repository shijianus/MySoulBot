"""上游路由池与同回合回退：坏一条线，这一句照样要有人答。

跑法：.venv/bin/python tests/upstream_test.py

假上游只有一个端口，两条「线」用两个模型名区分——坏哪条、通哪条，全按模型名点。
这里验的都是「换家」这件事本身：不重发、不重说、不把冷却当死刑、也不许把回退
做成慢上加慢。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qq_onebot_test as T  # noqa: E402
from config import Settings  # noqa: E402
from core.upstream import Route, UpstreamPool, parse_routes  # noqa: E402

Checker = T.Checker


def pool_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "api_key": "sk-test", "base_url": "https://main.invalid/v1", "model": "model-main",
        "storage_dir": Path(tempfile.mkdtemp(prefix="upstream-")), "log_level": "WARNING",
        "request_timeout": 25.0, "first_token_timeout": 0.4, "first_visible_timeout": 2.0,
        "first_token_retries": 0, "first_visible_hedge": 0.0, "route_fail_threshold": 2,
        "route_cooldown_seconds": 30.0, "prompt_tiers_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------------- 1. 池子本身
def pool_checks(check: Checker) -> None:
    """配置解析与选路顺序：这些错了，回退就是随机撞运气。"""
    plain = UpstreamPool(pool_settings())
    check.ok("没配 ROUTES 就是一条线，行为跟从前一样",
             len(plain.routes) == 1 and plain.routes[0].name == "default"
             and plain.pick("quick").model == "model-main", plain.view())

    text = json.dumps([
        {"name": "slow", "model": "glm-slow", "priority": 50, "tiers": "full"},
        {"name": "fast", "model": "luna", "priority": 50},
        {"name": "backup", "base_url": "https://b.invalid/v1", "api_key": "k2",
         "model": "deep", "priority": 90},
    ])
    pool = UpstreamPool(pool_settings(routes=text, base_url="https://a.invalid/v1"))
    check.ok("三条线都解出来了", len(pool.routes) == 3, pool.view())
    check.ok("base_url 留空跟着主配置", pool.route("fast").base_url == "https://a.invalid/v1")
    check.ok("tiers 只给 full 的线不接短话",
             [r.name for r in pool.candidates("quick")] == ["fast", "backup"],
             [r.name for r in pool.candidates("quick")])
    check.ok("full 档同优先级按名字稳定排（还没实测速度时）",
             [r.name for r in pool.candidates("full")][0] in ("slow", "fast"))
    check.ok("打过的不再选", [r.name for r in pool.candidates("full", tried=("fast",))][0] != "fast")

    # 实测速度均衡：同样 priority，谁首字快谁先打
    pool.report_ok("slow", 30.0)
    pool.report_ok("fast", 3.0)
    check.ok("同优先级里按实测首字均衡",
             pool.pick("full").name == "fast", [r.model for r in pool.candidates("full")])
    strict = UpstreamPool(pool_settings(routes=text, route_strict_order=True))
    strict.report_ok("slow", 30.0)
    strict.report_ok("fast", 3.0)
    check.ok("ROUTE_STRICT_ORDER=true 就只认优先级",
             strict.pick("full").name == "slow", strict.pick("full").name)

    # 冷启动的探索：没测过的线得轮到机会，否则第二条永远排不到
    spread = UpstreamPool(pool_settings(routes=json.dumps([
        {"name": "a", "model": "m-a", "priority": 10},
        {"name": "b", "model": "m-b", "priority": 10},
        {"name": "c", "model": "m-c", "priority": 10},
    ])))
    seen: list[str] = []
    for _ in range(3):
        chosen = spread.pick("quick")
        seen.append(chosen.name)
        spread.report_ok(chosen.name, 5.0)
    check.ok("冷启动会轮着探每条线，不是死打第一条", seen == ["a", "b", "c"], seen)
    spread.report_ok("c", 1.0)
    check.ok("都测过之后就纯按实测快慢走", spread.pick("quick").name == "c",
             [row["first_visible_avg"] for row in spread.view()["routes"]])

    # 记账与冷却
    bad = UpstreamPool(pool_settings(routes=text, route_fail_threshold=2,
                                     route_cooldown_seconds=60.0))
    bad.report_bad("fast", "连不上")
    check.ok("一次失败还不该判死（阈值 2）", bad.pick("quick").name == "fast", bad.view())
    bad.report_bad("fast", "又连不上")
    check.ok("连够阈值就进冷却，先打下一条",
             bad.pick("quick").name == "backup", bad.view())
    health = bad.view()["routes"][1]
    check.ok("冷却看得见还剩多久，也留得住原因",
             health["cooling"] > 0 and "连不上" in health["last_error"], health)
    bad.report_ok("fast", 2.0)
    check.ok("一旦打通过就立刻解除冷却（半开放行成功）",
             bad.pick("quick").name == "fast" and bad.view()["routes"][1]["cooling"] == 0.0,
             bad.view())

    # 全在冷却时不许「没有线路」
    allbad = UpstreamPool(pool_settings(routes=text))
    for _ in range(4):
        allbad.report_bad("fast", "死")
        allbad.report_bad("backup", "死")
    check.ok("全线冷却也要给一条可打的（宁试不空）",
             allbad.pick("quick") is not None, allbad.view())

    check.ok("档位写错要当场报错，不许悄悄不回退",
             _raises(lambda: parse_routes('[{"name":"x","tiers":["ture"]}]', pool_settings())),
             "")
    check.ok("ROUTES 不是 JSON 要报错",
             _raises(lambda: parse_routes("{不是 json", pool_settings())), "")
    key_pool = UpstreamPool(pool_settings(
        routes=json.dumps([{"name": "f", "api_key": "data:" + _write_key(), "model": "m"}])))
    check.ok("api_key 可以走 data: 文件，明文不进配置行",
             key_pool.route("f").api_key == "sekrit-key", key_pool.route("f").api_key[:4])

    route = Route(name="r", base_url="https://x/v1", model="text", vision_model="eyes")
    check.ok("带图的回合走这条线的 vision_model",
             route.model_for(has_images=True) == "eyes"
             and route.model_for(has_images=False) == "text")


def _raises(action: Any) -> bool:  # noqa: ANN401
    try:
        action()
    except ValueError:
        return True
    return False


def _write_key() -> str:
    path = Path(tempfile.mkdtemp(prefix="key-")) / "k"
    path.write_text("sekrit-key", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------- 2. 现场回退
async def failover_checks(check: Checker) -> None:
    """坏一条线时那一句话照样得有回音，而且不许说两遍。"""
    server, origin = T.serve_fake()
    T.ORIGIN = origin
    rig = _BotRig(origin)
    try:
        # A) 第一条线直接 503 → 同一回合换第二条
        T.reset_model()
        T.MODE["bad_models"] = ("line-a",)
        T.MODE["echo_map"] = {"line-a": "这句不该出现", "line-b": "换一条线也答上了"}
        rig.reload_lines()
        out = await rig.ask()
        check.ok("一条线报错，同一回合由下一条答上", out.texts == ["换一条线也答上了"], out.texts)
        check.ok("报错那条记了账，接通那条也记了账",
             rig.view_of("line-a")["fail"] >= 1 and rig.view_of("line-b")["ok"] >= 1,
             rig.pool_view())
        check.ok("没把两家的话叠着发", len(out.texts) == 1, out.texts)

        # B) 第一条线只思考不落正文 → 看门狗撤掉并换家
        T.reset_model()
        T.MODE["think_models"] = {"line-a": 40}     # 40 片 × 0.4s，远超 2 秒正文看门狗
        T.MODE["echo_map"] = {"line-a": "这句也不该出现", "line-b": "白等的换掉了"}
        rig.reload_lines()
        out = await rig.ask()
        check.ok("光思考不落正文的线被撤，换家答上", out.texts == ["白等的换掉了"], out.texts)
        check.ok("白等记成 stalled 而不是 fail",
                 rig.view_of("line-a")["stalled"] >= 1, rig.view_of("line-a"))

        # C) 两条线都坏 → 老实报错，不许编一句应付
        T.reset_model()
        T.MODE["bad_models"] = ("line-a", "line-b")
        rig.reload_lines()
        error = await rig.ask_error()
        check.ok("全线都坏时报错而不编话", error is not None and "没接住" not in str(error), str(error))

        # D) 说了一半才断：绝不换家重说
        T.reset_model()
        T.MODE["echo_map"] = {"line-a": "甲甲甲。乙乙乙。丙丙丙。", "line-b": "换家重说就是复读"}
        T.MODE["cut_models"] = {"line-a": 1}
        rig.reload_lines()
        got = await rig.ask_collect()
        check.ok("半路断线不换家（宁缺不复读）", "换家重说就是复读" not in "".join(got["texts"]),
                 got["texts"])
        check.ok("已经开口的那半句留在原样", bool(got["texts"]), got["texts"])

        # E) 只有一条线时：一次失败就该照旧报错，别兜圈
        solo = _BotRig(origin)
        solo.reload_lines(json.dumps([{"name": "only", "base_url": origin, "model": "line-a"}]))
        T.reset_model()
        T.MODE["bad_models"] = ("line-a",)
        seen_before = len(T.SEEN)
        err = await solo.ask_error()
        check.ok("单线路不重复兜圈", err is not None and len(T.SEEN) - seen_before <= 3,
                 f"打了 {len(T.SEEN) - seen_before} 次")
    finally:
        await rig.aclose()
        server.shutdown()


class _Outcome:
    def __init__(self, texts: list[str], error: str = "", seconds: float = 0.0) -> None:
        self.texts = texts
        self.error = error
        self.seconds = seconds


class _BotRig:
    """只装引擎，不装网桥：验选路与回退用不着 QQ 那一层。"""

    def __init__(self, origin: str, *, tiers: bool = False) -> None:
        from core.bot import MySoulBot
        from core.card_loader import PersonaLibrary
        from core.clawd_soul import ClawdSoul
        from core.memory_extractor import MemoryExtractor
        from core.prompt_builder import PromptBuilder
        from core.storage_manager import StorageManager

        self.settings = T.make_settings(Path(tempfile.mkdtemp(prefix="upstream-bot-")), origin,
                                        tools_enabled=False, extractor_enabled=False,
                                        first_token_timeout=0.4, first_visible_timeout=2.0,
                                        first_token_retries=0, first_visible_hedge=0.0,
                                        prompt_tiers_enabled=tiers, quick_prompt_max_chars=100)
        self.storage = StorageManager(self.settings)
        self.clawd = ClawdSoul(self.settings)
        self.extractor = MemoryExtractor(self.settings, self.storage)
        self.bot = MySoulBot(self.settings, self.storage,
                             PromptBuilder(self.settings, self.storage, self.clawd),
                             self.extractor, PersonaLibrary(self.settings), self.clawd)

    def reload_lines(self, routes: str = "") -> None:
        lines = routes or json.dumps([
            {"name": "A", "base_url": self.settings.base_url, "model": "line-a", "priority": 10},
            {"name": "B", "base_url": self.settings.base_url, "model": "line-b", "priority": 20},
        ])
        self.settings.routes = lines
        from core.upstream import UpstreamPool

        self.bot.routes = UpstreamPool(self.settings)

    def pool_view(self) -> dict[str, Any]:
        return self.bot.routes.view()

    def view_of(self, name: str) -> dict[str, Any]:
        for row in self.bot.routes.view()["routes"]:
            if row["model"] == name:
                return row
        return {}

    async def ask_collect(self) -> dict[str, Any]:
        """把正文一段段收下来；出错也带着已收的部分回来，好验「说了一半不再换家」。"""
        from core.bot import BotError

        await self.clawd.ensure()
        await self.bot.open_session("qq_private_700001", restore=False)
        texts: list[str] = []
        error = ""
        started = time.perf_counter()
        try:
            async for delta in self.bot.stream_reply("qq_private_700001", "在吗"):
                texts.append(delta)
        except BotError as exc:
            error = exc.message
        return {"texts": texts, "error": error,
                "seconds": round(time.perf_counter() - started, 2)}

    async def ask(self) -> _Outcome:
        got = await self.ask_collect()
        return _Outcome(got["texts"], got["error"], got["seconds"])

    async def ask_error(self) -> str | None:
        outcome = await self.ask()
        return outcome.error or None

    async def aclose(self) -> None:
        await self.bot.aclose()
        await self.extractor.aclose(timeout=3.0)
        shutil.rmtree(self.settings.storage_dir, ignore_errors=True)


async def hedge_checks(check: Checker) -> None:
    """对冲要补在**下一条线**上：同一家里再排一次队，多半还是那个水位。

    这里让 A 只思考不落正文（该被撤），B 正常——对冲那把打在 B 上就该先见字，
    而且 A 只是没跑完，不该被记成坏了。
    """
    server, origin = T.serve_fake()
    T.ORIGIN = origin
    rig = _BotRig(origin, tiers=True)
    try:
        T.reset_model()
        T.MODE["think_models"] = {"line-a": 40}        # 40 片 × 0.4s，永远不落正文
        T.MODE["echo_map"] = {"line-a": "这句不该出现", "line-b": "下一条线先见字"}
        rig.settings.first_visible_hedge = 0.5
        rig.settings.first_visible_timeout = 6.0
        rig.settings.first_token_retries = 1           # 没有对冲的话这里要白等两整轮
        rig.reload_lines()
        out = await rig.ask()
        check.ok("快捷档才对冲：这一趟确实补了第二条线",
                 len(T.SEEN) >= 2, f"上游被打了 {len(T.SEEN)} 次")
        check.ok("下一条线先见字，正文用它", out.texts == ["下一条线先见字"], out.texts)
        check.ok("没等同一家把两轮看门狗等满", out.seconds < 3.0, f"{out.seconds}s")
        # 只断言不变量：输的那条绝不能被记成「坏了」。
        # stalled 记不记要看胜者判定和回收谁先跑——line-a 确实是「光思考不落正文」，
        # 记成白等反而是对的（上面另有一条断言就是钉这个的），
        # 所以这里再要求 stalled==0 就是跟自己的另一条断言打架，变成计时抖动。
        view_a = rig.view_of("line-a")
        check.ok("输的那条只算没跑完，不记成坏了",
                 view_a["fail"] == 0 and view_a["ok"] >= 0, view_a)
        check.ok("赢的那条记了账", rig.view_of("line-b")["ok"] >= 1, rig.view_of("line-b"))
        T.reset_model()
    finally:
        await rig.aclose()
        server.shutdown()


async def ask_once_checks(check: Checker) -> None:
    """后台短产出（配对口令、招呼语）那条路：慢线路不能把预算吃光。

    A 条磨 3 秒、B 条立刻答——串行时先等的那 3 秒是白等；同时赛跑就该 1 秒内拿到
    B 的话。口令那一句还额外有条硬预算：到点没结果就本地现拼。
    """
    server, origin = T.serve_fake()
    T.ORIGIN = origin
    rig = _BotRig(origin)
    try:
        rig.reload_lines()
        # 串行（默认，对话那一路的老行为不许变）：先问 A，A 磨 3 秒就真等 3 秒
        T.reset_model()
        T.MODE["json_lag_models"] = {"line-a": 3.0}
        T.MODE["echo_map"] = {"line-a": "慢那条的话", "line-b": "快那条的话"}
        t0 = time.monotonic()
        serial = await rig.bot.ask_once("给一个词", timeout=8.0)
        spent = time.monotonic() - t0
        check.ok("串行时按池子顺序问（默认行为没变）", serial == "慢那条的话", serial)
        check.ok("串行就要白等慢那条的 3 秒", spent >= 2.5, f"{spent:.2f}s")

        # 赛跑：两条同时问，谁先落正文用谁
        T.reset_model()
        T.MODE["json_lag_models"] = {"line-a": 3.0}
        T.MODE["echo_map"] = {"line-a": "慢那条的话", "line-b": "快那条的话"}
        t0 = time.monotonic()
        raced = await rig.bot.ask_once("给一个词", timeout=8.0, race=True)
        spent = time.monotonic() - t0
        check.ok("同时赛跑：先见字的那条算赢", raced == "快那条的话", raced)
        check.ok("不等慢的那条（省下两秒以上）", spent < 1.5, f"{spent:.2f}s")

        # prefer：指定线路排到最前（口令该用最快那条，不是主力对话模型）
        T.reset_model()
        T.MODE["echo_map"] = {"line-a": "A 的话", "line-b": "B 的话"}
        check.ok("prefer 把指定线路挪到最前",
                 await rig.bot.ask_once("给一个词", prefer="B", race=False) == "B 的话", "")

        # 全炸：老实回空串，由调用方兜底——配对不能因为一次生成失败就卡死。
        # 后台那条抽取用的线路也在候选里，要坏就连它一起坏，不然它算「答上了」
        T.reset_model()
        T.MODE["bad_models"] = tuple(sorted({
            "line-a", "line-b",
            str(rig.settings.extractor_model or ""), str(rig.settings.model or "")}))
        t0 = time.monotonic()
        check.ok("线路全坏时立刻给空串（不抛、也不耗预算）",
                 await rig.bot.ask_once("给一个词", timeout=2.0, race=True) == ""
                 and time.monotonic() - t0 < 2.5, "")
        T.reset_model()
    finally:
        await rig.aclose()
        server.shutdown()


async def main() -> int:
    check = Checker()
    pool_checks(check)
    await failover_checks(check)
    await hedge_checks(check)
    await ask_once_checks(check)
    print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
    for name in check.failures:
        print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
