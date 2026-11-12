import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(process.env.LEGION_UI_FILE || new URL("../web/index_v2.html", import.meta.url), "utf8");

// Execute the page's actual functions without page startup, a browser, or network.
function pageFunction(name) {
  const start = html.search(new RegExp(`^(?:async )?function ${name}\\(`, "m"));
  if (start < 0) return ""; // Newly added helpers are absent in the red baseline.
  for (let end = html.indexOf("}", start); end >= 0; end = html.indexOf("}", end + 1)) {
    const code = html.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw new Error(`Incomplete page function: ${name}`);
}

// Only the DOM operations used by these consumers; no layout/visual claims.
class Element {
  constructor(tag = "div") {
    Object.assign(this, { tag, children: [], style: {}, dataset: {}, attributes: {},
      className: "", textContent: "", isConnected: true, scrollHeight: 0 });
    this.classList = {
      contains: (c) => this.className.split(/\s+/).includes(c),
      add: (c) => { if (!this.classList.contains(c)) this.className += ` ${c}`; },
      remove: (c) => { this.className = this.className.split(/\s+/).filter((x) => x !== c).join(" "); },
      toggle: (c, force) => {
        const open = force ?? !this.classList.contains(c);
        this.classList[open ? "add" : "remove"](c); return open;
      },
    };
  }
  addEventListener(type, listener) { (this.listeners ||= new Map()).set(type, listener); }
  dispatch(type) { this.listeners?.get(type)?.(); }
  get scrollHeight() { return this.measureHeight ? this.measureHeight() : this.height || 0; }
  set scrollHeight(value) { this.height = value; }
  get scrollTop() { return this.top || 0; }
  set scrollTop(value) { this.top = this.measureHeight ? Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)) : value; }
  get childElementCount() { return this.children.length; }
  get childNodes() { return this.children; }
  setAttribute(name, value) {
    this.attributes[name] = String(value);
    if (name === "class") this.className = value;
    if (name.startsWith("data-")) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    if (name === "style") for (const part of value.split(";")) {
      const [key, val] = part.split(":"); if (key && val) this.style[key.trim()] = val.trim();
    }
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) {
    delete this.attributes[name];
    if (name.startsWith("data-")) delete this.dataset[name.slice(5)];
  }
  set innerHTML(value) {
    this.markup = String(value); this.children = [];
    // Flat descendants suffice for selectors exercised here; production builds rows.
    for (const tag of this.markup.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)) {
      const child = new Element(tag[1]);
      for (const attr of tag[2].matchAll(/([\w-]+)="([^"]*)"/g)) child.setAttribute(attr[1], attr[2]);
      this.appendChild(child);
    }
  }
  get innerHTML() { return this.markup || ""; }
  remove() { if(this.parentNode){const a=this.parentNode.children,i=a.indexOf(this);if(i>=0)a.splice(i,1);}this.parentNode=null; }
  appendChild(child) { child.remove();this.children.push(child);child.parentNode=this;return child; }
  prepend(child) { child.remove();this.children.unshift(child);child.parentNode=this; }
  replaceChildren(...children) { for(const child of [...this.children])child.remove();this.children=[];children.forEach(child=>this.appendChild(child)); }
  insertBefore(child, reference) {
    child.remove();child.parentNode=this;
    const index = this.children.indexOf(reference);
    if (index < 0) this.children.push(child); else this.children.splice(index, 0, child);
    return child;
  }
  insertAdjacentHTML(position, value) {
    assert.equal(position, "afterbegin");
    const child = new Element();
    child.innerHTML = value;
    this.children.unshift(child);
  }
  querySelectorAll(selector) {
    const all = this.children.flatMap((c) => [c, ...c.querySelectorAll("*")]);
    return all.filter((c) => selector.split(",").some((s) => {
      if (s === "*") return true;
      const data = s.match(/^\[data-([\w-]+)="([^"]+)"\]$/);
      if (data) return c.getAttribute(`data-${data[1]}`) === data[2] ||
        c.dataset[data[1].replace(/-([a-z])/g, (_, x) => x.toUpperCase())] === data[2];
      const [tag, ...classes] = s.split(".");
      return (!tag || c.tag === tag) && classes.every((x) => c.classList.contains(x));
    }));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

async function screen(mode, requestedMode = "auto") {
  const root = new Element(), chat = new Element(), sendButton = new Element(), qEl = { value: "fixture" };
  const intervals = new Map(); let clock = 0, pendingRead, sequence = 0;
  const finals = [], notices = [];
  const context = vm.createContext({
    document: { createElement: (tag) => new Element(tag), title: "fixture" },
    Date: { now: () => clock }, TextDecoder, AbortSignal, ci: () => root,
    setInterval: (fn) => { intervals.set(fn, fn); return fn; },
    clearInterval: (id) => intervals.delete(id),
    setTimeout: () => 1, clearTimeout() {},
    $: (s) => s === "#chat" ? chat : s === "#modeSelect" ? {value: requestedMode} : s === "#send" ? sendButton : s === "#q" ? qEl : null,
    qEl, uploads: [], _squad: [], _SQUAD_AGENTS: [], _sessionView: 0,
    _personalSubject: "__ut_owner", _personalEpoch: 1, _personalSwitchPending: false,
    _readPersonalStatus: async () => "__ut_owner", _origTitle: "fixture",
    running: false, liveTeamId: "", lastEventId: 0, curSession: "__ut_sid",
    _resumeCtx: null, _resumeTimer: null, _initialSourceUrl: '', querySourceConsumed: false, nameOf: (name) => name,
    esc: (s) => String(s).replaceAll("<", "&lt;"), _toolLabel: (name) => ({ name, desc: "" }),
    _stepPurpose: () => "", colorOf: () => "#123456", _avatarIndex: () => 0,
    artifactHTML: () => "<div>真实产物卡</div>", hydrateArtifacts() {},
    _toggleJumpBtn(show) { chat.jumpVisible = show; },
    currentQuerySource: () => ({}), loadSessions: async () => {},
    consumeQuerySource() {}, applyModeReason() {}, toast: (text) => notices.push(text),
    renderLegionAnswer(results, data, message) {
      finals.push({ results, data });
      message.querySelector(".st").textContent = answerScreen(results, data).status;
    },
    _bindReplayCard: (m,team) => { m.dataset.teamId=team;return (m._replay ||= { cursor: 0, complete: false }); },
    sessionStorage: { removeItem() {} },
    ...Object.fromEntries(["_chatScroll", "addUserMsg", "autosize", "_savePersonalDraft",
      "_renderModeFacts", "_memberDeliverySignal", "saveLive", "_renderMilestones"]
      .map((name) => [name, () => {}])),
    fetch: async (url) => {
      assert.equal(url, "/api/legion/dispatch", "unexpected request");
      return { body: { getReader: () => ({ read: () => new Promise((resolve) => { pendingRead = resolve; }) }) } };
    },
  });
  const functions = ["_scrollAtLatest", "_processScrollState", "_captureProgressScroll", "_restoreProgressScroll", "_syncProcessJump", "_syncProcessProgress", "_renderRuntimePulse", "newLegionMsg",
    "_renderLegionHeartbeat", "_renderAgentHeartbeat", "_finalDeliveryData",
    "_deliveryDisplay", "_turnSignature", "_appendArtifactStatus", "_finishArtifactStatus", "_markReplayTerminal", "notifyDone",
    "_renderProductMilestone", "_renderToolStep", "_loadMemberDeliveries", "_renderTerminalError", "_syncProcessMode", "_upgradeMsgMode", "_bindHistoryProcess", "_loadHistoryProcess", "pkGetLane", "pkActivate", "pkPrebuildLanes", "pkUpdateStatus", "applyLiveEvent", "_finishResume", "_handoffLiveStream", "resumeLiveV2", "send"];
  vm.runInContext([html.match(/^const _PHASE_LABELS=.*$/m)[0], html.match(/^const _PRODUCT_MILESTONES=.*$/m)[0], ...functions.map(pageFunction)].join("\n"), context);
  let sending;
  if (mode === "live") { sending = context.send(); await new Promise(setImmediate); }
  else context.newLegionMsg(requestedMode);
  const message = root.children[0], state = { steps: 0, eventId: 0 };
  context.applyLiveEvent("meta", { team_id: "__ut_team", sid: "__ut_sid" }, message, { steps: 0, eventId: ++sequence });
  const proc = message.querySelector(".proc-detail"), toggle = message.querySelector(".proc-toggle");
  if (requestedMode === "pk") {
    // Preserve the real PK nesting in this otherwise flat markup fixture.
    for (const selector of [".pk-tabs", ".pk-panels"]) {
      const element=message.querySelector(selector);
      message.children=message.children.filter(child=>child!==element);
      proc.appendChild(element);
    }
  }
  message.querySelector(".progress").style.display = "flex";
  return {
    message, proc, toggle, context, finals, notices, chat,
    async event(data, eventId = ++sequence) {
      if (mode === "resume") context.applyLiveEvent("step", data, message, { ...state, eventId });
      else {
        assert.ok(pendingRead, message.querySelector(".st").textContent);
        const resolve = pendingRead; pendingRead = null;
        resolve({ value: new TextEncoder().encode(`id: ${eventId}\nevent: step\ndata: ${JSON.stringify(data)}\n\n`), done: false });
        await new Promise(setImmediate);
      }
      state.steps = message._replaySteps || 0;
      assert.doesNotMatch(message.querySelector(".st").textContent, /连不上服务/);
    },
    async final(data, eventId = ++sequence) {
      if (mode === "resume") context.applyLiveEvent("final", data, message, { ...state, eventId });
      else {
        assert.ok(pendingRead, message.querySelector(".st").textContent);
        const resolve = pendingRead; pendingRead = null;
        resolve({ value: new TextEncoder().encode(`id: ${eventId}\nevent: final\ndata: ${JSON.stringify(data)}\n\n`), done: false });
        await new Promise(setImmediate);
      }
      assert.doesNotMatch(message.querySelector(".st").textContent, /连不上服务/);
    },
    async error(data, eventId = ++sequence) {
      if(mode==='resume')context.applyLiveEvent('error',data,message,{...state,eventId});
      else{const resolve=pendingRead;pendingRead=null;resolve({done:false,value:new TextEncoder().encode(`id: ${eventId}\nevent: error\ndata: ${JSON.stringify(data)}\n\n`)});await new Promise(setImmediate);}
      assert.doesNotMatch(message.querySelector('.st').textContent,/连不上服务/);
    },
    tick(ms) { clock = ms; for (const fn of intervals.values()) fn(); },
    finish() {
      // Exercise the real completion-fold block, not unrelated artifact rendering.
      const render = pageFunction("renderLegionAnswer");
      const start = render.indexOf("  // 完成后自动收起执行过程");
      assert.ok(start >= 0);
      context.message = message;
      vm.runInContext(`(function(m,results){${render.slice(start, -1)}})(message,[])`, context);
    },
    async close() { if (pendingRead) pendingRead({ done: true }); await sending; },
  };
}

const artifactRecord = () => freezeRecord({t:'artifact_ready',agent:'codex',url:'/outputs/fixture-game.html',
  name:'原作品.html',description:'原始说明：任务仍在继续，等最终导出确认',next_action:'等最终导出确认 <原文>',in_progress:true});
const artifactTerminals = [
  ['error',{status:'error',error:'真实错误'},/本轮出错/],
  ['cancel',{cancelled:true,outcome:{status:'cancelled'}},/本轮已停止/],
  ['final',{outcome:{status:'partial'}},/本轮已结束/],
];
function artifactFixture(ui) {
  ui.context.artifactHTML = a => `<div class="artifact"><a href="${a.url}">原文件</a><button class="artifact-preview">查看</button></div>`;
}
for(const mode of ['live','resume'])for(const [terminal,data,label] of artifactTerminals)for(const withPlan of [false,true])
test(`artifact 当前状态：${mode}/${terminal}/plan=${withPlan} 只结束动态提示，保留原文节点和按钮`,async()=>{
  const ui=await screen(mode);artifactFixture(ui);
  const record=freezeRecord({...artifactRecord(),next_action:withPlan?artifactRecord().next_action:''}),before=JSON.stringify(record);
  try {
    await ui.event(record);
    const status=ui.message.querySelector('.artifact-current-state');
    assert.ok(status,'artifact must have a distinct current-state node');
    assert.match(status.textContent,/仍在继续/);
    const row=status.parentNode,button=row.querySelector('.artifact-preview'),link=row.querySelector('a');
    const plan=row.querySelector('.artifact-recorded-plan');
    if(withPlan)assert.equal(plan.textContent,'当时计划：'+record.next_action);else assert.equal(plan,null);
    const markup=row.innerHTML;let clicks=0;button.addEventListener('click',()=>clicks++);
    ui.proc.scrollTop=67;
    if(terminal==='error')await ui.error(data);else await ui.final(data);
    assert.equal(ui.message.querySelector('.artifact-current-state'),status);
    assert.match(status.textContent,label);assert.doesNotMatch(status.textContent,/仍在继续|导出完成|验证通过/);
    assert.equal(row.innerHTML,markup);assert.equal(row.querySelector('.artifact-preview'),button);
    assert.ok(row.innerHTML.includes(record.description),'historical description must not be rewritten');
    assert.equal(link.getAttribute('href'),record.url);button.dispatch('click');assert.equal(clicks,1);
    if(withPlan)assert.equal(plan.textContent,'当时计划：'+record.next_action);
    assert.equal(ui.proc.scrollTop,67);assert.equal(JSON.stringify(record),before);
  } finally {await ui.close();}
});

for(const [terminal,data,label] of artifactTerminals)for(const lateDiscovery of [false,true])
test(`artifact 历史重放：${terminal}/laterIdentity=${lateDiscovery} 快照之后读取旧 in_progress，不复活且不改其他运行任务`,async()=>{
  const ui=await terminalProcess();artifactFixture(ui);
  const record=artifactRecord(),before=JSON.stringify(record);
  try {
    // Use the actual saved-answer renderer, then the actual paged history GET.
    answerScreen([],data,ui.message);
    // The session snapshot can arrive before session/teams supplies the stream
    // identity. This must not turn a known terminal historical card back on.
    if(lateDiscovery)ui.message._replay.executionId='event:1';
    ui.context.fetch=async()=>({ok:true,json:async()=>({team_id:'__ut_saved',last_event_id:1,
      events:[{event_id:1,ts:1,event:'step',data:record}]})});
    await ui.context._loadHistoryProcess(ui.message);
    const status=ui.message.querySelector('.artifact-current-state');assert.ok(status);
    assert.match(status.textContent,label);assert.doesNotMatch(status.textContent,/仍在继续/);
    assert.equal(ui.message.querySelector('.artifact-recorded-plan').textContent,'当时计划：'+record.next_action);
    assert.equal(ui.context.liveTeamId,'__ut_other_running');assert.equal(ui.context.running,true);
    assert.equal(ui.context.lastEventId,999);assert.equal(JSON.stringify(record),before);
  } finally {await ui.close();}
});

test('artifact 新执行代次不会把旧产物的错误状态改成运行或完成',async()=>{
  const ui=await screen('resume');artifactFixture(ui);
  try {
    await ui.event(artifactRecord());await ui.error({error:'上一轮出错'});
    const old=ui.message.querySelector('.artifact-current-state');assert.ok(old);
    ui.context.applyLiveEvent('meta',{team_id:'__ut_team',sid:'__ut_sid'},ui.message,{eventId:10});
    await ui.event({...artifactRecord(),url:'/outputs/new-game.html'},11);
    const states=ui.message.querySelectorAll('.artifact-current-state');assert.equal(states.length,2);
    assert.match(states[0].textContent,/本轮出错/);assert.match(states[1].textContent,/仍在继续/);
    await ui.final({results:[]},12);
    assert.match(states[0].textContent,/本轮出错/);assert.match(states[1].textContent,/本轮已结束/);
  } finally {await ui.close();}
});

for(const mode of ['live','resume'])for(const completed of [false,true])test(`${mode}: terminal error preserves returned members and invalidates pending reads (${completed})`,async()=>{
  const ui=await screen(mode),{context,message}=ui;
  context.rich=text=>text;
  const dispatch=context.fetch;let pending,reads=0;
  context.fetch=(url,options)=>{
    if(!url.startsWith('/api/legion/team?'))return dispatch(url,options);
    reads++;
    return new Promise(resolve=>{pending=resolve;});
  };
  context._loadMemberDeliveries(message);
  if(completed){
    pending({ok:true,json:async()=>({team_id:'__ut_team',tasks:{first:{status:'done',result:{status:'done',final:'already returned'}}}})});
    await new Promise(setImmediate);
    assert.equal(message._memberDeliveryCards.size,1);
    context._loadMemberDeliveries(message);
  }
  const returned=message._memberDeliveryCards?.get('first')?.card;
  context._loadMemberDeliveries(message); // queued reread must not escape the terminal.
  await ui.event({t:'tool_started',call_id:'7',tool:'command',agent:'codex'});
  await ui.event({t:'tool_started',call_id:'done',tool:'read',agent:'codex'});
  await ui.event({t:'tool_end',call_id:'done',tool:'read',agent:'codex',ok:true});
  await ui.error({error:'fixture whole-request error'});
  const note=message.querySelector('.member-delivery-note');
  assert.match(note.textContent,/本轮出错/);assert.doesNotMatch(note.textContent,/仍在运行/);
  const text=note.textContent;
  pending({ok:true,json:async()=>({team_id:'__ut_team',tasks:{late:{status:'done',result:{status:'done',final:'late response'}}}})});
  await new Promise(setImmediate);
  assert.equal(note.textContent,text);assert.equal(reads,completed?2:1);
  assert.equal(message._memberDeliveryAgain,false);assert.equal(message._memberDeliveryFinal,true);
  assert.equal(message._memberDeliveryCards?.has('late')||false,false);
  if(completed){assert.equal(message._memberDeliveryCards.get('first').card,returned);assert.match(note.textContent,/保留 1 项/);}
  const unfinished=message.querySelector('[data-tool="7"]');
  assert.equal(unfinished.dataset.toolObservation,'terminal_unobserved');
  assert.match(unfinished.querySelector('.tl').textContent,/未收到工具终态/);
  assert.doesNotMatch(unfinished.querySelector('.tl').textContent,/失败|failed|运行中/);
  assert.ok(message.querySelector('[data-tool="done"]').querySelector('.tl.ok'));
  await ui.close();
});

for(const mode of ['live','resume'])test(`${mode}: recorded PK restores selected single without dropping nodes or handlers`,async()=>{
  const ui=await screen(mode,'pk'),{context,message,proc}=ui;
  vm.runInContext(pageFunction('_renderModeFacts'),context);
  await ui.event({t:'tool_started',agent:'codex',tool:'command',call_id:'existing'});
  const lane=message._pkLanes.codex.querySelector('.pk-lane-body');
  const artifact=new Element('button');artifact.className='fixture-view-artifact';let clicks=0;
  artifact.onclick=()=>clicks++;lane.appendChild(artifact);
  scrollBox(lane);scrollBox(proc);lane.scrollTop=87;lane.dispatch('scroll');
  const oldRow=lane.querySelector('[data-tool="existing"]');
  await ui.event({t:'mode_selected',mode:'single',owner:'codex'});
  assert.equal(message._pkMode,false);assert.equal(proc.dataset.mode,'single');
  assert.equal(proc.classList.contains('pk-swimlanes'),false);
  assert.equal(proc.querySelector('.pk-panels'),null);
  assert.equal(proc.querySelector('[data-tool="existing"]'),oldRow);
  assert.equal(proc.querySelector('.fixture-view-artifact'),artifact);
  artifact.onclick();assert.equal(clicks,1);assert.equal(proc.scrollTop,87);
  await ui.event({t:'tasklist',mode:'pk',tasks:[]}); // Internal executor hint is not a new selected mode.
  assert.equal(message._pkMode,false);
  await ui.close();
});

for(const mode of ['live','resume'])test(`${mode}: PK system events stay inside their own lane rather than squeezing member panels`,async()=>{
  const ui=await screen(mode,'pk'),{message,proc}=ui;
  await ui.event({t:'tool_started',agent:'codex',tool:'command',call_id:'author'});
  const author=message._pkLanes.codex.querySelector('.pk-lane-body');
  const artifact=new Element('button');author.appendChild(artifact);
  for(let i=0;i<12;i++)await ui.event({t:'tool_started',agent:'legion',tool:'coordination',call_id:'system-'+i});
  assert.deepEqual(proc.children.map(child=>child.className),['pk-tabs','pk-panels']);
  assert.equal(author.children.includes(artifact),true);
  assert.equal(message._pkLanes.legion.querySelectorAll('.tool-row').length,12);
  assert.equal(message._pkActive,'codex');
  await ui.close();
});

test('auto-to-PK presentation moves existing process nodes instead of clearing them',async()=>{
  const ui=await screen('resume'),{context,message,proc}=ui;
  const original=new Element('button');let clicks=0;original.onclick=()=>clicks++;proc.appendChild(original);
  context._upgradeMsgMode(message,'pk');
  assert.equal(message._pkLanes.legion.querySelector('.pk-lane-body').children.includes(original),true);
  original.onclick();assert.equal(clicks,1);
});

// Deterministic geometry at the DOM boundary, running the actual SSE consumer/renderers.
// This verifies scroll behavior, not browser layout or screenshot appearance.
function scrollBox(el, viewport = 280) {
  el.clientHeight = viewport;
  el.measureHeight = () => 1200 + el.children.reduce((sum, row) => sum + Math.max(180, row.innerHTML.length), 0);
  el.scrollTop = el.scrollHeight;
  return el;
}
test("过程框高度：原生纵向调整有上下限，手机预留输入区，PK内容随框伸展", () => {
  const css = html.match(/^\.proc-detail\{([^}]+)\}/m)[1];
  assert.match(css, /resize:vertical/);
  assert.match(css, /overflow-y:auto/);
  assert.match(css, /height:min\(420px,50dvh\)/);
  assert.match(css, /min-height:min\(160px,var\(--proc-max-height\)\)/);
  assert.match(css, /max-height:var\(--proc-max-height\)/);
  assert.match(css, /100dvh - var\(--top-chrome\) - var\(--composer-clearance\) - 48px/);
  assert.match(html, /@media\(max-width:640px\)\{\.proc-detail\{height:min\(340px,45dvh\);min-height:min\(120px,var\(--proc-max-height\)\)\}\}/);
  assert.match(html, /\.pk-swimlanes\.proc-detail\.show\{display:flex;flex-direction:column\}/);
  assert.match(html, /\.pk-panels\{[^}]*flex:1;min-height:0/);
  assert.match(html, /\.pk-lane\{[^}]*height:100%/);
});
for (const mode of ["live", "resume"]) {
  test(`${mode}: 过程框高度调整后更新和折叠保留尺寸、阅读位置与回最新`, async () => {
    const ui = await screen(mode);
    try {
      scrollBox(ui.proc, 480);
      ui.proc.style.height = "480px"; // Native resize writes an inline height.
      await ui.event({t:"harness_step",x:"first"});
      ui.proc.scrollTop = 92; ui.proc.dispatch("scroll");
      await ui.event({t:"harness_step",x:"new step while reading"});
      assert.equal(ui.proc.scrollTop, 92);
      ui.toggle.onclick(); ui.proc.clientHeight = 0;
      await ui.event({t:"harness_step",x:"while collapsed"});
      ui.proc.clientHeight = 480; ui.toggle.onclick();
      assert.equal(ui.proc.style.height, "480px");
      assert.equal(ui.proc.scrollTop, 92);
      ui.message.querySelector(".proc-latest").onclick();
      await ui.event({t:"harness_step",x:"follow at resized height"});
      assert.equal(ui.proc.scrollTop, ui.proc.scrollHeight - 480);
      assert.equal(ui.proc.style.height, "480px");
    } finally { await ui.close(); }
  });
  test(`${mode}: every progress branch preserves reading position and follows only the previous bottom`, async () => {
    const ui = await screen(mode);
    const events = [
      {t:"task_started", agent:"codex"}, {t:"harness_step", x:"公开动作"},
      {t:"tool_started", tool:"bash", call_id:"one", args_brief:"short"},
      {t:"tool_end", tool:"bash", call_id:"one", ok:true, result_brief:"long".repeat(200)},
      {t:"tool_timeout", call_id:"one"}, {t:"heartbeat", stage:"executing", x:"已 3s"},
      {t:"heartbeat", stage:"verifying", x:"已 4s"}, {t:"reason", hb:true, agent:"codex", x:"已 5s"},
      {t:"first_artifact_reported", x:"首份候选"}, {t:"artifact_handoff", x:"产物接力"},
      {t:"artifact_ready", url:"/media/a.png"}, {t:"artifact_recovered", url:"/media/b.png"},
      {t:"weapon_candidates", candidates:[{name:"工具"}]}, {t:"verification_result", ok:true},
    ];
    try {
      scrollBox(ui.proc); scrollBox(ui.chat, 600);
      ui.chat.measureHeight = () => 2400 + ui.proc.scrollHeight;
      for (const [i, data] of events.entries()) {
        ui.proc.scrollTop = 91; ui.proc.dispatch("scroll");
        ui.chat.scrollTop = 123;
        await ui.event({...data, url:data.url && `${data.url}?paused=${i}`});
        assert.equal(ui.proc.scrollTop, 91, `${data.t}: preserve process reading`);
        assert.equal(ui.chat.scrollTop, 123, `${data.t}: preserve chat reading`);
        const jump = ui.message.querySelector(".proc-latest");
        assert.equal(jump?.hidden, false);
        jump.onclick();
        assert.equal(ui.proc.scrollTop, ui.proc.scrollHeight-ui.proc.clientHeight);
        assert.equal(ui.chat.scrollTop, 123, "process jump must not jump the chat");
        assert.equal(jump.hidden, true);
        ui.chat.scrollTop = ui.chat.scrollHeight;
        await ui.event({...data, url:data.url && `${data.url}-follow-${i}`});
        assert.equal(ui.proc.scrollTop, ui.proc.scrollHeight-ui.proc.clientHeight, `${data.t}: follow previous bottom`);
        assert.equal(ui.chat.scrollTop, ui.chat.scrollHeight-ui.chat.clientHeight, `${data.t}: chat follows even after large growth`);
      }
      ui.proc.scrollTop -= 20;
      await ui.event({t:"harness_step",x:"略微上翻也应停住"});
      assert.equal(ui.message.querySelector(".proc-latest").hidden, false);
    } finally { await ui.close(); }
  });

  test(`${mode}: scrolling to bottom resumes follow; collapse keeps the paused position`, async () => {
    const ui = await screen(mode);
    try {
      scrollBox(ui.proc);
      await ui.event({t:"harness_step",x:"first"});
      ui.proc.scrollTop = 90; ui.proc.dispatch("scroll");
      ui.toggle.onclick();
      ui.proc.clientHeight = 0; // display:none has zero layout dimensions.
      ui.proc.dispatch("scroll");
      await ui.event({t:"harness_step",x:"while collapsed"});
      ui.proc.clientHeight = 280;
      ui.toggle.onclick();
      assert.equal(ui.proc.scrollTop, 90);
      ui.proc.scrollTop = ui.proc.scrollHeight; ui.proc.dispatch("scroll");
      assert.equal(ui.message.querySelector(".proc-latest").hidden, true);
      await ui.event({t:"harness_step",x:"follow again"});
      assert.equal(ui.proc.scrollTop, ui.proc.scrollHeight-ui.proc.clientHeight);
    } finally { await ui.close(); }
  });
}

