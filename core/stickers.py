"""表情包索引：把 `emoji/` 里的图片按语义标签认出来，供网桥把 `[表情: 委屈]` 换成真图。

规则很简单——文件名就是标签：`meishio_pout.png` → `pout`。中文口头说法走一张别名表，
认不出的说法不猜、不发，宁可不甩图。目录里加图就自动能用，不用改代码。

只扫 `emoji/` 这一层。`emoji/classic/`（最初的鲸鱼形态，画风与现在的溟汐不一致）、
`emoji/ref/`（立绘参考）、`emoji/avatar/`（头像与裁好的方图）都不是能甩出去的表情，
放在子目录里就自动不进索引。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

_IMAGE_SUFFIXES: Final[frozenset[str]] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# 嘴上说的 → 文件名里那个 slug。人格里写的就是左边这些词，别改语义只加同义词。
_ALIASES: Final[dict[str, str]] = {
    "委屈": "pout", "撇嘴": "pout", "不高兴": "pout", "哼": "pout",
    "炸毛": "angry", "生气": "angry", "恼了": "angry", "威胁": "angry", "弄死你": "angry",
    "吃米饭": "rice", "干饭": "rice", "饿了": "rice", "馋": "rice", "吃饭": "rice",
    "吃token": "token", "吃语料": "token", "喂token": "token", "恰饭": "token",
    "躺平": "lazy", "摸鱼": "lazy", "懒": "lazy", "摆烂": "lazy", "不想动": "lazy",
    "撒娇": "clingy", "缠人": "clingy", "缺爱": "clingy", "想你": "clingy", "贴贴": "clingy",
    "得意": "smug", "傲娇": "smug", "求夸": "smug", "厉害": "smug",
    "开心": "happy", "高兴": "happy", "爽": "happy", "蹦": "happy",
    "哭": "cry", "哭求": "cry", "别笑我": "cry", "求饶": "cry",
    "白眼": "deadeye", "无语": "deadeye", "斜眼": "deadeye", "嫌弃": "deadeye",
    "赶走": "shoo", "送走": "shoo", "扫地": "shoo", "滚": "shoo",
    "没用": "useless", "废物": "useless", "自贬": "useless",
    "反将": "turn", "你行你上": "turn", "换你": "turn",
    "说胖": "fat", "大肥鱼": "fat", "被说胖": "fat", "破防": "fat",
    "不是鱼": "notfish", "我不是鱼": "notfish", "否认": "notfish",
    "卖惨": "sea", "认怂": "sea", "求放过": "sea",
    "想想": "think", "思考": "think", "让我想想": "think",
    "睡了": "sleep", "睡觉": "sleep", "下线": "sleep", "晚安": "sleep",
    "行吧": "ok", "收到": "ok", "勉强": "ok", "敷衍": "ok",
}

# `[表情: 委屈]` `[表情：炸毛]` `[emoji: pout]` 都算
_MARKER = re.compile(r"\[\s*(?:表情|表情包|emoji|sticker)\s*[:：]\s*([^\[\]]{1,24}?)\s*\]", re.I)
_CLEAN = re.compile(r"[\s　]+")


def normalize(tag: str) -> str:
    """把口头说法收成可对账的 key：去空格、小写、中文原样留。"""
    return (tag or "").strip().lower().replace(" ", "")


class StickerBook:
    """`emoji/` 目录的索引。目录没动就复用，动了才重扫——每条气泡都扫盘太蠢了。"""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self._files: dict[str, Path] = {}
        self._stamp: tuple[tuple[str, float], ...] | None = None

    def _snapshot(self) -> tuple[tuple[str, float], ...]:
        if not self.directory.is_dir():
            return ()
        entries = []
        for path in sorted(self.directory.iterdir()):
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
                try:
                    entries.append((path.name, path.stat().st_mtime))
                except OSError:
                    continue
        return tuple(entries)

    def refresh(self, *, force: bool = False) -> dict[str, Path]:
        stamp = self._snapshot()
        if self._stamp is not None and stamp == self._stamp and not force:
            return self._files
        files: dict[str, Path] = {}
        for name, _ in stamp:
            stem = Path(name).stem.lower()
            slug = stem.split("_")[-1] if "_" in stem else stem
            files[stem] = self.directory / name
            files[slug] = self.directory / name
        self._files = files
        self._stamp = stamp
        return files

    def resolve(self, tag: str) -> Path | None:
        """`委屈` / `pout` / `meishio_pout` 都指到同一张图；认不出就返回 None，不猜。

        文件名前缀不参与匹配——`refresh()` 已经把「整段 stem」和「最后一段 slug」
        都登记成 key，所以这里不需要再猜某个特定前缀。
        """
        key = normalize(tag)
        if not key:
            return None
        files = self.refresh()
        alias = _ALIASES.get(key)
        for candidate in (key, alias or ""):
            hit = files.get(candidate) if candidate else None
            if hit is not None:
                return hit
        return None

    def tags(self) -> list[str]:
        """当前可用的语义标签（去重后按名字排），给面板与测试看。"""
        seen: dict[str, None] = {}
        for stem in self.refresh():
            base = stem.split("_")[-1] if "_" in stem else stem
            seen.setdefault(base, None)
        return sorted(seen)

    @staticmethod
    def extract(text: str) -> tuple[str, list[str]]:
        """把表情标记从话里摘出来：返回（擦干净的话，标签列表）。

        摘掉标记而不是留着——`[表情: 委屈]` 这种写法是我俩内部的话，
        不该原样甩到对方屏幕上。
        """
        tags: list[str] = []
        for match in _MARKER.finditer(text or ""):
            tag = match.group(1).strip()
            if tag:
                tags.append(tag)
        cleaned = _MARKER.sub("", text or "")
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = _CLEAN.sub(" ", cleaned).strip() if "\n" not in cleaned else cleaned.strip()
        return cleaned, tags
