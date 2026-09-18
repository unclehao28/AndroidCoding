"""测试夹具：临时源码树 + 真实配置对象 + FastAPI 测试客户端。

测试针对真实文件与真实路径解析，不使用内置演示数据。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from app.config import build_config  # noqa: E402
from app.main import create_app  # noqa: E402

JAVA_SOURCE = """package demo.display;

public final class DisplayController {
    private int lastBrightness = -1;

    public void setBrightness(int level) {
        int safeLevel = Math.max(0, level);
        if (safeLevel == lastBrightness) {
            return;
        }
        nativeSetBrightness(safeLevel);
        lastBrightness = safeLevel;
    }
}
"""

CPP_SOURCE = """#include "DisplayService.h"

void DisplayService::setBrightness(int level) {
    if (brightnessHal == nullptr) {
        return;
    }
    brightnessHal->setBrightness(level);
}
"""


def _write(root: Path, rel: str, data: bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    root = tmp_path / "sample-root"
    _write(root, "frameworks/base/core/java/demo/DisplayController.java", JAVA_SOURCE.encode("utf-8"))
    _write(root, "frameworks/native/DisplayService.cpp", CPP_SOURCE.encode("utf-8"))
    _write(root, "docs/notes.md", "# 说明\n调用 setBrightness 时注意范围\n".encode("utf-8"))
    _write(root, "out/gen/Foo.java", "// generated\nsetBrightness\n".encode("utf-8"))
    _write(root, ".hidden/secret.txt", "setBrightness\n".encode("utf-8"))
    _write(root, "data/binary.bin", b"\x00\x01\x02setBrightness\x00\xff")
    _write(root, "data/gbk.txt", "// 中文注释 setBrightness\n".encode("gbk"))
    _write(root, "data/utf8bom.txt", b"\xef\xbb\xbfsetBrightness\n")
    _write(root, "data/empty.txt", b"")
    _write(root, "data/中文目录/说明.txt", "setBrightness 在中文路径下\n".encode("utf-8"))
    _write(root, "data/huge.txt", ("x" * 100 + "\n").encode("utf-8") * 60)
    many = "".join(f"line {index} level\n" for index in range(1, 51))
    _write(root, "data/many_lines.txt", many.encode("utf-8"))
    return root


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def default_raw(root: Path) -> dict:
    return {
        "server": {"host": "127.0.0.1", "port": 8765, "corsOrigins": ["*"]},
        "roots": [{"id": "sample", "name": "样例源码", "path": str(root), "readonly": True}],
        "limits": {},
        "search": {},
        "features": {"write": False, "navigation": False},
        "prototypeDir": str(REPO_ROOT / "prototype"),
    }


@pytest.fixture
def make_config(tmp_path: Path, sample_tree: Path):
    def _make(**overrides):
        raw = _deep_merge(default_raw(sample_tree), overrides)
        return build_config(raw, source_path=tmp_path / "config.json")

    return _make


@pytest.fixture
def config(make_config):
    return make_config()


@pytest.fixture
def client(config):
    from fastapi.testclient import TestClient

    with TestClient(create_app(config)) as test_client:
        yield test_client


@pytest.fixture
def make_client(tmp_path: Path, sample_tree: Path):
    def _make(**overrides):
        from fastapi.testclient import TestClient

        raw = _deep_merge(default_raw(sample_tree), overrides)
        config = build_config(raw, source_path=tmp_path / "config.json")
        return TestClient(create_app(config))

    return _make


@pytest.fixture
def symlink_escape(tmp_path: Path, sample_tree: Path) -> Path:
    """在根目录内创建指向根目录外的符号链接；Windows 无权限时跳过。"""
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("outside secret setBrightness\n", encoding="utf-8")
    link = sample_tree / "escape-link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前系统不支持创建符号链接（Windows 需要开发者模式或管理员权限）")
    return link
