import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const script = readFileSync(new URL('../web/game_workbench.js', import.meta.url), 'utf8');
const html = readFileSync(process.env.LEGION_UI_FILE || new URL('../web/index_v2.html', import.meta.url), 'utf8');
function nativeFunction(name) {
  const start = html.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
  assert.notEqual(start, -1, `Native function ${name} must exist in the selected UI`);
  for (let end = html.indexOf('}', start); end >= 0; end = html.indexOf('}', end + 1)) {
    const code = html.slice(start, end + 1);
    try { new vm.Script(`(${code})`); return code; } catch {}
  }
  throw Error(name);
}

// Small DOM for real handlers and async races. It does not claim layout validation.
class Element {
  constructor(tag, document) {
    Object.assign(this, {tagName: tag, ownerDocument: document, children: [], dataset: {}, attributes: {}, value: '', textContent: '', className: '', hidden: false, disabled: false});
    this.classList = {add: name => { this.className += ` ${name}`; }};
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  get childElementCount() { return this.children.length; }
  focus() { this.focused = true; }
  all() { return [this, ...this.children.flatMap(node => node.all())]; }
}
function environment(search = '') {
  const callbacks = [];
  const document = {readyState: 'loading', createElement(tag) { return new Element(tag, this); },
    addEventListener(type, fn) { if (type === 'DOMContentLoaded') callbacks.push(fn); }};
  document.body = document.createElement('body');
  const observers = [];
  class MutationObserver {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe() {}
    disconnect() { this.disconnected = true; }
  }
  const context = vm.createContext({document, URL, URLSearchParams, console, MutationObserver,
    location: {search, href: 'https://play.example.test/' + search, origin: 'https://play.example.test'}});
  context.window = context;
  vm.runInContext(script, context);
  return {context, document, observers, callbacks, api: context.MeyoGameWorkbench};
}
function deferred() { let resolve, reject; const promise = new Promise((a, b) => {resolve = a; reject = b;}); return {promise, resolve, reject}; }
const file = (name = '参考.mp4', size = 1234) => ({name, size, lastModified: 100, type: 'video/mp4'});
async function screen(overrides = {}, initial = {}) {
  const env = environment(), root = env.document.createElement('div'); env.document.body.append(root);
  const state = {subject: 'alice', epoch: 1, switching: false, canExecute: true, sid: '', running: false, draft: '', uploads: [], ...initial};
  const calls = [];
  const host = {
    state: () => state,
    login() { calls.push(['login']); },
    async list() { calls.push(['list']); return [{sid: 'old', title: '原来的游戏'}]; },
    async session(sid) { calls.push(['session', sid]); return {sid, user_id: 'alice', turns: []}; },
    describe() { return {title: '本轮已停止', detail: '保留工作', candidates: []}; },
    async upload(f) { calls.push(['upload', f.name]); state.uploads.push({path: '/owned/' + f.name}); },
    removeUpload(item) { calls.push(['remove']); state.uploads.splice(state.uploads.indexOf(item), 1); },
    async select(sid) { calls.push(['select', sid]); state.sid = sid; },
    writeDraft(value) { calls.push(['draft', value]); state.draft = value; },
    async submit() { calls.push(['send', state.sid, state.draft, state.uploads.map(x => x.path)]); state.draft = ''; },
    close() { calls.push(['close']); },
    preview(artifact, sid) { calls.push(['preview', artifact, sid]); },
    ...overrides,
  };
  const view = env.api.mount(root, {host}); await view.ready;
  const node = name => root.all().find(n => n.dataset.game === name);
  const choose = (...items) => { node('files').files = items; return node('files').onchange(); };
  const select = async sid => { node('target').value = sid; node('target').onchange(); await Promise.resolve(); await Promise.resolve(); };
  return {...env, root, state, calls, host, view, node, choose, select};
}

test('入口挂载只读索引；无选择素材、模型、上传或发送的副作用', async () => {
  const s = await screen();
  assert.deepEqual(s.calls, [['list']]);
  assert.equal(s.node('status').textContent, '尚未开始制作');
  assert.equal(s.node('files').multiple, true);
  assert.equal(s.node('note').value, '');
  assert.equal(s.node('submit').disabled, false);
});

test('未登录不读取别人的历史，按钮只打开已有登录', async () => {
  const s = await screen({}, {subject: ''});
  assert.deepEqual(s.calls, []);
  assert.equal(s.node('submit').disabled, true);
  s.node('login').onclick(); assert.deepEqual(s.calls, [['login']]);
});

test('仅设置账号不能发制作请求，也不借用站长身份', async () => {
  const s = await screen({}, {canExecute: false});
  s.node('note').value = '一只猫'; await s.node('submit').onclick();
  assert.ok(!s.calls.some(x => x[0] === 'send'));
  assert.match(s.node('message').textContent, /尚未接通制作/);
});

test('多视频可分次选择，选择本身不上传；全部上传后只发一个原生任务', async () => {
  const s = await screen(); s.choose(file('a.mp4')); s.choose(file('b.mov'));
  assert.equal(s.node('file-list').childElementCount, 2);
  assert.deepEqual(s.calls, [['list']]);
  s.node('note').value = '做一个有声音的猫咪小镇'; await s.node('submit').onclick();
  assert.deepEqual(s.calls.filter(x => x[0] === 'upload').map(x => x[1]), ['a.mp4', 'b.mov']);
  const sends = s.calls.filter(x => x[0] === 'send');
  assert.equal(sends.length, 1); assert.match(sends[0][2], /同一个游戏/); assert.equal(sends[0][3].length, 2);
  assert.ok(s.calls.findIndex(x => x[0] === 'send') > s.calls.findLastIndex(x => x[0] === 'upload'));
});

test('空想法、大文件和非媒体文件不进入上传链', async () => {
  const s = await screen();
  s.choose(file('ok.mp4'), file('too-big.mp4', 25 * 1048576));
  assert.equal(s.node('file-list').childElementCount, 0);
  s.choose(file('code.html')); assert.equal(s.node('file-list').childElementCount, 0);
  await s.node('submit').onclick();
  assert.ok(!s.calls.some(x => ['upload', 'send'].includes(x[0])));
  assert.match(s.node('message').textContent, /写下/);
});

test('上传失败不发送，重试只重传失败素材，保留成功附件', async () => {
  const s = await screen(); let bad = true;
  s.host.upload = async f => {
    s.calls.push(['upload', f.name]);
    s.state.uploads.push(f.name === 'b.mp4' && bad ? {error: 'offline'} : {path: '/owned/' + f.name});
  };
  s.choose(file('a.mp4'), file('b.mp4')); s.node('note').value = '猫咪游戏';
  await s.node('submit').onclick();
  assert.equal(s.calls.filter(x => x[0] === 'send').length, 0);
  assert.match(s.node('message').textContent, /上传未成功/);
  bad = false; await s.node('submit').onclick();
  assert.deepEqual(s.calls.filter(x => x[0] === 'upload').map(x => x[1]), ['a.mp4', 'b.mp4', 'b.mp4']);
  assert.equal(s.calls.filter(x => x[0] === 'send').length, 1);
});

test('重复点击制作不能在上传未完成时创建第二个任务', async () => {
  const wait = deferred(), s = await screen();
  s.host.upload = async f => { s.calls.push(['upload', f.name]); const a = {uploading: true}; s.state.uploads.push(a); await wait.promise; Object.assign(a, {path: '/owned/file', uploading: false}); };
  s.choose(file()); s.node('note').value = '一只猫';
  const first = s.node('submit').onclick(); await s.node('submit').onclick();
  assert.equal(s.calls.filter(x => x[0] === 'upload').length, 1);
  assert.equal(s.calls.filter(x => x[0] === 'send').length, 0);
  wait.resolve(); await first; assert.equal(s.calls.filter(x => x[0] === 'send').length, 1);
});

test('上传中换账号，丢弃旧视图且不上传剩余文件、不发送', async () => {
  const wait = deferred(), s = await screen();
  s.host.upload = async f => { s.calls.push(['upload', f.name]); const a = {uploading: true}; s.state.uploads.push(a); await wait.promise; a.path = '/owned/a'; a.uploading = false; };
  s.choose(file('a.mp4'), file('b.mp4')); s.node('note').value = '两只猫';
  const sending = s.node('submit').onclick(); s.state.subject = 'bob'; s.state.epoch++;
  s.observers[0].callback(); wait.resolve(); await sending;
  assert.match(s.root.children[0].textContent, /账号状态已变化/);
  assert.equal(s.calls.filter(x => x[0] === 'upload').length, 1);
  assert.equal(s.calls.filter(x => x[0] === 'send').length, 0);
});

test('上传中切换原生会话不发送到另一作品', async () => {
  const wait = deferred(), s = await screen();
  s.host.upload = async () => { const a = {uploading: true}; s.state.uploads.push(a); await wait.promise; a.path = '/owned/a'; a.uploading = false; };
  s.choose(file()); s.node('note').value = '猫'; const pending = s.node('submit').onclick();
  s.state.sid = 'elsewhere'; wait.resolve(); await pending;
  assert.ok(!s.calls.some(x => x[0] === 'send'));
  assert.match(s.node('message').textContent, /状态已变化/);
});

test('继续当前作品保留会话与原有附件，无新建或重新打开动作', async () => {
  const existing = {path: '/owned/previous.mp4'};
  const s = await screen({}, {sid: 'old', uploads: [existing]});
  s.node('note').value = '改进猫的动作'; await s.node('submit').onclick();
  assert.equal(s.calls.filter(x => x[0] === 'select').length, 0);
  const send = s.calls.find(x => x[0] === 'send');
  assert.equal(send[1], 'old'); assert.match(send[2], /继续原作品/); assert.ok(s.state.uploads.includes(existing));
});

test('选择另一作品只读，明确制作才打开它，未自动续跑', async () => {
  const s = await screen(); await s.select('old');
  assert.equal(s.calls.filter(x => x[0] === 'select').length, 0);
  assert.equal(s.calls.filter(x => x[0] === 'send').length, 0);
  s.node('note').value = '把原来的猫做得更灵活'; await s.node('submit').onclick();
  assert.deepEqual(s.calls.filter(x => x[0] === 'select'), [['select', 'old']]);
  assert.equal(s.calls.find(x => x[0] === 'send')[1], 'old');
});

test('跨会话有原草稿或附件时禁止静默清空；返回执行详情同样保护', async () => {
  for (const initial of [{draft: '未发送的原文'}, {uploads: [{path: '/owned/input'}]}]) {
    const s = await screen({}, initial); await s.select('old'); s.node('note').value = '修改';
    await s.node('submit').onclick(); await s.node('conversation').onclick();
    assert.ok(!s.calls.some(x => ['select', 'send', 'draft'].includes(x[0])));
    assert.match(s.node('message').textContent, /原对话有未发送/);
  }
});

test('上传等待期间新写的原草稿不会被覆盖，已有失败附件也阻止发送', async () => {
  const wait = deferred(), s = await screen();
  s.host.upload = async () => { const a = {}; s.state.uploads.push(a); await wait.promise; a.path = '/owned/a'; };
  s.choose(file()); s.node('note').value = '猫'; const pending = s.node('submit').onclick();
  s.state.draft = '用户刚写的新文字'; wait.resolve(); await pending;
  assert.equal(s.state.draft, '用户刚写的新文字'); assert.ok(!s.calls.some(x => x[0] === 'send'));
  const failed = await screen({}, {uploads: [{error: 'old failure'}]});
  failed.node('note').value = '猫'; await failed.node('submit').onclick();
  assert.ok(!failed.calls.some(x => x[0] === 'send'));
});

test('原生任务运行中不能发第二单或跳往其他会话，查看当前执行不会重启恢复', async () => {
  const s = await screen({}, {sid: 'old', running: true});
  s.node('note').value = '修改'; await s.node('submit').onclick(); await s.node('conversation').onclick();
  assert.ok(!s.calls.some(x => ['select', 'send'].includes(x[0])));
  assert.ok(s.calls.some(x => x[0] === 'close'));
  assert.equal(s.node('status').textContent, '当前作品正在执行');
});

test('读取旧作品迟到的响应不能覆盖新选择，也不能显示另一个owner的数据', async () => {
  const wait = deferred(), s = await screen();
  s.host.session = () => wait.promise; await s.select('old'); await s.select('');
  wait.resolve({sid: 'old', user_id: 'alice'}); await Promise.resolve(); await Promise.resolve();
  assert.equal(s.node('status').textContent, '尚未开始制作');
  s.host.session = async sid => ({sid, user_id: 'bob'}); await s.select('old');
  assert.match(s.node('detail').textContent, /身份.*不一致/);
});

test('候选名称按文本展示；预览委托原画布，携带确切原会话，不自动制作', async () => {
  const artifact = {name: '<img src=x onerror=alert(1)>', url: '/home-outputs/exports/a/index.html', kind: 'html', earlier: true};
  const s = await screen({describe: () => ({title: '本轮已停止', detail: '停止不代表完成', recovery: true, candidates: [artifact]})}, {sid: 'old'});
  assert.equal(s.node('candidates').children[0].children[0].textContent, artifact.name);
  assert.match(s.node('detail').textContent, /是否能继续以实际执行结果为准/);
  await s.node('preview').onclick();
  assert.deepEqual(s.calls.find(x => x[0] === 'preview'), ['preview', artifact, 'old']);
  assert.ok(!s.calls.some(x => x[0] === 'send'));
});

function nativeEnvironment() {
  const e = environment(); const calls = [];
  Object.assign(e.context, {
    _personalSubject: 'alice', _personalEpoch: 1, _personalSwitchPending: false,
    _personalCanExecute: () => true, _openPersonalAuth: () => calls.push(['login']),
    curSession: 'old', running: false, qEl: {value: ''}, uploads: [],
    uploadFile: async f => calls.push(['uploadFile', f.name]), send: async () => calls.push(['send']),
    openSession: async sid => { calls.push(['openSession', sid]); e.context.curSession = sid; },
    newChat: () => { calls.push(['newChat']); e.context.curSession = ''; },
    openLightbox: (...args) => calls.push(['openLightbox', ...args]),
    renderUploads() {}, autosize() {}, _savePersonalDraft() {}, _closeAdvPanel() {},
    fetch: async path => { calls.push(['fetch', path]); return {ok: true, json: async () => ({sessions: []})}; },
  });
  for (const name of ['_finalDeliveryData', '_deliveryDisplay', '_artifactPresentationRole', '_chooseVisibleDeliveries', '_sessionDeliveryOverview', '_canvasArtifactUrl', '_artifactCanvasKind']) {
    vm.runInContext(nativeFunction(name), e.context);
  }
  return {...e, calls, host: e.api.nativeHost()};
}

test('真实两UI归一化：单家final不会变成空候选，turn_returned不会显示已完成', () => {
  const s = nativeEnvironment();
  const view = s.host.describe({turns: [{result: {node_id: 'codex', result: {
    status: 'done', outcome: {status: 'completed'}, pilot: {action: 'stop', kind: 'turn_returned'},
    artifacts: [{name: 'index.html', url: '/home-outputs/exports/snap/index.html'}],
  }}}]});
  assert.equal(view.title, '本轮已返回'); assert.equal(view.candidates.length, 1);
  assert.doesNotMatch(view.title, /已完成/);
});

test('真实停因与成果排序：规划失败仍保留之前候选，仅合并有来源身份的正式版本', () => {
  const s = nativeEnvironment();
  const artifact = n => ({name: 'index.html', url: `/home-outputs/exports/${n}/index.html`, provenance: {source: 'authorized-project-export', source_path: '/project/index.html'}});
  const view = s.host.describe({turns: [
    {result: {artifacts: [artifact('old')]}},
    {result: {artifacts: [artifact('new')]}},
    {result: {outcome: {failure_stage: 'planning', planner_error: 'planner_tool_unavailable'}, recovery: {endpoint: '/api/legion/dispatch'}}},
  ]});
  assert.equal(view.title, '本轮规划未完成'); assert.match(view.detail, /工具不可用/);
  assert.doesNotMatch(view.detail, /planner_tool_unavailable/);
  assert.equal(view.candidates.length, 1); assert.equal(view.candidates[0].url, artifact('new').url);
  assert.equal(view.candidates[0].earlier, true); assert.equal(view.recovery, true);
});

test('真实画布校验阻止外站/凭据URL；安全预览只调原函数，没有iframe或新导出器', async () => {
  const s = nativeEnvironment();
  for (const url of ['https://evil.test/a.html', '/home-outputs/a?token=secret', 'javascript:alert(1)']) {
    assert.throws(() => s.host.preview({url, name: 'a.html'}, 'old'));
  }
  await s.host.preview({url: '/home-outputs/exports/a/index.html', name: 'index.html'}, 'old');
  assert.equal(s.calls.length, 1); assert.equal(s.calls[0][0], 'openLightbox');
  assert.equal(s.calls[0][1], 'html'); assert.equal(s.calls[0][4].sid, 'old');
});

test('原生适配只调用已有上传/发送/会话，保留既有模型与成员选择', async () => {
  const s = nativeEnvironment(); s.context._squad = ['codex']; s.context.modeSelect = {value: 'orchestrate'};
  await s.host.upload(file()); await s.host.select('next'); s.host.writeDraft('继续'); await s.host.submit();
  assert.deepEqual(s.calls.map(x => x[0]), ['uploadFile', 'openSession', 'send']);
  assert.deepEqual(s.context._squad, ['codex']); assert.equal(s.context.modeSelect.value, 'orchestrate');
});

test('使用真实uploadFile的multipart响应后再交原send；错误响应绝不发单', async () => {
  for (const success of [true, false]) {
    const n = nativeEnvironment(), root = n.document.createElement('div'); n.document.body.append(root);
    n.context.curSession = ''; n.context.FormData = class {append(...value) {this.value = value;}};
    n.context.toast = () => {}; n.context.renderUploads = () => {};
    n.context.fetch = async (path, options) => {
      n.calls.push(['fetch', path, options?.method || 'GET']);
      if (path.includes('session-index')) return {ok: true, json: async () => ({sessions: []})};
      assert.equal(path, '/api/legion/upload'); assert.equal(options.method, 'POST');
      return {ok: success, status: success ? 200 : 413, json: async () => success ? {ok: true, files: [{path: '/owned/movie.mp4', name: 'movie.mp4'}]} : {ok: false, error: '过大'}};
    };
    vm.runInContext(nativeFunction('uploadFile'), n.context);
    const view = n.api.mount(root, {host: n.api.nativeHost()}); await view.ready;
    const node = name => root.all().find(x => x.dataset.game === name);
    node('files').files = [file('movie.mp4')]; node('files').onchange(); node('note').value = '做一只猫';
    await node('submit').onclick();
    assert.equal(n.calls.filter(x => x[0] === 'send').length, success ? 1 : 0);
    assert.equal(n.calls.filter(x => x[0] === 'fetch' && x[1] === '/api/legion/upload').length, 1);
  }
});

test('脚本加载无副作用；主入口身份恢复后才调用boot，studio=game只开视图不发单', async () => {
  const e = environment(); let opens = 0;
  e.context.showAdvDetail = () => { opens++; };
  assert.equal(e.callbacks.length, 0);
  e.api.boot(); assert.equal(opens, 0);
  // Real native dependencies are required before the explicit studio entry opens.
  const n = nativeEnvironment(); n.context.location.search = '?studio=game';
  n.context.showAdvDetail = () => { opens++; };
  const root = n.document.createElement('div'); n.document.getElementById = () => root;
  assert.equal(n.callbacks.length, 0);
  n.api.boot(); await Promise.resolve(); await Promise.resolve();
  assert.equal(opens, 1);
  assert.ok(!n.calls.some(x => ['send', 'uploadFile', 'newChat'].includes(x[0])));
});
