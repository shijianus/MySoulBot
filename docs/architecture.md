# 溟汐（MEISHIO / MySoulBot）架构说明

本文对着代码写过一遍，关键结论都带 `文件:行号`。改代码时请同步改这份文档——
它是目前项目唯一的设计文档，没有 README。

---

## 1. 全景

```
                          ┌──────────────────────────────────────────┐
QQ 客户端 ⇄ NapCat ──反向WS──►  OneBotBridge  :11556                 │
                          │  (我们是服务端，协议端拨进来)              │
                          ├──────────────────────────────────────────┤
其他客户端 ──OpenAI 兼容──►  SoulServer     :11555  /v1/chat/completions
                          │  /panel /api/*  /media  /healthz         │
                          ├──────────────────────────────────────────┤
管理者 ──键盘──►  main.py「admin ▸」控制台（唯一能开配对台、写 USER.md）│
                          └──────────────────────────────────────────┘
                                       │
              ┌────────────────────────┼─────────────────────────┐
              ▼                        ▼                         ▼
        MySoulBot 引擎           StorageManager             ToolRegistry
        core/bot.py              core/storage_manager.py    core/tools/*
        提示词/出话/工具往返        两棵资料树 + 锁 + 归档        能力闸门 + 审批工单
              │                        │                         │
              ▼                        ▼                         ▼
        UpstreamPool             向量检索两段式              灵魂资产同步
        线路赛跑/摘除             bge-m3 → bge-reranker       ClawdSoul 分支
```

分层职责：接入（网桥/HTTP）→ 身份（判定，不自称）→ 决策（要不要答、答成什么样）
→ 生成（提示词装配 + 上游路由）→ 修饰（出话过滤 + 长句定形 + 表情）→ 落盘（记忆与检索）。

## 2. 进程与入口

只有一个 asyncio 事件循环，两个入口，**QQ 接入只存在于 server 进程**：

| 入口 | 作用 |
|---|---|
| `server.py`（34 行 shim，`:31` 转 `core.server.main`） | 生产守护。手搓 HTTP/1.1（`asyncio.start_server`，无 FastAPI/aiohttp）提供 OpenAI 兼容口 `127.0.0.1:11555`，并在同进程内起 OneBot 网桥 |
| `core/server.py:799 main()` / `:832 _serve()` / `:826 asyncio.run` | 真实现。`SoulServer.start():144-153` 按 `onebot_enabled` 建桥、`bot.bind_qq_port(bridge):149`、`bind_vector_index():152` |
| `main.py` | `admin ▸` 交互控制台（`:107`、`:220-222`，输入线程 `:231-236`），命令派发 `:651-698`，帮助 `:701-725`，flag `:1132-1150`；退出走 `os._exit`（`:1184`）绕开线程 join |

唯一的线程是 CLI 的 `_StdinPump`（`main.py:227-280`）；其余全是协程。

## 3. 客户端接入与登录鉴权

协议是 **OneBot v11 反向 WebSocket**——机器人是 WS 服务端，NapCat/LLOneBot 主动拨入。
帧层自己实现（`qq_onebot.py:286 ws_accept_key`、`:292 encode_frame`、`:305 unmask`、`:317 read_frame`），零第三方 WS 依赖。

握手与鉴权（`core/adapters/qq_onebot.py`）：

- `:1481 start()`：`ONEBOT_ACCESS_TOKEN` 为空且绑定非回环地址 → **直接拒绝启动**（`:1484-1488`），
  理由写在异常里：那等于把 QQ 的进出摊给整个网段。
- `:1489 asyncio.start_server(self._handle, host, port)`
- `:381 check_handshake()`：要求 GET + Upgrade + `Sec-WebSocket-Key` + 版本 13
- `:394` token 用 `secrets.compare_digest` 比对，取自 `Authorization: Bearer` 或 `?access_token=`（`:369`）
- 心跳 `meta_event` → `_on_meta:1600`：记 `last_heartbeat`、`_whoami:1658`、`apply_profile`、`_drain_hellos:1636`

