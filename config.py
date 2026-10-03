"""MySoulBot 运行时配置。

从 `.env` 或环境变量加载所有可调参数，供 core 层注入使用。
配置项按「模型接入 / 记忆抽取 / Prompt 预算 / 存储」四组划分。
"""

from __future__ import annotations

import contextlib
import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_LOG: str = "runtime.log"
RUNTIME_LOG_MAX_BYTES: int = 2_000_000


class Settings(BaseSettings):
    """全局配置。字段名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------- 主对话模型（任意 OpenAI 兼容接口） ----------------
    api_key: str = Field(default="EMPTY", description="鉴权 Key，本地服务通常可填 EMPTY")
    base_url: str = Field(
        default="https://api.openai.com/v1",
        description="OpenAI 兼容接口的 Base URL，必须以 /v1 之类路径结尾",
    )
    model: str = Field(default="gpt-4o-mini", description="对话模型名")
    temperature: float = Field(default=0.85, ge=0.0, le=2.0)
    max_tokens: int = Field(default=800, ge=1)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    request_timeout: float = Field(default=120.0, gt=0, description="单次请求超时（秒）")
    max_retries: int = Field(default=2, ge=0, le=10, description="SDK 内建重试次数")
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)

    # ---------------- 记忆抽取（轻量模型 + 非阻塞后台任务） ----------------
    extractor_enabled: bool = True
    extractor_model: str = Field(default="", description="留空则复用 MODEL")
    extractor_base_url: str = Field(default="", description="留空则复用 BASE_URL")
    extractor_api_key: str = Field(default="", description="留空则复用 API_KEY")
    extractor_timeout: float = Field(default=60.0, gt=0)
    extractor_max_tokens: int = Field(default=400, ge=16)
    extractor_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    extractor_lookback_turns: int = Field(default=6, ge=2, le=40, description="每次分析最近多少条消息")
    extractor_max_facts: int = Field(default=4, ge=1, le=20, description="单轮最多落盘的事实条数")

    # ---------------- Prompt 编排预算（字符数，近似 token 的 1/2） ----------------
    context_max_turns: int = Field(default=12, ge=2, le=100, description="注入近期上下文的条数")
    soul_max_chars: int = Field(default=6000, ge=200)
    user_max_chars: int = Field(default=2500, ge=200)
    memory_max_entries: int = Field(default=60, ge=1, description="注入上下文的记忆条数上限")

    # ---------------- 双层灵魂：ClawdSoul（深层内核）+ Persona（外在人格） ----------------
    clawd_enabled: bool = Field(default=True, description="注入 LAYER 0 深层灵魂")
    clawd_max_chars: int = Field(default=4500, ge=200)
    relations_max_entries: int = Field(default=40, ge=1, description="注入的关系动态条数上限")
    reflection_enabled: bool = Field(default=True, description="后台抽取关系动态与态度演变")

    # ---------------- 客户端表现（沉浸化） ----------------
    immersive: bool = Field(default=True, description="主气泡只输出角色表达，不挂调试前缀")
    chat_mode: str = Field(default="solo", description="solo=1V1；group=群聊（锁死一切参数与状态指令）")
    diagnostics: bool = Field(default=False, description="显示错误细节与运维提示（由 /panel debug 开关）")
    trim_stock_closers: bool = Field(
        default=True, description="切掉角色在句尾挂的套话反问（「你想聊什么」「还有什么我能帮」）"
    )

    # ---------------- 真人体感：熟络度 / 节律 / 情绪惰性 ----------------
    rapport_enabled: bool = Field(default=True, description="熟络度演进引擎（引擎独占写入，不接受指令调温）")
    rapport_floor_ratio: float = Field(default=0.4, ge=0.0, le=1.0, description="久不联系的降温下限（相对峰值）")
    rhythm_enabled: bool = Field(default=True, description="按当地时区注入生理与时间感")
    user_timezone: str = Field(default="", description="IANA 时区名，留空用系统时区；例 Asia/Shanghai")
    night_start: int = Field(default=1, ge=0, le=23, description="深夜时段起点（整点，可跨零点）")
    night_end: int = Field(default=5, ge=0, le=23, description="深夜时段终点（不含）")
    mood_half_life_minutes: int = Field(default=120, ge=5, le=1440, description="情绪余温的半衰期")
    patience_turn_limit: int = Field(default=14, ge=3, description="一天内聊到这个数就该懒得说")
    patience_refill_per_hour: float = Field(default=0.06, ge=0.0, le=1.0)

    # ---------------- 本地酒馆兼容服务 ----------------
    server_host: str = Field(default="127.0.0.1", description="只绑回环：这套灵魂与记忆不该裸露在局域网里")
    server_port: int = Field(default=11555, ge=1, le=65535)

    # ---------------- OneBot 接入（QQ 等聊天协议，裸机部署；本阶段已激活）----------------
    onebot_enabled: bool = Field(
        default=False, description="总开关：false 时不额外监听任何端口，网桥一行代码都不会跑"
    )
    onebot_host: str = Field(
        default="127.0.0.1", description="只绑回环：QQ 那边的事不该被整个网段读到（与 SERVER_HOST 同一条规矩）"
    )
    onebot_port: int = Field(
        default=11556, ge=1, le=65535, description="与酒馆端点 11555 错开：一个给自己人用，一个给协议端进来"
    )
    onebot_access_token: str = Field(
        default="",
        description="协议端与本服务之间的鉴权串；留空时不许绑非回环地址（网桥启动就拒绝）",
    )
    onebot_bot_name: str = Field(
        default="",
        description="群聊里喊她接话的名字，逗号分隔可写好几个；留空则用协议端报回的昵称",
    )
    onebot_auto_record: bool = Field(
        default=False,
        description="私聊说完话是否顺手念出声（回一条 QQ 语音）；群聊永不出声，那是刷屏",
    )

    # ---------------- 工具层 ----------------
    tools_enabled: bool = True
    tool_native_calling: bool = Field(default=True, description="优先用接口的 function calling")
    tool_max_rounds: int = Field(default=3, ge=1, le=8, description="单轮对话内最多几趟工具往返")
    tool_timeout: float = Field(default=45.0, gt=0)
    tool_audit: bool = Field(default=True, description="把工具调用记进 storage/logs/tools.jsonl")
    web_enabled: bool = True
    web_max_bytes: int = Field(default=2_000_000, ge=1024, description="单次抓取的上限字节")
    web_timeout: float = Field(default=20.0, gt=0)
    web_max_chars: int = Field(default=6000, description="交给模型的正文上限")
    web_allow_private: bool = Field(
        default=False, description="允许抓内网/回环/元数据地址（只在本地开发与测试时打开）"
    )
    image_provider: str = Field(default="stub", description="stub | openai | none")
    image_model: str = Field(default="", description="绘图模型名，留空回落 MODEL")
    image_size: str = Field(default="1024x1024")
    snapshot_provider: str = Field(
        default="auto", description="auto | playwright | binary | text | none"
    )

    # ---------------- 视觉与多模态 ----------------
    vision_enabled: bool = Field(
        default=True, description="把图片本体送进模型：它看见什么就按什么说话"
    )
    vision_model: str = Field(default="", description="能看图的多模态模型名，留空复用 MODEL")
    vision_max_images: int = Field(default=2, ge=1, le=8, description="一轮最多看几张")
    vision_max_bytes: int = Field(default=4_000_000, ge=1024, description="单张图片的字节上限")

    # ---------------- 拟人语音 ----------------
    tts_enabled: bool = Field(
        default=True, description="总开关：false 时整条语音链静默，界面一根播放条都不挂"
    )
    tts_provider: str = Field(
        default="auto", description="auto=装了 edge-tts 就用真人声，否则标准库合成 | edge | stub | none"
    )
    tts_voice_day: str = Field(default="zh-CN-XiaoxiaoNeural", description="常态音色名（edge 用）")
    tts_voice_night: str = Field(default="zh-CN-XiaoxiaoNeural", description="深夜音色名（edge 用）")
    tts_max_chars: int = Field(default=600, ge=20, description="一次最多念这么多字，超了就截")
    tts_timeout: float = Field(default=20.0, gt=0, description="外部 TTS 的天花板秒数")

    # ---------------- 沉浸面板与守护 ----------------
    panel_enabled: bool = Field(default=True, description="在服务上挂只读 Web 面板")
    drain_timeout_seconds: float = Field(
        default=30.0, gt=0, description="退出前等在途记忆落盘的天花板秒数"
    )

    # ---------------- 存储体积与远端同步（GitHub 单文件 100MB 硬线） ----------------
    log_keep_days: int = Field(default=7, ge=1, description="明文日志保留天数，更早的 gzip 归档")
    log_max_file_bytes: int = Field(default=4_194_304, ge=65536, description="单个日志文件上限（4MB）")
    log_max_total_bytes: int = Field(default=33_554_432, ge=1_048_576, description="明文日志总量上限")
    archive_keep_days: int = Field(default=120, ge=1, description="归档保留天数，超期删除")
    doc_max_bytes: int = Field(default=8_388_608, ge=65536, description="单份 md 文档上限")
    memory_compact_threshold: int = Field(
        default=800, ge=20, description="事实条数超过这个值就归档最老的一段"
    )
    git_safe_file_bytes: int = Field(
        default=20_971_520, ge=1_048_576, description="同步闸门：超过它就拒绝入库"
    )
    sync_remote_url: str = Field(default="git@github.com:shijianus/MySoulBot.git")
    sync_remote_branch: str = Field(default="main", min_length=1)

    # ---------------- 存储与运行时 ----------------
    storage_dir: Path = Field(default=Path("storage"), description="相对路径按项目根解析")
    default_user_id: str = Field(default="guest", min_length=1, max_length=64)
    log_level: str = Field(default="WARNING", description="记录到 storage/logs/runtime.log 的级别；终端只留告警")
    persist_transcript: bool = Field(default=True, description="是否把逐轮对话写入 logs/")

    @field_validator("api_key", "base_url", "model", "extractor_model", "storage_dir", mode="before")
    @classmethod
    def _strip_whitespace(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("base_url", "model")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("不能为空，请在 .env 中设置 BASE_URL / MODEL")
        return value

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError(f"未知 log_level: {value}")
        return level

    @field_validator("chat_mode")
    @classmethod
    def _normalize_chat_mode(cls, value: str) -> str:
        mode = value.strip().lower()
        if mode not in {"solo", "group"}:
            raise ValueError(f"未知 CHAT_MODE: {value}（只能是 solo 或 group）")
        return mode

    @field_validator("user_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        if not value:
            return value
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(value)
        except Exception as exc:  # noqa: BLE001 - 时区名写错就明确报错，不要静默按系统时区跑
            raise ValueError(f"未知 USER_TIMEZONE: {value}（要 IANA 名，如 Asia/Shanghai）") from exc
        return value

    @field_validator("image_provider", "snapshot_provider", "tts_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        if not self.storage_dir.is_absolute():
            self.storage_dir = (PROJECT_ROOT / self.storage_dir).resolve()
        return self

    # ---------------- 派生路径 ----------------
    @property
    def template_dir(self) -> Path:
        return self.storage_dir / "templates"

    @property
    def presets_dir(self) -> Path:
        return self.storage_dir / "presets"

    @property
    def users_dir(self) -> Path:
        return self.storage_dir / "data" / "users"

    @property
    def soul_dir(self) -> Path:
        """深层灵魂（ClawdSoul）目录——全局唯一，跨用户、跨人格。"""
        return self.storage_dir / "soul"

    @property
    def clawd_path(self) -> Path:
        return self.soul_dir / "CLAWD.md"

    @property
    def audit_dir(self) -> Path:
        return self.storage_dir / "logs"

    @property
    def group_mode(self) -> bool:
        return self.chat_mode == "group"

    @property
    def night_hours(self) -> tuple[int, int]:
        """深夜区间 (start, end)，支持跨零点；写成两个标量字段是为了让 .env 能用 23,5 这种直觉写法。"""
        return (self.night_start, self.night_end)

    @property
    def effective_extractor_model(self) -> str:
        return self.extractor_model or self.model

    @property
    def effective_vision_model(self) -> str:
        """带图的那一趟实际用的模型名：没单独配就跟主模型走。"""
        return self.vision_model or self.model

    def extractor_credentials(self) -> tuple[str, str]:
        """返回 (api_key, base_url)，未单独配置时回落到主模型。"""
        return (
            self.extractor_api_key or self.api_key,
            self.extractor_base_url or self.base_url,
        )

    def ensure_directories(self) -> None:
        for path in (
            self.storage_dir,
            self.template_dir,
            self.presets_dir,
            self.users_dir,
            self.soul_dir,
            self.audit_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def apply_logging(self, *, terminal_info: bool = False) -> None:
        """引擎 chatter 进文件，终端只留角色表达与非同小可的告警。

        这是「去调试噪点」的根子：LOG_LEVEL=INFO 照样记全，但不再糊在对话气泡之间。
        """
        level = getattr(logging, self.log_level, logging.WARNING)
        root = logging.getLogger()
        root.setLevel(level)
        for handler in list(root.handlers):
            root.removeHandler(handler)

        console = logging.StreamHandler()
        console.setLevel(logging.INFO if terminal_info else max(level, logging.WARNING))
        console.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(console)

        for noisy in ("httpx", "httpx2", "httpcore", "openai", "urllib3"):
            logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

        try:
            self._rotate_runtime_log()
            file_handler = logging.FileHandler(self.audit_dir / RUNTIME_LOG, encoding="utf-8")
        except OSError:
            return
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", datefmt="%H:%M:%S")
        )
        root.addHandler(file_handler)

    def _rotate_runtime_log(self) -> None:
        path = self.audit_dir / RUNTIME_LOG
        if path.is_file() and path.stat().st_size > RUNTIME_LOG_MAX_BYTES:
            previous = self.audit_dir / f"{RUNTIME_LOG}.1"
            with contextlib.suppress(OSError):
                os.replace(path, previous)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置。"""
    settings = Settings()
    settings.ensure_directories()
    return settings
