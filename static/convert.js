/* 格式转换（独立功能模块）
 *
 * 与主应用的唯一接触面是 window.YingjiExtViews.convert 这一注册点，
 * 不读写主应用的任何状态、不覆盖任何既有样式与函数。
 * 页面被切走后轮询会自行停止（靠 DOM 存在性自检，无需外部通知）。
 */
(() => {
  "use strict";

  const API = "/api/convert";
  const PREF_KEY = "yingji_convert_prefs";
  const OUTDIR_KEY = "yingji_convert_output_dir";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  const fmtBytes = (bytes) => {
    const n = Number(bytes || 0);
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
    return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
  };

  function toast(message, tone = "") {
    const region = $("#toastRegion");
    if (!region) return;
    const el = document.createElement("div");
    el.className = `toast ${tone}`;
    el.textContent = message;
    region.appendChild(el);
    setTimeout(() => el.remove(), 3600);
  }

  function copyPath(p) {
    if (!p) return;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(p).catch(() => {});
      }
    } catch (_) { /* 剪贴板不可用时静默 */ }
  }

  const DEFAULTS = {
    image: {
      target: "jpg", quality: 88, resize_mode: "keep", percent: 100,
      long_edge: 1920, width: 0, height: 0, keep_ratio: true,
      auto_orient: true, keep_animation: true,
    },
    video: {
      target: "mp4", video_codec: "h264", resolution: "keep", width: 0, height: 0,
      fps: "keep", bitrate: "auto", crf: 23, preset: "medium",
      audio_codec: "aac", audio_bitrate: 128, mute: false,
    },
  };

  const S = {
    caps: null,
    capsError: "",
    loading: false,
    files: [],
    jobs: [],
    working: false,
    timer: 0,
    seq: 0,
    outputDir: "",
    image: { ...DEFAULTS.image },
    video: { ...DEFAULTS.video },
  };
  let dirPickerBusy = false;
  try { S.outputDir = localStorage.getItem(OUTDIR_KEY) || ""; } catch (_) {}

  function loadPrefs() {
    try {
      const saved = JSON.parse(localStorage.getItem(PREF_KEY) || "{}");
      if (saved.image) S.image = { ...DEFAULTS.image, ...saved.image };
      if (saved.video) S.video = { ...DEFAULTS.video, ...saved.video };
    } catch (_) { /* 忽略损坏的本地偏好 */ }
  }

  function savePrefs() {
    try {
      localStorage.setItem(PREF_KEY, JSON.stringify({ image: S.image, video: S.video }));
    } catch (_) { /* 存储不可用时静默降级 */ }
  }

  async function api(path, options = {}) {
    const res = await fetch(API + path, {
      credentials: "same-origin",
      headers: options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" } : undefined,
      ...options,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) throw new Error((data && data.error) || `请求失败（${res.status}）`);
    return data || {};
  }

  // ----------------------------------------------------------------
  // 能力
  // ----------------------------------------------------------------
  const imageFormats = () => (S.caps && S.caps.image_formats) || [];
  const videoFormats = () => (S.caps && S.caps.video_formats) || [];
  const imageFormat = (key) => imageFormats().find((f) => f.key === key) || null;
  const videoFormat = (key) => videoFormats().find((f) => f.key === key) || null;

  function hasKind(kind) {
    return S.files.some((f) => f.kind === kind);
  }

  function kindEnabled(kind) {
    if (!S.caps) return false;
    return kind === "image" ? Boolean(S.caps.image) : Boolean(S.caps.video);
  }

  // ----------------------------------------------------------------
  // 视图
  // ----------------------------------------------------------------
  function capsBar() {
    if (S.capsError) {
      return `<div class="cv-caps cv-caps-bad">
        <span>转换能力读取失败：${esc(S.capsError)}</span>
        <button class="button ghost" data-cv="reload-caps">重新检查</button>
      </div>`;
    }
    if (!S.caps) return `<div class="cv-caps"><span>正在检查本机转换能力……</span></div>`;
    const pill = (ok, label, hint) => `<span class="cv-pill ${ok ? "ok" : "off"}">
      <i></i><b>${esc(label)}</b><em>${esc(ok ? "可用" : hint)}</em></span>`;
    const absDir = S.outputDir || (S.caps && S.caps.output_dir) || "";
    const relDir = S.outputDir ? "" : ((S.caps && S.caps.output_dir_rel) || "");
    const showDir = relDir || absDir;
    return `<div class="cv-caps">
      ${pill(S.caps.image, "图片转换", "本机缺少图片处理组件")}
      ${pill(S.caps.video, "视频转换", "本机缺少视频处理组件")}
      <span class="cv-caps-path" title="${esc(absDir)}">输出位置 · ${esc(showDir)}</span>
      <button class="button ghost" data-cv="choose-output-dir">选择目录</button>
      <button class="button ghost" data-cv="open-output">打开输出位置</button>
    </div>`;
  }

  function dropZone() {
    return `<section class="cv-drop" id="cvDrop" tabindex="0" role="button"
      aria-label="拖入文件或点击选择文件">
      <div class="cv-drop-icon">＋</div>
      <b>把图片或视频拖到这里</b>
      <p>也可以点击选择文件，支持一次添加多个。文件只在本机处理，不会上传到任何服务器。</p>
      <div class="cv-drop-exts">
        <span>图片 JPG · PNG · WEBP · BMP · GIF · TIFF · ICO</span>
        <span>视频 MP4 · MKV · MOV · AVI · WMV · FLV · WEBM</span>
      </div>
      <input type="file" id="cvPicker" multiple hidden
        accept="image/*,video/*,.mkv,.flv,.wmv,.ts,.rmvb,.m2ts,.avi,.mov,.webm,.tiff,.tif,.bmp,.ico">
    </section>`;
  }

  function targetSelect(file) {
    const list = file.kind === "image" ? imageFormats() : videoFormats();
    const options = list.map((f) =>
      `<option value="${esc(f.key)}" ${f.key === file.target ? "selected" : ""}>${esc(f.label)}</option>`).join("");
    return `<select class="select cv-mini-select" data-cv="file-target" data-uid="${file.uid}"
      aria-label="输出格式">${options}</select>`;
  }

  function filesHtml() {
    if (!S.files.length) {
      return `<div class="cv-empty">还没有添加文件</div>`;
    }
    return S.files.map((file) => {
      const blocked = !kindEnabled(file.kind);
      const progress = file.uploading
        ? `<span class="cv-file-up">上传中 ${file.uploadPct || 0}%</span>` : "";
      return `<div class="cv-file ${blocked ? "is-blocked" : ""}" data-uid="${file.uid}">
        <span class="cv-file-kind ${file.kind}">${file.kind === "image" ? "图" : "视"}</span>
        <div class="cv-file-main">
          <b title="${esc(file.name)}">${esc(file.name)}</b>
          <span>${esc(fmtBytes(file.size))}${blocked ? " · 本机暂不支持该类型转换" : ""}</span>
        </div>
        ${progress}
        <div class="cv-file-target"><span>转为</span>${targetSelect(file)}</div>
        <button class="button icon ghost" data-cv="remove-file" data-uid="${file.uid}"
          title="移除" aria-label="移除 ${esc(file.name)}">×</button>
      </div>`;
    }).join("");
  }

  function row(label, control, hint = "") {
    return `<label class="cv-row"><span class="cv-row-label">${esc(label)}</span>
      <span class="cv-row-control">${control}</span>
      ${hint ? `<em class="cv-row-hint">${esc(hint)}</em>` : ""}</label>`;
  }

  function selectHtml(name, group, value, options) {
    return `<select class="select" data-cv="opt" data-group="${group}" data-name="${name}">
      ${options.map(([key, label]) =>
        `<option value="${esc(key)}" ${String(key) === String(value) ? "selected" : ""}>${esc(label)}</option>`).join("")}
    </select>`;
  }

  function numberHtml(name, group, value, placeholder = "", min = 0) {
    return `<input class="input plain cv-num" type="number" min="${min}" placeholder="${esc(placeholder)}"
      value="${value || value === 0 ? esc(value) : ""}" data-cv="opt" data-group="${group}" data-name="${name}">`;
  }

  function imageOptionsHtml() {
    const o = S.image;
    const meta = imageFormat(o.target);
    const parts = [];
    parts.push(row("输出格式", selectHtml("target", "image", o.target,
      imageFormats().map((f) => [f.key, f.label]))));

    if (meta && meta.quality) {
      parts.push(row("输出质量", `<span class="cv-slider">
        <input type="range" min="10" max="100" step="1" value="${o.quality}"
          data-cv="opt" data-group="image" data-name="quality" aria-label="输出质量">
        <b id="cvQualityValue">${o.quality}</b></span>`,
      o.target === "png" ? "数值越高压缩越轻，画质无损" : "数值越高越清晰，文件也越大"));
    }

    parts.push(row("尺寸", selectHtml("resize_mode", "image", o.resize_mode, [
      ["keep", "保持原始尺寸"],
      ["percent", "按百分比缩放"],
      ["long_edge", "限制长边像素"],
      ["exact", "指定宽高"],
    ])));

    if (o.resize_mode === "percent") {
      parts.push(row("缩放比例", `<span class="cv-inline">${numberHtml("percent", "image", o.percent, "100", 1)}<em>%</em></span>`));
    } else if (o.resize_mode === "long_edge") {
      parts.push(row("长边不超过", `<span class="cv-inline">${numberHtml("long_edge", "image", o.long_edge, "1920", 1)}<em>px</em></span>`));
    } else if (o.resize_mode === "exact") {
      parts.push(row("宽 × 高", `<span class="cv-inline">
        ${numberHtml("width", "image", o.width, "宽")}<em>×</em>${numberHtml("height", "image", o.height, "高")}
        <label class="cv-check"><input type="checkbox" data-cv="opt" data-group="image"
          data-name="keep_ratio" ${o.keep_ratio ? "checked" : ""}> 保持比例</label></span>`,
      "留空的一边会按比例自动计算"));
    }

    parts.push(`<div class="cv-row cv-row-checks">
      <label class="cv-check"><input type="checkbox" data-cv="opt" data-group="image"
        data-name="auto_orient" ${o.auto_orient ? "checked" : ""}> 自动摆正方向</label>
      ${S.caps && S.caps.animated_image ? `<label class="cv-check"><input type="checkbox" data-cv="opt"
        data-group="image" data-name="keep_animation" ${o.keep_animation ? "checked" : ""}> 保留动图帧</label>` : ""}
    </div>`);
    return parts.join("");
  }

  function videoOptionsHtml() {
    const o = S.video;
    const meta = videoFormat(o.target);
    const parts = [];
    parts.push(row("输出格式", selectHtml("target", "video", o.target,
      videoFormats().map((f) => [f.key, f.label]))));

    if (o.target === "gif") {
      parts.push(row("帧率", selectHtml("fps", "video", o.fps,
        [["8", "8 fps · 体积最小"], ["12", "12 fps · 推荐"], ["15", "15 fps"], ["24", "24 fps · 最流畅"]])));
      parts.push(row("画面尺寸", selectHtml("resolution", "video", o.resolution, [
        ["keep", "保持原始"], ["720", "长边 720P"], ["480", "480P"], ["360", "360P"], ["240", "240P"],
      ]), "动图建议缩小尺寸，否则文件会很大"));
      return parts.join("");
    }

    const codecs = (meta && meta.video_codecs) || [];
    if (codecs.length) {
      parts.push(row("视频编码", selectHtml("video_codec", "video", o.video_codec,
        codecs.map((c) => [c.key, c.label]))));
    }

    // 不重新编码时画面参数无从调整，直接隐藏，避免给出会被忽略的选项
    if (o.video_codec === "copy") {
      const audiosCopy = (meta && meta.audio_codecs) || [];
      if (audiosCopy.length) {
        parts.push(row("音频", selectHtml("audio_codec", "video", o.audio_codec,
          audiosCopy.map((c) => [c.key, c.label]))));
      }
      parts.push(`<div class="cv-row cv-row-checks"><em class="cv-row-hint">
        只更换容器、画面与编码原样保留，速度最快；分辨率、帧率、码率此时不可调整。
      </em></div>`);
      return parts.join("");
    }

    parts.push(row("分辨率", selectHtml("resolution", "video", o.resolution,
      ((S.caps && S.caps.resolution_presets) || []).map((p) => [p.key, p.label]))));
    if (o.resolution === "custom") {
      parts.push(row("宽 × 高", `<span class="cv-inline">
        ${numberHtml("width", "video", o.width, "宽")}<em>×</em>${numberHtml("height", "video", o.height, "高")}</span>`,
      "只填一边时，另一边按比例自动计算"));
    }

    parts.push(row("帧率", selectHtml("fps", "video", o.fps, [
      ["keep", "保持原始"], ["60", "60 fps"], ["30", "30 fps"], ["25", "25 fps"], ["24", "24 fps"], ["15", "15 fps"],
    ])));

    parts.push(row("码率", `<span class="cv-inline">
      ${selectHtml("bitrate_mode", "video", o.bitrate === "auto" ? "auto" : "custom",
        [["auto", "自动（按画质控制）"], ["custom", "指定码率"]])}
      ${o.bitrate === "auto" ? "" :
        `${numberHtml("bitrate", "video", o.bitrate, "4000", 100)}<em>kbps</em>`}</span>`,
    o.bitrate === "auto" ? "自动模式下由画质等级决定体积" : ""));

    if (o.bitrate === "auto" && ["h264", "h265"].includes(o.video_codec)) {
      parts.push(row("画质等级", `<span class="cv-slider">
        <input type="range" min="16" max="34" step="1" value="${o.crf}"
          data-cv="opt" data-group="video" data-name="crf" aria-label="画质等级">
        <b id="cvCrfValue">${o.crf}</b></span>`, "数值越小画质越好、文件越大，推荐 20–26"));
      parts.push(row("编码速度", selectHtml("preset", "video", o.preset, [
        ["veryfast", "很快（体积略大）"], ["fast", "较快"], ["medium", "均衡"], ["slow", "较慢（体积更小）"],
      ])));
    }

    const audios = (meta && meta.audio_codecs) || [];
    if (audios.length) {
      parts.push(row("音频", `<span class="cv-inline">
        ${selectHtml("audio_codec", "video", o.audio_codec, audios.map((c) => [c.key, c.label]))}
        ${numberHtml("audio_bitrate", "video", o.audio_bitrate, "128", 32)}<em>kbps</em>
        <label class="cv-check"><input type="checkbox" data-cv="opt" data-group="video"
          data-name="mute" ${o.mute ? "checked" : ""}> 移除声音</label></span>`));
    }
    return parts.join("");
  }

  function optionsHtml() {
    const blocks = [];
    if (hasKind("image")) {
      blocks.push(`<section class="cv-panel cv-opt-panel">
        <header class="cv-panel-head"><b>图片参数</b><span>作用于所有图片文件</span></header>
        <div class="cv-opt-grid">${imageOptionsHtml()}</div>
      </section>`);
    }
    if (hasKind("video")) {
      blocks.push(`<section class="cv-panel cv-opt-panel">
        <header class="cv-panel-head"><b>视频参数</b><span>作用于所有视频文件</span></header>
        <div class="cv-opt-grid">${videoOptionsHtml()}</div>
      </section>`);
    }
    return blocks.join("");
  }

  function jobStatusText(job) {
    if (job.status === "queued") return "排队中";
    if (job.status === "running") return `转换中 ${job.progress || 0}%`;
    if (job.status === "done") return `已完成 · ${fmtBytes(job.output_size)}`;
    if (job.status === "canceled") return "已取消";
    return `失败：${job.error || "未知原因"}`;
  }

  function jobStatusClass(job) {
    if (job.status === "done") return "complete";
    if (job.status === "running") return "downloading";
    if (job.status === "queued") return "queued";
    if (job.status === "canceled") return "cancelled";
    return "error";
  }

  function jobsHtml() {
    if (!S.jobs.length) {
      return `<div class="empty"><span>⇄</span>还没有转换记录，添加文件后点「开始转换」</div>`;
    }
    return S.jobs.slice().reverse().map((job) => {
      const pct = job.status === "done" ? 100 : Math.max(0, Math.min(100, job.progress || 0));
      const actions = [];
      if (["queued", "running"].includes(job.status)) {
        actions.push(`<button class="button ghost" data-cv="cancel-job" data-id="${job.id}">取消</button>`);
      }
      if (job.status === "done") {
        if (job.preview_kind) {
          actions.push(`<button class="button ghost" data-cv="preview" data-id="${job.id}">预览</button>`);
        }
        actions.push(`<a class="button" href="${API}/jobs/${job.id}/file" download>下载</a>`);
        actions.push(`<button class="button ghost" data-cv="locate" data-id="${job.id}">所在位置</button>`);
      }
      if (["done", "failed", "canceled"].includes(job.status)) {
        actions.push(`<button class="button icon ghost" data-cv="drop-job" data-id="${job.id}"
          title="从列表移除" aria-label="从列表移除">×</button>`);
      }
      return `<article class="cv-job" data-id="${job.id}">
        <div class="cv-job-top">
          <div class="cv-job-name">
            <b title="${esc(job.source_name)}">${esc(job.source_name)}</b>
            <span>${esc(fmtBytes(job.source_size))} → ${esc(job.target_label)}${job.detail ? ` · ${esc(job.detail)}` : ""}</span>
          </div>
          <span class="status ${jobStatusClass(job)}">${esc(jobStatusText(job))}</span>
        </div>
        <div class="cv-bar ${job.status}"><i style="width:${pct}%"></i></div>
        <div class="cv-job-actions">${actions.join("")}</div>
      </article>`;
    }).join("");
  }

  function actionBarHtml() {
    const usable = S.files.filter((f) => kindEnabled(f.kind)).length;
    const finished = S.jobs.filter((j) => ["done", "failed", "canceled"].includes(j.status)).length;
    return `<div class="cv-actions">
      <button class="button primary" data-cv="start" ${usable && !S.working ? "" : "disabled"}>
        ${S.working ? "正在提交…" : `开始转换${usable ? `（${usable}）` : ""}`}
      </button>
      <button class="button ghost" data-cv="clear-files" ${S.files.length ? "" : "disabled"}>清空待转列表</button>
      <span class="cv-actions-gap"></span>
      <button class="button ghost" data-cv="clear-jobs" ${finished ? "" : "disabled"}>清除已结束记录</button>
    </div>`;
  }

  function render() {
    return `<div class="view" id="cvRoot">
      <header class="page-head"><div>
        <small class="eyebrow">格式转换</small>
        <h1>把文件换成你需要的格式</h1>
        <p>图片与视频的格式互转，全部在本机完成。转换不改动原文件，产物单独存放。</p>
      </div><div class="head-actions">
        <span class="status complete">本机处理</span>
      </div></header>

      <div id="cvCaps">${capsBar()}</div>
      ${dropZone()}

      <div class="section-head"><div><h2>待转文件</h2></div>
        <span class="cv-count" id="cvCount">${S.files.length ? `${S.files.length} 个文件` : ""}</span></div>
      <section class="cv-panel"><div id="cvFileList" class="cv-file-list">${filesHtml()}</div></section>

      <div id="cvOptions" class="cv-options">${optionsHtml()}</div>
      <div id="cvActionBar">${actionBarHtml()}</div>

      <div class="section-head"><div><h2>转换记录</h2></div>
        <span class="cv-count">仅本次运行期间保留，产物文件会一直留在磁盘</span></div>
      <section class="cv-panel" id="cvJobs">${jobsHtml()}</section>
    </div>`;
  }

  // ----------------------------------------------------------------
  // 局部刷新
  // ----------------------------------------------------------------
  const alive = () => Boolean(document.getElementById("cvRoot"));

  function refreshFiles() {
    if (!alive()) return;
    $("#cvFileList").innerHTML = filesHtml();
    $("#cvCount").textContent = S.files.length ? `${S.files.length} 个文件` : "";
    $("#cvOptions").innerHTML = optionsHtml();
    $("#cvActionBar").innerHTML = actionBarHtml();
  }

  function refreshOptions() {
    if (!alive()) return;
    $("#cvOptions").innerHTML = optionsHtml();
  }

  function refreshJobs() {
    if (!alive()) return;
    $("#cvJobs").innerHTML = jobsHtml();
    $("#cvActionBar").innerHTML = actionBarHtml();
  }

  function refreshCaps() {
    if (!alive()) return;
    $("#cvCaps").innerHTML = capsBar();
  }

  // ----------------------------------------------------------------
  // 文件收集
  // ----------------------------------------------------------------
  const IMAGE_EXT = /\.(jpe?g|jfif|png|webp|bmp|gif|tiff?|ico|heic|heif|avif|ppm|tga)$/i;
  const VIDEO_EXT = /\.(mp4|m4v|mkv|mov|avi|wmv|flv|webm|ts|mpe?g|3gp|rmvb|vob|m2ts|ogv|asf)$/i;

  function detectKind(file) {
    if (IMAGE_EXT.test(file.name)) return "image";
    if (VIDEO_EXT.test(file.name)) return "video";
    if ((file.type || "").startsWith("image/")) return "image";
    if ((file.type || "").startsWith("video/")) return "video";
    return "";
  }

  function addFiles(list) {
    const incoming = [...list];
    let skipped = 0;
    let oversize = 0;
    const limit = (S.caps && S.caps.max_upload_bytes) || 4 * 1024 ** 3;
    for (const file of incoming) {
      const kind = detectKind(file);
      if (!kind) { skipped += 1; continue; }
      if (file.size > limit) { oversize += 1; continue; }
      if (S.files.some((f) => f.name === file.name && f.size === file.size)) continue;
      S.files.push({
        uid: `f${++S.seq}`,
        file, name: file.name, size: file.size, kind,
        target: kind === "image" ? S.image.target : S.video.target,
        uploading: false, uploadPct: 0,
      });
    }
    refreshFiles();
    if (skipped) toast(`已跳过 ${skipped} 个不支持的文件`, "error");
    if (oversize) toast(`已跳过 ${oversize} 个超过大小上限的文件`, "error");
  }

  // ----------------------------------------------------------------
  // 上传与提交
  // ----------------------------------------------------------------
  function uploadOne(entry) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", entry.file, entry.name);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API}/upload`, true);
      xhr.withCredentials = true;
      xhr.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        entry.uploadPct = Math.round((event.loaded / event.total) * 100);
        const node = document.querySelector(`.cv-file[data-uid="${entry.uid}"] .cv-file-up`);
        if (node) node.textContent = `上传中 ${entry.uploadPct}%`;
      };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (_) { data = null; }
        if (xhr.status >= 200 && xhr.status < 300 && data && data.token) resolve(data.token);
        else reject(new Error((data && data.error) || `上传失败（${xhr.status}）`));
      };
      xhr.onerror = () => reject(new Error("上传中断"));
      xhr.send(form);
    });
  }

  function buildOptions(kind) {
    if (kind === "image") {
      const o = S.image;
      return {
        quality: Number(o.quality) || 88,
        resize_mode: o.resize_mode,
        percent: Number(o.percent) || 100,
        long_edge: Number(o.long_edge) || 0,
        width: Number(o.width) || 0,
        height: Number(o.height) || 0,
        keep_ratio: Boolean(o.keep_ratio),
        auto_orient: Boolean(o.auto_orient),
        keep_animation: Boolean(o.keep_animation),
      };
    }
    const o = S.video;
    return {
      video_codec: o.video_codec,
      resolution: o.resolution,
      width: Number(o.width) || 0,
      height: Number(o.height) || 0,
      fps: o.fps,
      bitrate: o.bitrate === "auto" ? "auto" : Number(o.bitrate) || "auto",
      crf: Number(o.crf) || 23,
      preset: o.preset,
      audio_codec: o.audio_codec,
      audio_bitrate: Number(o.audio_bitrate) || 128,
      mute: Boolean(o.mute),
    };
  }

  async function startConvert() {
    const queue = S.files.filter((f) => kindEnabled(f.kind));
    if (!queue.length) return;
    S.working = true;
    $("#cvActionBar").innerHTML = actionBarHtml();

    const items = [];
    const failed = [];
    for (const entry of queue) {
      entry.uploading = true;
      entry.uploadPct = 0;
      refreshFiles();
      try {
        const token = await uploadOne(entry);
        items.push({ token, target: entry.target, options: buildOptions(entry.kind), output_dir: S.outputDir });
        S.files = S.files.filter((f) => f.uid !== entry.uid);
      } catch (error) {
        entry.uploading = false;
        failed.push(`${entry.name}：${error.message}`);
      }
      refreshFiles();
    }

    if (items.length) {
      try {
        const data = await api("/jobs", { method: "POST", body: JSON.stringify({ items }) });
        (data.errors || []).forEach((e) => failed.push(e.error));
        toast(`已加入转换队列（${(data.created || []).length}）`, "success");
        await pollJobs();
        startPolling();
      } catch (error) {
        toast(error.message, "error");
      }
    }
    if (failed.length) toast(failed[0], "error");

    S.working = false;
    refreshFiles();
    refreshJobs();
  }

  // ----------------------------------------------------------------
  // 轮询
  // ----------------------------------------------------------------
  async function pollJobs() {
    try {
      const data = await api("/jobs");
      S.jobs = data.jobs || [];
      refreshJobs();
      return data.active || 0;
    } catch (_) {
      return 0;
    }
  }

  function startPolling() {
    stopPolling();
    S.timer = setInterval(async () => {
      if (!alive()) { stopPolling(); return; }
      if (document.hidden) return;
      const active = await pollJobs();
      if (!active) stopPolling();
    }, 1000);
  }

  function stopPolling() {
    if (S.timer) clearInterval(S.timer);
    S.timer = 0;
  }

  // ----------------------------------------------------------------
  // 预览
  // ----------------------------------------------------------------
  function closePreview() {
    const node = document.getElementById("cvModalRoot");
    if (node) node.remove();
    document.removeEventListener("keydown", onPreviewKey);
  }

  function onPreviewKey(event) {
    if (event.key === "Escape") closePreview();
  }

  function openPreview(job) {
    closePreview();
    const url = `${API}/jobs/${job.id}/file?mode=inline&t=${Date.now()}`;
    const body = job.preview_kind === "video"
      ? `<video src="${url}" controls autoplay playsinline></video>`
      : `<img src="${url}" alt="${esc(job.output_name)}">`;
    const host = document.createElement("div");
    host.id = "cvModalRoot";
    host.innerHTML = `<div class="cv-modal-backdrop" data-cv="close-preview">
      <section class="cv-modal" role="dialog" aria-modal="true" aria-label="转换结果预览">
        <header><b>${esc(job.output_name)}</b>
          <button class="button icon ghost" data-cv="close-preview" aria-label="关闭预览">×</button>
        </header>
        <div class="cv-modal-body">${body}</div>
        <footer><span>${esc(fmtBytes(job.output_size))}</span>
          <a class="button" href="${API}/jobs/${job.id}/file" download>下载文件</a>
        </footer>
      </section></div>`;
    document.body.appendChild(host);
    host.addEventListener("click", (event) => {
      if (event.target.closest('[data-cv="close-preview"]')) closePreview();
    });
    document.addEventListener("keydown", onPreviewKey);
  }

  // ----------------------------------------------------------------
  // 交互绑定
  // ----------------------------------------------------------------
  function applyOption(group, name, value) {
    const bag = group === "image" ? S.image : S.video;
    if (name === "bitrate_mode") {
      bag.bitrate = value === "auto" ? "auto" : (Number(bag.bitrate) || 4000);
      savePrefs();
      refreshOptions();
      return;
    }
    bag[name] = value;
    if (group === "video" && name === "video_codec" && value === "copy") {
      // 换壳模式下这些参数会被忽略，先归位，避免残留值造成"设了却没生效"
      bag.resolution = "keep";
      bag.fps = "keep";
      bag.bitrate = "auto";
    }
    if (name === "target") {
      // 目标格式变了：同步未单独指定过的文件，并校正不兼容的编码
      S.files.forEach((f) => { if (f.kind === group) f.target = value; });
      if (group === "video") {
        const meta = videoFormat(value);
        const vlist = (meta && meta.video_codecs) || [];
        const alist = (meta && meta.audio_codecs) || [];
        if (vlist.length && !vlist.some((c) => c.key === bag.video_codec)) bag.video_codec = vlist[0].key;
        if (alist.length && !alist.some((c) => c.key === bag.audio_codec)) bag.audio_codec = alist[0].key;
      }
      savePrefs();
      refreshFiles();
      return;
    }
    savePrefs();
    const needsRelayout = ["resize_mode", "resolution", "video_codec"].includes(name);
    if (needsRelayout) refreshOptions();
  }

  function readControl(el) {
    if (el.type === "checkbox") return el.checked;
    if (el.type === "number" || el.type === "range") {
      const n = Number(el.value);
      return Number.isFinite(n) ? n : 0;
    }
    return el.value;
  }

  async function chooseOutputDir() {
    if (dirPickerBusy) { toast("目录选择窗口已经打开", "error"); return; }
    dirPickerBusy = true;
    try {
      const res = await fetch("/api/choose-dir", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!res.ok) throw new Error((data && data.error) || "无法打开目录选择");
      const path = (data && data.path) || "";
      if (!path) { toast("未选择目录", "ok"); return; }
      S.outputDir = path;
      try { localStorage.setItem(OUTDIR_KEY, path); } catch (_) {}
      const el = $("#cvRoot .cv-caps-path");
      if (el) { el.textContent = "输出位置 · " + path; el.title = path; }
      toast("已选择输出目录：" + path, "ok");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      dirPickerBusy = false;
    }
  }

  function bind(root) {
    const drop = $("#cvDrop", root);
    const picker = $("#cvPicker", root);

    if (drop && picker) {
      drop.addEventListener("click", () => picker.click());
      drop.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); picker.click(); }
      });
      picker.addEventListener("change", () => {
        if (picker.files && picker.files.length) addFiles(picker.files);
        picker.value = "";
      });
      ["dragenter", "dragover"].forEach((type) => drop.addEventListener(type, (event) => {
        event.preventDefault();
        drop.classList.add("is-over");
      }));
      ["dragleave", "drop"].forEach((type) => drop.addEventListener(type, (event) => {
        event.preventDefault();
        if (type === "drop" || !drop.contains(event.relatedTarget)) drop.classList.remove("is-over");
      }));
      drop.addEventListener("drop", (event) => {
        const files = event.dataTransfer && event.dataTransfer.files;
        if (files && files.length) addFiles(files);
      });
    }

    // 整页只在根节点上挂一次委托，避免局部刷新后重复绑定
    root.addEventListener("input", (event) => {
      const el = event.target.closest('[data-cv="opt"]');
      if (!el) return;
      if (el.type === "range") {
        const label = el.dataset.name === "quality" ? $("#cvQualityValue") : $("#cvCrfValue");
        if (label) label.textContent = el.value;
      }
      if (el.tagName === "INPUT" && el.type !== "checkbox") {
        applyOption(el.dataset.group, el.dataset.name, readControl(el));
      }
    });

    root.addEventListener("change", (event) => {
      const opt = event.target.closest('[data-cv="opt"]');
      if (opt) {
        applyOption(opt.dataset.group, opt.dataset.name, readControl(opt));
        return;
      }
      const fileTarget = event.target.closest('[data-cv="file-target"]');
      if (fileTarget) {
        const entry = S.files.find((f) => f.uid === fileTarget.dataset.uid);
        if (entry) entry.target = fileTarget.value;
      }
    });

    root.addEventListener("click", async (event) => {
      const hit = event.target.closest("[data-cv]");
      if (!hit) return;
      const action = hit.dataset.cv;

      if (action === "remove-file") {
        S.files = S.files.filter((f) => f.uid !== hit.dataset.uid);
        refreshFiles();
      } else if (action === "clear-files") {
        S.files = [];
        refreshFiles();
      } else if (action === "start") {
        event.preventDefault();
        await startConvert();
      } else if (action === "cancel-job") {
        try { await api(`/jobs/${hit.dataset.id}/cancel`, { method: "POST" }); await pollJobs(); }
        catch (error) { toast(error.message, "error"); }
      } else if (action === "drop-job") {
        try { await api(`/jobs/${hit.dataset.id}`, { method: "DELETE" }); await pollJobs(); }
        catch (error) { toast(error.message, "error"); }
      } else if (action === "clear-jobs") {
        try { await api("/jobs/clear", { method: "POST" }); await pollJobs(); }
        catch (error) { toast(error.message, "error"); }
      } else if (action === "preview") {
        const job = S.jobs.find((j) => j.id === hit.dataset.id);
        if (job) openPreview(job);
      } else if (action === "locate") {
        try {
          const r = await api("/open-output", { method: "POST", body: JSON.stringify({ job_id: hit.dataset.id }) });
          toast(`已在资源管理器打开：${r.path || ""}`, "ok");
          copyPath(r.path);
        } catch (error) { toast(error.message, "error"); }
      } else if (action === "open-output") {
        try {
          const r = await api("/open-output", { method: "POST", body: JSON.stringify({ output_dir: S.outputDir }) });
          toast(`已在资源管理器打开：${r.path || ""}`, "ok");
          copyPath(r.path);
        } catch (error) { toast(error.message, "error"); }
      } else if (action === "choose-output-dir") {
        event.preventDefault();
        await chooseOutputDir();
      } else if (action === "reload-caps") {
        await loadCaps();
      }
    });
  }

  async function loadCaps() {
    S.loading = true;
    try {
      S.caps = await api("/capabilities");
      S.capsError = "";
      // 首选格式若本机不支持则回落到第一个可用项
      if (!imageFormat(S.image.target) && imageFormats().length) S.image.target = imageFormats()[0].key;
      if (!videoFormat(S.video.target) && videoFormats().length) S.video.target = videoFormats()[0].key;
    } catch (error) {
      S.caps = null;
      S.capsError = error.message;
    }
    S.loading = false;
    refreshCaps();
    refreshFiles();
  }

  function mount(container) {
    const root = container.querySelector("#cvRoot");
    if (!root) return;
    bind(root);
    if (!S.caps) loadCaps();
    pollJobs().then((active) => { if (active) startPolling(); });
  }

  function unmount() {
    stopPolling();
    closePreview();
  }

  loadPrefs();
  window.YingjiExtViews = window.YingjiExtViews || {};
  window.YingjiExtViews.convert = { render, mount, unmount };
})();
