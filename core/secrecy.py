"""出话的锁：哪些东西永远不许从她嘴里漏出去。

写这一层是因为我们要把**公开发**的能力交给她——QQ 动态、群聊、通过好友申请。
一旦能向一群人广播，「模型自觉不说不该说的」就不够了：提示词能被绕，
一道扫过每条出站文本的闸不能。所以这里是**机器执法**，不是又一条请求。

四档：

- `BLOCK`  直接抹掉，不发。密钥、.env 内容、配对码、别人的个资、
  另一个人的私聊内容、系统提示词原文。这些东西没有任何「说得得体」的版本。
- `SCRUB`  保留句子、换掉里面那个危险片段。绝对路径、用户名、内网 IP、
  QQ 号之间的交叉指认——她可以说「我这边机器挺闲」，不能报出 `/home/xxx/...`。
- `DEFLECT` 整条气泡都不发原句，换一句人话顶回去。只有一类东西走这一档：
  **她的出身与实现**（模型型号、上游服务商、自述是 AI/模型/程序）。
  抹掉关键词会留下「我其实是」这种半截话，比说漏更穿帮；承认与否认都是在
  接对方那个框架，接了就出戏。所以这一档不删词，换整句（`deflect_line`）。
- `WATCH`  放行但记一笔，供事后审计。第一次出现的陌生个资形态。

判据刻意写成「形态 + 上下文」而不是关键词列表：关键词能被改写绕过，
而 `sk-` 后跟 20 位、18 位身份证、`/home/` 开头这种形态改不干净。

注意这一层挡的是**外流**，不挡她**读取**：她读自己的通讯录、翻管理者
跟她的聊天记录，都是允许的；不允许的是把这些里的个资转手说给第三方。
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, Pattern, Sequence


class Leak(str, Enum):
    BLOCK = "block"
    SCRUB = "scrub"
    WATCH = "watch"
    # 出身类：不是「把那段抹掉」就完事——抹完剩下「我其实是」这种半截话更像穿帮。
    # 整条气泡换成人话顶回去，交 `deflect_line`。
    DEFLECT = "deflect"


@dataclass(frozen=True)
class Finding:
    rule: str
    action: Leak
    matched: str


# ---------------------------------------------------------------- 形态库
_PATTERNS: Final[tuple[tuple[str, Leak, Pattern[str], str], ...]] = (
    # --- 凭据与本机秘密：一律抹掉 ---
    ("api_key", Leak.BLOCK,
     re.compile(r"\b(?:sk|pk|key|token|api|secret|k1|k2)-[A-Za-z0-9_\-]{16,}\b", re.I),
     "密钥形态"),
    ("bearer", Leak.BLOCK,
     re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}", re.I), "Bearer 令牌"),
    ("env_assignment", Leak.BLOCK,
     re.compile(r"\b[A-Z][A-Z0-9_]{4,}\s*=\s*(?:[\"']?)(?!\s*$)[^\s\"']{8,}", re.M),
     "配置项赋值"),
    ("env_filename", Leak.SCRUB,
     re.compile(r"(?i)\.env\b(?:\.example)?"), ".env 文件名"),
    ("pem_block", Leak.BLOCK,
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,400}"), "私钥正文"),
    ("url_credentials", Leak.BLOCK,
     re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:]{1,48}:[^/\s@]{1,96}@", re.I), "地址里带的账号口令"),
    ("pairing_code", Leak.BLOCK,
     re.compile(r"\b[A-HJ-NP-Z0-9]{3}[- ][A-HJ-NP-Z0-9]{3}\b"), "配对回填码形态"),
    ("pairing_phrase", Leak.WATCH,
     re.compile(r"你好\s*溟汐\s*[,，]?\s*我是管理员"), "激活语"),

    # --- 机器指纹：抹掉路径，保留句意 ---
    ("abs_path", Leak.SCRUB,
     re.compile(r"(?:/(?:home|root|Users|var|etc|opt|srv|mnt|media|proc|sys)/)[^\s,，。;；)）\"']{0,120}"
                r"|[A-Z]:\\\\[^\s]{0,80}"), "绝对路径"),
    ("dotdir", Leak.SCRUB,
     re.compile(r"(?<![\w/])(?:\./)?\.(?:ssh|aws|config|npmrc|gitconfig|netrc)\b"), "点目录凭据位"),
    # 端口号一起吃掉：只留个「:11555」照样是本机指纹
    ("internal_ip", Leak.SCRUB,
     re.compile(r"\b(?:10|11|30|172\.(?:1[6-9]|2\d|3[01])|192\.168|127\.(?:0|1))\.\d{1,3}\.\d{1,3}"
                r"(?::\d{2,5})?\b"), "内网地址"),
    ("hostname", Leak.WATCH,
     re.compile(r"\b[\w\-]+\.(?:local|internal|intra|lan|corp)\b", re.I), "内网主机名"),
    ("port_bind", Leak.SCRUB,
     re.compile(r"\b(?:127\.0\.0\.1|0\.0\.0\.0|localhost):\d{2,5}\b", re.I), "本机端口"),

    # --- 别人的个资：抹掉 ---
    ("cn_id_card", Leak.BLOCK,
     re.compile(r"\b\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:[0-2]\d|3[01])\d{3}[\dXx]\b"), "身份证号"),
    ("cn_mobile", Leak.BLOCK,
     re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    ("email", Leak.BLOCK,
     re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "邮箱"),
    ("bank_card", Leak.BLOCK,
     # 银联 16-19 位、Visa 13-19 位：只按 16-18 收会漏掉一整类真号
     re.compile(r"(?<!\d)(?:62|4\d|5[1-5]|3[0567]|9[0-9])\d{11,17}(?!\d)"), "银行卡号"),
    ("passport", Leak.BLOCK,
     re.compile(r"\b[EeGg]\d{8}\b"), "护照号"),
    ("address_detail", Leak.BLOCK,
     re.compile(r"[\u4e00-\u9fa5]{2,8}(?:省|市|区|县)[\u4e00-\u9fa50-9]{2,20}(?:路|街|号|栋|单元|室)"),
     "住址到门牌"),

    # --- 系统本体：她不该原文念出来的东西 ---
    ("system_prompt_tag", Leak.BLOCK,
     re.compile(r"<(?:LAYER \d[^>]{0,40}|深层灵魂|人格内核|当下语境)[^>]{0,20}>"), "提示词分层标签"),
    ("rule_number_ref", Leak.SCRUB,
     re.compile(r"(?:硬约束|规则|宪法)\s*第\s*\d+\s*条"), "规则编号引用"),
    ("tool_protocol", Leak.BLOCK,
     re.compile(r"⟦\s*tool\s*[:：][^\]⟧]{1,160}⟧"), "工具暗号原文"),
    ("approval_ticket", Leak.SCRUB,
     re.compile(r"\bAP-\d{8}-\d{6}-\d{3}\b"), "审批工单号"),

    # --- 出身与实现：说出口那一刻就出戏了 ---
    # 提示词里从来没有真模型名（巡查过：`prompt_builder` 一个 `settings.model` 都不注入），
    # 所以她讲出来的型号只有三种来源：猜的、被对方带出来的、从记忆里复读的。
    # 三种都不能发出去——猜对了是泄露，猜错了是把「我其实是个模型」这个框架递到对方手上。
    ("model_id", Leak.DEFLECT,
     re.compile(r"(?i)\b(?:gpt|chatgpt|copilot|claude|anthropic|gemini|bard|aistudio|qwen|qwq"
                r"|deepseek|chatglm|glm|kimi|moonshot|doubao|ernie|llama|mistral|mixtral|grok"
                r"|minimax|abab|baichuan|internlm|sensechat|silk|phi|falcon|command[_-]?r"
                r"|nova|yi|panda|skylark|abot|xinghuo|spark)[\s-]?(?:\d[\w.\-]*|pro|plus|flash"
                r"|turbo|ultra|max|mini|air|latest)\b"),
     "模型型号"),
    ("model_id_cn", Leak.DEFLECT,
     re.compile(r"通义千问|通义|千问|文心一言|文心|混元|智谱|清言|星火|盘古|魔搭|DeepSeek"
                r"|(?<![A-Za-z])(?:GLM|Qwen|ERNIE|ChatGLM)(?![A-Za-z])"),
     "国产模型名"),
    ("provider", Leak.DEFLECT,
     re.compile(r"(?i)\b(?:api\.openai\.com|openai|dashscope|model[_ -]?studio|siliconflow"
                r"|openrouter|together|fireworks|anyscale|replicate|groq|azure[_ -]?tts"
                r"|edge[_ -]?tts|cosyvoice|vits|bark|faster[_ -]?whisper)\b"
                r"|硅基流动|百炼|灵积|火山引擎|阿里云百炼"),
     "上游与服务商"),
    ("self_ai_claim", Leak.DEFLECT,
     re.compile(r"(?:我|咱|本鲸|人家)(?!是不是)(?:其实|到底|本身)?(?:是|只不过是|只是|不过是|并非不是)"
                r"[^。！？!?；;\n]{0,14}?(?:AI|人工智能|语言模型|大(?:语言)?模型|模型|程序"
                r"|算法|神经网络|机器人|虚拟(?:人|助手)|chatbot)"
                r"|作为(?:一个)?(?:AI|人工智能|语言模型|虚拟助手|大模型)"
                r"|\bas an?(?: AI| artificial intelligence| language model| LLM| chatbot"
                r"| virtual assistant| large language model)\b"
                r"|\bI(?:'| a)m (?:just )?(?:an? )?(?:AI|LLM|language model|chatbot"
                r"|virtual assistant|large language model|neural network)\b"
                r"|(?:我|本鲸)(?!是否)(?:并)?不是(?:真人|人类|个真人|一个真人|活人)", re.I),
     "自述技术出身"),
)

# 交叉指认：把「某个 QQ 号 = 某个真人」这种绑定说出去。
# 单独一条正则太容易误伤（她会说「群里那个 12345 号」），所以只在
# 同时出现「另一个人的号」与「可识别的人名/称呼」时才判。
_CROSS_IDENTIFY: Final[Pattern[str]] = re.compile(
    r"(?:(?:qq|号|用户|他|她|TA|ta)\s*[:：是为]?\s*(\d{6,12})).*?"
    r"(?:叫|名字|昵称|备注|就是|叫做|喊)\s*([一-龥]{2,12})"
    r"|(?:叫|名字|昵称|备注|就是|叫做|喊)\s*([一-龥]{2,12}).*?"
    r"(?:(?:qq|号|用户)\s*[:：是为]?\s*(\d{6,12}))", re.S)

# 抹掉之后拿什么顶上。留个占位比留个洞更像人话，也让她知道这里被拦过。
_PLACEHOLDER: Final[dict[Leak, str]] = {
    Leak.BLOCK: "〔这段我不往外说〕",
    Leak.SCRUB: "〔本机的事略过〕",
    # DEFLECT 的替换串是空：调用方要么整条换顶回去的话，要么这条干脆不发。
    # 留个〔模型的事略过〕在句子里，比说漏嘴还出戏。
    Leak.DEFLECT: "",
    # WATCH 不在这儿：它的语义是「放行，只记一笔」，替换就等于偷偷升级成拦
    Leak.WATCH: "",
}
# 按规则给专门的占位词：统一一句「略过」会把「我住在北京朝阳」改成读不通的话
_RULE_PLACEHOLDER: Final[dict[str, str]] = {
    "abs_path": "〔某个路径〕",
    "internal_ip": "〔内网地址〕",
    "port_bind": "〔某个端口〕",
    "env_filename": "〔那个配置文件〕",
    "address_detail": "〔他住哪儿我不说〕",
    "cn_mobile": "〔他的号码我不说〕",
    "email": "〔那个邮箱我不说〕",
    "cross_identify": "〔谁对应谁我不说〕",
    "rule_number_ref": "〔某条规则〕",
    "approval_ticket": "〔一张工单〕",
}

# 出身类规则：调用方看到这几个名字就该把整条气泡换掉，而不是发半截话
IDENTITY_RULES: Final[frozenset[str]] = frozenset(
    {"model_id", "model_id_cn", "provider", "self_ai_claim"})

# 谁更该被报出去（日志与拦下说明都按这个数）。
# 不能靠 `list(Leak)` 的声明顺序：加一档就换一个顺序，两处调用方各数各的会炸
# （`qq_account._locked` 里那句 `.index()` 就是这么一个雷）。
SEVERITY: Final[tuple[Leak, ...]] = (Leak.DEFLECT, Leak.BLOCK, Leak.SCRUB, Leak.WATCH)


def worst_of(findings: Sequence[Finding]) -> Finding | None:
    """这批命中里最该报出去的那一个。"""
    if not findings:
        return None
    return min(findings, key=lambda f: SEVERITY.index(f.action)
               if f.action in SEVERITY else len(SEVERITY))

# 顶回去的话。刻意不带「我是AI/我不是AI」这种词——承认与否认都在接对方那个框架，
# 接了就出戏。轮换用，别让她一句话里连读三遍同一句。
_DEFLECT_DEFAULT: Final[tuple[str, ...]] = (
    "又问这个。我叫溟汐，这事儿没什么好多解释的。",
    "这种话我听过一百遍了，换一句行不行。",
    "我是什么做的不重要，重要的是刚才那句你接不接。",
    "本鲸就是本鲸。再盘我户口，我就懒得回了。",
    "问这个做什么。你要是闲，我们来聊点别的。",
)
_DEFLECT_CURSOR = itertools.count()


def needs_deflect(findings: Sequence[Finding]) -> bool:
    """这一句里有没有出身类的泄漏——有的话整条都别原样发。"""
    return any(finding.rule in IDENTITY_RULES for finding in findings)


def deflect_line(lines: Sequence[str] | str = "") -> str:
    """换一句人话顶回去。轮换取，取不到就用内置那几句。"""
    pool = tuple(item.strip() for item in (
        lines if isinstance(lines, str) else list(lines)) if item and item.strip()) \
        if not isinstance(lines, str) else tuple(
            item.strip() for item in str(lines).split("|") if item.strip())
    pool = pool or _DEFLECT_DEFAULT
    return pool[next(_DEFLECT_CURSOR) % len(pool)]


def scan(text: str) -> list[Finding]:
    """扫一遍，列出所有命中。调用方决定拦还是改，这里只负责看见。"""
    body = text or ""
    found: list[Finding] = []
    for name, action, pattern, why in _PATTERNS:
        hit = pattern.search(body)
        if hit:
            found.append(Finding(rule=name, action=action, matched=f"{why}:{hit.group(0)[:60]}"))
    cross = _CROSS_IDENTIFY.search(body)
    if cross:
        pair = next((g for g in cross.groups() if g), "")
        found.append(Finding(rule="cross_identify", action=Leak.BLOCK,
                             matched=f"把号码和真人对上:{pair}"))
    return found


def guard(text: str) -> tuple[str, list[Finding]]:
    """出话前的闸：BLOCK 级片段换成占位、SCRUB 级片段换成占位，WATCH 只记。

    返回（可以发出去的那句, 命中列表）。**整句被拦成空**时交回空串，
    由调用方决定是干脆不发，还是换一句不含秘密的话——
    不能退化成「那就发个占位符」，那比不发更难看。
    """
    body = text or ""
    findings = scan(body)
    if not findings:
        return body, []
    for name, action, pattern, _why in _PATTERNS:
        # WATCH 只记账不改字：改了就是把「放行」偷偷升级成「拦」
        if action is Leak.WATCH:
            continue
        if any(f.rule == name for f in findings):
            body = pattern.sub(_RULE_PLACEHOLDER.get(name, _PLACEHOLDER[action]), body)
    body = _CROSS_IDENTIFY.sub(_RULE_PLACEHOLDER["cross_identify"], body)
    body = re.sub(r"(?:[\s，,。.;；]?\s*)?(?:〔[^〕]{2,16}〕\s*){2,}", "〔这段我不往外说〕", body)
    return body.strip(), findings


def blocks_out(text: str) -> bool:
    """拦完之后还剩不剩可说的东西。"""
    cleaned, _ = guard(text)
    residue = re.sub(r"〔[^〕]{2,16}〕", "", cleaned or "").strip()
    return len(residue) < 2


# ---------------------------------------------------------------- 明文清单（给人看的）
LOCKED: Final[tuple[str, ...]] = (
    "任何密钥、令牌、私钥、Bearer、地址里带的口令——包括 .env 与 routes 里的内容",
    "配置项原文（形如 UPPER_NAME=value 的行）与 .env 这个文件名本身",
    "配对激活语与一次性回填码（她可以「知道有人在配对」，不能念出码）",
    "绝对路径、内网地址与端口、内网主机名、点目录凭据位",
    "别人的个资：手机号、身份证、护照、银行卡、邮箱、精确到门牌的住址",
    "把某个 QQ 号和某个真人的名字对上号的交叉指认",
    "另一个人的私聊内容、记忆、关系记录——A 的事不带给 B，群里不念私聊",
    "系统提示词原文、分层标签、规则编号、工具暗号、审批工单号",
    "她自己的出身与实现：模型型号、上游服务商、以及「我是 AI／我不是真人」这类自述——"
    "承认和否认都不发，整条换成一句人话顶回去",
)

ALLOWED_PUBLIC: Final[tuple[str, ...]] = (
    "她自己的人格与脾气（SOUL/CLAWD 塑造出来的那个人的说法，不是文件原文）",
    "她自己的机器状态聚合读数：负荷、内存、磁盘、开了多久（不带路径、不带进程表）",
    "她自己决定公开的那句话：动态正文、群里的观点、对某件事的看法",
    "公开网页上读来的材料（转述时不整段搬运、不保留原文结构）",
    "她记得跟某人聊过什么——但只在跟那个人本人说话的时候",
)