for (const mode of ["live", "resume"]) {
  test(`${mode}: PK members keep independent scroll positions, including hidden lanes`, async () => {
    const ui = await screen(mode, "pk");
    try {
      await ui.event({t:"tasklist", tasks:[{key:"a"},{key:"b"}]});
      const a=ui.context.pkGetLane(ui.message,"a"), b=ui.context.pkGetLane(ui.message,"b");
      scrollBox(a); scrollBox(b);
      Object.defineProperty(a,"clientHeight",{get:()=>ui.message._pkActive==="a"?280:0});
      Object.defineProperty(b,"clientHeight",{get:()=>ui.message._pkActive==="b"?280:0});
      a.scrollTop=97; a.dispatch("scroll");
      await ui.event({t:"harness_step",agent:"b",x:"hidden b update"});
      assert.equal(a.scrollTop,97);
      ui.context.pkActivate(ui.message,"b");
      assert.equal(b.scrollTop,b.scrollHeight-b.clientHeight,"hidden following lane catches up on activation");
      b.scrollTop=62; b.dispatch("scroll");
      ui.context.pkActivate(ui.message,"a");
      assert.equal(a.scrollTop,97);
      await ui.event({t:"harness_step",agent:"b",x:"hidden paused b update"});
      await ui.event({t:"harness_step",agent:"a",x:"active paused a update"});
      assert.equal(a.scrollTop,97);
      ui.message.querySelector(".proc-latest").onclick();
      assert.equal(a.scrollTop,a.scrollHeight-a.clientHeight);
      ui.context.pkActivate(ui.message,"b");
      assert.equal(b.scrollTop,62,"jumping a must not reset hidden b");
    } finally { await ui.close(); }
  });
}

