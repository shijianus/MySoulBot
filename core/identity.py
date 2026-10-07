"""身份分层与管理者配对。

一个机器人只有一个管理者（owner），其余全是交互者（interactor）。这条线不是装饰：
它决定她能把什么放进提示词、能翻谁的资料夹、能替谁做决定。

四件事是硬的：

1. **配对只能由人在 CLI/管理面板上发起。** 她自己没有路径调到 `start()`，
   也不能续期，更不能从提示词或任何工具输出里读到那个码——
   否则「谁能当主人」就又变成模型说了算。
2. **唯一来源。** 挑战有效期内，报出激活语的来源必须**恰好一个**。多一个就整场作废、
   从头再来：宁可让人多试一次，也不给「谁喊得响谁当主人」留缝。
3. **群聊不配对。** 群里喊这句话的人可以有一百个，而且那头的真人并不知道自己
   被一个模型审核过。配对只认一对一的来源。
4. **码是从「谁来认」算出来的，不是抽出来的。** 一场挑战只有一把主密钥，
   回填码 = `HMAC(主密钥, 挑战 id | 来源 key)`：同一个来源每次算出同一个码，
   不同来源算出不同的码。所以别人即使看见了这条码，从他那个来源也填不进来——
   码本身就是身份的函数。主密钥以 0600 落盘（跨进程要能算），2 分钟过期即删。

分层落到目录上就是两棵树：`storage/data/owner/` 与 `storage/data/users/`。
交互者那条路径上的任何工具、检索、白名单都够不到另一棵——越界与否由路径本身决定，
不靠调用方自觉。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import string
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final

from config import OWNER_USER_ID as _OWNER_ID, Settings
from core.pair_phrase import code_line as _code_line, done_line as _done_line
from core.pair_phrase import nudge_line as _nudge_line
from core.pair_phrase import late_line as _late_line
from core.pair_phrase import restart_line as _restart_line
from core.storage_manager import atomic_write

logger: Final = logging.getLogger("mysoulbot.identity")

__all__ = [
    "Tier", "Identity", "Challenge", "OwnerRecord", "PairingError", "PairingDesk",
    "OWNER_USER_ID", "phrase_matches", "format_code", "extract_code", "derive_code",
    "candidate_key", "consume_pairing",
    "owner_user_ids",
    "normalize_code", "read_owner", "resolve_identity", "unpair",
]

# 引擎侧管理者固定用这个 user_id 落盘：不管他从哪个号来，资料夹只有一个
OWNER_USER_ID: Final[str] = _OWNER_ID
# 激活语**不再是固定的一句**：原来那句写在仓库里，等于把密码本公开了。
# 现在每一场配对现生成一句 ≤10 字的短句，只出现在这一次的控制台输出里。
# 见 core/pair_phrase.py。这里只留一个形状检查用的正则。
# 易混字符一律不进码：OI01 在 QQ 里抄一次错一次
_ALPHABET: Final[str] = "".join(c for c in string.ascii_uppercase + string.digits if c not in "OI01")
# 回填码是**从整句话里挑出来的**，不要求人家把整条消息正好写成码。
# 手机上真实的回法是：复制整条气泡（带「（119 秒内，过期作废）：」）、中文输入法打出
# 全角「－」、末尾顺手一个「。」、来一句「码：6WA-K8A」。原来那条整句形状检查
# 对这些一律不回话——正确的码就这么掉进对话里，她还跟你扯两句，
# 而日志里连一行都没有。挑不出码才是事故，挑得出就走正常比对。
_CODE_CHARS: Final[str] = "A-HJ-NP-Z2-9a-hj-np-z2-9"   # 跟 _ALPHABET 同一个字符集（大小写都收）
_CODE_CACHE: dict[int, re.Pattern[str]] = {}
# 看着像码、却挑不出码（比如把 2 打成了 O，那不在字母表里）：提醒一句，别默默掉进对话。
# 只认「五个起头的连续字母数字」或「一段-一段」这两种形状，免得他报个 QQ 号也被当码催
_CODEISH: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9]{5,8}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[A-Za-z0-9]{2,4}[-\s][A-Za-z0-9]{2,4}(?![A-Za-z0-9])")
_STRIP: Final[re.Pattern[str]] = re.compile(r"[\s\-_·．.,，。!！?？]+")
_CODE_STRIP: Final[re.Pattern[str]] = re.compile(r"[^A-Z0-9]")

# 挑战的三个阶段：等激活语 → 已确认唯一来源（此时才敢把码念出来）→ 终态
_STAGE_OPEN: Final[frozenset[str]] = frozenset({"waiting", "unique"})


class Tier(str, Enum):
    OWNER = "owner"
    INTERACTOR = "interactor"

    @property
    def label(self) -> str:
        return "管理者" if self is Tier.OWNER else "交互者"


class PairingError(RuntimeError):
    """配对没成。消息本身就是一句能直接说给人听的话。"""


def _now() -> float:
    return time.time()


def _stamp(ts: float | None = None) -> str:
    moment = datetime.fromtimestamp(ts if ts is not None else _now(), tz=timezone.utc)
    return moment.astimezone().isoformat(timespec="seconds")


def normalize_phrase(text: str) -> str:
    """中文标点、全角空格、大小写都不该成为「输错」的理由。

    先过一遍 NFKC：手机上打出来的是全角「？」「　」与全角字母，
    控制台上的口令是半角的——这层差别不该让一句正确的口令变成闲聊。
    """
    return _STRIP.sub("", unicodedata.normalize("NFKC", (text or "")).strip().lower())


def phrase_matches(text: str, want: str) -> bool:
    """跟**这一场**的口令比。比的是归一化后的整句相等，不做包含匹配——
    包含匹配会让一句长闲聊顺嘴把口令带进去。"""
    body, target = normalize_phrase(text), normalize_phrase(want)
    return bool(target) and body == target


def normalize_code(raw: str) -> str:
    return _CODE_STRIP.sub("", (raw or "").strip().upper())


def code_pattern(chars: int) -> re.Pattern[str]:
    """挑码的形状跟着 `format_code` 走：前 3 个一组、其余一组，中间至多一个短横或空格。

    写死 3+3 的话，`PAIRING_CODE_CHARS` 一改（比如 5）显示与识别就对不上：
    显示成 ABC-DE，而只认 3+3——正确的码照样会被当成一句闲话。
    """
    cached = _CODE_CACHE.get(chars)
    if cached is None:
        tail = max(1, chars - 3)
        cached = re.compile(
            rf"(?<![A-Za-z0-9])([{_CODE_CHARS}]{{3}})[-\s]?"
            rf"([{_CODE_CHARS}]{{{tail}}})(?![A-Za-z0-9])")
        _CODE_CACHE[chars] = cached
    return cached


def extract_code(raw: str, chars: int = 6) -> str:
    """从一句话里挑出那串回填码；挑不出、或挑出**不一样的两串**，一律给空。

    先过一遍 NFKC：全角「－」「ＷＡ」与全角空格都落回 ASCII，中文输入法那一套
    怪形状不用我这边特判。给空不等于这一句不重要——是让它回到正常对话里去，
    而不是我猜一个当码用。
    """
    body = unicodedata.normalize("NFKC", raw or "")
    found = {normalize_code(f"{a}{b}") for a, b in code_pattern(chars).findall(body)}
    return found.pop() if len(found) == 1 else ""


def format_code(raw: str) -> str:
    """`AS1H7K` → `AS1-H7K`：分组只为好读好抄，比对时一律去掉分隔符。"""
    clean = normalize_code(raw)
    return f"{clean[:3]}-{clean[3:]}" if len(clean) > 3 else clean


def candidate_key(source: str, qq: str = "") -> str:
    """「哪一个来源在认」。空 qq 记成 anon，
    这样「本机命令行上敲的」和「某个 QQ 号发进来的」天然是两个键。"""
    return f"{source}|{str(qq or '').strip() or 'anon'}"


def derive_code(secret: str, *, challenge_id: str, key: str, chars: int = 6) -> str:
    """从「哪一个来源来认」算出这一场给他的码。

    `_ALPHABET` 正好 32 个符号，一个字节的高 5 位刚好均匀落在上面——
    不取模，就没有偏置。同一把主密钥、同一个来源，永远算出同一个码；
    换一个来源就是另一个码，所以码抄不走。
    """
    digest = hmac.new(secret.encode("utf-8"), f"{challenge_id}|{key}".encode("utf-8"),
                      hashlib.sha256).digest()
    return "".join(_ALPHABET[byte >> 3] for byte in digest[:max(1, chars)])


# ---------------------------------------------------------------- 身份判定
@dataclass(frozen=True)
class Identity:
    tier: Tier
    user_id: str
    source: str = ""
    qq: str = ""

    @property
    def is_owner(self) -> bool:
        return self.tier is Tier.OWNER

    @property
    def folder(self) -> str:
        return "owner" if self.is_owner else "users"


def owner_user_ids(settings: Settings) -> set[str]:
    """管理者会以哪些 user_id 出现。

    QQ 那侧进来的话一律被折成 `qq_private_<qq号>`（群聊是 `qq_group_<群号>`），
    所以只认字面量 "owner" 会漏掉他本人——那等于账号级能力在他唯一真正需要它的
    场景里永远不可用。这里把两种形态都算上。
    """
    record = read_owner(settings) if settings.owner_enabled else None
    if record is None:
        return set()
    ids = {record.user_id}
    for uin in (record.qq, settings.owner_qq):
        uin = str(uin or "").strip()
        if uin.isdigit():
            ids.add(f"qq_private_{uin}")
    return ids


def resolve_identity(settings: Settings, user_id: str, *, source: str = "") -> Identity:
    """这个 user_id 是谁。只读绑定记录，绝不采信对话里的自称。

    `owner_enabled=false` 是退回旧行为（只有一棵树、人人平等）给测试和不分层部署用的，
    不是给谁留的后门：关掉之后没有任何路径能凭空拿到管理者目录。
    """
    if settings.owner_enabled:
        record = read_owner(settings)
        if record is not None and user_id in owner_user_ids(settings):
            qq = record.qq or settings.owner_qq
            if user_id.startswith("qq_private_"):
                qq = user_id[len("qq_private_"):]
            return Identity(Tier.OWNER, user_id, source, qq=qq)
    return Identity(Tier.INTERACTOR, user_id, source)


# ---------------------------------------------------------------- 绑定记录
@dataclass
class OwnerRecord:
    user_id: str = OWNER_USER_ID
    qq: str = ""
    source: str = ""
    paired_at: str = ""
    history: list[dict[str, str]] = field(default_factory=list)

    def sources(self) -> list[str]:
        return [str(item.get("source") or "") for item in self.history]

    def binding_key(self) -> str:
        """当初是把管理者认作「哪个来源上的哪个号」——与 `candidate_key` 同一个形状，
        所以「CLI 上绑过的」和「某个 QQ 号来认」是两个键，顶替不了。"""
        return candidate_key(self.source, self.qq)


def read_owner(settings: Settings) -> OwnerRecord | None:
    path = settings.owner_record_path
    if not path.is_file():
        return None
    try:
        raw: dict[str, Any] = json.loads(path.read_text("utf8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("管理者记录读不出来，按未配对处理：%s", path)
        return None
    known = set(OwnerRecord.__dataclass_fields__)
    return OwnerRecord(**{k: v for k, v in raw.items() if k in known})


def write_owner(settings: Settings, record: OwnerRecord) -> None:
    path = settings.owner_record_path
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n")


def unpair(settings: Settings) -> OwnerRecord | None:
    """解绑。这是命令行上的动作——本来就要求机器权限，不是她或任何交互者能触发的。"""
    record = read_owner(settings)
    try:
        settings.owner_record_path.unlink()
    except FileNotFoundError:
        return None
    for stale in settings.pairing_dir.glob("PAIR-*.json"):
        try:
            stale.unlink()
        except OSError:
            pass
    return record


def consume_pairing(desk: "PairingDesk", text: str, *, source: str,
                    qq: str = "", group: bool = False) -> str | None:
    """有挑战挂起时，把口令与回填码从对话里截走。没截走就返回 None。

    两步都走这里：先认这一场的口令（定来源），再认回填码（定身份）。
    只在挑战开着的时候截——口令是现生成的短句，平时她说到相近的词也不会被误截，
    因为比的是**归一化后整句相等**，不是包含。
    群聊来源一律放行给对话：群里说话的人可以有一百个，那不是配对，是热闹。
    """
    if group:
        return None
    challenge = desk.active()
    if challenge is None:
        # 没有开着的挑战，但不该继续装没看见：他可能就是晚了那么几秒
        if desk.late_attempt(text or "", source=source, qq=qq):
            return desk.ceremony.late_line()
        return None
    body = (text or "").strip()
    key = candidate_key(source, qq)

    if phrase_matches(body, challenge.phrase):
        challenge = desk.present(source=source, qq=qq) or challenge
        unique, keys = desk.check_unique(challenge)
        if not unique:
            if keys:
                return (f"✗ 报口令的来源有 {len(keys)} 个（{'、'.join(keys)}），"
                        f"这场作废。重新发起一次。")
            return "…（没认出来路，重来一次）"
        code = desk.plaintext_code(challenge)
        if not code:
            return "…（这一场的码已经不在生效的范围里了，重来一次）"
        # 仪式感在这儿：话是她接下去说的——本子在那盘密文里，AI 全断了也有得说，
        # 而且模型读不到它。最后一行单独是那串码：他要原样带回来的就是这一行，
        # 混在句子里反而容易抄漏半个字（那是上一轮「回正确的码也验证失败」的一半原因）
        return desk.ceremony.code_line(challenge.seconds_left()) + "\n" + format_code(code)

    # 回填码：只在唯一来源已确认之后才认。码是从整句话里挑的（见 extract_code），
    # 所以「码：XXX-YYY。」「整条气泡粘回来」这些真实回法都还能完成配对
    attempt = extract_code(body, desk.code_chars)
    if challenge.stage != "unique" and attempt:
        # 他确实是在回填，只是这一场还在等口令——多半是控制台又 /pair 了一次（口令换了），
        # 或者过期后重开过。这一路最怕沉默：他以为自己发了、她以为没收到
        return desk.ceremony.restart_line()
    if challenge.stage == "unique":
        if attempt:
            try:
                record = desk.submit_code(challenge, attempt, source=source, qq=qq)
            except PairingError as exc:
                return f"✗ {exc}"
            # 招呼语不在这一层生成：安全模块不该依赖大模型调用。
            # 调用方（CLI / 网桥）拿到成功回执后自己现生成一句发出去。
            # 「配对完成」这四个字留在句子里——它是调用方的信号
            return (f"{desk.ceremony.done_line()}\n"
                    f"  （记下了：{record.qq or record.binding_key()} · {record.paired_at}）")
        if key == challenge.source_key and _CODEISH.search(unicodedata.normalize("NFKC", body)):
            # 他大概率是在回填，只是那串里没有我能认的字符（O/I/0/1 不进字母表）。
            # 沉默地把它交给对话，就是他说的「回正确的码也验证失败」
            return desk.ceremony.nudge_line()
    # 只有正在配对那一个号的句子才值得记：别人聊天不该被抄进日志（那是要上锁的东西）
    if challenge is not None and key == challenge.source_key:
        logger.info("配对进行中（%s），这一句没截走：%d 个字", key, len(body))
    return None


# ---------------------------------------------------------------- 挑战
@dataclass
class Challenge:
    """一场配对。口令与主密钥以明文落盘，但文件权限锁到 0600。

    为什么把明文写进文件而不是只留在内存：守护进程和 CLI 是**两个进程**，
    只活在内存里就意味着「CLI 发起、手机上回填」这条路永远走不通——
    而管理者真正会说话的地方就是他的手机。落盘 + 0600 + 2 分钟过期 +
    完成即删，换来的是跨进程可完成，代价是可控的。

    盘上没有现成的码，只有算码的那把主密钥：读文件的人能为任意来源算出码，
    但拿到聊天里那条码的人反过来推不出别的来源的码——抄来的码在别人身上不成立。
    """

    id: str = ""
    phrase: str = ""
    salt: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    channel: str = "cli"
    # 报出激活语的来源：key 归一化过，同一来源重复喊只记一次
    candidates: dict[str, str] = field(default_factory=dict)
    stage: str = "waiting"
    attempts: int = 0
    # 过期之后留一小段「认错码」的宽限：只留 salt，口令与来源照删（见 get()）
    grace_until: float = 0.0

    def alive(self, now: float | None = None) -> bool:
        return self.expires_at > (now if now is not None else _now())

    def seconds_left(self, now: float | None = None) -> int:
        return max(0, int(self.expires_at - (now if now is not None else _now())))

    @property
    def open(self) -> bool:
        return self.stage in _STAGE_OPEN and self.alive()

    @property
    def source_key(self) -> str:
        """唯一来源确认后的那一个；没确认或不止一个就是空。"""
        keys = sorted(self.candidates)
        return keys[0] if self.stage == "unique" and len(keys) == 1 else ""


class _Ceremony:
    """把「怎么说」交给那盘加密的话术本。安全模块只管要句子，不管词从哪儿来。

    为什么绕这一道：这些话是配对的一部分，不该由模型临场发挥（它会说漏、也会被绕），
    也不该明文躺在源码里让谁都能背。本子在盘上是密文，AI 读不到；
    于是**模型全断了，一场配对照样能走完**——口令、交码、应承、提醒，全是本地的。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def code_line(self, ttl: int) -> str:
        return _code_line(self._settings, ttl)

    def done_line(self) -> str:
        return _done_line(self._settings)

    def nudge_line(self) -> str:
        return _nudge_line(self._settings, int(self._settings.pairing_code_chars))

    def restart_line(self) -> str:
        return _restart_line(self._settings)

    def late_line(self) -> str:
        return _late_line(self._settings)


