/* 面板前端：只做两件事——读状态、把状态摊得好看。
 *
 * 没有写入路径。熟络度在这里是一根不能拖的条：温度只能由相处攒出来，
 * 后端本来也不接受任何修改，这里连一个 PATCH 按钮都不长。
 *
 * 所有来自记忆与文档的文字一律走 textContent，绝不拼进 HTML——
 * 她记得的那句话里万一夹着尖括号，也不该把这页变成别人的脚本。
 */
(() => {
  "use strict";

  const REFRESH_MS = 20000;
  const params = new URLSearchParams(location.search);
  const user = params.get("user") || "";
  const query = user ? `?user=${encodeURIComponent(user)}` : "";

  const $ = (id) => document.getElementById(id);
  const set = (id, text) => { const el = $(id); if (el) el.textContent = text || ""; };

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

  async function get(path) {
    const response = await fetch(path + query, { headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    return response.json();
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

  async function refresh() {
    try {
      const [state, timeline] = await Promise.all([get("/api/state"), get("/api/timeline")]);
      paintState(state);
      paintMachine(timeline);
      set("quiet", "");
      $("quiet").classList.remove("err");
    } catch (error) {
      markOffline();
    }
  }

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((node) => node.classList.remove("on"));
      document.querySelectorAll(".view").forEach((node) => node.classList.remove("on"));
      tab.classList.add("on");
      $(`view-${tab.dataset.view}`).classList.add("on");
    });
  });

  buildDocTabs();
  loadDoc("USER");
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
