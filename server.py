"""MySoulBot 本地服务入口：给酒馆（SillyTavern）用的 OpenAI 兼容端点。

    python server.py                     # 只绑 127.0.0.1，端口取 SERVER_PORT（默认 11555）
    python server.py --port 11555
    python server.py --public            # 绑 0.0.0.0：整个网段都能读写这套灵魂与记忆，想清楚再用

酒馆侧：API 连接 → Chat Completion，客户端选 OpenAI，
Server URL 填 `http://127.0.0.1:11555/v1`，Model 填引擎的 MODEL。
多端共享同一套 `storage/`：CLI 说完切酒馆，人格、记忆、熟络度、情绪余温全都在。

真正的实现都在 `core/server.py`，这里只是入口。
"""

from __future__ import annotations

import sys

from core.server import main

if __name__ == "__main__":
    sys.exit(main())