配置项：`ONEBOT_ENABLED/HOST/PORT/ACCESS_TOKEN`（`config.py:343-355`）。NapCat 侧契约留档在
`scripts/qq/napcat_config.json`（含真实鉴权串，**已 ignore**），其 `websocketClients[0].url` 与
`token` 必须分别等于 `ws://<ONEBOT_HOST>:<ONEBOT_PORT>` 和 `ONEBOT_ACCESS_TOKEN`，
`messagePostFormat:"array"`、`heartInterval:5000`。`onebot/` 是裸机 NapCat 运行目录（整机不入版本库）。

## 4. 身份与配对：两棵树 + 两道反向 crossing

**身份是判定出来的，不是自称出来的。**

- `core/identity.py:225 resolve_identity()` 读 `storage/data/owner/OWNER.json`（`config.py:710`）
- `:207 owner_user_ids()` = `{owner}` ∪ `{qq_private_<record.qq>}` ∪ `{qq_private_<OWNER_QQ>}`
  —— 管理者从自己 QQ 私聊说话时不会被降级成交互者
- `Tier`（`:97`）与 `is_owner`（`:199`）决定 `tree`（`:204`）

物理两棵树，不是 `users/` 下多一个子目录：

| | 路径 | 谁能碰 |
|---|---|---|
| 管理者树 | `storage/data/owner/`（`config.py:703`） | 只有管理者身份 |
| 交互者树 | `storage/data/users/<engine_user_id>/` | 各人只见自己 |

`storage_manager.py:157 tree_root()` 只把字面量 `owner` 路由到 owner 树，`:168 user_dir()` 再用
`path.parent != root` 把拼路径越界挡死——越界由路径本身决定，不靠调用方自觉。
注意：**管理者自己的日常聊天记录仍在 `users/qq_private_<qq>/`**，owner 树只放配对事实与长期资产。

配对流程（`core/identity.py:8-25` 是这段的设计说明）：

```
①口令：控制台 → QQ
   main.py:601 _pairing() → PairingDesk.start(channel="cli")   ← 只有键盘前的人能开台
   core/pair_phrase.py:84 make_phrase()：5 秒硬预算、全线路赛跑，让模型现生成一句 ≤15 字短语
   :263 phrase_ok() 长度上限 + 禁用词（管理员/验证/口令/配对/password/token）
   :167 local_phrase() 模型没接住时，回落到本地话术本按槽位拼装
   话术本 storage/soul/PAIRING.box + 钥匙 storage/run/keys/pairing.box.key
   core/pair_box.py：HMAC-SHA256 keystream-CTR + encrypt-then-MAC（magic MSBPAIRBOX\x01），
   模型与工具都没有读这个盒子的路径

②码：QQ → 控制台
   identity.py:294 consume_pairing() 口令命中 → present():666 记录来源
   → check_unique():675（同一场出现 ≥2 个来源则整场作废 :322）
   → derive_code():178 = HMAC(mainkey, challenge_id|source_key)，字符表去掉 OI01（:72）
   人把 QQ 上收到的码粘回控制台 → submit_code():688

成功后：write_owner():272 原子写 OWNER.json → 留跨进程 HELLO-<qq>.json
        → 网桥下次心跳 _drain_hellos 让她主动招呼一句
```

## 5. 一条消息的完整旅程

