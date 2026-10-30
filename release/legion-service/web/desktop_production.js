(() => {
  const api = "/api/legion/cloud_desktop";
  const state = { projects: [], project: null, tasks: [], task: null, quality: {}, handoffs: [], recipes: [], software: [], url: "", view: "work" };
  const handoffState = { known: new Map(), initialized: false, timer: null, polling: false, notificationRequested: false };
  const $ = selector => document.querySelector(selector);
  const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const uid = () => String(window.clientUid || "default");

  // HTTP 身份由服务端依据现有凭据绑定，不从页面全局变量补 uid。
  async function get(path) {
    const response = await fetch(api + path);
    const data = await response.json().catch(() => ({}));
    return response.ok ? data : {...data, ok: false, error: data.error || `服务返回 ${response.status}`};
  }

  async function post(path, body = {}) {
    const response = await fetch(api + path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    return response.ok ? data : {...data, ok: false, error: data.error || `服务返回 ${response.status}`};
  }

  async function selectProject(projectId, renderAfter = true) {
    const data = await get("/workspace/project?project_id=" + encodeURIComponent(projectId));
    if (!data.ok) return;
    state.project = data.project;
    state.tasks = data.tasks || [];
    state.task = state.tasks.find(task => state.task && task.task_id === state.task.task_id) || state.tasks[0] || null;
    await loadQuality();
    if (renderAfter) render();
  }

  function projectRows() {
    return state.projects.map(project => {
      const summary = project.task_summary || {};
      return `<button class="cd-project ${state.project?.project_id === project.project_id ? "active" : ""}" data-cd-project="${escapeHtml(project.project_id)}">${escapeHtml(project.title)}<span class="cd-row-meta">${summary.running || 0} 执行 · ${summary.waiting_human || 0} 待你 · ${summary.completed || 0} 完成</span></button>`;
    }).join("") || '<div class="cd-empty">还没有项目。创建后，文件、轨迹和交付都在同一文件夹。</div>';
  }

  function taskRows() {
    return state.tasks.map(task => `<button class="cd-task ${state.task?.task_id === task.task_id ? "active" : ""}" data-cd-task="${escapeHtml(task.task_id)}">${escapeHtml(task.goal || task.task_id)}<span class="cd-row-meta">${escapeHtml(task.state)} · ${escapeHtml(task.phase || "")}</span></button>`).join("") || '<div class="cd-empty">当前项目还没有任务。</div>';
  }

  function handoffRows() {
    const rows = state.handoffs.filter(row => !state.project || row.project_id === state.project.project_id);
    return rows.map(row => `<div class="cd-handoff" data-handoff="${escapeHtml(row.handoff_id)}" data-project-id="${escapeHtml(row.project_id)}" data-task-id="${escapeHtml(row.task_id)}"><b>${escapeHtml(row.title)}</b><span class="cd-row-meta">${escapeHtml(row.kind)} · ${escapeHtml(row.instructions)}</span>${row.consequence ? `<span class="cd-row-meta">后果：${escapeHtml(row.consequence)}</span>` : ""}<div class="cd-handoff-actions"><button class="btn btn-primary" data-handoff-open>打开对应真机</button><button class="btn" data-handoff-done data-version="${row.version}">我已完成</button><button class="btn" data-handoff-cancel data-version="${row.version}">拒绝 / 取消</button></div></div>`).join("") || '<div class="cd-empty">没有等待你的登录、验证码或高后果确认。</div>';
  }

  function recipeRows() {
    return state.recipes.map(recipe => `<button class="cd-recipe" data-recipe="${escapeHtml(recipe.recipe_id)}">▶ ${escapeHtml(recipe.name)}<span class="cd-row-meta">v${recipe.version} · ${recipe.successes || 0}/${recipe.runs || 0} 成功</span></button>`).join("") || '<div class="cd-empty">完成有轨迹的任务后，可保存为流程。</div>';
  }

  function softwareRows() {
    const visible = new Set(["vscode", "geany", "chromium", "libreoffice", "gimp", "inkscape"]);
    return state.software.filter(item => visible.has(item.id)).map(item => `<button class="cd-software" data-software="${escapeHtml(item.id)}">${item.installed ? "✓" : "○"} ${escapeHtml(item.name)}<span class="cd-row-meta">${escapeHtml(item.version || "未安装")}</span></button>`).join("");
  }

  function qualityLabel(quality) {
    return ({VERIFIED_NARROW_WIN:"已验证窄面胜出", PARITY_OR_INCONCLUSIVE:"持平或证据不足", BELOW_FRONTIER:"仍低于参照"})[quality?.verdict] || quality?.state || "尚未进入质量评审";
  }

  async function loadQuality() {
    state.quality = {};
    if (!state.project || !state.task) return;
    const result = await get("/quality?project_id=" + encodeURIComponent(state.project.project_id) + "&task_id=" + encodeURIComponent(state.task.task_id));
    if (result.ok) state.quality = result.run || {};
  }

  function qualityHtml() {
    const quality = state.quality || {}, metrics = quality.metrics || {}, context = quality.quality_context || {};
    const references = (quality.references || []).slice(0, 3).map(row => `<a href="${escapeHtml(row.url || "#")}" target="_blank" rel="noopener">${escapeHtml(row.name || row.work || "参照")}</a>`).join(" · ");
    const sources = (context.sources || []).slice(0, 8).map(row => `<li><a href="${escapeHtml(row.url || "#")}" target="_blank" rel="noopener">${escapeHtml(row.title || row.url || "来源")}</a><span>${escapeHtml(row.page_or_state || row.decision_reason || "")}</span></li>`).join("");
    const queries = (context.query_ledger || []).slice(0, 8).map(row => `<li><b>${escapeHtml(row.query || "")}</b><span>${escapeHtml(row.why || "")}</span></li>`).join("");
    const patterns = (context.reference_patterns || []).slice(0, 8).map(row => `<li><b>${escapeHtml(row.dimension || "产品")}</b> · ${escapeHtml(row.pattern || "")}</li>`).join("");
    const journey = (context.golden_journey || []).map((row, index) => { const observed = (quality.journey_observations || []).find(item => item.step_id === row.step_id) || {}; return `<li class="${escapeHtml(observed.status || "pending")}"><span class="cd-journey-num">${index + 1}</span><div><b>${escapeHtml(row.name || row.step_id || "步骤")}</b><span>${escapeHtml(observed.observed || row.expected_observation || "等待执行")}</span></div></li>`; }).join("");
    const loop = context.strong_mvp?.value_loop || [];
    const anti = context.strong_mvp?.anti_goals || [];
    const gaps = (quality.gap_plan || []).slice(0, 4).map(value => `<li><b>${escapeHtml(value.problem || "")}</b><span>${escapeHtml(value.observed || value.acceptance_observation || "")}</span></li>`).join("") || (quality.remaining_gaps || []).slice(0, 4).map(value => `<li>${escapeHtml(value)}</li>`).join("");
    const strengths = (quality.verified_strengths || []).slice(0, 4).map(value => `<li>${escapeHtml(value)}</li>`).join("");
    const roles = Object.entries(quality.role_contexts || {}).slice(0, 8).map(([id, value]) => `<li><b>${escapeHtml(id)}</b><span>${escapeHtml(value.role || value.core_job || "共享核心 Context")}</span></li>`).join("");
    if (!state.task) return '<div class="cd-empty">选择任务后可查看质量目标、参照、评审和返工进度。</div>';
    const contextBody = context.original_query ? `<div class="cd-context-summary"><div><small>原始 Query</small><b>${escapeHtml(context.original_query)}</b></div><div><small>目标用户</small><b>${escapeHtml(context.target_user || "研究中")}</b></div><div><small>核心 Job</small><b>${escapeHtml(context.core_job || "研究中")}</b></div><div><small>为何会回来</small><b>${escapeHtml(context.retention_reason || "研究中")}</b></div></div>${loop.length ? `<div class="cd-value-loop">${loop.map((item, index) => `<span>${index ? "→ " : ""}${escapeHtml(item)}</span>`).join("")}</div>` : ""}${anti.length ? `<div class="cd-quality-line"><b>明确不做成</b><span>${anti.map(escapeHtml).join(" · ")}</span></div>` : ""}` : '<div class="cd-empty">正在把原始 Query 编译为用户、强 MVP、旅程和商业参考 Context。</div>';
    return `<section class="cd-pane cd-quality"><div class="cd-pane-title"><span>质量上下文与独立评审</span><span class="cd-badge ${quality.verdict === "VERIFIED_NARROW_WIN" ? "completed" : ""}">${escapeHtml(qualityLabel(quality))}</span></div><div class="cd-quality-grid"><div><small>Context 版本</small><b>v${context.revision || 0}</b></div><div><small>改进轮次</small><b>${quality.round || 0} / ${quality.max_rounds || "—"}</b></div><div><small>总耗时</small><b>${metrics.wall_clock_s == null ? "—" : Math.round(metrics.wall_clock_s) + " 秒"}</b></div></div>${contextBody}<div class="cd-context-columns">${journey ? `<details open><summary>黄金用户旅程</summary><ol class="cd-journey">${journey}</ol></details>` : ""}${queries || sources ? `<details><summary>研究轨迹 · ${(context.sources || []).length} 个来源</summary>${queries ? `<h5>Query 如何演化</h5><ul class="cd-evidence-list">${queries}</ul>` : ""}${sources ? `<h5>实际查看的页面 / 状态</h5><ul class="cd-evidence-list">${sources}</ul>` : ""}</details>` : ""}${patterns ? `<details><summary>从参考提炼的模式</summary><ul class="cd-evidence-list">${patterns}</ul></details>` : ""}${roles ? `<details><summary>军团职责与定向 Context</summary><ul class="cd-evidence-list">${roles}</ul></details>` : ""}</div>${references ? `<div class="cd-quality-line"><b>可用于同题比较的参照</b><span>${references}</span></div>` : ""}${strengths ? `<div class="cd-quality-line"><b>已证实长板</b><ul>${strengths}</ul></div>` : ""}${gaps ? `<div class="cd-quality-line issue"><b>下一轮最大差距</b><ul class="cd-evidence-list">${gaps}</ul></div>` : ""}<div class="as-d">公开商业参考用于补全 Context；只有完全同题的可运行参照、机器证据和独立盲评同时成立，才会显示“已验证窄面胜出”。</div></section>`;
  }

  function statusHtml() {
    const task = state.task || {};
    return `<div class="cd-status"><div><small>当前项目</small><b>${escapeHtml(state.project?.title || "未选择")}</b></div><div><small>任务状态</small><b><span class="cd-badge ${escapeHtml(task.state || "")}">${escapeHtml(task.state || "无任务")}</span></b></div><div><small>阶段 / checkpoint</small><b>${escapeHtml(task.phase || "—")}</b></div><div><small>交付物</small><b>${(task.artifacts || []).length} 个</b></div></div>${qualityHtml()}`;
  }

  function render() {
    const task = state.task || {};
    const files = (task.artifacts || []).map(file => `<button class="cd-task" data-cd-file="${escapeHtml(file.relative_path || file.path || "")}">📦 ${escapeHtml(file.name || "文件")}<span class="cd-row-meta">${escapeHtml(file.relative_path || "")}</span></button>`).join("") || '<div class="cd-empty">任务的真实交付物会在这里出现。</div>';
    const html = `<div class="cd-tabs"><button class="btn cd-tab" data-cd-mobile="work">🖥 工作</button><button class="btn cd-tab" data-cd-mobile="manage">📋 管理</button></div><div class="cd-production" data-mobile-view="${state.view}"><aside class="cd-side"><section class="cd-pane"><div class="cd-pane-title"><span>项目</span><button class="btn" id="cdNewProject">＋</button></div><div>${projectRows()}</div><div class="cd-create-row" id="cdProjectForm" hidden><input class="adv-search" id="cdProjectTitle" placeholder="项目名称"><button class="btn btn-primary" id="cdProjectSave">创建</button></div></section><section class="cd-pane"><div class="cd-pane-title"><span>任务</span><button class="btn" id="cdNewTask" ${state.project ? "" : "disabled"}>＋</button></div><div>${taskRows()}</div><div class="cd-create-row" id="cdTaskForm" hidden><input class="adv-search" id="cdTaskGoal" placeholder="要在云电脑完成什么"><button class="btn btn-primary" id="cdTaskSave">创建</button></div></section><section class="cd-pane"><div class="cd-pane-title"><span>待你处理</span><span class="cd-badge waiting_human">${state.handoffs.length}</span></div>${handoffRows()}</section><section class="cd-pane"><div class="cd-pane-title"><span>可重放流程</span></div>${recipeRows()}</section></aside><main class="cd-main">${statusHtml()}<div class="cap-wb-actions"><button class="btn" id="cdShot">📷 截屏</button><button class="btn" id="cdApps">📱 应用</button><button class="btn" id="cdSync">🔄 同步</button><button class="btn" id="cdSaveRecipe" ${task.state === "completed" ? "" : "disabled"}>保存为流程</button><button class="btn" id="cdFiles">项目文件</button><button class="btn" id="stopCloudDesktop">停止真机</button></div><div class="cd-console-grid"><div class="cd-screen"><div class="cd-create-row"><input class="adv-search" id="cdSay" placeholder="一句话让军团在当前项目继续干"><button class="btn btn-primary" id="cdAsk">🚀 派军团</button></div><div class="cd-create-row"><input class="adv-search" id="cdCmd" placeholder="在真机执行 shell 命令"><button class="btn" id="cdRun">▶ 执行</button></div><pre id="cdOut">控制台就绪。关闭网页后任务仍保存在项目工作区。</pre><iframe src="${escapeHtml(state.url)}" title="我的云真机"></iframe></div><aside class="cd-pane cd-evidence"><div class="cd-pane-title"><span>交付 / 证据</span></div>${files}<div class="cd-pane-title cd-software-title"><span>生产软件</span></div>${softwareRows()}</aside></div></main></div>`;
    window.showAdvDetail("☁️ 云电脑工作台", html, "项目、任务、真机、待办、交付和流程属于同一现场；刷新或重开不会丢。");
    bindWorkspace();
  }

  async function open(target = {}) {
    window.showAdvDetail("☁️ 云电脑工作台", '<div class="muted">正在恢复项目、任务与独立 CUA 云真机…</div>', "关闭网页不会停止任务；重开会从持久 checkpoint 继续。");
    try {
      const [desktop, projects, handoffs, recipes, software] = await Promise.all([post("/ensure"), get("/workspace/projects"), get("/handoffs?pending_only=1"), get("/recipes"), get("/software")]);
      const session = desktop.session;
      if (!session?.session_url) throw new Error(desktop.reason || desktop.error || "未获得经核验的 noVNC 地址");
      state.url = session.session_url;
      state.projects = projects.projects || [];
      state.handoffs = handoffs.handoffs || [];
      state.recipes = recipes.recipes || [];
      state.software = software.items || [];
      const targetProject = target.project_id || state.project?.project_id;
      if (targetProject && state.projects.some(project => project.project_id === targetProject)) {
        await selectProject(targetProject, false);
      } else if (!state.project || !state.projects.some(project => project.project_id === state.project.project_id)) {
        state.project = null;
        if (state.projects.length) await selectProject(state.projects[0].project_id, false);
      }
      if (target.task_id) state.task = state.tasks.find(task => task.task_id === target.task_id) || state.task;
      state.view = "work";
      render();
    } catch (error) {
      window.showAdvDetail("☁️ 云电脑工作台", `<div class="muted">启动失败：${escapeHtml(error.message)}</div><button class="btn btn-primary" id="retryCloudDesktop">重试</button>`, "真实错误未被伪装为就绪。");
      $("#retryCloudDesktop").onclick = () => open(target);
    }
  }

  async function openHandoff(row) {
    if (!row) return;
    const needsProject = state.project?.project_id !== row.project_id;
    if (needsProject) await selectProject(row.project_id, false);
    state.task = state.tasks.find(task => task.task_id === row.task_id) || state.task;
    state.view = "work";
    if ($(".cd-production")) render();
    else await open({project_id: row.project_id, task_id: row.task_id});
  }

  function showHandoffNotification(row) {
    window.toast?.(`⚠️ ${row.title} · 打开云电脑后可进入对应真机`);
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const notification = new Notification(row.title, {
      body: row.instructions || "点击进入对应项目和任务现场",
      tag: `cloud-desktop-handoff:${uid()}:${row.handoff_id}`,
      renotify: false,
      data: {uid: uid(), handoff_id: row.handoff_id, project_id: row.project_id, task_id: row.task_id}
    });
    notification.onclick = () => {
      window.focus();
      open({project_id: row.project_id, task_id: row.task_id});
      notification.close();
    };
  }

  function notifyHandoffs(rows) {
    rows.forEach(showHandoffNotification);
  }

  function requestNotificationPermission() {
    if (handoffState.notificationRequested || !("Notification" in window) || Notification.permission !== "default") return;
    handoffState.notificationRequested = true;
    Notification.requestPermission().catch(() => {});
  }

  async function pollHandoffs() {
    if (handoffState.polling || document.visibilityState === "hidden") return;
    handoffState.polling = true;
    try {
      const result = await get("/handoffs?pending_only=1");
      if (!result.ok) return;
      const rows = result.handoffs || [];
      const changed = rows.length !== handoffState.known.size
        || rows.some(row => handoffState.known.get(row.handoff_id) !== row.version);
      const incoming = handoffState.initialized
        ? rows.filter(row => handoffState.known.get(row.handoff_id) !== row.version)
        : rows;
      state.handoffs = rows;
      handoffState.known = new Map(rows.map(row => [row.handoff_id, row.version]));
      handoffState.initialized = true;
      if (changed) {
        const projects = await get("/workspace/projects");
        if (projects.ok) state.projects = projects.projects || [];
        if ($(".cd-production")) render();
      }
      if (incoming.length) notifyHandoffs(incoming);
    } finally {
      handoffState.polling = false;
    }
  }

  function startHandoffPolling() {
    if (handoffState.timer) return;
    pollHandoffs();
    handoffState.timer = window.setInterval(pollHandoffs, 5000);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") pollHandoffs();
    });
  }

  function output(text) {
    const target = $("#cdOut");
    if (!target) return;
    target.textContent += "\n" + text;
    target.scrollTop = target.scrollHeight;
  }

  async function command(path, body, label) {
    output("… " + label);
    const result = await post(path, body);
    output(result.ok ? `${label} ✓` : `${label} ✗ ${result.error || result.reason || ""}`);
    if (result.stdout) output(String(result.stdout).slice(0, 2000));
    return result;
  }

  function bindWorkspace() {
    requestNotificationPermission();
    document.querySelectorAll("[data-cd-mobile]").forEach(button => button.onclick = () => { state.view = button.dataset.cdMobile; render(); });
    document.querySelectorAll("[data-cd-project]").forEach(button => button.onclick = () => selectProject(button.dataset.cdProject));
    document.querySelectorAll("[data-cd-task]").forEach(button => button.onclick = async () => { state.task = state.tasks.find(task => task.task_id === button.dataset.cdTask) || null; await loadQuality(); render(); });
    $("#cdNewProject").onclick = () => { $("#cdProjectForm").hidden = false; };
    $("#cdProjectSave").onclick = async () => { const title = $("#cdProjectTitle").value.trim(); if (!title) return; const result = await post("/workspace/project/create", {title}); if (result.ok) { state.project = result.project; state.projects = [result.project, ...state.projects.filter(project => project.project_id !== result.project.project_id)]; state.tasks = []; state.task = null; render(); } else window.toast?.(result.error || "创建失败"); };
    if ($("#cdNewTask")) $("#cdNewTask").onclick = () => { $("#cdTaskForm").hidden = false; };
    if ($("#cdTaskSave")) $("#cdTaskSave").onclick = async () => { const goal = $("#cdTaskGoal").value.trim(); if (!goal || !state.project) return; const result = await post("/workspace/task/create", {project_id: state.project.project_id, goal, session_id: window.curSession || "", team_id: window.liveTeamId || "", execution_spec: {kind: "quality_delivery", max_quality_rounds: 3, quality_wall_clock_s: 7200}}); if (result.ok) { state.task = result.task; await selectProject(state.project.project_id); } else window.toast?.(result.error || "创建失败"); };
    document.querySelectorAll("[data-handoff-open]").forEach(button => button.onclick = () => { const card = button.closest("[data-handoff]"); openHandoff(state.handoffs.find(row => row.handoff_id === card.dataset.handoff)); });
    document.querySelectorAll("[data-handoff-done],[data-handoff-cancel]").forEach(button => button.onclick = async () => { const card = button.closest("[data-handoff]"); const projectId = card.dataset.projectId; const taskId = card.dataset.taskId; const handoffId = card.dataset.handoff; const result = await post("/handoff/decide", {project_id: projectId, handoff_id: handoffId, decision: button.hasAttribute("data-handoff-done") ? "complete" : "decline", version: Number(button.dataset.version || 1)}); if (!result.ok) return window.toast?.(result.error || "处理失败"); handoffState.known.delete(handoffId); await open({project_id: projectId, task_id: taskId}); await pollHandoffs(); });
    document.querySelectorAll("[data-recipe]").forEach(button => button.onclick = async () => { const result = await post("/recipe/replay", {recipe_id: button.dataset.recipe, inputs: {project_id: state.project?.project_id || ""}}); if (!result.ok) return window.toast?.(result.error || "流程不可用"); sendToChat(`请在我的云真机重放流程 ${result.name} v${result.version}。${result.instruction}\n步骤：${JSON.stringify(result.steps)}\n当前项目：${state.project?.project_id || "未选择"}`); });
    document.querySelectorAll("[data-software]").forEach(button => button.onclick = async () => { const id = button.dataset.software; if (!confirm(`安装或升级 ${id}？系统会保存清单、真探测，失败自动回滚。`)) return; const result = await post("/software/transact", {software_id: id, action: "install", confirm: true}); window.toast?.(result.ok ? `✓ ${id} 已验证` : `✗ ${result.error || result.status || "安装失败"}`); await open(); });
    $("#cdRun").onclick = () => { const value = $("#cdCmd").value.trim(); if (value) command("/exec", {command: value, timeout_s: 60}, "命令"); };
    $("#cdShot").onclick = () => command("/screenshot", {}, "截屏");
    $("#cdApps").onclick = () => command("/app", {action: "list"}, "应用");
    $("#cdSync").onclick = () => command("/fs/sync_personal", {}, "同步个人数据");
    $("#stopCloudDesktop").onclick = async () => { await post("/session/stop"); await open(); };
    $("#cdFiles").onclick = () => window.openCloudFiles?.("projects", state.project?.project_id || "");
    $("#cdSaveRecipe").onclick = async () => { const name = prompt("流程名称", state.task?.goal || "云电脑流程"); if (!name) return; const result = await post("/recipe/create", {project_id: state.project.project_id, task_id: state.task.task_id, name}); window.toast?.(result.ok ? "✓ 已保存可重放流程" : result.error || "保存失败"); if (result.ok) await open(); };
    $("#cdAsk").onclick = startTask;
  }

  function sendToChat(promptText) {
    if (typeof window._setPrompt === "function") window._setPrompt(promptText);
    else if ($("#q")) $("#q").value = promptText;
    $("#send")?.click();
  }

  function resumeInChat(task) {
    sendToChat(`继续云电脑生产任务 ${task.task_id || ""}：${task.goal || ""}。用户已完成待办，请读取项目 ${task.project_id || ""} 的 checkpoint 和当前真机现场后继续，不要重做已完成步骤。`);
  }

  async function startTask() {
    const goal = $("#cdSay").value.trim();
    if (!goal) return;
    if (!state.project) {
      const created = await post("/workspace/project/create", {title: goal.slice(0, 40) || "云电脑项目", brief: goal});
      if (!created.ok) return output("创建项目失败：" + created.error);
      state.project = created.project;
    }
    if (!state.task || ["completed", "partial", "failed", "cancelled"].includes(state.task.state)) {
      const created = await post("/workspace/task/create", {project_id: state.project.project_id, goal, session_id: window.curSession || "", team_id: window.liveTeamId || "", execution_spec: {kind: "quality_delivery", max_quality_rounds: 3, quality_wall_clock_s: 7200}});
      if (!created.ok) return output("创建任务失败：" + created.error);
      state.task = created.task;
    }
    const updated = await post("/workspace/task/update", {project_id: state.project.project_id, task_id: state.task.task_id, state: "running", phase: "军团执行中", version: state.task.version, checkpoint: {goal, session_id: window.curSession || "", team_id: window.liveTeamId || ""}});
    if (updated.ok) state.task = updated.task;
    sendToChat(`请在我的云真机上完成：${goal}。当前生产项目：${state.project.project_id}；任务：${state.task.task_id}。先读该任务 checkpoint 和项目 README；真实操作 IDE/浏览器/办公或设计软件。每步写回阶段、证据和恢复点；遇登录/注册/验证码/隐私授权创建人类待办，付款/下单/发消息/公开发布另需明确确认；完成时验证 README、主产物、截图和轨迹，并如实标 completed/partial/failed。`);
    output("🚀 已派军团，并持久记录项目与 checkpoint。");
  }

  window.openCloudDesktopPanel = open;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", startHandoffPolling);
  else startHandoffPolling();
})();