async function terminalProcess() {
  const ui=await screen("resume"), {message,context}=ui;
  message.dataset.teamId="__ut_saved"; message.dataset.sid=context.curSession;
  message._replay={cursor:800,complete:true,terminalId:800};
  message._memberDeliveryFinal=true;
  message.querySelector(".st").textContent="本轮已停止";
  message.querySelector(".progress").style.display="none";
  context.liveTeamId="__ut_other_running"; context.lastEventId=999; context.running=true;
  context._resumeCtx={teamId:"__ut_other_running"};
  context._bindHistoryProcess(message,{ts:100});
  scrollBox(ui.proc);
  const requests=[];
  context.fetch=async(url,options)=>{
    requests.push({url,options});
    return {ok:true,json:async()=>({team_id:"__ut_saved",last_event_id:7,events:[
      {event_id:1,ts:1,event:"meta",data:{team_id:"__ut_saved",sid:"do-not-switch"}},
      {event_id:2,ts:2,event:"step",data:{t:"tool_started",tool:"bash",call_id:"saved",args_brief:"实际命令"}},
      {event_id:3,ts:3,event:"step",data:{t:"tool_end",tool:"bash",call_id:"saved",ok:false,result_brief:"真实报错"}},
      {event_id:4,ts:4,event:"step",data:{t:"approval",status:"waiting_approval"}},
      {event_id:5,ts:5,event:"step",data:{t:"task_completed",agent:"codex"}},
      {event_id:6,ts:6,event:"step",data:{t:"reasoning",channel:"analysis",x:"PRIVATE_REASONING_SENTINEL"}},
      {event_id:7,ts:7,event:"final",data:{results:[],outcome:{status:"completed"}}},
    ]})};
  };
  for(const name of ["saveLive","loadWorkspace","_memberDeliverySignal","notifyDone"])
    context[name]=()=>{throw new Error("history side effect: "+name);};
  return {...ui,requests};
}

