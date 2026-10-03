"""MySoulBot 本地服务入口：酒馆（SillyTavern）的 OpenAI 兼容端点 + 沉浸 Web 面板。

    python server.py                     # 只绑 127.0.0.1，端口取 SERVER_PORT（默认 11555）
    python server.py --port 11555
    python server.py --grace 45          # 停机时最多等 45s，让在途记忆落盘
    python server.py --public            # 绑 0.0.0.0：整个网段都能读写这套灵魂与记忆，想清楚再用

酒馆侧：API 连接 → Chat Completion，客户端选 OpenAI，
Server URL 填 `http://127.0.0.1:11555/v1`，Model 填引擎的 MODEL。
多端共享同一套 `storage/`：CLI 说完切酒馆，人格、记忆、熟络度、情绪余温全都在。

面板侧：浏览器打开 `http://127.0.0.1:11555/panel`（可加 `?user=alice`）。
纯静态 HTML/CSS/JS，零打包零外链；只读——熟络度在这里是一根不能拖的条。
`PANEL_ENABLED=false` 只关面板，酒馆端点不受影响。

QQ 侧：`.env` 里 `ONEBOT_ENABLED=true` 之后，本进程另开 `ONEBOT_PORT`（默认 11556）
当 OneBot v11 反向 WebSocket 的服务端，等 LLOneBot / NapCat 那类裸机协议端连进来。
同一个 she、同一份记忆：私聊记在 `qq_private_<号>`，群聊记在 `qq_group_<群号>`。
脚手架：`bash scripts/qq/setup_onebot.sh`，起协议端与扫码：`bash scripts/qq/run_onebot.sh`。

常驻：`bash scripts/daemon.sh start|stop|restart|status|logs`，
或把 `scripts/mysoulbot.service` 装进 systemd（SIGTERM 先排空记忆再退出）。

真正的实现都在 `core/server.py`，这里只是入口。
"""

from __future__ import annotations

import sys

from core.server import main

if __name__ == "__main__":
    sys.exit(main())
