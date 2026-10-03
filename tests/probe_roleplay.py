"""角色扮演质量对比：用引擎真实组装的 system prompt 测候选模型。

检查三件事：流式首字延迟、是否出现 AI 自指/客服腔、是否代用户发言。
用法：
    BASE_URL=... API_KEY=... .venv/bin/python tests/probe_roleplay.py 模型1 模型2 ...
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import AsyncOpenAI  # noqa: E402

from config import Settings  # noqa: E402
from core.prompt_builder import PromptBuilder  # noqa: E402
from core.storage_manager import StorageManager  # noqa: E402

USER_TEXT = (
    "你好！我是新来的，我叫李逍遥，平时最讨厌吃香菜和葱，"
    "下周三要去成都参加技术交流会。"
)
AI_TELLS = ("作为AI", "作为 AI", "人工智能", "语言模型", "我无法", "抱歉", "建议您", "请您")
PROXY_TELLS = ("你：", "你说：", "（你", "你笑了笑", "你点头", "你回答", "用户：")


async def one(settings: Settings, prompt: str, model: str) -> dict[str, object]:
    client = AsyncOpenAI(
        api_key=settings.api_key, base_url=settings.base_url, timeout=120.0, max_retries=0
    )
    started = time.perf_counter()
    ttft = 0.0
    pieces: list[str] = []
    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": USER_TEXT},
            ],
            temperature=0.9,
            max_tokens=400,
            stream=True,
            timeout=120.0,
        )
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None) if delta else None
            if isinstance(text, str) and text:
                if not pieces:
                    ttft = time.perf_counter() - started
                pieces.append(text)
    except Exception as exc:  # noqa: BLE001
        await client.close()
        return {"model": model, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
    await client.close()
    reply = "".join(pieces).strip()
    return {
        "model": model,
        "ttft": ttft,
        "total": time.perf_counter() - started,
        "chars": len(reply),
        "ai_tells": [t for t in AI_TELLS if t in reply],
        "proxy_tells": [t for t in PROXY_TELLS if t in reply],
        "reply": reply,
    }


async def main() -> int:
    models = sys.argv[1:] or ["gpt-oss-20b", "gemma-26b-a4b-it-free", "nemotron-3-ultra-550b-a55b"]
    storage_dir = os.environ.get("PROBE_STORAGE", "/tmp/mysoulbot-probe")
    os.makedirs(f"{storage_dir}/templates", exist_ok=True)
    templates_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "storage", "templates"
    )
    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        target = f"{storage_dir}/templates/{name}"
        if not os.path.exists(target):
            with open(f"{templates_src}/{name}", encoding="utf-8") as src:
                body = src.read()
            with open(target, "w", encoding="utf-8") as dst:
                dst.write(body)

    settings = Settings(api_key=os.environ["API_KEY"], base_url=os.environ["BASE_URL"],
                        storage_dir=storage_dir)
    storage = StorageManager(settings)
    await storage.ensure_user("probe")
    prompt, layers = await PromptBuilder(settings, storage).build_system_prompt("probe")
    print(f"system prompt {len(prompt)} 字符 · 候选 {len(models)} 个\n")

    for model in models:
        result = await one(settings, prompt, model)
        print("=" * 78)
        print(f"模型 {model}")
        if result.get("error"):
            print(f"  调用失败：{result['error']}")
            continue
        print(f"  首字 {result['ttft']:.2f}s · 总耗时 {result['total']:.2f}s · {result['chars']} 字")
        print(f"  AI自指/客服腔命中：{result['ai_tells'] or '无'}")
        print(f"  代用户发言命中：{result['proxy_tells'] or '无'}")
        print("  回复：")
        for line in str(result["reply"]).splitlines():
            print(f"    {line}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