test("历史详情：用户展开才 GET 本团队过程，复用真实工具渲染且不碰在线任务",async()=>{
  const ui=await terminalProcess();
  try {
    assert.equal(ui.requests.length,0);
    assert.match(ui.toggle.textContent,/执行详情/);
    ui.toggle.onclick(); await new Promise(setImmediate);
    assert.equal(ui.requests.length,1);
    assert.equal(ui.requests[0].url,"/api/legion/events?team_id=__ut_saved&after=0");
    assert.equal(ui.requests[0].options.method,undefined,"only GET, no dispatch/resume");
    assert.equal(ui.proc.querySelectorAll(".tool-row").length,1,"tool start/end update one real row");
    assert.match(ui.proc.querySelector(".tool-row").innerHTML,/真实报错/);
    assert.doesNotMatch(ui.proc.children.map(x=>x.innerHTML).join(""),/PRIVATE_REASONING_SENTINEL/);
    assert.equal(ui.message._replaySteps,4);
    assert.equal(ui.context.liveTeamId,"__ut_other_running");
    assert.equal(ui.context.lastEventId,999);
    assert.equal(ui.context.curSession,"__ut_sid");
    assert.equal(ui.context.running,true);
    assert.equal(ui.message._replay.complete,true);
    assert.equal(ui.message._replay.cursor,800);
    assert.equal(ui.message.querySelector(".st").textContent,"本轮已停止");
    assert.equal(ui.message.querySelector(".progress").style.display,"none");
    const count=ui.proc.children.length;
    ui.context.fetch=async url=>{ui.requests.push({url});return {ok:true,json:async()=>({team_id:"__ut_saved",last_event_id:7,events:[]})};};
    await ui.message.querySelector(".history-process-note").querySelector("button").onclick();
    assert.match(ui.requests[1].url,/after=7$/);
    assert.equal(ui.message._historyProcess.loaded,true);
    ui.toggle.onclick(); ui.toggle.onclick(); await new Promise(setImmediate);
    assert.equal(ui.requests.length,2,"loaded detail must not fetch again");
    assert.equal(ui.proc.children.length,count,"reopen must not duplicate rows");
  } finally {await ui.close();}
});

test("历史详情：响应失败可重新读取；不接受别的团队、切会话或切身份后的响应",async()=>{
  for(const scenario of ["http","team","session","identity","collapse","reopened"]){
    const ui=await terminalProcess();
    try {
      let resolve;
      ui.context.fetch=()=>new Promise(done=>{resolve=done;});
      ui.toggle.onclick();
      if(scenario==="session")ui.context._sessionView++;
      if(scenario==="identity")ui.context._personalEpoch++;
      if(scenario==="collapse")ui.toggle.onclick();
      if(scenario==="reopened")ui.message._memberDeliveryFinal=false;
      resolve({ok:scenario!=="http",status:403,json:async()=>({team_id:scenario==="team"?"other":"__ut_saved",last_event_id:1,events:[{event_id:1,event:"step",data:{t:"harness_step",x:"must not render"}}]})});
      await new Promise(setImmediate);
      assert.equal(ui.proc.children.length,0,scenario);
      assert.equal(ui.message._historyProcess.cursor,0,scenario);
      assert.equal(ui.message._historyProcess.loading,false);
      if(["http","team"].includes(scenario)){
        const note=ui.message.querySelector(".history-process-note");
        assert.match(note.textContent,/读取失败/);
        ui.context.fetch=async()=>({ok:true,json:async()=>({team_id:"__ut_saved",last_event_id:0,events:[]})});
        await note.querySelector("button").onclick();
        assert.match(note.textContent,/未找到/);
      }
    } finally {await ui.close();}
  }
});

test("历史详情：超过500条按after分页，过滤后的空页仍沿服务端游标继续",async()=>{
  const ui=await terminalProcess(),afters=[];
  try {
    ui.message._historyProcess.until=0;
    ui.context.fetch=async url=>{
      const after=Number(new URL(url,"https://fixture.test").searchParams.get("after"));afters.push(after);
      // The second raw page contains only private events, omitted by the server.
      const end=Math.min(after+500,1003),events=after===500?[]:
        Array.from({length:end-after},(_,index)=>({event_id:after+index+1,event:"step",data:{t:"harness_step",x:"saved "+(after+index+1)}}));
      return {ok:true,json:async()=>({team_id:"__ut_saved",last_event_id:end,events})};
    };
    ui.toggle.onclick();await new Promise(setImmediate);
    assert.equal(ui.message._historyProcess.cursor,500);
    for(let page=0;page<3;page++)await ui.context._loadHistoryProcess(ui.message);
    assert.deepEqual(afters,[0,500,1000,1003]);
    assert.equal(ui.message._replaySteps,503);
    assert.equal(ui.message._historyProcess.loaded,true);
  } finally {await ui.close();}
});

test("历史详情：旧轮只读截至该轮的事件，分页重复 ID 不重放",async()=>{
  const ui=await terminalProcess();
  try {
    ui.context.fetch=async()=>({ok:true,json:async()=>({team_id:"__ut_saved",last_event_id:4,events:[
      {event_id:1,ts:80,event:"step",data:{t:"harness_step",x:"old"}},
      {event_id:1,ts:80,event:"step",data:{t:"harness_step",x:"duplicate"}},
      {event_id:2,ts:101,event:"step",data:{t:"harness_step",x:"later turn"}},
    ]})});
    ui.toggle.onclick(); await new Promise(setImmediate);
    assert.equal(ui.message._replaySteps,1);
    assert.equal(ui.message._historyProcess.loaded,true);
    assert.doesNotMatch(ui.proc.children.map(x=>x.innerHTML).join(""),/duplicate|later turn/);
  } finally {await ui.close();}
});

test("live/resume expose real progress, preserve manual collapse, and exclude pulses/private reasoning", async () => {
  for (const mode of ["live", "resume"]) {
    const ui = await screen(mode);
    try {
      await ui.event({ t: "reason", agent: "codex", hb: true, x: "仍在工作（已 3s）" });
      assert.equal(ui.toggle.style.display, "none", `${mode}: heartbeat must not reveal a real-step entry`);
      await ui.event({ t: "tool_started", agent: "codex", tool: "command_execution", call_id: "one", args_brief: "fixture" });
      assert.equal(ui.toggle.style.display, "inline-flex", `${mode}: first real event must reveal the process entry`);
      assert.match(ui.toggle.textContent, /1步/);
      assert.equal(ui.proc.classList.contains("show"), true);
      ui.toggle.onclick();
      assert.equal(ui.proc.classList.contains("show"), false);
      await ui.event({ t: "tool_end", agent: "codex", tool: "command_execution", call_id: "one", ok: true });
      assert.match(ui.toggle.textContent, /2步/);
      await ui.event({ t: "tool_end", agent: "codex", tool: "command_execution", call_id: "one", ok: true }, 4);
      assert.match(ui.toggle.textContent, /2步/, `${mode}: replaying an event ID must not count it twice`);
      assert.equal(ui.proc.classList.contains("show"), false, `${mode}: another event must not reopen a user fold`);
      assert.equal(ui.toggle.getAttribute("aria-expanded"), "false");
      await ui.event({ t: "heartbeat", stage: "executing", x: "执行中（已 6s）" });
      await ui.event({ t: "reason", heartbeat: true, agent: "codex", x: "活动信号" });
      await ui.event({ t: "reason", agent: "codex", x: "成员正在执行任务（已 8s）" });
      await ui.event({ t: "reasoning", agent: "codex", x: "PRIVATE_REASONING_SENTINEL" });
      await ui.event({ t: "harness_step", channel: "analysis", x: "PRIVATE_REASONING_SENTINEL" });
      assert.match(ui.toggle.textContent, /2步/);
      assert.doesNotMatch(ui.proc.children.map((c) => c.innerHTML).join(""), /PRIVATE_REASONING_SENTINEL/);
      ui.message.querySelector(".progress").onclick();
      assert.equal(ui.proc.classList.contains("show"), true);
      await ui.event({ t: "harness_step", agent: "codex", x: "公开动作" });
      assert.match(ui.toggle.textContent, /3步/);
      ui.tick(65000);
      assert.match(ui.message.querySelector(".elapsed-timer").textContent, /已运行 1分5s/);
      ui.finish();
      assert.match(ui.toggle.textContent, /3步/);
      assert.equal(ui.proc.classList.contains("show"), false);
      ui.toggle.onclick();
      assert.equal(ui.proc.classList.contains("show"), true, `${mode}: completed process remains inspectable`);
    } finally { await ui.close(); }
  }
});

test("a user fold before the first real action survives; pulses alone never gain a completion count", async () => {
  for (const mode of ["live", "resume"]) {
    const ui = await screen(mode);
    try {
      await ui.event({ t: "reasoning", x: "PRIVATE_REASONING_SENTINEL" });
      await ui.event({ t: "heartbeat", stage: "executing", x: "运行中（已 3s）" });
      ui.finish();
      assert.equal(ui.toggle.style.display, "none");
      assert.equal(ui.message._replaySteps, 0);
      const progress = ui.message.querySelector(".progress");
      progress.onclick(); progress.onclick();
      await ui.event({ t: "task_started", agent: "codex", phase: "执行任务" });
      assert.equal(ui.toggle.style.display, "inline-flex");
      assert.match(ui.toggle.textContent, /1步/);
      assert.equal(ui.proc.classList.contains("show"), false);
    } finally { await ui.close(); }
  }
});

test("all inline page scripts remain syntactically valid without executing page startup", () => {
  const scripts = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  assert.ok(scripts.length);
  for (const [, code] of scripts) assert.doesNotThrow(() => new vm.Script(code));
});

test("milestones do not turn quoted task text into relay or model-switch facts", () => {
  const inserted = [];
  const proc = {
    querySelector: () => null,
    querySelectorAll: (selector) => selector === ".proc-step"
      ? [{ textContent: "不要换模型或自救；未发生接手补位；artifact只是参考字符串" }] : [],
  };
  const message = { querySelector: () => null, insertBefore: (node) => inserted.push(node) };
  const context = vm.createContext({
    document: { createElement: () => ({}) }, esc: String,
  });
  vm.runInContext(pageFunction("_renderMilestones"), context);
  context._renderMilestones(message, proc, [{
    status: "done", model_used: "one-model", prior_attempts: [], relay_by: "", relayed_from: "",
  }]);
  assert.equal(inserted.length, 0, "task wording is not a runtime milestone");
  context._renderMilestones(message, proc, [{
    status: "done", model_used: "one-model", artifacts: [{ name: "actual.md" }],
  }]);
  assert.equal(inserted.length, 1);
  assert.match(inserted[0].innerHTML, /文件.*上报/);
  assert.doesNotMatch(inserted[0].innerHTML, /换模型|补位/);
});

// Files are independent runtime facts, even when the producing attempt failed.
// Exercise the full answer renderer and its real artifact links, without hydration/network.
function freezeRecord(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.values(value).forEach(freezeRecord);
    Object.freeze(value);
  }
  return value;
}

function answerScreen(results, data, existingMessage = null) {
  freezeRecord(results); freezeRecord(data);
  const message = existingMessage || new Element();
  if (!existingMessage) message.innerHTML = '<div class="legion-answers"></div><span class="st"></span><div class="next"></div>';
  const context = vm.createContext({
    document: { createElement: (tag) => new Element(tag) },
    nameOf: (name) => name, colorOf: () => "#5b8cff", _avatarIndex: () => 0,
    rich: (text) => String(text), _usageBrief: () => "未上报", _num: String,
    _renderModeFacts() {}, hydrateArtifacts() {}, _syncProcessProgress() {}, _renderMilestones() {},
    _captureProgressScroll() { return null; }, _restoreProgressScroll() {},
    fetch() { throw new Error("产物渲染测试不允许访问网络"); },
  });
  vm.runInContext([
    "esc", "_artifactPresentationRole", "_chooseVisibleDeliveries",
    "_artifactPurpose", "_artifactSizeLabel", "_toolLabel", "artifactHTML",
    "_looksInternalFailure", "_isVisualArtifact", "_requestedPpt",
    "_bindResultTabs", "_deliveryDisplay", "_planningFailureHTML", "_taskFailureHTML", "_finishArtifactStatus", "renderLegionAnswer",
  ].map(pageFunction).join("\n"), context);
  context.renderLegionAnswer(results, data, message);
  const answers = message.querySelector(".legion-answers");
  return {
    context, message, answers,
    summary: answers.querySelector(".deliver-summary"),
    downloads: answers.querySelectorAll("a").filter((node) => node.getAttribute("download") !== null),
    artifacts: answers.querySelectorAll(".artifact"),
    status: message.querySelector(".st").textContent,
  };
}