| 阶段 | 位置 | 做的事 |
|---|---|---|
| 收帧 | `_Connection.serve:1362` → `read_message:1336` | 分片重组、ping/pong/close |
| 分流 | `on_frame:1565` | `echo` 先结算 → `meta_event` 心跳 → `message` 派生 `_on_message:1785` → `request` 走 `_on_request:2081` |
| 归属 | `parse_inbound:698` → `engine_user_id:610` | `qq_group_<gid>` / `qq_private_<uid>`（`_safe_id:661` 清洗）；群内说话人前缀 `[昵称]: `，名片优先于昵称（`:765`） |
| 过滤 | `:1790/:1793`、`_is_duplicate:2035` | 丢自发、丢空、按 `message_id` LRU 去重 |
| 配对拦截 | `consume_pairing:1802` | 群来源永不拦截 |
| 唤醒 | `decide_wake:640` | 私聊必答；群聊看 @/点名，`mentioned_other` 静默；另有 `GROUP_ALWAYS_REPLY` 与 discretion 两档 |
| 防抖 | `_fresh_topic:1833`、`_Batch:227`、`_hold/_flush:1850-1901` | `loop.call_later` 合批，`merge_inbound:914` 并句 |
| 洪水闸 | `_too_many:2046` | 刷屏期直接吞 |
| 补全 | `_enrich:1993` | 向协议端回查引用/转发正文 |
| 出话锁 | `_answer:2113` | per-user `asyncio.Lock`；忙则 `_enqueue:1903`（上限 `_QUEUE_MAX=8`），释放后回放 `:2158` |
| 引擎 | `core/bot.py:622 stream_reply()` | `Session.busy` 守卫 `:646`、图片入库 `_ingest:806`、节律与熟络 `_pulse:568` |
| 装配 | `prompt_builder.py:437/497`、`decide_prompt_tier:322` | 人格/宪法/回看/检索/判断层/温度 分层进系统提示 |
| 生成 | `bot.py:999 _call_stream()` → `:1064 _stream_on_route()` | 选路、同回合 failover、赛跑 |
| 修饰 | `_outbound_filter:2309` | 剥舞台提示、`neutralize_cq`、`secrecy.guard`、`BubbleStream:1057` 长句定形、`split_bubbles:1169`、表情抽取 |
| 发送 | `_send_bubble:2331` → `_call:2460`/`_request:2431` | `send_group_msg` / `send_private_msg`；打字状态保活 `_keep_typing_loop:2245`；语音后台 `_speak:2400` |

## 6. 记忆数据模型

`StorageManager`（`core/storage_manager.py:145`）是唯一文件闸口，受管文档
`DOCS = ("SOUL","USER","MEMORY","RELATIONS")`（`:39`），路径 `<tree>/<user_id>/<DOC>.md`。

| 文件 | 谁写 |
|---|---|
| `SOUL/USER/MEMORY/RELATIONS.md` 初始化 | `_init_user_dir:274`、`_ensure_doc_file:302`（从 `storage/templates/`） |
| `MEMORY.md ## 事实` | 抽取器 → `append_facts` → `_append_entries:403` |
| `RELATIONS.md ## 动态` | `append_dynamics:393`（抽取器 + `Reflect` 工具 `tools/soul.py:74`） |
| `RELATIONS.md ## 温度` | **只有** `RapportEngine.publish:211`（`core/rapport.py`） |
| `RECAP.md` | `recap._write:124` |
| `state.json`（不入库） | `bot.py:602/1426`：心情、耐心、熟络计数器 |
| `persona.json` | `bot.apply_persona:506` |
| `CLAWD.md`（跨用户宪法） | `clawd_soul.py:119 append_note`，来自 `Reflect target=self` |
| `MOOD.md`（今日日记） | `mood_soul.py:87`，来自认知回路 `cognition.py:112` |
| `presets/<slug>/{SOUL.md,preset.json,card.json}` | `PersonaLibrary._write_pair:550`、`import_card:508` |

`USER.md` **没有引擎写入方**，只有模板播种和人通过 `/panel edit user`（`main.py:73,890-908`）手写。

### 熟络度（温度）

阶段 `stranger 0-25 / acquaintance 26-50 / friend 51-75 / confidant 76-100`
（`rapport.py:39-48`）。增量：基线 `0.35`、深夜 `+0.5`、修复 `+1.6`、自我披露 `0.7×n`（上限 2.0）、
动态 `0.5×n`（上限 1.5）；7 天不动 cooling `0.9/天`（上限 18），地板是 `峰值 × 0.4`
（`config.py:329`；`advance:130`）。计数器在 `state.json`，人读的那段由 `publish` 落进 RELATIONS.md。
全代码没有任何 set 入口：CLI（`main.py:922`）和面板（`panel.py:93 editable: False`）都改不了它。

## 7. 检索：两段式，且没有分块

`vector_index.py:176 search_sync()`：查询嵌入 → **只扫该用户的行**（`:192`）→ 纯 Python 点积
（地板 `floor=0.12`）→ 放大到 `rerank_recall=20` → `:217 _rerank` → 低于 `rerank_floor` 砍掉。

