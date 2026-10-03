/* 面板前端：读状态，以及陪她说话。
 *
 * 看板那一半仍然只读。熟络度在这里是一根不能拖的条：温度只能由相处攒出来，
 * 后端本来也不接受任何修改，这里连一个 PATCH 按钮都不长。
 *
 * 对话这一半只开两个出口：`/v1/chat/completions`（说话）与 `/voice/say`（出声）。
 * 发出去的是标准 OpenAI 形状，收回来的只有角色说的字——流层已经把「正在调用工具」
 * 与暗号剥干净了，这里不再显示 token、模型名或任何路径。
 *
 * 所有来自记忆、文档与对话的文字一律走 textContent，绝不拼进 HTML——
 * 她记得的那句话里万一夹着尖括号，也不该把这页变成别人的脚本。
 */
(() => {
  "use strict";

  const REFRESH_MS = 20000;
  const MAX_EDGE = 1280;
  const JPEG_QUALITY = 0.82;
  const params = new URLSearchParams(location.search);
  const user = params.get("user") || "";
  const query = user ? `?user=${encodeURIComponent(user)}` : "";

  let busy = false;
  let voiceOn = false;
  let slots = 2;
  let shots = [];

  const $ = (id) => document.getElementById(id);
  const set = (id, text) => { const el = $(id); if (el) el.textContent = text || ""; };

  // ---------------------------------------------------------------- 唯一的取数出口
  async function request(path, options) {
    const response = await fetch(path, options || {});
    if (!response.ok) throw new Error(String(response.status));
    return response;
  }

  async function get(path) {
    const response = await request(path + query, { headers: { accept: "application/json" } });
    return response.json();
  }

  function post(path, payload) {
    return request(path + query, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(payload),
    });
  }

  // ---------------------------------------------------------------- 看板
  function li(day, text) {
    const row = document.createElement("li");
    const when = document.createElement("time");
    when.textContent = day;
    row.appendChild(when);
    row.appendChild(document.createTextNode(text));
    return row;
  }

  function fill(root, items) {
    while (root.firstChild) root.removeChild(root.firstChild);
    items.forEach((item) => root.appendChild(item));
  }

  function paintState(state) {
    set("name", state.persona.name);
    set("title", state.persona.title);
    set("clock", state.rhythm.clock);
    set("slot", state.rhythm.slot);

    set("stage-label", state.rapport.label);
    set("score", `${state.rapport.score}/100`);
    $("heat-fill").style.width = `${state.rapport.score}%`;
    set("conduct", state.rapport.conduct);
    set("evidence", state.rapport.evidence.join(" · "));

    set("mood", state.mood.word);
    set("mood-left", state.mood.residual >= 15 ? `还剩 ${state.mood.residual}%${state.mood.cause ? "：" + state.mood.cause : ""}` : "没什么余温");
    set("energy", `${state.energy.patience}%`);
    set("energy-note", state.energy.turns_today ? `今天来回 ${state.energy.turns_today} 轮` : "今天还没说话");
    set("together", `${state.memory.days_together} 天`);
    set("together-note", `${state.memory.turns} 轮 · 记住 ${state.memory.facts} 件事`);
    set("body", state.rhythm.body + " " + state.rhythm.conduct);
    set("gap", state.rhythm.gap);
  }

  function paintTimeline(when) {
    const max = Math.max(1, ...when.map((day) => day.turns));
    const root = $("activity");
    root.replaceChildren(
      ...when.map((day, index) => {
        const bar = document.createElement("i");
        bar.style.height = `${Math.max(2, Math.round((day.turns / max) * 42))}px`;
        bar.className = index === when.length - 1 ? "today" : day.turns ? "recent" : "";
        bar.title = `${day.date} · ${day.turns} 轮`;
        return bar;
      })
    );
  }

  function paintMachine(timeline) {
    fill($("facts"), timeline.facts.slice().reverse().map((fact) => li(fact.day, fact.text)));
    fill($("dynamics"), timeline.dynamics.slice().reverse().map((item) => li(item.day, item.text)));
    const archived = timeline.archived.facts + timeline.archived.dynamics;
    set("archive-note", archived ? `更早的 ${archived} 条收在归档里，她记得，只是没摊在桌上` : "");
    paintTimeline(timeline.activity);
  }

  const DOCS = [["USER", "他眼中的我"], ["MEMORY", "事实"], ["RELATIONS", "相处"], ["SOUL", "人格"]];

  function buildDocTabs() {
    const root = $("doc-tabs");
    DOCS.forEach(([key, label], index) => {
      const button = document.createElement("button");
      button.textContent = label;
      button.className = index === 0 ? "on" : "";
      button.addEventListener("click", () => {
        root.querySelectorAll("button").forEach((node) => node.classList.remove("on"));
        button.classList.add("on");
        loadDoc(key);
      });
      root.appendChild(button);
    });
  }

  async function loadDoc(key) {
    try {
      const body = await get(`/api/docs/${key}`);
      $("doc-body").textContent = body.content + (body.truncated ? "\n……（后面还有，面板只摊开这一段）" : "");
    } catch (error) {
      $("doc-body").textContent = "这一页没读到。";
    }
  }

  function markOffline(message) {
    set("quiet", message || "连接断了——她还在，只是这页读不到她。");
    $("quiet").classList.add("err");
  }

  function clearOffline() {
    set("quiet", "");
    $("quiet").classList.remove("err");
  }

  async function refresh() {
    try {
      const [state, timeline] = await Promise.all([get("/api/state"), get("/api/timeline")]);
      paintState(state);
      paintMachine(timeline);
      clearOffline();
    } catch (error) {
      markOffline();
    }
  }

  // ---------------------------------------------------------------- 对话视窗
  function chatOpen() {
    return $("view-chat").classList.contains("on");
  }

  function hint(text) {
    set("thread-hint", text);
  }

  function paintHint() {
    if (busy) hint("（她在听…）");
    else if (shots.length) hint(`这条会带上 ${shots.length} 张图。她一次能看 ${slots} 张。`);
    else hint($("thread").children.length ? "" : "她在听。想说什么就说，也可以贴一张图给她看。");
  }

  function appendBubble(side, text) {
    const row = document.createElement("li");
    row.className = `bubble ${side}`;
    const line = document.createElement("span");
    line.className = "line";
    line.textContent = text || "";
    row.appendChild(line);
    $("thread").appendChild(row);
    return row;
  }

  function pieceOf(frame) {
    for (const raw of frame.split("\n")) {
      const line = raw.trim();
      if (line.slice(0, 5) !== "data:") continue;
      const payload = line.slice(5).trim();
      if (!payload || payload === "[DONE]") return "";
      let parsed;
      try {
        parsed = JSON.parse(payload);
      } catch (error) {
        return "";
      }
      const choices = parsed.choices || [];
      const delta = choices[0] && choices[0].delta;
      const piece = delta && delta.content;
      return typeof piece === "string" ? piece : "";
    }
    return "";
  }

  async function streamInto(payload, host) {
    const response = await post("/v1/chat/completions", payload);
    const line = host.querySelector(".line");
    const caret = document.createElement("span");
    caret.className = "caret";
    caret.textContent = "▍";
    host.appendChild(caret);
    if (!response.body) return line.textContent;
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "", spoken = "";
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let cut = buffer.indexOf("\n\n");
      while (cut >= 0) {
        const piece = pieceOf(buffer.slice(0, cut));
        buffer = buffer.slice(cut + 2);
        if (piece) {
          spoken += piece;
          line.textContent += piece;
        }
        cut = buffer.indexOf("\n\n");
      }
    }
    caret.remove();
    return spoken;
  }

  function payloadOf(text, images) {
    const parts = [];
    if (text) parts.push({ type: "text", text });
    images.forEach((url) => parts.push({ type: "image_url", image_url: { url } }));
    // 只发这一句：上下文由引擎自己管，重启后也从日志接着，历史不该由界面叠第二份
    return { model: "mysoulbot", stream: true, messages: [{ role: "user", content: parts.length === 1 ? text : parts }] };
  }

  function paintPending() {
    const root = $("pending");
    root.replaceChildren(
      ...shots.map((url, index) => {
        const button = document.createElement("button");
        button.title = "不要这张了";
        const image = document.createElement("img");
        image.src = url;
        image.alt = "准备发出的图";
        button.appendChild(image);
        button.addEventListener("click", () => {
          shots.splice(index, 1);
          paintPending();
          paintHint();
        });
        return button;
      })
    );
    paintHint();
  }

  function toDataUrl(file) {
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        URL.revokeObjectURL(url);
        try {
          const scale = Math.min(1, MAX_EDGE / Math.max(image.width || 1, image.height || 1));
          const canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round((image.width || MAX_EDGE) * scale));
          canvas.height = Math.max(1, Math.round((image.height || MAX_EDGE) * scale));
          canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
          // 手机原图动辄三四兆，两张就顶到服务的请求体上限；在这里压掉，别去抬那条线
          resolve(canvas.toDataURL("image/jpeg", JPEG_QUALITY));
        } catch (error) {
          reject(error);
        }
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("这张读不出来"));
      };
      image.src = url;
    });
  }

  async function takeFiles(files) {
    const picked = Array.from(files || []).filter((file) => file && /^image\//.test(file.type || ""));
    if (!picked.length) return;
    for (const file of picked) {
      if (shots.length >= slots) {
        hint(`她一次能看 ${slots} 张，多出来的这张先放下了。`);
        break;
      }
      try {
        shots.push(await toDataUrl(file));
      } catch (error) {
        hint("这张读不出来——换一张，或者直接讲给我听。");
      }
    }
    paintPending();
  }

  function mountVoice(host, spoken) {
    post("/voice/say", { text: spoken })
      .then((response) => response.json())
      .then((result) => {
        if (!result || !result.ok || !result.audio) return;
        const bar = document.createElement("div");
        bar.className = "say";
        const tag = document.createElement("span");
        tag.textContent = result.seconds > 0 ? `${Number(result.seconds).toFixed(1)}″` : "念给你听";
        const audio = document.createElement("audio");
        audio.setAttribute("controls", "");
        audio.setAttribute("preload", "none");
        audio.src = result.audio;
        bar.appendChild(tag);
        bar.appendChild(audio);
        host.appendChild(bar);
      })
      .catch(() => {});  // 没有声音就不挂东西：一句「加载失败」都不该出现在这页上
  }

  async function send() {
    if (busy) return;
    const draft = $("draft");
    const text = (draft.value || "").trim();
    if (!text && !shots.length) return;
    busy = true;
    $("send").disabled = true;
    draft.disabled = true;
    appendBubble("me", text);
    const mine = $("thread").lastElementChild;
    shots.forEach((url) => {
      const image = document.createElement("img");
      image.className = "thumb";
      image.src = url;
      image.alt = "他发来的图";
      mine.appendChild(image);
    });
    const outgoing = shots;
    shots = [];
    paintPending();
    draft.value = "";
    const hers = appendBubble("her", "");
    let spoken = "";
    try {
      spoken = await streamInto(payloadOf(text, outgoing), hers);
      clearOffline();
    } catch (error) {
      const line = hers.querySelector(".line");
      line.textContent = line.textContent || "（这头没接上，她没听见。）";
      markOffline();
    } finally {
      busy = false;
      $("send").disabled = false;
      draft.disabled = false;
      draft.focus();
      paintHint();
      hers.scrollIntoView({ block: "nearest" });
    }
    if (voiceOn && spoken.trim()) mountVoice(hers, spoken);
    refresh();
  }

  function buildChat() {
    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      send();
    });
    const wired = [["pick-file", "file-image"], ["pick-shot", "shot-image"]];
    wired.forEach(([button, input]) => {
      $(button).addEventListener("click", () => $(input).click());
      $(input).addEventListener("change", (event) => {
        takeFiles(event.target.files);
        event.target.value = "";
      });
    });
    ["composer", "thread"].forEach((id) => {
      const node = $(id);
      node.addEventListener("dragover", (event) => {
        event.preventDefault();
        node.classList.add("drop");
      });
      node.addEventListener("dragleave", () => node.classList.remove("drop"));
      node.addEventListener("drop", (event) => {
        event.preventDefault();
        node.classList.remove("drop");
        takeFiles(event.dataTransfer && event.dataTransfer.files);
      });
    });
    document.addEventListener("paste", (event) => {
      const items = event.clipboardData && event.clipboardData.items;
      if (!items || !chatOpen()) return;
      const images = [];
      for (const item of items) {
        if (item.kind === "file" && /^image\//.test(item.type || "")) images.push(item.getAsFile());
      }
      if (images.length) {
        event.preventDefault();
        takeFiles(images);
      }
    });
    paintHint();
  }

  async function probe() {
    try {
      const health = await get("/healthz");
      voiceOn = Boolean(health.tts_enabled) && health.tts_provider !== "none";
      slots = Math.max(1, Number(health.vision_max_images) || 2);
    } catch (error) {
      voiceOn = false;
    }
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((node) => node.classList.remove("on"));
      document.querySelectorAll(".view").forEach((node) => node.classList.remove("on"));
      tab.classList.add("on");
      $(`view-${tab.dataset.view}`).classList.add("on");
      if (tab.dataset.view === "chat") $("draft").focus();
      paintHint();
    });
  });

  buildDocTabs();
  buildChat();
  loadDoc("USER");
  probe();
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
