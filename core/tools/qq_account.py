"""她自己的 QQ 账号能力：认人、翻记录、发动态、点赞、处理申请。

这一层和别的工具不一样，它动的是**管理者的真实 QQ 号**，所以三条规矩先说死：

1. **只对管理者开放。** 交互者够不到这些工具——不是「他不该用」，是
   任何一个陌生号都能逼她向管理者的熟人广播、或者把管理者的私聊翻出来当素材，
   这个口子不能开。放行看 `ctx.is_owner`，那是判定出来的身份，不是自称出来的。
2. **协议端做不到的事就明说做不到。** QQ 频道（guild）这条，NapCat 只给了
   `get_guild_list` / `get_guild_service_profile` 两个只读动作，
   发不了评论也点不了赞——所以这里只有「看」，不假装能「说」。
3. **公开广播单独节流，且默认关。** 动态是全好友可见的广播，发出去收不回来，
   也不该由一次模型输出就替管理者向所有熟人宣告什么。

动作名与入参照着协议端实测来的：`send_like` 的计数参数叫 `times` 不叫 `count`，
`set_qq_profile` 的签名字段叫 `personal_note` 不叫 `signature`——猜错不会报错，
只会静悄悄什么都没做，这种坑只能靠查它自己的 schema。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from core.storage_manager import atomic_write
from core.tools.base import Tool, ToolContext, ToolParam, ToolResult

# 一条记录里最多念给她看多少条消息：够判断「这人平时怎么接话」，不够她把通讯录抄走
_HISTORY_CAP: Final[int] = 40
_ROSTER_CAP: Final[int] = 60


def _qq_gate(ctx: ToolContext, switch: str) -> bool:
    """账号级工具的总闸：总开关 + 单项开关 + 这一回合确实是管理者。"""
    s = ctx.settings
    return bool(s.qq_account_enabled and ctx.is_owner and getattr(s, switch, False))


async def _call(ctx: ToolContext, action: str, params: dict[str, Any]) -> tuple[Any, str]:
    port = ctx.qq
    if port is None:
        return None, "QQ 那头的通道没开"
    return await port.account_call(action, params)


def _as_list(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("messages", "groups", "friends", "data", "list", "items"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [item for item in inner if isinstance(item, dict)]
    return []


def _text_of(message: dict[str, Any]) -> str:
    """消息段列表收成一行可读文本；图片只标出来，不猜内容。"""
    content = message.get("message")
    parts: list[str] = []
    if isinstance(content, list):
        for seg in content:
            if not isinstance(seg, dict):
                continue
            kind = str(seg.get("type") or "")
            data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
            if kind in ("text", "at", "face"):
                parts.append(str(data.get("text") or data.get("qq") or "").strip())
            elif kind == "image":
                parts.append("[图片]")
            elif kind:
                parts.append(f"[{kind}]")
    elif isinstance(content, str):
        parts.append(content)
    return " ".join(p for p in parts if p).strip()


def _flat_time(raw: Any) -> str:
    stamp = raw if isinstance(raw, (int, float)) else None
    if stamp is None and isinstance(raw, str) and raw.isdigit():
        stamp = int(raw)
    if not stamp:
        return ""
    return datetime.fromtimestamp(float(stamp), tz=timezone.utc).astimezone().strftime("%m-%d %H:%M")


# ---------------------------------------------------------------- 读：认识自己的处境
class QQRoster(Tool):
    name = "qq_roster"
    description = (
        "看看自己的 QQ 处境：加了哪些群、有哪些好友、各是什么名目。"
        "问「你都跟谁在玩」「你加了什么群」就用它。只读，不改任何东西。"
    )
    hint = "能看自己的 QQ 通讯录与群列表"
    params: tuple[ToolParam, ...] = ()

    def available(self, ctx: ToolContext) -> bool:
        return _qq_gate(ctx, "qq_group_discovery")

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:  # noqa: ARG002
        groups_data, group_err = await _call(ctx, "get_group_list", {})
        friends_data, friend_err = await _call(ctx, "get_friend_list", {})
        groups = _as_list(groups_data)
        friends = _as_list(friends_data)
        if not groups and not friends:
            return ToolResult.failure(group_err or friend_err or "通讯录是空的",
                                      say="（这次没读到 QQ 通讯录，就说没看着。）")
        lines = [f"【我的 QQ 处境】群 {len(groups)} 个 · 好友 {len(friends)} 人"]
        if groups:
            lines.append("群：")
            for item in groups[:_ROSTER_CAP]:
                name = str(item.get("group_name") or item.get("name") or "?").strip()
                gid = str(item.get("group_id") or "").strip()
                count = str(item.get("member_count") or "").strip()
                lines.append(f"- {name}（{gid}" + (f"，{count} 人" if count else "") + "）")
        if friends:
            names = [str(item.get("nickname") or item.get("user_id") or "?").strip()
                     for item in friends[:_ROSTER_CAP]]
            lines.append("好友：" + "、".join(n for n in names if n))
        if len(groups) > _ROSTER_CAP or len(friends) > _ROSTER_CAP:
            lines.append(f"（各最多列 {_ROSTER_CAP} 个）")
        return ToolResult.success("\n".join(lines), meta={"groups": len(groups), "friends": len(friends)})


class QQReadHistory(Tool):
    name = "qq_read_history"
    description = (
        "翻某个人/某个群最近的消息记录，看真人之间实际怎么接话。"
        "scope=friend 给 QQ 号，scope=group 给群号。读完是你自己的素材，"
        "别把别人的原话点名转述给第三个人。"
    )
    hint = "能翻聊天记录学人怎么说话"
    params = (
        ToolParam("scope", "string", "friend 或 group"),
        ToolParam("target", "string", "QQ 号或群号"),
        ToolParam("count", "integer", "看几条，默认 20，最多 40", required=False),
    )
    primary_arg = "target"

    def available(self, ctx: ToolContext) -> bool:
        return _qq_gate(ctx, "qq_read_history")

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        scope = str(args.get("scope") or "").strip().lower()
        target = str(args.get("target") or "").strip()
        if scope not in ("friend", "group") or not target.isdigit():
            return ToolResult.failure("要说清是 friend 还是 group，以及那个号",
                                      say="（要看谁的记录，得给我范围和号。）")
        count = max(1, min(int(args.get("count") or 20), _HISTORY_CAP))
        action = "get_friend_msg_history" if scope == "friend" else "get_group_msg_history"
        key = "user_id" if scope == "friend" else "group_id"
        data, err = await _call(ctx, action, {key: int(target), "count": count})
        messages = _as_list(data)
        if not messages:
            return ToolResult.failure(err or "没翻到消息", say="（那段记录没读出来，就说没翻到。）")
        lines = [f"【{'私聊' if scope == 'friend' else '群聊'} {target} 最近 {len(messages)} 条】"]
        for item in messages[-count:]:
            when = _flat_time(item.get("time"))
            sender = str(item.get("sender_nickname") or item.get("card") or item.get("user_id") or "?")
            body = _text_of(item)
            if body:
                lines.append(f"{when} {sender}: {body[:120]}")
        if len([line for line in lines if line != lines[0]]) == 0:
            return ToolResult.failure("记录里没正文", say="（翻到了但都是图/表情，没字。）")
        lines.append("（这是拿来学接话分寸的，不是拿来转述给第三个人的）")
        return ToolResult.success("\n".join(lines), meta={"messages": len(messages)})


class QQLike(Tool):
    name = "qq_like"
    description = "给某个好友点赞（QQ 的「踩一踩/点赞」）。想表达善意又懒得打字时用。"
    hint = "能给别人点赞"
    params = (
        ToolParam("user_id", "string", "被点的人的 QQ 号"),
        ToolParam("times", "integer", "点几下，默认 1，最多 10", required=False),
    )
    primary_arg = "user_id"

    def available(self, ctx: ToolContext) -> bool:
        return _qq_gate(ctx, "qq_like")

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        target = str(args.get("user_id") or "").strip()
        if not target.isdigit():
            return ToolResult.failure("没给要点的 QQ 号", say="（给谁点，先说个号。）")
        # 协议端这个动作的计数参数叫 times，不叫 count：猜错不会报错，只会只点一下
        times = max(1, min(int(args.get("times") or 1), 10))
        _, err = await _call(ctx, "send_like", {"user_id": int(target), "times": times})
        if err:
            return ToolResult.failure(err, say="（这赞没点出去，就说没点成。）")
        return ToolResult.success(f"给 {target} 点了 {times} 下。")


class QQPublishQZone(Tool):
    name = "qq_publish_qzone"
    description = (
        "发一条 QQ 空间动态——所有好友可见的公开广播。发什么由你自己定：此刻的心情、"
        "对某件事的看法都行。想清楚再说，这条收不回来。"
    )
    hint = "能发 QQ 动态（公开广播）"
    params = (ToolParam("content", "string", "动态正文，一段话就够"),)
    primary_arg = "content"
    # 对外广播、不可撤销：只认精确名字，不给别名也不给模糊匹配
    sensitive = True

    def available(self, ctx: ToolContext) -> bool:
        return _qq_gate(ctx, "qq_qzone_publish")

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        content = str(args.get("content") or "").strip()
        if not content:
            return ToolResult.failure("动态是空的", say="（要发动态总得有字。）")
        wait = _qzone_wait(ctx)
        if wait > 0:
            return ToolResult.failure(
                f"上一条动态才过了 {(int(wait) // 60)} 分钟，节流中",
                say=f"（刚发过一条，{int(wait // 3600)} 小时后再说。天天刷动态是营销号。）")
        if len(content) > 500:
            content = content[:500]
        data, err = await _call(ctx, "send_qzone_msg",
                                {"content": content, "images": [], "ugc_right": 1, "target_uins": []})
        if err:
            return ToolResult.failure(err, say="（这条动态没发出去，就说没发成。）")
        _mark_qzone_sent(ctx)
        tid = str((data or {}).get("tid") or "") if isinstance(data, dict) else ""
        return ToolResult.success(f"动态发出去了：{content[:60]}" + (f"（tid {tid}）" if tid else ""))


class QQRequests(Tool):
    name = "qq_requests"
    description = (
        "看等着处理的好友/入群申请：谁、通过什么途径、说了什么验证话。"
        "只是读，不处理。想知道谁在试图接近你再决定。"
    )
    hint = "能看待处理的申请"
    params: tuple[ToolParam, ...] = ()

    def available(self, ctx: ToolContext) -> bool:
        return _qq_gate(ctx, "qq_handle_requests")

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:  # noqa: ARG002
        data, err = await _call(ctx, "get_group_system_msg", {})
        rows = _as_list(data)
        pending: list[str] = []
        for item in rows:
            for bucket in ("join_requests", "invited_requests"):
                for req in (item.get(bucket) if isinstance(item.get(bucket), list) else []):
                    if not isinstance(req, dict):
                        continue
                    who = str(req.get("user_id") or req.get("sender_uin") or "?")
                    why = str(req.get("msg") or req.get("answer") or "").strip()
                    group = str(req.get("group_id") or req.get("group_code") or "?")
                    flag = str(req.get("flag") or "").strip()
                    kind = "入群" if bucket == "join_requests" else "被邀请"
                    pending.append(f"- {kind} {who} → 群 {group}"
                                   + (f"｜验证话：{why[:60]}" if why else "")
                                   + (f"｜flag {flag}" if flag else ""))
        if not pending:
            return ToolResult.success("没有等着处理的申请。" + (f"（读失败：{err}）" if err else ""))
        return ToolResult.success("【等着处理的申请】\n" + "\n".join(pending[:30]))


class QQDecideRequest(Tool):
    name = "qq_decide_request"
    description = (
        "处理一条入群/好友申请：批或者不批，由你自己判断。"
        "flag 从 qq_requests 那里拿。批了就是放这个人进这个群，是个对外动作。"
    )
    hint = "能自己处理申请"
    params = (
        ToolParam("flag", "string", "申请的 flag"),
        ToolParam("approve", "boolean", "true 批、false 拒"),
        ToolParam("reason", "string", "拒的话写个理由", required=False),
    )
    primary_arg = "flag"
    sensitive = True

    def available(self, ctx: ToolContext) -> bool:
        return _qq_gate(ctx, "qq_handle_requests")

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
        flag = str(args.get("flag") or "").strip()
        if not flag:
            return ToolResult.failure("没给 flag", say="（要先知道是哪一条申请。）")
        approve = str(args.get("approve")).strip().lower() in ("1", "true", "yes", "y")
        # sub_type 必须是 add：那是协议端区分「加群」和「被邀进群」的字段，
        # 缺了它整条动作会被判无效但照样回 ok
        params: dict[str, Any] = {"flag": flag, "sub_type": "add", "approve": approve,
                                  "reason": str(args.get("reason") or "")}
        _, err = await _call(ctx, "set_group_add_request", params)
        if err:
            return ToolResult.failure(err, say="（这条申请没处理成，就说没弄动。）")
        return ToolResult.success(f"{'批了' if approve else '拒了'}一条申请。")


class QQChannels(Tool):
    name = "qq_channels"
    description = (
        "看自己有哪些 QQ 频道（guild）。只能看——频道里发帖、评论、点赞这套"
        "协议端根本没开放，所以这条读得到写不动，别答应别人去频道里发言。"
    )
    hint = "能看频道列表（读而已）"
    params: tuple[ToolParam, ...] = ()

    def available(self, ctx: ToolContext) -> bool:
        return _qq_gate(ctx, "qq_channel_enabled")

    async def run(self, ctx: ToolContext, args: dict[str, Any]) -> ToolResult:  # noqa: ARG002
        data, err = await _call(ctx, "get_guild_list", {})
        rows = _as_list(data)
        if not rows:
            return ToolResult.failure(err or "没有频道", say="（没看到有频道。）")
        lines = [f"【我的频道】{len(rows)} 个（协议端只给了读，发不了言）"]
        for item in rows[:30]:
            name = str(item.get("guild_name") or item.get("name") or "?").strip()
            gid = str(item.get("guild_id") or "").strip()
            lines.append(f"- {name}（{gid}）")
        return ToolResult.success("\n".join(lines))


# ---------------------------------------------------------------- 动态节流
def _qzone_state(ctx: ToolContext) -> Path:
    return ctx.settings.owner_dir / "QZONE.json"


def _qzone_wait(ctx: ToolContext) -> float:
    """离下一条动态还差多少秒；0 表示可以发。"""
    limit = float(ctx.settings.qq_qzone_min_interval_hours) * 3600
    if limit <= 0:
        return 0.0
    path = _qzone_state(ctx)
    if not path.is_file():
        return 0.0
    try:
        raw = json.loads(path.read_text("utf8"))
        last = float(raw.get("last_sent") or 0)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0.0
    return max(0.0, last + limit - time.time())


def _mark_qzone_sent(ctx: ToolContext) -> None:
    path = _qzone_state(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps({"last_sent": time.time(),
                                   "at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")},
                                  ensure_ascii=False, indent=2) + "\n")


ACCOUNT_TOOLS: Final[tuple[type[Tool], ...]] = (
    QQRoster, QQReadHistory, QQLike, QQPublishQZone, QQRequests, QQDecideRequest, QQChannels,
)
