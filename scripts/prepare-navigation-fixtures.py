"""Generate portable navigation expectations and local C++ compile commands.

This does not implement or test a language server. No compilation is performed.
Run: python scripts/prepare-navigation-fixtures.py
"""
from pathlib import Path
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'

cpp = 'navigation-cpp/'
java = 'navigation-java/src/demo/'
specs = [
    ('cpp-outer-before', cpp+'main.cpp', 'NAV_USE:outer-before', 'level', cpp+'main.cpp', 'NAV_DEF:outer', 'level'),
    ('cpp-inner-shadow', cpp+'main.cpp', 'NAV_USE:inner', 'level', cpp+'main.cpp', 'NAV_DEF:inner', 'level'),
    ('cpp-outer-after', cpp+'main.cpp', 'NAV_USE:outer-after', 'level', cpp+'main.cpp', 'NAV_DEF:outer', 'level'),
    ('cpp-parameter', cpp+'state.cpp', 'NAV_USE:parameter', 'brightness', cpp+'state.cpp', 'NAV_DEF:parameter', 'brightness'),
    ('cpp-member', cpp+'state.cpp', 'NAV_USE:member', 'brightness', cpp+'state.h', 'NAV_DEF:member', 'brightness'),
    ('cpp-global', cpp+'state.cpp', 'NAV_USE:global', 'gBrightness', cpp+'state.cpp', 'NAV_DEF:global', 'gBrightness'),
    ('cpp-constant', cpp+'main.cpp', 'NAV_USE:constant', 'kDefaultBrightness', cpp+'state.h', 'NAV_DEF:constant', 'kDefaultBrightness'),
    ('java-parameter', java+'ScopeDemo.java', 'NAV_USE:java-parameter', 'level', java+'ScopeDemo.java', 'NAV_DEF:java-parameter', 'level'),
    ('java-field', java+'ScopeDemo.java', 'NAV_USE:java-field', 'level', java+'ScopeDemo.java', 'NAV_DEF:java-field', 'level'),
    ('java-field-read', java+'ScopeDemo.java', 'NAV_USE:java-field-read', 'level', java+'ScopeDemo.java', 'NAV_DEF:java-field', 'level'),
    ('java-first-local', java+'ScopeDemo.java', 'NAV_USE:java-first-local', 'value', java+'ScopeDemo.java', 'NAV_DEF:java-first-local', 'value'),
    ('java-second-local', java+'ScopeDemo.java', 'NAV_USE:java-second-local', 'value', java+'ScopeDemo.java', 'NAV_DEF:java-second-local', 'value'),
]
# Match marker tokens exactly, so member is not confused with member-read.
def exact_position(path, marker, symbol):
    rows = (FIXTURES / path).read_text(encoding='utf-8').splitlines()
    matches = [(i, s) for i, s in enumerate(rows) if s.rstrip().endswith(marker)]
    if len(matches) != 1:
        raise ValueError(f'Expected unique exact marker {marker} in {path}')
    line, text = matches[0]
    column = len(text[:text.index(symbol)].encode('utf-16-le')) // 2
    return {'path': path, 'line': line, 'character': column, 'symbol': symbol}

cases=[]
for ident, source, source_marker, symbol, target, target_marker, target_symbol in specs:
    cases.append({'id': ident, 'method': 'textDocument/definition',
                  'from': exact_position(source, source_marker, symbol),
                  'expected': exact_position(target, target_marker, target_symbol)})
(FIXTURES / 'navigation-cases.json').write_text(json.dumps({
    'format': 'fixture-navigation-v1', 'coordinate_system': 'zero-based UTF-16',
    'note': 'Expectations only. No LSP navigation has been executed.', 'cases': cases
}, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

compiler = shutil.which('clang++') or shutil.which('g++') or 'clang++'
source_root = FIXTURES / 'navigation-cpp'
commands=[{'directory':str(source_root), 'file':str(p),
           'arguments':[compiler, '-std=c++17', '-I'+str(source_root), '-c', str(p)]}
          for p in sorted(source_root.glob('*.cpp'))]
destination=source_root/'compile_commands.json'
destination.write_text(json.dumps(commands, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'cases':len(cases), 'compile_commands':str(destination), 'compiler':compiler}))
