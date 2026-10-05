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
4. **码只存哈希。** 明文码留在内存里、只活这一次挑战；落盘的是 HMAC。
   2 分钟自动过期，答错即作废，没有半成品状态可续。

分层落到目录上就是两棵树：`storage/data/owner/` 与 `storage/data/users/`。
交互者那条路径上的任何工具、检索、白名单都够不到另一棵——越界与否由路径本身决定，
不靠调用方自觉。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import string
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final

from config import OWNER_USER_ID as _OWNER_ID, Settings
from core.storage_manager import atomic_write

logger: Final = logging.getLogger("mysoulbot.identity")

__all__ = [
    "Tier", "Identity", "Challenge", "OwnerRecord", "PairingError", "PairingDesk",
    "OWNER_USER_ID", "ACTIVATION_PHRASE", "phrase_matches", "format_code", "consume_pairing",
    "owner_user_ids",
    "normalize_code", "read_owner", "resolve_identity", "unpair",
]

# 引擎侧管理者固定用这个 user_id 落盘：不管他从哪个号来，资料夹只有一个
OWNER_USER_ID: Final[str] = _OWNER_ID
ACTIVATION_PHRASE: Final[str] = "你好溟汐，我是管理员"
# 易混字符一律不进码：OI01 在 QQ 里抄一次错一次
_ALPHABET: Final[str] = "".join(c for c in string.ascii_uppercase + string.digits if c not in "OI01")
_STRIP: Final[re.Pattern[str]] = re.compile(r"[\s\-_·．.,，。!！?？]+")
_CODE_STRIP: Final[re.Pattern[str]] = re.compile(r"[^A-Z0-9]")
# 回填码的形状：字母数字，中间最多一个短横/空格。不卡形状的话，
# 她随口回的一句「A1 一下」就会被当成码去试
_CODE_LIKE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9]{2,4}[- ]?[A-Za-z0-9]{2,4}$")

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
    """中文标点、全角空格、大小写都不该成为「输错」的理由。"""
    return _STRIP.sub("", (text or "").strip().lower())


def phrase_matches(text: str) -> bool:
    return normalize_phrase(text) == normalize_phrase(ACTIVATION_PHRASE)


def normalize_code(raw: str) -> str:
    return _CODE_STRIP.sub("", (raw or "").strip().upper())


def format_code(raw: str) -> str:
    """`AS1H7K` → `AS1-H7K`：分组只为好读好抄，比对时一律去掉分隔符。"""
    clean = normalize_code(raw)
    return f"{clean[:3]}-{clean[3:]}" if len(clean) > 3 else clean


def _hash_code(code: str, salt: str) -> str:
    return hmac.new(salt.encode("utf-8"), code.encode("utf-8"), hashlib.sha256).hexdigest()


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
        """当初是把管理者认作「哪个来源上的哪个号」。空 qq 记成 anon，
        这样「本机命令行绑的」和「某个 QQ 号来认」天然是两个不同的键。"""
        return f"{self.source}|{self.qq or 'anon'}"


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
    """有挑战挂起时，把激活语与回填码从对话里截走。没截走就返回 None。

    两步都走这里：先认激活语（定来源），再认回填码（定身份）。
    只在挑战开着的时候截——否则她连「你好溟汐，我是管理员」这句玩笑都不能说。
    群聊来源一律放行给对话：群里喊这句话的人可以有一百个，那不是配对，是热闹。
    """
    if group:
        return None
    challenge = desk.active()
    if challenge is None:
        return None
    body = (text or "").strip()

    if phrase_matches(body):
        challenge = desk.present(source=source, qq=qq) or challenge
        unique, keys = desk.check_unique(challenge)
        if not unique:
            if keys:
                return (f"✗ 报激活语的来源有 {len(keys)} 个（{'、'.join(keys)}），"
                        f"这场作废。重新发起一次。")
            return "…（没认出来路，重来一次）"
        code = desk.plaintext_code(challenge)
        return (f"✓ 唯一来源确认：{keys[0]}\n"
                f"  把下面这串码原样回到这里（{desk.ttl} 秒内，过期作废）："
                f"{format_code(code) if code else '（码已不在内存里，重发一次）'}\n"
                f"  短横可有可无，大小写不限。")

    # 回填码：只在唯一来源已确认之后才认，且形状必须像码——
    # 不然她随便回一句「ABC-123」也可能把谁的话误当成码
    if challenge.stage == "unique" and _CODE_LIKE.fullmatch(body):
        try:
            record = desk.submit_code(challenge, body, source=source)
        except PairingError as exc:
            return f"✗ {exc}"
        return f"✓ 配对完成。管理者：{record.qq or record.binding_key()}（{record.paired_at}）"
    return None


