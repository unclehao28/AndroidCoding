"""语义跳转服务测试。

语言服务用独立进程的 mock LSP（tests/mock_lsp_server.py）代替 clangd：
协议、握手、文档同步、超时、进程死亡都是真的，只有"符号解析结果"是构造的。
真机 clangd 与 fixtures/navigation-cpp 的 12 条预期由
scripts/verify-p2-navigation.py 在服务器上验证。
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import build_config
from app.lsp.manager import LanguageServerManager, LanguageServerSpec
from app.config import RootConfig
from app.lsp.manager import LanguageServerManager, detect_compile_commands, detect_query_driver
from app.main import create_app
from app.navigation import NavigationService
from conftest import REPO_ROOT, _deep_merge, default_raw

MOCK_SERVER = Path(__file__).resolve().parent / "mock_lsp_server.py"

MAIN_CPP = (
    '#include "state.h"\n'  # 0
    "int level = 3;\n"  # 1
    "// 中文注释：下面用 level 变量\n"  # 2
    "\n"  # 3
    "int useLevel(int level) {\n"  # 4
    "    return level + kDefault;\n"  # 5
    "}\n"  # 6
)

STATE_H = """#pragma once
const int kDefault = 8;
"""


class MockLanguageServerManager(LanguageServerManager):
    """把 clangd 换成 mock LSP，其余（进程管理、文档版本、上限）完全复用真实实现。"""

    def spec_for(self, language: str, workspace_id: str) -> LanguageServerSpec | None:
        if language not in ("c", "cpp"):
            return None
        return LanguageServerSpec(
            language=language,
            command=[sys.executable, str(MOCK_SERVER)],
            display_name="mock-lsp 1.0",
            source="测试替身（真机为 clangd）",
        )


@pytest.fixture
def cpp_tree(tmp_path: Path) -> Path:
    root = tmp_path / "cpp-root"
    root.mkdir()
    (root / "main.cpp").write_text(MAIN_CPP, encoding="utf-8")
    (root / "state.h").write_text(STATE_H, encoding="utf-8")
    (root / "ambiguous.cpp").write_text(MAIN_CPP, encoding="utf-8")
    (root / "empty.cpp").write_text(MAIN_CPP, encoding="utf-8")
    (root / "crash.cpp").write_text(MAIN_CPP, encoding="utf-8")
    (root / "cold.cpp").write_text(MAIN_CPP, encoding="utf-8")
    (root / "needsymbol.cpp").write_text(MAIN_CPP, encoding="utf-8")
    (root / "中文目录").mkdir()
    (root / "中文目录" / "带中文.cpp").write_text("int x = 1;\n", encoding="utf-8")
    return root


def build_app(tmp_path: Path, cpp_tree: Path, *, features=None, navigation=None):
    raw = _deep_merge(
        default_raw(cpp_tree),
        {
            "features": features or {"write": False, "navigation": True},
            "navigation": navigation or {"enabled": True},
        },
    )
    raw["roots"] = [{"id": "sample", "name": "C++ 样例", "path": str(cpp_tree), "readonly": True}]
    config = build_config(raw, source_path=tmp_path / "config.json")
    manager = MockLanguageServerManager(config.navigation, config.roots)
    service = NavigationService(config, manager)
    app = create_app(config, navigation_service=service)
    return app, config, manager


def sha256_of(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def navigate(client, path: str, line=0, character=0, kind="definition", version=None):
    body = {
        "workspace": "sample",
        "path": path,
        "kind": kind,
        "position": {"line": line, "character": character},
    }
    if version is not None:
        body["sourceVersion"] = version
    return client.post("/api/navigation", json=body)


def test_definition_is_resolved_from_language_server(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        response = navigate(client, "main.cpp", line=5, character=16)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "resolved"
        assert body["kind"] == "semantic"
        assert body["targetCount"] == 1
        target = body["targets"][0]
        assert target["path"] == "main.cpp"
        assert target["range"]["start"] == {"line": 1, "character": 8}
        assert target["range"]["unit"] == "utf-16"
        assert target["evidence"].startswith("L2:")
        assert body["server"]["name"] == "mock-lsp 1.0"
        assert body["positionUnit"] == "utf-16"
        assert body["symbol"] is None or isinstance(body["symbol"], str)
        assert body["sourceVersion"] is None  # 未带版本时不判定 stale


def test_retries_when_server_says_document_not_added(tmp_path, cpp_tree):
    """实测 clangd 12.0.7：刚 didOpen 后第一次 definition 会被拒（non-added document），重试即成功。"""
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "cold.cpp", line=5, character=16).json()
    assert body["status"] == "resolved"
    # 5:6 是 mock 只在"第二次请求"才返回的位置：拿到它就证明真的重试过
    assert body["targets"][0]["range"]["start"] == {"line": 5, "character": 6}


def test_first_open_sends_readiness_barrier(tmp_path, cpp_tree):
    """刚打开的文档先做一次 documentSymbol 就绪确认，否则第一次请求必然失败。"""
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "needsymbol.cpp", line=5, character=16).json()
    assert body["status"] == "resolved"
    assert body["documentWarmUp"] == "ok"


def make_aosp_like_tree(base: Path) -> Path:
    """造一棵带 Soong compdb 与自带工具链的"类 AOSP"树。"""
    tree = base / "aosp"
    compdb = tree / "out" / "soong" / "development" / "ide" / "compdb"
    compdb.mkdir(parents=True)
    (compdb / "compile_commands.json").write_text("[]", encoding="utf-8")
    toolchain = tree / "prebuilts" / "clang" / "host" / "linux-x86" / "clang-r416183b1" / "bin"
    toolchain.mkdir(parents=True)
    (toolchain / "clang++").write_text("", encoding="utf-8")
    return tree


class FakeClangdManager(LanguageServerManager):
    """只把 clangd 可执行文件固定住，其余发现逻辑走真实实现。"""

    def clangd(self):
        return "/usr/bin/clangd", "12.0.7", "测试替身"


def test_detect_compile_commands_and_query_driver(tmp_path):
    tree = make_aosp_like_tree(tmp_path)
    directory, note = detect_compile_commands(tree)
    assert directory is not None and directory.endswith("compdb")
    assert "自动探测" in note
    assert detect_query_driver(tree) is not None
    empty = tmp_path / "plain"
    empty.mkdir()
    assert detect_compile_commands(empty) == (None, "")
    assert detect_query_driver(empty) is None


def test_clangd_spec_uses_detected_compdb_and_query_driver(tmp_path, cpp_tree):
    """真实 AOSP 上跳转准不准的关键：自动带上 compdb 与 --query-driver。"""
    tree = make_aosp_like_tree(tmp_path)
    app, config, _manager = build_app(tmp_path, cpp_tree)
    root = RootConfig(id="aosp", name="AOSP", path=tree, readonly=True)
    manager = FakeClangdManager(config.navigation, [root])
    spec = manager.spec_for("cpp", "aosp")
    joined = " ".join(spec.command)
    assert "--compile-commands-dir=" in joined and "compdb" in joined
    assert "--query-driver=" in joined and "clang++" in joined

    info = manager.compile_commands_info()
    assert info["workspaces"][0]["dir"].endswith("compdb")
    assert "自动探测" in info["workspaces"][0]["source"]
    assert info["queryDriver"]


def test_clangd_spec_without_compdb_has_no_query_driver(tmp_path, cpp_tree):
    app, config, _manager = build_app(tmp_path, cpp_tree)
    root = RootConfig(id="plain", name="普通目录", path=cpp_tree, readonly=True)
    manager = FakeClangdManager(config.navigation, [root])
    joined = " ".join(manager.spec_for("cpp", "plain").command)
    assert "--compile-commands-dir=" not in joined
    assert "--query-driver=" not in joined
    info = manager.compile_commands_info()
    assert info["workspaces"][0]["dir"] is None
    assert "启发式" in info["workspaces"][0]["source"]
    assert "SOONG_GEN_COMPDB" in info["howToGenerate"]


def test_configured_compile_commands_dir_wins_over_detection(tmp_path, cpp_tree):
    tree = make_aosp_like_tree(tmp_path)
    explicit = tmp_path / "my-compdb"
    explicit.mkdir()
    (explicit / "compile_commands.json").write_text("[]", encoding="utf-8")
    app, config, _manager = build_app(
        tmp_path, cpp_tree, navigation={"enabled": True, "compileCommandsDir": str(explicit)}
    )
    root = RootConfig(id="aosp", name="AOSP", path=tree, readonly=True)
    manager = FakeClangdManager(config.navigation, [root])
    joined = " ".join(manager.spec_for("cpp", "aosp").command)
    assert str(explicit) in joined
    info = manager.compile_commands_info()
    assert info["workspaces"][0]["source"] == "配置"


def test_multiple_targets_are_ambiguous_not_silently_first(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "ambiguous.cpp", line=5, character=16).json()
        assert body["status"] == "ambiguous"
        assert body["targetCount"] == 2
        paths = sorted(target["path"] for target in body["targets"])
        assert paths == ["ambiguous.cpp", "ambiguous.h"] or paths == ["ambiguous.cpp", "ambiguous.cpp"]
        assert "请选择" in body["reason"] or "不会默认取第一个" in body["reason"]


def test_references_are_never_ambiguous(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "main.cpp", line=5, character=16, kind="references").json()
        assert body["status"] == "resolved"
        assert body["targetCount"] == 2
        assert all(target["kind"] == "reference" for target in body["targets"])


def test_empty_result_reports_unavailable_with_hint(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "empty.cpp", line=5, character=16).json()
        assert body["status"] == "unavailable"
        assert body["targets"] == []
        assert "没有返回目标" in body["reason"]
        assert any("compile_commands.json" in hint for hint in body["hints"])


def test_empty_references_are_a_normal_outcome(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "empty.cpp", line=5, character=16, kind="references").json()
        assert body["status"] == "resolved"
        assert body["targets"] == []
        assert "引用" in body["reason"]


def test_language_server_crash_is_reported_with_stderr(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "crash.cpp", line=5, character=16).json()
        assert body["status"] == "unavailable"
        assert "语言服务" in body["reason"]


def test_stale_source_version_is_detected(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "main.cpp", line=5, character=16, version="sha256:deadbeef").json()
        assert body["status"] == "stale"
        assert body["targets"] == []
        assert body["currentVersion"] == sha256_of(cpp_tree / "main.cpp")
        assert "刷新" in body["reason"]


def test_matching_source_version_proceeds(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "main.cpp", line=5, character=16, version=sha256_of(cpp_tree / "main.cpp")).json()
        assert body["status"] == "resolved"


def test_modified_file_is_detected_between_requests(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    version = sha256_of(cpp_tree / "main.cpp")
    with TestClient(app) as client:
        assert navigate(client, "main.cpp", line=5, character=16, version=version).json()["status"] == "resolved"
        (cpp_tree / "main.cpp").write_text(MAIN_CPP + "\nint extra = 1;\n", encoding="utf-8")
        body = navigate(client, "main.cpp", line=5, character=16, version=version).json()
        assert body["status"] == "stale"


def test_position_out_of_range_is_reported(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "main.cpp", line=9999, character=0).json()
        assert body["status"] == "unavailable"
        assert "超出文件范围" in body["reason"]


def test_java_without_jdtls_reports_what_is_missing(tmp_path, cpp_tree):
    """Java 已接入（走 JDT LS），但本机没装时要说清缺什么、怎么补，而不是含糊的"未接入"。"""
    (cpp_tree / "Demo.java").write_text("class Demo {}\n", encoding="utf-8")
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "Demo.java", line=0, character=6).json()
        assert body["status"] == "unavailable"
        assert "JDT LS" in body["reason"]
        hints = " ".join(body["hints"])
        assert "javaLsPath" in hints and "JDK" in hints
        status = client.get("/api/health").json()["navigation"]
        assert status["supported"]["java"]["available"] is False
        assert status["supported"]["java"]["reason"]
        assert status["supported"]["java"]["classpathSupport"] is False  # Soong 适配未完成要如实标注
        assert ".kt" in status["notImplemented"]  # 未接入的语言仍在清单里



def test_unknown_kind_is_rejected(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "main.cpp", kind="typeDefinition").json()
        assert body["status"] == "unavailable"
        assert "不支持的导航类型" in body["reason"]


def test_feature_flag_disables_navigation(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree, features={"write": False, "navigation": False})
    with TestClient(app) as client:
        body = navigate(client, "main.cpp", line=5, character=16).json()
        assert body["status"] == "unavailable"
        assert "关闭" in body["reason"]
        health = client.get("/api/health").json()
        assert health["navigation"]["enabled"] is False


def test_language_server_is_reused_across_requests(tmp_path, cpp_tree):
    """核心验收点：连续点击不能每次都新起一个语言服务进程。"""
    app, _config, manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        for _ in range(3):
            assert navigate(client, "main.cpp", line=5, character=16).json()["status"] == "resolved"
        assert manager.start_count == 1, f"语言服务被启动了 {manager.start_count} 次"
        status = client.get("/api/health").json()["navigation"]["manager"]
        assert len(status["instances"]) == 1
        instance = status["instances"][0]
        assert instance["requests"] == 3
        assert instance["openDocuments"] == 1


def test_utf16_position_with_non_bmp_text(tmp_path, cpp_tree):
    """非 BMP 字符会同时影响 UTF-16 列号与字符索引，这里确认服务端不会算错行。"""
    (cpp_tree / "emoji.cpp").write_text("// 😀 中文 setBrightness 调用\nint setBrightness = 0;\n", encoding="utf-8")
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "emoji.cpp", line=0, character=9).json()
        assert body["status"] == "resolved"
        assert body["symbolRange"]["unit"] == "utf-16"
        assert body["targets"][0]["path"] == "emoji.cpp"


def test_chinese_path_is_handled(tmp_path, cpp_tree):
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    with TestClient(app) as client:
        body = navigate(client, "中文目录/带中文.cpp", line=0, character=4).json()
        assert body["status"] == "resolved"
        assert body["targets"][0]["path"] == "中文目录/带中文.cpp"


def test_clangd_missing_is_reported_honestly(tmp_path, cpp_tree):
    """没有 clangd 时必须明确说不可用，而不是悄悄退回文本匹配。"""
    raw = _deep_merge(
        default_raw(cpp_tree),
        {
            "features": {"write": False, "navigation": True},
            "navigation": {"clangdPath": str(tmp_path / "no-such-clangd")},
        },
    )
    raw["roots"] = [{"id": "sample", "path": str(cpp_tree), "readonly": True}]
    config = build_config(raw, source_path=tmp_path / "config.json")
    app = create_app(config)
    with TestClient(app) as client:
        body = navigate(client, "main.cpp", line=5, character=16).json()
        assert body["status"] == "unavailable"
        assert "语言服务不可用" in body["reason"] or "clangdPath" in body["reason"]
        assert body["targets"] == []
        health = client.get("/api/health").json()
        assert health["navigation"]["supported"]["cpp"]["available"] is False


def test_navigation_target_outside_root_is_marked(tmp_path, cpp_tree):
    """语言服务可能返回工作区外的路径（系统头文件等），要标记出来而不是假装在根目录内。"""
    outside = tmp_path / "outside.h"
    outside.write_text("// 外部头文件\n", encoding="utf-8")
    app, _config, _manager = build_app(tmp_path, cpp_tree)
    # mock 用 uri.replace(".cpp", ".h") 生成第二个目标，这里手工验证路径映射逻辑
    service = app.state.navigation
    root = app.state.config.root("sample")
    targets = service._normalize_targets(
        [{"uri": outside.as_uri(), "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}}],
        root,
        "definition",
    )
    assert len(targets) == 1
    assert targets[0]["outsideRoot"] is True
    assert targets[0]["path"].endswith("outside.h")
