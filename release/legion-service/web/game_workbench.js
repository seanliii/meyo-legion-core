/* Optional view for index_v2 / the private split overlay. Load after the native UI.
 * Call MeyoGameWorkbench.boot() AFTER native identity/draft restoration; it opens
 * ?studio=game. A normal entry button calls open(). Loading this file is inert.
 * Serving this JS/CSS and adding the script/link are the entry owner's responsibility.
 */
(function (global) {
  'use strict';
  const MAX_FILE = 24 * 1024 * 1024; // Leave multipart overhead below the native 25 MiB request cap.
  const ACCEPT = 'video/mp4,video/quicktime,video/webm,image/png,image/jpeg,image/webp,.mp4,.mov,.webm';
  const text = value => typeof value === 'string' ? value : '';

  function nativeHost() {
    const required = [typeof uploadFile, typeof send, typeof openSession, typeof newChat,
      typeof _sessionDeliveryOverview, typeof _deliveryDisplay, typeof _finalDeliveryData,
      typeof openLightbox, typeof _canvasArtifactUrl, typeof _artifactCanvasKind];
    if (required.some(kind => kind !== 'function')) throw new Error('游戏制作入口尚未接好，请返回原对话。');
    async function read(path) {
      const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? '请先登录后再查看作品。' : '作品记录暂时无法读取，请重试。');
      const data = await response.json();
      if (!data || data.error || data.ok === false) throw new Error('作品记录暂时无法读取，请重试。');
      return data;
    }
    return {
      state: () => ({subject: _personalSubject, epoch: _personalEpoch,
        switching: _personalSwitchPending, canExecute: _personalCanExecute(),
        sid: curSession || '', running, draft: qEl.value, uploads}),
      login: () => _openPersonalAuth(),
      list: async () => (await read('/api/legion/session-index?limit=50')).sessions || [],
      session: sid => read('/api/legion/session?sid=' + encodeURIComponent(sid)),
      describe(session) {
        const turns = (session.turns || []).map(turn => ({...turn,
          result: _finalDeliveryData(turn.result && typeof turn.result === 'object' ? turn.result : turn)}));
        const overview = _sessionDeliveryOverview(turns);
        const latest = overview.latest, outcome = latest.outcome || {};
        const status = _deliveryDisplay(latest, latest.results || []);
        const planning = outcome.failure_stage === 'planning' || latest.planner_error || outcome.planner_error;
        return {
          title: turns.length ? status.title : '尚无制作记录',
          detail: planning ? ((latest.planner_error || outcome.planner_error) === 'planner_tool_unavailable'
            ? '规划所需工具不可用，本轮未能开始制作。' : '本轮规划遇到问题，具体原因已保留在原对话。') : (turns.length ? status.detail : '点击制作后，实际进展会出现在原对话。'),
          recovery: !!(latest.recovery || outcome.recovery)?.endpoint,
          candidates: overview.primary.filter(a => _canvasArtifactUrl(a.url)).map(a => ({
            name: a.name || a.title || '已记录作品', url: a.url, kind: a.kind || '',
            earlier: a._sourceTurn < overview.turnCount,
          })),
        };
      },
      upload: file => uploadFile(file),
      removeUpload(item) { const index = uploads.indexOf(item); if (index >= 0) { uploads.splice(index, 1); renderUploads(); } },
      async select(sid) {
        if (sid) { await openSession(sid); if (curSession !== sid) throw new Error('原作品未能打开，未发送制作请求。'); }
        else newChat();
      },
      writeDraft(value) { qEl.value = value; autosize(); _savePersonalDraft(); },
      submit: () => send(),
      close: () => _closeAdvPanel(),
      preview(artifact, sid) {
        const url = _canvasArtifactUrl(artifact.url);
        if (!url) throw new Error('这份作品暂时不能安全预览。');
        return openLightbox(_artifactCanvasKind(artifact.name, artifact.kind), url, artifact.name, {sid});
      },
    };
  }

  function mount(root, options = {}) {
    const host = options.host || nativeHost(), doc = root.ownerDocument || document;
    const owner = {...host.state()}, files = [];
    let disposed = false, busy = false, readVersion = 0, selected = owner.sid || '';
    let boundSid = null, observer, reading = false;
    let expectedDraft = owner.draft || '', preparedDraft = '';
    const el = (tag, content, cls) => { const node = doc.createElement(tag); if (content) node.textContent = content; if (cls) node.className = cls; return node; };
    const button = (label, action, name) => {
      const node = el('button', label); node.type = 'button'; node.dataset.game = name;
      node.onclick = action; return node;
    };
    root.replaceChildren(); root.classList.add('game-workbench');
    const heading = el('h2', '把参考画面，做成可玩的游戏');
    const intro = el('p', '放入几段视频或图片，说说你想怎么玩。制作过程与作品都留在原对话。', 'gw-muted');
    const targetLabel = el('label', '制作哪份作品', 'gw-field');
    const target = el('select'); target.dataset.game = 'target'; targetLabel.append(target);
    function option(value, title) { const item = el('option', title); item.value = value; target.append(item); }
    option('', '制作新作品'); if (selected) option(selected, '继续当前对话的作品'); target.value = selected;
    const noteLabel = el('label', '想做什么', 'gw-field');
    const note = el('textarea'); note.rows = 4; note.maxLength = 6000; note.dataset.game = 'note';
    note.placeholder = '例如：把这几段视频里的猫做成可以探索小镇的游戏，保留它的动作和声音。';
    note.value = expectedDraft; noteLabel.append(note);
    const fileLabel = el('label', '参考视频或图片（可选多份）', 'gw-field');
    const input = el('input'); input.type = 'file'; input.multiple = true; input.accept = ACCEPT; input.dataset.game = 'files';
    fileLabel.append(input);
    const fileHint = el('p', '每份最多 24 MiB，可分次选择。点击制作才上传；只选素材不会开始制作。', 'gw-muted');
    const fileList = el('ul', '', 'gw-files'); fileList.dataset.game = 'file-list';
    const message = el('p', '', 'gw-message'); message.setAttribute('role', 'status'); message.dataset.game = 'message';
    const submit = button('开始制作', start, 'submit'); submit.className = 'gw-primary';
    const conversation = button('查看原对话与执行详情', showConversation, 'conversation');
    const login = button('使用军团账号登录', () => host.login(), 'login');
    const actions = el('div', '', 'gw-actions'); actions.append(submit, conversation, login);
    const records = el('section', '', 'gw-records');
    const recordsHead = el('div', '', 'gw-records-head');
    const refreshButton = button('刷新记录', refresh, 'refresh');
    recordsHead.append(el('h3', '实际进展与作品'), refreshButton);
    const status = el('p', '尚未读取记录'); status.dataset.game = 'status'; status.setAttribute('role', 'status');
    const detail = el('p', '', 'gw-muted'); detail.dataset.game = 'detail';
    const candidates = el('ul', '', 'gw-candidates'); candidates.dataset.game = 'candidates';
    records.append(recordsHead, status, detail, candidates);
    root.append(heading, intro, targetLabel, noteLabel, fileLabel, fileHint, fileList, message, actions, records);

    function current() {
      const state = host.state();
      if (disposed) return false;
      if (state.subject !== owner.subject || state.epoch !== owner.epoch || state.switching) {
        dispose(); files.length = 0;
        root.replaceChildren(el('p', '账号状态已变化，请重新打开游戏制作。'),
          button('重新打开游戏制作', () => global.MeyoGameWorkbench.open(), 'reopen'));
        return false;
      }
      return true;
    }
    function available() {
      if (!current()) return false;
      const state = host.state();
      if (!state.subject) { message.textContent = '请先使用军团账号登录。'; host.login(); return false; }
      if (!state.canExecute) { message.textContent = '此账号尚未接通制作能力，请在原账号页面查看。'; return false; }
      if (state.running) { message.textContent = '当前作品仍在执行，请先到原对话查看进展。'; return false; }
      return true;
    }
    function controls() {
      if (!current()) return;
      const state = host.state();
      submit.textContent = busy ? '正在交给军团…' : selected ? '继续制作这份作品' : '开始制作';
      submit.disabled = busy || !state.subject || !state.canExecute;
      target.disabled = input.disabled = note.disabled = busy;
      conversation.disabled = busy || !selected;
      refreshButton.disabled = busy || reading || !state.subject;
      login.hidden = !!state.subject;
    }
    function renderFiles() {
      fileList.replaceChildren();
      files.forEach(entry => {
        const row = el('li');
        const label = entry.attachment?.error ? '上传失败，可点击制作重试' : entry.attachment?.uploading ? '正在上传' : entry.attachment?.path ? '已上传' : '待上传';
        row.append(el('span', `${entry.file.name} · ${(entry.file.size / 1048576).toFixed(1)} MiB · ${label}`));
        const remove = button('移除', () => {
          if (busy || !current()) return;
          if (entry.attachment && host.state().sid !== boundSid) { message.textContent = '原对话已切换，请回到上传时的对话处理附件。'; return; }
          if (entry.attachment) host.removeUpload(entry.attachment);
          files.splice(files.indexOf(entry), 1); renderFiles();
        }, 'remove');
        remove.disabled = busy; row.append(remove); fileList.append(row);
      });
    }
    input.onchange = () => {
      if (busy || !current()) return;
      const batch = Array.from(input.files || []); input.value = '';
      if (batch.some(file => !file.size || file.size > MAX_FILE || !/\.(mp4|mov|webm|png|jpe?g|webp)$/i.test(file.name))) {
        message.textContent = '请使用每份不超过 24 MiB 的视频或图片；这次选择尚未加入。'; return;
      }
      for (const file of batch) if (!files.some(e => e.file.name === file.name && e.file.size === file.size && e.file.lastModified === file.lastModified)) files.push({file});
      message.textContent = '参考素材已选好，点击制作后才会上传和发起任务。'; renderFiles();
    };
    target.onchange = () => {
      if (busy || !current()) return;
      if (files.some(entry => entry.attachment)) {
        target.value = selected; message.textContent = '已有素材上传到原对话，请先移除这些附件再切换作品。'; return;
      }
      selected = target.value; controls(); refresh(false);
    };
    async function refresh(withList = true) {
      if (!current()) return;
      if (!owner.subject) { status.textContent = '登录后查看自己的作品'; controls(); return; }
      const version = ++readVersion, sid = selected;
      const valid = () => current() && version === readVersion && sid === selected;
      reading = true; controls(); status.textContent = '正在读取已保存记录…'; detail.textContent = ''; candidates.replaceChildren();
      try {
        if (withList !== false) {
          const sessions = await host.list(); if (!valid()) return;
          target.replaceChildren(); option('', '制作新作品');
          const seen = new Set();
          for (const item of sessions) if (typeof item.sid === 'string' && item.sid && !seen.has(item.sid)) {
            seen.add(item.sid); option(item.sid, item.title || '已有对话中的作品');
          }
          if (sid && !seen.has(sid)) option(sid, '当前对话的作品');
          target.value = sid;
        }
        if (!sid) { status.textContent = '尚未开始制作'; detail.textContent = '选择参考、写下想法，再点击开始制作。'; return; }
        const session = await host.session(sid); if (!valid()) return;
        if ((session.sid && session.sid !== sid) || (session.user_id && session.user_id !== owner.subject)) throw new Error('作品身份与当前选择不一致，未显示记录。');
        const view = host.describe(session);
        const live = host.state().running && host.state().sid === sid;
        status.textContent = live ? '当前作品正在执行' : view.title;
        detail.textContent = (live ? '最新步骤请查看原对话；下方是已保存的作品。' : view.detail) +
          (view.recovery ? ' 已保存继续所需记录；是否能继续以实际执行结果为准。' : '');
        for (const artifact of view.candidates || []) {
          const row = el('li');
          row.append(el('span', artifact.name), el('small', artifact.earlier ? '此前已记录的候选' : '已记录候选 · 请预览检查'));
          row.append(button('预览', async () => {
            if (!valid()) return;
            try { await host.preview(artifact, sid); } catch (error) { if (valid()) message.textContent = error.message; }
          }, 'preview'));
          candidates.append(row);
        }
        if (!candidates.childElementCount) candidates.append(el('li', '这份作品尚无可预览的候选。'));
      } catch (error) { if (valid()) { status.textContent = '记录暂时无法读取'; detail.textContent = error.message; } }
      finally { if (valid()) { reading = false; controls(); } }
    }
    function canSwitch(sid) {
      const state = host.state();
      if (state.sid !== sid && (state.draft.trim() || state.uploads.length)) {
        message.textContent = '原对话有未发送的文字或附件，请先处理后再切换作品。'; return false;
      }
      return true;
    }
    async function showConversation() {
      if (busy || !current() || !selected) return;
      if (host.state().running && host.state().sid !== selected) { message.textContent = '当前仍在执行，请先留在原对话查看。'; return; }
      if (!canSwitch(selected)) return;
      const originalSid = host.state().sid;
      busy = true; controls();
      try {
        if (originalSid !== selected) await host.select(selected);
        if (current() && host.state().sid === selected) host.close();
      } catch (error) { if (current()) message.textContent = error.message; }
      finally { busy = false; controls(); }
    }
    async function start() {
      if (busy || !available()) return;
      const goal = note.value.trim();
      if (!goal) { message.textContent = '请先写下你想做什么，或希望原作品改哪里。'; note.focus(); return; }
      if (!canSwitch(selected)) return;
      const before = host.state();
      if (before.draft && before.draft !== expectedDraft && before.draft !== preparedDraft) {
        message.textContent = '原对话的草稿已变化，请先回去确认，避免覆盖。'; return;
      }
      if (files.some(entry => entry.attachment) && boundSid !== before.sid) { message.textContent = '对话已切换，请回到原作品后再制作。'; return; }
      busy = true; controls(); renderFiles(); message.textContent = '正在准备参考素材…';
      try {
        if (before.sid !== selected) await host.select(selected);
        if (!current() || host.state().sid !== selected) throw new Error('原作品未能打开，未发送制作请求。');
        boundSid = selected;
        const stillBound = () => current() && host.state().sid === boundSid && !host.state().running;
        for (const entry of files) {
          if (!stillBound()) throw new Error('对话状态已变化，未继续上传或发送。');
          if (entry.attachment && !host.state().uploads.includes(entry.attachment)) entry.attachment = null;
          if (entry.attachment?.path && !entry.attachment.error && !entry.attachment.uploading) continue;
          if (entry.attachment) host.removeUpload(entry.attachment);
          const previous = new Set(host.state().uploads);
          const promise = host.upload(entry.file);
          entry.attachment = host.state().uploads.find(item => !previous.has(item)); renderFiles();
          await promise;
          if (!stillBound()) throw new Error('对话状态已变化，未发送制作请求。');
          entry.attachment ||= host.state().uploads.find(item => !previous.has(item)); renderFiles();
          if (!entry.attachment?.path || entry.attachment.error || entry.attachment.uploading) throw new Error('参考素材上传未成功，未发送任务；可点击制作重试。');
        }
        if (!stillBound()) throw new Error('对话状态已变化，未发送制作请求。');
        const state = host.state();
        if (state.uploads.some(item => item.error || item.uploading || !item.path)) throw new Error('原对话还有未就绪的附件，请先处理；未发送任务。');
        if (state.draft && state.draft !== expectedDraft && state.draft !== preparedDraft) throw new Error('原对话的草稿已变化，未覆盖或发送。');
        preparedDraft = `${selected ? '继续原作品' : '制作一个可玩的3D游戏'}：${goal}\n\n${files.length ? '结合本次所附的全部参考素材，制作同一个游戏。' : ''}请在原项目中工作，保留有效成果，实际检查画面、操作和声音；如遇阻请保留具体原因与候选，不把尚未验证的结果当作完成。`;
        host.writeDraft(preparedDraft); expectedDraft = preparedDraft;
        // The existing send() owns identity revalidation, choices, dispatch, SSE and recovery.
        host.close(); await host.submit();
      } catch (error) { if (current()) message.textContent = error.message; }
      finally { busy = false; if (current()) { renderFiles(); controls(); } }
    }
    function dispose() { disposed = true; ++readVersion; observer?.disconnect(); }
    // Native account changes update its DOM. Drop this view's old account data too,
    // without wrapping native functions or introducing a second polling/dispatch loop.
    if (typeof MutationObserver === 'function' && doc.body) {
      observer = new MutationObserver(() => { current(); });
      observer.observe(doc.body, {subtree: true, childList: true, characterData: true});
    }
    controls();
    const ready = refresh();
    return {ready, refresh, dispose};
  }

  let active;
  function open() {
    if (typeof showAdvDetail !== 'function') return false;
    const host = nativeHost();
    active?.dispose();
    showAdvDetail('游戏制作', '<div id="gameWorkbench"></div>', '');
    const root = document.getElementById('gameWorkbench');
    if (!root) return false;
    active = mount(root, {host}); return active;
  }
  function boot() { if (new URLSearchParams(location.search).get('studio') === 'game') return open(); return false; }
  global.MeyoGameWorkbench = Object.freeze({open, boot, mount, nativeHost});
})(window);