- 库在 `storage/data/vectors.db`（`:87`），schema `memories(key,user_id,kind,text,backend,dim,day,updated)`（`:38`），WAL + 4s busy（`:116`）；嵌入后端换过一次就清表（`:123`），维度上限 2048（`:155`）
- **一行 `- [日期] 文本` 就是一条记录**（`parse_facts:84`，key 是 `blake2b(user+kind+text)`，正文截 600 字）
- 增量只由 `MemoryExtractor._index_writes:315` 写入（`kind=fact|relation`）；全量重建 `reindex:266` **没有生产调用方**
- `embeddings.py:139 Embedder` 支持 cohere `/v2/embed` 与 OpenAI 兼容 `/embeddings`，失败降级为确定性 384 维字符 ngram 哈希；只在一次真成功之后才自称远程后端（`:148`）
- `rerank.py:90 Reranker` POST `{base}/rerank`，任何失败返回 `[]` 让调用方保持向量序
- 生产配置：`EMBED_MODEL=BAAI/bge-m3`、`RERANK_MODEL=BAAI/bge-reranker-v2-m3`（硅基流动，独立于回复线路的 key）

红线：`prompt_builder._recall:661` 在 `soul_files_only` 为真时返回 `[]`——生产就是这个状态（`config.py:246`）。
检索结果只以【被这句话勾起来的旧事】（`:686`）这一种形态出现，标明按相关度而非时间。

## 8. 写入并发与体积闸门

三层叠着（`core/storage_manager.py`）：

1. per-path `asyncio.Lock`（`:225 _lock_for`）
2. 兄弟文件 `fcntl.flock(LOCK_EX)`，`.MEMORY.md.lock` 这种命名（`:242 _acquire_file_lock`），
   覆盖整个「读-改-写」事务（`_critical:232`、`mutate_doc:314`、`compact_memory:464`）
3. 原子落盘：目录内 `mkstemp` → `fsync` → `os.replace`（`atomic_write:68`）

体积：条目淘汰后 gzip 进 `archive/<DOC>-*.md.gz`（`_archive_entries:485`），日志轮转只碰往日
（`rotate_transcripts:569`），上限 4 MB/日志、8 MB/文档（`config.py:613`），备份留 5 份（`backup_doc:354`），
`scan_size_gate:722` 的 20 MB 由同步流程使用。
逐轮追加用独立的 `fcntl.flock`（`_append_text:747`）。

**已知不一致**：`recap.py:124 _write` 用裸 `write_text`，既不走 `atomic_write` 也不拿 flock。

## 9. 上游模型路由与延迟工程

`ROUTES` 是一个 JSON 数组（`config.py:86`），每项 `{name, base_url, api_key, model, tiers, priority, vision_model, timeout}`；
`api_key` 支持字面量、`env:VAR`、`data:<文件>` 三种取法（`upstream.py:227 _resolve_key`）。主线路用 `API_KEY/BASE_URL/MODEL`（`config.py:80-85`）。

- `UpstreamPool.candidates:111` / `pick:136`：先 `priority`，再按 **EWMA 首字可见延迟**排序（`report_ok:141`，α=0.4）
- 坏线路摘除 `route_fail_threshold` 次 + 冷却 `route_cooldown_seconds`（`config.py:94-102`），`route_strict_order` 可关掉排序
- 赛跑在引擎里：`bot.py:1064 _stream_on_route` 打首发，`first_visible_hedge` 秒内不见正文就在**下一条线路**放对手任务，`asyncio.FIRST_COMPLETED` 认先见字的那条（`bot.py:1177`），不等慢的
- 短输出点名线路：`ask_once(prefer, race)`（`bot.py:351`）；`pair_phrase.short_ask` 传 `race=not PAIR_PHRASE_ROUTE` —— **留空即同时赛跑，这是推荐值**
- 看门狗：`first_token_timeout:110`、`first_visible_timeout:115`、`first_token_retries:126`、`empty_retry_max_tokens:130`
- 全线路皆坏 → 立刻给空串，不抛也不烧预算

## 10. 工具层与能力闸门

`Tool` ABC（`tools/base.py:64`）同时提供两套出口：`native_spec():96` 的 OpenAI function-calling JSON，
和 `from_bare():128` 的内联 `⟦tool:name k=v⟧` 文本协议。`registry.native_specs():167` 在 native 模式下
作为 `tools=` 传入（`bot.py:699`），否则 `instructions():185` 把协议写进提示词；
模式由 `bot._tool_mode:864` 决定。`StreamGuard`（`tools/protocol.py:199`）负责把指令与标记从可见流里剥掉。

