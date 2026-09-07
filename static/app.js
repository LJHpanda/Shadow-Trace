(() => {
  "use strict";

  const API = "/api";
  const statusLabels = {
    downloading: "进行中", complete: "已完成", error: "失败",
    cancelled: "已取消", incomplete: "残留待确认", queued: "排队中",
    paused: "待开始", pending: "待开始", verifying: "正在检查文件",
  };
  // UI-P0-03: 完成依据（仅 complete 任务有值）
  const basisLabels = {
    verified: "检查通过", user_confirmed: "人工确认", legacy_unknown: "历史记录",
  };
  function basisBadgeHtml(task) {
    const b = task.completion_basis;
    if (!b || !basisLabels[b]) return "";
    const tips = {
      verified: "文件已通过自动检查（可读且信息完整）",
      user_confirmed: "自动检查未通过或未完成，由你人工确认保留。",
      legacy_unknown: "早期版本的下载记录，当时未保存检查数据",
    };
    return `<span class="basis-badge basis-${esc(b)}" data-tip="${esc(tips[b])}">${esc(basisLabels[b])}</span>`;
  }

  // 平台风控徽章：error_code 为 RISK_CONTROLLED 时高亮提醒
  // （风控 ≠ 登录缺失，换 Cookie 通常无效，需暂停/降频/换网络等待冷却）
  function riskBadgeHtml(task) {
    if (!task || task.error_code !== "RISK_CONTROLLED") return "";
    return `<span class="risk-badge" data-tip="平台风控：当前账号或网络被限流 / 机器人验证 / 账号或访问受限 / 验证码。换 Cookie 通常无法解除，需暂停、降频或换网络等待冷却。">平台风控</span>`;
  }
  const state = {
    view: "download",
    extMounted: "",        // 当前挂载的外部功能模块（如格式转换），内置视图恒为空
    tasks: [],
    groups: [],
    stats: null,
    batchEntries: [],
    batchTitle: "",
    batchStep: 1,
    batchMode: "flow",
    workbenchRows: [],
    workbenchDedup: true,
    workbenchGroupId: "",
    detectRecords: [],
    recordsLoaded: false,
    recordsSearch: "",
    taskSearch: "",
    taskStatus: "all",
    batches: [],
    viewMode: localStorage.getItem("yingji_view_mode") === "card" ? "card" : "compact",
    expandedTasks: new Set(),
    expandedUrls: new Set(),
    collapsedGroups: new Set(),
    selectedBatch: "",
    selectedTaskIds: new Set(),
    showArchived: false,
    polling: localStorage.getItem("yingji_alt_polling") !== "off",
    theme: localStorage.getItem("yingji_alt_theme") === "light" ? "light" : "dark",
    timer: null,
    busy: false,
    // UI-P0-04：设置中心
    settingsSection: "general",
    system: null,          // 系统只读状态快照（版本、凭据状态、默认目录等）
    systemLoadFailed: false,
    // R-1：数据中心统计
    stats: null,
    statsLoadFailed: false,
    // UI-P0-05：首次启动引导 + 异常恢复能力检查
    capability: null,      // 来自 /api/startup-status 的只读能力快照
    onboardingStep: 0,     // 0 = 未打开；1/2/3 = 引导步骤
    dismissedAnomalies: new Set(),  // 当前会话内已忽略的提示型异常（如未加载 Cookie）
  };

  // P0-1：画质 / 音轨结构化选择状态（由 /api/formats 的 video/audio/combined 填充）
  let formatData = null;
  let directoryPickerBusy = false;
  // 分组「...」菜单 / 批量移动菜单 的浮动弹层引用
  let activePop = null;

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[ch]));
  const fmtBytes = (bytes) => {
    const n = Number(bytes) || 0;
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
    return `${(n / 1024 ** i).toFixed(i > 2 ? 2 : i ? 1 : 0)} ${units[i]}`;
  };
  const fmtDuration = (seconds) => {
    const n = Number(String(seconds ?? "").replace(/s$/i, "")) || 0;
    if (!n) return "时长未知";
    const m = Math.floor(n / 60);
    return `${m}:${String(Math.floor(n % 60)).padStart(2, "0")}`;
  };
  const fmtElapsed = (seconds) => {
    const n = Math.round(Number(seconds) || 0);
    if (n <= 0) return "";
    const m = Math.floor(n / 60);
    const s = n % 60;
    if (m > 0 && s > 0) return `${m}分${s}秒`;
    if (m > 0) return `${m}分钟`;
    return `${s}秒`;
  };
  // 时长（紧凑/详情中文格式）：X分Y秒 / X分钟 / Y秒
  const fmtDurationCN = (seconds) => {
    const n = Number(String(seconds ?? "").replace(/s$/i, "")) || 0;
    if (n <= 0) return "";
    const m = Math.floor(n / 60);
    const s = Math.floor(n % 60);
    if (m > 0 && s > 0) return `${m}分${s}秒`;
    if (m > 0) return `${m}分钟`;
    return `${s}秒`;
  };
  const fmtDateTime = (value) => {
    if (!value) return "时间未知";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 16);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  };

  async function api(path, options = {}) {
    const opts = { ...options, headers: { ...(options.headers || {}) } };
    if (options.body && typeof options.body !== "string" && !(options.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(options.body);
    }
    const response = await fetch(API + path, opts);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const err = new Error(data.error || `请求失败（${response.status}）`);
      err.status = response.status;
      err.code = data.code || "";
      err.method = opts.method || "GET";
      err.path = path;
      throw err;
    }
    return data;
  }

  function toast(message, tone = "") {
    const el = document.createElement("div");
    el.className = `toast ${tone}`;
    el.textContent = message;
    $("#toastRegion").appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }

  function confirmAction(title, message, okText = "确定") {
    return new Promise((resolve) => {
      const root = $("#modalRoot");
      root.innerHTML = `<div class="modal-backdrop">
        <section class="modal" role="dialog" aria-modal="true" aria-labelledby="confirmTitle">
          <h2 id="confirmTitle">${esc(title)}</h2><p>${esc(message)}</p>
          <div class="modal-actions">
            <button class="button ghost" data-result="false">取消</button>
            <button class="button danger" data-result="true">${esc(okText)}</button>
          </div>
        </section></div>`;
      const finish = (result) => { root.innerHTML = ""; resolve(result); };
      root.onclick = (event) => {
        const value = event.target.dataset.result;
        if (value) finish(value === "true");
        else if (event.target.classList.contains("modal-backdrop")) finish(false);
      };
      root.onkeydown = (event) => event.key === "Escape" && finish(false);
      $('[data-result="false"]', root).focus();
    });
  }

  // 文本输入弹窗（重命名等），返回输入字符串或 null（取消）
  function promptText(title, message, defaultValue = "") {
    return new Promise((resolve) => {
      const root = $("#modalRoot");
      root.innerHTML = `<div class="modal-backdrop">
        <section class="modal" role="dialog" aria-modal="true" aria-labelledby="promptTitle">
          <h2 id="promptTitle">${esc(title)}</h2><p>${esc(message)}</p>
          <input class="input" id="promptInput" value="${esc(defaultValue)}" placeholder="请输入">
          <div class="modal-actions">
            <button class="button ghost" data-result="cancel">取消</button>
            <button class="button primary" data-result="ok">确定</button>
          </div>
        </section></div>`;
      const input = $("#promptInput", root);
      const finish = (value) => { root.innerHTML = ""; resolve(value); };
      root.onclick = (event) => {
        const r = event.target.dataset.result;
        if (r === "cancel") return finish(null);
        if (r === "ok") return finish(input.value.trim());
        if (event.target.classList.contains("modal-backdrop")) finish(null);
      };
      root.onkeydown = (event) => {
        if (event.key === "Escape") finish(null);
        else if (event.key === "Enter") finish(input.value.trim());
      };
      input.focus();
      input.select();
    });
  }

  function chooseDeleteMode(task) {
    return new Promise((resolve) => {
      const root = $("#modalRoot");
      const name = task?.display_name || task?.output_name || "当前任务";
      root.innerHTML = `<div class="modal-backdrop">
        <section class="modal" role="dialog" aria-modal="true" aria-labelledby="deleteTitle">
          <h2 id="deleteTitle">删除任务</h2>
          <p>请选择“${esc(name)}”的删除方式。删除本地文件时会优先移入系统回收站。</p>
          <div class="modal-actions">
            <button class="button ghost" data-delete-mode="cancel">取消</button>
            <button class="button" data-delete-mode="record">仅删除记录</button>
            <button class="button danger" data-delete-mode="files">删除记录和文件</button>
          </div>
        </section></div>`;
      const finish = (result) => { root.innerHTML = ""; resolve(result); };
      root.onclick = (event) => {
        const mode = event.target.dataset.deleteMode;
        if (mode) finish(mode);
        else if (event.target.classList.contains("modal-backdrop")) finish("cancel");
      };
      root.onkeydown = (event) => event.key === "Escape" && finish("cancel");
      $('[data-delete-mode="cancel"]', root).focus();
    });
  }

  function chooseDeleteGroupMode(g) {
    return new Promise((resolve) => {
      const root = $("#modalRoot");
      const name = g?.name || "该分组";
      root.innerHTML = `<div class="modal-backdrop">
        <section class="modal" role="dialog" aria-modal="true" aria-labelledby="deleteGroupTitle">
          <h2 id="deleteGroupTitle">删除分组</h2>
          <p>删除“${esc(name)}”后，其下任务如何处理？删除本地文件时会优先移入系统回收站。</p>
          <div class="modal-actions">
            <button class="button ghost" data-delete-group-mode="cancel">取消</button>
            <button class="button" data-delete-group-mode="keep">保留任务并上移</button>
            <button class="button" data-delete-group-mode="tasks">删除任务记录</button>
            <button class="button danger" data-delete-group-mode="files">删除任务及文件</button>
          </div>
        </section></div>`;
      const finish = (result) => { root.innerHTML = ""; resolve(result); };
      root.onclick = (event) => {
        const mode = event.target.dataset.deleteGroupMode;
        if (mode) finish(mode);
        else if (event.target.classList.contains("modal-backdrop")) finish("cancel");
      };
      root.onkeydown = (event) => event.key === "Escape" && finish("cancel");
      $('[data-delete-group-mode="cancel"]', root).focus();
    });
  }

  function chooseConfirmResult(task) {
    return new Promise((resolve) => {
      const root = $("#modalRoot");
      const name = task?.display_name || task?.output_name || "当前任务";
      root.innerHTML = `<div class="modal-backdrop">
        <section class="modal" role="dialog" aria-modal="true" aria-labelledby="confirmTitle">
          <h2 id="confirmTitle">确认任务结果</h2>
          <p>“${esc(name)}”的文件未通过自动检查或检查未完成。请自行播放确认文件是否可用：</p>
          <p class="modal-note">选择「确认文件可用」后，该任务会记录为「人工确认保留」，与自动检查通过的任务分开统计。</p>
          <div class="modal-actions">
            <button class="button ghost" data-confirm-result="cancel">取消</button>
            <button class="button danger" data-confirm-result="failed">文件不可用，标记失败</button>
            <button class="button" data-confirm-result="complete">确认文件可用，保留</button>
          </div>
        </section></div>`;
      const finish = (result) => { root.innerHTML = ""; resolve(result); };
      root.onclick = (event) => {
        const r = event.target.dataset.confirmResult;
        if (r) finish(r);
        else if (event.target.classList.contains("modal-backdrop")) finish("cancel");
      };
      root.onkeydown = (event) => event.key === "Escape" && finish("cancel");
      $('[data-confirm-result="cancel"]', root).focus();
    });
  }

  function applyTheme() {
    document.documentElement.dataset.theme = state.theme;
    const meta = $('meta[name="color-scheme"]');
    if (meta) meta.content = state.theme;
  }

  async function loadTasks(render = true) {
    try {
      state.tasks = await api("/tasks");
      computeBatches();
      const active = state.tasks.filter((t) => ["downloading", "queued"].includes(t.status));
      $("#navTaskCount").textContent = active.length;
      $("#navTaskCount").style.display = active.length ? "grid" : "none";
      if (render && state.view === "tasks") refreshTaskLiveRegions();
      else if (render && state.view === "download") refreshDownloadLiveRegions();
    } catch (error) {
      if (render) toast("暂时无法读取任务，请稍后重试", "error");
    }
  }

  function refreshDownloadLiveRegions() {
    const active = state.tasks.filter((t) => ["downloading", "queued"].includes(t.status));
    const recent = state.tasks.slice()
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
      .slice(0, 3);
    const count = $("#activeTaskCount");
    const speed = $("#activeTaskSpeed");
    const total = $("#recentTaskTotal");
    const list = $("#recentTaskList");
    if (count) count.textContent = `${active.length} 个任务进行中`;
    if (speed) speed.textContent = active[0]?.speed || "当前无活动速度";
    if (total) total.textContent = `查看全部 ${state.tasks.length} 个任务 →`;
    if (list) {
      list.innerHTML = recent.length
        ? recent.map((task) => taskRow(task, false, false)).join("")
        : '<div class="empty"><span>↓</span>还没有任务，粘贴一个链接开始使用</div>';
    }
  }

  function filteredTasks() {
    let list = state.tasks.slice();
    if (state.taskSearch) {
      const q = state.taskSearch.toLowerCase();
      list = list.filter((task) =>
        `${task.display_name || ""} ${task.output_name || ""} ${task.url || ""}`
          .toLowerCase()
          .includes(q),
      );
    }
    if (state.taskStatus !== "all") {
      list = list.filter((task) =>
        state.taskStatus === "failed"
          ? ["error", "cancelled", "incomplete"].includes(task.status)
          : state.taskStatus === "pending"
            ? ["paused", "pending", "queued"].includes(task.status)
            : task.status === state.taskStatus,
      );
    }
    if (state.selectedBatch) {
      list = list.filter((task) => String(task.batch_id) === state.selectedBatch);
    }
    if (!state.showArchived) {
      const archivedSet = new Set((state.groups || []).filter((g) => g.archived).map((g) => g.id));
      if (archivedSet.size) list = list.filter((task) => !archivedSet.has(task.group_id));
    }
    return list.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  }

  // 计算批次清单（仅含带 batch_id 的任务），按最新创建时间倒序，用于筛选器与标签
  function computeBatches() {
    const map = new Map();
    for (const t of state.tasks) {
      if (!t.batch_id) continue;
      const bid = String(t.batch_id);
      if (!map.has(bid)) map.set(bid, { id: bid, count: 0, lastAt: "" });
      const e = map.get(bid);
      e.count += 1;
      if (String(t.created_at || "") > e.lastAt) e.lastAt = String(t.created_at || "");
    }
    state.batches = [...map.values()].sort((a, b) => String(b.lastAt).localeCompare(String(a.lastAt)));
  }

  // 批次标签：批次 N（N 为按时间倒序的序号），找不到时回退为原始 id
  function batchLabel(bid) {
    const id = bid != null ? String(bid) : "";
    if (!id) return "";
    const arr = state.batches || [];
    const idx = arr.findIndex((b) => b.id === id);
    return idx >= 0 ? `批次 ${idx + 1}` : `批次 ${id}`;
  }

  // 任务行内的批次标签（仅当任务属于某批次时渲染）
  function batchBadgeHtml(task) {
    const id = task.batch_id != null ? String(task.batch_id) : "";
    if (!id) return "";
    return `<span class="tr-batch" title="在顶部「批次」筛选器中可只看该批次">${esc(batchLabel(id))}</span>`;
  }

  // 批次筛选下拉选项（含「全部批次」）
  function batchOptionsHtml() {
    computeBatches();
    return (state.batches || []).map((b) =>
      `<option value="${esc(b.id)}" ${state.selectedBatch === b.id ? "selected" : ""}>${esc(batchLabel(b.id))}</option>`
    ).join("");
  }

  function refreshTaskLiveRegions() {
    computeBatches();
    const list = filteredTasks();
    const summary = $("#taskSummary");
    const taskList = $("#tasksList");
    if (summary) {
      const prefix = state.selectedBatch ? `${batchLabel(state.selectedBatch)} · ` : "";
      summary.textContent = `${prefix}${state.tasks.length} 条记录 · 当前显示 ${list.length} 条`;
    }
    const batchSel = $("#taskBatch");
    if (batchSel) {
      batchSel.innerHTML = `<option value="">全部批次</option>${batchOptionsHtml()}`;
      batchSel.value = state.selectedBatch;
    }
    if (taskList) {
      taskList.innerHTML = tasksBodyHtml();
      $$("[data-view-mode]").forEach((b) => b.classList.toggle("active", b.dataset.viewMode === state.viewMode));
    }
    updateBatchBar();
  }

  /* ====== 分组操作 + 任务批量处理（2026-07-28） ====== */
  function closePop() { if (activePop) { activePop.remove(); activePop = null; } }

  function positionPop(pop, anchor) {
    const r = anchor.getBoundingClientRect();
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = Math.min(r.right - pw, window.innerWidth - 8);
    if (left < 8) left = 8;
    let top = r.bottom + 6;
    if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  async function renameGroup(gid) {
    const g = (state.groups || []).find((x) => x.id === gid);
    const name = await promptText("重命名分组", "输入新的分组名称", g?.name || "");
    if (name == null || !name) return;
    try {
      await api(`/groups/${encodeURIComponent(gid)}`, { method: "PATCH", body: { name } });
      await loadGroups();
      toast("分组已重命名", "success");
      refreshTaskLiveRegions();
    } catch { toast("重命名未完成，请重试", "error"); }
  }

  async function archiveGroup(gid, archived = true) {
    try {
      await api(`/groups/${encodeURIComponent(gid)}/archive`, { method: "POST", body: { archived } });
      await loadGroups();
      toast(archived ? "分组已归档" : "分组已取消归档", "success");
      refreshTaskLiveRegions();
    } catch { toast(archived ? "归档未完成，请重试" : "取消归档未完成，请重试", "error"); }
  }

  async function deleteGroup(gid) {
    const g = (state.groups || []).find((x) => x.id === gid);
    const mode = await chooseDeleteGroupMode(g);
    if (mode === "cancel") return;
    const params = new URLSearchParams();
    if (mode === "tasks") params.set("delete_tasks", "true");
    if (mode === "files") {
      params.set("delete_tasks", "true");
      params.set("delete_files", "true");
    }
    try {
      const query = params.toString() ? `?${params.toString()}` : "";
      const data = await api(`/groups/${encodeURIComponent(gid)}${query}`, { method: "DELETE" });
      await loadGroups();
      await loadTasks(true);
      if (data.partial) {
        toast(`分组已删除，${data.partial_failures?.length || 0} 个任务的文件未能移入回收站`, "error");
      } else if ((mode === "tasks" || mode === "files") && typeof data.removed_tasks !== "number") {
        toast("分组已删除，但任务未被移除，请重启服务后重试", "error");
      } else if (mode === "keep") {
        toast("分组已删除，任务已保留并上移", "success");
      } else if (mode === "tasks") {
        toast(`分组及 ${data.removed_tasks} 个任务记录已删除`, "success");
      } else if (mode === "files") {
        toast(`分组及 ${data.removed_tasks} 个任务与本地文件已处理`, "success");
      }
      refreshTaskLiveRegions();
    } catch (err) { toast(err?.message || "删除未完成，请重试", "error"); }
  }

  function groupTaskIds(gid) {
    const groups = state.groups || [];
    const children = groups.filter((g) => g.parent_id === gid).map((g) => g.id);
    let ids = state.tasks.filter((t) => (t.group_id || "") === gid).map((t) => t.id);
    for (const c of children) ids = ids.concat(groupTaskIds(c));
    return ids;
  }

  async function moveGroupTasks(gid, targetGid) {
    const ids = groupTaskIds(gid);
    if (!ids.length) return toast("该分组下没有可移动的任务", "error");
    try {
      await api("/tasks/batch", { method: "POST", body: { ids, action: "move", target_group_id: targetGid } });
      await loadTasks(true);
      toast(`已将 ${ids.length} 个任务移动到目标分组`, "success");
    } catch { toast("移动未完成，请重试", "error"); }
  }

  function openGroupMenu(anchor, gid) {
    closePop();
    const pop = document.createElement("div");
    pop.className = "pop-menu";
    pop.dataset.forGroup = gid;
    const g = (state.groups || []).find((x) => x.id === gid);
    const isArchived = !!g?.archived;
    // 通用：新建子分组 / 导出本组 / 删除分组
    // 状态相关：未归档 → 重命名 + 归档分组；已归档 → 取消归档（无重命名）
    const topRow = isArchived
      ? '<button type="button" class="pop-item" data-gm="unarchive">取消归档</button>'
      : '<button type="button" class="pop-item" data-gm="rename">重命名</button><button type="button" class="pop-item" data-gm="archive">归档分组</button>';
    pop.innerHTML = `
      ${topRow}
      <div class="pop-sep"></div>
      <button type="button" class="pop-item" data-gm="new-child">新建子分组</button>
      <button type="button" class="pop-item" data-gm="export">导出本组</button>
      <div class="pop-sep"></div>
      <button type="button" class="pop-item danger" data-gm="delete">删除分组</button>`;
    document.body.appendChild(pop);
    positionPop(pop, anchor);
    activePop = pop;
    pop.addEventListener("click", (e) => {
      if (e.target.closest('[data-gm="rename"]')) { closePop(); void renameGroup(gid); }
      else if (e.target.closest('[data-gm="archive"]')) { closePop(); void archiveGroup(gid, true); }
      else if (e.target.closest('[data-gm="unarchive"]')) { closePop(); void archiveGroup(gid, false); }
      else if (e.target.closest('[data-gm="new-child"]')) { closePop(); void newGroup(gid); }
      else if (e.target.closest('[data-gm="export"]')) { closePop(); exportExcel(gid); }
      else if (e.target.closest('[data-gm="delete"]')) { closePop(); void deleteGroup(gid); }
    });
  }

  /* 任务级：移动到分组（在任务详情 ⋯ 操作区点击后弹出） */
  function openTaskMoveMenu(anchor, taskId) {
    closePop();
    const task = (state.tasks || []).find((x) => x.id === taskId);
    if (!task) return;
    const currentGid = task.group_id || "";
    // 只列出未归档的分组作为可移动目标；"未分组"始终是一项
    const groups = (state.groups || []).filter((g) => !g.archived);
    const current = groups.find((g) => g.id === currentGid);
    const others = groups.filter((g) => g.id !== currentGid);
    const itemBtn = (gid, label, currentFlag) => {
      const cls = currentFlag ? "pop-item is-current" : "pop-item";
      const dis = currentFlag ? "disabled" : "";
      const suffix = currentFlag ? "（当前）" : "";
      return `<button type="button" class="${cls}" ${dis} data-tm-move="${esc(gid)}">${esc(label)}${suffix}</button>`;
    };
    const pop = document.createElement("div");
    pop.className = "pop-menu";
    pop.dataset.forTaskMove = taskId;
    pop.innerHTML = `
      <div class="pop-label">移动到分组</div>
      ${itemBtn("", "未分组", currentGid === "")}
      ${others.map((g) => itemBtn(g.id, g.name, g.id === currentGid)).join("")}
      ${!others.length && currentGid !== "" ? '<span class="pop-empty">没有其他可移动到的分组</span>' : ""}`;
    document.body.appendChild(pop);
    positionPop(pop, anchor);
    activePop = pop;
    pop.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-tm-move]");
      if (!btn || btn.disabled) return;
      closePop();
      void moveTaskToGroup(taskId, btn.dataset.tmMove);
    });
  }

  async function moveTaskToGroup(taskId, targetGid) {
    const targetName = targetGid ? (state.groups || []).find((g) => g.id === targetGid)?.name || "目标分组" : "未分组";
    try {
      await api("/tasks/batch", { method: "POST", body: { ids: [taskId], action: "move", target_group_id: targetGid } });
      await loadTasks(true);
      toast(`已移动到“${targetName}”`, "success");
    } catch { toast("移动未完成，请重试", "error"); }
  }

  /* ====== 任务列表头部操作：归档视图 / 新建分组 / 导出 Excel / 刷新（2026-07-29） ====== */
  async function newGroup(parentId = "") {
    const name = await promptText("新建分组", "输入分组名称", "");
    if (name == null || !name.trim()) return;
    try {
      await api("/groups", { method: "POST", body: { name: name.trim(), parent_id: parentId } });
      await loadGroups();
      toast("分组已新建", "success");
      refreshTaskLiveRegions();
    } catch { toast("新建分组未完成，请重试", "error"); }
  }

  function exportExcel(groupId = "") {
    const qs = new URLSearchParams();
    if (groupId) qs.set("group_id", groupId);
    else if (state.taskStatus && state.taskStatus !== "all") qs.set("status", state.taskStatus);
    const url = "/api/export/excel" + (qs.toString() ? "?" + qs.toString() : "");
    const a = document.createElement("a");
    a.href = url;
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast(groupId ? "本组 Excel 已开始下载" : "Excel 已开始下载", "success");
  }

  function toggleShowArchived() {
    state.showArchived = !state.showArchived;
    refreshTaskLiveRegions();
  }

  function refreshTasks() {
    void loadTasks(true);
    toast("任务列表已刷新", "success");
  }

  function openMoveTasksMenu(anchor, ids) {
    closePop();
    const pop = document.createElement("div");
    pop.className = "pop-menu";
    const groups = (state.groups || []).filter((g) => !g.archived);
    const optsHtml = groups.length
      ? groups.map((g) => `<button type="button" class="pop-item" data-move="${esc(g.id)}">${esc(g.name)}</button>`).join("")
      : '<span class="pop-empty">暂无分组</span>';
    pop.innerHTML = `<div class="pop-label">移动到分组</div>${optsHtml}<button type="button" class="pop-item" data-move="">未分组</button>`;
    document.body.appendChild(pop);
    positionPop(pop, anchor);
    activePop = pop;
    pop.addEventListener("click", (e) => {
      const mv = e.target.closest("[data-move]");
      if (mv) { closePop(); void batchMove(ids, mv.dataset.move); }
    });
  }

  async function batchOp(action, ids, opts = {}) {
    return api("/tasks/batch", { method: "POST", body: { ids, action, ...opts } });
  }
  async function batchRetry(ids) {
    try { await batchOp("retry", ids); await loadTasks(true); toast(`已提交 ${ids.length} 个任务的重试`, "success"); }
    catch { toast("重试未完成，请稍后重试", "error"); }
  }
  async function batchStart(ids) {
    try {
      const r = await batchOp("start", ids);
      await loadTasks(true);
      toast(`已开始 ${r.ok || ids.length} 个任务`, "success");
    } catch { toast("开始未完成，请稍后重试", "error"); }
  }
  async function batchDelete(ids) {
    if (!(await confirmAction("删除所选任务", `删除 ${ids.length} 个任务？本地文件会一并移入回收站（可在回收站找回）。`, "删除任务"))) return;
    try {
      await batchOp("delete", ids, { delete_files: true });
      state.selectedTaskIds.clear();
      await loadTasks(true);
      toast("所选任务已删除", "success");
    } catch { toast("删除未完成，请稍后重试", "error"); }
  }
  async function batchMove(ids, targetGid) {
    try {
      await batchOp("move", ids, { target_group_id: targetGid });
      state.selectedTaskIds.clear();
      await loadTasks(true);
      toast(`已移动 ${ids.length} 个任务`, "success");
    } catch { toast("移动未完成，请稍后重试", "error"); }
  }
  function selectAllVisible() {
    for (const t of filteredTasks()) state.selectedTaskIds.add(t.id);
    refreshTaskLiveRegions();
  }
  function updateBatchBar() {
    const bar = $("#batchBar");
    if (!bar) return;
    const ids = [...state.selectedTaskIds];
    if (!ids.length) { bar.hidden = true; bar.innerHTML = ""; return; }
    bar.hidden = false;
    bar.innerHTML = `
      <div class="bb-info">已选 <b>${ids.length}</b> 项</div>
      <div class="bb-actions">
        <button type="button" class="button ghost" data-batch-action="select-all">选择全部</button>
        <button type="button" class="button ghost" data-batch-action="clear">取消选择</button>
        <button type="button" class="button" data-batch-action="move">移动到分组</button>
        <button type="button" class="button" data-batch-action="start">开始所选</button>
        <button type="button" class="button" data-batch-action="retry">重试所选</button>
        <button type="button" class="button danger" data-batch-action="delete">删除所选</button>
      </div>`;
  }

  async function loadGroups() {
    try { state.groups = (await api("/groups")).groups || []; } catch { state.groups = []; }
  }
  async function loadStats(render = true) {
    try {
      state.stats = await api("/stats");
      state.statsLoadFailed = false;
      if (render && state.view === "data") renderView(false);
    } catch (error) {
      // R-1：接口失败时清空旧成功结果，不得冒充最新状态
      state.stats = null;
      state.statsLoadFailed = true;
      if (render) toast("暂时无法读取使用数据", "error");
      if (render && state.view === "data") renderView(false);
    }
  }

  // UI-P0-04：读取系统只读状态（版本、登录凭据、默认保存位置、并发默认值）。
  // 仅展示，不在设置页修改任何后端行为。
  async function loadSystemStatus(render = true) {
    try {
      state.system = await api("/ffmpeg-status");
      state.systemLoadFailed = false;
    } catch (error) {
      // 读取失败时清空旧快照：过期状态不得继续显示为当前结果
      state.system = null;
      state.systemLoadFailed = true;
    }
    if (render && state.view === "settings") renderView(false);
  }

  async function loadDetectRecords(render = true) {
    try {
      state.detectRecords = await api("/detect-records");
      state.recordsLoaded = true;
      if (render && state.view === "batch" && state.batchMode === "records") renderView(false);
    } catch (error) {
      if (render) toast("暂时无法读取检测记录", "error");
    }
  }

  async function saveDetectRecord(sourceUrl, data) {
    if (!sourceUrl) return;
    try {
      await api("/detect-records", {
        method: "POST",
        body: {
          sourceUrl,
          title: data.title || "未命名检测",
          // 保留已检测到的文件名与格式，避免每次重新检测
          entries: (data.entries || []).map((entry) => ({
            url: entry.url || "",
            title: entry.title || "",
            filename: entry.filename || "",
            ext: entry.ext || "",
          })),
        },
      });
      await loadDetectRecords(false);
    } catch (error) {
      console.warn("检测记录未能保存");
    }
  }

  function loadRecordToWorkbench(recordId) {
    const record = state.detectRecords.find((item) => String(item.id) === String(recordId));
    if (!record) return toast("没有找到这条检测记录", "error");
    state.workbenchRows = (record.entries || []).map((entry) => ({
      url: entry.url || "",
      filename: entry.filename || entry.title || "",
      ext: entry.ext || "",
      selected: true,
    }));
    state.batchTitle = record.title || "采集清单";
    state.batchMode = "workbench";
    renderView();
    toast(`已载入 ${state.workbenchRows.length} 条内容到编辑台`, "success");
  }

  async function deleteDetectRecord(recordId) {
    const record = state.detectRecords.find((item) => String(item.id) === String(recordId));
    if (!(await confirmAction("删除检测记录", `删除“${record?.title || "当前记录"}”？这不会删除下载任务或本地文件。`, "删除记录"))) return;
    try {
      await api(`/detect-records/${encodeURIComponent(recordId)}`, { method: "DELETE" });
      state.detectRecords = state.detectRecords.filter((item) => String(item.id) !== String(recordId));
      renderView(false);
      toast("检测记录已删除", "success");
    } catch (error) {
      toast("删除未完成，请稍后重试", "error");
    }
  }

  function pageHead(eyebrow, title, text, actions = "") {
    return `<header class="page-head"><div><small class="eyebrow">${esc(eyebrow)}</small>
      <h1>${esc(title)}</h1><p>${esc(text)}</p></div><div class="head-actions">${actions}</div></header>`;
  }

  function taskActionButtons(task) {
    const buttons = [];
    const add = (action, label, icon, tone = "") => buttons.push(
      `<button class="button icon ${tone}" data-task-action="${action}" aria-label="${label}" title="${label}">${icon}</button>`,
    );
    // 「移动到分组」对所有状态都开放，弹层处理不走 taskAction
    add("move", "移动到分组", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 8 4 11l3 3"/><path d="M4 11h11"/><path d="M17 16l3-3-3-3"/><path d="M20 13H9"/></svg>', "action-move");
    // UI-P0-03 返修：正在检查文件（verifying）期间不提供删除/取消等任何破坏性操作，
    // 也绝不落入下方兜底删除逻辑，等待检查结束后再按最终状态渲染。
    if (task.status === "verifying") return buttons.join("");
    if (task.status === "downloading") add("cancel", "中断下载", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="none"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>', "action-danger");
    else if (task.status === "queued") add("cancel", "取消排队", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M7 7l10 10"/><path d="M17 7L7 17"/></svg>', "action-danger");
    else if (["error", "cancelled"].includes(task.status)) {
      add("resume", "续传任务", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/></svg>', "action-warning");
      add("delete", "删除任务", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>', "action-danger");
    } else if (["paused", "pending"].includes(task.status)) {
      add("start", "开始下载", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="none"><path d="M8 5v14l11-7z"/></svg>', "action-success");
      add("cancel", "取消待开始", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M7 7l10 10"/><path d="M17 7L7 17"/></svg>', "action-danger");
    }
    if (["complete", "incomplete"].includes(task.status)) {
      add("open-file", "播放视频", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="currentColor" stroke="none"><path d="M8 5v14l11-7z"/></svg>', "action-success");
      add("open-folder", "打开所在文件夹", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>');
      add("delete", "删除任务", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>', "action-danger");
    }
    // UI-P0-03: 确认按钮只在「残留待确认」状态出现（后端也只接受该状态）
    if (task.status === "incomplete") add("confirm", "确认任务", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-11"/></svg>', "action-success");
    if (!buttons.length) add("delete", "删除任务", '<svg class="ico" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6"/><path d="M14 11v6"/></svg>', "action-danger");
    return buttons.join("");
  }

  function userFriendlyTaskError(task) {
    if (!task?.error) return "";
    if (task.status === "incomplete") return "下载未完整完成，可重试或检查已有文件";
    if (task.status === "cancelled") return "任务已取消，可随时重新开始";
    if (task.error_code === "TLS_CERTIFICATE") return "安全连接验证失败，请使用更新版本后重试";
    if (task.error_code === "AUTH_REQUIRED") return "该内容可能需要登录凭据，请检查 Cookie 后重试";
    if (task.error_code === "RISK_CONTROLLED") return "平台已触发风控（限流/机器人验证/账号或访问受限），建议暂停下载、降低频率或更换网络，等待冷却后再试";
    if (task.error_code === "NETWORK_ERROR") return "网络连接未完成，请检查网络后重试";
    if (task.error_code === "NO_MEDIA_OUTPUT") return "下载工具没有生成媒体文件，请稍后重试";
    return "任务处理未完成，请检查链接后重试";
  }

  function taskProgressHtml(task, placement = "") {
    if (task.status !== "downloading") return "";
    const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
    const details = [];
    if (task.speed) details.push(task.speed);
    if (task.eta) details.push(`剩余 ${task.eta}`);
    if (task.fragments) details.push(`分段 ${task.fragments}`);
    const detailText = details.join(" · ") || "正在获取速度…";
    return `<div class="task-live-progress ${esc(placement)}">
      <div class="task-live-progress-head"><b>${progress.toFixed(1)}%</b><span>${esc(detailText)}</span></div>
      <div class="task-live-progress-track"><span style="width:${progress}%"></span></div>
    </div>`;
  }

  function taskRow(task, full = false, showCheckbox = true) {
    const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
    const title = task.display_name || task.output_name || "未命名任务";
    const running = task.status === "downloading";
    const active = ["downloading", "queued", "running", "processing"].includes(task.status);
    const expanded = state.expandedTasks.has(task.id) ? "expanded" : "";
    // 紧凑行右侧元信息：大小 · 时长 · 日期
    const compactMeta = [];
    if (task.file_size) compactMeta.push(fmtBytes(task.file_size));
    if (task.duration) compactMeta.push(fmtDurationCN(task.duration));
    compactMeta.push((task.completed_at || task.created_at || "").slice(0, 10));
    const checked = showCheckbox && state.selectedTaskIds.has(task.id) ? "checked" : "";
    const checkHtml = showCheckbox ? `<input type="checkbox" class="task-check" data-task-check="${esc(task.id)}" ${checked} aria-label="选择任务">` : "";
    return `<article class="task-row ${showCheckbox ? "" : "no-check "}${expanded}${active ? " is-active" : ""}" data-task-id="${esc(task.id)}">
      ${checkHtml}
      <div class="thumb"><svg class="ico" viewBox="0 0 24 24" width="20" height="20" fill="currentColor" stroke="none"><path d="M8 5v14l11-7z"/></svg></div>
      <div class="task-title-block">
        <span class="status ${esc(task.status)}">${esc(statusLabels[task.status] || task.status)}</span>
        ${basisBadgeHtml(task)}
        ${riskBadgeHtml(task)}
        <h3 class="task-title" title="${esc(title)}">${esc(title)}</h3>
        ${batchBadgeHtml(task)}
        ${taskProgressHtml(task, "compact")}
      </div>
      <div class="task-compact-meta">${esc(compactMeta.filter(Boolean).join(" · "))}</div>
      <span class="task-chevron" aria-hidden="true">▾</span>
      ${taskDetailHtml(task)}
    </article>`;
  }

  /* ====== legacy 迁移：任务详情 / 分组树 / 批次折叠条 / 卡片视图（2026-07-28） ====== */

  // 任务行内可展开的详情面板（点击整行切换）
  // 本次下载消耗时间（completed_at - created_at，仅已完成任务）
  function taskDownloadElapsed(task) {
    if (task.status !== "complete") return 0;
    const a = Date.parse(task.completed_at);
    const b = Date.parse(task.created_at);
    if (!Number.isFinite(a) || !Number.isFinite(b) || a <= b) return 0;
    return Math.round((a - b) / 1000);
  }

  function taskDetailHtml(task) {
    const url = task.url || "";
    const title = task.display_name || task.output_name || "未命名任务";
    const urlExpanded = state.expandedUrls.has(task.id);
    const started = (task.created_at || "").slice(0, 19).replace("T", " ");
    const completed = (task.completed_at || "").slice(0, 19).replace("T", " ") || "—";
    const elapsedSec = taskDownloadElapsed(task);
    const elapsedTxt = elapsedSec ? fmtElapsed(elapsedSec) : "";

    const meta = [];
    const resolutionUnknown = !task.resolution || task.resolution === "?" || task.resolution === "?x?";
    if (resolutionUnknown) {
      meta.push(`<span class="td-unknown" data-tip="未从流中解析到分辨率信息，可能源站未提供该字段"><b>分辨率</b>未知<span class="td-info-icon" aria-hidden="true">?</span></span>`);
    } else {
      meta.push(`<span><b>分辨率</b>${esc(task.resolution)}</span>`);
    }
    if (task.duration) meta.push(`<span><b>时长</b>${esc(fmtDurationCN(task.duration))}</span>`);
    if (task.file_size) meta.push(`<span><b>大小</b>${esc(fmtBytes(task.file_size))}</span>`);
    if (elapsedSec) meta.push(`<span><b>耗时</b>${esc(elapsedTxt)}</span>`);

    // UI-P0-03: 「文件检查」区块——如实展示检查结果与完成依据
    const checkLines = [];
    if (task.verify_ok === true) checkLines.push(`<span><b>检查结果</b>已通过</span>`);
    else if (task.verify_ok === false) checkLines.push(`<span><b>检查结果</b>未通过${task.verify_reason ? `（${esc(task.verify_reason)}）` : ""}</span>`);
    else checkLines.push(`<span><b>检查结果</b>未检查</span>`);
    if (task.has_audio === true) checkLines.push(`<span><b>音轨</b>含音轨</span>`);
    else if (task.has_audio === false) checkLines.push(`<span><b>音轨</b>无音轨</span>`);
    if (task.completion_basis) {
      const basisText = {
        verified: "自动检查通过",
        user_confirmed: `人工确认保留${task.confirmed_at ? `（${esc(task.confirmed_at.slice(0, 19).replace("T", " "))}）` : ""}`,
        legacy_unknown: "历史记录，当时未保存检查数据",
      }[task.completion_basis] || "";
      if (basisText) checkLines.push(`<span><b>完成依据</b>${basisText}</span>`);
      if (task.completion_basis === "user_confirmed" && task.confirmation_note) {
        checkLines.push(`<span><b>备注</b>${esc(task.confirmation_note)}</span>`);
      }
    }
    const checkSection = (task.status === "complete" || (task.verify_ok !== null && task.verify_ok !== undefined))
      ? `<div class="td-check"><div class="td-check-title">文件检查</div><div class="td-meta">${checkLines.join("")}</div></div>`
      : "";

    const err = userFriendlyTaskError(task)
      ? `<div class="td-line td-error"><b>说明</b><span>${esc(userFriendlyTaskError(task))}</span></div>`
      : "";

    return `<div class="tr-detail">
      <div class="td-headline">
        <div class="td-title-block">
          <h3 class="td-title" title="${esc(title)}">${esc(title)}</h3>
          ${url ? `<div class="td-url-wrap">
            <a class="td-url ${urlExpanded ? "is-expanded" : ""}" href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>
            <button type="button" class="td-url-toggle" data-task-url-toggle="${esc(task.id)}">${urlExpanded ? "收起链接" : "显示完整链接"}</button>
          </div>` : ""}
        </div>
        <div class="td-side">
          <span class="status ${esc(task.status)}">${esc(statusLabels[task.status] || task.status)}</span>
          ${basisBadgeHtml(task)}
          ${riskBadgeHtml(task)}
          <div class="task-actions">${taskActionButtons(task)}</div>
        </div>
      </div>
      ${task.site ? `<div class="td-site"><span class="td-site-icon" aria-hidden="true"><svg class="ico" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18 14 14 0 0 1 0-18"/></svg></span><span>${esc(task.site)}</span></div>` : ""}
      ${taskProgressHtml(task, "detail")}
      ${meta.length ? `<div class="td-meta">${meta.join("")}</div>` : ""}
      ${checkSection}
      <div class="td-time">
        <span><b>开始</b>${esc(started || "—")}</span>
        ${elapsedSec ? `<span><b>耗时</b>${esc(elapsedTxt)}</span>` : ""}
        <span><b>完成</b>${esc(completed)}</span>
      </div>
      ${err}
    </div>`;
  }

  // 卡片视图单元
  function taskCard(task, showCheckbox = true) {
    const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
    const title = task.display_name || task.output_name || "未命名任务";
    const running = task.status === "downloading";
    const meta = [];
    if (task.file_size) meta.push(fmtBytes(task.file_size));
    if (task.duration) meta.push(fmtDurationCN(task.duration));
    if (task.resolution) meta.push(task.resolution);
    meta.push((task.completed_at || task.created_at || "").slice(0, 10));
    const checked = showCheckbox && state.selectedTaskIds.has(task.id) ? "checked" : "";
    const checkHtml = showCheckbox ? `<input type="checkbox" class="task-check" data-task-check="${esc(task.id)}" ${checked} aria-label="选择任务">` : "";
    return `<article class="task-card status-${esc(task.status)}" data-task-id="${esc(task.id)}">
      <div class="task-card-head">${checkHtml}<h3 title="${esc(title)}">${esc(title)}</h3>
        <span class="status ${esc(task.status)}">${esc(statusLabels[task.status] || task.status)}</span>${basisBadgeHtml(task)}${riskBadgeHtml(task)}${batchBadgeHtml(task)}</div>
      <p class="task-card-sub">${task.site ? esc(task.site) : esc(task.url || "暂无详情")}${userFriendlyTaskError(task) ? ` · ${esc(userFriendlyTaskError(task))}` : ""}</p>
      ${running ? taskProgressHtml(task, "card") : ""}
      <div class="task-card-meta">${esc(meta.filter(Boolean).join(" · "))}</div>
      <div class="task-actions">${taskActionButtons(task)}</div>
    </article>`;
  }

  // 按当前视图模式返回行或卡片（任务中心始终显示复选框）
  function taskItem(task) {
    return state.viewMode === "card" ? taskCard(task, true) : taskRow(task, true, true);
  }

  // 构建分组树（递归，仅渲染含有任务的子树），供单个批次内部使用
  function renderGroupTree(tasks) {
    const groups = state.groups || [];
    const byGid = new Map();
    const orphan = [];
    for (const t of tasks) {
      const gid = t.group_id || "";
      if (gid) { if (!byGid.has(gid)) byGid.set(gid, []); byGid.get(gid).push(t); }
      else orphan.push(t);
    }
    const childMap = new Map();
    for (const g of groups) {
      const pid = g.parent_id || "";
      if (!childMap.has(pid)) childMap.set(pid, []);
      childMap.get(pid).push(g);
    }
    const hasTasks = (gid) =>
      (byGid.get(gid) || []).length > 0 ||
      (childMap.get(gid) || []).some((c) => hasTasks(c.id));
    const renderNode = (gid, depth) => {
      const g = groups.find((x) => x.id === gid);
      if (!g) return "";
      if (!state.showArchived && g.archived) return "";
      const gtasks = byGid.get(gid) || [];
      const collapsed = state.collapsedGroups.has(gid) ? "collapsed" : "";
      let html = `<div class="task-group ${collapsed}" data-group-id="${esc(gid)}" style="margin-left:${depth * 16}px">
        <div class="task-group-header" data-group-toggle="${esc(gid)}" data-tip="点击折叠或展开该分组">
          <span class="gh-arrow">▾</span>
          <span class="gh-name">${esc(g.name)}</span>
          <span class="gh-count">${gtasks.length}</span>
          <button type="button" class="gh-menu" data-group-menu="${esc(gid)}" title="分组操作" aria-label="分组操作">⋯</button>
        </div>
        <ul class="task-list ${state.viewMode === "card" ? "is-card" : ""}">
          ${gtasks.map((t) => taskItem(t)).join("")}
        </ul>
      </div>`;
      for (const c of (childMap.get(gid) || []).sort((a, b) => ((a.order || 0) - (b.order || 0)) || String(a.name).localeCompare(String(b.name)))) {
        if (hasTasks(c.id)) html += renderNode(c.id, depth + 1);
      }
      return html;
    };
    let html = "";
    for (const root of (childMap.get("") || []).sort((a, b) => ((a.order || 0) - (b.order || 0)) || String(a.name).localeCompare(String(b.name)))) {
      if (hasTasks(root.id)) html += renderNode(root.id, 0);
    }
    if (orphan.length) {
      html += `<ul class="task-list ${state.viewMode === "card" ? "is-card" : ""}" style="margin-top:4px">${orphan.map((t) => taskItem(t)).join("")}</ul>`;
    }
    return html || '<div class="empty" style="padding:18px">该筛选条件下没有可分组任务</div>';
  }

  // 任务中心主体：以分组树为骨架（批次仅作筛选条件与任务标签，不再嵌套为容器）
  function tasksBodyHtml() {
    computeBatches();
    const list = filteredTasks();
    if (!list.length) return '<div class="empty"><span>⌕</span>没有符合当前条件的任务</div>';
    return renderGroupTree(list);
  }

  function renderDownload() {
    const active = state.tasks.filter((t) => ["downloading", "queued"].includes(t.status));
    const recent = state.tasks.slice().sort((a,b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0,3);
    const kpi = state.stats?.kpi || {};
    const disk = state.stats?.disk || {};
    return `<div class="view">
      ${pageHead("本地媒体工作台", "保存内容，留下清晰的轨迹。", "粘贴你有权保存的媒体页面，影迹会完成解析、下载、合并与文件检查。", '<button class="button" data-go="batch">打开批量工作台</button>')}
      <section class="download-card">
        <div class="ready"><i></i> 准备就绪</div><h2>从一个链接开始</h2>
        <p>首页只处理单个视频；主页、合集与多个链接请使用批量工作台。</p>
        <label class="field-label" for="mediaUrl">媒体页面或视频链接</label>
        <div class="composer"><div class="input-wrap"><span>⌁</span>
          <input class="input" id="mediaUrl" type="url" autocomplete="off" placeholder="粘贴链接 · 重点支持 B 站 / YouTube / 抖音 / 公开 M3U8，其他站点尽力兼容">
        </div><button class="button primary" id="startDownload" disabled>开始下载</button></div>
        <div class="advanced-fields">
          <label><span>文件名</span><input class="input plain" id="outputName" placeholder="自动识别，也可手动填写"></label>
          <div class="format-bar" id="formatBar">
            <div class="format-bar-head">
              <span class="format-bar-title">画质与音轨</span>
              <button class="button ghost" id="detectInfo" disabled>检测名称与可用格式</button>
            </div>
            <div class="fmt-field">
              <span class="fmt-label">画质</span>
              <select class="select" id="videoQuality"><option value="">自动选择最佳画质</option></select>
            </div>
            <div class="fmt-field" id="audioBlock">
              <span class="fmt-label">音轨</span>
              <select class="select" id="audioTrack"><option value="">不单独指定（自动配对最佳音轨）</option></select>
              <label class="merge-check"><input type="checkbox" id="mergeAudio" checked> 合并音轨</label>
            </div>
            <span class="format-hint" id="formatHint"></span>
          </div>
        </div>
        <div class="download-foot">
          <div class="save-path-display">
            <span class="fmt-label">保存位置</span>
            <input class="input" id="savePath" type="text" autocomplete="off" spellcheck="false" placeholder="留空保存到软件内 Downloads，也可手填路径或点浏览">
            <button class="button ghost" id="browseDir" type="button">浏览…</button>
          </div>
          <div class="download-foot-meta">
            <span>路径与任务记录仅存于本机，不会上传</span>
          </div>
        </div>
      </section>
      <div id="capabilityAnomalyRegion" aria-live="polite">${anomalyCardsHtml()}</div>
      <section class="health">
        <div class="health-item"><i>今</i><div><b>今日保存 ${kpi.today?.count || 0} 项</b><span>${fmtBytes(kpi.today?.bytes)}</span></div></div>
        <div class="health-item"><i>周</i><div><b>本周保存 ${kpi.week?.count || 0} 项</b><span>${fmtBytes(kpi.week?.bytes)}</span></div></div>
        <div class="health-item"><i class="blue">盘</i><div><b>可用空间</b><span>${fmtBytes(disk.free)}</span></div></div>
        <div class="health-item"><i class="blue">↓</i><div><b id="activeTaskCount">${active.length} 个任务进行中</b><span id="activeTaskSpeed">${esc(active[0]?.speed || "当前没有进行中的任务")}</span></div></div>
      </section>
      <div class="more-data-row"><button class="link" data-go="data">更多数据</button></div>
      <div class="section-head"><div><h2>最近任务</h2></div><button class="link" id="recentTaskTotal" data-go="tasks">查看全部 ${state.tasks.length} 个任务 →</button></div>
      <div class="task-list" id="recentTaskList">${recent.length ? recent.map((t) => taskRow(t, false, false)).join("") : '<div class="empty"><span>↓</span>还没有任务，粘贴一个链接开始使用</div>'}</div>
    </div>`;
  }

  function completedUrlSet() {
    return new Set(
      state.tasks
        .filter((task) => task.status === "complete" && task.url)
        .map((task) => normalizeUrl(task.url)),
    );
  }

  function normalizeUrl(url) {
    return String(url || "").trim().replace(/#.*$/, "").replace(/\/$/, "").toLowerCase();
  }

  function visibleWorkbenchRows() {
    const done = state.workbenchDedup ? completedUrlSet() : new Set();
    return state.workbenchRows
      .map((row, index) => ({ row, index }))
      .filter(({ row }) => !state.workbenchDedup || !done.has(normalizeUrl(row.url)));
  }

  function batchSwitcher() {
    const items = [
      ["flow", "批量获取", state.batchEntries.length],
      ["records", "检测记录", state.detectRecords.length],
      ["workbench", "采集编辑台", visibleWorkbenchRows().length],
    ];
    return `<nav class="batch-switcher" aria-label="批量工作台功能">
      ${items.map(([mode, label, count]) => `<button class="${state.batchMode === mode ? "active" : ""}" data-batch-mode="${mode}">
        <span>${label}</span>${count ? `<em>${count}</em>` : ""}
      </button>`).join("")}
    </nav>`;
  }

  function filteredDetectRecords() {
    const query = state.recordsSearch.trim().toLowerCase();
    if (!query) return state.detectRecords;
    return state.detectRecords.filter((record) =>
      `${record.title || ""} ${record.sourceUrl || ""}`.toLowerCase().includes(query),
    );
  }

  function renderDetectRecords() {
    const records = filteredDetectRecords();
    return `<div class="view">
      ${pageHead("检测记录", "把历史检测结果，继续变成可执行的清单。", "每次成功检测都会自动保存在本机，可随时载入采集编辑台继续整理。", '<button class="button" id="refreshDetectRecords">刷新记录</button><span class="status complete">仅本机保存</span>')}
      ${batchSwitcher()}
      <section class="panel records-panel">
        <div class="records-toolbar"><div><h2>历史检测</h2><p>共 ${state.detectRecords.length} 条记录 · 当前显示 ${records.length} 条</p></div>
          <input class="input plain" id="recordsSearch" value="${esc(state.recordsSearch)}" placeholder="搜索标题或来源链接">
        </div>
        <div class="record-list" id="detectRecordList">${records.map((record) => `
          <article class="record-card" data-record-id="${esc(record.id)}">
            <div class="record-mark"><strong>${Number(record.count) || (record.entries || []).length}</strong><small>条内容</small></div>
            <div class="record-main"><div class="record-title-line"><h3>${esc(record.title || "未命名检测")}</h3><span>${fmtDateTime(record.createdAt)}</span></div>
              <p class="record-url" title="${esc(record.sourceUrl)}">${esc(record.sourceUrl || "未记录来源链接")}</p>
              <small>载入后可继续改名、过滤重复内容并批量入队</small>
            </div>
            <div class="record-actions"><button class="button" data-record-load="${esc(record.id)}">载入编辑台</button>
              <button class="button icon action-danger" data-record-delete="${esc(record.id)}" aria-label="删除检测记录" title="删除检测记录">×</button></div>
          </article>`).join("") || '<div class="empty record-empty"><span>⌕</span>还没有检测记录，完成一次播放列表检测后会自动保存在这里</div>'}</div>
      </section>
    </div>`;
  }

  function renderWorkbench() {
    const visible = visibleWorkbenchRows();
    const selected = visible.filter(({ row }) => row.selected).length;
    const skipped = state.workbenchDedup ? Math.max(0, state.workbenchRows.length - visible.length) : 0;
    return `<div class="view">
      ${pageHead("采集编辑台", "先整理清楚，再加入下载队列。", "批量修改文件名、分配下载路径，并可导出 Excel 留存。", '<button class="button" id="importWorkbench">导入 Excel</button><a class="button" href="/api/import/excel/template">下载模板</a><input id="workbenchFile" type="file" accept=".xlsx,.xls" hidden>')}
      ${batchSwitcher()}
      <section class="panel">
        <div class="workbench-command-bar"><div><small class="eyebrow">当前清单</small><h2>编辑与入队</h2><p id="workbenchSummaryTop">当前 ${visible.length} 行 · 已选择 ${selected} 行</p></div>
          <div class="workbench-command-actions"><button class="button" id="exportWorkbench">导出 Excel</button>
          <button class="button primary" id="queueWorkbench">加入下载队列</button></div></div>
        <div class="workbench-tools">
          <div class="workbench-tool"><div class="tool-heading"><b>下载路径</b><small>逐行指定保存位置</small></div><div class="tool-line">
            <input class="input plain" id="workbenchDir" placeholder="例如：D:\\videos\\java课">
            <button class="button" data-workbench-action="apply-dir-selected">应用到选中</button>
            <button class="button" data-workbench-action="apply-dir-all">应用到全部</button></div></div>
          <div class="workbench-tool"><div class="tool-heading"><b>批量命名</b><small>前缀 + 序号</small></div><div class="tool-line">
            <input class="input plain" id="workbenchPrefix" placeholder="例如：课程_">
            <button class="button" data-workbench-action="prefix">改名</button></div></div>
          <div class="workbench-tool"><div class="tool-heading"><b>替换文件名</b><small>只修改已选内容</small></div><div class="tool-line">
            <input class="input plain" id="workbenchFind" placeholder="查找">
            <input class="input plain" id="workbenchReplace" placeholder="替换为">
            <button class="button" data-workbench-action="replace">替换</button></div></div>
          <div class="workbench-tool"><div class="tool-heading"><b>任务分组</b><small>便于后续查找</small></div><div class="tool-line">
            <select class="select" id="workbenchGroup"><option value="">不指定分组</option>${state.groups.filter(g => !g.archived).map(g => `<option value="${esc(g.id)}" ${g.id === state.workbenchGroupId ? "selected" : ""}>${esc(g.name)}</option>`).join("")}</select></div></div>
        </div>
        <div class="workbench-table-wrap"><table class="workbench-table">
          <thead><tr><th><input id="workbenchCheckAll" type="checkbox" ${visible.length && selected === visible.length ? "checked" : ""} ${selected > 0 && selected < visible.length ? 'data-partial="true"' : ""} aria-label="全选编辑台内容"></th><th>视频链接</th><th>文件名</th><th class="col-ext">格式</th><th>下载路径</th><th></th></tr></thead>
          <tbody>${visible.map(({ row, index }) => `<tr data-workbench-index="${index}">
            <td><input class="workbench-check" type="checkbox" ${row.selected ? "checked" : ""} aria-label="选择第 ${index + 1} 行"></td>
            <td><input class="input plain url-field" data-workbench-field="url" value="${esc(row.url)}" readonly></td>
            <td><input class="input plain" data-workbench-field="filename" value="${esc(row.filename)}"></td>
            <td><input class="input plain col-ext" data-workbench-field="ext" value="${esc(row.ext || "")}" placeholder="如 mp4"></td>
            <td><input class="input plain" data-workbench-field="output_dir" value="${esc(row.output_dir || "")}" placeholder="留空=用全局所选路径"></td>
            <td><button class="button icon action-danger" data-workbench-remove="${index}" aria-label="删除第 ${index + 1} 行">×</button></td>
          </tr>`).join("") || '<tr><td colspan="6"><div class="empty"><span>＋</span>从播放列表发送内容，或导入 Excel 开始编辑</div></td></tr>'}</tbody>
        </table></div>
        <div class="panel-foot workbench-foot"><div class="workbench-summary"><span id="workbenchSummaryBottom">共 ${visible.length} 行 · 已选 ${selected}</span>
          <label><input id="workbenchDedup" type="checkbox" ${state.workbenchDedup ? "checked" : ""}> 自动过滤已下载<span id="workbenchDedupHint">${skipped ? `（已过滤 ${skipped} 条）` : ""}</span></label></div>
          <small>未指定路径的内容，加入队列时会提示选择保存位置</small></div>
      </section>
    </div>`;
  }

  function renderBatch() {
    if (state.batchMode === "workbench") return renderWorkbench();
    if (state.batchMode === "records") return renderDetectRecords();
    const entries = state.batchEntries;
    return `<div class="view">
      ${pageHead("批量工作台", "把复杂导入，拆成清楚的三步。", "来源、检查与入队彼此独立，检测不会直接创建下载任务。", '<span class="status complete">内容仅保存在本机</span>')}
      ${batchSwitcher()}
      <ol class="stepper">${["导入来源","检查与选择","加入队列"].map((label,i) => `<li class="${state.batchStep === i+1 ? "current" : state.batchStep > i+1 ? "done" : ""}">
        <button data-batch-step="${i+1}"><span>${state.batchStep > i+1 ? "✓" : i+1}</span><div><small>步骤 ${i+1}</small><b>${label}</b></div></button></li>`).join("")}</ol>
      <section class="panel">
        ${state.batchStep === 1 ? `<div class="batch-source"><span class="number">01</span><div>
          <h2>检测合集或播放列表</h2><p>先识别全部内容，再统一检查标题、时长与选择范围。</p>
          <label class="field-label" for="playlistUrl">播放列表、合集或频道链接</label>
          <div class="composer"><div class="input-wrap"><span>⌁</span><input class="input" id="playlistUrl" placeholder="在此粘贴播放列表链接…"></div>
          <button class="button primary" id="detectPlaylist">开始检测</button></div>
        </div></div><div class="panel-foot"><span>检测期间不会创建下载任务</span><div class="head-actions"><button class="button" id="importWorkbenchFromBatch">导入 Excel</button><button class="button" data-go="download">返回单链接下载</button><input id="batchExcelFile" type="file" accept=".xlsx,.xls" hidden></div></div>`
        : state.batchStep === 2 ? `<div class="batch-toolbar"><div><h2>${esc(state.batchTitle || "检测结果")}</h2><p>共识别 ${entries.length} 条内容，可取消不需要的项目</p></div>
          <div class="batch-toolbar-actions"><button class="button ghost" id="toggleBatchAll">全选 / 取消全选</button>
          <button class="button" id="inspectBatchSelected">检测文件名与格式</button></div></div>
          <div class="batch-table">${entries.map((item,i) => {
            const hasFilename = !!item.filename;
            const hasExt = !!item.ext;
            const inspecting = item.inspecting === true;
            const filenameText = hasFilename
              ? esc(item.filename)
              : (inspecting ? "检测中…" : esc(item.inspectError || "未检测"));
            const extText = hasExt ? esc(item.ext) : (inspecting ? "…" : "—");
            return `<label class="batch-row${inspecting ? " is-inspecting" : (hasFilename || hasExt ? " is-inspected" : "")}">
            <input type="checkbox" class="batch-check" data-index="${i}" ${item.selected !== false ? "checked" : ""}>
            <small>${String(i+1).padStart(2,"0")}</small>
            <b class="batch-row-title" title="${esc(item.title || "")}">${esc(item.title || "未命名内容")}</b>
            <span class="batch-row-filename${hasFilename ? " has-result" : ""}" title="${esc(item.filename || "")}">${filenameText}</span>
            <span class="batch-row-ext${hasExt ? " has-ext" : ""}">${extText}</span>
            <span class="batch-row-duration">${fmtDuration(item.duration)}</span>
          </label>`;
          }).join("")}</div>
          <div class="panel-foot"><button class="button ghost" data-batch-step="1">重新检测</button><span id="batchSelectedText">已选择 ${entries.filter(e => e.selected !== false).length} 条</span>
            <div class="head-actions"><button class="button" id="sendToWorkbench">发送到编辑台</button><button class="button primary" data-batch-step="3">确认下载设置</button></div></div>`
        : `<div class="batch-source"><span class="number">03</span><div><h2>确认任务分组</h2><p>加入列表时将弹出目录选择，请为本次任务指定保存位置。</p>
          <div class="advanced-fields">
            <label><span>目标分组</span><select class="select" id="batchGroup"><option value="">未分组</option>${state.groups.filter(g => !g.archived).map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("")}</select></label>
          </div></div></div><div class="panel-foot"><button class="button ghost" data-batch-step="2">返回检查内容</button>
          <span>已选择 ${entries.filter(e => e.selected !== false).length} 条</span><button class="button primary" id="createBatch">加入待下载列表</button></div>`}
      </section>
    </div>`;
  }

  function renderTasks() {
    const list = filteredTasks();
    return `<div class="view">
      ${pageHead("任务中心", "每个任务，都知道下一步该做什么。", "搜索、筛选、继续、取消和打开文件都集中在一个清晰列表中。")}
      <div class="task-toolbar">
        <div class="task-toolbar-head">
          <div class="task-toolbar-title">
            <h2>任务列表 <span class="badge">${state.tasks.length}</span></h2>
            <p id="taskSummary">${state.tasks.length} 条记录 · 当前显示 ${list.length} 条${state.selectedBatch ? ` · ${esc(batchLabel(state.selectedBatch))}` : ""}</p>
          </div>
          <div class="task-toolbar-actions">
            <button type="button" class="icon-btn ${state.showArchived ? "active" : ""}" data-show-archived title="${state.showArchived ? "隐藏已归档分组的任务" : "显示已归档分组的任务"}" aria-label="归档视图"><span class="icon-glyph" aria-hidden="true">🗑</span></button>
            <button type="button" class="icon-btn primary" data-new-group title="新建分组" aria-label="新建分组"><span class="icon-glyph" aria-hidden="true">+</span></button>
            <button type="button" class="icon-btn with-text" data-export-excel title="导出当前任务为 Excel" aria-label="导出 Excel"><span class="icon-glyph" aria-hidden="true">↗</span><span>Excel</span></button>
            <button type="button" class="icon-btn" data-refresh-tasks title="刷新任务列表" aria-label="刷新"><span class="icon-glyph" aria-hidden="true">⟳</span></button>
          </div>
        </div>
        <div class="task-toolbar-filters">
          <input class="input plain" id="taskSearch" value="${esc(state.taskSearch)}" placeholder="搜索标题或链接">
          <select class="select" id="taskStatus"><option value="all">全部状态</option><option value="downloading">进行中</option><option value="pending">待开始</option><option value="complete">已完成</option><option value="failed">需要处理</option></select>
          <select class="select" id="taskBatch" title="按批次筛选，仅显示该批次下的任务"><option value="">全部批次</option>${batchOptionsHtml()}</select>
          <div class="tt-view-switch" title="切换展示方式">
            <button class="tt-view-btn ${state.viewMode === "compact" ? "active" : ""}" data-view-mode="compact" title="紧凑列表">≣</button>
            <button class="tt-view-btn ${state.viewMode === "card" ? "active" : ""}" data-view-mode="card" title="卡片视图">▦</button>
          </div>
        </div>
      </div>
      <div id="tasksList">${tasksBodyHtml()}</div>
      <div id="batchBar" class="batch-bar" hidden></div>
    </div>`;
  }

  // R-1：数据中心分组分布（沿用来源分布语义，同时展示占比与体量）
  function dataGroupsDistributionHtml(groups) {
    const rows = (groups || []);
    if (!rows.length) return '<div class="empty">暂无分组数据</div>';
    return `<div class="distribution">${rows.map((x) => `<div class="dist-row dist-group">
      <span class="dist-name" title="${esc(x.name || "未分组")}">${esc(x.name || "未分组")}</span>
      <span class="dist-track"><i style="width:${x.pct}%"></i></span>
      <b>${x.pct}%</b>
      <small class="dist-bytes">${fmtBytes(x.bytes)}</small>
    </div>`).join("")}</div>`;
  }

  // R-1：数据中心最近批次（全宽，默认最近 8 个；批次 N 为倒序友好名）
  // 返修：窄屏（含 1366 视口）经 CSS 转为两列卡片布局，所有字段保留文字标签，
  // 不依赖横向滚动；other > 0 时显示「其他状态 N」，不静默丢失。
  function dataBatchesTableHtml(batches) {
    const rows = (batches || []).slice(0, 8);
    if (!rows.length) {
      return '<div class="empty">暂无批次任务，使用批量工作台加入队列后可在这里查看。</div>';
    }
    const hasOther = rows.some((b) => (Number(b.other) || 0) > 0);
    const head = `<tr><th>批次</th><th>最近时间</th><th>总任务</th><th>已完成</th><th>需要处理</th><th>已取消</th><th>进行中</th>${hasOther ? "<th>其他状态</th>" : ""}<th>已保存体量</th><th>操作</th></tr>`;
    const body = rows.map((b, idx) => {
      const label = `批次 ${idx + 1}`;
      const needs = Number(b.needs_attention) || 0;
      const cancelled = Number(b.cancelled) || 0;
      const active = Number(b.active) || 0;
      const other = Number(b.other) || 0;
      const complete = Number(b.complete) || 0;
      const pct = Number(b.completion_pct) || 0;
      return `<tr>
        <td data-label="批次" class="batch-name">${esc(label)}</td>
        <td data-label="最近时间">${esc(fmtDateTime(b.latest_at))}</td>
        <td data-label="总任务">${b.total}</td>
        <td data-label="已完成"><span class="status-pill done">已完成 ${complete}</span></td>
        <td data-label="需要处理">${needs ? `<span class="status-pill attention">需要处理 ${needs}</span>` : "0"}</td>
        <td data-label="已取消">${cancelled ? `<span class="status-pill cancelled">已取消 ${cancelled}</span>` : "0"}</td>
        <td data-label="进行中">${active ? `<span class="status-pill active">进行中 ${active}</span>` : "0"}</td>
        ${hasOther ? `<td data-label="其他状态">${other ? `<span class="status-pill other">其他状态 ${other}</span>` : "0"}</td>` : ""}
        <td data-label="已保存体量">${esc(fmtBytes(b.bytes))} <small class="muted">${pct}% 完成</small></td>
        <td data-label="操作" class="batch-actions"><button class="button ghost" data-batch-view="${esc(b.batch_id)}">查看任务</button></td>
      </tr>`;
    }).join("");
    return `<div class="table-wrap"><table class="stats-table batches-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
  }

  function renderData() {
    const data = state.stats;
    // R-1：接口失败，清空旧结果并展示失败面板（不冒充最新状态）
    if (!data && state.statsLoadFailed) {
      return `<div class="view">${pageHead("数据中心","看见成果，也看见空间。","所有统计都在当前设备完成，不上传任务或文件信息。")}
        <section class="panel stats-failed">
          <h2>暂时无法读取使用数据</h2>
          <p>请检查网络或本地服务后重试，数据不会停留在上一次的结果上。</p>
          <div class="settings-actions"><button class="button" data-reload-stats>重新加载</button></div>
        </section>
      </div>`;
    }
    if (!data) {
      const skKpi = Array(4).fill(0).map(() => `<div class="kpi"><span class="sk sk-line" style="width:42%"></span><b class="sk sk-block" style="width:64%"></b><small class="sk sk-line" style="width:30%"></small></div>`).join("");
      const skPanel = (body) => `<section class="panel"><h2 class="sk sk-line" style="width:38%"></h2><p class="sk sk-line" style="width:56%"></p>${body}</section>`;
      return `<div class="view">${pageHead("数据中心","看见成果，也看见空间。","所有统计都在当前设备完成，不上传任务或文件信息。")}
      <div class="skeleton" aria-busy="true" aria-label="正在读取使用数据">
        <div class="kpi-grid">${skKpi}</div>
        <div class="data-grid">${skPanel(`<div class="sk-shape"></div>`)}${skPanel(`<div class="sk-bars"></div>`)}</div>
        <div class="data-grid">${skPanel(`<div class="sk-shape"></div>`)}${skPanel(`<div class="sk-bars"></div>`)}</div>
      </div></div>`;
    }
    const k = data.kpi || {};
    const disk = data.disk || {};
    const trend = data.trend || [];
    const max = Math.max(...trend.map((x) => Number(x.bytes) || 0), 1);
    const failureTrend = data.failure_trend || [];
    const maxNeeds = Math.max(...failureTrend.map((x) => Number(x.needs_attention_count) || 0), 1);
    const totalNeeds = failureTrend.reduce((s, x) => s + (Number(x.needs_attention_count) || 0), 0);
    const groupsBody = dataGroupsDistributionHtml(data.groups);
    const batchesBody = dataBatchesTableHtml(data.batches);
    return `<div class="view">
      ${pageHead("数据中心","看见成果，也看见空间。","保存成果与空间信息都在当前设备汇总。", '<a class="button" href="/api/stats/report?range=week&sections=totals,sources,disk,outcomes,batches&redact=1">导出本周报告</a>')}
      <section class="kpi-grid">
        ${[["今日下载",k.today],["本周下载",k.week],["本月下载",k.month],["累计保存",k.total]].map(([label,item]) => `<div class="kpi"><span>${label}</span><b>${fmtBytes(item?.bytes)}</b><small>${item?.count || 0} 个文件</small></div>`).join("")}
      </section>
      <div class="data-grid">
        <section class="panel"><h2>近 14 天保存趋势</h2><p>按任务完成时间统计</p>
          <div class="chart">${trend.map((x) => `<i class="bar" style="--h:${Math.max(2,(x.bytes/max)*100)}%" title="${esc(x.date)} · ${fmtBytes(x.bytes)}"></i>`).join("")}</div></section>
        <section class="panel"><h2>近 14 天需要处理趋势</h2><p>按当前任务结果汇总；重试成功后会归入已完成</p>
          ${totalNeeds === 0 ? '<p class="empty-inline">近 14 天没有需要处理的任务</p>' : ""}
          <div class="chart chart-needs" role="img" aria-label="近14天需要处理任务趋势，共 ${totalNeeds} 个需要处理任务">${failureTrend.map((x) => {
            const n = Number(x.needs_attention_count) || 0;
            const zero = n === 0 ? " bar-zero" : "";
            const h = Math.max(3, (n / maxNeeds) * 100);
            return `<i class="bar needs${zero}" style="--h:${h}%" title="${esc(x.date)} · 需要处理 ${n} 个（失败 ${x.error_count}，未完成 ${x.incomplete_count}）" aria-label="${esc(x.date)} 需要处理 ${n} 个"></i>`;
          }).join("")}</div></section>
      </div>
      <div class="data-grid">
        <section class="panel"><h2>来源分布</h2><p>累计文件体量</p><div class="distribution">${(data.sources || []).map((x) => `<div class="dist-row"><span>${esc(x.name)}</span><span class="dist-track"><i style="width:${x.pct}%"></i></span><b>${x.pct}%</b></div>`).join("") || '<div class="empty">暂无已完成任务</div>'}</div></section>
        <section class="panel"><h2>分组分布</h2><p>按已保存文件体量</p>${groupsBody}</section>
      </div>
      <section class="panel panel-wide"><h2>最近批次</h2><p>按最近时间倒序，最多展示最近 8 个批次</p>${batchesBody}</section>
      <div class="data-grid"><section class="panel"><h2>磁盘健康</h2><p>${esc(disk.drive || "下载盘")} · 已用 ${disk.used_pct || 0}% · 剩余 ${fmtBytes(disk.free)}</p>
        <div class="disk-meter"><i style="width:${Math.min(100,disk.used_pct || 0)}%"></i></div><small class="setting-value">${esc(disk.alert?.title || "当前容量状态正常")}</small></section>
        <section class="panel"><h2>大文件排行</h2><div class="distribution">${(data.top_files || []).slice(0,4).map((x) => `<div class="setting-row"><div><b>${esc(x.name)}</b><small>${esc(x.group)}</small></div><span class="setting-value">${fmtBytes(x.bytes)}</span></div>`).join("") || '<div class="empty">暂无数据</div>'}</div></section></div>
    </div>`;
  }

  // ===== UI-P0-04：设置中心（六分区） =====
  // 原则：只展示真实能力。可编辑项即时生效；无底层能力的项只作只读状态，不渲染假开关。
  const SETTINGS_SECTIONS = [
    ["general", "常规"],
    ["download", "下载"],
    ["network", "网络与 Cookie"],
    ["update", "更新"],
    ["privacy", "隐私"],
    ["about", "关于"],
  ];

  // 设置行：标题 + 辅助说明 + 右侧控件/状态，统一对齐
  function settingRow(title, desc, control) {
    return `<div class="setting-row"><div class="setting-info"><b>${esc(title)}</b>${desc ? `<small>${esc(desc)}</small>` : ""}</div>${control}</div>`;
  }
  // 只读状态文本（视觉 + 语义均表达状态，不依赖颜色）
  function settingStatus(text, tone = "") {
    return `<span class="setting-status ${tone}" role="status">${esc(text)}</span>`;
  }
  // 暂未提供的能力：明确文字说明，绝不渲染可点击但不生效的控件
  const unavailable = () => settingStatus("暂未提供", "unavailable");

  function renderSettingsGeneral() {
    return `<section class="panel settings-panel"><h2>常规</h2>
      ${settingRow("亮色主题", "切换为明亮纸面风格，立即生效并在本机保留", `<label class="switch"><input id="themeSwitch" aria-label="亮色主题" type="checkbox" ${state.theme === "light" ? "checked" : ""}><span></span></label>`)}
      ${settingRow("自动刷新任务", "页面可见时定期获取最新任务进度，立即生效", `<label class="switch"><input id="pollingSwitch" aria-label="自动刷新任务" type="checkbox" ${state.polling ? "checked" : ""}><span></span></label>`)}
      ${settingRow("界面语言", "当前提供简体中文", settingStatus("简体中文"))}
      ${settingRow("新手引导", "查看产品介绍与基础使用条件检查", `<button class="button" id="replayOnboarding" type="button">重新查看</button>`)}
      ${settingRow("开机自动启动", "跟随系统启动影迹", unavailable())}
      ${settingRow("最小化到托盘", "关闭窗口时保留后台任务", unavailable())}
    </section>`;
  }

  function renderSettingsDownload() {
    const sys = state.system;
    if (!sys) return settingsUnreadablePanel("下载");
    return `<section class="panel settings-panel"><h2>下载</h2>
      ${settingRow("默认保存位置", "未单独指定保存位置时，文件保存到这里", `<span class="setting-value setting-path">${esc(sys.download_dir || "读取中")}</span>`)}
      ${settingRow("同时下载任务数", `最多 ${Number(sys.max_concurrent_tasks) || 3} 个任务同时进行，超出的自动排队`, settingStatus(`${Number(sys.max_concurrent_tasks) || 3} 个`))}
      ${settingRow("单任务分段加速", "每个任务同时下载多个分段以加快速度", settingStatus(`${Number(sys.concurrent_fragments) || 8} 路`))}
      ${settingRow("文件命名", "自动使用检测到的标题命名，可在采集编辑台逐条修改", settingStatus("自动命名"))}
      ${settingRow("重复任务处理", "批量入队时自动过滤重复链接", settingStatus("自动过滤"))}
      <p class="settings-note">以上默认行为暂不支持在此修改。保存位置可在每次下载或采集编辑台中单独指定。</p>
    </section>`;
  }

  function renderSettingsNetwork() {
    const sys = state.system;
    if (!sys) return settingsUnreadablePanel("网络与 Cookie");
    const engineOk = Boolean(sys.ffmpeg) && Boolean(sys.yt_dlp);
    return `<section class="panel settings-panel"><h2>网络与 Cookie</h2>
      ${settingRow("登录凭据（Cookie）", "用于向对应网站发起已登录访问请求。凭据由本机调用，不会发送给影迹开发者或无关服务；现在也可直接在应用内粘贴添加。", sys.cookies_loaded ? settingStatus("已加载", "ok") : settingStatus("未加载", "warn"))}
      <div class="settings-actions"><button class="button" id="manageCookies">${sys.cookies_loaded ? "管理 Cookie" : "添加 Cookie"}</button></div>
      ${settingRow("下载与合并组件", "影迹完成解析、下载与合并所需的本机组件", engineOk ? settingStatus("就绪", "ok") : settingStatus("异常", "warn"))}
      ${settingRow("代理设置", "通过代理服务器访问网络", unavailable())}
      <div class="settings-actions"><button class="button" id="recheckNetwork">重新检查</button></div>
      <p class="settings-note">重新检查只会读取本机状态，不会发送任何数据。</p>
    </section>`;
  }

  // 系统状态读取失败时，依赖分区内的持续失败提示 + 重试入口
  function systemRetryBlock() {
    return `<p class="settings-note">系统状态读取失败，以上信息暂时无法显示。</p>
      <div class="settings-actions"><button class="button" id="retrySystemStatus">重试读取</button></div>`;
  }

  function renderSettingsUpdate() {
    const sys = state.system;
    return `<section class="panel settings-panel"><h2>更新</h2>
      ${settingRow("当前版本", "正在使用的影迹版本", sys ? settingStatus(String(sys.version || "未知")) : settingStatus("读取失败", "warn"))}
      ${settingRow("应用内更新", "在设置中直接检查并安装新版本", unavailable())}
      ${sys ? "" : systemRetryBlock()}
      <p class="settings-note">当前尚未启用应用内更新。</p>
    </section>`;
  }

  function renderSettingsPrivacy() {
    return `<section class="panel settings-panel"><h2>隐私</h2>
      ${settingRow("数据存储", "任务记录与设置仅保存在当前设备", settingStatus("仅本机", "ok"))}
      ${settingRow("数据上传", "除完成解析和下载所需的目标网站请求外，影迹不向开发者服务器上传下载链接、登录凭据或本地文件信息", settingStatus("不上传给开发者", "ok"))}
      ${settingRow("使用数据收集", "当前版本不包含任何使用情况收集或上报功能", settingStatus("无收集", "ok"))}
      ${settingRow("检测记录", "仅保存在本机，最多保留 500 条，可在批量工作台的检测记录中逐条删除", settingStatus("仅本机", "ok"))}
    </section>`;
  }

  function renderSettingsAbout() {
    const sys = state.system;
    return `<section class="panel settings-panel"><h2>关于</h2>
      ${settingRow("产品名称", "", settingStatus("影迹"))}
      ${settingRow("产品定位", "开源的本地媒体采集与批量下载管理工具", "")}
      ${settingRow("开源许可", "允许查看、修改和按相同许可分发源代码", settingStatus("GPLv3"))}
      ${settingRow("版本", "", sys ? settingStatus(String(sys.version || "未知")) : settingStatus("读取失败", "warn"))}
      ${settingRow("数据位置", "所有任务与设置仅保存在当前设备", settingStatus("仅本机", "ok"))}
      ${sys ? "" : systemRetryBlock()}
    </section>`;
  }

  // 系统状态读取失败时的可恢复面板（提供真实可用的重试）
  function settingsUnreadablePanel(title) {
    return `<section class="panel settings-panel"><h2>${esc(title)}</h2>
      <p class="settings-note">系统状态读取失败，部分信息暂时无法显示。</p>
      <div class="settings-actions"><button class="button" id="retrySystemStatus">重试读取</button></div>
    </section>`;
  }

  function renderSettings() {
    const current = SETTINGS_SECTIONS.some(([key]) => key === state.settingsSection)
      ? state.settingsSection : "general";
    const renders = {
      general: renderSettingsGeneral,
      download: renderSettingsDownload,
      network: renderSettingsNetwork,
      update: renderSettingsUpdate,
      privacy: renderSettingsPrivacy,
      about: renderSettingsAbout,
    };
    const nav = SETTINGS_SECTIONS.map(([key, label]) =>
      `<button class="settings-nav-item${key === current ? " active" : ""}" data-settings-section="${key}" aria-current="${key === current ? "true" : "false"}">${esc(label)}</button>`,
    ).join("");
    return `<div class="view">
      ${pageHead("偏好设置", "让影迹更符合你的使用习惯。", "只展示真实能力：可以修改的立即生效，暂未提供的如实标注。", '<span class="status complete">数据仅存本机</span>')}
      <div class="settings-layout">
        <nav class="settings-nav" aria-label="设置分区">${nav}</nav>
        <div class="settings-content">${renders[current]()}</div>
      </div>
    </div>`;
  }

  function renderView(focus = true) {
    const renders = { download: renderDownload, batch: renderBatch, tasks: renderTasks, data: renderData, settings: renderSettings };
    // 外部功能模块（如格式转换）自带渲染与事件，走独立分支，不参与内置视图逻辑。
    // 没有注册任何外部模块时，这段等价于不存在。
    const extView = window.YingjiExtViews && window.YingjiExtViews[state.view];
    if (extView && !renders[state.view]) {
      if (state.extMounted && state.extMounted !== state.view) {
        try { window.YingjiExtViews[state.extMounted]?.unmount?.(); } catch (_) { /* 外部模块异常不影响主程序 */ }
      }
      state.extMounted = state.view;
      $("#mainContent").innerHTML = extView.render();
      $$("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
      try { extView.mount?.($("#mainContent")); } catch (_) { /* 同上 */ }
      if (focus) { $("#mainContent").focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: "smooth" }); }
      return;
    }
    if (state.extMounted) {
      try { window.YingjiExtViews?.[state.extMounted]?.unmount?.(); } catch (_) { /* 同上 */ }
      state.extMounted = "";
    }
    $("#mainContent").innerHTML = renders[state.view]();
    if (!focus) $(".view", $("#mainContent"))?.classList.add("no-entry-animation");
    $$("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === state.view));
    if (state.view === "tasks") {
      $("#taskStatus").value = state.taskStatus;
      $("#taskSearch")?.addEventListener("input", (e) => {
        state.taskSearch = e.target.value;
        refreshTaskLiveRegions();
      });
      $("#taskStatus")?.addEventListener("change", (e) => {
        state.taskStatus = e.target.value;
        refreshTaskLiveRegions();
      });
      $("#taskBatch")?.addEventListener("change", (e) => {
        state.selectedBatch = e.target.value;
        refreshTaskLiveRegions();
      });
    }
    if (state.view === "download") bindDownload();
    if (state.view === "batch") bindBatch();
    if (state.view === "settings") {
      $("#pollingSwitch")?.addEventListener("change", togglePolling);
      $("#themeSwitch")?.addEventListener("change", toggleTheme);
      $$("[data-settings-section]").forEach((button) => button.addEventListener("click", () => {
        state.settingsSection = button.dataset.settingsSection;
        renderView(false);
        $(`[data-settings-section="${state.settingsSection}"]`)?.focus();
      }));
      $("#recheckNetwork")?.addEventListener("click", async () => {
        await loadSystemStatus(true);
        toast(state.systemLoadFailed ? "检查未完成，请稍后重试" : "已重新检查本机状态", state.systemLoadFailed ? "error" : "success");
      });
      $("#retrySystemStatus")?.addEventListener("click", async () => {
        await loadSystemStatus(true);
        toast(state.systemLoadFailed ? "仍无法读取，请稍后重试" : "系统状态已恢复", state.systemLoadFailed ? "error" : "success");
      });
      $("#manageCookies")?.addEventListener("click", () => openCookieModal());
    }
    if (focus) { $("#mainContent").focus({ preventScroll: true }); window.scrollTo({ top: 0, behavior: "smooth" }); }
  }

  function bindDownload() {
    const url = $("#mediaUrl");
    const start = $("#startDownload");
    const detect = $("#detectInfo");
    url?.addEventListener("input", () => {
      const valid = /^https?:\/\//i.test(url.value.trim());
      start.disabled = !valid; detect.disabled = !valid;
    });
    url?.addEventListener("keydown", (e) => e.key === "Enter" && !start.disabled && start.click());
    detect?.addEventListener("click", detectDownloadInfo);
    start?.addEventListener("click", createDownload);
    $("#browseDir")?.addEventListener("click", async () => {
      const button = $("#browseDir");
      button.disabled = true;
      button.textContent = "选择中…";
      try {
        const dir = (await requestDownloadDirectory()).trim();
        if (dir) $("#savePath").value = dir;
      } finally {
        button.disabled = false;
        button.textContent = "浏览…";
      }
    });
    resetFormatSelections();
    $("#videoQuality")?.addEventListener("change", onVideoChange);
    $("#audioTrack")?.addEventListener("change", onAudioChange);
    $("#mergeAudio")?.addEventListener("change", onMergeToggle);
  }

  async function detectDownloadInfo() {
    const url = $("#mediaUrl").value.trim();
    const button = $("#detectInfo");
    const controller = new AbortController();
    const startedAt = Date.now();
    let nameDone = false;
    let formatsDone = false;
    const updateLabel = () => {
      const elapsed = Math.max(1, Math.floor((Date.now() - startedAt) / 1000));
      if (!nameDone) button.textContent = `正在识别名称 · ${elapsed}s`;
      else if (!formatsDone) button.textContent = `正在读取格式 · ${elapsed}s`;
      else button.textContent = "正在整理结果…";
    };
    button.disabled = true;
    updateLabel();
    const labelTimer = setInterval(updateLabel, 1000);
    const requestTimeout = setTimeout(() => controller.abort(), 25000);
    try {
      const [nameResult, formatResult] = await Promise.allSettled([
        api("/detect-name", { method: "POST", body: { url }, signal: controller.signal })
          .finally(() => { nameDone = true; updateLabel(); }),
        api("/formats", { method: "POST", body: { url }, signal: controller.signal })
          .finally(() => { formatsDone = true; updateLabel(); }),
      ]);
      const nameData = nameResult.status === "fulfilled" ? nameResult.value : null;
      const formatData = formatResult.status === "fulfilled" ? formatResult.value : null;
      const hasName = Boolean(nameData?.name);
      const hasFormats = Boolean(formatData?.ok !== false && formatData);
      if (hasName && !$("#outputName").value) $("#outputName").value = `${nameData.name}.mp4`;
      if (hasFormats) renderFormats(formatData);
      const total = hasFormats
        ? (formatData.video?.length || 0) + (formatData.combined?.length || 0) + (formatData.audio?.length || 0)
        : 0;
      if (hasName && hasFormats) toast(`已识别名称和 ${total} 个可用格式`, "success");
      else if (hasName) toast("已识别名称，格式暂不可用，可直接下载", "success");
      else if (hasFormats) toast(`已识别 ${total} 个可用格式，名称可手动填写`, "success");
      else toast("检测在 25 秒内未完成，可直接填写文件名下载", "error");
    } finally {
      clearInterval(labelTimer);
      clearTimeout(requestTimeout);
      button.disabled = false;
      button.textContent = "检测名称与可用格式";
    }
  }

  function formatLabelCombined(f) {
    const res = f.height ? `${f.height}p` : (f.note || "复合轨");
    return `${res} · ${f.ext || ""} · 含音轨`;
  }
  function formatLabelVideo(f) {
    const bits = [];
    if (f.height) bits.push(`${f.height}p`);
    if (f.fps) bits.push(`${f.fps}fps`);
    if (f.vcodec) bits.push(f.vcodec);
    if (f.dynamic_range) bits.push(f.dynamic_range);
    if (f.tbr) bits.push(`${Math.round(f.tbr)}k`);
    return bits.join(" · ") || (f.note || "视频轨");
  }
  function formatLabelAudio(f) {
    const bits = [];
    if (f.abr) bits.push(`${Math.round(f.abr)}k`);
    if (f.acodec) bits.push(f.acodec);
    if (f.language) bits.push(f.language);
    return bits.join(" · ") || (f.note || "音轨");
  }

  function renderFormats(data) {
    formatData = data || {};
    const video = formatData.video || [];
    const combined = formatData.combined || [];
    const audio = formatData.audio || [];
    const q = $("#videoQuality");
    const a = $("#audioTrack");
    if (!q) return;

    q.innerHTML = '<option value="">自动选择最佳画质</option>'
      + combined.map((f) => `<option value="${esc(f.id)}" data-combined="1">${esc(formatLabelCombined(f))}</option>`).join("")
      + video.map((f) => `<option value="${esc(f.id)}">${esc(formatLabelVideo(f))}</option>`).join("");

    a.innerHTML = '<option value="">不单独指定（自动配对最佳音轨）</option>'
      + audio.map((f) => `<option value="${esc(f.id)}">${esc(formatLabelAudio(f))}</option>`).join("");

    // 默认选中：复合轨优先，否则最高画质纯视频；音轨默认最佳
    if (combined.length) q.value = combined[0].id;
    else if (video.length) q.value = video[0].id;
    const bestAudio = formatData.best_audio_id || (audio[0] && audio[0].id) || "";
    if (bestAudio && audio.length) a.value = bestAudio;

    onVideoChange();
  }

  function onVideoChange() {
    const q = $("#videoQuality");
    const audioBlock = $("#audioBlock");
    const merge = $("#mergeAudio");
    if (!q) return;
    const opt = q.selectedOptions[0];
    const isCombined = opt && opt.dataset.combined === "1";
    if (audioBlock) audioBlock.style.display = isCombined ? "none" : "";
    if (merge) merge.disabled = isCombined;
    updateFormatHint();
  }
  function onAudioChange() { updateFormatHint(); }
  function onMergeToggle() { updateFormatHint(); }

  function computeFormatId() {
    const data = formatData || {};
    const q = $("#videoQuality");
    const a = $("#audioTrack");
    const merge = $("#mergeAudio");
    if (!q) return "";
    const videoId = q.value;
    if (!videoId) return ""; // 留空 = 走后端默认最佳（已含音轨）
    const opt = q.selectedOptions[0];
    const isCombined = opt && opt.dataset.combined === "1";
    if (isCombined) return videoId; // 复合轨本身已含音
    const mergeOn = merge && merge.checked;
    if (!mergeOn) return videoId; // 仅视频，不合并
    const audioId = (a && a.value) || data.best_audio_id || "";
    return audioId ? `${videoId}+${audioId}` : videoId;
  }

  function updateFormatHint() {
    const hint = $("#formatHint");
    if (!hint) return;
    const fid = computeFormatId();
    hint.textContent = fid ? `将下载格式：${fid}` : "将使用默认最佳格式（自动含音轨）";
  }

  function resetFormatSelections() {
    formatData = null;
    const q = $("#videoQuality");
    const a = $("#audioTrack");
    const m = $("#mergeAudio");
    if (q) q.innerHTML = '<option value="">自动选择最佳画质</option>';
    if (a) a.innerHTML = '<option value="">不单独指定（自动配对最佳音轨）</option>';
    if (m) m.checked = true;
    updateFormatHint();
  }

  // 新任务添加后：只展开进行中任务、折叠其余，把进度重点突出到最前
  function focusActiveTasks() {
    const ACTIVE = ["downloading", "queued", "running", "processing"];
    state.expandedTasks = new Set(
      state.tasks.filter((t) => ACTIVE.includes(t.status)).map((t) => t.id),
    );
  }

  async function createDownload() {
    const button = $("#startDownload");
    button.disabled = true; button.textContent = "创建中…";
    // 留空时由后端写入 Portable 自带 Downloads；只有明确点击“浏览”才弹系统窗口。
    const outputDir = ($("#savePath").value || "").trim();
    button.textContent = "正在创建…";
    try {
      const result = await api("/download", { method: "POST", body: {
        url: $("#mediaUrl").value.trim(), output_name: $("#outputName").value.trim(),
        output_dir: outputDir, format_id: computeFormatId(),
      }});
      toast(result.queued ? "任务已加入等待队列" : "下载任务已开始", "success");
      await Promise.all([loadTasks(false), loadGroups()]);
      focusActiveTasks();
      state.view = "tasks"; renderView();
    } catch (error) { toast("未能创建下载任务，请检查链接后重试", "error"); button.disabled = false; button.textContent = "开始下载"; }
  }

  async function requestDownloadDirectory() {
    if (directoryPickerBusy) {
      toast("目录选择窗口已经打开", "error");
      return "";
    }
    directoryPickerBusy = true;
    try {
      const data = await api("/choose-dir", { method: "POST" });
      return data.path || "";
    } catch (error) { toast("无法打开目录选择，请稍后重试", "error"); }
    finally { directoryPickerBusy = false; }
    return "";
  }

  function bindBatch() {
    $("#importWorkbenchFromBatch")?.addEventListener("click", () => $("#batchExcelFile").click());
    $("#batchExcelFile")?.addEventListener("change", importWorkbenchExcel);
    $("#importWorkbench")?.addEventListener("click", () => $("#workbenchFile").click());
    $("#workbenchFile")?.addEventListener("change", importWorkbenchExcel);
    $("#sendToWorkbench")?.addEventListener("click", sendSelectedToWorkbench);
    $("#inspectBatchSelected")?.addEventListener("click", inspectBatchSelected);
    $("#detectPlaylist")?.addEventListener("click", detectPlaylist);
    $("#toggleBatchAll")?.addEventListener("click", () => {
      const allSelected = state.batchEntries.every((x) => x.selected !== false);
      state.batchEntries.forEach((x) => x.selected = !allSelected); renderView(false);
    });
    $$(".batch-check").forEach((input) => input.addEventListener("change", () => {
      state.batchEntries[Number(input.dataset.index)].selected = input.checked;
      $("#batchSelectedText").textContent = `已选择 ${state.batchEntries.filter((x) => x.selected !== false).length} 条`;
    }));
    $("#createBatch")?.addEventListener("click", createBatch);
    $("#workbenchDedup")?.addEventListener("change", (event) => {
      state.workbenchDedup = event.target.checked;
      renderView(false);
    });
    $("#workbenchCheckAll")?.addEventListener("change", (event) => {
      visibleWorkbenchRows().forEach(({ row }) => { row.selected = event.target.checked; });
      renderView(false);
    });
    $$(".workbench-check").forEach((input) => input.addEventListener("change", () => {
      const index = Number(input.closest("[data-workbench-index]").dataset.workbenchIndex);
      state.workbenchRows[index].selected = input.checked;
      updateWorkbenchSelectionSummary();
    }));
    $$("[data-workbench-field]").forEach((input) => input.addEventListener("input", () => {
      const index = Number(input.closest("[data-workbench-index]").dataset.workbenchIndex);
      state.workbenchRows[index][input.dataset.workbenchField] = input.value;
    }));
    $$("[data-workbench-remove]").forEach((button) => button.addEventListener("click", () => {
      state.workbenchRows.splice(Number(button.dataset.workbenchRemove), 1);
      renderView(false);
    }));
    $$("[data-workbench-action]").forEach((button) => button.addEventListener("click", () => {
      editWorkbenchRows(button.dataset.workbenchAction);
    }));
    $("#exportWorkbench")?.addEventListener("click", exportWorkbench);
    $("#queueWorkbench")?.addEventListener("click", queueWorkbench);
    $("#workbenchGroup")?.addEventListener("change", (event) => {
      state.workbenchGroupId = event.target.value;
    });
    $("#refreshDetectRecords")?.addEventListener("click", () => loadDetectRecords(true));
    $("#recordsSearch")?.addEventListener("input", (event) => {
      state.recordsSearch = event.target.value;
      const query = state.recordsSearch.trim().toLowerCase();
      let visible = 0;
      $$(".record-card").forEach((card) => {
        const match = !query || card.textContent.toLowerCase().includes(query);
        card.hidden = !match;
        if (match) visible += 1;
      });
      const summary = $(".records-toolbar p");
      if (summary) summary.textContent = `共 ${state.detectRecords.length} 条记录 · 当前显示 ${visible} 条`;
    });
    $$("[data-record-load]").forEach((button) => button.addEventListener("click", () => {
      loadRecordToWorkbench(button.dataset.recordLoad);
    }));
    $$("[data-record-delete]").forEach((button) => button.addEventListener("click", () => {
      deleteDetectRecord(button.dataset.recordDelete);
    }));
    /* 表头全选复选框的三态视觉：HTML indeterminate 属性无效，必须用 JS 显式设置 */
    const headerCheck = $("#workbenchCheckAll");
    if (headerCheck) headerCheck.indeterminate = headerCheck.hasAttribute("data-partial");
  }

  function updateWorkbenchSelectionSummary() {
    const visible = visibleWorkbenchRows();
    const selected = visible.filter(({ row }) => row.selected).length;
    if ($("#workbenchSummaryTop")) $("#workbenchSummaryTop").textContent = `当前 ${visible.length} 行 · 已选择 ${selected} 行`;
    if ($("#workbenchSummaryBottom")) $("#workbenchSummaryBottom").textContent = `共 ${visible.length} 行 · 已选 ${selected}`;
  }

  async function inspectBatchSelected() {
    const targets = state.batchEntries
      .map((entry, i) => ({ entry, i }))
      .filter(({ entry }) => entry.selected !== false && entry.url);
    if (!targets.length) return toast("请先勾选要检测的内容", "error");
    const btn = $("#inspectBatchSelected");
    btn?.setAttribute("disabled", "true");
    targets.forEach(({ i }) => { state.batchEntries[i].inspecting = true; });
    renderView(false);
    try {
      const data = await api("/batch-inspect", {
        method: "POST",
        body: { urls: targets.map(({ entry }) => entry.url) },
      });
      const items = (data && data.items) || [];
      let inspected = 0;
      targets.forEach(({ i }, idx) => {
        const entry = state.batchEntries[i];
        entry.inspecting = false;
        const info = items[idx];
        if (info) {
          if (info.filename) entry.filename = info.filename;
          if (info.ext) entry.ext = info.ext;
          if (info.filesize) entry.filesize = info.filesize;
          if (info.source) entry.inspectSource = info.source;
          entry.inspectError = "";
          inspected += 1;
        } else {
          entry.inspectError = "未拿到结果";
        }
      });
      toast(`已检测 ${inspected} / ${targets.length} 条内容`, inspected === targets.length ? "success" : "error");
      // 检测名/格式成功后，把 filename/ext 补存进检测记录（与初次检测同源同量，后端按 sourceUrl+count 去重置换，不产生重复）
      if (inspected > 0 && state.batchSourceUrl) {
        void saveDetectRecord(state.batchSourceUrl, { title: state.batchTitle, entries: state.batchEntries });
      }
    } catch (error) {
      targets.forEach(({ i }) => {
        state.batchEntries[i].inspecting = false;
        state.batchEntries[i].inspectError = (error && error.message) || "检测失败";
      });
      toast("检测未完成，请稍后重试", "error");
    } finally {
      btn?.removeAttribute("disabled");
      renderView(false);
    }
  }

  function sendSelectedToWorkbench() {
    const rows = state.batchEntries
      .filter((entry) => entry.selected !== false)
      .map((entry) => ({
        url: entry.url || "",
        filename: entry.filename || entry.title || "",
        ext: entry.ext || "",
        output_dir: "",
        selected: true,
      }));
    if (!rows.length) return toast("请先选择要发送到编辑台的内容", "error");
    const known = new Set(state.workbenchRows.map((row) => normalizeUrl(row.url)));
    rows.forEach((row) => {
      if (!known.has(normalizeUrl(row.url))) state.workbenchRows.push(row);
    });
    state.batchMode = "workbench";
    renderView();
    toast(`已发送 ${rows.length} 条内容到编辑台`, "success");
  }

  async function importWorkbenchExcel(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    const body = new FormData();
    body.append("file", file);
    try {
      const data = await api("/import/excel", { method: "POST", body });
      const rows = (data.rows || []).map((row) => ({
        url: row.url || "",
        filename: row.filename || "",
        output_dir: row.output_dir || "",
        selected: true,
      }));
      if (!rows.length) throw new Error("Excel 中没有可用的链接行");
      state.workbenchRows = rows;
      state.batchMode = "workbench";
      renderView();
      toast(`已导入 ${rows.length} 行到编辑台`, "success");
    } catch (error) {
      toast("导入未完成，请检查文件内容", "error");
    } finally {
      event.target.value = "";
    }
  }

  function selectedWorkbenchRows(fallbackToAll = false) {
    const visible = visibleWorkbenchRows().map(({ row }) => row);
    const selected = visible.filter((row) => row.selected);
    return selected.length || !fallbackToAll ? selected : visible;
  }

  function editWorkbenchRows(action) {
    if (action === "apply-dir-selected") {
      const dir = ($("#workbenchDir")?.value || "").trim();
      if (!dir) return toast("请先填写下载路径", "error");
      const targets = selectedWorkbenchRows(false);
      if (!targets.length) return toast("请先勾选要应用路径的行", "error");
      targets.forEach((row) => { row.output_dir = dir; });
      toast(`已为 ${targets.length} 行设置下载路径`, "success");
      renderView(false);
      return;
    }
    if (action === "apply-dir-all") {
      const dir = ($("#workbenchDir")?.value || "").trim();
      if (!dir) return toast("请先填写下载路径", "error");
      const targets = visibleWorkbenchRows().map(({ row }) => row);
      if (!targets.length) return toast("没有可应用的行", "error");
      targets.forEach((row) => { row.output_dir = dir; });
      toast(`已为全部 ${targets.length} 行设置下载路径`, "success");
      renderView(false);
      return;
    }
    const targets = selectedWorkbenchRows(true);
    if (!targets.length) return toast("编辑台中没有可操作的内容", "error");
    if (action === "prefix") {
      const prefix = $("#workbenchPrefix").value;
      if (!prefix) return toast("请先填写文件名前缀", "error");
      targets.forEach((row, index) => { row.filename = `${prefix}${String(index + 1).padStart(2, "0")}`; });
      toast(`已按前缀和序号重命名 ${targets.length} 行`, "success");
    } else if (action === "replace") {
      const find = $("#workbenchFind").value;
      const replacement = $("#workbenchReplace").value;
      if (!find) return toast("请先填写查找内容", "error");
      let changed = 0;
      targets.forEach((row) => {
        if (row.filename.includes(find)) {
          row.filename = row.filename.split(find).join(replacement);
          changed += 1;
        }
      });
      toast(changed ? `已替换 ${changed} 行文件名` : "没有找到匹配内容", changed ? "success" : "error");
    }
    renderView(false);
  }

  async function exportWorkbench() {
    const rows = visibleWorkbenchRows().map(({ row }) => ({
      url: row.url.trim(),
      filename: row.filename.trim(),
      output_dir: (row.output_dir || "").trim(),
    })).filter((row) => row.url);
    if (!rows.length) return toast("编辑台中没有可导出的内容", "error");
    try {
      const response = await fetch(`${API}/harvest/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows, name: state.batchTitle || "采集清单" }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.error || "导出失败");
      }
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `采集清单_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}.xlsx`;
      link.click();
      URL.revokeObjectURL(link.href);
      toast(`已导出 ${rows.length} 行`, "success");
    } catch (error) {
      toast("导出未完成，请稍后重试", "error");
    }
  }

  async function queueWorkbench() {
    const rows = selectedWorkbenchRows(false);
    if (!rows.length) return toast("请先勾选要加入队列的内容", "error");
    // 逐行路径：空白行回落到全局保存路径；存在空白且未设全局路径时才弹窗
    let globalDir = ($("#savePath")?.value || "").trim();
    const needDir = rows.some((r) => !(r.output_dir || "").trim());
    if (needDir && !globalDir) {
      globalDir = (await requestDownloadDirectory()).trim();
      if (globalDir && $("#savePath")) $("#savePath").value = globalDir;
      if (!globalDir) return toast("存在未指定路径的内容，请先设置保存路径", "error");
    }
    const button = $("#queueWorkbench");
    button.disabled = true;
    button.textContent = "加入中…";
    button.textContent = "正在加入…";
    try {
      const data = await api("/batch-download", { method: "POST", body: {
        items: rows.map((row) => ({
          url: row.url.trim(),
          output_name: row.filename.trim(),
          output_dir: (row.output_dir || "").trim() || globalDir,
        })),
        output_dir: globalDir,
        group_id: state.workbenchGroupId,
      }});
      toast(`已加入 ${data.created} 个待下载任务${data.skipped ? `，跳过重复 ${data.skipped} 个` : ""}`, "success");
      await Promise.all([loadTasks(false), loadGroups()]);
      state.view = "tasks";
      renderView();
    } catch (error) {
      toast("任务未能加入队列，请稍后重试", "error");
      button.disabled = false;
      button.textContent = "加入下载队列";
    }
  }

  async function detectPlaylist() {
    const url = $("#playlistUrl").value.trim();
    if (!url) return toast("请先输入播放列表链接", "error");
    const button = $("#detectPlaylist"); button.disabled = true; button.textContent = "正在检测…";
    try {
      const data = await api("/playlist", { method: "POST", body: { url } });
      state.batchTitle = data.title; state.batchEntries = (data.entries || []).map((x) => ({ ...x, selected: true }));
      state.batchSourceUrl = url;
      state.batchStep = 2; renderView(); toast(`已识别 ${state.batchEntries.length} 条内容`, "success");
      void saveDetectRecord(url, data);
    } catch (error) { toast(`检测未完成：${error?.message || "请检查链接后重试"}`, "error"); button.disabled = false; button.textContent = "开始检测"; }
  }

  async function createBatch() {
    const selected = state.batchEntries.filter((x) => x.selected !== false);
    if (!selected.length) return toast("请至少选择一条内容", "error");
    const button = $("#createBatch"); button.disabled = true; button.textContent = "加入中…";
    let outputDir = ($("#savePath").value || "").trim();
    if (!outputDir) {
      outputDir = (await requestDownloadDirectory()).trim();
      if (outputDir) $("#savePath").value = outputDir;
    }
    button.textContent = "正在加入…";
    try {
      const data = await api("/batch-download", { method: "POST", body: {
        items: selected.map((x) => ({ url: x.url, title: x.title })),
        output_dir: outputDir,
        group_id: $("#batchGroup").value,
      }});
      toast(`已创建 ${data.created} 个任务${data.skipped ? `，跳过 ${data.skipped} 个重复项` : ""}`, "success");
      await Promise.all([loadTasks(false), loadGroups()]);
      focusActiveTasks();
      state.view = "tasks"; renderView();
    } catch (error) { toast("任务未能加入列表，请稍后重试", "error"); button.disabled = false; button.textContent = "加入待下载列表"; }
  }

  async function taskAction(taskId, action) {
    if (!action) return;
    const task = state.tasks.find((x) => String(x.id) === String(taskId));
    try {
      if (action === "cancel" && !(await confirmAction("取消下载", `取消“${task?.display_name || "当前任务"}”？已下载的断点文件会保留。`, "取消任务"))) return;
      if (action === "delete") {
        const mode = await chooseDeleteMode(task);
        if (mode === "cancel") return;
        const query = mode === "files" ? "?delete_files=true" : "";
        await api(`/tasks/${encodeURIComponent(taskId)}${query}`, { method: "DELETE" });
        toast(mode === "files" ? "任务与本地文件已处理" : "已删除任务记录，本地文件保留", "success");
      } else if (action === "confirm") {
        const result = await chooseConfirmResult(task);
        if (!result || result === "cancel") return;
        const data = await api(`/tasks/${encodeURIComponent(taskId)}/confirm`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ result }),
        });
        toast(result === "complete" ? "已记录为「人工确认保留」" : "已确认为失败", "success");
      } else {
        const data = await api(`/tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST" });
        if (action === "open-file") {
          toast(data.fallback === "folder" ? "未找到视频文件，已打开所在文件夹" : "已调用系统播放器", "success");
        } else if (action === "open-folder") {
          toast("已打开文件所在位置", "success");
        } else if (action === "cleanup") {
          toast(data.message || "已清理任务残留", "success");
        } else {
          toast("任务状态已更新", "success");
        }
      }
      await loadTasks(false);
      if (state.view === "tasks") refreshTaskLiveRegions();
      else if (state.view === "download") refreshDownloadLiveRegions();
    } catch (error) { toast("操作未完成，请稍后重试", "error"); }
  }

  function setView(view) {
    state.view = view;
    if (view === "data") loadStats(false).finally(() => renderView());
    else if (view === "settings" && !state.system) {
      renderView();
      loadSystemStatus(true);
    } else renderView();
  }

  // R-1：从数据中心「查看任务」进入任务中心，并同步批次筛选器
  async function goToBatchTasks(batchId) {
    state.selectedBatch = String(batchId);
    state.view = "tasks";
    await loadTasks(false);
    renderView();
    $("#taskBatch")?.focus();
  }

  function togglePolling(event) {
    state.polling = event.target.checked;
    localStorage.setItem("yingji_alt_polling", state.polling ? "on" : "off");
    schedulePolling();
    toast(state.polling ? "已开启任务自动刷新" : "已关闭任务自动刷新", "success");
  }
  function toggleTheme(event) {
    state.theme = event.target.checked ? "light" : "dark";
    localStorage.setItem("yingji_alt_theme", state.theme);
    applyTheme();
    toast(state.theme === "light" ? "已切换为亮色主题" : "已切换为暗色主题", "success");
  }
  function schedulePolling() {
    clearInterval(state.timer);
    if (state.polling) state.timer = setInterval(() => {
      if (!document.hidden) loadTasks(true);
    }, 2500);
  }

  document.addEventListener("click", (event) => {
    const viewButton = event.target.closest("[data-view],[data-go]");
    if (viewButton) return setView(viewButton.dataset.view || viewButton.dataset.go);
    // R-1：数据中心「查看任务」复用现有批次筛选进入任务中心
    const batchViewBtn = event.target.closest("[data-batch-view]");
    if (batchViewBtn) {
      event.preventDefault();
      void goToBatchTasks(batchViewBtn.dataset.batchView);
      return;
    }
    // R-1：数据中心读取失败后的重新加载
    const reloadStatsBtn = event.target.closest("[data-reload-stats]");
    if (reloadStatsBtn) {
      event.preventDefault();
      void loadStats(true);
      return;
    }
    const batchModeButton = event.target.closest("[data-batch-mode]");
    if (batchModeButton) {
      state.batchMode = batchModeButton.dataset.batchMode;
      renderView();
      if (state.batchMode === "records" && !state.recordsLoaded) void loadDetectRecords(true);
      return;
    }
    const stepButton = event.target.closest("[data-batch-step]");
    if (stepButton) {
      const next = Number(stepButton.dataset.batchStep);
      if (next > 1 && !state.batchEntries.length) return toast("请先检测播放列表", "error");
      state.batchStep = next; return renderView();
    }
    const actionButton = event.target.closest("[data-task-action]");
    if (actionButton) {
      const taskEl = actionButton.closest("[data-task-id]");
      if (!taskEl) return;
      const tid = taskEl.dataset.taskId;
      // 「移动到分组」走弹层选择目标，不接 taskAction 的后端接口映射
      if (actionButton.dataset.taskAction === "move") return openTaskMoveMenu(actionButton, tid);
      return taskAction(tid, actionButton.dataset.taskAction);
    }

    // 分组「...」操作菜单
    const groupMenuBtn = event.target.closest("[data-group-menu]");
    if (groupMenuBtn) {
      const gid = groupMenuBtn.dataset.groupMenu;
      if (activePop && activePop.dataset.forGroup === gid) closePop();
      else openGroupMenu(groupMenuBtn, gid);
      return;
    }
    // 批量操作栏按钮
    const batchBtn = event.target.closest("[data-batch-action]");
    if (batchBtn) {
      const action = batchBtn.dataset.batchAction;
      if (action === "select-all") return selectAllVisible();
      if (action === "clear") { state.selectedTaskIds.clear(); refreshTaskLiveRegions(); return; }
      const ids = [...state.selectedTaskIds];
      if (!ids.length) return;
      if (action === "retry") return batchRetry(ids);
      if (action === "start") return batchStart(ids);
      if (action === "delete") return batchDelete(ids);
      if (action === "move") return openMoveTasksMenu(batchBtn, ids);
    }

    // 任务行：点击整行（避开操作按钮/链接/URL 展开/选择框）展开或收起详情
    const taskRowEl = event.target.closest(".task-row");
    if (taskRowEl && !event.target.closest("[data-task-action]") && !event.target.closest("a") && !event.target.closest("[data-task-url-toggle]") && !event.target.closest(".task-check")) {
      const id = taskRowEl.dataset.taskId;
      if (state.expandedTasks.has(id)) state.expandedTasks.delete(id);
      else state.expandedTasks.add(id);
      taskRowEl.classList.toggle("expanded");
      return;
    }
    // 详情内「显示/收起完整链接」切换
    const urlToggle = event.target.closest("[data-task-url-toggle]");
    if (urlToggle) {
      const id = urlToggle.dataset.taskUrlToggle;
      if (state.expandedUrls.has(id)) state.expandedUrls.delete(id);
      else state.expandedUrls.add(id);
      refreshTaskLiveRegions();
      return;
    }
    // 分组树折叠/展开（以状态为准重新渲染，避免折叠态错位）
    const groupToggle = event.target.closest("[data-group-toggle]");
    if (groupToggle) {
      const gid = groupToggle.dataset.groupToggle;
      if (state.collapsedGroups.has(gid)) state.collapsedGroups.delete(gid);
      else state.collapsedGroups.add(gid);
      refreshTaskLiveRegions();
      return;
    }
    // 紧凑 / 卡片 视图切换
    const viewBtn = event.target.closest("[data-view-mode]");
    if (viewBtn) {
      state.viewMode = viewBtn.dataset.viewMode;
      try { localStorage.setItem("yingji_view_mode", state.viewMode); } catch {}
      refreshTaskLiveRegions();
      return;
    }

    if (event.target.closest("#refreshTasks")) loadTasks(true);
    if (event.target.closest("[data-show-archived]")) { toggleShowArchived(); return; }
    if (event.target.closest("[data-new-group]")) { void newGroup(); return; }
    if (event.target.closest("[data-export-excel]")) { exportExcel(); return; }
    if (event.target.closest("[data-refresh-tasks]")) { refreshTasks(); return; }

    // UI-P0-05：异常恢复卡片（重新检查 / 暂不处理提示型异常）
    // 只做异常区域局部更新，不重建整个下载页，避免清空用户已填写的表单
    const anomalyRetry = event.target.closest("[data-anomaly-retry]");
    if (anomalyRetry) { void runCapabilityCheck(); return; }
    const anomalyDismiss = event.target.closest("[data-anomaly-dismiss]");
    if (anomalyDismiss) {
      state.dismissedAnomalies.add(anomalyDismiss.dataset.anomalyDismiss);
      updateAnomalyRegion();
      return;
    }
    // UI-P0-04：登录凭据（Cookie）应用内管理入口
    const cookieManage = event.target.closest("[data-cookie-manage]");
    if (cookieManage) { openCookieModal(); return; }
    // UI-P0-05：设置中「重新查看新手引导」（记录触发按钮，关闭时焦点返回）
    const replayBtn = event.target.closest("#replayOnboarding");
    if (replayBtn) { openOnboarding(true, replayBtn); void runCapabilityCheck(); return; }
  });
  // 任务选择框：同步到 state.selectedTaskIds 并更新批量操作栏
  document.addEventListener("change", (event) => {
    const check = event.target.closest("[data-task-check]");
    if (!check) return;
    const id = check.dataset.taskCheck;
    if (check.checked) state.selectedTaskIds.add(id);
    else state.selectedTaskIds.delete(id);
    updateBatchBar();
  });
  // 点击浮动菜单之外 / 按 Esc 时关闭弹层（触发器与菜单内部不关闭）
  document.addEventListener("click", (event) => {
    if (!activePop) return;
    if (activePop.contains(event.target)) return;
    if (event.target.closest("[data-group-menu]")) return;
    if (event.target.closest('[data-batch-action="move"]')) return;
    closePop();
  }, true);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closePop(); });
  document.addEventListener("visibilitychange", () => !document.hidden && state.polling && loadTasks(true));

  // 全局轻量 tooltip：悬浮带 data-tip 的元素时显示原因气泡（挂在 body，不受卡片 overflow 裁切）
  let tipEl = null;
  document.addEventListener("mouseover", (event) => {
    const t = event.target.closest("[data-tip]");
    if (!t) return;
    const text = t.getAttribute("data-tip");
    if (!text) return;
    if (!tipEl) { tipEl = document.createElement("div"); tipEl.className = "tt-tip"; document.body.appendChild(tipEl); }
    tipEl.textContent = text;
    const r = t.getBoundingClientRect();
    tipEl.style.left = Math.min(window.innerWidth - 270, r.left) + "px";
    tipEl.style.top = (r.bottom + 6) + "px";
    tipEl.classList.add("show");
  });
  document.addEventListener("mouseout", (event) => {
    if (event.target.closest("[data-tip]") && tipEl) tipEl.classList.remove("show");
  });

  // ===== UI-P0-05：首次启动引导 + 能力异常恢复 =====
  const ONBOARDING_KEY = "yingji_onboarding_v1";
  const ONBOARDING_DONE = "done";
  const ONBOARDING_LATER = "later";

  function shouldShowOnboarding() {
    try { return !localStorage.getItem(ONBOARDING_KEY); } catch { return false; }
  }

  // 打开引导的触发元素（如设置页「重新查看」按钮），关闭时用于焦点返回
  let onboardingTrigger = null;
  // 阻断状态下「查看解决方法」的展开标记（仅当前弹窗内有效）
  let onboardingShowSolutions = false;

  function openOnboarding(force = false, trigger = null) {
    if (!force && !shouldShowOnboarding()) return;
    onboardingTrigger = trigger || null;
    onboardingShowSolutions = false;
    state.onboardingStep = 1;
    renderOnboardingModal();
  }

  // 能力检查五态：loading（检查中）/ ready（就绪）/ advisory（非阻断提示）/
  // blocking（组件或保存位置阻断）/ unavailable（接口读取失败）
  function capabilityStatus() {
    const cap = state.capability;
    if (!cap) return "loading";
    if (cap.error) return "unavailable";
    if (cap.items.some((it) => it.level === "error")) return "blocking";
    if (cap.items.some((it) => it.level !== "ok")) return "advisory";
    return "ready";
  }

  function renderOnboardingModal(focusSelector = "") {
    const root = $("#modalRoot");
    root.innerHTML = onboardingModalHtml(state.onboardingStep);
    const modal = $(".onb-modal", root);
    trapFocus(modal);
    bindOnboardingButtons(root);
    if (focusSelector) {
      // 部分状态（如第2步逐项列表）没有状态标题元素，回落到步骤主体容器，
      // 保证「重新检查」后键盘焦点始终落在最新状态区域
      const focusTarget = $(focusSelector, root) || $("#onboardingStepBody", root);
      focusTarget?.focus();
    }
  }

  function onboardingModalHtml(step) {
    const total = 3;
    const titles = { 1: "认识影迹", 2: "检查使用条件", 3: "开始使用" };
    const status = capabilityStatus();
    const dots = Array.from({ length: total }, (_, i) =>
      `<span class="onb-dot${i + 1 === step ? " active" : ""}" aria-hidden="true"></span>`).join("");
    let body;
    if (step === 1) body = onboardingStep1Body();
    else if (step === 2) body = `<ul class="onb-checks" id="onboardingStepBody" tabindex="-1" aria-live="polite">${onboardingStep2Body()}</ul>`;
    else body = `<div id="onboardingStepBody" tabindex="-1" aria-live="polite">${onboardingStep3Body()}</div>`;
    const prev = step > 1 ? `<button class="button ghost" data-onboarding-prev>上一步</button>` : "";
    const skip = `<button class="button ghost" data-onboarding-skip>稍后再看</button>`;
    // 底部主操作随能力状态变化：检查未完成或读取失败时，不提供可绕过检查的完成入口
    let primary;
    if (step === 1) {
      primary = `<button class="button primary" data-onboarding-next>下一步</button>`;
    } else if (step === 2) {
      if (status === "loading") primary = `<button class="button primary" disabled>正在检查…</button>`;
      else if (status === "unavailable") primary = `<button class="button primary" data-onboarding-recheck>重新检查</button>`;
      else primary = `<button class="button primary" data-onboarding-next>下一步</button>`;
    } else if (status === "ready" || status === "advisory") {
      primary = `<button class="button primary" data-onboarding-finish>开始第一次下载</button>`;
    } else if (status === "loading") {
      primary = `<button class="button primary" disabled>正在检查…</button>`;
    } else {
      const solutions = status === "blocking"
        ? `<button class="button ghost" data-onboarding-solutions>${onboardingShowSolutions ? "收起解决方法" : "查看解决方法"}</button>`
        : "";
      primary = `${solutions}<button class="button primary" data-onboarding-recheck>重新检查</button>`;
    }
    return `<div class="modal-backdrop onb-backdrop">
      <section class="modal onb-modal" role="dialog" aria-modal="true" aria-labelledby="onbTitle">
        <div class="onb-head">
          <div class="onb-dots" aria-hidden="true">${dots}</div>
          <button class="onb-close" type="button" data-onboarding-skip aria-label="跳过新手引导">×</button>
        </div>
        <h2 id="onbTitle">${esc(titles[step])}</h2>
        <div class="onb-body">${body}</div>
        <div class="onb-actions">${prev}${skip}${primary}</div>
      </section></div>`;
  }

  function onboardingStep1Body() {
    return `<p class="onb-lead">影迹是一款在本机运行的媒体采集与下载管理工具。</p>
      <ul class="onb-points">
        <li><b>粘贴链接即可下载</b><span>支持 B 站、YouTube、抖音、公开 M3U8 等，其它站点尽力兼容。</span></li>
        <li><b>批量与采集</b><span>主页、合集与多链接可用批量工作台，支持导入 Excel 与逐条编辑。</span></li>
        <li><b>数据只留在本机</b><span>任务记录、设置与下载文件都保存在你的设备上，不会上传给开发者。</span></li>
      </ul>
      <p class="onb-note">下一步会检查本机的使用条件；仅检查本机使用条件，不读取任务内容或登录凭据，也不会修改程序设置。</p>`;
  }

  function onboardingStep2Body() {
    const status = capabilityStatus();
    if (status === "loading") {
      return `<li class="onb-check loading"><b id="onbStatusTitle" tabindex="-1">正在检查本机使用条件……</b></li>`;
    }
    if (status === "unavailable") {
      return `<li class="onb-check warn"><b id="onbStatusTitle" tabindex="-1">暂时无法读取使用条件</b>
        <p>请点击下方「重新检查」再试一次；也可以先关闭引导，稍后在「设置 → 常规 → 重新查看新手引导」中再看。</p></li>`;
    }
    return state.capability.items.map((it) => {
      const label = it.level === "ok" ? "已就绪" : (it.level === "info" ? "可选" : "需注意");
      const tone = it.level === "ok" ? "ok" : (it.level === "info" ? "info" : "warn");
      return `<li class="onb-check ${tone}">
        <b>${esc(it.title)}</b><span class="onb-check-status">${label}</span>
        <p>${esc(it.what)}</p>
      </li>`;
    }).join("");
  }

  function onboardingStep3Body() {
    const status = capabilityStatus();
    if (status === "loading") {
      return `<p class="onb-lead" id="onbStatusTitle" tabindex="-1">正在检查本机使用条件……</p>
        <p class="onb-note">检查完成后即可开始使用。</p>`;
    }
    if (status === "unavailable") {
      return `<p class="onb-lead onb-status-bad" id="onbStatusTitle" tabindex="-1">暂时无法读取使用条件</p>
        <p class="onb-note">可点击「重新检查」再试一次；也可以先关闭引导稍后处理，读取成功前不会视为正常完成。</p>`;
    }
    if (status === "blocking") {
      const blockers = state.capability.items.filter((it) => it.level === "error");
      const solutions = onboardingShowSolutions
        ? `<ul class="onb-points onb-solutions">${blockers.map((it) =>
            `<li><b>${esc(it.title)}</b><span>${esc(it.advice)}</span></li>`).join("")}</ul>`
        : "";
      return `<p class="onb-lead onb-status-bad" id="onbStatusTitle" tabindex="-1">部分使用条件不可用</p>
        <ul class="onb-points onb-blockers">${blockers.map((it) =>
          `<li><b>${esc(it.title)}</b><span>${esc(it.what)}${it.impact ? " " + esc(it.impact) : ""}</span></li>`).join("")}</ul>
        ${solutions}
        <p class="onb-note">你可以先处理后点击「重新检查」；也可以先关闭引导，下载页会继续显示对应提示。</p>`;
    }
    const advisory = status === "advisory"
      ? `<p class="onb-note">暂未添加登录凭据。公开内容仍可正常尝试；需要登录的内容可稍后按帮助说明添加凭据。</p>`
      : "";
    return `<p class="onb-lead onb-status-ok" id="onbStatusTitle" tabindex="-1">使用条件已就绪</p>
      <ul class="onb-points">
        <li><b>从单个链接开始</b><span>在「下载」页粘贴链接，选择画质与音轨后即可下载。</span></li>
        <li><b>需要时再看引导</b><span>随时可在「设置 → 常规 → 重新查看新手引导」重新打开本引导。</span></li>
      </ul>${advisory}`;
  }

  function closeOnboarding(mark) {
    try { localStorage.setItem(ONBOARDING_KEY, mark); } catch {}
    const root = $("#modalRoot");
    if (root) root.innerHTML = "";
    state.onboardingStep = 0;
    onboardingShowSolutions = false;
    // 焦点返回：优先回到打开引导的触发按钮（如设置页「重新查看」）；
    // 首次启动自动打开时无触发按钮，焦点放到当前页面第一个合理的主操作
    const trigger = onboardingTrigger;
    onboardingTrigger = null;
    if (trigger && document.contains(trigger)) { trigger.focus(); return; }
    const main = $("#mediaUrl") || $("#mainContent button, #mainContent input, #mainContent select") || $("#mainContent");
    if (main && typeof main.focus === "function") main.focus();
  }

  // 正常 / 非阻断状态下的完成：关闭引导 → 进入「下载」页 → 聚焦链接输入框
  function finishOnboarding() {
    const status = capabilityStatus();
    if (status !== "ready" && status !== "advisory") return;
    try { localStorage.setItem(ONBOARDING_KEY, ONBOARDING_DONE); } catch {}
    const root = $("#modalRoot");
    if (root) root.innerHTML = "";
    state.onboardingStep = 0;
    onboardingTrigger = null;
    onboardingShowSolutions = false;
    state.view = "download";
    renderView();
    $("#mediaUrl")?.focus();
  }

  function bindOnboardingButtons(root) {
    root.onclick = (event) => {
      if (event.target.closest("[data-onboarding-next]")) { state.onboardingStep++; renderOnboardingModal(); }
      else if (event.target.closest("[data-onboarding-prev]")) { state.onboardingStep--; renderOnboardingModal(); }
      else if (event.target.closest("[data-onboarding-skip]")) { closeOnboarding(ONBOARDING_LATER); }
      else if (event.target.closest("[data-onboarding-finish]")) { finishOnboarding(); }
      else if (event.target.closest("[data-onboarding-solutions]")) { onboardingShowSolutions = !onboardingShowSolutions; renderOnboardingModal("#onbStatusTitle"); }
      else if (event.target.closest("[data-onboarding-recheck]")) { void runCapabilityCheck(); }
    };
  }

  function trapFocus(container) {
    const focusables = () => $$('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])', container)
      .filter((el) => !el.disabled && el.offsetParent !== null);
    const first = focusables()[0];
    if (first) first.focus();
    container.onkeydown = (event) => {
      if (event.key === "Escape") { event.preventDefault(); closeOnboarding(ONBOARDING_LATER); return; }
      if (event.key !== "Tab") return;
      const f = focusables();
      if (!f.length) return;
      const idx = f.indexOf(document.activeElement);
      if (event.shiftKey && idx <= 0) { event.preventDefault(); f[f.length - 1].focus(); }
      else if (!event.shiftKey && idx === f.length - 1) { event.preventDefault(); f[0].focus(); }
    };
  }

  let capabilityCheckSeq = 0;
  async function runCapabilityCheck() {
    const seq = ++capabilityCheckSeq;
    // 检查开始即进入「检查中」：旧结果（包括旧的成功结果）不再继续展示
    state.capability = null;
    if (state.onboardingStep >= 2) renderOnboardingModal();
    updateAnomalyRegion();
    try {
      const data = await api("/startup-status");
      if (seq !== capabilityCheckSeq) return;
      state.capability = mapCapability(data);
    } catch (err) {
      if (seq !== capabilityCheckSeq) return;
      // 读取失败：保持「暂时无法读取使用条件」，不得把失败当成正常完成
      state.capability = { error: true, items: [] };
    }
    // 引导打开时刷新弹窗（底部按钮随状态变化），并把焦点带到最新状态标题，
    // 方便键盘用户感知「重新检查」后的结果变化
    if (state.onboardingStep >= 2) renderOnboardingModal("#onbStatusTitle");
    // 下载页只做异常区域局部更新，不重建整个表单
    updateAnomalyRegion();
  }

  function mapCapability(data) {
    const items = [];
    const componentOk = Boolean(data.ffmpeg) && Boolean(data.yt_dlp);
    items.push({
      key: "component",
      title: "下载与合并组件",
      level: componentOk ? "ok" : "error",
      code: componentOk ? "" : "E-COMPONENT-MISSING",
      what: componentOk
        ? "解析、下载与合并所需的本机组件已就绪。"
        : "用于解析、下载与合并视频的本机组件未找到。",
      impact: componentOk ? "" : "大多数链接将无法完成下载。",
      advice: componentOk ? "" : "请在影迹完整安装包中确认组件存在，或重新安装影迹以恢复该功能。",
      retry: !componentOk,
    });
    // 默认保存位置：接口为兼容保留目录字段，但用户可见文案不展示内部完整路径
    const dirExists = data.download_dir_exists === undefined ? true : Boolean(data.download_dir_exists);
    const dirIsDir = data.download_dir_is_directory === undefined ? true : Boolean(data.download_dir_is_directory);
    const dirOk = Boolean(data.download_dir_writable);
    items.push({
      key: "saveDir",
      title: "默认保存位置",
      level: dirOk ? "ok" : "error",
      code: dirOk ? "" : "E-DIR-READONLY",
      what: dirOk ? "默认保存位置可正常使用。" : "默认保存位置当前不可用。",
      impact: dirOk ? "" : "未单独指定保存位置的下载会失败。",
      advice: dirOk ? "" : (!dirExists || !dirIsDir
        ? "默认保存位置不存在或已被占用，下载时可在「保存位置」处点「浏览」选择一个可用文件夹。"
        : "默认保存位置暂时无法写入，请检查权限，或在下载时用「保存位置 → 浏览」选择其他文件夹。"),
      retry: !dirOk,
    });
    const cookieOk = Boolean(data.cookies_loaded);
    items.push({
      key: "cookie",
      title: "登录凭据（Cookie）",
      level: cookieOk ? "ok" : "info",
      code: cookieOk ? "" : "W-COOKIE-MISSING",
      what: cookieOk
        ? "已加载登录凭据，可尝试需要登录的内容。"
        : "暂未添加登录凭据。公开内容仍可正常尝试。",
      impact: cookieOk ? "" : "需要登录的内容可能无法下载；公开内容不受影响。",
      advice: cookieOk ? "" : "如需访问必须登录的内容，请按帮助说明添加登录凭据；未添加凭据时仍可尝试公开内容。",
      retry: false,
    });
    return { items };
  }

  // 只更新下载页的异常卡片区域，不重建整个下载表单（保护用户未提交的输入）
  function updateAnomalyRegion() {
    const region = $("#capabilityAnomalyRegion");
    if (region) region.innerHTML = anomalyCardsHtml();
  }

  function anomalyCardsHtml() {
    const cap = state.capability;
    if (!cap) return "";
    if (cap.error) {
      // 读取失败：清空旧结果，如实提示暂时无法读取，并提供重新检查入口
      return `<div class="anomaly-list"><div class="anomaly-card level-warn" data-anomaly-key="status">
        <div class="anomaly-head"><b>暂时无法读取使用条件</b></div>
        <p class="anomaly-what">当前无法确认本机的使用条件，部分功能可能受影响；可稍后重新检查。</p>
        <div class="anomaly-actions"><button class="button" data-anomaly-retry="status">重新检查</button></div>
      </div></div>`;
    }
    const dismissed = state.dismissedAnomalies;
    const cards = cap.items.filter((it) => it.level !== "ok" && !(it.level === "info" && dismissed.has(it.key)));
    if (!cards.length) return "";
    return `<div class="anomaly-list">${cards.map(anomalyCardHtml).join("")}</div>`;
  }

  function anomalyCardHtml(it) {
    const isInfo = it.level === "info";
    const head = `<div class="anomaly-head"><b>${esc(it.title)}</b>${it.code ? `<span class="anomaly-code">${esc(it.code)}</span>` : ""}</div>`;
    const what = it.what ? `<p class="anomaly-what">${esc(it.what)}</p>` : "";
    const impact = (it.impact && !isInfo) ? `<p class="anomaly-impact">影响：${esc(it.impact)}</p>` : "";
    const advice = it.advice ? `<p class="anomaly-advice">建议：${esc(it.advice)}</p>` : "";
    const actions = isInfo
      ? `<div class="anomaly-actions">${it.key === "cookie" ? `<button class="button" data-cookie-manage>添加 Cookie</button>` : ""}<button class="button ghost" data-anomaly-dismiss="${esc(it.key)}">暂不处理</button></div>`
      : `<div class="anomaly-actions">${it.retry ? `<button class="button" data-anomaly-retry="${esc(it.key)}">重新检查</button>` : ""}</div>`;
    return `<div class="anomaly-card level-${esc(it.level)}" data-anomaly-key="${esc(it.key)}">${head}${what}${impact}${advice}${actions}</div>`;
  }
  // ===== /UI-P0-05 =====

  // UI-P0-04：登录凭据（Cookie）应用内粘贴管理
  function openCookieModal() {
    const root = $("#modalRoot");
    root.innerHTML = `<div class="modal-backdrop">
      <section class="modal" role="dialog" aria-modal="true" aria-labelledby="cookieTitle" style="max-width:580px">
        <h2 id="cookieTitle">登录凭据（Cookie）</h2>
        <p class="modal-note">应用内粘贴 · 即时生效 · 不需重启。凭据仅保存在你本机的 Data/cookies.txt，不会上传给任何第三方。</p>
        <div id="cookieStatus"></div>
        <div class="modal-actions" style="justify-content:flex-start">
          <button class="button ghost" id="cookieHelp">[帮助] 如何从浏览器导出</button>
        </div>
        <label style="display:block;font-size:13px;color:var(--color-text-secondary,#555);margin:8px 0 4px">粘贴 Netscape 格式 cookies.txt：</label>
        <textarea id="cookieText" placeholder="# Netscape HTTP Cookie File&#10;.bilibili.com  TRUE  /  FALSE  0  buvid3  xxxx&#10;.bilibili.com  TRUE  /  FALSE  0  SESSDATA  yyyy" style="width:100%;min-height:120px;font-family:var(--font-mono,monospace);font-size:12px;border:0.5px solid var(--color-border-secondary,#ccc);border-radius:8px;padding:8px;box-sizing:border-box"></textarea>
        <div id="cookieMsg" class="modal-note" style="min-height:18px"></div>
        <div class="modal-actions">
          <button class="button ghost" data-cookie-close>取消</button>
          <button class="button" id="cookieTest">测试</button>
          <button class="button primary" id="cookieSave">保存并启用</button>
        </div>
      </section></div>`;
    const ta = $("#cookieText", root);
    const msg = $("#cookieMsg", root);
    const statusEl = $("#cookieStatus", root);
    const renderStatus = (info) => {
      if (info && info.loaded) {
        const kb = (info.size_bytes / 1024).toFixed(1);
        const when = info.last_modified ? new Date(info.last_modified * 1000).toLocaleString() : "未知";
        statusEl.innerHTML = `<div class="modal-note" style="border-left:3px solid #2e7d32;padding-left:8px;color:#2e7d32">已加载 · ${kb} KB · 更新于 ${esc(when)} <button class="button ghost" id="cookieRemove" style="margin-left:8px">移除</button></div>`;
        $("#cookieRemove", statusEl)?.addEventListener("click", async () => {
          try { await api("/cookies", { method: "DELETE" }); toast("已移除 Cookie", "success"); await refreshCookieUI(); openCookieModal(); }
          catch (e) { toast(e.message || "移除失败", "error"); }
        });
      } else {
        statusEl.innerHTML = `<div class="modal-note">当前未添加 Cookie。</div>`;
      }
    };
    api("/cookies/info").then(renderStatus).catch(() => renderStatus(null));
    $("#cookieHelp", root).addEventListener("click", () => {
      msg.textContent = "在浏览器安装「Get cookies.txt LOCALLY」扩展，登录网站后一键导出 Netscape 格式 cookies.txt，再粘贴到此处。";
      msg.style.color = "";
    });
    $("#cookieTest", root).addEventListener("click", async () => {
      try {
        const r = await api("/cookies/test", { method: "POST" });
        msg.textContent = r.detail || (r.ok ? "Cookie 可被工具正常读取" : r.error);
        msg.style.color = r.ok ? "#2e7d32" : "#c62828";
      } catch (e) { msg.textContent = `${e.message}${e.status ? `（${e.method} ${e.path}）` : ""}`; msg.style.color = "#c62828"; }
    });
    $("#cookieSave", root).addEventListener("click", async () => {
      const content = ta.value;
      if (!content.trim()) { msg.textContent = "请先粘贴 Cookie 内容"; msg.style.color = "#c62828"; return; }
      try {
        await api("/cookies", { method: "POST", body: { content } });
        toast("Cookie 已保存并启用", "success");
        await refreshCookieUI();
        root.innerHTML = "";
      } catch (e) { msg.textContent = `${e.message}${e.status ? `（${e.method} ${e.path}）` : ""}`; msg.style.color = "#c62828"; }
    });
    root.onclick = (event) => {
      if (event.target.hasAttribute("data-cookie-close") || event.target.classList.contains("modal-backdrop")) root.innerHTML = "";
    };
    root.onkeydown = (event) => { if (event.key === "Escape") root.innerHTML = ""; };
    ta.focus();
  }

  async function refreshCookieUI() {
    await loadSystemStatus(true);
    updateAnomalyRegion();
    if (state.view === "settings") renderView(false);
  }

  async function init() {
    applyTheme();
    renderView(false);
    await Promise.all([loadTasks(false), loadGroups(), loadStats(false)]);
    if (state.view === "download") renderView(false);
    else if (state.view === "tasks") refreshTaskLiveRegions();
    else renderView(false);
    schedulePolling();
    // UI-P0-05：首次启动引导（仅首次，受 localStorage 标记控制）+ 能力检查（用于引导与异常卡片）
    void runCapabilityCheck();
    openOnboarding();
  }
  init();
})();