test("规划诊断：0成员的持久规划失败显示具体原因与恢复事实，取消状态保留",()=>{
  const outcome={status:"failed",failure_stage:"planning",planner_error:"planner_tool_unavailable",
    planner_attempts:[{error:"unavailable"}],recovery:{endpoint:"/api/legion/team/resume",session_id:"saved-session",team_id:"original-team",automatic_started:false}};
  for(const data of [{outcome}, {planner_error:outcome.planner_error,recovery:outcome.recovery,outcome},
    {cancelled:true,outcome:{...outcome,status:"cancelled"}}]){
    const ui=answerScreen([],data);
    assert.match(ui.status,data.cancelled?/本轮已停止/:/本轮规划未完成.*尚未派出成员/);
    const markup=ui.answers.children.map(row=>row.innerHTML).join("\n");
    assert.match(markup,/规划所需工具不可用/);
    assert.match(markup,/planner_tool_unavailable/);
    assert.match(markup,/原会话 saved-session/);
    assert.match(markup,/原任务 original-team/);
    assert.match(markup,/尚未自动启动/);
    assert.match(markup,/恢复入口不代表问题已解决/);
    assert.doesNotMatch(markup,/onclick=.*(?:resume|dispatch)/);
  }
});

test("规划诊断：持久错误或恢复事实变化会刷新同一轮；未知信息不造承诺",()=>{
  const screen=historyScreen(),base={question:"q",team_id:"team",status:"error",results:[],ts:1};
  const old=screen.context.renderSessionTurn(base,0);screen.attach(old);
  const changed=screen.context.renderSessionTurn({...base,outcome:{status:"failed",failure_stage:"planning",planner_error:"planner_tool_unavailable"}},0);
  assert.equal(changed[1],old[1]);assert.equal(changed[1].renderCount,2);
  assert.match(changed[1].recordedDisplay,/规划未完成/);
  const data={outcome:{failure_stage:"planning",planner_error:'<img src=x onerror="alert(1)">',recovery:{endpoint:"/recorded",automatic_started:true}}};
  const ui=answerScreen([],data),markup=ui.answers.children.map(x=>x.innerHTML).join("\n");
  assert.match(markup,/&lt;img/);assert.doesNotMatch(markup,/<img src=x/);
  assert.match(markup,/是否已启动以实际运行记录为准/);
  assert.doesNotMatch(markup,/尚未自动启动|会自动恢复|恢复成功/);
});

test("执行诊断：只显示最后一次已保存公开动作，不捏造补位或泄漏私有思考",()=>{
  const rows=[{key:"codex",status:"error",error:"cancelled",tool_trace:[
    {t:"tool_started",tool:"bash",brief:"first"},
    {t:"tool_end",tool:"bash",ok:false,brief:"最后的实际报错 <script>"},
    {t:"reasoning",channel:"analysis",x:"PRIVATE_REASONING_SENTINEL"},
  ]}];
  const ui=answerScreen(rows,{}),diag=ui.answers.querySelector('[data-pane="diag"]').innerHTML;
  assert.match(diag,/最后一次已记录动作.*bash.*已返回失败.*最后的实际报错 &lt;script&gt;/);
  assert.doesNotMatch(diag,/PRIVATE_REASONING_SENTINEL|已尝试补位|已经恢复/);
  const empty=answerScreen([{key:"codex",status:"error"}],{}).answers.querySelector('[data-pane="diag"]').innerHTML;
  assert.doesNotMatch(empty,/最后一次|已尝试补位/);
});

test("审计展示：只有有效通过结论可标推荐，返工/失败/未验证不冒充通过", () => {
  const rows = [{key: "a", task_id: "a", harness: "codex", status: "done", final: "candidate"}];
  const variants = [
    {verdict: "pass", profile: "ordinary", frontier: null, recommended: true},
    {verdict: "revise", profile: "ordinary", frontier: null, recommended: false},
    {verdict: "fail", profile: "ordinary", frontier: null, recommended: false},
    {verdict: "blocked", profile: "ordinary", frontier: null, recommended: false},
    {verdict: "unverified", profile: "ordinary", frontier: null, recommended: false},
    {verdict: "pass", profile: "frontier", frontier: {proven: false}, recommended: false},
    {verdict: "pass", profile: "frontier", frontier: {proven: true}, recommended: true},
  ];
  for (const variant of variants) {
    const data = {winner: "a", selection: {source: "success_audit", selected_task_id: "a"},
      success_audit: {enabled: true, winner: "a", verdict: variant.verdict, profile: variant.profile,
        ...(variant.frontier ? {frontier: variant.frontier} : {}), reason: "audit evidence"}};
    const ui = answerScreen(rows, data);
    assert.equal(ui.answers.innerHTML.includes("🏆 推荐"), variant.recommended, JSON.stringify(variant));
    assert.match(ui.answers.innerHTML, /audit evidence/, "actual audit reason must remain visible");
    if (!variant.recommended) assert.match(ui.answers.innerHTML, /审计结论/);
  }
});

test("审计和评委只为各自选中的候选授予推荐", () => {
  const rows = [{key: "a", task_id: "task-a", harness: "codex", status: "done", final: "A"},
                {key: "b", task_id: "task-b", harness: "cc", status: "done", final: "B"}];
  const audit = {enabled: true, verdict: "pass", winner: "task-b", reason: "audit chose B"};
  const conflicts = [
    {winner: "a", success_audit: audit},
    {winner: "a", selection: {source: "success_audit", selected_task_id: "task-b"}, success_audit: audit},
    {winner: "a", judge: {winner: "b", reason: "judge chose B"}},
  ];
  for (const data of conflicts) {
    const ui = answerScreen(rows, data);
    assert.doesNotMatch(ui.answers.innerHTML, /🏆 推荐/);
    assert.doesNotMatch(ui.answers.innerHTML, /audit chose B|judge chose B/,
                        "another candidate's reason cannot be attributed to A");
  }
  const validAudit = answerScreen(rows, {winner: "task-b",
    selection: {source: "success_audit", selected_task_id: "task-b"}, success_audit: audit});
  assert.match(validAudit.answers.innerHTML, /🏆 推荐/);
  assert.match(validAudit.answers.innerHTML, /audit chose B/);
  const validJudge = answerScreen(rows, {winner: "b", judge: {winner: "b", reason: "judge chose B"}});
  assert.match(validJudge.answers.innerHTML, /🏆 推荐/);
});

test("整体审计选择冲突作为整单诊断保留，不冒充末棒的推荐依据", () => {
  const rows = [{key: "a", task_id: "A", status: "done", final: "earlier contribution"},
                {key: "b", task_id: "B", status: "done", final: "relay tail"}];
  const data = {winner: "", execution_mode: "relay", final_result: rows[1],
    selection: {source: "final_result_reference", selected_task_id: "B"},
    success_audit: {enabled: true, verdict: "unverified", winner: "",
      reason: "cannot verify the whole delivery", winner_rejected: {
        alias: "candidate_1", reason: "bundle_requires_delivery_bundle"}}};
  const ui = answerScreen(rows, data);
  assert.doesNotMatch(ui.answers.innerHTML, /🏆 推荐/);
  assert.match(ui.summary.innerHTML, /选择范围不匹配，未通过/);
  const notice = ui.answers.querySelectorAll(".diagnostic-card").find(x => /整体审计/.test(x.textContent));
  assert.ok(notice);
  assert.match(notice.textContent, /cannot verify the whole delivery/);
  assert.match(ui.answers.innerHTML, /earlier contribution/);
  assert.match(ui.answers.innerHTML, /relay tail/);
});

test("显式选中不等于独立质量推荐，旧 winner 也不冒充审计", () => {
  const rows = [{key: "a", task_id: "a", harness: "codex", status: "done", final: "candidate"}];
  for (const data of [
    {winner: "a", selection: {source: "explicit_winner", selected_task_id: "a"}},
    {winner: "a", selection: {source: "final_result_reference", selected_task_id: "a"}},
    {winner: "a"},
  ]) {
    const ui = answerScreen(rows, data);
    assert.doesNotMatch(ui.answers.innerHTML, /🏆 推荐/);
    assert.match(ui.answers.innerHTML, /当前选中/);
  }
});

test("未记录审计不是未启用审计；审计事实变化要刷新原会话卡", () => {
  const row = {key: "a", task_id: "a", status: "done", final: "candidate"};
  assert.match(answerScreen([row], {}).summary.innerHTML, /审计状态未记录/);
  const noAudit = {enabled: false, verdict: "not_required"};
  assert.match(answerScreen([row], {success_audit: noAudit}).summary.innerHTML, /未启用重型质量审计/);
  const {context} = historyScreen();
  const base = {ts: 1, status: "done", results: [row], winner: "a",
    selection: {source: "success_audit", selected_task_id: "a"},
    success_audit: {enabled: true, verdict: "pass", winner: "a", reason: "first"}};
  const revised = {...base, success_audit: {...base.success_audit, verdict: "revise", reason: "needs work"}};
  assert.notEqual(context._turnSignature(base), context._turnSignature(revised));
  const selected = {...base, selection: {...base.selection, selected_task_id: "b"}};
  assert.notEqual(context._turnSignature(base), context._turnSignature(selected));
  const scoreChange = {...base, success_audit: {...base.success_audit, scores: {a: {overall: 70}}}};
  assert.notEqual(context._turnSignature(base), context._turnSignature(scoreChange));
});

const GAME_URL = "/home-outputs/codex_web_11a7a60aba57_1792296493_task_codex/pelican-bike/dist/index.html";
const gameArtifact = () => ({ name: "index.html", url: GAME_URL, kind: "html", bytes: 765050 });

test("交付入口：模板源码不是可运行网页，保留下载不提供假运行按钮", () => {
  const { context } = answerScreen([], {});
  const markup = context.artifactHTML({
    name: "index.template.html", kind: "web", url: "/home-outputs/__ut/src/index.template.html",
  });
  assert.match(markup, /模板源码/);
  assert.match(markup, /download=/);
  assert.doesNotMatch(markup, /data-lb="html"|data-preview|在线访问/);
});

