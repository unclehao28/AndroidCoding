"""配置校验测试：字段错误必须让启动失败并给出可读原因。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import ConfigError, build_config, load_config, resolve_config_path
from conftest import REPO_ROOT, SERVER_DIR, default_raw


def test_example_config_is_valid_and_points_at_real_dirs():
    config = load_config(SERVER_DIR / "config.example.json")
    assert {root.id for root in config.roots} == {"demo", "fixtures"}
    assert all(root.path.is_dir() for root in config.roots)
    assert config.features.write is False
    assert config.features.navigation is False
    assert config.server.host == "127.0.0.1"


def test_relative_root_paths_resolve_against_config_location():
    config = load_config(SERVER_DIR / "config.example.json")
    demo_root = config.root("demo")
    assert demo_root is not None
    assert demo_root.path == (REPO_ROOT / "fixtures/demo-tree").resolve()


def test_valid_config_builds(tmp_path, sample_tree):
    config = build_config(default_raw(sample_tree), source_path=tmp_path / "config.json")
    root = config.root("sample")
    assert root is not None and root.path == sample_tree.resolve()
    assert config.limits.max_file_bytes == 2 * 1024 * 1024


def test_unknown_top_level_field_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limitsx"] = {}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("未知字段" in message for message in excinfo.value.messages)


def test_unknown_section_field_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limits"] = {"maxFileBytez": 1024}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("maxFileBytez" in message for message in excinfo.value.messages)


def test_missing_root_path_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["roots"] = [{"id": "gone", "path": str(tmp_path / "not-here")}]
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("不存在" in message for message in excinfo.value.messages)


def test_root_path_must_be_directory(tmp_path, sample_tree):
    file_path = tmp_path / "afile.txt"
    file_path.write_text("x", encoding="utf-8")
    raw = default_raw(sample_tree)
    raw["roots"] = [{"id": "afile", "path": str(file_path)}]
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("不是目录" in message for message in excinfo.value.messages)


def test_duplicate_root_id_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["roots"].append({"id": "sample", "path": str(sample_tree)})
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("重复" in message for message in excinfo.value.messages)


def test_limits_out_of_range_rejected(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limits"] = {"maxSearchResults": 999999, "rgThreads": 0}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    messages = " ".join(excinfo.value.messages)
    assert "maxSearchResults" in messages and "rgThreads" in messages


def test_write_and_navigation_must_stay_disabled(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["features"] = {"write": True, "navigation": True}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    messages = " ".join(excinfo.value.messages)
    assert "features.write" in messages and "features.navigation" in messages


def test_search_engine_value_checked(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["search"] = {"engine": "zoekt"}
    with pytest.raises(ConfigError) as excinfo:
        build_config(raw, source_path=tmp_path / "config.json")
    assert any("search.engine" in message for message in excinfo.value.messages)


def test_non_loopback_host_is_warning_not_error(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["server"] = {"host": "0.0.0.0", "port": 8765}
    config = build_config(raw, source_path=tmp_path / "config.json")
    assert config.server.host == "0.0.0.0"
    assert any("回环" in warning for warning in config.warnings)


def test_search_limit_larger_than_read_limit_is_only_a_warning(tmp_path, sample_tree):
    raw = default_raw(sample_tree)
    raw["limits"] = {"maxFileBytes": 4096}
    config = build_config(raw, source_path=tmp_path / "config.json")
    assert config.limits.max_file_bytes == 4096
    assert any("maxSearchFileBytes" in warning for warning in config.warnings)


def test_invalid_json_reports_location(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"roots": [}', encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert any("JSON" in message for message in excinfo.value.messages)


def test_config_path_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    explicit.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ASW_CONFIG", str(tmp_path / "from-env.json"))
    assert resolve_config_path(str(explicit)) == explicit
    assert resolve_config_path(None) == Path(tmp_path / "from-env.json")
    monkeypatch.delenv("ASW_CONFIG")
    assert resolve_config_path(None).name in ("config.json", "config.example.json")


def test_public_config_hides_nothing_but_marks_features_disabled(config):
    public = config.to_public()
    assert public["features"] == {"write": False, "navigation": False}
    assert public["search"]["engine"] == "auto"
    assert public["limits"]["maxSearchResults"] == 500
    json.dumps(public)  # 必须可序列化