安全边界，逐层：

1. `registry.resolve:200` 的别名与模糊匹配（阈值 0.62）**只对非 sensitive 工具生效**。
   sensitive = `git_sync`(`tools/git.py:23`)、`reflect`(`tools/soul.py:34`)、
   `qq_publish_qzone`/`qq_decide_request`/`qq_delete_qzone`/`qq_group_verify`(`tools/qq_account.py:229,307,333,362`)
2. 判不出危险性的未知动作 → 只写一张工单 `storage/run/approvals/<id>.json`（`sandbox.py:151`），
   **什么都不执行**；裁决只能由人做（`decide():183`，走 `scripts/approve.sh`），面板只列不做
3. 文件闸门 `assert_writable:68`：先黑名单代码与资产本体（`*.py`、`.env`、`scripts/`、`web/`、`core/`、
   `emoji/`、`CLAWD.md`、`templates/`、`run/approvals/`），再白名单只放 `data/users/<id>/*.md` 与 `soul/MOOD.md`
4. 草稿工位 `storage/sandbox/<uid>/`（`scratch_dir:99`）是**另一道独立闸** `assert_scratch:105`：
   能在那儿放查来的材料和算一半的数，不代表能改任何灵魂文件。`ScratchWrite/Read/List`（`tools/host.py:136`）
   限平铺文件名、64 KB/张、40 张
5. QQ 账号级能力 `ACCOUNT_TOOLS`（`tools/qq_account.py:451`）三重门：`qq_account_enabled` ∧ `ctx.is_owner`
   （`tools/base.py:157`，来自 `resolve_identity`）∧ 单项开关（`config.py:462-490`）。
   说说广播按 `storage/data/owner/QZONE.json` 限流（`:423`），出口照旧过 `secrecy.guard`（`:55`）
6. 审计写 `storage/logs/tools.jsonl`，**只记参数名不记值**，1 MB 截尾（`registry.py:264`）

`HostStats`（`tools/host.py:82`）无参数，只读 `/proc`。

## 11. 灵魂资产与人格切换

- `clawd_soul.py`：跨用户宪法 `storage/soul/CLAWD.md`，含自我演进段「我给自己的备注」
- `mood_soul.py`：今日日记，上限 12 条 / 160 字，带提示注入拒绝正则（`:93`）
- `cognition.py`：慢速反思回路，往 MOOD.md 落笔
- `sync.py`：主仓备份（密钥扫描 `:42/:373`、体积闸、origin 钉死 `:227`、`pull --rebase --autostash` 永不 force `:242`）
- `soul_sync.py`：灵魂资产走**独立分支** `shijianus/ClawdSoul @ soul`（`:30-31`），中转目录 `storage/run/clawdsoul`，
  搬 `soul/{CLAWD,MOOD}.md`、`templates/*.md`、各用户文档 + `persona.json` + `presets/`，owner 树单独一份（`:90`），
  自带 ignore 排除 logs/state/artifacts/backups/keys（`:110`）；定时 `10 4 * * *`
- 换人格 `bot.apply_persona:489`：备份 SOUL → 写 preset SOUL → persona.json → 清近程上下文（除非 `keep`）→ 播 `first_mes`；
  **CLAWD.md 明确不动**（`main.py:1038`）
- `card_loader.py` 只进不出：酒馆角色卡编译成 `SOUL.md`

## 12. 面板管理

三块，写权限严格递减：

**① `admin ▸` 控制台**（`main.py`）。提示符只在等键盘那一刻出现（`:107,220`），回话一律浅色无前缀。
30+ 命令：status/prompt/memory/relations/soul/clawd/profile/edit/append/note/persona/user/model/tools/tool/log/archive/audit/web/sync/clear/debug/whoami/rapport/rhythm。
唯一能手写 `USER.md`、开配对台、裁决的地方。

**② Web 面板**（`web/panel/` + `core/panel.py`）：**纯只读视图模型**，和终端共用同一套
`build_presence` / `RapportEngine.read`（`panel.py:10-11`）。`build_status:64`、`activity_of:142`、
`archive_facts:163`（能读 gzip 归档）、`build_timeline:177`、`build_doc:202` 限四篇文档，温度 `editable: False`（`:93`）。