test("最近产物：使用同一角色判断，构建入口先于折叠源码，模板没有打开按钮", () => {
  const { context } = answerScreen([], {});
  let body = "";
  Object.assign(context, {
    window: { _recentArts: [
      { sid: "__ut_sid", name: "index.template.html", url: "/home-outputs/__ut/src/index.template.html" },
      { sid: "__ut_sid", ...gameArtifact() },
      { sid: "__ut_sid", name: "build.py", url: "/home-outputs/__ut/build.py" },
    ] },
    _artsMarkSeen() {}, _renderRecentBadge() {},
    showAdvDetail(title, html) { body = html; },
  });
  context.document.querySelectorAll = () => [];
  vm.runInContext(["_groupArtsBySession", "_canPreviewInBrowser", "_artIcon", "_artRowHtml", "openRecentArtifacts"]
    .map(pageFunction).join("\n"), context);
  assert.equal(context._canPreviewInBrowser("index.template.html", "web"), false);
  context.openRecentArtifacts();
  assert.ok(body.indexOf('data-ra-open="0_1"') < body.indexOf('data-ra-body="0"'));
  assert.doesNotMatch(body, /data-ra-open="0_0"/);
  assert.match(body, /模板源码/);
  assert.match(body, /build\.py/);
});

test("交付入口：从真实附件中优先展示唯一构建入口，不把截图或模板当成品", () => {
  const { context } = answerScreen([], {});
  const rows = freezeRecord([
    { name: "shot.png", url: "/home-outputs/__ut/shots/shot.png" },
    { name: "index.template.html", url: "/home-outputs/__ut/src/index.template.html" },
    { name: "source.js", url: "/home-outputs/__ut/src/source.js" },
    gameArtifact(),
    { name: "README.md", url: "/home-outputs/__ut/README.md" },
  ]);
  assert.deepEqual(Array.from(context._chooseVisibleDeliveries(rows), (a) => a.url), [GAME_URL]);
  assert.deepEqual(Array.from(context._chooseVisibleDeliveries([
    rows[1], { name: "index.html", path: "/unpublished/index.html" },
  ])), []);
});

test("交付入口：显式交付角色优先；多个网页不冒选一个质量冠军", () => {
  const { context } = answerScreen([], {});
  const report = { name: "report.pdf", url: "/home-outputs/__ut/report.pdf", role: "primary" };
  assert.equal(context._chooseVisibleDeliveries([gameArtifact(), report])[0], report);
  const alternative = { name: "other.html", url: "/home-outputs/__ut/build/other.html" };
  assert.equal(context._chooseVisibleDeliveries([gameArtifact(), alternative]).length, 2);
  for (const url of ["javascript:alert(1)", "data:text/html,unsafe", "//outside.example/file"]) {
    assert.equal(context._chooseVisibleDeliveries([{ name: "index.html", url, is_primary: true }]).length, 0);
  }
});

test("会话成果：前轮文件与最新失败并列，不清历史或把末轮改成完成", () => {
  const context = vm.createContext({});
  vm.runInContext(["_artifactPresentationRole", "_chooseVisibleDeliveries", "_sessionDeliveryOverview"]
    .map(pageFunction).join("\n"), context);
  const turns = freezeRecord(Array.from({ length: 8 }, (_, i) => ({
    question: "original question " + i,
    result: { status: "error", results: [{ status: "error", artifacts: i ? [] : [
      gameArtifact(), { name: "trace.py", url: "/home-outputs/__ut/trace.py" },
    ] }] },
  })));
  const before = JSON.stringify(turns);
  const result = context._sessionDeliveryOverview(turns);
  assert.equal(result.turnCount, 8);
  assert.equal(result.primary[0].url, GAME_URL);
  assert.equal(result.latest.status, "error");
  assert.equal(result.fromEarlier, true);
  assert.equal(result.artifactCount, 2);
  assert.equal(JSON.stringify(turns), before);
});

test("会话成果：同一源文件的新快照置前，旧快照留在文件和原始历史里", () => {
  const context = vm.createContext({});
  vm.runInContext(["_artifactPresentationRole", "_chooseVisibleDeliveries", "_sessionDeliveryOverview"]
    .map(pageFunction).join("\n"), context);
  const exported = (snapshot) => ({
    name: "index.html", url: `/home-outputs/exports/scope/${snapshot}/index.html`,
    export_snapshot_id: snapshot,
    provenance: { source: "authorized-project-export", source_path: "/project/app/index.html" },
  });
  const turns = freezeRecord([
    { result: { status: "completed", artifacts: [exported("old")] } },
    { result: { status: "completed", artifacts: [exported("new")] } },
    { result: { status: "error", artifacts: [] } },
  ]);
  const before = JSON.stringify(turns);
  const view = context._sessionDeliveryOverview(turns);
  assert.deepEqual(Array.from(view.primary, a => a.export_snapshot_id), ["new"]);
  assert.equal(view.primary[0]._sourceTurn, 2);
  assert.equal(view.artifactCount, 2, "折叠旧版本不删除文件");
  assert.equal(view.latest.status, "error", "新版文件不改变执行终态");
  assert.equal(view.fromEarlier, true);
  assert.equal(JSON.stringify(turns), before, "不重写会话或产物元数据");
});

test("会话成果：相同文件名但来源不同不能合并，无来源旧记录也完整保留", () => {
  const context = vm.createContext({});
  vm.runInContext(["_artifactPresentationRole", "_chooseVisibleDeliveries", "_sessionDeliveryOverview"]
    .map(pageFunction).join("\n"), context);
  const rows = [
    { name: "index.html", url: "/home-outputs/a/index.html",
      provenance: { source: "authorized-project-export", source_path: "/project/a/index.html" } },
    { name: "index.html", url: "/home-outputs/b/index.html",
      provenance: { source: "authorized-project-export", source_path: "/project/b/index.html" } },
    { name: "index.html", url: "/home-outputs/legacy/index.html" },
  ];
  const view = context._sessionDeliveryOverview(rows.map(a => ({ result: { artifacts: [a] } })));
  assert.equal(view.primary.length, 3);
  assert.equal(new Set(Array.from(view.primary, a => a.url)).size, 3);
  assert.equal(view.primary[0]._sourceTurn, 3, "仅按新旧排序，不宣称质量胜出");
});

test("产物可达：error 尝试的真实网页、截图和源码仍可下载，不改成完成", () => {
  const results = [{
    key: "codex", harness: "codex", status: "error", error: "已写文件，随后执行失败",
    final: "不能以此文字把任务改成完成", artifacts: [
      gameArtifact(),
      { name: "shot.png", url: "/home-outputs/__ut/shot.png", bytes: 128 },
      { name: "game.js", url: "/home-outputs/__ut/game.js", bytes: 256 },
    ],
  }];
  const data = { status: "error", all_failed: true, outcome: { status: "failed" }, results };
  const before = JSON.stringify(data);
  const ui = answerScreen(results, data);
  assert.equal(ui.downloads.length, 3, "任务失败不能抹掉已经记录的文件");
  assert.ok(ui.downloads.some((link) => link.getAttribute("href") === GAME_URL + "?download=1"));
  assert.match(ui.summary.innerHTML, /3 份文件与预览/);
  assert.match(ui.summary.innerHTML, /未完成尝试.*不代表任务已完成/);
  assert.match(ui.status, /本轮未完成.*0\/1 项任务已返回/);
  assert.doesNotMatch(ui.answers.innerHTML + ui.summary.innerHTML, /本轮已完成|🏆 推荐|独立质量审计通过/);
  assert.match(ui.answers.querySelector('[data-pane="diag"]').innerHTML, /已写文件，随后执行失败/);
  assert.equal(JSON.stringify(data), before, "渲染不能修改输入状态、产物记录或完成结论");
});

test("产物可达：partial、cancelled、blocked 尝试同样保留明确产物", () => {
  for (const status of ["partial", "cancelled", "blocked"]) {
    const results = [{ key: "codex", harness: "codex", status, artifacts: [gameArtifact()] }];
    const ui = answerScreen(results, { outcome: { status }, results });
    assert.equal(ui.downloads.length, 1, status);
    assert.match(ui.summary.innerHTML, /未完成尝试.*不代表任务已完成/, status);
    assert.doesNotMatch(ui.status, /✓|本轮已完成/, status);
    assert.equal(results[0].status, status);
  }
});

test("产物可达：成功与失败成员引用同一文件仍去重，并保留实际来源", () => {
  const artifact = gameArtifact();
  const results = [
    { key: "build", harness: "codex", status: "error", error: "后续步骤失败",
      artifacts: [artifact, { ...artifact }] },
    { key: "review", harness: "hermes", status: "done", final: "成员回复",
      artifacts: [{ ...artifact }] },
  ];
  const ui = answerScreen(results, { winner: "review", outcome: { status: "partial" }, results });
  assert.equal(ui.artifacts.length, 1);
  assert.equal(ui.downloads.length, 1);
  assert.match(ui.answers.querySelector('[data-pane="visual"]').innerHTML, /来自 hermes、codex 的结果/);
  assert.match(ui.summary.innerHTML, /1 份文件与预览/);
  assert.match(ui.summary.innerHTML, /未完成尝试.*不代表任务已完成/);
  assert.match(ui.status, /本轮仍有未完成事项.*1\/2 项任务已返回/);
});

test("产物可达：同 URL 的不同版本元数据不被错误合并", () => {
  const results = [{
    key: "codex", harness: "codex", status: "done", final: "成员回复",
    artifacts: [
      { ...gameArtifact(), provenance: { sha256: "a".repeat(64) } },
      { ...gameArtifact(), provenance: { sha256: "b".repeat(64) } },
    ],
  }];
  const ui = answerScreen(results, { outcome: { status: "completed" }, results });
  assert.equal(ui.artifacts.length, 2);
  assert.equal(ui.downloads.length, 2);
  assert.match(ui.summary.innerHTML, /2 份文件与预览/);
});

test("产物可达：正常完成结果不被误标成失败尝试，仍使用原下载地址", () => {
  const results = [{ key: "codex", harness: "codex", status: "done", final: "成员回复",
    artifacts: [gameArtifact()] }];
  const ui = answerScreen(results, { outcome: { status: "completed" }, results });
  assert.equal(ui.downloads.length, 1);
  assert.equal(ui.downloads[0].getAttribute("href"), GAME_URL + "?download=1");
  assert.match(ui.status, /✓ 本轮已完成.*1\/1 项任务已返回/);
  assert.doesNotMatch(ui.summary.innerHTML, /未完成尝试/);
});

