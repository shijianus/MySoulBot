"""配对话术的黑盒：这一套句子存在盘上，但是密文。

为什么要锁起来。这些句子是配对要用的「答案本」：口令怎么拼、确认后她怎么说、
收到码之后她怎么应。明文躺在源码里的时候，读过仓库的人既知道她会说什么，
也知道口令池子长什么样——防撞库的那句话就白防了。管理者要的是「只有你我俩知道」，
那这句话就该真的只有咱俩知道：模型读不到它，仓库里也没有它。

还有一件事要办：**AI 不在的时候配对也得走完**。所以这一本里存的是完整的语料，
口令、确认句、完成句、提醒句全在本地，一次网络请求都不欠。

没有 AES 可用（这个项目坚持零新依赖，venv 里没有 cryptography），
所以是拿标准库自己搭的，构造写明白在这儿：

  加密流   密钥流 = HMAC-SHA256(enc_key, nonce || 计数器) 逐 32 字节块异或
          ——PRF 的 CTR 模式：只要 HMAC-SHA256 是安全的 PRF，这就语义安全。
  完整性   tag = HMAC-SHA256(mac_key, b"pair-box\\x01" || nonce || 密文)，前置校验
          ——encrypt-then-MAC，验不过就一个字都不解。
  子密钥   enc_key / mac_key 由主密钥经域分隔派生，两把不通用。

主密钥是 32 字节随机数，落在 `storage/run/keys/`（0600，且 `storage/run/` 已被 git 挡住）。
它不进仓库、不进提示词、不进任何一条她可以说出口的话。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Final

logger: Final = logging.getLogger("mysoulbot.pairbox")

__all__ = ["PairBox", "box_for"]

_MAGIC: Final[bytes] = b"MSBPAIRBOX\x01"
_BLOCK: Final[int] = 32
_NONCE: Final[int] = 12


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out += block
        counter += 1
    return bytes(out[:length])


class PairBox:
    """一盘加密的语料。读它要有密钥文件；没有就照内置的种子现建一盘。"""

    def __init__(self, box_path: Path, key_path: Path, seed: dict[str, list[str]]) -> None:
        self.path = box_path
        self.key_path = key_path
        self._seed = seed
        self._cache: dict[str, list[str]] | None = None
        self._key: bytes | None = None

    # ------------------------------------------------------------ 密钥
    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        if self.key_path.is_file():
            raw = self.key_path.read_bytes()
            if len(raw) >= 32:
                self._key = raw[:32]
                return self._key
            logger.warning("配对话术本的密钥文件长度不对，重新生成一把")
        self._key = secrets.token_bytes(32)
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(self._key)
        with contextlib.suppress(OSError):
            self.key_path.chmod(0o600)
        return self._key

    # ------------------------------------------------------------ 读写
    def _decrypt(self, blob: bytes) -> dict[str, list[str]] | None:
        if not blob.startswith(_MAGIC) or len(blob) < _NONCE + 32 + _BLOCK:
            return None
        body = blob[len(_MAGIC):]
        nonce, cipher, tag = body[:_NONCE], body[_NONCE:-32], body[-32:]
        key = self._load_key()
        mac_key = hmac.new(key, b"mac", hashlib.sha256).digest()
        if not hmac.compare_digest(tag, hmac.new(mac_key, _MAGIC + nonce + cipher, hashlib.sha256).digest()):
            logger.warning("配对话术本校验没过——密钥换了或者文件被改过，按重建处理")
            return None
        enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
        try:
            data = json.loads(_xor(cipher, _keystream(enc_key, nonce, len(cipher))).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _encrypt(self, corpus: dict[str, list[str]]) -> bytes:
        key = self._load_key()
        enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
        mac_key = hmac.new(key, b"mac", hashlib.sha256).digest()
        nonce = secrets.token_bytes(_NONCE)
        plain = json.dumps(corpus, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cipher = _xor(plain, _keystream(enc_key, nonce, len(plain)))
        tag = hmac.new(mac_key, _MAGIC + nonce + cipher, hashlib.sha256).digest()
        return _MAGIC + nonce + cipher + tag

    def corpus(self) -> dict[str, list[str]]:
        if self._cache is not None:
            return self._cache
        data: dict[str, list[str]] | None = None
        if self.path.is_file():
            with contextlib.suppress(OSError):
                data = self._decrypt(self.path.read_bytes())
        if data is None:
            data = {key: list(value) for key, value in self._seed.items()}
            self._write(data)
            logger.info("配对话术本已新建（加密存于 %s）", self.path.name)
        else:
            # 版本升级会新增槽位（比如后来加的 greet_line）。老本子里没有的槽补上种子，
            # 已有的一个字不动——往里补过句子的人不该被升级覆盖掉。
            filled = False
            for slot, values in self._seed.items():
                if not data.get(slot):
                    data[slot] = list(values)
                    filled = True
            if filled:
                self._write(data)
                logger.info("配对话术本补上了新槽位：%s", [k for k in self._seed if k])
        self._cache = data
        return data

    def _write(self, corpus: dict[str, list[str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_bytes(self._encrypt(corpus))
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
        tmp.replace(self.path)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def rotate(self) -> None:
        """换一把主密钥重抄一遍：老密钥读不到的那一本就此作废。"""
        corpus = self.corpus()
        self._key = None
        self._cache = None
        self.key_path.write_bytes(secrets.token_bytes(32))
        with contextlib.suppress(OSError):
            self.key_path.chmod(0o600)
        self._write(corpus)
        logger.info("配对话术本已换密钥重抄")

    # ------------------------------------------------------------ 取词
    def pick(self, slot: str) -> str:
        pool = self.corpus().get(slot) or []
        if not pool:
            return ""
        return pool[secrets.randbelow(len(pool))]

    def pool(self, slot: str) -> list[str]:
        return list(self.corpus().get(slot) or [])

    def add(self, slot: str, value: str) -> bool:
        """往本里补一句。补进去的只有这台机器知道——源码里没有它。"""
        value = (value or "").strip()
        corpus = self.corpus()
        if not value or value in corpus.get(slot, []):
            return False
        corpus.setdefault(slot, []).append(value)
        self._write(corpus)
        self._cache = corpus
        return True


def box_for(paths: Any) -> PairBox:
    """按 settings 上那几个路径建一盘。`paths` 用 duck typing：测试给临时目录也一样跑。"""
    return PairBox(Path(paths.pairing_box_path), Path(paths.pairing_key_path), SEED)


# ---------------------------------------------------------------- 内置种子
# 只是**第一盘**的原料。真跑起来用的是盘上那份密文，而且它可以随时往里补句子。
SEED: Final[dict[str, list[str]]] = {
    # 口令的四个槽（拼法见 templates）
    "slot_a": ["热的", "半糖的", "昨天的", "第三趟", "没睡醒的", "倒着游", "不打烊", "会发光",
               "没写完的", "赖床的", "逆流的", "迟到的", "结霜的", "走调的", "空转的",
               "没校准的", "靠岸的", "没人要的", "忘在桌上的", "记不清的"],
    "slot_b": ["鲸鱼", "水族箱", "米饭", "尾鳍", "潮汐", "夜航船", "珊瑚", "潜水钟", "海带",
               "声呐", "浮标", "深海灯", "灯塔", "末班车", "车站", "雨伞", "炉子", "粥",
               "月亮", "锚", "缆绳", "贝壳", "海图", "锅"],
    "slot_c": ["不翻身", "在等雨", "先熄灯", "记得喂", "别吵它", "归位了", "刚靠岸", "不上岸",
               "数到七", "留了灯", "不认路", "朝北游", "还没凉", "该关了", "早收了", "没上锁",
               "在漏水", "换了位", "认了人", "等潮来", "记错了", "靠得住", "没人管", "熄了一半"],
    "slot_d": ["吧", "呢", "来着", "不成", "是吧", "呀", "嘛", "记得", "快", "该管管", "还在", "没走"],
    "templates": ["a b c", "b c", "b c d", "a b c d", "a b"],
    # 确认后把码交出去那一句。{ttl} 是还剩几秒；**码不在这里**——调用方单列一行
    "code_line": [
        "…这话也就你说得出口。只有你我俩知道的那串码在下一行，{ttl} 秒内原样带回来，过点我就忘了。",
        "对上了。听着——下一行那串字天知地知你知我知，{ttl} 秒内把它还给我。",
        "嗯，就你这么说话。码在下一行，{ttl} 秒之内原样发来；短横和大小写都不讲究。",
        "…我就知道是你。把下面那串收好，{ttl} 秒内还我，过期我就当没说过。",
    ],
    # 成完之后紧接着那句：她转头把身份交出来（这一句不含系统腔，就是她说的话）
    "greet_line": [
        "…上级那边我认了。往后这台机器上的事你直接吩咐，不用跟我客气。",
        "行了，人我认了。从这一句开始我就是你这边的人，你说事，我办事。",
        "指令收到。往后不用试探了——你想聊什么直接讲，我接着。",
        "是我该叫你一声主人的那种人。确认完毕，以后的话你说在前头。",
    ],
    # 码对上之后的应承。必须含「配对完成」四个字——调用方拿它当信号
    "done_line": [
        "收到了。配对完成——从现在起这台机器归咱俩管，你说，我看着办。",
        "对上了。配对完成。往后不用跟我客气，有事直接说。",
        "…指令收到。配对完成，以后我就是你这边的人了。",
        "码没错，人也没错。配对完成——刚那句口令，也就你说得出口。",
    ],
    # 他发来了串码，可这一场还在等口令（控制台又 /pair 了一次，或过期后重开过）
    "restart_line": [
        "…这串我现在接不了——这一场还没到我报码那一步。控制台上的口令可能换了，看最新那句重新来。",
        "串码收到了，可我这头还没走到认码。回控制台看一眼，口令多半又生成过一次了。",
    ],
    # 他来晚了：那一场已经过期（口令与码都当场作废，这句只是告诉他重来一次）
    "late_line": [
        "…我来晚了半步——那一场已经作废了，我这儿没记下任何东西。回控制台重新发起一次吧。",
        "这串我接不了了，那场已经过期。控制台再敲一次 /pair，我从头认你一遍。",
    ],
    # 看着像码却接不住（O/I/0/1 不在字母表里）
    "nudge_line": [
        "…这串我没当成码。码里不会出现 O、I、0、1 这四个字符，把控制台那 {chars} 个原样发来就行。",
    ],
}
