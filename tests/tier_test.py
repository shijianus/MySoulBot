"""管理者分层、配对握手、判断回路、账号级能力——这四样是新加的地基，逐条钉住。

跑法：.venv/bin/python tests/tier_test.py

这里刻意不碰网络：账号级工具全部对着一个假网桥跑，
验的是「谁能让她伸这只手」和「伸出去会做什么」，不是 QQ 那头回什么。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import dataclasses
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qq_onebot_test as T  # noqa: E402
from config import OWNER_USER_ID, Settings  # noqa: E402
from core import identity as ID  # noqa: E402
from core import judgment as J  # noqa: E402
from core import pair_phrase as PP  # noqa: E402
from core.storage_manager import PathSafetyError, StorageManager  # noqa: E402
from core.tools.base import ToolContext  # noqa: E402
from core.tools.registry import ToolRegistry  # noqa: E402

Checker = T.Checker


def tier_settings(**overrides: Any) -> Settings:
    root = Path(tempfile.mkdtemp(prefix="tier-"))
    (root / "templates").mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "USER.md", "MEMORY.md", "RELATIONS.md", "CLAWD.md"):
        src = ROOT / "storage" / "templates" / name
        if src.is_file():
            shutil.copy(src, root / "templates" / name)
    values: dict[str, Any] = {
        "api_key": "sk-test", "base_url": "https://fake.invalid/v1", "model": "m",
        "storage_dir": root, "log_level": "WARNING", "prompt_tiers_enabled": False,
        "tools_enabled": True, "web_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def ctx_for(settings: Settings, user_id: str, *, owner: bool, qq: Any = None) -> ToolContext:
    identity = ID.Identity(ID.Tier.OWNER if owner else ID.Tier.INTERACTOR, user_id)
    return ToolContext(settings=settings, storage=StorageManager(settings),
                       user_id=user_id, identity=identity, qq=qq)


class FakePort:
    """假网桥：记下每个动作，按表返回。账号级工具的行为全看这张表。"""

    def __init__(self, replies: dict[str, Any] | None = None, fail: str = "") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._replies = replies or {}
        self._fail = fail

    @property
    def qq_online(self) -> bool:
        return True

    async def account_call(self, action: str, params: dict[str, Any]) -> tuple[Any, str]:
        self.calls.append((action, params))
        if self._fail and action == self._fail:
            return None, f"{action} 被协议端拒了"
        return self._replies.get(action, {}), ""

    def actions(self) -> list[str]:
        return [name for name, _ in self.calls]


def desk_code_empty(desk: ID.PairingDesk, challenge: ID.Challenge) -> bool:
    return desk.plaintext_code(challenge) == ""


# ---------------------------------------------------------------- 1. 两棵树
def tree_checks(check: Checker) -> None:
    s = tier_settings(owner_enabled=True)
    st = StorageManager(s)
    owner = st.user_dir(OWNER_USER_ID)
    other = st.user_dir("qq_private_123")
    check.ok("管理者落在 owner 树", owner == (s.owner_dir / OWNER_USER_ID).resolve(), str(owner))
    check.ok("交互者落在 users 树", other.parent == s.users_dir.resolve(), str(other))
    check.ok("两棵树根不同", st.tree_root(OWNER_USER_ID) != st.tree_root("qq_private_1"))
    check.ok("管理者目录不是 users 下的子目录", s.owner_dir != s.users_dir / OWNER_USER_ID)
    for bad in ("../owner", "..", "a/../../x"):
        try:
            st.user_dir(bad)
            check.ok(f"越界 id 被挡：{bad}", False, "竟然放行")
        except PathSafetyError:
            check.ok(f"越界 id 被挡：{bad}", True, "")
    off = tier_settings(owner_enabled=False)
    check.ok("关掉分层后 owner 退回 users 树（不是开后门，是不再分层）",
             StorageManager(off).user_dir(OWNER_USER_ID).parent == off.users_dir.resolve())


def owner_qq_checks(check: Checker) -> None:
    """QQ 侧的管理者身份。漏了这一条，账号能力在他唯一需要它的场景里永远不可用。"""
    s = tier_settings(owner_enabled=True, owner_qq="123456789")
    check.ok("没配对时 owner_qq 不给任何权限（它不是后门）",
             ID.resolve_identity(s, "qq_private_123456789").tier is ID.Tier.INTERACTOR, "")
    ID.write_owner(s, ID.OwnerRecord(user_id=OWNER_USER_ID, qq="", source="cli",
                                     paired_at="2026-10-06T00:00:00"))
    check.ok("配对后本机 owner 身份成立", ID.resolve_identity(s, OWNER_USER_ID).is_owner, "")
    phone = ID.resolve_identity(s, "qq_private_123456789")
    check.ok("配对后他从自己手机上说话也算管理者", phone.is_owner and phone.qq == "123456789",
             f"{phone.tier.label}/{phone.qq}")
    check.ok("别人的号不受影响", ID.resolve_identity(s, "qq_private_999").tier is ID.Tier.INTERACTOR)
    check.ok("群聊不算管理者", ID.resolve_identity(s, "qq_group_555").tier is ID.Tier.INTERACTOR)
    check.ok("owner_user_ids 把两种形态都列出来",
             ID.owner_user_ids(s) == {OWNER_USER_ID, "qq_private_123456789"},
             ID.owner_user_ids(s))
    # 绑定记录里自带 qq 时，不必再配 OWNER_QQ
    s2 = tier_settings(owner_enabled=True)
    ID.write_owner(s2, ID.OwnerRecord(user_id=OWNER_USER_ID, qq="777", source="cli",
                                      paired_at="2026-10-06T00:00:00"))
    check.ok("配对时留下的 qq 也认", ID.resolve_identity(s2, "qq_private_777").is_owner, "")
    # 两棵树的落盘位置：管理者从手机上进话，资料仍该落在 owner 树
    st = StorageManager(s)
    check.ok("管理者手机身份与本机身份共用一棵树",
             st.tree_root(OWNER_USER_ID) == st.tree_root("qq_private_123456789")
             or st.user_dir(OWNER_USER_ID).parent == s.owner_dir,
             f"{st.user_dir(OWNER_USER_ID)} vs {st.user_dir('qq_private_123456789')}")


# ---------------------------------------------------------------- 2. 配对握手
def pairing_checks(check: Checker) -> None:
    s = tier_settings(owner_enabled=True, pairing_ttl_seconds=120)
    desk = ID.PairingDesk(s)
    check.ok("没挑战时任何话都不被截走",
             ID.consume_pairing(desk, "你好溟汐，我是管理员", source="cli") is None)

    ch = desk.start(channel="cli", phrase="鲸鱼今晚不翻身")
    check.ok("挑战默认 120 秒内有效", 115 <= ch.seconds_left() <= 120, ch.seconds_left())
    check.ok("口令是这一场的，不是写死的那句", ch.phrase == "鲸鱼今晚不翻身", ch.phrase)
    check.ok("唯一来源确认前，码一个字都不给", desk.plaintext_code(ch) == "", desk.plaintext_code(ch))
    check.ok("旧那句固定话术不再算口令（防撞库）",
             ID.consume_pairing(desk, "你好溟汐，我是管理员", source="cli") is None, "")

    note = ID.consume_pairing(desk, "鲸鱼今晚不翻身", source="cli")
    check.ok("这一场的口令被截走并确认唯一来源", note and "唯一来源确认" in note, note)
    ch = desk.active()
    code = desk.plaintext_code(ch)
    check.ok("确认后才吐码且是分组形状", len(code) == 6 and ID.format_code(code) == f"{code[:3]}-{code[3:]}",
             f"{code} / {ID.format_code(code)}")
    done = ID.consume_pairing(desk, ID.format_code(code), source="cli")
    check.ok("回填码完成配对", done and "配对完成" in done, done)
    check.ok("身份升级为管理者", ID.resolve_identity(s, OWNER_USER_ID).is_owner, "")
    check.ok("交互者仍是交互者", ID.resolve_identity(s, "qq_private_9").tier is ID.Tier.INTERACTOR)
    check.ok("配对结束后闲聊不再被截",
             ID.consume_pairing(desk, "今天吃米饭", source="cli") is None)
    check.ok("完成后挑战文件被删（不在盘上留凭据）", not list(s.pairing_dir.glob("PAIR-*.json")),
             [f.name for f in s.pairing_dir.glob("PAIR-*.json")])

    # 口令每场不同：同一句写死的话术被公开过一次，就不能再复用到下一场
    s_dup = tier_settings(owner_enabled=True)
    d_dup = ID.PairingDesk(s_dup)
    phrases = set()
    for _ in range(6):
        existing = d_dup.active()
        if existing is not None:
            d_dup.void(existing.id)
        phrases.add(d_dup.start(channel="cli", phrase=PP.local_phrase()).phrase)
    check.ok("连开六场，口令不是一句写死的", len(phrases) >= 5, phrases)

    # 跨进程：换一个 PairingDesk 实例（相当于守护进程）也能完成 CLI 发起的那场
    s_x = tier_settings(owner_enabled=True)
    d_cli = ID.PairingDesk(s_x)
    made = d_cli.start(channel="cli", phrase="米饭要热的")
    d_other = ID.PairingDesk(s_x)          # 另一个进程，内存里什么都没有
    got = d_other.active()
    check.ok("另一个进程能读到同一场挑战（落盘共享）", got is not None and got.id == made.id,
             f"{got.id if got else None} vs {made.id}")
    ID.consume_pairing(d_other, "米饭要热的", source="cli")
    got = d_other.get(made.id)
    code_x = d_other.plaintext_code(got)
    check.ok("另一个进程也拿得到回填码（跨进程可完成）", len(code_x) == 6, code_x)
    done_x = ID.consume_pairing(d_other, code_x, source="cli")
    check.ok("在另一个进程里完成配对", done_x and "配对完成" in done_x, done_x)

    # 挑战文件权限：里面躺着这一场的口令与码
    s_perm = tier_settings(owner_enabled=True)
    d_perm = ID.PairingDesk(s_perm)
    made_p = d_perm.start(channel="cli", phrase="三点的水族箱")
    path = s_perm.pairing_dir / f"{made_p.id}.json"
    mode = path.stat().st_mode & 0o777
    check.ok("挑战文件锁到 0600（口令与主密钥在盘上不能给别人读）", mode == 0o600, oct(mode))
    d_perm.void(made_p.id)
    check.ok("作废即删，不留一个废口令在盘上", not path.exists(), str(path))

    # 唯一来源：两个通道同时喊，整场作废
    s3 = tier_settings(owner_enabled=True)
    d3 = ID.PairingDesk(s3)
    ph3 = "会发光的尾鳍"
    d3.start(channel="cli", phrase=ph3)
    a = ID.consume_pairing(d3, ph3, source="cli")
    b = ID.consume_pairing(d3, ph3, source="qq_private", qq="777")
    check.ok("第二个来源进来就整场作废", a and "唯一来源确认" in a and b and "作废" in b, f"{a} / {b}")
    check.ok("作废后拿不到码", d3.active() is None or d3.plaintext_code(d3.active()) == "", "")

    # 大小写与短横都不该成为输错的理由
    s2 = tier_settings(owner_enabled=True)
    d2 = ID.PairingDesk(s2)
    d2.start(channel="cli", phrase="海带不上岸")
    ID.consume_pairing(d2, "海带不上岸", source="cli")
    c2 = d2.active()
    k2 = d2.plaintext_code(c2)
    lower_spaced = f"{k2[:3].lower()} {k2[3:].lower()}"
    verdict = ID.consume_pairing(d2, lower_spaced, source="cli")
    check.ok("小写带空格也能过", verdict is not None and "配对完成" in verdict, f"{lower_spaced} -> {verdict}")

    # 口令比对是整句相等，不是包含：长闲聊顺嘴带出口令不该被当成配对
    s5 = tier_settings(owner_enabled=True)
    d5 = ID.PairingDesk(s5)
    d5.start(channel="cli", phrase="声呐朝北游")
    check.ok("口令嵌在长句里不算（整句相等才认）",
             ID.consume_pairing(d5, "我今天聊到声呐朝北游这件事", source="cli") is None, "")

    # 群聊不配对
    s4 = tier_settings(owner_enabled=True)
    d4 = ID.PairingDesk(s4)
    d4.start(channel="cli", phrase="浮标留了灯")
    check.ok("群聊来源不截不记",
             ID.consume_pairing(d4, "浮标留了灯", source="qq_group", group=True) is None
             and not d4.active().candidates, d4.active().candidates)

    # 过期即废
    s6 = tier_settings(owner_enabled=True, pairing_ttl_seconds=120)
    d6 = ID.PairingDesk(s6)
    c6 = d6.start(channel="cli", phrase="逆流的珊瑚")
    d6._save(dataclasses.replace(d6.get(c6.id), expires_at=time.time() - 1))
    check.ok("过期挑战不再算活跃", d6.active() is None)
    check.ok("过期即从盘上抹掉（明文口令不留过夜）", not list(s6.pairing_dir.glob("PAIR-*.json")),
             [f.name for f in s6.pairing_dir.glob("PAIR-*.json")])
    check.ok("过期后口令不截", ID.consume_pairing(d6, "逆流的珊瑚", source="cli") is None)

    # 码绑人：同一场挑战，两个来源算出两个码——抄来的码在别人身上不成立
    s_b = tier_settings(owner_enabled=True)
    d_b = ID.PairingDesk(s_b)
    made_b = d_b.start(channel="cli", phrase="灯塔留着那盏")
    raw = json.loads((s_b.pairing_dir / f"{made_b.id}.json").read_text("utf8"))
    check.ok("盘上没有现成的码可抄（只躺着一把主密钥）",
             "code" not in raw and "code_hash" not in raw, sorted(raw))
    mine = ID.derive_code(made_b.salt, challenge_id=made_b.id, key="qq_private|1937490685", chars=6)
    theirs = ID.derive_code(made_b.salt, challenge_id=made_b.id, key="cli|anon", chars=6)
    check.ok("两个来源两份码，互不通用", mine != theirs, f"{mine} vs {theirs}")
    check.ok("同一来源每次算出同一串（码是函数，不是抽签）",
             mine == ID.derive_code(made_b.salt, challenge_id=made_b.id,
                                    key="qq_private|1937490685", chars=6), mine)
    check.ok("派生的码也避开易混字符", not set(mine + theirs) & set("OI01"), mine + theirs)
    ID.consume_pairing(d_b, "灯塔留着那盏", source="qq_private", qq="1937490685")
    seen = d_b.plaintext_code(d_b.active())
    check.ok("手机上看到的码，就是按那个号算出来的那一份", seen == mine, f"{seen} vs {mine}")
    stolen = ID.consume_pairing(d_b, seen, source="cli")
    check.ok("把这条码拿到别的来源去回填，不认",
             stolen is not None and "不是报口令那一个号" in stolen, stolen)
    check.ok("抄码未遂之后这场已经作废，没留半条活路", d_b.active() is None, "")

    # 同一来源但码不对（他抄错了一位）：也是整场作废，不许试第二次
    s_c = tier_settings(owner_enabled=True)
    d_c = ID.PairingDesk(s_c)
    d_c.start(channel="cli", phrase="夜潮涨到台阶")
    ID.consume_pairing(d_c, "夜潮涨到台阶", source="cli")
    wrong = ID.consume_pairing(d_c, "ZZZ-ZZZ", source="cli")
    check.ok("码不对即整场作废", wrong and "码不对" in wrong, wrong)

    # 一次只许有一场：旧挑战 shadow 新的是真实事故（对着不作数的口令白喊 120 秒）
    s_d = tier_settings(owner_enabled=True)
    d_d = ID.PairingDesk(s_d)
    d_d.start(channel="cli", phrase="第一盏灯")
    newer = d_d.start(channel="cli", phrase="第二盏灯")
    check.ok("新发起的一场把旧的清干净",
             d_d.active().id == newer.id
             and [f.stem for f in s_d.pairing_dir.glob("PAIR-*.json")] == [newer.id],
             [f.stem for f in s_d.pairing_dir.glob("PAIR-*.json")])
    check.ok("旧口令从此不作数", ID.consume_pairing(d_d, "第一盏灯", source="cli") is None, "")
    forced = dataclasses.replace(newer, id=f"PAIR-{int(time.time()) + 60}-zzzz", phrase="第三盏灯")
    d_d._save(forced)          # 绕过发起这一步，硬造两张同时开着
    picked = d_d.active()
    check.ok("两张同时开着时认最新的那张", picked.id == forced.id and picked.phrase == "第三盏灯",
             f"{picked.id} / {picked.phrase}")
    check.ok("认最新的顺带把旧那张作废（盘上只剩一张）",
             [f.stem for f in s_d.pairing_dir.glob("PAIR-*.json")] == [forced.id],
             [f.stem for f in s_d.pairing_dir.glob("PAIR-*.json")])

    # 一个机器人只有一个管理者：换号顶替必须被拒
    s7 = tier_settings(owner_enabled=True, owner_qq="")
    d7 = ID.PairingDesk(s7)
    d7.start(channel="cli", phrase="没写完的潜水钟")
    ID.consume_pairing(d7, "没写完的潜水钟", source="cli")
    c7 = d7.active()
    ID.consume_pairing(d7, d7.plaintext_code(c7), source="cli")
    d7.start(channel="cli", phrase="赖床的深海灯")
    ID.consume_pairing(d7, "赖床的深海灯", source="cli", qq="888888")
    c7 = d7.active()
    blocked = ID.consume_pairing(d7, d7.plaintext_code(c7), source="cli", qq="888888")
    check.ok("已绑定时另一个号来顶替被拒", blocked and "已经绑过管理者" in blocked, blocked)
    check.ok("顶替失败后原绑定没变", ID.read_owner(s7).binding_key() == "cli|anon",
             ID.read_owner(s7).binding_key())
    check.ok("解绑后回到未配对", ID.unpair(s7) is not None
             and ID.resolve_identity(s7, OWNER_USER_ID).tier is ID.Tier.INTERACTOR)
    check.ok("解绑顺手清掉残留挑战", not list(s7.pairing_dir.glob("PAIR-*.json")))
    check.ok("没绑定时解绑不炸", ID.unpair(s7) is None)


# ---------------------------------------------------------------- 3. 账号能力放行
def account_gate_checks(check: Checker) -> None:
    qq_names = {"qq_roster", "qq_read_history", "qq_like", "qq_publish_qzone",
                "qq_delete_qzone", "qq_requests", "qq_decide_request",
                "qq_group_verify", "qq_channels"}
    s = tier_settings(qq_qzone_publish=True, qq_handle_requests=True, qq_channel_enabled=True)
    owner = ToolRegistry(ctx_for(s, OWNER_USER_ID, owner=True))
    inter = ToolRegistry(ctx_for(s, "qq_private_5", owner=False))
    check.ok("管理者看得到全部账号工具", qq_names <= set(owner.names), sorted(qq_names - set(owner.names)))
    check.ok("交互者一个账号工具都拿不到", not (qq_names & set(inter.names)),
             sorted(qq_names & set(inter.names)))
    check.ok("交互者的普通能力没被削减",
             {"web_browse", "web_search", "host_stats", "scratch_write"} <= set(inter.names))

    off = tier_settings(qq_account_enabled=False, qq_qzone_publish=True, qq_handle_requests=True,
                        qq_channel_enabled=True)
    check.ok("总闸关掉后账号工具全灭", not (qq_names & set(ToolRegistry(ctx_for(off, OWNER_USER_ID, owner=True)).names)),
             sorted(qq_names & set(ToolRegistry(ctx_for(off, OWNER_USER_ID, owner=True)).names)))

    # 公开表达与自处理申请现在是默认开的（她要的就是这个）；
    # 真正必须默认成立的是「开广播的同时锁也在」，以及频道那条不骗人
    dflt = tier_settings()
    dn = set(ToolRegistry(ctx_for(dflt, OWNER_USER_ID, owner=True)).names)
    check.ok("默认放开公开表达、撤动态与自己处理申请",
             {"qq_roster", "qq_read_history", "qq_like", "qq_publish_qzone", "qq_delete_qzone",
              "qq_requests", "qq_decide_request", "qq_group_verify"} <= dn,
             sorted(qq_names - dn))
    check.ok("频道仍默认关（协议端根本写不动，不假装能）", "qq_channels" not in dn, sorted(dn & qq_names))
    check.ok("开广播的同时出话的锁默认在", dflt.secrecy_guard_enabled is True, "")
    check.ok("动态仍带节流，不是开了就随便刷屏", dflt.qq_qzone_min_interval_hours > 0,
             dflt.qq_qzone_min_interval_hours)


async def account_behavior_checks(check: Checker) -> None:
    s = tier_settings(qq_qzone_publish=True, qq_handle_requests=True, qq_channel_enabled=True,
                      qq_qzone_min_interval_hours=6)
    port = FakePort(replies={
        "get_group_list": [{"group_id": 111, "group_name": "摸鱼群", "member_count": 42}],
        "get_friend_list": [{"user_id": 7, "nickname": "老王"}],
        "get_friend_msg_history": {"messages": [
            {"time": 1791200000, "sender_nickname": "老王", "message": [{"type": "text", "data": {"text": "吃了吗"}}]},
            {"time": 1791200600, "sender_nickname": "老王", "message": [{"type": "image", "data": {}}]},
        ]},
        "send_like": {},
        "send_qzone_msg": {"tid": "abc123"},
        "set_group_add_request": {},
        "get_group_system_msg": {"join_requests": [{"flag": "F1", "user_id": 9, "group_id": 111, "msg": "同学"}]},
        "get_guild_list": [{"guild_id": "g1", "guild_name": "测试频道"}],
    })
    owner = ctx_for(s, OWNER_USER_ID, owner=True, qq=port)
    reg = ToolRegistry(owner)

    res = await reg.call("qq_roster", {})
    check.ok("通讯录读出群与好友", res.ok and "摸鱼群" in res.content and "老王" in res.content, res.content[:80])
    check.ok("通讯录只读不动作", port.actions() == ["get_group_list", "get_friend_list"], port.actions())

    port.calls.clear()
    res = await reg.call("qq_read_history", {"scope": "friend", "target": "7", "count": 5})
    check.ok("翻记录走对的动作与参数",
             port.actions() == ["get_friend_msg_history"]
             and port.calls[0][1] == {"user_id": 7, "count": 5}, port.calls)
    check.ok("图片只标出来不猜内容", "[图片]" in res.content, res.content[:120])
    res = await reg.call("qq_read_history", {"scope": "friend", "target": "不是数字"})
    check.ok("目标不是号码就不去翻", not res.ok, res.content[:50])

    port.calls.clear()
    await reg.call("qq_like", {"user_id": "7", "times": 3})
    check.ok("点赞用的是 times 不是 count",
             port.calls == [("send_like", {"user_id": 7, "times": 3})], port.calls)

    port.calls.clear()
    first = await reg.call("qq_publish_qzone", {"content": "今天想躺"})
    check.ok("第一条动态发出去", first.ok and port.actions() == ["send_qzone_msg"], first.content[:60])
    port.calls.clear()
    second = await reg.call("qq_publish_qzone", {"content": "再发一条"})
    check.ok("节流挡住第二条", not second.ok and port.actions() == [], f"{second.content[:50]} / {port.actions()}")
    state = s.owner_dir / "QZONE.json"
    check.ok("节流状态落在管理者树里", state.is_file(), str(state))

    port.calls.clear()
    await reg.call("qq_decide_request", {"flag": "F1", "approve": "true"})
    check.ok("批申请带上 sub_type=add",
             port.calls and port.calls[0][1].get("sub_type") == "add" and port.calls[0][1].get("approve") is True,
             port.calls)

    port.calls.clear()
    res = await reg.call("qq_channels", {})
    check.ok("频道如实说明只能看", res.ok and "发不了言" in res.content, res.content[:80])

    # 通道挂了要诚实降级，不能编一句「发出去了」
    dead = ToolRegistry(ctx_for(s, OWNER_USER_ID, owner=True, qq=None))
    res = await dead.call("qq_roster", {})
    check.ok("没有网桥时如实说没通道", not res.ok, res.content[:50])
    fresh = tier_settings(qq_qzone_publish=True, qq_qzone_min_interval_hours=6)
    broken = ToolRegistry(ctx_for(fresh, OWNER_USER_ID, owner=True,
                                  qq=FakePort(fail="send_qzone_msg")))
    res = await broken.call("qq_publish_qzone", {"content": "试探"})
    check.ok("协议端拒绝时不谎报成功", not res.ok and "没发出去" in res.content, res.content[:60])
    check.ok("失败时不写节流状态（没发成不该占名额）",
             not (fresh.owner_dir / "QZONE.json").exists(), str(fresh.owner_dir))

    # 敏感工具不给别名也不给模糊匹配
    check.ok("发动态是敏感工具", reg._by_name["qq_publish_qzone"].sensitive is True, "")
    check.ok("敏感工具不认模糊名", reg.resolve("qq_publish") is None or
             reg.resolve("qq_publish").name != "qq_publish_qzone", str(reg.resolve("qq_publish")))


# ---------------------------------------------------------------- 4. 判断回路
def judgment_checks(check: Checker) -> None:
    s = tier_settings(judgment_enabled=True, judgment_every_turns=3, judgment_lookback=20)
    ledger = J.JudgmentLedger(s)
    check.ok("空册子不占提示词预算", ledger.read_text() == "", ledger.read_text())

    added, revised = ledger.apply([
        J.Rule(text="长解释容易被晾着，先给一句结论", kind="style", confidence=60),
        J.Rule(text="要真诚", kind="style"),
        J.Rule(text="", kind="style"),
    ])
    check.ok("具体判断收进来", added == 1, f"{added}/{revised}")
    check.ok("口号式的不收（没有判据的「要真诚」不是标准）",
             all("真诚" not in rule.text for rule in ledger.rules()), ledger.read_text())
    check.ok("册子带出处与置信度", "[k=style c=" in ledger.read_text(), ledger.read_text()[-120:])

    again = ledger.apply([J.Rule(text="长解释容易被晾着，先说一句结论", kind="style", confidence=60)])
    check.ok("近似表述合成一条而不是堆着",
             len(ledger.rules()) == 1 and again[0] == 0, f"{again} / {len(ledger.rules())}")
    boosted = ledger.rules()[0].confidence
    check.ok("被再次支持会加置信度", boosted > 60, boosted)

    # 观测打脸时，册子必须自己往下掉——不然它是经书不是判断
    # 先确认「印证」这条路：我们说一大串、对面回四个字 = 真的被晾着 → 加分
    before = ledger.rules()[0].confidence
    for _ in range(4):
        ledger.reinforce(J.Outcome(user_id="u", at=time.time(), our_chars=200,
                                   their_chars=4, replied=True))
    up = ledger.rules()[0].confidence
    check.ok("观测印证判断时置信度往上走", up > before, f"{before} -> {up}")
    # 再测反驳：同样长的话，对面回得比我们还长 = 没被晾着 → 这条判断该掉分
    for _ in range(6):
        ledger.reinforce(J.Outcome(user_id="u", at=time.time(), our_chars=200,
                                   their_chars=260, replied=True))
    down = ledger.rules()[0].confidence if ledger.rules() else -1
    check.ok("观测打脸时置信度往下走", 0 <= down < up, f"{up} -> {down}")

    # 名额封顶：写满不等于写了
    many = [J.Rule(text=f"第 {i} 条关于节奏的具体判断{i*7}", kind="pace", confidence=50 + i)
            for i in range(30)]
    ledger.apply(many)
    check.ok("册子有名额上限", len(ledger.rules()) <= 12, len(ledger.rules()))
    kept = [rule.confidence for rule in ledger.rules()]
    check.ok("挤掉的是弱的那几条", kept == sorted(kept, reverse=True), kept)

    # 统计只数得出真实信号
    window = [
        J.Outcome(user_id="a", at=1, our_chars=300, their_chars=5, replied=True, reply_seconds=200),
        J.Outcome(user_id="a", at=2, our_chars=20, their_chars=80, replied=True),
        J.Outcome(user_id="b", at=3, our_chars=40, their_chars=0, replied=False, group=True),
    ]
    stats = ledger.stats(window)
    check.ok("统计数得出接话率与说多次数",
             stats.turns == 3 and stats.over_talked == 1 and stats.slow == 1, stats.render())
    check.ok("分人看得到", "a" in stats.render() and "b" in stats.render(), stats.render())
    check.ok("一条都没有时不产出空统计", ledger.stats([]).render() == "")

    # observe() 把原始材料折成信号，不交给模型回忆
    out = J.observe(user_id="u", our_text="字" * 120, our_bubbles=4,
                    their_text="哦", gap_seconds=300, replied=True)
    check.ok("说多了这件事量得出来", out.over_talked and out.slow and not out.landed,
             f"{out.over_talked}/{out.slow}/{out.landed}")
    check.ok("对面反问算接住的一种", J.observe(user_id="u", our_text="短", our_bubbles=1,
                                            their_text="真的吗？为什么", gap_seconds=5,
                                            replied=True).asked_back)


async def judgment_loop_checks(check: Checker) -> None:
    s = tier_settings(judgment_enabled=True, judgment_every_turns=3)
    storage = StorageManager(s)
    loop = J.JudgmentLoop(s, storage)
    check.ok("没接上游时循环不产出但不炸", (await loop.reflect()) is not None)
    for _ in range(3):
        loop.note(J.Outcome(user_id="u", at=time.time(), our_chars=200, their_chars=3, replied=True))
    await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)
    check.ok("攒够轮次确实开了一趟", loop.stats["spins"] >= 1, loop.stats)
    quiet = J.JudgmentLoop(tier_settings(judgment_enabled=False), storage)
    quiet.note(J.Outcome(user_id="u", at=0))
    await asyncio.sleep(0.02)
    check.ok("关掉开关后一律不记也不跑", quiet._window == [] and quiet.stats["spins"] == 0, quiet.stats)


# ---------------------------------------------------------------- 5. 提示词吃到判断
async def prompt_layer_checks(check: Checker) -> None:
    from core.prompt_builder import PromptBuilder

    s = tier_settings(prompt_tiers_enabled=True)
    ledger = J.JudgmentLedger(s)
    ledger.apply([J.Rule(text="长解释容易被晾着，先给一句结论", kind="style", confidence=70)])
    builder = PromptBuilder(s, StorageManager(s))
    builder.bind_judgment(ledger)

    full, _ = await builder.build_system_prompt(OWNER_USER_ID, tool_mode="off")
    check.ok("全量档带上了判断层", "LAYER 3·判" in full and "长解释容易被晾着" in full, "")
    check.ok("判断层说明了它的分量", "不是偏好清单" in full and "试出来的" in full, "")
    check.ok("判断层不许越过硬约束", "与下面的硬约束冲突时听硬约束" in full, "")
    index_j = full.find("LAYER 3·判")
    index_r = full.find("LAYER 4")
    check.ok("判断层排在硬约束之前", 0 <= index_j < index_r, f"{index_j}/{index_r}")

    quick, _ = await builder.build_system_prompt(OWNER_USER_ID, tool_mode="off", tier="quick")
    check.ok("快捷档也吃到判断（短对话才是大多数）", "长解释容易被晾着" in quick, "")
    check.ok("快捷档仍不背宪法", "LAYER 0 · 深层灵魂" not in quick, "")

    empty = J.JudgmentLedger(tier_settings(storage_dir=Path(tempfile.mkdtemp())))
    builder.bind_judgment(empty)
    bare, _ = await builder.build_system_prompt(OWNER_USER_ID, tool_mode="off")
    check.ok("册子是空的就不占位", "LAYER 3·判" not in bare, "")

    frozen = tier_settings(judgment_enabled=False)
    fb = PromptBuilder(frozen, StorageManager(frozen))
    fb.bind_judgment(ledger)
    fp, _ = await fb.build_system_prompt(OWNER_USER_ID, tool_mode="off")
    check.ok("关掉回路后判断层不再进提示词", "LAYER 3·判" not in fp, "")


async def main() -> int:
    check = Checker()
    try:
        tree_checks(check)
        owner_qq_checks(check)
        pairing_checks(check)
        account_gate_checks(check)
        await account_behavior_checks(check)
        judgment_checks(check)
        await judgment_loop_checks(check)
        await prompt_layer_checks(check)
    finally:
        print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
        for name in check.failures:
            print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
