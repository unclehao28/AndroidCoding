"""检查包内资源与夹具的一致性。

说明（P1）：
- 前端已拆分为 styles.css / demo-data.js / api.js / app.js，全部为本地资源，无 CDN 依赖；
- 内置示例数据必须与 fixtures/demo-files.json、fixtures/demo-tree 保持一致；
- 本脚本只做静态与数据一致性检查，不代表后端接口或浏览器交互已经验收。
"""
from pathlib import Path
from html.parser import HTMLParser
import json
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PROTOTYPE = ROOT / 'prototype'
report = {}

class Resources(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'script' and values.get('src'):
            self.refs.append(values['src'])
        if tag == 'link' and values.get('rel') == 'stylesheet':
            self.refs.append(values['href'])

parser = Resources()
parser.feed((PROTOTYPE / 'index.html').read_text(encoding='utf-8'))
assert 'app.js' in parser.refs, parser.refs
for ref in parser.refs:
    assert not ref.startswith(('http://', 'https://', '//')), f'不允许外部资源：{ref}'
    assert (PROTOTYPE / ref).is_file(), f'缺少本地资源：{ref}'
report['local_assets'] = 'passed: ' + ', '.join(parser.refs)

node = shutil.which('node')
if node:
    for script in sorted(PROTOTYPE.glob('*.js')):
        subprocess.run([node, '--check', str(script)], check=True)
    report['javascript_syntax'] = 'passed: ' + ', '.join(sorted(p.name for p in PROTOTYPE.glob('*.js')))

    loader = (
        "global.window={};"
        f"require({json.dumps(str(PROTOTYPE / 'demo-data.js'))});"
        "process.stdout.write(JSON.stringify(window.ASW_DEMO.files));"
    )
    completed = subprocess.run([node, '-e', loader], check=True, capture_output=True)
    builtin = json.loads(completed.stdout.decode('utf-8'))
    manifest = json.loads((ROOT / 'fixtures/demo-files.json').read_text(encoding='utf-8'))
    assert builtin == manifest, '内置示例数据与 fixtures/demo-files.json 不一致'
    report['demo_data_consistency'] = f'passed: {len(builtin)} 条内置示例与清单一致'

    # 语法高亮器是自研的（不引入 CDN）：分词、跨行块注释、防注入、不改动可见文本都要有测试
    highlight_test = ROOT / 'scripts' / 'test-highlight.js'
    completed = subprocess.run([node, str(highlight_test)], capture_output=True)
    output = completed.stdout.decode('utf-8', 'replace')
    passed = output.count('PASS ')
    if completed.returncode != 0:
        print(output)
        raise AssertionError('语法高亮测试失败：node scripts/test-highlight.js')
    report['highlight_behavior'] = f'passed: {passed} 项（含防注入与"着色不改变可见文本"）'
else:
    report['javascript_syntax'] = 'not run: node unavailable'
    report['highlight_behavior'] = 'not run: node unavailable'

demo = json.loads((ROOT / 'fixtures/demo-files.json').read_text(encoding='utf-8'))
for item in demo:
    disk = (ROOT / 'fixtures/demo-tree' / item['path']).read_text(encoding='utf-8')
    assert disk == '\n'.join(item['lines']) + '\n', item['path']
report['demo_files_on_disk'] = f'passed: {len(demo)} 个示例文件与清单一致'

cases = json.loads((ROOT / 'fixtures/navigation-cases.json').read_text(encoding='utf-8'))['cases']
assert len({case['id'] for case in cases}) == len(cases)
for case in cases:
    for key in ['from', 'expected']:
        loc = case[key]
        line = (ROOT / 'fixtures' / loc['path']).read_text(encoding='utf-8').splitlines()[loc['line']]
        suffix = line.encode('utf-16-le')[loc['character'] * 2:].decode('utf-16-le')
        assert suffix.startswith(loc['symbol']), (case['id'], key, loc)
report['navigation_expected_positions'] = f'passed: {len(cases)} 条预期；实际 LSP 未执行（属于 P2）'

cpp = shutil.which('g++') or shutil.which('clang++')
if cpp:
    sources = sorted((ROOT / 'fixtures/navigation-cpp').glob('*.cpp'))
    subprocess.run([cpp, '-std=c++17', '-fsyntax-only', *map(str, sources)], check=True)
    report['cpp_fixture_syntax'] = 'passed'
else:
    report['cpp_fixture_syntax'] = 'not run: C++ compiler unavailable'

server = ROOT / 'server'
report['backend_tests'] = 'not run by this script: cd server && python -m pytest'
report['browser_interaction'] = 'not run by this script'
report['actual_aosp'] = 'not available'
assert (server / 'app' / 'main.py').is_file(), '缺少后端入口 server/app/main.py'
report['server_files'] = 'passed: configuration + FastAPI app present'
print(json.dumps(report, ensure_ascii=False, indent=2))