# ---------------------------------------------------------------- 挑战
@dataclass
class Challenge:
    id: str = ""
    phrase: str = ACTIVATION_PHRASE
    code_hash: str = ""
    salt: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    channel: str = "cli"
    # 报出激活语的来源：key 归一化过，同一来源重复喊只记一次
    candidates: dict[str, str] = field(default_factory=dict)
    stage: str = "waiting"
    attempts: int = 0

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


class PairingDesk:
    """配对台账。挑战落盘 `storage/run/pairing/`，一次一张，过期即废。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # 明文码只活在内存里这一次：进程重启就得重新发起，不给它留盘
        self._codes: dict[str, str] = {}

    @property
    def directory(self) -> Path:
        return self._settings.pairing_dir

    @property
    def ttl(self) -> int:
        return int(self._settings.pairing_ttl_seconds)

    # ------------------------------------------------------------ 发起
    def start(self, *, channel: str = "cli") -> Challenge:
        """人在控制台敲下配对命令才走到这里。"""
        current = self.active()
        if current is not None:
            self.void(current.id)
        code = "".join(secrets.choice(_ALPHABET) for _ in range(self._settings.pairing_code_chars))
        salt = secrets.token_hex(16)
        now = _now()
        challenge = Challenge(
            id=f"PAIR-{int(now)}-{secrets.token_hex(2)}",
            code_hash=_hash_code(code, salt),
            salt=salt,
            created_at=now,
            expires_at=now + self._settings.pairing_ttl_seconds,
            channel=channel,
        )
        self._save(challenge)
        self._codes[code] = challenge.id
        logger.info("配对挑战已发起（%s），%d 秒内有效", challenge.id, self._settings.pairing_ttl_seconds)
        return challenge

    def plaintext_code(self, challenge: Challenge) -> str:
        """把码念给控制台。唯一来源没确认之前，一个字都不给看。"""
        if challenge.stage != "unique" or not challenge.alive():
            return ""
        for code, cid in self._codes.items():
            if cid == challenge.id:
                return code
        return ""

    # ------------------------------------------------------------ 读写
    def _path(self, challenge_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9\-]", "", challenge_id)
        return self.directory / f"{safe}.json"

    def _save(self, challenge: Challenge) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path(challenge.id),
                     json.dumps(asdict(challenge), ensure_ascii=False, indent=2) + "\n")

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
        if not challenge.alive() and challenge.open:
            challenge.stage = "expired"
            self._save(challenge)
        return challenge

    def active(self) -> Challenge | None:
        if not self.directory.is_dir():
            return None
        for path in sorted(self.directory.glob("PAIR-*.json")):
            challenge = self.get(path.stem)
            if challenge is not None and challenge.open:
                return challenge
        return None

    def void(self, challenge_id: str) -> Challenge | None:
        challenge = self._load(challenge_id)
        if challenge is None:
            return None
        challenge.stage = "void"
        self._codes = {k: v for k, v in self._codes.items() if v != challenge_id}
        self._save(challenge)
        return challenge

    # ------------------------------------------------------------ 第一步：报激活语
    def present(self, *, source: str, qq: str = "", group: bool = False) -> Challenge | None:
        """有人说了那句激活语，记下来路。群聊来源一律不记。"""
        challenge = self.active()
        if challenge is None or group:
            return challenge
        key = f"{source}|{qq or 'anon'}"
        challenge.candidates.setdefault(key, _stamp())
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
    def submit_code(self, challenge: Challenge, code: str, *, source: str) -> OwnerRecord:
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
                if keys else "还没人报激活语，先说那句「你好溟汐，我是管理员」")

        expected_source = keys[0].split("|", 1)[0]
        if expected_source != source:
            self.void(challenge.id)
            raise PairingError("回填码的来源和报激活语的不是同一个，作废重发")

        attempt = normalize_code(code)
        challenge.attempts += 1
        right = bool(attempt) and hmac.compare_digest(challenge.code_hash, _hash_code(attempt, challenge.salt))
        if not right:
            self.void(challenge.id)
            raise PairingError("码不对，这场作废了——从头再来")

        record = read_owner(self._settings) or OwnerRecord()
        qq = keys[0].partition("|")[2]
        qq = "" if qq == "anon" else qq
        # 「一个机器人只有一个管理者」要比来源，不能只比 QQ 号：
        # 先在本机命令行上绑过（qq 为空）再拿某个 QQ 号来，是同一场顶替——
        # 只查 `record.qq` 的话那条判断永远不成立，等于没闸。
        claimed = keys[0]
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
        self._save(challenge)
        self._codes = {k: v for k, v in self._codes.items() if v != challenge.id}
        logger.info("管理者配对成功：%s（来源 %s）", record.qq or "本机", source)
        return record
