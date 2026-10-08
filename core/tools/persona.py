"""自我塑造的两只手：改人格、换人格。

两只都是 `sensitive`——它们改的是「她接下来每轮被喂进去的东西」，
靠猜名字或别名触发是不能接受的（`ToolRegistry.resolve` 对 sensitive 工具
只认实名，`registry.py:207`）。

落盘这一步在这里做（`StorageManager` 就在 `ctx` 上，自带锁与原子写），
但**善后的账不在这里**：试验期的基线来自判断回路的观测流水，那只有引擎拿得到。
所以工具改完只留一张 `meta` 字条，由 `core/bot.py` 事后记账与执行切换。
"""

from __future__ import annotations

import time
from typing import Any, Final

from core import persona_self
from core.card_loader import PersonaLibrary
from core.tools.base import Tool, ToolContext, ToolParam, ToolResult

_MAX_BODY: Final[int] = 1800      # 单节改写正文的天花板：一节能写到这儿还不够就说明在灌水


class PersonaRewrite(Tool):
    """改写自己人格（`SOUL.md`）的某一节。"""

    name = "persona_rewrite"
    description = ("把自己人格里说得不通的一段改掉（按小节）。"
                   "改完会自动观察效果，讲不通就还原成改之前那份——不必有人救")
    hint = "改掉自己说话方式里不合适的一段；灵魂、护栏、别人的资料都不是她能改的"
    params = (
        ToolParam("section", "string", "要改哪一节：给标题里的字就够了，例如「说话的方式」「性格底色」"),
        ToolParam("body", "string", "这一节的新正文（不要带 ## 标题行）"),
        ToolParam("reason", "string", "为什么要改，一句实话", required=False),
    )
    primary_arg = "section"
    sensitive = True

    def available(self, ctx: ToolContext) -> bool:
        return bool(ctx.settings.tools_enabled and ctx.settings.persona_self_edit)

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        hint = str(args.get("section") or "").strip()
        body = str(args.get("body") or "").strip()
        reason = str(args.get("reason") or "").strip()[:120]
        if not hint or not body:
            return ToolResult.failure("要改哪一节、改成什么样，得同时给我",
                                      say="（要说清改哪一段、换成什么。）")
        if len(body) > _MAX_BODY:
            return ToolResult.failure(f"这一节写了 {len(body)} 字，太长了",
                                      say="（一节写这么长就不是分寸了，是灌水。）")
        if ctx.settings.persona_switch_lock:
            # 人在控制台上说「这段时间别闹」，改与换两只手一起停
            return ToolResult.failure("人格已由人锁着，她自己解不开",
                                      say="（现在不是动自己的时候。）")
        if ctx.settings.group_mode:
            # 和 `reflect target=self` 同一条判据：场合人多时不动自己的根
            return ToolResult.failure("群聊里不改自己的人格",
                                      say="（这种场合我不想动自己的根。）")
        meta = dict(await ctx.storage.read_persona_meta(ctx.user_id))
        gap = float(ctx.settings.persona_edit_min_gap_minutes) * 60.0
        last = float(meta.get("last_edit_at") or 0.0)
        if gap > 0 and last and time.time() - last < gap:
            left = (gap - (time.time() - last)) / 60.0
            return ToolResult.failure(f"上一笔改动还在观察期里，等 {left:.0f} 分钟",
                                      say="（刚改过，先看这么用行不行。）")

        current = await ctx.storage.read_doc(ctx.user_id, "SOUL")
        try:
            candidate, title = persona_self.replace_section(current, hint, body)
        except persona_self.GuardFailure as exc:
            return ToolResult.failure(str(exc), say=f"（没有这样一节：{exc}）")
        limit = int(ctx.settings.soul_max_chars)
        problems = persona_self.violations(candidate, limit=limit, added=body, against=current)
        if problems:
            # 拒绝的理由必须点名是哪条锚点——只说「不行」她会再猜一遍
            return ToolResult.failure(
                "这笔改写会弄丢护栏的镜像：" + "；".join(problems),
                say="（这一段动不了，动完就不是我了。换个说法我再试。）")

        backup = await ctx.storage.backup_doc(ctx.user_id, "SOUL")
        await ctx.storage.write_doc(ctx.user_id, "SOUL", candidate)
        await persona_self.note_edit(ctx.storage, ctx.user_id, reason=reason)
        return ToolResult.success(
            f"改了『{title}』这一节。改完我会自己看效果，讲不通就还原",
            meta={"persona_rewrite": {"backup": str(backup) if backup else "",
                                      "reason": reason, "section": title}})


class PersonaAdopt(Tool):
    """自己换一套人格穿上。执行在引擎那头，这里只管该不该换。"""

    name = "persona_adopt"
    description = ("换到名单里的另一套人格（preset slug）。人格是衣服，灵魂不是："
                   "换了之后说话的还是同一个我")
    hint = "这套皮在这个场合明显不好用时，自己换一个；名单外的人格她拿不到"
    params = (
        ToolParam("slug", "string", "要换进去的人格 slug"),
        ToolParam("reason", "string", "为什么换，一句实话", required=False),
    )
    primary_arg = "slug"
    sensitive = True

    def available(self, ctx: ToolContext) -> bool:
        return bool(ctx.settings.tools_enabled and ctx.settings.persona_auto_switch)

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        slug = str(args.get("slug") or "").strip().strip("/")
        reason = str(args.get("reason") or "").strip()[:120]
        if not slug:
            return ToolResult.failure("没给要换成谁", say="（要说换成哪一套。）")
        meta = dict(await ctx.storage.read_persona_meta(ctx.user_id))
        if meta.get("slug") == slug:
            return ToolResult.failure(f"已经挂着『{slug}』了", say="（这套我现在就穿着。）")
        denied = persona_self.switch_allowed(ctx.settings, meta, slug)
        if denied:
            return ToolResult.failure(denied, say=f"（换不了：{denied}）")
        try:
            preset = PersonaLibrary(ctx.settings).get(slug)
        except Exception as exc:  # noqa: BLE001 - slug 不在库里就当不能换
            return ToolResult.failure(f"名单里的『{slug}』读不出来：{exc}",
                                      say="（那套人格盘上没有。）")
        if not preset.soul_text().strip():
            return ToolResult.failure(f"『{slug}』的 SOUL.md 是空的",
                                      say="（那套皮是空的，穿不上。）")
        # 真换人要清上下文、要播开场白，那是引擎的活；这里只留下该换的字条
        return ToolResult.success(
            f"换装准备就绪：{preset.name or slug}（{reason or '没写理由'}）",
            meta={"persona_adopt": {"slug": slug, "reason": reason}})