服务侧（`core/server.py`）：静态文件来自 `web/panel/`（`:69`）且过文件名白名单（`:93`、`_send_asset:323`）；
`_dispatch:196` / `_panel_route:250`；`/api/*`、`/panel*`、`/media*` **只允许 GET**，其余 405（`:92,231`）。
`app.js`：每 20s 拉 `/api/state` + `/api/timeline`（`:16,148,434`），点 tab 取 `/api/docs/<key>`（`:129`），
`/healthz` 探 TTS/视觉位（`:410`）；仅有的两个 POST 是 `/v1/chat/completions`（`:204`）与 `/voice/say`（`:306`）；
渲染全走 `textContent`（`:11`）。产物经 `/media/<uid>/<file>` 出，后缀白名单 + 内容嗅探交叉校验，单文件、防穿越（`_send_artifact:348`）。

**鉴权现状**：面板只受 `PANEL_ENABLED`（`config.py:606`）控制，`?user=` 仅靠路径安全（`:302`）校验，
CORS 为 `*`（`core/server.py:64`）。安全靠默认绑 `127.0.0.1`；`server.py --public` 一旦开启即等于把全部只读记忆摊开。

**③ 审批**：面板只列 `storage/run/approvals/` 的工单（`:268`），裁决在 `scripts/approve.sh`。

## 13. 视觉、表情与语音

`vision.ingest:125` 把本地文件 / data URL / http(s) 归一成 `ImageRef`，按**魔数**而非扩展名嗅探类型（`:75`），
落盘到 `storage/data/users/<id>/artifacts/in-<stamp>-<user>.<ext>`（`:111`）。带 `meta["images"]` 的工具结果
按 `vision_max_images` 裁剪后，以 `role:"user"` 多模态消息重新注入（`bot.py:932`），
`note():252` 负责框住它（防「报菜名」，并显式允许承认看不清）。

表情是反方向：`StickerBook`（`core/stickers.py`）把 `emoji/` 按文件名 slug 平铺索引 + 中文别名表
（`:20-41,61`），`extract():112` 从说出口的话里剥掉 `[表情: 委屈]`，网桥另发一段
`{"type":"image","data":{"file":"file://…"}}`（`qq_onebot.py:2338-2358,2378-2397`），认不出的标签静默跳过。
TTS 在后台走（`edge-tts` 是可选增强，不在 `requirements.txt`）。

## 14. 运维

- `scripts/daemon.sh start|stop|restart|status|logs`：`setsid nohup python server.py >> storage/logs/server.out`（`:129`），
  pidfile `storage/run/server.pid`（`:29`），健康检查用引擎自己的解释器打 `/healthz`（不依赖 curl，`:37`），
  OneBot 状态读 `/healthz` 的 `onebot` 字段（网桥在同进程，不是第二个守护）
- `stop` 发 SIGTERM 等 `GRACE=60`（`:31,142`），**故意不 `kill -9`**：后台记忆抽取会丢。排空计数见 `server.py:614`
- `scripts/mysoulbot.service` 模板：`Type=exec`、`Restart=on-failure`、`TimeoutStopSec=90`、`ProtectSystem=full`（路径需替换 `:20-23`）
- 无 Docker/Makefile 是有意的（`scripts/qq/setup_onebot.sh:11`）
- 排障与延迟测量脚本：`latency_probe.py`、`first_visible_probe.py`、`hedge_ab.py`、`tier_speed_matrix.py`、
  `model_sonde.py`、`turn_instrument.py`、`delivery_speedrun.py`（真网桥 + 生产防抖）、
  `live_bridge_check/live_burst_check/live_group_check`（只测时间，**从不记录回复正文**）、`long_context_check.py`
- QQ 侧：`scripts/qq/setup_onebot.sh`（探测二进制、产出 `onebot/config/reverse-ws.json`、`--merge`，从不下载或容器化）、
  `run_onebot.sh`（前台启动 + 终端二维码，二进制路径来自 `onebot/state.env` 的 `ONEBOT_BIN`）

## 15. 依赖与测试

依赖只有 5 个：`openai`、`pydantic`、`pydantic-settings`、`python-dotenv`、`rich`。
HTTP 服务、RFC-6455 帧、向量点积全部自己实现；**没有 pytest**。