class PairingDesk:
    """配对台账。挑战落盘 `storage/run/pairing/`，一次一张，过期即废。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.ceremony = _Ceremony(settings)

    @property
    def directory(self) -> Path:
        return self._settings.pairing_dir

    @property
    def ttl(self) -> int:
        return int(self._settings.pairing_ttl_seconds)

    @property
    def code_chars(self) -> int:
        return int(self._settings.pairing_code_chars)

    # ------------------------------------------------------------ 发起
    def start(self, *, channel: str = "cli", phrase: str = "") -> Challenge:
        """人在控制台敲下配对命令才走到这里。`phrase` 是这一场现生成的口令。

        开新的之前把盘上**所有**还开着的都作废。只 void `active()` 那一张是不够的：
        剩下一张会按文件名先被撞上，于是新配的口令对着旧算的码干活——
        「一次只有一场」得由发起这一步保证，不能靠运气。
        """
        self.sweep()
        now = _now()
        challenge = Challenge(
            id=f"PAIR-{int(now)}-{secrets.token_hex(2)}",
            phrase=(phrase or "").strip(),
            salt=secrets.token_hex(16),
            created_at=now,
            expires_at=now + self._settings.pairing_ttl_seconds,
            channel=channel,
        )
        self._save(challenge)
        logger.info("配对挑战已发起（%s），%d 秒内有效", challenge.id, self._settings.pairing_ttl_seconds)
        return challenge

    def plaintext_code(self, challenge: Challenge) -> str:
        """把码念给**这一个来源**。唯一来源没确认之前一个字都不给看；
        确认之后给的也是按那一个来源算出来的码——换个人来认，就不是这串。"""
        if challenge.stage != "unique" or not challenge.alive():
            return ""
        key = challenge.source_key
        if not key:
            return ""
        return derive_code(challenge.salt, challenge_id=challenge.id, key=key,
                           chars=self._settings.pairing_code_chars)

    # ------------------------------------------------------------ 读写
    def _path(self, challenge_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9\-]", "", challenge_id)
        return self.directory / f"{safe}.json"

    def _save(self, challenge: Challenge) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(challenge.id)
        atomic_write(path, json.dumps(asdict(challenge), ensure_ascii=False, indent=2) + "\n")
        # 里面躺着这一场的口令与算码的主密钥：只许本人读写。
        # 原子写是先建临时文件再 rename，所以权限要在 rename 之后落到正式路径上。
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    def _load(self, challenge_id: str) -> Challenge | None:
        path = self._path(challenge_id)
        if not path.is_file():
            return None
        try:
            raw: dict[str, Any] = json.loads(path.read_text("utf8"))
        except (json.JSONDecodeError, OSError):
            return None
        known = set(Challenge.__dataclass_fields__)
        return Challenge(**{k: v for k, v in raw.items() if k in known})

    def get(self, challenge_id: str) -> Challenge | None:
        challenge = self._load(challenge_id)
        if challenge is None:
            return None
        # `open` 里已经带了 alive()，所以「过期但仍算活跃」这个判断永远不成立，
        # 过期的文件于是从来没被清过。这里比的是阶段：过期即删——
        # 口令是明文写的，过期了还留在盘上，只是多一个可撞的东西
        if not challenge.alive() and challenge.stage in _STAGE_OPEN:
            challenge.stage = "expired"
            self._expire(challenge)
            return challenge
        if challenge.stage == "expired" and time.time() > challenge.grace_until:
            # 宽限也过了：口令、salt、来源一个都不该留在盘上
            with contextlib.suppress(OSError):
                self._path(challenge_id).unlink()
            return None
        return challenge

    def _expire(self, challenge: Challenge) -> None:
        """过期不是消失：口令当场抹掉，salt 留 {ttl} 秒用来认一句「你来晚了」。

        为什么要这一段：手机上打完那句口令再抄一串码，120 秒很容易踩线过去。
        踩线之后他发的码就没人接了——她还跟他聊两句，那就是第二轮报障的现场。
        """
        grace = self._settings.pairing_grace_seconds
        challenge.phrase = ""      # 口令当场作废：宽限期只用来认「你来晚了」，不再留着可猜的东西
        challenge.grace_until = time.time() + grace if grace > 0 else 0.0
        if challenge.grace_until > 0:
            self._save(challenge)
        else:
            with contextlib.suppress(OSError):
                self._path(challenge.id).unlink()

    def late_attempt(self, text: str, *, source: str, qq: str = "") -> str:
        """没有开着的挑战时，看这一句是不是**上一场来晚了**的码。是就返回那一场的 id。"""
        chars = self.code_chars
        attempt = extract_code(text or "", chars)
        if not attempt:
            return ""
        key = candidate_key(source, qq)
        now = time.time()
        if not self.directory.is_dir():
            return ""
        for path in sorted(self.directory.glob("PAIR-*.json"), reverse=True):
            challenge = self._load(path.stem)
            if challenge is None:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if challenge.stage != "expired" or now > challenge.grace_until:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if key not in challenge.candidates:
                continue
            expected = derive_code(challenge.salt, challenge_id=challenge.id, key=key,
                                   chars=chars)
            if hmac.compare_digest(expected, attempt):
                return challenge.id
        return ""

    def active(self) -> Challenge | None:
        """当前这一场。取**最新**的那张，其余还开着的一律作废。

        按文件名升序取第一张是出过事故的：一张没人回填的旧挑战能活 120 秒，
        足够把刚发起的那一场 shadow 掉——人对着一句已经不作数的口令白喊。
        """
        if not self.directory.is_dir():
            return None
        found: Challenge | None = None
        for path in sorted(self.directory.glob("PAIR-*.json"), reverse=True):
            challenge = self.get(path.stem)
            if challenge is None or not challenge.open:
                continue
            if found is None:
                found = challenge
            else:
                self.void(challenge.id)
        return found

    def sweep(self) -> int:
        """清场：作废所有还开着的挑战（过期的由 `get()` 顺手抹掉）。返回清了几张。"""
        if not self.directory.is_dir():
            return 0
        cleared = 0
        for path in sorted(self.directory.glob("PAIR-*.json")):
            challenge = self.get(path.stem)
            if challenge is not None and challenge.open:
                self.void(challenge.id)
                cleared += 1
        return cleared

    def void(self, challenge_id: str) -> Challenge | None:
        challenge = self._load(challenge_id)
        if challenge is None:
            return None
        challenge.stage = "void"
        path = self._path(challenge_id)
        # 作废就整张删掉：留一个已经废了的口令在盘上，只是多一个可撞的东西
        with contextlib.suppress(OSError):
            path.unlink()
        return challenge

    # ------------------------------------------------------------ 第一步：报激活语
    def present(self, *, source: str, qq: str = "", group: bool = False) -> Challenge | None:
        """有人说了那句激活语，记下来路。群聊来源一律不记。"""
        challenge = self.active()
        if challenge is None or group:
            return challenge
        challenge.candidates.setdefault(candidate_key(source, qq), _stamp())
        self._save(challenge)
        return challenge

    def check_unique(self, challenge: Challenge) -> tuple[bool, list[str]]:
        """唯一来源判定。多于一个就整场作废。"""
        keys = sorted(challenge.candidates)
        if len(keys) != 1:
            if keys:
                self.void(challenge.id)
            return False, keys
        if challenge.stage == "waiting":
            challenge.stage = "unique"
            self._save(challenge)
        return True, keys

    # ------------------------------------------------------------ 第二步：回填码
    def submit_code(self, challenge: Challenge, code: str, *, source: str,
                    qq: str = "") -> OwnerRecord:
        """码对了才升级成管理者。答错即作废，必须从头再来。"""
        if challenge.stage not in ("waiting", "unique"):
            raise PairingError("这场配对已经结束了，重新发起一次")
        if not challenge.alive():
            raise PairingError("配对已经过期了，重新发起一次")

        challenge = self.get(challenge.id) or challenge
        unique, keys = self.check_unique(challenge)
        if not unique:
            raise PairingError(
                f"报激活语的来源有 {len(keys)} 个，这场作废了"
                if keys else "还没人报激活语——控制台上的那句口令，原样发进来")

        key = keys[0]
        # 比的是**整把键**，不是只比通道名：同是 qq_private，换一个号就不是同一个人。
        # 原来只比 `source` 前缀，等于「A 号报了口令，B 号抄到码就能认领」
        if key != candidate_key(source, qq):
            self.void(challenge.id)
            raise PairingError(
                f"这一场的口令是 {key} 报的，码得发回那儿去才算你——"
                f"你现在从 {candidate_key(source, qq)} 发来，对不上。"
                f"想在这头绑，就回控制台重新 /pair，口令也在这头说")

        attempt = normalize_code(code)
        challenge.attempts += 1
        # 码是**按 key 算出来的**：只有当初报激活语的那一个来源算得出他这一份。
        # 别人抄到这条码，从他自己的来源回填也对不上
        expected = derive_code(challenge.salt, challenge_id=challenge.id, key=key,
                               chars=self._settings.pairing_code_chars)
        right = bool(attempt) and hmac.compare_digest(expected, attempt)
        if not right:
            self.void(challenge.id)
            raise PairingError("码不对，这场作废了——从头再来")

        record = read_owner(self._settings) or OwnerRecord()
        qq = key.partition("|")[2]
        qq = "" if qq == "anon" else qq
        # 「一个机器人只有一个管理者」要比来源，不能只比 QQ 号：
        # 先在本机命令行上绑过（qq 为空）再拿某个 QQ 号来，是同一场顶替——
        # 只查 `record.qq` 的话那条判断永远不成立，等于没闸。
        claimed = key
        if record.paired_at and record.binding_key() != claimed:
            raise PairingError(f"已经绑过管理者（{record.binding_key()}）。一个机器人只有一个，"
                               "要换先在命令行上解绑")
        record.qq = qq or record.qq
        record.source = source
        record.user_id = OWNER_USER_ID
        record.paired_at = _stamp()
        record.history.append({"at": record.paired_at, "source": source, "qq": record.qq})
        write_owner(self._settings, record)
        challenge.stage = "verified"
        with contextlib.suppress(OSError):
            self._path(challenge.id).unlink()   # 用完即删，不在盘上留凭据
        logger.info("管理者配对成功：%s（来源 %s）", record.qq or "本机", source)
        return record
