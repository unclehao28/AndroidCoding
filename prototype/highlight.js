/* 轻量语法高亮：不依赖任何外部库或 CDN，按行着色。
 *
 * 为什么自己写：产品约束里明确不依赖 CDN；而且 AOSP 的单文件可能上万行，
 * 只能按窗口渲染，逐行着色必须足够快（这里每行一次线性扫描，超长行直接跳过着色）。
 *
 * 用法：
 *   const state = {block: false};                       // 块注释状态在行之间传递
 *   html = __aswHighlight.codeLine(text, 'cpp', state); // 返回已转义的 HTML
 *
 * 诚实性：只做着色，不改变文本内容——所有文本都经过 HTML 转义，不会注入。
 */
(function (global) {
  'use strict';

  const ESCAPES = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  const esc = text => String(text).replace(/[&<>"']/g, c => ESCAPES[c]);
  const span = (cls, text) => '<span class="' + cls + '">' + esc(text) + '</span>';
  const MAX_LINE = 4000; // 超过这个长度不着色（避免极端行拖慢渲染）

  const C_KEYWORDS = new Set(
    ('alignas alignof asm auto break case catch class const consteval constexpr constinit const_cast continue '
      + 'decltype default delete do dynamic_cast else enum explicit export extern false for friend goto if inline '
      + 'mutable namespace new noexcept nullptr operator override final private protected public register '
      + 'reinterpret_cast return sizeof static static_assert static_cast struct switch template this throw true try '
      + 'typedef typeid typename union using virtual volatile while concept requires co_await co_return co_yield').split(' ')
  );
  const C_TYPES = new Set(
    ('bool char char8_t char16_t char32_t double float int long short signed unsigned void wchar_t size_t ssize_t '
      + 'int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t status_t sp wp String8 String16 '
      + 'std string vector map set unordered_map shared_ptr unique_ptr weak_ptr optional').split(' ')
  );
  const JAVA_KEYWORDS = new Set(
    ('abstract assert boolean break byte case catch char class const continue default do double else enum extends '
      + 'final finally float for goto if implements import instanceof int interface long native new package private '
      + 'protected public return short static strictfp super switch synchronized this throw throws transient try void '
      + 'volatile while var record sealed permits yield true false null').split(' ')
  );
  const RUST_KEYWORDS = new Set(
    ('as async await break const continue crate dyn else enum extern false fn for if impl in let loop match mod move '
      + 'mut pub ref return self Self static struct super trait true type unsafe use where while').split(' ')
  );
  const JS_KEYWORDS = new Set(
    ('async await break case catch class const continue debugger default delete do else export extends false finally '
      + 'for function if import in instanceof let new null of return static super switch this throw true try typeof '
      + 'undefined var void while yield').split(' ')
  );
  const PY_KEYWORDS = new Set(
    ('and as assert async await break class continue def del elif else except False finally for from global if import '
      + 'in is lambda None nonlocal not or pass raise return True try while with yield self').split(' ')
  );
  const SHELL_KEYWORDS = new Set(
    ('if then else elif fi for while until do done case esac function return in local export readonly unset shift '
      + 'trap set eval exec source').split(' ')
  );

  // 语言 → 扫描参数
  const PROFILES = {
    c: {lineComment: '//', blockComment: true, preprocessor: true, keywords: C_KEYWORDS, types: C_TYPES, charLiteral: true},
    cpp: {lineComment: '//', blockComment: true, preprocessor: true, keywords: C_KEYWORDS, types: C_TYPES, charLiteral: true},
    'cpp-header': {lineComment: '//', blockComment: true, preprocessor: true, keywords: C_KEYWORDS, types: C_TYPES, charLiteral: true},
    java: {lineComment: '//', blockComment: true, keywords: JAVA_KEYWORDS, charLiteral: true},
    kotlin: {lineComment: '//', blockComment: true, keywords: JAVA_KEYWORDS, charLiteral: true},
    aidl: {lineComment: '//', blockComment: true, keywords: JAVA_KEYWORDS, charLiteral: true},
    rust: {lineComment: '//', blockComment: true, keywords: RUST_KEYWORDS, charLiteral: true},
    javascript: {lineComment: '//', blockComment: true, keywords: JS_KEYWORDS},
    typescript: {lineComment: '//', blockComment: true, keywords: JS_KEYWORDS},
    python: {hashComment: true, keywords: PY_KEYWORDS},
    shell: {hashComment: true, keywords: SHELL_KEYWORDS, variables: true},
    make: {hashComment: true, variables: true},
    init: {hashComment: true, keywords: SHELL_KEYWORDS, variables: true},
    selinux: {hashComment: true},
    seccomp: {lineComment: '//', blockComment: true, keywords: C_KEYWORDS},
    cmake: {hashComment: true, variables: true, keywords: SHELL_KEYWORDS},
    kconfig: {hashComment: true},
    json: {jsonish: true},
    soong: {jsonish: true},
    xml: {xml: true},
    vintf: {xml: true},
  };

  function isIdentStart(ch) {
    return /[A-Za-z_$]/.test(ch);
  }
  function isIdentChar(ch) {
    return /[A-Za-z0-9_$]/.test(ch);
  }
  function isDigit(ch) {
    return ch >= '0' && ch <= '9';
  }

  /** C 家族扫描：注释、字符串、字符、预处理指令、数字、关键字/类型、标识符 */
  function scanClike(text, profile, state) {
    let out = '';
    let i = 0;
    const length = text.length;

    if (state.block) {
      const end = text.indexOf('*/');
      if (end < 0) {
        return span('cw-tok-comment', text);
      }
      out += span('cw-tok-comment', text.slice(0, end + 2));
      i = end + 2;
      state.block = false;
    }

    while (i < length) {
      const ch = text[i];
      const two = text.slice(i, i + 2);

      if (profile.lineComment && two === profile.lineComment) {
        out += span('cw-tok-comment', text.slice(i));
        return out;
      }
      if (profile.blockComment && two === '/*') {
        const end = text.indexOf('*/', i + 2);
        if (end < 0) {
          out += span('cw-tok-comment', text.slice(i));
          state.block = true;
          return out;
        }
        out += span('cw-tok-comment', text.slice(i, end + 2));
        i = end + 2;
        continue;
      }
      if (profile.preprocessor && ch === '#' && /^\s*#/.test(text.slice(0, i + 1))) {
        // 预处理指令整行着色（#include <...> 之类的尖括号不是标签）
        const rest = text.slice(i);
        out += span('cw-tok-preproc', rest);
        return out;
      }
      if (ch === '"' || (profile.charLiteral && ch === "'")) {
        let j = i + 1;
        while (j < length) {
          if (text[j] === '\\') { j += 2; continue; }
          if (text[j] === ch) { j += 1; break; }
          j += 1;
        }
        out += span('cw-tok-string', text.slice(i, j));
        i = j;
        continue;
      }
      if (isDigit(ch)) {
        let j = i;
        while (j < length && /[0-9a-fA-FxXoObB._+\-']/.test(text[j])) {
          // 只允许紧跟的 +- 出现在指数位置，避免把 a-1 吃掉
          if ((text[j] === '+' || text[j] === '-') && !/[eEpP]$/.test(text.slice(i, j))) break;
          j += 1;
        }
        out += span('cw-tok-number', text.slice(i, j));
        i = j;
        continue;
      }
      if (isIdentStart(ch)) {
        let j = i;
        while (j < length && isIdentChar(text[j])) j += 1;
        const word = text.slice(i, j);
        if (profile.keywords && profile.keywords.has(word)) out += span('cw-tok-keyword', word);
        else if (profile.types && profile.types.has(word)) out += span('cw-tok-type', word);
        else if (text[j] === '(') out += span('cw-tok-func', word);
        else out += esc(word);
        i = j;
        continue;
      }
      out += esc(ch);
      i += 1;
    }
    return out;
  }

  /** XML / AndroidManifest / vintf：标签名、属性名、属性值、注释 */
  function scanXml(text, state) {
    let out = '';
    let i = 0;
    const length = text.length;
    if (state.block) {
      const end = text.indexOf('-->');
      if (end < 0) return span('cw-tok-comment', text);
      out += span('cw-tok-comment', text.slice(0, end + 3));
      i = end + 3;
      state.block = false;
    }
    while (i < length) {
      if (text.slice(i, i + 4) === '<!--') {
        const end = text.indexOf('-->', i + 4);
        if (end < 0) { out += span('cw-tok-comment', text.slice(i)); state.block = true; return out; }
        out += span('cw-tok-comment', text.slice(i, end + 3));
        i = end + 3;
        continue;
      }
      if (text[i] === '<') {
        out += esc('<');
        let j = i + 1;
        if (text[j] === '/') { out += esc('/'); j += 1; }
        if (text[j] === '?' || text[j] === '!') {
          const end = text.indexOf('>', j);
          if (end < 0) { out += esc(text.slice(j)); return out; }
          out += span('cw-tok-tag', text.slice(j, end));
          out += esc('>');
          i = end + 1;
          continue;
        }
        let k = j;
        while (k < length && /[\w:.\-]/.test(text[k])) k += 1;
        if (k > j) out += span('cw-tok-tag', text.slice(j, k));
        let m = k;
        while (m < length && text[m] !== '>') {
          const ch = text[m];
          if (ch === '"' || ch === "'") {
            let e = m + 1;
            while (e < length && text[e] !== ch) e += 1;
            out += span('cw-tok-string', text.slice(m, Math.min(e + 1, length)));
            m = e + 1;
            continue;
          }
          if (/[\w:.\-]/.test(ch)) {
            let e = m;
            while (e < length && /[\w:.\-]/.test(text[e])) e += 1;
            out += span('cw-tok-attr', text.slice(m, e));
            m = e;
            continue;
          }
          out += esc(ch);
          m += 1;
        }
        if (m < length) { out += esc('>'); i = m + 1; } else { i = length; }
        continue;
      }
      const next = text.indexOf('<', i);
      const chunk = next < 0 ? text.slice(i) : text.slice(i, next);
      out += esc(chunk);
      i = next < 0 ? length : next;
    }
    return out;
  }

  /** JSON / Android.bp：键、字符串、数字、字面量、注释 */
  function scanJsonish(text, state) {
    let out = '';
    let i = 0;
    const length = text.length;
    if (state.block) {
      const end = text.indexOf('*/');
      if (end < 0) return span('cw-tok-comment', text);
      out += span('cw-tok-comment', text.slice(0, end + 2));
      i = end + 2;
      state.block = false;
    }
    while (i < length) {
      const two = text.slice(i, i + 2);
      if (two === '//') { out += span('cw-tok-comment', text.slice(i)); return out; }
      if (two === '/*') {
        const end = text.indexOf('*/', i + 2);
        if (end < 0) { out += span('cw-tok-comment', text.slice(i)); state.block = true; return out; }
        out += span('cw-tok-comment', text.slice(i, end + 2));
        i = end + 2;
        continue;
      }
      if (text[i] === '"') {
        let j = i + 1;
        while (j < length) {
          if (text[j] === '\\') { j += 2; continue; }
          if (text[j] === '"') { j += 1; break; }
          j += 1;
        }
        const raw = text.slice(i, j);
        const rest = text.slice(j);
        // 后面紧跟冒号的是键
        out += /^\s*:/.test(rest) ? span('cw-tok-attr', raw) : span('cw-tok-string', raw);
        i = j;
        continue;
      }
      if (isDigit(text[i]) || (text[i] === '-' && isDigit(text[i + 1]))) {
        let j = i + 1;
        while (j < length && /[0-9a-fA-FxX.eE+\-]/.test(text[j])) j += 1;
        out += span('cw-tok-number', text.slice(i, j));
        i = j;
        continue;
      }
      if (isIdentStart(text[i])) {
        let j = i;
        while (j < length && isIdentChar(text[j])) j += 1;
        const word = text.slice(i, j);
        const after = text.slice(j);
        if (/^(true|false|null|True|False)$/.test(word)) out += span('cw-tok-keyword', word);
        // Android.bp（blueprint）的键是不带引号的标识符：name: "libfoo"
        else if (/^\s*:/.test(after)) out += span('cw-tok-attr', word);
        else out += esc(word);
        i = j;
        continue;
      }
      out += esc(text[i]);
      i += 1;
    }
    return out;
  }

  /** Makefile / init.rc / shell / cmake：注释、字符串、变量 */
  function scanShellish(text, profile) {
    let out = '';
    let i = 0;
    const length = text.length;
    while (i < length) {
      const ch = text[i];
      if (profile.hashComment && ch === '#') { out += span('cw-tok-comment', text.slice(i)); return out; }
      if (ch === '"' || ch === "'") {
        let j = i + 1;
        while (j < length) {
          if (text[j] === '\\') { j += 2; continue; }
          if (text[j] === ch) { j += 1; break; }
          j += 1;
        }
        out += span('cw-tok-string', text.slice(i, j));
        i = j;
        continue;
      }
      if (profile.variables && ch === '$') {
        const match = /^\$[({]?[A-Za-z_][\w.]*[)}]?/.exec(text.slice(i));
        if (match) { out += span('cw-tok-var', match[0]); i += match[0].length; continue; }
      }
      if (isIdentStart(ch)) {
        let j = i;
        while (j < length && isIdentChar(text[j])) j += 1;
        const word = text.slice(i, j);
        out += (profile.keywords && profile.keywords.has(word)) ? span('cw-tok-keyword', word) : esc(word);
        i = j;
        continue;
      }
      if (isDigit(ch)) {
        let j = i;
        while (j < length && /[\w.]/.test(text[j])) j += 1;
        out += span('cw-tok-number', text.slice(i, j));
        i = j;
        continue;
      }
      out += esc(ch);
      i += 1;
    }
    return out;
  }

  function codeLine(text, language, state) {
    const value = text === null || text === undefined ? '' : String(text);
    if (!value) return '';
    if (value.length > MAX_LINE) return esc(value);
    const profile = PROFILES[String(language || '').toLowerCase()];
    const current = state || {block: false};
    if (!profile) return esc(value);
    if (profile.xml) return scanXml(value, current);
    if (profile.jsonish) return scanJsonish(value, current);
    if (profile.hashComment || profile.variables) return scanShellish(value, profile);
    return scanClike(value, profile, current);
  }

  global.__aswHighlight = {
    codeLine: codeLine,
    languages: Object.keys(PROFILES),
    maxLineLength: MAX_LINE,
  };
})(typeof window !== 'undefined' ? window : globalThis);
