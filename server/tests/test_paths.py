"""路径边界与内容解码测试（P1 的真实风险点）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.errors import ApiError
from app.pathtools import (
    decode_source,
    detect_eol,
    detect_language,
    is_within,
    normalize_rel_path,
    resolve_under_root,
)


def test_is_within_does_not_accept_sibling_prefix(tmp_path):
    root = tmp_path / "root"
    sibling = tmp_path / "root2"
    root.mkdir()
    sibling.mkdir()
    assert is_within(root / "a.txt", root) is True
    assert is_within(root, root) is True
    assert is_within(sibling, root) is False
    assert is_within(sibling / "a.txt", root) is False
    assert is_within(tmp_path, root) is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("a/b/c.txt", "a/b/c.txt"),
        ("a//b/./c.txt", "a/b/c.txt"),
        ("/a/b.txt", "a/b.txt"),
        ("a\\b\\c.txt", "a/b/c.txt"),
        ("   ", ""),
    ],
)
def test_normalize_accepts_and_cleans(raw, expected):
    assert normalize_rel_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "../outside.txt",
        "a/../../etc/passwd",
        "a/..",
        "C:/Windows/system32/config/SAM",
        "//server/share/file.txt",
        "\\\\server\\share\\file.txt",
        "a/\x00b",
    ],
)
def test_normalize_rejects_escapes(raw):
    with pytest.raises(ApiError) as excinfo:
        normalize_rel_path(raw)
    assert excinfo.value.status_code == 400


def test_absolute_posix_like_path_stays_inside_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "etc").mkdir()
    (root / "etc" / "passwd").write_text("inside only\n", encoding="utf-8")
    resolved, rel = resolve_under_root(root, "/etc/passwd")
    assert resolved == (root / "etc" / "passwd").resolve()
    assert rel == "etc/passwd"


def test_resolve_rejects_target_outside_root(tmp_path, sample_tree):
    with pytest.raises(ApiError) as excinfo:
        resolve_under_root(sample_tree, "../outside.txt")
    assert excinfo.value.status_code == 400
    assert excinfo.value.code == "invalid_path"


def test_symlink_escape_is_rejected(tmp_path, sample_tree, symlink_escape):
    with pytest.raises(ApiError) as excinfo:
        resolve_under_root(sample_tree, "escape-link/secret.txt")
    assert excinfo.value.status_code == 403
    assert excinfo.value.code == "path_outside_root"


def test_internal_symlink_is_allowed(tmp_path, sample_tree):
    import os

    target = sample_tree / "data" / "notes.md"
    target.write_text("inner link target\n", encoding="utf-8")
    link = sample_tree / "inner-link.md"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("当前系统不支持创建符号链接")
    resolved, rel = resolve_under_root(sample_tree, "inner-link.md")
    assert rel == "inner-link.md"
    assert resolved == target.resolve()


def test_decode_utf8_and_bom_and_gbk_and_binary():
    assert decode_source("setBrightness\n".encode("utf-8")).lines == ("setBrightness",)
    bom = decode_source(b"\xef\xbb\xbfsetBrightness\n")
    assert bom.encoding == "utf-8-sig" and bom.lines == ("setBrightness",)
    gbk = decode_source("// 中文 setBrightness\n".encode("gbk"))
    assert gbk.ok and gbk.encoding == "gbk" and "中文" in gbk.lines[0]
    binary = decode_source(b"\x00\x01\x02abc")
    assert binary.status == "binary" and binary.lines == ()


def test_decode_empty_file_has_no_lines():
    decoded = decode_source(b"")
    assert decoded.ok and decoded.lines == ()


def test_decode_undecodable_bytes_falls_back_to_latin1_and_marks_lossy():
    decoded = decode_source(b"caf\xe9 setBrightness\n")
    assert decoded.ok and decoded.lossy is True and decoded.encoding == "latin-1"


def test_line_splitting_handles_crlf_and_trailing_newline():
    decoded = decode_source(b"a\r\nb\r\n")
    assert decoded.lines == ("a", "b")
    assert detect_eol(b"a\r\nb\r\n") == "crlf"
    assert detect_eol(b"a\nb\n") == "lf"
    assert detect_eol(b"a\nb\r\n") == "mixed"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("DisplayController.java", "java"),
        ("panel_backlight.c", "c"),
        ("state.h", "cpp-header"),
        ("IBrightness.aidl", "aidl"),
        ("Android.bp", "soong"),
        ("Android.mk", "make"),
        ("display.rc", "init"),
        ("display_config.xml", "xml"),
        ("unknown.zzz", "unknown"),
    ],
)
def test_language_detection(name, expected):
    assert detect_language(name) == expected


def test_resolve_returns_normalized_relative_path(sample_tree):
    resolved, rel = resolve_under_root(sample_tree, "docs/notes.md")
    assert isinstance(resolved, Path)
    assert rel == "docs/notes.md"
    assert resolved.is_file()
