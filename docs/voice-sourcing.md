# 那把嗓子：素材、判断与下一步

先说结论：**素材我找到了，但选哪一把要人来听**。工具那侧我已经配好（`TTS_SPEECH_*`
+ `scripts/voice_audition.py`），克隆本身**不在本机跑**（这台是 ARM 板子，没独显），
要起一个本地 VoiceStudio 或接一个兼容网关才能真做。

## 1. 三条来源的实测结果

| 来源 | 取到了什么 | 规格 | 能不能当母音色 |
|---|---|---|---|
| DeepSeek 官方 demo（`voice-demos/mira_zh.*.mp3`） | ✅ 61 KB，单条 7.63 秒 | 24 kHz / 单声道 / 64 kbps | **能当参考，但有两条硬伤**：① 只有一句、无情绪跨度，克隆出来稳但平；② 那是 DeepSeek 的产品声线，拿它当「她的声音」既不像她、也把别人的角色身份抄过来 |
| `zuishi2006-pixel/dsh-whale-pet`（桌宠衍生） | ✅ 14 条分好类的短句，合计 39.7 秒 | 24 kHz / 单声道，每条 2.3–3.3 秒 | **最适合当母音色的料**：同一个声优、三种情绪（`welcome` 打招呼 / `coquetry` 撒嗲 / `celebrate` 高兴），零样本克隆要的正是「干净 + 有情绪起伏 + 单人」 |
| `aceice01/dsh-whale-pet`（上游那份） | 仓里**没有音频文件**，只有代码里引的远程 URL | — | 要看它 `lib/pet.html` 引的那个 CDN；同一条声线，不必重复取 |
| Fish Audio「鲸鱼娘西丝特」 | ❌ 该页需要登录/JS，这里取不到原始 wav | — | 那是较早的 VTuber 角色声，且是**别人角色的音色**——不建议作为母样本（角色身份与声音都容易串味） |

## 2. 我已经拼好的那条母样本

放在 `storage/run/voice-ref/`（`storage/run/` 已 gitignore：**第三方录音不进公开仓**，
这是版权上的最低要求，也避免仓库被二进制撑大）：

```
storage/run/voice-ref/
├─ deepseek-mira-zh.mp3            官方那句，7.63s，留着做对照
├─ pet/*.mp3                       桌宠 14 条原始料
└─ pet-reference-combined.mp3      ★ 母样本：8.16s / 24kHz / 单声道
```

母样本的做法（要换组合就重跑这三行）：

```bash
cd storage/run/voice-ref
for f in pet/voice-welcome-0.mp3 pet/voice-coquetry-1.mp3 pet/voice-celebrate-3.mp3; do
  ffmpeg -y -i "$f" -af "highpass=f=90,loudnorm=I=-16:TP=-1.5:LRA=11" -ar 24000 -ac 1 "norm_$(basename $f .mp3).wav"
done
ffmpeg -y -i norm_voice-welcome-0.wav -i norm_voice-coquetry-1.wav -i norm_voice-celebrate-3.wav \
  -filter_complex "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]" -map "[out]" -ar 24000 -ac 1 \
  pet-reference-combined.wav && ffmpeg -y -i pet-reference-combined.wav -c:a libmp3lame -q:a 3 pet-reference-combined.mp3
```

三条各取一种情绪（打招呼 / 撒嗲 / 高兴）、去低频、响度归一，比单条 3 秒的样本稳得多。

## 3. 用哪个工具克隆（按「零改动程度」排）

1. **VoiceStudio（本地，推荐）**：`TTS_PROVIDER=openai_compat` +
   `TTS_SPEECH_BASE_URL=http://127.0.0.1:<port>/v1` + `TTS_VOICE_DAY=<克隆出来的音色名>`。
   免 key、OpenAI 兼容 `/v1/audio/speech`，我们**已经**支持这条通路，代码零改动。
   注意：应用是 AGPL-3.0、自带模型权重多为 CC-BY-NC（禁商用）——**当外部服务调用不受传染**，
   别把它的码 vendor 进本仓。
2. **CosyVoice（阿里）**：v3-flash / v3-plus 支持**复刻音色 + SSML 停顿**，中文口语自然度是这几条里最好的。
   代价：官方通路是 DashScope WebSocket + SDK，要加依赖；自建开源版要 torch。
   真要接，改动只该落在 `core/tools/voice.py` 里加一个 `tts_ssml` 生成分支（把 `_pauses_in`
   补出来的句读换成 `<break time="240ms"/>`），其余不动。
3. **edge-tts（现状）**：只能从微软那批公共音色里挑，挑不到「像她」的那一把——
   这就是为什么要克隆。但**先别拆**：VoiceStudio 起不来时它是兜底（`auto` 的第一选择）。

## 4. 还差的一步（要你做）

1. 听 `storage/run/voice-ref/pet-reference-combined.mp3`，判一句：**这把嗓子是不是她**。
   不是的话从 `pet/` 里换三条重拼（2.3 秒那几条都行，同一声优即可）。
2. 定下工具（我建议 VoiceStudio 本地起一个），把那条母样本喂进去克隆，拿到音色名。
3. 回来填 `.env` 两行——**日夜分成两套**才是「像她」：白天正常、深夜那把更低更慢
   （时段韵律 `0.79 倍速 / 173Hz` 已经实现，缺的只是音色本身换一套）。
4. 用 `.venv/bin/python scripts/voice_audition.py --provider openai_compat` 先验通路，
   再决定改哪套 `TTS_RATE_BIAS` / `TTS_PITCH_BIAS_HZ`。

## 5. 一句话边界

声音克隆的是**嗓子的形状**，不是任何人的身份。母样本用第三方配音这件事，自用可以，
一旦把克隆出的音频公开分发或商用，就得先解决那 14 条素材的授权——
Fish Audio 那个角色声我为什么不建议用，也是同一个理由。
