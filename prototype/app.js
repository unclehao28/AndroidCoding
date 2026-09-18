/* 安卓源码工作台前端（P1）。
   两种数据来源严格分开：
   - 示例数据（demo）：使用 demo-data.js 中的内置文件，全部交互在浏览器内完成；
   - 真实服务器（real）：只使用后端 /api 接口读取真实文件与检索，连不上就显示错误，
     绝不回退到内置示例数据。
   未接入的能力（语义跳转、真实保存、全库索引）在界面上明确标注未就绪。 */
(() => {
  const root = document.getElementById('android-workbench');
  const $ = id => root.querySelector('#' + id);
  const DEMO = window.ASW_DEMO || {files: [], definitions: {}, note: ''};
  const api = window.ASWApi;
  const demoFiles = DEMO.files;
  const byId = Object.fromEntries(demoFiles.map(f => [f.id, f]));
  const definitions = DEMO.definitions;

  const WINDOW_LINES = 800;
  const NAV_NOT_READY = '语义跳转需要语言服务（clangd / JDT LS），属于 P2，本轮未接入。';

  const state = {
    dataMode: 'demo',
    apiBase: (api.sameOriginBase() || api.DEFAULT_BASE),
    connection: 'idle',
    connectionError: '',
    serverInfo: null,
    workspaces: [],
    workspace: '',
    scope: '',
    tab: 'search',
    q: 'setBrightness',
    options: {regex: false, caseSensitive: false, wholeWord: false},
    search: null,
    searching: false,
    searchError: '',
    searchRequestId: '',
    abort: null,
    file: null,
    fileError: '',
    fileLine: 1,
    tree: {byPath: {}, expanded: {}},
    demoSymbol: 'setBrightness',
    demoFile: 'controller',
    demoLine: 8,
    edits: {},
    editing: false,
    diff: false,
    history: [],
    trail: [],
    nav: null,
    navWord: '',
    sync: null,
    syncTimer: null,
    notice: '这是内置示例数据，不是真实源码。切到「真实服务器」才会读取服务器上的文件。',
    status: '示例数据已加载'
  };

  const esc = value => String(value === null || value === undefined ? '' : value)
    .replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const baseName = p => String(p || '').split('/').pop();
  const dirName = p => String(p || '').split('/').slice(0, -1).join('/');

  function highlight(text, query) {
    if (!query) return esc(text);
    const at = String(text).toLowerCase().indexOf(String(query).toLowerCase());
    if (at < 0) return esc(text);
    return esc(text.slice(0, at)) + '<mark>' + esc(text.slice(at, at + query.length)) + '</mark>' + esc(text.slice(at + query.length));
  }

  function setStatus(text, kind) {
    state.status = text;
    state.statusKind = kind || 'ok';
    renderFoot();
  }

  function setNotice(text) {
    state.notice = text || '';
    renderBanner();
  }

  // ---------------------------------------------------------------- 示例数据模式
  const demoLines = f => (Object.hasOwn(state.edits, f.id) ? state.edits[f.id].split('\n') : f.lines);
  const currentDemoFile = () => byId[state.demoFile] || demoFiles[0];

  function demoPositions(symbol) {
    const re = new RegExp('\\b' + symbol + '\\b');
    return demoFiles.flatMap(f => demoLines(f).flatMap((text, index) => (re.test(text) ? [{file: f.id, line: index + 1, text}] : [])));
  }

  function colorCode(text) {
    if (text.trim().startsWith('//') || text.trim().startsWith('# 示例') || text.trim().startsWith('<!--')) {
      return '<span class="cw-comment">' + esc(text) + '</span>';
    }
    const parts = text.split(/([A-Za-z_][A-Za-z_0-9]*)/g);
    return parts.map(token => definitions[token]
      ? '<button type="button" class="cw-symbol cursor-interaction" data-symbol="' + token + '">' + esc(token) + '</button>'
      : (/^(public|private|static|final|class|interface|void|int|return|if|const|package|struct|true|nullptr)$/.test(token)
        ? '<span class="cw-keyword">' + token + '</span>' : esc(token))).join('');
  }

  // ---------------------------------------------------------------- 真实服务器
  async function connectServer() {
    state.connection = 'connecting';
    state.connectionError = '';
    state.serverInfo = null;
    render();
    try {
      const health = await api.health(state.apiBase, {});
      const list = await api.workspaces(state.apiBase, {});
      state.serverInfo = health;
      state.workspaces = list.workspaces || [];
      state.sync = list.sync || null;
      if (!state.workspaces.length) throw new api.ApiError('后端没有配置任何源码根目录');
      const known = state.workspaces.some(item => item.id === state.workspace);
      if (!known) state.workspace = state.workspaces[0].id;
      state.connection = 'ready';
      state.tree = {byPath: {}, expanded: {}};
      const selected = currentWorkspace();
      if (selected && selected.remote && !selected.exists) {
        setNotice(`工作区 ${selected.id} 是远程仓库，尚未同步到服务器本地缓存：${selected.path}。点「同步远程仓库」或让服务器执行 python3 -m app --sync ${selected.id}`);
        setStatus('远程工作区未同步', 'warn');
      } else {
        setNotice(`已连接 ${state.apiBase} · 版本 ${health.version} · 检索 ${health.search.name}${health.search.version ? ' ' + health.search.version : ''}`);
        setStatus('已切换为真实源码模式：读取服务器文件，连不上不会回退到示例数据');
        if (!state.file && state.tab === 'files') loadTree('');
      }
    } catch (error) {
      state.connection = 'error';
      state.connectionError = error.message || String(error);
      setNotice('后端连接失败：' + state.connectionError + '（请确认服务已启动，以及 SSH 端口转发是否成功）');
      setStatus('真实模式不可用', 'error');
    }
    render();
  }

  function currentWorkspace() {
    return state.workspaces.find(item => item.id === state.workspace) || null;
  }

  function remoteWorkspaceNeedsSync() {
    const selected = currentWorkspace();
    return !!(selected && selected.remote && !selected.exists);
  }

  function startSyncPolling() {
    stopSyncPolling();
    state.syncTimer = setInterval(async () => {
      await refreshSyncStatus();
      if (!state.sync || !state.sync.running) {
        stopSyncPolling();
        await connectServer();
        state.file = null;
        state.tree = {byPath: {}, expanded: {}};
        render();
      }
    }, 1500);
  }

  function stopSyncPolling() {
    if (state.syncTimer) clearInterval(state.syncTimer);
    state.syncTimer = null;
  }

  async function refreshSyncStatus() {
    try {
      state.sync = await api.syncStatus(state.apiBase, {});
    } catch (error) {
      setStatus('读取同步状态失败：' + (error.message || error), 'error');
    }
    renderTop();
    renderBanner();
    renderFoot();
  }

  async function startSync() {
    const selected = currentWorkspace();
    if (!selected || !selected.remote) return;
    try {
      const body = await api.startSync(state.apiBase, {workspace: selected.id});
      state.sync = body.sync || state.sync;
      setNotice(body.started
        ? `已开始同步 ${selected.id}（只做 clone/fetch/checkout，不会 reset，也不会覆盖本地修改）`
        : body.message);
      setStatus(body.started ? '同步进行中…' : body.message, body.started ? 'ok' : 'warn');
      if (body.started) startSyncPolling();
    } catch (error) {
      setNotice('同步请求失败：' + (error.message || error));
      setStatus('同步失败：' + (error.message || error), 'error');
    }
    render();
  }

  async function cancelSync() {
    try {
      const body = await api.cancelSync(state.apiBase, {});
      state.sync = body.sync || state.sync;
      setStatus(body.cancelled ? '已发送取消同步' : '没有正在进行的同步任务', body.cancelled ? 'warn' : 'ok');
    } catch (error) {
      setStatus('取消同步失败：' + (error.message || error), 'error');
    }
    render();
  }

  function handleWorkspaceError(error) {
    if (error && error.code === 'workspace_not_synced') {
      const selected = currentWorkspace();
      setNotice(error.message + (error.details && error.details.hint ? '（' + error.details.hint + '）' : ''));
      setStatus('远程工作区未同步，请先同步', 'warn');
      if (selected) selected.exists = false;
      render();
      return true;
    }
    return false;
  }

  function useDemoMode() {
    cancelRunningSearch(true);
    stopSyncPolling();
    state.dataMode = 'demo';
    state.notice = '这是内置示例数据，不是真实源码。切到「真实服务器」才会读取服务器上的文件。';
    setStatus('示例数据已加载');
    render();
  }

  function useRealMode() {
    state.dataMode = 'real';
    state.nav = null;
    state.navWord = '';
    state.search = null;
    state.notice = '';
    setStatus('真实源码模式：读取服务器上的真实文件');
    connectServer();
  }

  async function loadTree(path, force) {
    const node = state.tree.byPath[path];
    if (node && node.entries && !force) return;
    state.tree.byPath[path] = {loading: true, entries: [], error: ''};
    renderList();
    try {
      const body = await api.tree(state.apiBase, {workspace: state.workspace, path});
      state.tree.byPath[path] = {loading: false, entries: body.entries || [], truncated: body.truncated, limit: body.limit, error: ''};
    } catch (error) {
      if (handleWorkspaceError(error)) {
        state.tree.byPath[path] = {loading: false, entries: [], error: ''};
      } else {
        state.tree.byPath[path] = {loading: false, entries: [], error: error.message || String(error)};
      }
    }
    renderList();
  }

  async function openReal(path, line) {
    const target = Number(line) || 1;
    const startLine = Math.max(0, target - 1 - Math.floor(WINDOW_LINES / 4));
    state.abortFile && state.abortFile.abort();
    const controller = new AbortController();
    state.abortFile = controller;
    state.fileError = '';
    state.file = {status: 'loading', path, startLine, lines: [], name: baseName(path)};
    state.fileLine = target;
    render();
    try {
      const body = await api.file(state.apiBase, {workspace: state.workspace, path, startLine, lineCount: WINDOW_LINES}, {signal: controller.signal});
      if (state.file.path !== path) return;
      state.file = body;
      state.fileLine = target;
      const statusText = body.status === 'ok'
        ? `已读取 ${body.path} · 第 ${body.startLine + 1}-${body.startLine + body.lineCount} 行 / 共 ${body.totalLines} 行`
        : `读取 ${body.path}：${body.status}${body.message ? ' · ' + body.message : ''}`;
      setStatus(statusText, body.status === 'ok' ? 'ok' : 'warn');
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (handleWorkspaceError(error)) return;
      state.file = {status: 'error', path, lines: [], name: baseName(path), message: error.message || String(error)};
      setStatus('读取失败：' + (error.message || error), 'error');
    }
    render();
  }

  async function loadMoreLines() {
    if (!state.file || state.file.status !== 'ok' || !state.file.hasMore) return;
    const from = state.file.startLine + state.file.lineCount;
    try {
      const body = await api.file(state.apiBase, {workspace: state.workspace, path: state.file.path, startLine: from, lineCount: WINDOW_LINES});
      if (body.status !== 'ok') { setStatus('继续读取失败：' + body.status, 'warn'); return; }
      state.file = {...body, startLine: state.file.startLine, lines: state.file.lines.concat(body.lines), lineCount: state.file.lineCount + body.lineCount};
      setStatus(`已加载到第 ${state.file.startLine + state.file.lineCount} 行 / 共 ${state.file.totalLines} 行`);
    } catch (error) {
      setStatus('继续读取失败：' + (error.message || error), 'error');
    }
    render();
  }

  function searchBody(requestId) {
    return {
      query: state.q,
      workspace: state.workspace,
      scope: state.scope,
      regex: state.options.regex,
      caseSensitive: state.options.caseSensitive,
      wholeWord: state.options.wholeWord,
      requestId
    };
  }

  async function runSearch() {
    if (state.dataMode !== 'real') { renderList(); return; }
    if (state.connection !== 'ready') { setStatus('未连接后端，无法执行真实检索', 'warn'); return; }
    const query = $('cw-search').value.trim();
    if (!query) { setStatus('请输入检索内容', 'warn'); return; }
    state.q = query;
    cancelRunningSearch(true);
    const requestId = 'web-' + Date.now().toString(36) + '-' + Math.random().toString(16).slice(2, 8);
    state.searchRequestId = requestId;
    state.searching = true;
    state.searchError = '';
    state.tab = 'search';
    const controller = new AbortController();
    state.abort = controller;
    render();
    try {
      const body = await api.search(state.apiBase, searchBody(requestId), {signal: controller.signal});
      if (state.searchRequestId !== requestId) return;
      state.search = body;
      const labels = {ok: '完成', cancelled: '已取消', timeout: '超时', busy: '并发已满', error: '失败'};
      const label = labels[body.status] || body.status;
      setStatus(`检索${label}：${body.matchCount} 处 / ${body.filesMatched} 个文件 · ${body.elapsedMs}ms · 引擎 ${body.engine}${body.reason ? ' · ' + body.reason : ''}`, body.status === 'ok' ? 'ok' : 'warn');
    } catch (error) {
      if (error && error.name === 'AbortError') { setStatus('检索已取消（浏览器请求已中断）', 'warn'); return; }
      state.search = null;
      state.searchError = error.message || String(error);
      handleWorkspaceError(error);
      setStatus('检索失败：' + state.searchError, 'error');
    } finally {
      if (state.searchRequestId === requestId) state.searching = false;
      render();
    }
  }

  function cancelRunningSearch(silent) {
    if (!state.searching) return;
    const requestId = state.searchRequestId;
    state.searchRequestId = '';
    state.searching = false;
    state.abort && state.abort.abort();
    state.abort = null;
    if (requestId && state.dataMode === 'real') {
      api.cancelSearch(state.apiBase, requestId, {}).catch(() => {});
    }
    if (!silent) setStatus('已发送取消：服务端会终止对应检索进程');
  }

  async function requestNavigation(word, line, column) {
    state.navWord = word;
    const path = state.file && state.file.path;
    if (!path) return;
    try {
      state.nav = await api.navigation(state.apiBase, {
        workspace: state.workspace,
        path,
        kind: 'definition',
        sourceVersion: (state.file && state.file.hash) || null,
        position: {line: Math.max(0, line - 1), character: column}
      });
      setStatus(`跳转请求返回 ${state.nav.status}：${state.nav.reason || ''}`, 'warn');
    } catch (error) {
      state.nav = {status: 'error', kind: 'semantic', reason: error.message || String(error), targets: []};
      setStatus('跳转请求失败：' + (error.message || error), 'error');
    }
    renderInspector();
  }

  // ---------------------------------------------------------------- 渲染
  function renderBanner() {
    const banner = $('cw-banner');
    const sync = state.sync;
    let text = state.notice || (state.dataMode === 'real' && state.connection === 'error' ? '后端未连接' : '');
    if (state.dataMode === 'real' && sync && (sync.running || (sync.status && sync.status !== 'idle'))) {
      const tail = (sync.log || []).slice(-2).join(' ｜ ');
      const head = sync.running
        ? `正在同步 ${sync.workspaceId}（${(sync.steps || []).join(' → ') || '准备中'}）`
        : `同步${sync.status === 'ready' ? '完成' : '：' + sync.status} · ${sync.workspaceId} · ${sync.message}`;
      text = head + (tail ? ` ｜ ${tail}` : '') + (text ? ` ｜ ${text}` : '');
    }
    banner.hidden = !text;
    banner.textContent = text || '';
  }

  function renderFoot() {
    $('cw-status').textContent = state.status;
    $('cw-status').dataset.kind = state.statusKind || 'ok';
    const right = $('cw-footright');
    if (state.dataMode === 'demo') {
      right.textContent = `示例数据 ${demoFiles.length} 个内置文件 · 后端未使用`;
      return;
    }
    if (state.connection !== 'ready') {
      right.textContent = `真实模式 · 后端状态：${state.connection === 'connecting' ? '连接中' : '未连接'}`;
      return;
    }
    const info = state.serverInfo || {};
    const engine = info.search ? info.search.name + (info.search.version ? ' ' + info.search.version : '') : '未知';
    const active = (info.activeSearches || []).length;
    const selected = currentWorkspace();
    let remote = '';
    if (selected && selected.remote) {
      const gitState = selected.sync || {};
      remote = selected.exists
        ? ` · 远程 HEAD ${gitState.head || '—'}${gitState.shallow ? ' · 浅克隆' : ''}${gitState.dirtyFiles ? ' · 本地改动 ' + gitState.dirtyFiles + ' 个文件' : ''}`
        : ' · 远程未同步';
    }
    right.textContent = `真实模式 · ${state.workspace} · 引擎 ${engine} · 索引：未建立（P3） · 写入：未开放（P4）${remote}${active ? ' · 进行中检索 ' + active : ''}`;
  }

  function renderTop() {
    const real = state.dataMode === 'real';
    $('cw-use-demo').setAttribute('aria-pressed', String(!real));
    $('cw-use-real').setAttribute('aria-pressed', String(real));
    $('cw-mode-tag').textContent = real ? '真实源码模式' : '示例数据 · 内置演示';
    $('cw-mode-tag').className = 'cw-tag ' + (real ? 'cw-real' : 'cw-demo');
    $('cw-serverwrap').hidden = !real;
    $('cw-workwrap').hidden = !real || state.connection !== 'ready';
    $('cw-serverurl').value = state.apiBase;
    const selected = currentWorkspace();
    const workSelect = $('cw-workspace');
    const options = state.workspaces.length
      ? state.workspaces.map(item => {
        const suffix = item.remote
          ? '（远程只读' + (item.exists ? '' : ' · 未同步') + '）'
          : (item.exists === false ? '（不可用）' : '');
        return `<option value="${esc(item.id)}"${item.id === state.workspace ? ' selected' : ''}>${esc(item.name)}${suffix}</option>`;
      }).join('')
      : '<option value="">未连接</option>';
    if (workSelect.innerHTML !== options) workSelect.innerHTML = options;
    workSelect.disabled = state.connection !== 'ready' || state.workspaces.length < 2;

    const syncRunning = !!(state.sync && state.sync.running);
    const showSync = real && state.connection === 'ready' && !!(selected && selected.remote);
    $('cw-sync').hidden = !showSync || syncRunning;
    $('cw-sync').textContent = selected && selected.exists ? '同步远程仓库（fetch）' : '同步远程仓库（首次 clone）';
    $('cw-sync-cancel').hidden = !syncRunning;

    const scopeSelect = $('cw-scope');
    const scopeOptions = [];
    if (real) {
      const current = state.file && state.file.path ? dirName(state.file.path) : '';
      scopeOptions.push(`<option value="">全部源码（${esc(state.workspace || '未连接')}）</option>`);
      if (current) scopeOptions.push(`<option value="${esc(current)}"${state.scope === current ? ' selected' : ''}>当前目录：${esc(current)}</option>`);
    } else {
      scopeOptions.push('<option value="">全部示例源码</option>');
    }
    const html = scopeOptions.join('');
    if (scopeSelect.innerHTML !== html) scopeSelect.innerHTML = html;
    scopeSelect.disabled = real && state.connection !== 'ready';
    scopeSelect.value = real ? state.scope : '';

    $('cw-opt-regex').checked = state.options.regex;
    $('cw-opt-case').checked = state.options.caseSensitive;
    $('cw-opt-word').checked = state.options.wholeWord;
    root.querySelectorAll('#cw-opts input').forEach(input => { input.disabled = !real; });
    $('cw-search-go').disabled = !real || state.connection !== 'ready' || state.searching;
    $('cw-search-cancel').hidden = !state.searching;
    if ($('cw-search').value !== state.q) $('cw-search').value = state.q;
  }

  function renderSearchListReal() {
    if (state.connection !== 'ready') {
      return `<div class="cw-empty cw-empty-error">真实模式未连接后端。<div class="cw-empty-sub">${esc(state.connectionError || '请检查服务状态与 SSH 端口转发')}</div><div class="cw-empty-sub">此模式不会显示内置示例数据。</div></div>`;
    }
    if (state.searching) return '<div class="cw-empty">正在检索…<div class="cw-empty-sub">可按「取消」终止服务端扫描</div></div>';
    if (state.searchError) return `<div class="cw-empty cw-empty-error">检索失败<div class="cw-empty-sub">${esc(state.searchError)}</div></div>`;
    if (!state.search) return '<div class="cw-empty">回车或点「搜索」执行全库检索<div class="cw-empty-sub">默认字面量匹配；正则、大小写、全字可选</div></div>';
    const body = state.search;
    if (body.status === 'cancelled') return `<div class="cw-empty">检索已取消<div class="cw-empty-sub">${esc(body.reason || '')}</div></div>`;
    if (body.status === 'timeout') return `<div class="cw-empty cw-empty-error">检索超时<div class="cw-empty-sub">${esc(body.reason || '')}</div></div>`;
    if (body.status === 'error') return `<div class="cw-empty cw-empty-error">检索不可用<div class="cw-empty-sub">${esc(body.reason || '')}</div></div>`;
    if (!body.matches.length) return '<div class="cw-empty">没有匹配结果<div class="cw-empty-sub">可尝试取消「全字」或检查范围设置</div></div>';
    let html = body.matches.map(match => `<button type="button" class="cw-result cursor-interaction${state.file && state.file.path === match.path && state.fileLine === match.lineDisplay ? ' cw-current' : ''}" data-real="${esc(match.path)}" data-line="${match.lineDisplay}"><span class="cw-result-name">${esc(baseName(match.path))}<span class="cw-src">${esc(match.source)}</span></span><span class="cw-path">${esc(dirName(match.path))}</span><span class="cw-snippet">${match.lineDisplay} · ${highlight(match.text.trim(), state.q)}${match.textTruncated ? ' …' : ''}</span></button>`).join('');
    if (body.truncated) html += `<div class="cw-note">结果已截断：上限 ${body.limit} 条${body.reason ? ' · ' + esc(body.reason) : ''}</div>`;
    if (Object.values(body.skipped || {}).some(value => value)) {
      const skipped = body.skipped;
      html += `<div class="cw-note">跳过：二进制 ${skipped.binary || 0} · 大文件 ${skipped.large || 0} · 无法解码 ${skipped.undecodable || 0}</div>`;
    }
    if (body.filesScanned === null) html += '<div class="cw-note">引擎未报告扫描文件数（外部 ripgrep 只报告命中文件）</div>';
    return html;
  }

  function renderTreeList() {
    if (state.tab !== 'files') return '';
    const parts = [];
    const walk = (path, depth) => {
      const node = state.tree.byPath[path];
      if (!node) return;
      const indent = 8 + depth * 12;
      if (node.loading) { parts.push(`<div class="cw-note" style="padding-left:${indent}px">加载中…</div>`); return; }
      if (node.error) { parts.push(`<div class="cw-note cw-note-error" style="padding-left:${indent}px">${esc(node.error)}</div>`); return; }
      if (!node.entries.length) { parts.push(`<div class="cw-note" style="padding-left:${indent}px">空目录</div>`); return; }
      for (const entry of node.entries) {
        const isDir = entry.type === 'dir';
        const expanded = !!state.tree.expanded[entry.path];
        const blocked = entry.accessible === false;
        const marker = blocked ? '⤫' : (isDir ? (expanded ? '▾' : '▸') : '·');
        parts.push(`<button type="button" class="cw-file cw-treeitem cursor-interaction${blocked ? ' cw-blocked' : ''}" data-tree="${esc(entry.path)}" data-kind="${entry.type}" data-accessible="${entry.accessible !== false}" style="padding-left:${indent}px" aria-pressed="${expanded}"><span class="cw-icontext" aria-hidden="true">${marker}</span><span>${esc(entry.name)}${entry.symlink ? ' ↗' : ''}${blocked ? '（根目录外，已阻止）' : ''}</span></button>`);
        if (isDir && expanded && !blocked) walk(entry.path, depth + 1);
      }
      if (node.truncated) parts.push(`<div class="cw-note" style="padding-left:${indent}px">目录条目超过上限 ${node.limit}，已截断</div>`);
    };
    walk('');
    return parts.join('') || '<div class="cw-empty">展开目录以浏览真实文件</div>';
  }

  function renderUnsyncedPrompt() {
    const selected = currentWorkspace() || {};
    const git = selected.git || {};
    return `<div class="cw-empty cw-empty-error">远程工作区尚未同步<div class="cw-empty-sub">来源：${esc(git.url || '')}<br>检出目录：${esc(selected.path || '')}<br>点上方「同步远程仓库」，或在服务器执行<br><code>python3 -m app --sync ${esc(selected.id || '')}</code></div><div class="cw-empty-sub">同步只做 clone / fetch / checkout，不会 reset --hard，也不会覆盖缓存里已有的本地修改。</div></div>`;
  }

  function renderList() {
    root.querySelectorAll('[data-nav]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.nav === state.tab)));
    $('cw-change-count').textContent = state.dataMode === 'demo' ? Object.keys(state.edits).length : '—';

    if (state.dataMode === 'demo') {
      if (state.tab === 'search') {
        $('cw-list-title').textContent = '示例代码中的匹配';
        const query = state.q.trim().toLowerCase();
        const matches = demoFiles.flatMap(f => {
          const found = demoLines(f).flatMap((text, index) => (query && text.toLowerCase().includes(query) ? [{file: f, line: index + 1, text}] : []));
          if (found.length) return found;
          if (query && f.path.toLowerCase().includes(query)) return [{file: f, line: 1, text: demoLines(f)[0] || ''}];
          return [];
        });
        $('cw-count').textContent = matches.length + ' 处';
        let html = matches.slice(0, 40).map(m => `<button type="button" class="cw-result cursor-interaction${m.file.id === state.demoFile && m.line === state.demoLine ? ' cw-current' : ''}" data-open="${m.file.id}" data-line="${m.line}"><span class="cw-result-name">${esc(baseName(m.file.path))}<span class="cw-src">示例</span></span><span class="cw-path">${esc(dirName(m.file.path))}</span><span class="cw-snippet">${m.line} · ${highlight(m.text.trim(), state.q.trim())}</span></button>`).join('');
        if (!matches.length) html = '<div class="cw-empty">' + (query ? '没有匹配结果' : '输入文件名、符号或代码') + '</div>';
        if (matches.length > 40) html += `<div class="cw-note">示例模式只展示前 40 条（共 ${matches.length} 条）</div>`;
        $('cw-list').innerHTML = html;
        return;
      }
      const changed = Object.keys(state.edits);
      const list = state.tab === 'changes' ? demoFiles.filter(f => changed.includes(f.id)) : demoFiles;
      $('cw-list-title').textContent = state.tab === 'changes' ? '本次演示中的修改' : '内置示例源码树';
      $('cw-count').textContent = list.length + ' 文件';
      let html = '';
      let lastGroup = '';
      for (const f of list) {
        const group = f.path.split('/')[0];
        if (group !== lastGroup) { html += '<div class="cw-group">' + esc(group) + '</div>'; lastGroup = group; }
        html += `<button type="button" class="cw-file cursor-interaction" data-open="${f.id}" data-line="1" aria-pressed="${f.id === state.demoFile}"><span class="cw-icontext" aria-hidden="true">‹›</span><span>${esc(baseName(f.path))}${changed.includes(f.id) ? ' · M' : ''}</span></button>`;
      }
      if (!list.length) html = '<div class="cw-empty">暂无修改</div>';
      $('cw-list').innerHTML = html;
      return;
    }

    if (remoteWorkspaceNeedsSync()) {
      $('cw-list-title').textContent = '远程工作区待同步';
      $('cw-count').textContent = '';
      $('cw-list').innerHTML = renderUnsyncedPrompt();
      return;
    }

    if (state.tab === 'search') {
      $('cw-list-title').textContent = '全库检索（真实文件）';
      $('cw-count').textContent = state.search ? (state.search.matchCount + ' 处 / ' + state.search.filesMatched + ' 文件') : '';
      $('cw-list').innerHTML = renderSearchListReal();
      return;
    }
    if (state.tab === 'files') {
      $('cw-list-title').textContent = '真实源码树（按层加载）';
      const total = Object.values(state.tree.byPath).reduce((sum, node) => sum + (node.entries ? node.entries.length : 0), 0);
      $('cw-count').textContent = total ? total + ' 项' : '';
      $('cw-list').innerHTML = state.connection === 'ready' ? renderTreeList() : renderSearchListReal();
      return;
    }
    $('cw-list-title').textContent = '修改';
    $('cw-count').textContent = '未开放';
    $('cw-list').innerHTML = '<div class="cw-empty cw-empty-error">真实写入未开放（P1）<div class="cw-empty-sub">服务端保存、冲突检测与 diff 属于 P4，本轮不提供写入接口。</div></div>';
  }

  function renderEditor() {
    const back = $('cw-back');
    const edit = $('cw-edit');
    const diff = $('cw-diff');
    if (state.dataMode === 'demo') {
      const f = currentDemoFile();
      const lines = demoLines(f);
      $('cw-filename').textContent = baseName(f.path) + (Object.hasOwn(state.edits, f.id) ? ' · M' : '');
      $('cw-path').textContent = '示例数据 · ' + f.path + ' · ' + f.lang;
      back.disabled = !state.history.length || state.editing;
      edit.disabled = state.editing;
      diff.disabled = state.editing;
      diff.setAttribute('aria-pressed', String(state.diff));
      edit.textContent = '编辑';
      if (state.editing) {
        $('cw-view').innerHTML = '<div class="cw-editarea"><textarea id="cw-buffer" aria-label="编辑示例代码" spellcheck="false"></textarea><div class="cw-editactions"><button type="button" id="cw-cancel" class="cursor-interaction">取消</button><button type="button" id="cw-save" class="cw-primary cursor-interaction">保存到演示</button></div></div>';
        $('cw-buffer').value = lines.join('\n');
      } else if (state.diff) {
        const modified = lines;
        const original = f.lines;
        if (!Object.hasOwn(state.edits, f.id)) {
          $('cw-view').innerHTML = '<div class="cw-code"><div class="cw-empty">当前文件与示例基线一致。编辑后可在此查看差异。</div></div>';
        } else {
          let out = '<div class="cw-diffhead">示例基线 → 本次修改（按行对齐的简易对照，不是最终 diff）</div>';
          for (let i = 0; i < Math.max(original.length, modified.length); i += 1) {
            if (original[i] === modified[i]) out += '<div class="cw-diffline">  ' + esc(original[i] ?? '') + '</div>';
            else {
              if (i < original.length) out += '<div class="cw-diffline cw-removed">− ' + esc(original[i]) + '</div>';
              if (i < modified.length) out += '<div class="cw-diffline cw-added">+ ' + esc(modified[i]) + '</div>';
            }
          }
          $('cw-view').innerHTML = '<div class="cw-code">' + out + '</div><div class="cw-editactions"><button type="button" id="cw-revert" class="cursor-interaction">恢复此文件的示例基线</button></div>';
        }
      } else {
        $('cw-view').innerHTML = '<div class="cw-code">' + lines.map((text, index) => `<div class="cw-line${index + 1 === state.demoLine ? ' cw-highlight' : ''}"><span class="cw-lineno">${index + 1}</span><span class="cw-linecode">${colorCode(text)}</span></div>`).join('') + '</div>';
      }
    } else {
      back.disabled = !state.history.length;
      edit.disabled = false;
      diff.disabled = false;
      diff.setAttribute('aria-pressed', 'false');
      edit.textContent = '编辑（P1 未开放）';
      const file = state.file;
      const unsynced = remoteWorkspaceNeedsSync();
      $('cw-filename').textContent = unsynced ? '（远程工作区未同步）' : (file ? baseName(file.path) : '（未打开文件）');
      if (unsynced) {
        const selected = currentWorkspace() || {};
        $('cw-path').textContent = `远程只读 · ${(selected.git || {}).url || ''}`;
        $('cw-view').innerHTML = '<div class="cw-code">' + renderUnsyncedPrompt() + '</div>';
      } else if (!file) {
        $('cw-path').textContent = state.connection === 'ready' ? `真实模式 · 工作区 ${state.workspace} · 从左侧选择文件` : '真实模式 · 后端未连接';
        $('cw-view').innerHTML = `<div class="cw-code"><div class="cw-empty">${state.connection === 'ready' ? '在左侧「文件」中浏览真实目录，或用搜索定位文件。' : '未连接后端，真实模式不显示示例数据。'}</div></div>`;
      } else {
        $('cw-path').textContent = `真实文件 · ${file.path} · ${file.language || '未知语言'}${file.hash ? ' · ' + file.hash.slice(0, 15) + '…' : ''} · 只读：${file.readonly ? '是' : '否'}`;
        if (file.status === 'loading') $('cw-view').innerHTML = '<div class="cw-code"><div class="cw-empty">读取中…</div></div>';
        else if (file.status !== 'ok') {
          $('cw-view').innerHTML = `<div class="cw-code"><div class="cw-empty cw-empty-error">无法作为文本显示（${esc(file.status)}）<div class="cw-empty-sub">${esc(file.message || '')}</div></div></div>`;
        } else {
          const lines = file.lines.map((text, index) => {
            const number = file.startLine + index + 1;
            return `<div class="cw-line${number === state.fileLine ? ' cw-highlight' : ''}" data-lineno="${number}"><span class="cw-lineno">${number}</span><span class="cw-linecode">${esc(text) || '&nbsp;'}</span></div>`;
          }).join('');
          const more = file.hasMore ? `<div class="cw-editactions"><button type="button" id="cw-loadmore" class="cursor-interaction">继续加载后续 ${WINDOW_LINES} 行（已到 ${file.startLine + file.lineCount} / ${file.totalLines}）</button></div>` : '';
          $('cw-view').innerHTML = `<div class="cw-code cw-realcode" id="cw-realcode">${lines}</div>${more}<div class="cw-note">点击任意标识符会发起语义跳转请求；P1 会明确返回未就绪，不会伪造跳转。哈希 ${esc(file.hash || '')}，编码 ${esc(file.encoding || '')}，换行 ${esc(file.eol || '')}</div>`;
        }
      }
    }
    $('cw-history').innerHTML = state.trail.slice(-4).map((point, index) => {
      const label = point.mode === 'demo' ? baseName((byId[point.key] || {}).path || point.key) : baseName(point.key);
      return `<button type="button" class="cursor-interaction" data-trail="${index}">${index ? '› ' : ''}${esc(label)}</button>`;
    }).join('');
  }

  function renderInspector() {
    if (state.dataMode === 'demo') {
      const symbol = state.demoSymbol;
      $('cw-symbol-name').textContent = symbol;
      const defs = definitions[symbol] || [];
      $('cw-relation-note').textContent = '示例候选 · 手工登记的 ' + defs.length + ' 个位置' + (symbol === 'nativeSetBrightness' ? ' · 注册表提供关联线索' : '');
      $('cw-definitions').innerHTML = defs.map(([id, line, why]) => {
        const changed = Object.hasOwn(state.edits, id);
        return `<button type="button" class="cw-definition cursor-interaction" data-open="${id}" data-line="${line}"><span class="cw-defname">${esc(baseName((byId[id] || {}).path || id))}</span><span class="cw-path">${esc(dirName((byId[id] || {}).path || ''))}</span><span class="cw-evidence">${esc(changed ? '文件已修改 · 原位置需核对' : why)} · L${line} ↗</span></button>`;
      }).join('') || '<div class="cw-note">示例数据中没有登记该符号。</div>';
      const refs = demoPositions(symbol);
      $('cw-refs-count').textContent = refs.length + ' 处（文本匹配）';
      $('cw-refs').innerHTML = refs.slice(0, 8).map(p => `<button type="button" class="cw-ref cursor-interaction" data-open="${p.file}" data-line="${p.line}">${esc(baseName((byId[p.file] || {}).path || p.file))} : ${p.line}</button>`).join('') + (refs.length > 8 ? `<button type="button" class="cw-ref cursor-interaction" data-opensearch="${esc(symbol)}">在示例搜索中查看全部</button>` : '');
      return;
    }

    $('cw-symbol-name').textContent = state.navWord || '（未选择标识符）';
    if (!state.nav) {
      $('cw-relation-note').textContent = '语义能力：未接入（P2）';
      $('cw-definitions').innerHTML = `<div class="cw-notready"><strong>未就绪</strong><div>${esc(NAV_NOT_READY)}</div><div class="cw-empty-sub">当前只提供文本检索：结果只表示字面匹配，不代表定义或引用。</div></div>`;
    } else {
      const nav = state.nav;
      $('cw-relation-note').textContent = `最近一次跳转请求：${nav.status} · ${nav.kind}`;
      const targets = (nav.targets || []).map(target => `<button type="button" class="cw-definition cursor-interaction" data-real="${esc(target.path)}" data-line="${(target.range && target.range.start ? target.range.start.line : 0) + 1}"><span class="cw-defname">${esc(baseName(target.path))}</span><span class="cw-path">${esc(dirName(target.path))}</span><span class="cw-evidence">${esc(target.evidence || '')}</span></button>`).join('');
      $('cw-definitions').innerHTML = `<div class="cw-notready cw-notready-${esc(nav.status)}"><strong>状态：${esc(nav.status)}</strong><div>${esc(nav.reason || '')}</div>${nav.sourceVersion ? `<div class="cw-empty-sub">请求携带版本：${esc(nav.sourceVersion)}</div>` : '<div class="cw-empty-sub">未携带源码版本</div>'}</div>${targets}`;
    }
    if (state.navWord && state.connection === 'ready') {
      $('cw-refs-count').textContent = '未接入语义引用';
      $('cw-refs').innerHTML = `<button type="button" class="cw-ref cursor-interaction" data-textsearch="${esc(state.navWord)}">在全库检索中查看「${esc(state.navWord)}」的字面匹配</button><div class="cw-note">字面匹配不等于引用：注释和字符串也会命中。</div>`;
    } else {
      $('cw-refs-count').textContent = '—';
      $('cw-refs').innerHTML = '<div class="cw-note">点击代码中的标识符后，这里显示请求结果与文本匹配入口。</div>';
    }
  }

  function render() {
    renderTop();
    renderBanner();
    renderList();
    renderEditor();
    renderInspector();
    renderFoot();
  }

  // ---------------------------------------------------------------- 打开与返回
  function pushHistory() {
    if (state.dataMode === 'demo') state.history.push({mode: 'demo', key: state.demoFile, line: state.demoLine});
    else state.history.push({mode: 'real', key: state.file ? state.file.path : '', line: state.fileLine});
  }

  function openDemo(id, line) {
    if (!byId[id]) return;
    if (state.editing) { setStatus('请先保存到演示或取消编辑', 'warn'); return; }
    pushHistory();
    state.demoFile = id;
    state.demoLine = Math.min(Number(line) || 1, demoLines(byId[id]).length || 1);
    state.diff = false;
    state.trail.push({mode: 'demo', key: id, line: state.demoLine});
    render();
    setStatus('阅读位置：' + baseName(byId[id].path) + ' : ' + state.demoLine);
  }

  function goBack() {
    const previous = state.history.pop();
    if (!previous) return;
    if (previous.mode === 'demo') {
      state.demoFile = previous.key;
      state.demoLine = previous.line;
      state.diff = false;
      state.trail.push(previous);
      render();
      setStatus('已返回 ' + baseName((byId[previous.key] || {}).path || previous.key));
    } else {
      state.trail.push(previous);
      openReal(previous.key, previous.line);
    }
  }

  function toggleTree(path, kind, accessible) {
    if (accessible === false) { setStatus('该符号链接指向根目录之外，已按安全策略阻止访问', 'warn'); return; }
    if (kind === 'dir') {
      state.tree.expanded[path] = !state.tree.expanded[path];
      if (state.tree.expanded[path] && !state.tree.byPath[path]) loadTree(path);
      else renderList();
      return;
    }
    openReal(path, 1);
  }

  function wordAtPoint(x, y) {
    let node = null;
    let offset = 0;
    if (document.caretRangeFromPoint) {
      const range = document.caretRangeFromPoint(x, y);
      if (range) { node = range.startContainer; offset = range.startOffset; }
    } else if (document.caretPositionFromPoint) {
      const position = document.caretPositionFromPoint(x, y);
      if (position) { node = position.offsetNode; offset = position.offset; }
    }
    if (!node || node.nodeType !== 3) return null;
    const text = node.nodeValue || '';
    const isWord = ch => !!ch && /[A-Za-z0-9_$]/.test(ch);
    if (!isWord(text[offset]) && !isWord(text[offset - 1])) return null;
    let start = offset;
    let end = offset;
    while (start > 0 && isWord(text[start - 1])) start -= 1;
    while (end < text.length && isWord(text[end])) end += 1;
    const word = text.slice(start, end);
    if (!word || /^[0-9]/.test(word)) return null;
    return {word, start, end};
  }

  // ---------------------------------------------------------------- 事件
  root.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (target && !target.disabled) {
      if (target.dataset.nav) { state.tab = target.dataset.nav; if (state.tab === 'files' && state.dataMode === 'real' && state.connection === 'ready' && !state.tree.byPath['']) loadTree(''); renderList(); return; }
      if (target.dataset.open) { openDemo(target.dataset.open, target.dataset.line); return; }
      if (target.dataset.real) { pushHistory(); openReal(target.dataset.real, target.dataset.line); return; }
      if (target.dataset.tree) { toggleTree(target.dataset.tree, target.dataset.kind, target.dataset.accessible !== 'false'); return; }
      if (target.dataset.symbol) { state.demoSymbol = target.dataset.symbol; renderInspector(); setStatus('显示 ' + state.demoSymbol + ' 的示例候选和文本出现位置'); return; }
      if (target.dataset.opensearch) { state.q = target.dataset.opensearch; state.tab = 'search'; render(); return; }
      if (target.dataset.textsearch) { state.q = target.dataset.textsearch; state.tab = 'search'; render(); runSearch(); return; }
      if (target.dataset.trail !== undefined && target.dataset.trail !== '') {
        const point = state.trail[Number(target.dataset.trail)];
        if (point) { point.mode === 'demo' ? openDemo(point.key, point.line) : openReal(point.key, point.line); }
        return;
      }
      switch (target.id) {
        case 'cw-use-demo': useDemoMode(); return;
        case 'cw-use-real': useRealMode(); return;
        case 'cw-connect': state.apiBase = api.normalizeBase($('cw-serverurl').value); state.workspace = ''; connectServer(); return;
        case 'cw-sync': startSync(); return;
        case 'cw-sync-cancel': cancelSync(); return;
        case 'cw-search-go': runSearch(); return;
        case 'cw-search-cancel': cancelRunningSearch(false); renderList(); return;
        case 'cw-back': goBack(); return;
        case 'cw-loadmore': loadMoreLines(); return;
        case 'cw-edit':
          if (state.dataMode === 'real') { setNotice('编辑未开放：P1 不提供真实文件写入，服务端保存与冲突检测属于 P4。'); setStatus('编辑未开放（P4）', 'warn'); return; }
          state.editing = true; state.diff = false; renderEditor(); $('cw-buffer').focus(); setStatus('正在编辑示例代码；不会写入服务器'); return;
        case 'cw-cancel': state.editing = false; renderEditor(); setStatus('已取消编辑'); return;
        case 'cw-save': {
          const value = $('cw-buffer').value;
          const original = currentDemoFile().lines.join('\n');
          if (value === original) delete state.edits[state.demoFile];
          else state.edits[state.demoFile] = value;
          state.editing = false;
          state.diff = true;
          render();
          setStatus('已保存到本次演示；搜索已包含修改内容（不会写入磁盘）');
          return;
        }
        case 'cw-diff':
          if (state.dataMode === 'real') { setNotice('真实文件的差异对比属于 P4；当前仅示例模式可用。'); setStatus('差异对比：未就绪（P4）', 'warn'); return; }
          state.diff = !state.diff; renderEditor(); return;
        case 'cw-revert': delete state.edits[state.demoFile]; state.diff = false; render(); setStatus('此文件已恢复到示例基线'); return;
      }
    }

    const lineElement = event.target.closest('.cw-line');
    if (lineElement && state.dataMode === 'real' && state.file && state.file.status === 'ok' && state.connection === 'ready') {
      const found = wordAtPoint(event.clientX, event.clientY);
      if (!found) return;
      const number = Number(lineElement.dataset.lineno) || 1;
      requestNavigation(found.word, number, found.start);
    }
  });

  root.addEventListener('change', event => {
    const target = event.target;
    if (target.id === 'cw-workspace') {
      state.workspace = target.value;
      state.file = null;
      state.scope = '';
      state.tree = {byPath: {}, expanded: {}};
      state.search = null;
      state.notice = '';
      const selected = currentWorkspace();
      setStatus(`已切换工作区：${target.value}${selected && selected.remote ? '（远程只读' + (selected.exists ? '' : ' · 未同步') + '）' : ''}`);
      render();
      if (selected && selected.remote && !selected.exists) {
        setNotice(`工作区 ${selected.id} 尚未同步，点「同步远程仓库」或让服务器执行 python3 -m app --sync ${selected.id}`);
      } else if (state.tab === 'files') {
        loadTree('');
      }
      return;
    }
    if (target.id === 'cw-scope') { state.scope = target.value; return; }
    if (target.id === 'cw-opt-regex') { state.options.regex = target.checked; return; }
    if (target.id === 'cw-opt-case') { state.options.caseSensitive = target.checked; return; }
    if (target.id === 'cw-opt-word') { state.options.wholeWord = target.checked; }
  });

  const searchInput = $('cw-search');
  searchInput.addEventListener('input', event => {
    state.q = event.target.value;
    if (state.dataMode === 'demo') { state.tab = 'search'; renderList(); }
  });
  searchInput.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); runSearch(); }
  });
  root.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); searchInput.focus(); searchInput.select(); }
    if (event.key === 'Escape') {
      if (state.searching) { cancelRunningSearch(false); renderList(); return; }
      if (state.editing) { state.editing = false; renderEditor(); setStatus('已取消编辑'); }
    }
  });

  // ---------------------------------------------------------------- 启动
  /* 允许用 URL 参数显式选择数据来源，方便书签和自动化检查：
     ?mode=real|demo &workspace=<id> &tab=search|files|changes &q=<关键字> */
  function applyUrlParams() {
    const params = new URLSearchParams(location.search);
    const mode = params.get('mode');
    const workspace = params.get('workspace');
    const tab = params.get('tab');
    const query = params.get('q');
    if (workspace) state.workspace = workspace;
    if (tab && ['search', 'files', 'changes'].includes(tab)) state.tab = tab;
    if (query !== null) state.q = query;
    return mode === 'real' ? 'real' : (mode === 'demo' ? 'demo' : '');
  }

  (async function boot() {
    render();
    const base = api.sameOriginBase() || api.DEFAULT_BASE;
    state.apiBase = base;
    const requested = applyUrlParams();
    if (requested === 'real') {
      state.notice = '';
      useRealMode();
      return;
    }
    render();
    try {
      const health = await api.health(base, {});
      if (health && health.status === 'ok') {
        state.serverInfo = health;
        setNotice('检测到同源后端可用（' + base + '，版本 ' + health.version + '）。点击右上「真实服务器」即可切换到真实源码；示例数据不会被真实模式读取。');
      }
    } catch (error) {
      /* 直接打开本地文件或后端未启动：保持示例模式，不报错也不伪装已连接 */
    }
    render();
  })();
})();
