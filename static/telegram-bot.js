/* Telegram 助手（独立扩展模块）
 *
 * 与主应用的唯一接触面是 window.YingjiExtViews.telegramBot 这一注册点，
 * 不读写主应用的任何状态、不覆盖任何既有样式与函数。
 * 页面被切走后轮询会自行停止（靠 DOM 存在性自检，无需外部通知）。
 *
 * 文案口径：本模块为可选扩展，默认关闭、可随时停用；界面不出现任何
 * 凭据 / 隐私 / 技术栈相关表述，保持中性。
 */
(() => {
  "use strict";

  const API = "/api/telegram-bot";
  const POLL_MS = 2500;
  const MAX_LOGS = 60;

  let root = null;
  let pollTimer = null;
  let statusData = null;
  let tokenVisible = false;
  let busy = false;
  let PROVIDERS = {};
  let lastTransCfg = {};

  const $ = (sel, scope) => (scope || document).querySelector(sel);
  const $$ = (sel, scope) => Array.from((scope || document).querySelectorAll(sel));

  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
  }

  function formatSize(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
    return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  // ---------------------------------------------------------------- 数据

  async function apiGet(path) {
    const res = await fetch(API + path, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function apiPost(path, body) {
    const res = await fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function toast(message, kind) {
    const region = document.getElementById("toastRegion");
    if (!region) return;
    const node = document.createElement("div");
    node.className = `tgb-toast${kind ? " is-" + kind : ""}`;
    node.textContent = message;
    region.appendChild(node);
    setTimeout(() => node.remove(), 3200);
  }

  async function refresh() {
    try {
      const [status, logs, files, providers] = await Promise.all([
        apiGet("/status"),
        apiGet(`/logs?limit=${MAX_LOGS}`),
        apiGet("/downloads"),
        apiGet("/translation/providers").catch(() => ({ providers: {} })),
      ]);
      statusData = status;
      PROVIDERS = (providers && providers.providers) || {};
      paintStatus(status);
      paintLogs(logs.logs || []);
      paintFiles(files.items || []);
    } catch (err) {
      paintError(err);
    }
  }

  // ---------------------------------------------------------------- 绘制

  function paintStatus(data) {
    const cfg = data.config || {};
    const badge = $("#tgbBadge", root);
    if (badge) {
      const state = data.running ? "on" : (data.enabled ? "idle" : "off");
      badge.dataset.state = state;
      badge.textContent = data.running ? "运行中" : (data.enabled ? "已停止" : "未启用");
    }

    setText("#tgbStatAccount", data.username || "—");
    setText("#tgbStatBigFile", data.local_available ? "可用"
      : (data.local_configured ? "未连接" : "未启用"));
    setText("#tgbStatPending", String(data.pending || 0));

    const toggle = $("#tgbToggle", root);
    if (toggle) {
      toggle.dataset.mode = data.running ? "stop" : "start";
      toggle.textContent = data.running ? "停用" : "启用";
      toggle.disabled = busy;
    }

    // 配置回显（避免覆盖用户正在输入的内容）
    const tokenInput = $("#tgbToken", root);
    if (tokenInput && document.activeElement !== tokenInput) {
      tokenInput.value = "";
      tokenInput.placeholder = cfg.token_configured
        ? "已保存，如需更换请重新输入"
        : "在此粘贴机器人令牌";
    }
    const autoStart = $("#tgbAutoStart", root);
    if (autoStart) autoStart.checked = !!cfg.auto_start;
    const proxyInput = $("#tgbProxy", root);
    if (proxyInput && document.activeElement !== proxyInput) proxyInput.value = cfg.proxy || "";
    const localInput = $("#tgbLocalUrl", root);
    if (localInput && document.activeElement !== localInput) localInput.value = cfg.local_api_url || "";

    paintWhitelist(cfg.whitelist || []);

    const dir = $("#tgbDir", root);
    if (dir) dir.textContent = data.downloads_dir || "—";

    paintTranslation(cfg);

    const err = $("#tgbError", root);
    if (err) {
      err.hidden = !data.last_error;
      err.textContent = data.last_error ? `最近一次问题：${data.last_error}` : "";
    }
  }

  function paintWhitelist(list) {
    const wrap = $("#tgbWhitelist", root);
    if (!wrap) return;
    if (!list.length) {
      wrap.innerHTML = '<p class="tgb-empty">未设置。先启用助手并向 Bot 发送 /start 获取 Chat ID，再把编号加入这里；加入前不会创建下载任务。</p>';
      return;
    }
    wrap.innerHTML = list.map((id) => `
      <span class="tgb-chip">
        <b>${esc(id)}</b>
        <button type="button" class="tgb-chip-x" data-act="wl-remove" data-id="${esc(id)}" aria-label="移除">&times;</button>
      </span>`).join("");
  }

  function paintLogs(logs) {
    const wrap = $("#tgbLogs", root);
    if (!wrap) return;
    if (!logs.length) {
      wrap.innerHTML = '<p class="tgb-empty">暂无记录。</p>';
      return;
    }
    wrap.innerHTML = logs.slice().reverse().map((item) => `
      <div class="tgb-log" data-level="${esc(item.level || "info")}">
        <span class="tgb-log-ts">${esc(item.ts || "")}</span>
        <span class="tgb-log-msg">${esc(item.message || "")}</span>
      </div>`).join("");
  }

  function paintFiles(items) {
    const wrap = $("#tgbFiles", root);
    if (!wrap) return;
    if (!items.length) {
      wrap.innerHTML = '<p class="tgb-empty">还没有文件。</p>';
      return;
    }
    wrap.innerHTML = items.map((item) => `
      <div class="tgb-file">
        <span class="tgb-file-name" title="${esc(item.path || "")}">${esc(item.name || "")}</span>
        <span class="tgb-file-size">${formatSize(item.size)}</span>
      </div>`).join("");
  }

  function paintTranslation(cfg) {
    const card = $("#tgbTransCard", root);
    if (!card) return;
    if (card.contains(document.activeElement)) return; // 用户正在编辑则跳过，避免丢输入
    const t = (cfg && cfg.translation) || {};
    lastTransCfg = t;
    const on = $("#tgbTransOn", root);
    if (on) on.checked = !!t.enabled;
    const body = $("#tgbTransBody", root);
    if (body) body.hidden = !t.enabled; // 关闭时只保留标题/开关/说明
    const sel = $("#tgbTransProvider", root);
    if (sel) {
      if (!sel.options.length && PROVIDERS) {
        sel.innerHTML = Object.keys(PROVIDERS).map((k) =>
          `<option value="${esc(k)}">${esc(PROVIDERS[k].label || k)}</option>`).join("");
      }
      if (PROVIDERS && PROVIDERS[t.provider]) sel.value = t.provider || "deepseek";
    }
    const target = $("#tgbTransTarget", root);
    if (target) target.value = t.target_lang || "zh";
    renderTransFields(t.provider || "deepseek", t);
  }

  function renderTransFields(provider, values) {
    const wrap = $("#tgbTransFields", root);
    if (!wrap) return;
    const meta = (PROVIDERS && PROVIDERS[provider]) || {};
    const fields = meta.fields || [];
    values = values || {};
    wrap.innerHTML = fields.map((f) => {
      const saved = f.key === "api_key" && values[f.key + "_configured"] && !values[f.key];
      const val = saved ? "" : (values[f.key] || "");
      const ph = saved ? "已保存，如需更换请重新输入" : "";
      return `
        <div class="tgb-field">
          <label for="tgbTr_${esc(f.key)}">${esc(f.label)}</label>
          <input id="tgbTr_${esc(f.key)}" type="${f.type === "password" ? "password" : "text"}"
                 value="${esc(val)}" placeholder="${esc(ph)}" autocomplete="off" spellcheck="false">
        </div>`;
    }).join("");
  }

  function paintError(err) {
    const badge = $("#tgbBadge", root);
    if (badge) {
      badge.dataset.state = "off";
      badge.textContent = "未启用";
    }
    const wrap = $("#tgbError", root);
    if (wrap) {
      wrap.hidden = false;
      wrap.textContent = `暂时无法读取状态（${esc(err.message || err)}）`;
    }
    stopPolling();
  }

  function setText(sel, text) {
    const node = $(sel, root);
    if (node) node.textContent = text;
  }

  // ---------------------------------------------------------------- 交互

  async function runAction(action) {
    if (busy) return;
    busy = true;
    const toggle = $("#tgbToggle", root);
    if (toggle) toggle.disabled = true;
    try {
      if (action === "toggle") {
        const running = statusData && statusData.running;
        const res = await apiPost(running ? "/stop" : "/start");
        if (!res.ok) toast(res.error || "操作未成功", "warn");
        else toast(res.message || "已更新", "ok");
      } else if (action === "save-basic") {
        const patch = {
          auto_start: $("#tgbAutoStart", root).checked,
          proxy: $("#tgbProxy", root).value.trim(),
          local_api_url: $("#tgbLocalUrl", root).value.trim(),
        };
        const res = await apiPost("/config", patch);
        toast(res.ok ? "设置已保存" : (res.error || "保存失败"), res.ok ? "ok" : "warn");
      } else if (action === "save-token") {
        const value = $("#tgbToken", root).value.trim();
        if (!value) { toast("请先粘贴令牌", "warn"); return; }
        const res = await apiPost("/config", { token: value });
        if (res.ok) {
          $("#tgbToken", root).value = "";
          toast("令牌已保存", "ok");
        } else {
          toast(res.error || "保存失败", "warn");
        }
      } else if (action === "wl-add") {
        const input = $("#tgbWlInput", root);
        const value = (input.value || "").trim();
        if (!value) { toast("请输入使用者编号", "warn"); return; }
        const current = ((statusData && statusData.config && statusData.config.whitelist) || []);
        if (current.includes(value)) { toast("该使用者已在名单中", "warn"); return; }
        const res = await apiPost("/config", { whitelist: current.concat([value]) });
        if (res.ok) { input.value = ""; toast("已添加", "ok"); }
        else toast(res.error || "添加失败", "warn");
      } else if (action === "clear-logs") {
        await apiPost("/logs/clear", {});
        paintLogs([]);
        toast("记录已清空", "ok");
      } else if (action === "trans-save") {
        const on = $("#tgbTransOn", root).checked;
        const provider = $("#tgbTransProvider", root).value;
        const target = ($("#tgbTransTarget", root).value || "").trim() || "zh";
        const fields = ((PROVIDERS && PROVIDERS[provider]) || {}).fields || [];
        const patch = { enabled: on, provider: provider, target_lang: target };
        fields.forEach((f) => {
          const node = $("#tgbTr_" + f.key, root);
          patch[f.key] = node ? (node.value || "").trim() : "";
        });
        const res = await apiPost("/config", { translation: patch });
        toast(res.ok ? "翻译设置已保存" : (res.error || "保存失败"), res.ok ? "ok" : "warn");
      } else if (action === "trans-test") {
        const provider = $("#tgbTransProvider", root).value;
        const target = ($("#tgbTransTarget", root).value || "").trim() || "zh";
        const fields = ((PROVIDERS && PROVIDERS[provider]) || {}).fields || [];
        const payload = { provider: provider, target_lang: target };
        fields.forEach((f) => {
          const node = $("#tgbTr_" + f.key, root);
          payload[f.key] = node ? (node.value || "").trim() : "";
        });
        const btn = $('[data-act="trans-test"]', root);
        if (btn) btn.disabled = true;
        const res = await apiPost("/translation/test", payload);
        if (btn) btn.disabled = false;
        if (res.ok) toast(`测试成功：${res.result}`, "ok");
        else toast(res.error || "测试失败", "warn");
        return;
      }
      await refresh();
    } catch (err) {
      toast(`操作失败：${err.message || err}`, "warn");
    } finally {
      busy = false;
      if (toggle) toggle.disabled = false;
    }
  }

  function onClick(event) {
    const target = event.target.closest("[data-act]");
    if (!target || !root.contains(target)) return;
    const action = target.dataset.act;

    if (action === "toggle-token") {
      tokenVisible = !tokenVisible;
      const input = $("#tgbToken", root);
      if (input) input.type = tokenVisible ? "text" : "password";
      target.textContent = tokenVisible ? "隐藏" : "显示";
      return;
    }
    if (action === "wl-remove") {
      const current = ((statusData && statusData.config && statusData.config.whitelist) || []);
      apiPost("/config", { whitelist: current.filter((x) => x !== target.dataset.id) })
        .then(refresh)
        .catch((err) => toast(`移除失败：${err.message}`, "warn"));
      return;
    }
    runAction(action);
  }

  // ---------------------------------------------------------------- 生命周期

  function bindEvents() {
    if (!root) return;
    root.addEventListener("click", onClick);
    root.addEventListener("change", onChange);
  }

  function onChange(event) {
    if (!root || !root.contains(event.target)) return;
    if (event.target.id === "tgbTransProvider") {
      renderTransFields(event.target.value, {}); // 切换服务时清空凭证字段
      return;
    }
    if (event.target.id === "tgbTransOn") {
      const body = $("#tgbTransBody", root);
      const checked = !!event.target.checked;
      if (body) {
        body.hidden = !checked;
        if (checked) renderTransFields(lastTransCfg.provider || "deepseek", lastTransCfg);
      }
      return;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
      if (document.hidden) return;
      if (!root || !root.isConnected || !$("#tgbBadge", root)) {
        stopPolling();
        return;
      }
      refresh().catch(() => { /* 单次失败不打断轮询 */ });
    }, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function render() {
    return `
      <div class="view tgb-view">
        <header class="tgb-head">
          <div class="tgb-head-text">
            <h1>Telegram 助手</h1>
            <p>把链接发给你的 Telegram 机器人，下载完成后自动把文件回传给你。此功能为可选扩展，默认关闭，可以随时停用。</p>
          </div>
          <span class="tgb-badge" id="tgbBadge" data-state="off">未启用</span>
        </header>

        <p class="tgb-error" id="tgbError" hidden></p>

        <section class="tgb-stats" aria-label="运行状态">
          <div class="tgb-stat">
            <span>机器人账号</span>
            <b id="tgbStatAccount">—</b>
          </div>
          <div class="tgb-stat">
            <span>大文件直传</span>
            <b id="tgbStatBigFile">—</b>
          </div>
          <div class="tgb-stat">
            <span>进行中的任务</span>
            <b id="tgbStatPending">0</b>
          </div>
          <div class="tgb-stat">
            <span>文件保存位置</span>
            <b id="tgbDir" class="tgb-path">—</b>
          </div>
        </section>

        <section class="tgb-card">
          <div class="tgb-card-head">
            <h2>连接设置</h2>
            <div class="tgb-actions">
              <button type="button" class="tgb-btn ghost" data-act="toggle-token">显示</button>
              <button type="button" class="tgb-btn primary" id="tgbToggle" data-act="toggle" data-mode="start">启用</button>
            </div>
          </div>

          <div class="tgb-field">
            <label for="tgbToken">机器人令牌</label>
            <div class="tgb-row">
              <input id="tgbToken" type="password" placeholder="在此粘贴机器人令牌" autocomplete="off" spellcheck="false">
              <button type="button" class="tgb-btn" data-act="save-token">保存</button>
            </div>
            <small>令牌只保存在本机，界面中始终以脱敏形式显示。</small>
          </div>

          <label class="tgb-switch">
            <input type="checkbox" id="tgbAutoStart">
            <span>启动影迹时自动运行</span>
          </label>

          <div class="tgb-field">
            <label for="tgbProxy">网络代理（可选）</label>
            <input id="tgbProxy" type="text" placeholder="如 http://127.0.0.1:7890" autocomplete="off" spellcheck="false">
          </div>

          <div class="tgb-field">
            <label for="tgbLocalUrl">本地加速服务地址（可选）</label>
            <input id="tgbLocalUrl" type="text" placeholder="如 http://127.0.0.1:8081" autocomplete="off" spellcheck="false">
            <small>填写后，较大的文件也可以直接回传；未填写时，较大的文件会保存在本机并告知你位置。</small>
          </div>

          <div class="tgb-actions end">
            <button type="button" class="tgb-btn" data-act="save-basic">保存设置</button>
          </div>
        </section>

        <section class="tgb-card" id="tgbTransCard">
          <div class="tgb-card-head">
            <h2>回传翻译</h2>
            <label class="tgb-switch">
              <input type="checkbox" id="tgbTransOn">
              <span class="tgb-switch-track"></span>
              <span class="tgb-switch-label">启用</span>
            </label>
          </div>
          <p class="tgb-hint">开启后回传的视频会附带译后的描述。先选择翻译服务，再填写对应凭证即可。</p>
          <div id="tgbTransBody" hidden>
            <div class="tgb-field">
              <label for="tgbTransProvider">翻译服务</label>
              <select id="tgbTransProvider"></select>
            </div>
            <div id="tgbTransFields"></div>
            <div class="tgb-field">
              <label for="tgbTransTarget">目标语言</label>
              <input id="tgbTransTarget" type="text" value="zh" autocomplete="off" spellcheck="false">
            </div>
            <div class="tgb-actions end">
              <button type="button" class="tgb-btn ghost" data-act="trans-test">测试</button>
              <button type="button" class="tgb-btn" data-act="trans-save">保存翻译设置</button>
            </div>
          </div>
        </section>

        <section class="tgb-card">
          <div class="tgb-card-head">
            <h2>使用权限</h2>
          </div>
          <div class="tgb-whitelist" id="tgbWhitelist"></div>
          <div class="tgb-row">
            <input id="tgbWlInput" type="text" placeholder="输入使用者编号后添加" autocomplete="off" spellcheck="false">
            <button type="button" class="tgb-btn" data-act="wl-add">添加</button>
          </div>
          <small>留空时，第一个与机器人对话的人会被自动记录并获得使用权限。</small>
        </section>

        <section class="tgb-card">
          <div class="tgb-card-head">
            <h2>运行记录</h2>
            <button type="button" class="tgb-btn ghost" data-act="clear-logs">清空</button>
          </div>
          <div class="tgb-logs" id="tgbLogs"></div>
        </section>

        <section class="tgb-card">
          <div class="tgb-card-head">
            <h2>已下载</h2>
          </div>
          <div class="tgb-files" id="tgbFiles"></div>
        </section>
      </div>`;
  }

  function mount(el) {
    root = el;
    $$("input, button", root).forEach((node) => { node.disabled = false; });
    bindEvents();
    refresh().catch(() => { /* 首次刷新失败不阻断渲染 */ });
    startPolling();
  }

  function unmount() {
    stopPolling();
    root = null;
  }

  window.YingjiExtViews = window.YingjiExtViews || {};
  window.YingjiExtViews.telegramBot = { render, mount, unmount };
})();
