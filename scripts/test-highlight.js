/* 语法高亮器（prototype/highlight.js）的行为测试。
 *
 * 用法：node scripts/test-highlight.js      （scripts/check-package.py 会自动调用）
 *
 * 重点验证三件事：
 * 1. 分词正确（关键字/类型/注释/字符串/预处理/XML/bp/变量）；
 * 2. 跨行块注释状态正确；
 * 3. 输出只做着色，不改变可见文本，且任何语言里的标签都被转义（防注入）。
 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const SOURCE = path.join(ROOT, 'prototype', 'highlight.js');

global.window = {};
eval(fs.readFileSync(SOURCE, 'utf8'));
const HL = global.window.__aswHighlight;
if (!HL) {
  console.error('无法加载高亮模块');
  process.exit(1);
}

let failures = 0;
function check(name, condition, detail) {
  if (condition) {
    console.log('PASS ' + name);
  } else {
    failures += 1;
    console.log('FAIL ' + name + ' :: ' + detail);
  }
}

const state = {block: false};
const cpp = line => HL.codeLine(line, 'cpp', state);

// 1) C/C++ 分词
const include = cpp('#include <utils/Log.h>');
check('预处理指令整体着色', include.includes('cw-tok-preproc') && include.includes('utils/Log.h'), include);
check('预处理里的尖括号被转义', include.includes('&lt;utils/Log.h&gt;'), include);

const decl = cpp('static void Foo::bar(int level) {  // 注释');
check('关键字着色', decl.includes('<span class="cw-tok-keyword">static</span>'), decl);
check('类型着色', decl.includes('<span class="cw-tok-type">int</span>'), decl);
check('行注释着色到行尾', decl.includes('cw-tok-comment">// 注释</span>'), decl);
check('函数名着色', decl.includes('<span class="cw-tok-func">bar</span>'), decl);

const str = cpp('const char* s = "a\\"b"; int n = 0x1F;');
check('字符串整体着色（含转义引号）', str.includes('cw-tok-string">&quot;a\\&quot;b&quot;</span>'), str);
check('十六进制数字着色', str.includes('<span class="cw-tok-number">0x1F</span>'), str);

// 2) 跨行块注释
check('块注释起始行整行着色', cpp('/* 块注释开始').includes('cw-tok-comment'), 'open');
const inside = cpp('这行也在注释里 level */ int x = 1;');
check('块注释内的行整行着色', inside.startsWith('<span class="cw-tok-comment">'), inside);
check('注释结束后继续分词', inside.includes('<span class="cw-tok-type">int</span>'), inside);
check('块注释状态已复位', state.block === false, String(state.block));

// 3) 防注入
check('注释里的标签被转义', !cpp('int x = 1; // <script>alert(1)</script>').includes('<script>'), 'inject');
check('字符串里的标签被转义', !cpp('const char* a = "<img src=x>";').includes('<img'), 'inject');
const plain = HL.codeLine('plain <b>text</b>', 'unknown', {block: false});
check('未知语言也转义', !plain.includes('<b>') && plain.includes('&lt;b&gt;'), plain);

// 4) 其它语言
const xml = HL.codeLine('<service android:name=".Foo" >', 'xml', {block: false});
check('XML 标签名着色', xml.includes('<span class="cw-tok-tag">service</span>'), xml);
check('XML 属性名与值着色', xml.includes('cw-tok-attr">android:name') && xml.includes('cw-tok-string">&quot;.Foo&quot;'), xml);
check('XML 注释着色', HL.codeLine('<!-- 注释 --><x/>', 'xml', {block: false}).startsWith('<span class="cw-tok-comment">'), 'xml comment');
const bp = HL.codeLine('    name: "libfoo",  // 模块名', 'soong', {block: false});
check('bp 的无引号键着色', bp.includes('cw-tok-attr">name</span>'), bp);
check('bp 的值与注释着色', bp.includes('cw-tok-string">&quot;libfoo&quot;') && bp.includes('cw-tok-comment">// 模块名'), bp);
check('Makefile 变量着色', HL.codeLine('LOCAL_SRC_FILES := $(SRC) foo.c', 'make', {block: false}).includes('cw-tok-var">$(SRC)'), 'make');
check('Java 关键字着色', HL.codeLine('private static final int LEVEL = 42;', 'java', {block: false}).includes('cw-tok-keyword">private'), 'java');
check('Python 注释着色', HL.codeLine('def f(x):  # 注释', 'python', {block: false}).includes('cw-tok-comment"># 注释'), 'python');

// 5) 边界
check('空行返回空串', HL.codeLine('', 'cpp', {block: false}) === '', 'not empty');
const longLine = 'x'.repeat(HL.maxLineLength + 10);
check('超长行跳过着色（仅转义）', HL.codeLine(longLine, 'cpp', {block: false}) === longLine, 'highlighted');
const jsonLine = HL.codeLine('  "path": "a\\"b.txt",', 'json', {block: false});
check('JSON 键与带转义的值都正确', jsonLine.includes('cw-tok-attr">&quot;path&quot;') && jsonLine.includes('cw-tok-string">&quot;a\\&quot;b.txt&quot;'), jsonLine);

// 6) 着色不得改变可见文本
function visibleText(html) {
  return html.replace(/<[^>]+>/g, '')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}
const samples = [
  ['cpp', '#include <utils/Log.h>  // X'],
  ['cpp', 'if (a && b) { return "x<y>"; }'],
  ['java', 'List<String> xs = new ArrayList<>();'],
  ['xml', '<a b="c">text &amp; more</a>'],
  ['soong', 'cc_library { name: "libx", srcs: ["a.cpp"] }'],
];
let mismatch = 0;
for (const [lang, line] of samples) {
  const restored = visibleText(HL.codeLine(line, lang, {block: false}));
  if (restored !== line) {
    mismatch += 1;
    console.log('  文本被改动: ' + JSON.stringify(line) + ' → ' + JSON.stringify(restored));
  }
}
check('着色不改变可见文本', mismatch === 0, mismatch + ' 行不一致');

console.log(failures ? '\n高亮测试失败 ' + failures + ' 项' : '\n高亮测试全部通过');
process.exit(failures ? 1 : 0);
