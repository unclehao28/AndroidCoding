/* 安卓源码工作台后端客户端（P1 接口）。
   所有请求都指向配置的服务器地址；从服务器页面打开时使用同源地址，
   直接双击打开 index.html 时需要手填地址（例如 SSH 端口转发后的 http://127.0.0.1:8765）。 */
window.ASWApi = (() => {
  const DEFAULT_BASE = 'http://127.0.0.1:8787';

  function sameOriginBase() {
    return (location.protocol === 'http:' || location.protocol === 'https:') ? location.origin : '';
  }

  function normalizeBase(value) {
    const text = (value || '').trim().replace(/\/+$/, '');
    return text || DEFAULT_BASE;
  }

  class ApiError extends Error {
    constructor(message, info = {}) {
      super(message);
      this.name = 'ApiError';
      this.status = info.status || 0;
      this.code = info.code || 'client_error';
      this.details = info.details || null;
      this.url = info.url || '';
    }
  }

  async function request(base, path, options = {}) {
    const {method = 'GET', params, body, signal} = options;
    let url = normalizeBase(base) + path;
    if (params) {
      const search = new URLSearchParams();
      Object.entries(params).forEach(([key, value]) => {
        if (value !== undefined && value !== null && value !== '') search.append(key, String(value));
      });
      const qs = search.toString();
      if (qs) url += '?' + qs;
    }
    let response;
    try {
      response = await fetch(url, {
        method,
        signal,
        headers: body === undefined ? undefined : {'Content-Type': 'application/json'},
        body: body === undefined ? undefined : JSON.stringify(body)
      });
    } catch (error) {
      if (error && error.name === 'AbortError') throw error;
      throw new ApiError('无法连接后端：' + (error && error.message ? error.message : String(error)), {url});
    }
    const text = await response.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (error) { data = null; }
    }
    if (!response.ok) {
      const info = (data && data.error) || {};
      throw new ApiError(info.message || `后端返回 HTTP ${response.status}`, {
        status: response.status, code: info.code || 'http_error', details: info.details, url
      });
    }
    if (data === null) throw new ApiError('后端响应不是合法 JSON', {status: response.status, url});
    return data;
  }

  return {
    DEFAULT_BASE,
    sameOriginBase,
    normalizeBase,
    ApiError,
    health: (base, options = {}) => request(base, '/api/health', options),
    workspaces: (base, options = {}) => request(base, '/api/workspaces', options),
    publicConfig: (base, options = {}) => request(base, '/api/config', options),
    tree: (base, {workspace, path = ''}, options = {}) => request(base, '/api/tree', {...options, params: {workspace, path}}),
    file: (base, {workspace, path, startLine = 0, lineCount}, options = {}) =>
      request(base, '/api/file', {...options, params: {workspace, path, startLine, lineCount}}),
    search: (base, body, options = {}) => request(base, '/api/search', {...options, method: 'POST', body}),
    cancelSearch: (base, requestId, options = {}) =>
      request(base, '/api/search/cancel', {...options, method: 'POST', body: {requestId}}),
    navigation: (base, body, options = {}) => request(base, '/api/navigation', {...options, method: 'POST', body}),
    startSync: (base, body, options = {}) => request(base, '/api/workspace/sync', {...options, method: 'POST', body}),
    syncStatus: (base, options = {}) => request(base, '/api/workspace/sync', options),
    cancelSync: (base, options = {}) => request(base, '/api/workspace/sync/cancel', {...options, method: 'POST', body: {}})
  };
})();
