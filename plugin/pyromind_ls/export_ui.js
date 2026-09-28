/**
 * PyroMind export bridge for Label Studio.
 *
 * Clicking Export in the data manager starts the export straight away and shows a
 * PyroMind dialog: "exporting" while the request runs, then a link into the PyroMind
 * console once the file has landed in the caller's storage.
 *
 * The stock flow cannot be reused. It is a React route (/data/export) whose modal is
 * rendered by the Label Studio bundle, and this deployment does not rebuild that
 * bundle -- it runs the published front-end from the nginx image. The click is
 * therefore intercepted in the capture phase on `window`, which runs before React's
 * own listener on the root container, so the stock navigation never happens.
 *
 * Runs on every page but only reacts to a click on the data manager's export button,
 * so leaving it installed elsewhere costs nothing.
 */
(function () {
  "use strict";

  var INSTALLED_FLAG = "__pyromindExportBridgeInstalled";
  if (window[INSTALLED_FLAG]) return;
  window[INSTALLED_FLAG] = true;

  /** The data manager's export button, identified the same way its own tests do. */
  var EXPORT_BUTTON_SELECTOR = '[data-testid="dm-export-button"]';

  /**
   * Where every export lands, as written into the project description by the SDK
   * (openhands-tools/openhands/tools/label_studio/executor.py, _project_description).
   * A line under this prefix is the object key of the exported JSON.
   */
  var STORAGE_PREFIX = "/.pyromind-agent/label-studio/";

  /** The export API answers synchronously, so this only guards against a hung proxy. */
  var REQUEST_TIMEOUT_MS = 120000;

  /**
   * The console origin. The middleware substitutes this placeholder when it serves the
   * script; the guard below covers a deployment that forgot to configure it.
   */
  var CONSOLE_BASE = "__PYROMIND_CONSOLE_BASE__";
  if (CONSOLE_BASE.indexOf("__PYROMIND") === 0) CONSOLE_BASE = "";
  CONSOLE_BASE = String(CONSOLE_BASE).replace(/\/+$/, "");

  // ------------------------------------------------------------------ helpers

  function currentProjectId() {
    var match = /\/projects\/(\d+)(?:\/|$)/.exec(window.location.pathname);
    return match ? match[1] : "";
  }

  function exportObjectKey(description) {
    if (typeof description !== "string") return "";
    var lines = description.split(/\r?\n/);
    for (var i = 0; i < lines.length; i += 1) {
      var line = lines[i].trim();
      if (line.indexOf(STORAGE_PREFIX) === 0 && /\.json$/.test(line)) return line;
    }
    return "";
  }

  /** The directory holding the export, which is what the console should open. */
  function objectFolder(key) {
    return key.slice(0, key.lastIndexOf("/") + 1);
  }

  function consoleFolderUrl(key) {
    if (!CONSOLE_BASE) return "";
    return (
      CONSOLE_BASE +
      "/storage?path=" +
      encodeURIComponent(objectFolder(key)) +
      "&tab=dataset"
    );
  }

  async function fetchJson(url) {
    var response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error("HTTP " + response.status + " from " + url);
    }
    return response.json();
  }

  /**
   * Runs one export and resolves with the storage object key.
   *
   * The body is drained even though the browser download is not wanted: the plugin
   * uploads to storage synchronously inside the same request, so reading the response
   * to the end is what guarantees the file is really there before the console link is
   * offered.
   */
  async function runExport(projectId) {
    var controller = new AbortController();
    var timer = window.setTimeout(function () {
      controller.abort();
    }, REQUEST_TIMEOUT_MS);

    try {
      var response = await fetch(
        "/api/projects/" + projectId + "/export?exportType=JSON",
        {
          credentials: "same-origin",
          signal: controller.signal,
        },
      );

      if (!response.ok) {
        throw new Error("导出接口返回 HTTP " + response.status);
      }

      await response.blob();

      var project = await fetchJson("/api/projects/" + projectId);
      var key = exportObjectKey(project && project.description);

      if (!key) {
        throw new Error(
          "项目简介里没有找到存储路径。请确认项目由 PyroMind 创建。",
        );
      }

      return key;
    } finally {
      window.clearTimeout(timer);
    }
  }

  // -------------------------------------------------------------------- view

  var STYLES = [
    ":host{all:initial;display:block;}",
    "*{box-sizing:border-box;}",
    ".overlay{position:fixed;inset:0;display:flex;align-items:center;justify-content:center;",
    "background:rgba(15,23,42,.45);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',",
    "'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;}",
    ".card{width:min(460px,calc(100vw - 32px));background:#fff;border-radius:14px;",
    "box-shadow:0 24px 60px rgba(15,23,42,.28);padding:28px 28px 22px;color:#0f172a;}",
    ".head{display:flex;align-items:center;gap:12px;margin-bottom:14px;}",
    ".title{font-size:16px;font-weight:600;letter-spacing:.01em;flex:1;}",
    ".close{appearance:none;border:0;background:transparent;color:#94a3b8;font-size:20px;",
    "line-height:1;cursor:pointer;padding:4px 6px;border-radius:6px;}",
    ".close:hover{background:#f1f5f9;color:#475569;}",
    ".body{font-size:13px;line-height:1.65;color:#475569;}",
    ".path{margin-top:12px;padding:10px 12px;background:#f8fafc;border:1px solid #e2e8f0;",
    "border-radius:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;",
    "color:#334155;word-break:break-all;max-height:96px;overflow:auto;}",
    ".actions{display:flex;justify-content:flex-end;gap:10px;margin-top:22px;}",
    ".btn{appearance:none;border:1px solid transparent;border-radius:8px;padding:9px 18px;",
    "font-size:13px;font-weight:500;cursor:pointer;text-decoration:none;display:inline-block;}",
    ".btn.primary{background:#2563eb;color:#fff;}",
    ".btn.primary:hover{background:#1d4ed8;}",
    ".btn.ghost{background:#fff;color:#334155;border-color:#cbd5e1;}",
    ".btn.ghost:hover{background:#f8fafc;}",
    ".spinner{width:18px;height:18px;border:2px solid #dbeafe;border-top-color:#2563eb;",
    "border-radius:50%;animation:spin .8s linear infinite;flex:none;}",
    "@keyframes spin{to{transform:rotate(360deg);}}",
    ".icon{width:20px;height:20px;flex:none;}",
    ".stage{display:none;}",
    ":host([data-state='exporting']) .stage.exporting,",
    ":host([data-state='done']) .stage.done,",
    ":host([data-state='error']) .stage.error{display:block;}",
    ".stage.done .body,.stage.error .body{margin-top:0;}",
    ".hint{margin-top:10px;font-size:12px;color:#94a3b8;}",
  ].join("");

  var TEMPLATE =
    "<style>" +
    STYLES +
    "</style>" +
    '<div class="overlay"><div class="card" role="dialog" aria-modal="true">' +
    '<div class="head">' +
    '<div class="stage exporting"><div class="spinner"></div></div>' +
    '<div class="stage done">' +
    '<svg class="icon" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="9" fill="#dcfce7"/>' +
    '<path d="M6 10.4l2.6 2.6L14 7.6" stroke="#16a34a" stroke-width="1.8" ' +
    'stroke-linecap="round" stroke-linejoin="round"/></svg></div>' +
    '<div class="stage error">' +
    '<svg class="icon" viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="9" fill="#fee2e2"/>' +
    '<path d="M10 6v5" stroke="#dc2626" stroke-width="1.8" stroke-linecap="round"/>' +
    '<circle cx="10" cy="14" r="1" fill="#dc2626"/></svg></div>' +
    '<div class="title"></div>' +
    '<button type="button" class="close" aria-label="关闭">&times;</button>' +
    "</div>" +
    '<div class="stage exporting"><div class="body">正在把项目数据写入你的存储空间，' +
    "请不要关闭这个页面。</div><div class=\"hint\">数据量较大时可能需要几十秒。</div></div>" +
    '<div class="stage done"><div class="body">文件已写入你的存储空间。</div>' +
    '<div class="path"></div></div>' +
    '<div class="stage error"><div class="body message"></div></div>' +
    '<div class="actions">' +
    '<button type="button" class="btn ghost retry" hidden>重试</button>' +
    '<a class="btn primary open" target="_blank" rel="noreferrer" hidden>打开存储目录</a>' +
    '<button type="button" class="btn ghost dismiss" hidden>关闭</button>' +
    "</div>" +
    "</div></div>";

  function createDialog() {
    var host = document.createElement("div");
    host.setAttribute("data-pyromind-export-ui", "");
    host.style.cssText = "position:fixed;inset:0;z-index:2147483000;";
    var shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = TEMPLATE;
    document.documentElement.appendChild(host);
    return { host: host, shadow: shadow };
  }

  var active = null;

  function closeDialog() {
    if (active) {
      active.host.remove();
      active = null;
    }
  }

  function render(dialog, state, options) {
    var settings = options || {};
    dialog.host.setAttribute("data-state", state);

    var title = dialog.shadow.querySelector(".title");
    var close = dialog.shadow.querySelector(".close");
    var open = dialog.shadow.querySelector(".open");
    var retry = dialog.shadow.querySelector(".retry");
    var dismiss = dialog.shadow.querySelector(".dismiss");
    var path = dialog.shadow.querySelector(".path");
    var message = dialog.shadow.querySelector(".message");

    close.hidden = state !== "done" && state !== "error";
    retry.hidden = state !== "error";
    dismiss.hidden = state !== "done";
    open.hidden = state !== "done";

    if (state === "exporting") {
      title.textContent = "正在导出…";
    } else if (state === "done") {
      title.textContent = "导出完成";
      path.textContent = settings.key || "";
      open.href = consoleFolderUrl(settings.key || "") || "#";
      open.hidden = !CONSOLE_BASE;
    } else {
      title.textContent = "导出失败";
      message.textContent = settings.error || "未知错误";
    }
  }

  async function startExport(dialog, projectId) {
    try {
      var key = await runExport(projectId);
      render(dialog, "done", { key: key });
    } catch (error) {
      var text =
        error && error.name === "AbortError"
          ? "导出超时，请稍后重试或改用 SDK 导出。"
          : (error && error.message) || String(error);
      render(dialog, "error", { error: text });
    }
  }

  function openDialog() {
    var projectId = currentProjectId();
    if (!projectId) return;

    // An export already in flight owns the dialog; a second click must not start
    // another one.
    if (active && active.host.getAttribute("data-state") === "exporting") return;

    if (active) closeDialog();
    active = createDialog();
    var dialog = active;

    render(dialog, "exporting", {});

    dialog.shadow
      .querySelector(".close")
      .addEventListener("click", closeDialog);
    dialog.shadow
      .querySelector(".dismiss")
      .addEventListener("click", closeDialog);
    dialog.shadow.querySelector(".retry").addEventListener("click", function () {
      render(dialog, "exporting", {});
      startExport(dialog, projectId);
    });

    startExport(dialog, projectId);
  }

  // ------------------------------------------------------------------ wiring

  /**
   * Capture phase on `window`: the outermost node, so this runs before React's own
   * listener on the root container. Stopping the event here keeps the stock handler
   * from navigating to /data/export.
   */
  window.addEventListener(
    "click",
    function (event) {
      var node = event.target;
      if (!node || typeof node.closest !== "function") return;
      if (!node.closest(EXPORT_BUTTON_SELECTOR)) return;

      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      openDialog();
    },
    true,
  );
})();
