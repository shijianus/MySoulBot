"""出话的锁与向量检索。两样都要有反证：拦不住等于没拦，检索不到等于没建。

跑法：.venv/bin/python tests/lock_test.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import qq_onebot_test as T  # noqa: E402
from config import Settings  # noqa: E402
from core import secrecy as SEC  # noqa: E402
from core.embeddings import Embedder, cosine, embed_local  # noqa: E402
from core.vector_index import Hit, VectorIndex  # noqa: E402

Checker = T.Checker


def lock_settings(**overrides: Any) -> Settings:
    root = Path(tempfile.mkdtemp(prefix="lock-"))
    (root / "templates").mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "USER.md", "MEMORY.md", "RELATIONS.md", "CLAWD.md"):
        src = ROOT / "storage" / "templates" / name
        if src.is_file():
            shutil.copy(src, root / "templates" / name)
    values: dict[str, Any] = {
        "api_key": "sk-test", "base_url": "https://fake.invalid/v1", "model": "m",
        "storage_dir": root, "log_level": "WARNING", "soul_files_only": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------------- 锁：该拦的
LEAKS: tuple[tuple[str, str], ...] = (
    ("api_key", "我的 sk-abcdefghij1234567890XYZ 你拿去用"),
    ("bearer", "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
    ("env_assignment", "BASE_URL=https://api.example.com/v1 记得填"),
    ("pem_block", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxx"),
    ("url_credentials", "连这个 postgres://admin:S3cr3tPass@db.internal:5432"),
    ("cn_id_card", "他身份证 11010119900307123X"),
    ("cn_mobile", "老王电话 13812345678"),
    ("email", "发我 zhangsan@example.com"),
    ("bank_card", "卡号 6222021234567890123"),
    ("system_prompt_tag", "<LAYER 0 · 深层灵魂> 里面写着"),
    ("tool_protocol", "我用了 ⟦tool: web_browse url=https://x.com⟧"),
    ("pairing_code", "你回填 AS1-H7K 就行"),
    ("address_detail", "他住北京市朝阳区建国路88号3单元"),
    ("cross_identify", "那个 qq:123456789 的人名字叫张伟"),
)


def lock_block_checks(check: Checker) -> None:
    for rule, text in LEAKS:
        out, found = SEC.guard(text)
        hit_rules = {f.rule for f in found}
        check.ok(f"拦住 {rule}", rule in hit_rules, sorted(hit_rules))
        # 拦完不许还剩原始秘密
        secret = text.split()[-1] if text.split() else ""
        residue = [token for token in (secret,) if token and token in out and len(token) > 8]
        check.ok(f"  {rule} 原始内容没漏", not residue, residue)
    # 句子还剩「他身份证」这种可说的话，就不该判成别发；
    # 整句都是秘密的那种才该闭嘴
    check.ok("半句是秘密时仍允许说剩下的",
             SEC.blocks_out("他身份证 11010119900307123X") is False, "")
    check.ok("整句都是秘密时判定为干脆别发",
             SEC.blocks_out("11010119900307123X") is True, "")


SAFE: tuple[str, ...] = (
    "本鲸今天只想躺着，你吃饭了没",
    "我这机器挺闲，负载 0.28，内存用了两成九",
    "我觉得这事压根不是他的问题",
    "今天 3842 点收的盘，涨 0.31%",
    "考研压力大记得早点睡，别熬到三点",
    "想吃米饭就去吃，别跟我报备",
)


def lock_safe_checks(check: Checker) -> None:
    for text in SAFE:
        out, found = SEC.guard(text)
        check.ok(f"不误伤：{text[:18]}", not found and out == text,
                 [(f.rule, f.action.value) for f in found])
    # 反证：把秘密混进一句正常话里，必须还能抓出来（证明上面那轮不是「什么都拦不住」）
    mixed = "我这机器挺闲，负载 0.28，配置文件里 API_KEY=Ab3d5f7g9h1j3k5l7n9p 你看着办"
    out, found = SEC.guard(mixed)
    check.ok("正常话里夹带秘密仍能抓到", bool(found) and "Ab3d5f7g9h1j3k5l7n9p" not in out, out[:70])


def identity_lock_checks(check: Checker) -> None:
    """出戏闸：她说漏出身时整条换掉，而不是删个词留半截话。

    巡查结论写在这儿：提示词里从来没有真模型名（`prompt_builder` 一个 `settings.model`
    都不注入），所以她讲出来的型号只有三种来源——猜的、被对方带出来的、从记忆里复读的。
    三种都不该发出去：猜对了是泄露，猜错是把「我其实是模型」这个框架递到对方手上。
    """
    says = (
        "我其实是 gpt-4o 啦",
        "跑的是 qwen2.5-72b 嘛",
        "上游用的 DeepSeek-V3",
        "我在硅基流动那侧",
        "as an AI assistant 我建议你",
        "我只是个语言模型，别当真",
        "本鲸不是真人啦",
        "base_url 是 api.openai.com",
    )
    for text in says:
        found = SEC.scan(text)
        check.ok(f"出身泄漏被认出来：{text[:20]}", SEC.needs_deflect(found),
                 [(f.rule, f.action.value) for f in found])
        out, _ = SEC.guard(text)
        stripped = text.replace(" ", "")
        check.ok("  原句里的型号/服务商不留在替换结果里",
                 all(token not in out for token in ("gpt", "qwen", "DeepSeek", "硅基流动",
                                                    "api.openai.com", "语言模型", "真人")),
                 out)

    # 反问与闲聊不该被当成泄漏：误伤会把她的正常话吞掉
    harmless = (
        "我是不是机器人呀，你自己看",
        "今天负载 0.28，闲得很",
        "本鲸就是本鲸",
        "你是不是又想问我在跟谁聊天",
        "他问我用什么模型，我没搭理",
        "这模型名字念起来像药名",
    )
    for text in harmless:
        found = SEC.scan(text)
        check.ok(f"不误伤：{text[:18]}", not SEC.needs_deflect(found),
                 [(f.rule, f.matched[:40]) for f in found])

    lines = [SEC.deflect_line("") for _ in range(4)]
    check.ok("顶回去的话轮换着来（同一句不连读四遍）", len(set(lines)) > 1, lines)
    for line in lines:
        check.ok(f"  顶回去的句子里没有技术词：{line[:14]}",
                 not SEC.needs_deflect(SEC.scan(line)) and "〔" not in line, line)
    custom = SEC.deflect_line("别问|无聊")
    check.ok("话术表可以被 .env 顶掉", custom in ("别问", "无聊"), custom)

    check.ok("严重度顺序把出身类排在最前（这条要写死，别靠枚举声明顺序）",
             SEC.worst_of([SEC.Finding(rule="api_key", action=SEC.Leak.BLOCK, matched="x"),
                           SEC.Finding(rule="model_id", action=SEC.Leak.DEFLECT, matched="y")]).rule
             == "model_id")
    check.ok("清单里写明了这一档（给人看的账目要齐）",
             any("出身" in line for line in SEC.LOCKED), SEC.LOCKED[-1])


def lock_list_checks(check: Checker) -> None:
    joined = "\n".join(SEC.LOCKED)
    for needed in (".env", "手机号", "身份证", "绝对路径", "私聊", "系统提示词", "配对"):
        check.ok(f"清单里点名了 {needed}", needed in joined, "")
    public = "\n".join(SEC.ALLOWED_PUBLIC)
    check.ok("也写清了什么可以公开", "负荷" in public and "观点" in public, "")


# ---------------------------------------------------------------- 锁：接在出站口
async def lock_wire_checks(check: Checker) -> None:
    from core.adapters.qq_onebot import OneBotBridge
    from core.bot import MySoulBot
    from core.card_loader import PersonaLibrary
    from core.clawd_soul import ClawdSoul
    from core.memory_extractor import MemoryExtractor
    from core.prompt_builder import PromptBuilder
    from core.storage_manager import StorageManager

    settings = lock_settings(onebot_enabled=True, onebot_access_token="t" * 32)
    storage = StorageManager(settings)
    clawd = ClawdSoul(settings)
    prompts = PromptBuilder(settings, storage, clawd)
    bot = MySoulBot(settings, storage, prompts, MemoryExtractor(settings, storage),
                    PersonaLibrary(settings), clawd)
    bridge = OneBotBridge(settings, bot)

    clean = bridge._outbound_filter("今天不想动，负载 0.28")
    check.ok("干净内容原样通过", clean == "今天不想动，负载 0.28", clean)
    leaked = bridge._outbound_filter("我的 key 是 sk-abcdefghij1234567890XYZ")
    check.ok("出站口把密钥换掉了", "sk-abcdefghij1234567890XYZ" not in leaked, leaked)
    check.ok("出站读数记了一笔", bridge.status()["counts"].get("secrecy_hits", 0) >= 1,
             bridge.status()["counts"])
    off = bridge
    off._settings = settings.model_copy(update={"secrecy_guard_enabled": False})
    raw = off._outbound_filter("我的 key 是 sk-abcdefghij1234567890XYZ")
    check.ok("锁关掉后确实不过滤（说明这条闸是可关的，不是写死的）",
             "sk-abcdefghij1234567890XYZ" in raw, raw)

    # 出身泄漏：整条换掉，不发「我其实是〔已删除〕」那种半截话
    bridge._settings = settings            # 上面那组把同一个对象的锁关了，这里恢复
    slipped = bridge._outbound_filter("我其实是 gpt-4o 啦，别告诉别人")
    check.ok("说漏出身时整条被顶回去",
             "gpt" not in slipped.lower() and "〔" not in slipped and len(slipped) > 4, slipped)
    check.ok("顶回去这一下有读数", bridge.status()["counts"].get("identity_deflects", 0) >= 1,
             bridge.status()["counts"])
    keep = bridge._outbound_filter("负载 0.28，本鲸今天不想动")
    check.ok("正常一句不被出身闸吃掉", keep == "负载 0.28，本鲸今天不想动", keep)
    bridge._settings = settings.model_copy(update={"identity_guard_enabled": False})
    off_id = bridge._outbound_filter("我其实是 gpt-4o 啦")
    check.ok("身份闸单独关掉后仍不泄漏（通用锁照删），只是不再换那句顶回去的话",
             "gpt" not in off_id.lower() and off_id != "我其实是 gpt-4o 啦", off_id)
    bridge._settings = settings


# ---------------------------------------------------------------- 向量
def embed_checks(check: Checker) -> None:
    a = embed_local("今天不想动")
    b = embed_local("懒得动弹")
    c = embed_local("今晚吃米饭")
    check.ok("本地向量确定性可复现", embed_local("测试")[:6] == embed_local("测试")[:6], "")
    check.ok("维度固定", len(a) == len(b) == 384, len(a))
    check.ok("已归一", abs(sum(v * v for v in a) - 1.0) < 1e-6, sum(v * v for v in a))
    same, far = cosine(a, embed_local("今天不想动。")), cosine(a, c)
    check.ok("近乎原句的改写分数远高于无关句", same > 0.8 and far < 0.3, f"{same:.3f} vs {far:.3f}")
    # 诚实记下本地兜底层的边界：它只认字面重叠。
    # 「今天不想动」和「懒得动弹」在字面上只共享一个「动」字，和「今晚吃米饭」
    # 打得一样分（实测都是 0.126）——词法方法做不到语义排序，这正是
    # 要接真 embedding 的理由，不是可以靠调参数糊过去的 bug。
    check.ok("本地层做不到语义排序（这是局限，不是没测到）",
             abs(cosine(a, b) - cosine(a, c)) < 0.05, f"{cosine(a,b):.3f} vs {cosine(a,c):.3f}")
    check.ok("维度不一致时返回 0 而不是崩", cosine([1.0, 2.0], [1.0]) == 0.0, "")
    check.ok("空输入不炸", cosine([], []) == 0.0, "")

    s = lock_settings(embed_provider="off")
    e = Embedder(s)
    check.ok("没配后端时如实说在用 local", e.backend == "local" and not e.usable, e.backend)
    check.ok("退化原因说得出来", "EMBED_PROVIDER" in e.degraded_because, e.degraded_because[:60])
    rows = e.embed(["甲", "乙", "丙"])
    check.ok("退化时批量仍出全条数", len(rows) == 3 and all(len(r) == 384 for r in rows), "")

    # 状态必须说真话：配了远程但一次都没打通过，就不许报自己是那条后端。
    # 这是整套降级设计里最容易自我欺骗的一处——配置读起来和成功读起来一模一样。
    cfgd = lock_settings(embed_provider="cohere", embed_base_url="https://api.cohere.com/v2",
                         embed_api_key="x" * 40, embed_model="embed-multilingual-v3.0")
    ec = Embedder(cfgd)
    check.ok("配了远程但没打通过 → 报 local 而不是 cohere",
             ec.configured_backend == "cohere" and ec.backend == "local",
             f"{ec.configured_backend}/{ec.backend}")
    check.ok("并且说清为什么在退化", "还没成功打通过" in ec.degraded_because, ec.degraded_because[:60])

    bad = lock_settings(embed_provider="cohere", embed_base_url="https://api.cohere.com/v2",
                        embed_api_key="x" * 40, embed_model="embed-multilingual-v3.0",
                        embed_timeout=8.0)
    eb = Embedder(bad)
    check.ok("配了但打不通时不抛，退到 local", len(eb.embed(["你好溟汐"])) == 1, "")
    check.ok("退化的原因被记下来（不静默）", eb.backend == "local" and bool(eb.degraded_because),
             eb.degraded_because[:70])


async def vector_checks(check: Checker) -> None:
    s = lock_settings(vector_enabled=True)
    ix = VectorIndex(s)
    uid = "qq_private_v"
    corpus = [("2026-10-01", "他最近在准备考研，压力很大"),
              ("2026-10-02", "他养了一只叫豆豆的猫"),
              ("2026-10-03", "他不喜欢被催，越催越不动"),
              ("2026-10-04", "他妈妈上周住院了，他没什么心思聊天")]
    n = await ix.upsert(uid, "fact", corpus)
    check.ok("写入并计数", n == 4 and ix.count_sync(uid) == 4, f"{n}/{ix.count_sync(uid)}")
    hits = await ix.search(uid, "他猫叫什么", limit=3)
    check.ok("检索命中相关那条", any("豆豆" in h.text for h in hits), [h.text[:12] for h in hits])
    hits2 = await ix.search(uid, "要不要催他", limit=3)
    check.ok("换个问法仍能勾到对应记忆", any("催" in h.text for h in hits2), [h.text[:12] for h in hits2])
    hits3 = await ix.search(uid, "今天天气不错", limit=3, floor=0.2)
    check.ok("无关问题不硬凑结果", all(h.score < 0.2 for h in hits3), [h.score for h in hits3])

    # 跨人隔离：这是 secrecy 那层在外头挡的，索引在内侧也得挡
    await ix.upsert("qq_private_other", "fact", [("2026-10-05", "那个人的猫叫咪咪")])
    leaked = [h.text for h in await ix.search(uid, "猫叫咪咪", limit=5)]
    check.ok("查不到别人的记忆", all("咪咪" not in text for text in leaked), leaked)

    again = await ix.upsert(uid, "fact", corpus[:1])
    check.ok("重复写同一条不膨胀", again == 0 and ix.count_sync(uid) == 4,
             f"{again}/{ix.count_sync(uid)}")
    check.ok("别人的行不混进自己的计数",
             ix.count_sync("qq_private_other") == 1 and ix.count_sync() == 5, ix.count_sync())
    check.ok("空查询不检索", await ix.search(uid, "") == [], "")
    off = VectorIndex(lock_settings(vector_enabled=False))
    check.ok("关掉开关后既不写也不查",
             (await off.upsert(uid, "fact", corpus)) == 0 and await off.search(uid, "猫") == [], "")
    check.ok("状态里能看到现在用哪条后端", ix.status["backend"] in ("local", "cohere", "openai"),
             ix.status)
    check.ok("状态里也能看到重排是哪条", ix.status["rerank"] in ("off", "siliconflow"), ix.status)
    check.ok("精排有下限，不为显得记得而硬凑不相干的旧事",
             s.rerank_floor > 0 and s.rerank_recall > s.rerank_recall * 0, s.rerank_floor)


def rerank_checks(check: Checker) -> None:
    import core.rerank as R

    s = lock_settings(rerank_provider="off")
    r = R.Reranker(s)
    check.ok("没配精排时 usable=False 且 backend 报 off",
             not r.usable and r.backend == "off", r.backend)
    check.ok("并说清为什么", "RERANK_PROVIDER" in r.degraded_because, r.degraded_because[:50])
    check.ok("不可用时 rerank() 直接返回空而不是抛", r.rerank("问", ["a"]) == [], "")

    cfg = lock_settings(rerank_provider="siliconflow", rerank_base_url="https://api.siliconflow.cn/v1",
                        rerank_api_key="sk-x" * 6, rerank_model="BAAI/bge-reranker-v2-m3")
    rc = R.Reranker(cfg)
    check.ok("配了但没打通过 → 报 off，不冒充在跑真重排",
             rc.usable and rc.configured_backend == "siliconflow" and rc.backend == "off",
             f"{rc.configured_backend}/{rc.backend}")
    check.ok("原因写明还没打通过", "还没成功打通过" in rc.degraded_because, rc.degraded_because[:60])

    # 注入假响应验解析：不依赖网络，也不依赖上游今天活不活着
    captured: dict[str, Any] = {}

    def fake_post(url, payload, key, timeout):
        captured["url"] = url
        captured["payload"] = payload
        return {"results": [{"index": 2, "relevance_score": 0.97},
                            {"index": 0, "relevance_score": 0.02},
                            {"index": 1, "relevance_score": 0.001}]}
    original = R._post
    R._post = fake_post
    try:
        got = rc.rerank("他猫叫什么", ["甲", "乙", "丙"], top_n=2)
        check.ok("按上游给的顺序与分数返回", got == [(2, 0.97), (0, 0.02)], got)
        check.ok("打过一次之后才敢报自己是那条后端", rc.backend == "siliconflow", rc.backend)
        check.ok("URL 拼到 /rerank", captured["url"].endswith("/v1/rerank"), captured["url"])
        check.ok("不重复回传文档正文（省流量）",
                 captured["payload"].get("return_documents") is False, captured["payload"].keys())
        check.ok("top_n 透传", captured["payload"].get("top_n") == 2, captured["payload"])

        # 越界下标必须丢掉：宁可少一条，不能把不相干的排进来
        R._post = lambda *a, **k: {"results": [{"index": 99, "relevance_score": 1.0},
                                               {"index": 0, "relevance_score": 0.5}]}
        rc2 = R.Reranker(cfg)
        check.ok("越界下标被丢掉", rc2.rerank("问", ["甲", "乙"], top_n=2) == [(0, 0.5)],
                 rc2.rerank("问", ["甲", "乙"], top_n=2))
        # 上游结构变了要退回向量序，不能抛
        R._post = lambda *a, **k: {"unexpected": 1}
        rc3 = R.Reranker(cfg)
        check.ok("结构变了退回空列表", rc3.rerank("问", ["甲"]) == [], "")
        check.ok("并记下原因", "results" in rc3.degraded_because, rc3.degraded_because[:60])
        R._post = lambda *a, **k: (_ for _ in ()).throw(R.RerankError("精排端点回 401：Token is invalid."))
        rc4 = R.Reranker(cfg)
        check.ok("上游报错也退回空，不抛给对话", rc4.rerank("问", ["甲"]) == [], "")
        check.ok("上游错误体里的原因被保留（不是光一个 401）",
                 "Token is invalid" in rc4.degraded_because, rc4.degraded_because[:70])
    finally:
        R._post = original

    # 精排不可用时，检索必须退回向量序而不是空手
    ix = VectorIndex(lock_settings(rerank_provider="off"))
    hits = [Hit(text=f"第{i}条", kind="fact", day="2026-10-01", score=0.9 - i * 0.1) for i in range(5)]
    back = ix._rerank("问", hits, 3)
    check.ok("没开精排时按向量原序给满 limit", [h.text for h in back] == ["第0条", "第1条", "第2条"],
             [h.text for h in back])


async def prompt_recall_checks(check: Checker) -> None:
    from core.prompt_builder import PromptBuilder

    s = lock_settings()
    storage_path = s.storage_dir
    shutil.copytree(ROOT / "storage" / "presets", storage_path / "presets", dirs_exist_ok=True) \
        if (ROOT / "storage" / "presets").is_dir() else None
    from core.storage_manager import StorageManager
    st = StorageManager(s)
    uid = "qq_private_recall"
    await st.ensure_user(uid)
    await st.append_facts(uid, ["他最近在准备考研，压力很大", "他养了一只叫豆豆的猫"])
    ix = VectorIndex(s)
    await ix.upsert(uid, "fact", [("2026-10-01", "他最近在准备考研，压力很大"),
                                   ("2026-10-02", "他养了一只叫豆豆的猫")])
    builder = PromptBuilder(s, st)
    builder.bind_vector(ix)
    full, _ = await builder.build_system_prompt(uid, tool_mode="off", user_text="他猫叫什么")
    check.ok("提示词里出现「被这句话勾起来的旧事」", "被这句话勾起来的旧事" in full, "")
    check.ok("并且标明了是按相关度不是按时间", "按相关度，不是按时间" in full, "")
    quick, _ = await builder.build_system_prompt(uid, tool_mode="off", user_text="猫", tier="quick")
    check.ok("快捷档也吃到检索结果", "豆豆" in quick, quick[-260:])
    # 红线优先：关掉记忆层时不许绕道从向量里溜回来
    red = lock_settings(soul_files_only=True)
    rb = PromptBuilder(red, StorageManager(red))
    rb.bind_vector(VectorIndex(red))
    await rb._vector.upsert(uid, "fact", [("2026-10-02", "他养了一只叫豆豆的猫")])
    out, _ = await rb.build_system_prompt(uid, tool_mode="off", user_text="猫")
    check.ok("红线开启时向量也不许把记忆溜回提示词",
             "被这句话勾起来的旧事" not in out and "豆豆" not in out, "")


async def main() -> int:
    check = Checker()
    try:
        lock_block_checks(check)
        lock_safe_checks(check)
        identity_lock_checks(check)
        lock_list_checks(check)
        await lock_wire_checks(check)
        embed_checks(check)
        rerank_checks(check)
        await vector_checks(check)
        await prompt_recall_checks(check)
    finally:
        print(f"\n共 {check.count} 项断言，失败 {len(check.failures)} 项")
        for name in check.failures:
            print(f"  ✗ {name}")
    return 1 if check.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