test("产物可达：失败文字提及路径不生成链接，空记录不伪造文件", () => {
  const results = [{ key: "codex", harness: "codex", status: "error",
    error: "执行失败", final: "计划生成 " + GAME_URL, artifacts: [null, {}] }];
  const ui = answerScreen(results, { all_failed: true, outcome: { status: "failed" }, results });
  assert.equal(ui.downloads.length, 0);
  assert.equal(ui.artifacts.length, 0);
  assert.match(ui.summary.innerHTML, /0 份文件与预览/);
  assert.doesNotMatch(ui.summary.innerHTML, /未完成尝试/);
  assert.match(ui.status, /本轮未完成/);
});

test("产物可达：无真实 URL 的文件卡片不报错、不把 path 拼成下载或预览", () => {
  const { context } = answerScreen([], { outcome: { status: "failed" } });
  for (const url of [undefined, null, "", "   ", 7]) {
    const record = freezeRecord({
      name: "recorded.html", path: "/home-outputs/__ut/recorded.html", url, bytes: 64,
    });
    let markup;
    assert.doesNotThrow(() => { markup = context.artifactHTML(record); });
    assert.match(markup, /recorded\.html/);
    assert.match(markup, /暂无可用入口/);
    assert.doesNotMatch(markup, /<a\b|<iframe\b|<img\b|<video\b|<audio\b|\bdata-render\b/);
  }
});

test("产物可达：失败轮混有 name/path 元数据时，有 URL 的真实文件仍可下载", () => {
  const results = [{
    key: "codex", harness: "codex", status: "error", error: "保存后执行失败",
    artifacts: [
      gameArtifact(),
      { name: "notes.txt", path: "/var/lib/meyo-legion/data/outputs/__ut/notes.txt" },
      { name: "image.png", path: "/home-outputs/__ut/image.png", url: null },
      { path: "/var/lib/meyo-legion/data/outputs/__ut/path-only.txt" },
    ],
  }];
  let ui;
  assert.doesNotThrow(() => {
    ui = answerScreen(results, { all_failed: true, outcome: { status: "failed" }, results });
  });
  assert.equal(ui.artifacts.length, 4);
  assert.equal(ui.downloads.length, 1);
  assert.equal(ui.downloads[0].getAttribute("href"), GAME_URL + "?download=1");
  const panes = ui.answers.querySelectorAll(".result-pane").map((node) => node.innerHTML).join("\n");
  assert.match(panes, /path-only\.txt/);
  assert.equal((panes.match(/暂无可用入口/g) || []).length, 3);
  assert.match(ui.summary.innerHTML, /4 份文件与预览/);
  assert.match(ui.status, /本轮未完成/);
});

test("产物可达：文件类型默认文案不宣称最终页面或完整成品", () => {
  const { context } = answerScreen([], { outcome: { status: "failed" } });
  for (const artifact of [gameArtifact(), { name: "project.zip", url: "/home-outputs/__ut/project.zip" }]) {
    const markup = context.artifactHTML(freezeRecord(artifact));
    assert.doesNotMatch(markup, /最终页面|完整项目包|最终成品/);
  }
});

test("历史预览：网页按需运行，图片延迟加载，保留原文件与安全沙箱", () => {
  const { context } = answerScreen([], { outcome: { status: "failed" } });
  const page = context.artifactHTML(gameArtifact());
  assert.doesNotMatch(page, /<iframe\b[^>]*\bsrc=/,
    "切换旧记录不能自动启动每份可运行网页");
  assert.match(page, /data-lb="html"/);
  assert.match(page, /<button\b[^>]*data-preview/);
  assert.match(page, /download="index.html"/);
  const picture = context.artifactHTML({ name: "old.png", url: "/home-outputs/__ut/old.png" });
  assert.match(picture, /<img\b[^>]*loading="lazy"/);
  assert.match(picture, /decoding="async"/);
  const lightbox = pageFunction("openLightbox");
  assert.match(lightbox, /sandbox="allow-scripts allow-forms allow-popups"/);
  assert.doesNotMatch(lightbox, /allow-same-origin/);
});

test("历史文档：离屏不请求转换，进入视口后仅转换一次", () => {
  const nodes = [new Element(), new Element()];
  const observerCallbacks = [], observed = new Set(), rendered = [];
  for (const node of nodes) node.dataset.render = "";
  const context = vm.createContext({
    document: { querySelectorAll: () => [] },
    IntersectionObserver: class {
      constructor(callback) { observerCallbacks.push(callback); }
      observe(node) { observed.add(node); }
      unobserve(node) { observed.delete(node); }
      disconnect() { observed.clear(); }
    },
    renderArtDoc: (node) => rendered.push(node),
    openLightbox() {},
  });
  const root = {
    querySelectorAll(selector) {
      return selector.includes("data-render") ? nodes : [];
    },
  };
  vm.runInContext(pageFunction("hydrateArtifacts"), context);
  context.hydrateArtifacts(root);
  assert.equal(rendered.length, 0);
  assert.equal(observed.size, 2);
  observerCallbacks[0]([{ target: nodes[0], isIntersecting: true }]);
  assert.deepEqual(rendered, [nodes[0]]);
  observerCallbacks[0]([{ target: nodes[0], isIntersecting: true }]);
  assert.equal(rendered.length, 1);
});

test("历史切换：切换已列出的会话不重新下载整个目录，过期响应不能回拨", () => {
  const code = pageFunction("openSession");
  assert.doesNotMatch(code, /await loadSessions\(\)/);
  assert.match(code, /view!==_sessionView/);
  assert.match(code, /aria-busy/);
  assert.match(code, /classList\.toggle\('active'/);
  const sidebar = html.match(/\.sb-item\{[^}]+\}/)?.[0] || "";
  assert.match(sidebar, /width:100%/);
  assert.match(sidebar, /text-align:left/);
});

test("后台打开：没有绘制帧时也恢复身份，绘制和兜底不会重复启动", () => {
  const frames = [], timers = new Map();
  const context = vm.createContext({
    requestAnimationFrame(fn) { frames.push(fn); },
    setTimeout(fn) { timers.set(1, fn); return 1; },
    clearTimeout(id) { timers.delete(id); },
  });
  const source = pageFunction("_afterFirstPaint");
  assert.ok(source, "启动回调需要有无绘制帧的有界兜底");
  vm.runInContext(source, context);
  let started = 0;
  context._afterFirstPaint(() => { started++; });
  assert.equal(started, 0);
  assert.equal(timers.size, 1);
  timers.get(1)();
  assert.equal(started, 1);
  frames.shift()();
  frames.shift()();
  assert.equal(started, 1);
});

test("未登录入口：发送按钮可打开授权而非灰死，历史明确提示登录", () => {
  const elements = new Map(["#whoAmI", "#send", "#sbList", "#historySignIn"]
    .map((key) => [key, new Element()]));
  let opened = 0;
  const context = vm.createContext({
    $: (selector) => elements.get(selector),
    _personalSubject: "", _personalSwitchPending: false, running: false,
    _openPersonalAuth() { opened++; },
  });
  vm.runInContext(pageFunction("_personalLabel"), context);
  context._personalLabel();
  assert.equal(elements.get("#send").disabled, false);
  assert.match(elements.get("#send").textContent, /登录/);
  assert.match(elements.get("#sbList").innerHTML, /登录.*历史|历史.*登录/);
  elements.get("#historySignIn").onclick();
  assert.equal(opened, 1);
  context._personalSubject = "__ut_owner";
  context._personalLabel();
  assert.equal(elements.get("#send").textContent, "派出军团");
  context.running = true;
  context._personalLabel();
  assert.equal(elements.get("#send").disabled, true);
});

test("身份恢复：初始焦点不误报切号，过期焦点检查不能清掉新身份", async () => {
  let checks = 0, lose = 0, reject;
  const context = vm.createContext({
    _personalSubject: "", _personalSwitchPending: false, _personalEpoch: 0,
    _readPersonalStatus() { checks++; return new Promise((resolve, fail) => { reject = fail; }); },
    _losePersonalIdentity() { lose++; },
  });
  const source = pageFunction("_checkFocusedIdentity");
  assert.ok(source);
  vm.runInContext(source, context);
  context._checkFocusedIdentity();
  assert.equal(checks, 0);
  context._personalSubject = "__ut_old";
  const pending = context._checkFocusedIdentity();
  assert.equal(checks, 1);
  context._personalEpoch++;
  context._personalSubject = "__ut_new";
  reject(new Error("old check failed"));
  await pending;
  assert.equal(lose, 0);
});

function historyScreen() {
  const attached = [];
  const create = (tag = "div") => {
    const element = new Element(tag);
    element.isConnected = false;
    element.remove = () => {};
    return element;
  };
  const context = vm.createContext({
    document: {
      createElement: create,
      querySelector(selector) {
        const index = selector.match(/data-turn-index="([^"]+)"/)?.[1];
        return attached.find((pair) => pair[1].dataset.turnIndex === index)?.[1] || null;
      },
    },
    _teamReplay: new Map(), curSession: "__ut_history", _resumeCtx: null,
    _renderModeFacts() {}, esc: (text) => String(text),
    newLegionMsg() {
      const message = create();
      message.innerHTML = '<div class="progress"></div><span class="st"></span>';
      return message;
    },
    renderLegionAnswer(results, data, message) {
      message.recordedFiles = results.flatMap((result) => result.artifacts || []);
      message.renderCount = (message.renderCount || 0) + 1;
      message.recordedDisplay = answerScreen(results, data).status;
    },
  });
  vm.runInContext(["_replayEntry", "_bindReplayCard", "_turnSignature", "_bindHistoryProcess", "renderSessionTurn"]
    .map(pageFunction).join("\n"), context);
  return {
    context,
    attach(pair) {
      pair[0].isConnected = pair[1].isConnected = true;
      pair[1].previousElementSibling = pair[0];
      attached.push(pair);
    },
  };
}

test("历史轮次：相同任务的八条原记录不折叠成两张卡，也不覆盖早期文件", () => {
  const { context } = historyScreen();
  const pairs = [];
  for (let index = 0; index < 8; index++) {
    pairs.push(context.renderSessionTurn({
      question: "saved turn " + index,
      result: {
        team_id: "__ut_team_" + (index % 2), status: "error",
        results: [{ status: "error", artifacts: index === 0 ? [gameArtifact()] : [] }],
      },
    }, index));
  }
  assert.equal(new Set(pairs.map((pair) => pair[1])).size, 8);
  assert.equal(new Set(pairs.map((pair) => pair[0])).size, 8);
  assert.equal(pairs[0][1].recordedFiles.length, 1);
  pairs.forEach((pair, index) => assert.equal(pair[1].dataset.turnIndex, String(index)));
  assert.equal(context._teamReplay.get("__ut_team_0").msgEl, pairs[6][1]);
  assert.equal(context._teamReplay.get("__ut_team_1").msgEl, pairs[7][1]);
});

