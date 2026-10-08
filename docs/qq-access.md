# 接进来有哪两条路：NapCat 与「扫码即登录」那一类

QQ 只是第一个通道。引擎侧要的东西自始至终只有一件事——**一个会说 OneBot v11 的进程，
主动连到我们的反向 WS 上**。谁满足这条，谁就能接：NapCat、LLOneBot、ClawdBot 那一类
扫码即登录的机器人、或者将来自己写的适配器。要配的东西全在 token 与地址这两处。

## 0. 先认清方向：我们是服务端，不是客户端

```
   QQ 官方服务器 ⇄ 协议端进程（NapCat / 扫码型 bot / LLOneBot）
                          │
                          │  它主动拨出 WebSocket（reverse WS）
                          ▼
                   MySoulBot 监听  ws://ONEBOT_HOST:ONEBOT_PORT      ← 我们等它连进来
                   (core/adapters/qq_onebot.py:1489)
```

所以「接入」不是我们去找它，是**把它指向我们**。两个后果：

1. 协议端那边填的是**我们的**地址与 token，不是它的。
2. 我们的 `ONEBOT_HOST` 默认 `127.0.0.1`：协议端和 bot 不同机时，要改成 `0.0.0.0`
   或内网地址——而**没有 token 就绑非回环地址，网桥直接拒绝启动**
   （`qq_onebot.py:1484-1488`）。那一句异常是有意写死的：QQ 的进出不能摊给整个网段。

## 1. 我们这一侧要定的三行

```ini
ONEBOT_ENABLED=true
ONEBOT_HOST=127.0.0.1          # 同机就环回；跨机改成 0.0.0.0 或内网 IP（必须同时配好 token）
ONEBOT_PORT=11556
ONEBOT_ACCESS_TOKEN=<32 位随机串，两边一字不差>
```

生成 token：`python -c "import secrets;print(secrets.token_hex(16))"`
真 token 只进 `.env`；`.env` 已经在 `.gitignore` 里，**永远不许出现在任何进版本库的文件里**
（`scripts/qq/napcat_config.json` 带着真实 token，所以那份留档也一并被 ignore 了）。

token 两边都可以这样传：`Authorization: Bearer <token>` 或者 URL 上 `?access_token=<token>`。
比的是 `secrets.compare_digest`，不是 `==`（`qq_onebot.py:394`）。

## 2. 路径 A：NapCat（现在在跑的这套）

```bash
bash scripts/qq/setup_onebot.sh --merge     # 探二进制、产出 onebot/config/reverse-ws.json
bash scripts/qq/run_onebot.sh               # 前台启动 + 终端二维码，扫码登录
```

NapCat 侧必须对上这四项：`url` = `ws://<ONEBOT_HOST>:<ONEBOT_PORT>`、
`token` = `ONEBOT_ACCESS_TOKEN`、`messagePostFormat: "array"`、`heartInterval: 5000`。
心跳不只是保活——它是判断回路唯一的钟（被晾着这件事没人说话就不会触发，见 §5）。

留档样例在 `scripts/qq/napcat_config.json`（本机那份含真 token，不进版本库）。

## 3. 路径 B：扫码即登录那一类（ClawdBot 式的机器人）

这类程序通常自带扫码登录与账号托管，只要它能**向外拨一条 OneBot v11 的 reverse WS**
（或 HTTP POST 上报），就能接进来。要核对的四件事，按顺序：

1. **协议形状**：是不是 OneBot v11。v12 / 各自的私有 HTTP 形状都接不上——
   那要新写一个适配器（见 §4），不是配参数能解决的。
2. **连接方向**：它必须能配置「主动连接 ws://<我们>:11556」。只肯**监听**端口、
   等别人连它的，需要中间加一层转发（或者把适配器写成客户端模式，工作量在 §4）。
3. **token 怎么带**：`Authorization: Bearer` 还是 URL 的 `?access_token=`——两种我们都认。
   只肯在自家面板里填、发出来格式奇怪的，用 `curl -v` 或我们的日志确认头到底长什么样。
4. **消息格式**：`array`（段结构）。只回 `string` 的话，`[CQ:...]`、图片、语音、引用
   全都拿不到——`parse_inbound` 是按段结构读的（`qq_onebot.py:698`）。
   `at` 事件、`group_id`、`sender.card/user_id` 也依赖段结构。

接上之后自查（不接 QQ 也能验）：

```bash
bash scripts/daemon.sh status            # /healthz 的 onebot 字段：连上了没有、心跳新鲜不新鲜
bash scripts/live_bridge_check.sh 2>/dev/null || .venv/bin/python scripts/live_bridge_check.py
```
`/healthz` 里 `onebot.peer_online` 为真、`last_heartbeat` 在 5 秒内一跳，就算通了。
通不通的第一嫌疑人永远是 token 不一致或方向填反了（它去监听、我们也在等）。

## 4. 换通道时要动哪里（这也是当初把身份层去 QQ 硬编码的理由）

引擎里唯一的用户标识形状是 `<空间>_<那个通道里的原生号>`：QQ 报的是
`qq_private_1937490685` / `qq_group_950689514`。**新通道不需要改身份层**——
它在配对时把来源名报上来（`tg_private`、`feishu_group`），`owner_aliases()`
就按那个来源认人（`core/identity.py:207`）。历史回落只对控制台来源生效，
所以别的通道撞出一个和某个 QQ 号一样的数字，也拿不到管理者目录（这条钉了测试）。

接一个新适配器要提供的东西，照 `OneBotBridge` 的形状来：

| 要做的事 | 引擎侧对应 |
|---|---|
| 收帧、鉴权、心跳 | `on_frame` / `check_handshake` 那一段 |
| 把平台事件折成 `Inbound`：`engine_user_id`、`prompt_text`、`is_group`、`mentioned` | `parse_inbound` / `decide_wake` |
| 出站过同一道闸（舞台提示、假 CQ、出话锁、出身闸） | `_outbound_filter`（**别绕过去**，那是唯一执法点） |
| 招呼字条只取自己那一份 | `pending_hellos()` 带 `source`，`ack_hello(native, source=...)` |
| 想暴露账号级能力 | `ToolContext.qq` 那个口，工具按 `ctx.is_owner` 放行 |

## 5. 三条容易踩的坑

- **改了 `.env` 不重启不生效**：`Settings` 只在进程启动时读一次 `.env`。
- **token 一致但连不上**：多半是方向填反了（对方在监听、我们也在等）；
  或者它只发 HTTP 上报，那需要适配器而不是配置。
- **非回环绑定 + 空 token 起不来**：这是设计，不是 bug。要么配 token，
  要么把 `ONEBOT_HOST` 收回 `127.0.0.1`，别为了图快把 QQ 的进出摊给整个网段。