十套离线测试，各自 `python tests/<name>_test.py`，用自带 `check.ok()` 打印「共 N 项断言，失败 M 项」：

| 套件 | 断言 | 钉住的东西 |
|---|---|---|
| `qq_onebot_test` | 428 | 鉴权、心跳、帧向量、群礼仪、CQ 注入防御 |
| `evolve_test` | 343 | 自我演进与灵魂资产外派 |
| `tier_test` | 170 | 管理者分层、配对握手、判断回路、账号级能力 |
| `panel_web_test` | 193 | 面板只读边界与 systemd |
| `human_test` | 167 | 拟人表现 |
| `companion_chat_test` | 157 | 会话编排与静态资产 |
| `smoke_test` | 129 | 全链路便宜闸门 |
| `lock_test` | 95 | 出话锁、检索红线反例 |
| `meishio_test` | 85 | 长句定形、板块切分 |
| `upstream_test` | 42 | 赛跑、点名线路、全线坏快速失败 |

合计 1,809 项。全部离线（假 OpenAI SSE 服务 + 真 socket + 临时 storage）。
联网探针另有 `tests/probe_models.py`、`probe_roleplay.py`、`verify_real_api.py`、`verify_persona_card.py`。

配置层面：**启动不需要任何环境变量**（`config.py` 每字段都有默认，只有 `BASE_URL`/`MODEL` 空值会被拒），
`.env` 提供的是凭据而非可启动性。

## 16. 版本边界：什么进库，什么不进

进库（本文写就时 `git ls-files` 共 197 个，准确数以该命令为准）：`config.py`、`main.py`、`server.py`、`core/**`、`scripts/**`、`tests/**`、
`web/panel/**`、`emoji/**`，加 48 份记忆资产——`storage/templates/*.md`、`storage/soul/CLAWD.md`、
`storage/presets/<3 套>/`、`storage/data/users/*/{SOUL,USER,MEMORY,RELATIONS,RECAP}.md`、
`storage/data/owner/OWNER.json`。

刻意不进库的本机状态（`.gitignore`）：`.env`、`storage/soul/*.key` 与 `PAIRING.box`、`**/credentials*`、
`storage/run/`（pidfile、`server.out`、线路 key、配对钥匙、一次性配对挑战、审批工单）、
`storage/logs/`、整个 `onebot/`、`scripts/qq/napcat_config.json`、
每棵树的 `logs/`、`state.json`、`backups/`、`artifacts/`、`*.bak-*`、`QZONE.json`、
`storage/soul/MOOD.md`、`storage/sandbox/`、`vectors.db*`、`.venv/`、`.*.lock`、`vibe_images/`。

判据是「可重建的加速器与机器本地状态不入库，人格与关系的长期资产入库」。

## 17. 已知缺口

1. Web 面板零鉴权 + CORS `*`，唯一屏障是默认环回绑定；`--public` 一开即全量只读记忆外泄。
2. `RECAP.md` 是唯一绕过原子写与 flock 的记忆文件（`recap.py:124`），与守护进程并发存在撕裂窗口。
3. 判断回路里「生成新规则」是休眠的：`JudgmentLoop._ask`（`judgment.py:380`）要 `bind_client`（`:397`），
   生产路径无人调用，线上只有 `reinforce:235` 在跑。
4. `vector_index.reindex:266`（全量重建）没有生产调用方；向量库实际只靠增量。
5. `.env.example` 与实态不同步：11 个在用的是文档里没有的（`COGNITION_ENABLED/EVERY_TURNS/LOOKBACK/TIMEOUT`、
   `PAIRING_TTL_SECONDS`、`PAIRING_GRACE_SECONDS`、`PAIR_PHRASE_ROUTE`、`ROUTES`、`EXTRACTOR_TEMPERATURE`、
   `TTS_RATE_BIAS`、`TTS_PITCH_BIAS_HZ`），`ONEBOT_NICKNAME` 缺 `=`，`EXTRACTOR_MAX_FACTS` 重复定义。
6. 配对台只能由键盘前的人开（`PairingDesk.start` 只被 `main.py:601` 调用），远程无法发起——这是设计，但值得写死在文档里。