test('运行中会话快照展示原记录模式；缺失时不猜实际模式',()=>{
  const {context}=historyScreen();context.setTimeout=()=>{};
  const seen=[];context._renderModeFacts=(card,data,final)=>seen.push({recorded:card._recordedMode,final});
  for(const mode of ['relay','']){
    const [,card]=context.renderSessionTurn({team_id:'__ut_live_'+mode,question:'feedback',status:'running',mode},-1);
    assert.equal(card._recordedMode,mode);
    assert.deepEqual(seen.at(-1),{recorded:mode,final:undefined});
  }
});

test("历史轮次：同一轮更新仍复用原卡和气泡，在线未编号卡可被快照接管", () => {
  const screen = historyScreen();
  const turn = { question: "same", result: {
    team_id: "__ut_live", status: "error", results: [{ status: "error", artifacts: [] }],
  } };
  const first = screen.context.renderSessionTurn(turn, 0);
  screen.attach(first);
  const same = screen.context.renderSessionTurn(turn, 0);
  assert.equal(same[0], first[0]);
  assert.equal(same[1], first[1]);
  delete first[1].dataset.turnIndex;
  const adopted = screen.context.renderSessionTurn(turn, 0);
  assert.equal(adopted[0], first[0]);
  assert.equal(adopted[1], first[1]);
  assert.equal(adopted[1].dataset.turnIndex, "0");
});

test("历史轮次：旧轮不能抢占未编号在线卡或回拨当前回放状态", () => {
  const screen = historyScreen(), { context } = screen;
  const live = context.newLegionMsg();
  const user = new Element(); user.className = "msg user";
  live.previousElementSibling = user;
  const entry = context._replayEntry("__ut_live");
  Object.assign(entry, { msgEl: live, userEl: user, cursor: 19, terminalId: 12,
                        executionId: "__ut_current", complete: false });
  const turn = { question: "saved", result: { team_id: "__ut_live", status: "error",
    results: [{ status: "error", artifacts: [gameArtifact()] }] } };
  const older = context.renderSessionTurn(turn, 0, false);
  assert.notEqual(older[1], live);
  assert.notEqual(older[0], user);
  assert.equal(entry.msgEl, live);
  assert.equal(entry.complete, false);
  assert.equal(entry.cursor, 19);
  assert.equal(entry.executionId, "__ut_current");
  screen.attach(older);
  const latest = context.renderSessionTurn(turn, 1, true);
  assert.equal(latest[1], live);
  context.renderSessionTurn(turn, 0, false);
  assert.equal(entry.msgEl, live);
  assert.equal(entry.msgEl.dataset.turnIndex, "1");
});

const returnPilot = () => ({
  round: 1, action: "stop", kind: "turn_returned", reason: "原记录：未收到继续请求",
});
const surfaceResult = () => ({
  key: "codex", harness: "codex", status: "done", final: "好", artifacts: [gameArtifact()],
});

test("停因：只有明确的新 stop/turn_returned 修正标签，结果和下载原样保留", () => {
  for (const pilot of [undefined, { ...returnPilot(), action: "continue" },
    { ...returnPilot(), kind: "done" }, returnPilot()]) {
    const results = [surfaceResult()];
    const data = { results, outcome: { status: "completed" }, ...(pilot ? { pilot } : {}) };
    const before = JSON.stringify(data);
    const ui = answerScreen(results, data);
    const returned = pilot?.action === "stop" && pilot.kind === "turn_returned";
    assert.match(ui.status, returned ? /✓ 本轮已返回/ : /✓ 本轮已完成/);
    if (returned) {
      assert.match(ui.summary.innerHTML, /目标.*尚未确认/);
      assert.match(ui.summary.innerHTML, /原记录：未收到继续请求/);
    }
    assert.equal(ui.downloads[0].getAttribute("href"), GAME_URL + "?download=1");
    assert.doesNotMatch(ui.summary.innerHTML, /未完成尝试/);
    assert.equal(JSON.stringify(data), before);
  }
});

test("停因：取消、契约、验证、partial 和失败事实不被返回标签覆盖", () => {
  const rows = [surfaceResult()];
  for (const [data, title] of [
    [{ cancelled: true, outcome: { status: "completed" } }, "本轮已停止"],
    [{ outcome: { status: "cancelled" } }, "本轮已停止"],
    [{ contract_incomplete: true, outcome: { status: "completed" } }, "要求的成品尚未齐全"],
    [{ outcome: { status: "completed", delivery_contract: { missing: [{ description: "真实缺项" }] } } }, "要求的成品尚未齐全"],
    [{ verification_incomplete: true, outcome: { status: "completed" } }, "成品仍待验证"],
    [{ outcome: { status: "partial" } }, "本轮仍有未完成事项"],
    [{ partial: true }, "本轮仍有未完成事项"],
    ...["failed", "incomplete", "blocked"].map((status) => [{ outcome: { status } }, "本轮未完成"]),
    [{ all_failed: true }, "本轮未完成"],
  ]) {
    const ui = answerScreen(rows, { ...data, pilot: returnPilot() });
    assert.ok(ui.status.includes(title), ui.status);
    assert.doesNotMatch(ui.status, /本轮已返回/);
  }
});

test("停因：公开 reason 有界且按文本转义，不作为 HTML，也不改原记录", () => {
  const data = { results: [surfaceResult()], outcome: { status: "completed" },
    pilot: { ...returnPilot(), reason: '<img src=x onerror="bad()"> & ' + "x".repeat(900) + "TAIL_NOT_VISIBLE" } };
  const before = JSON.stringify(data);
  const ui = answerScreen(data.results, data);
  assert.match(ui.summary.innerHTML, /&lt;img/);
  assert.match(ui.summary.innerHTML, /&amp;/);
  assert.doesNotMatch(ui.summary.innerHTML, /<img|TAIL_NOT_VISIBLE/);
  assert.equal(JSON.stringify(data), before);
});

for (const mode of ["live", "resume"]) {
  for (const shape of ["dispatch", "single"]) {
    for (const fresh of [true, false]) {
      test(`final包装：${mode}/${shape}/${fresh ? "新停因" : "历史无字段"} 终态渲染与通知一致`, async () => {
        const ui = await screen(mode);
        const rows = [surfaceResult()];
        const facts = { outcome: { status: "completed" }, session: { sid: "__ut_sid", turns: 1 },
          ...(fresh ? { pilot: returnPilot() } : {}) };
        const data = shape === "dispatch" ? { ...facts, results: rows }
          : { node_id: "codex", result: { ...rows[0], ...facts } };
        freezeRecord(data);
        const before = JSON.stringify(data);
        try {
          await ui.final(data, 17);
          assert.equal(ui.finals.length, 1);
          assert.equal(ui.finals[0].results.length, 1, "single final must not become an empty team");
          assert.equal(ui.finals[0].results[0].final, "好");
          assert.equal(ui.context.curSession, "__ut_sid");
          assert.equal(ui.message._replay.terminalId, 17);
          assert.equal(ui.message._replay.complete, true);
          assert.equal(ui.message._turnSignature, ui.context._turnSignature(ui.finals[0].data));
          const title = fresh ? "本轮已返回" : "本轮已完成";
          assert.ok(ui.message.querySelector(".st").textContent.includes(title));
          assert.ok(ui.notices[0].includes(title));
          assert.equal(ui.message.querySelector(".progress").style.display, "none");
          await ui.final(data, 17);
          assert.equal(ui.finals.length, 1, "duplicate event must not render or notify again");
          assert.equal(ui.notices.length, 1);
          assert.equal(JSON.stringify(data), before);
        } finally { await ui.close(); }
      });
    }
  }
}

test("停因：薄投影只认单家 API 包装，不从某个成员推导团队停因", () => {
  const context = vm.createContext({});
  vm.runInContext(pageFunction("_finalDeliveryData"), context);
  assert.equal(typeof context._finalDeliveryData, "function");
  const member = freezeRecord({ ...surfaceResult(), pilot: returnPilot() });
  const multi = freezeRecord({ results: [member, surfaceResult()], outcome: { status: "completed" } });
  const projected = context._finalDeliveryData(multi);
  assert.equal(projected, multi);
  assert.equal(projected.pilot, undefined);
  assert.match(answerScreen(projected.results, projected).status, /本轮已完成/);
  const wrapper = freezeRecord({ node_id: "codex", result: member });
  const single = context._finalDeliveryData(wrapper);
  assert.equal(single.results[0], member);
  assert.equal(single.pilot, member.pilot, "reuse facts, do not recursively copy/reclassify them");
  const unknown = freezeRecord({ result: member });
  assert.equal(context._finalDeliveryData(unknown), unknown);
  assert.equal(context._finalDeliveryData(unknown).pilot, undefined);
});

test("停因：同轮新增顶层事实触发原卡刷新，新旧历史包装一致且不增卡", () => {
  for (const nested of [false, true]) {
    const screen = historyScreen(), { context } = screen;
    const result = { team_id: "__ut_refresh", status: "done", ts: 123,
      results: [surfaceResult()], outcome: { status: "completed" } };
    const wrap = (r) => nested ? { question: "same", result: r } : { question: "same", ...r };
    const first = context.renderSessionTurn(freezeRecord(wrap(result)), 0);
    screen.attach(first);
    assert.match(first[1].recordedDisplay, /本轮已完成/);
    const next = freezeRecord(wrap({ ...result, pilot: returnPilot() }));
    const second = context.renderSessionTurn(next, 0);
    assert.equal(second[0], first[0]);
    assert.equal(second[1], first[1]);
    assert.equal(second[1].renderCount, 2, "top-level pilot-only change must refresh this turn");
    assert.match(second[1].recordedDisplay, /本轮已返回/);
    assert.equal(second[1].recordedFiles[0].url, GAME_URL);
    context.renderSessionTurn(next, 0);
    assert.equal(second[1].renderCount, 2);
  }
});

test("停因：signature 只纳入有限公开字段，不序列化无界诊断或私有子对象", () => {
  const { context } = historyScreen();
  const base = { ts: 1, status: "done", results: [], pilot: returnPilot() };
  const signature = context._turnSignature(base);
  for (const [field, value] of [["round", 2], ["action", "continue"], ["kind", "done"], ["reason", "new fact"]]) {
    assert.notEqual(context._turnSignature({ ...base, pilot: { ...base.pilot, [field]: value } }), signature);
  }
  const unbounded = { toJSON() { throw new Error("must not serialize unbounded report"); } };
  assert.equal(context._turnSignature({ ...base, persistent_diagnostics: unbounded,
    pilot: { ...base.pilot, shared_goals: unbounded, owner_proxy: unbounded } }), signature);
  const long = { ...base, pilot: { ...base.pilot, reason: "a".repeat(500) + "ONE" } };
  const other = { ...base, pilot: { ...base.pilot, reason: "a".repeat(500) + "TWO" } };
  assert.equal(context._turnSignature(long), context._turnSignature(other), "only rendered reason prefix affects the card");
  assert.ok(context._turnSignature(long).length < 800);
});
