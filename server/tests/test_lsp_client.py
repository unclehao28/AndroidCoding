"""LSP 客户端与位置转换测试。

客户端用一个独立进程的 mock LSP 服务器验证：分帧、服务端请求应答、通知、
每个请求独立超时、进程异常退出时把等待中的请求立刻失败并带出 stderr。
真机 clangd 的行为由 scripts/verify-p2-navigation.py 在服务器上验证。

注意：语言服务子进程与其 transport 绑定在创建它的 event loop 上，
所以每个测试必须在**同一个 asyncio.run** 里完成一个客户端的全部交互。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.lsp import LspClient, LspError, LspProcessError, LspTimeoutError
from app.lsp.positions import (
    codepoint_to_utf16,
    column_to_codepoint,
    range_to_text,
    split_lines,
    utf16_index_to_codepoint,
    utf16_len,
    word_at,
)

MOCK_SERVER = Path(__file__).resolve().parent / "mock_lsp_server.py"
URI = "file:///tmp/fixtures/main.cpp"


async def start_client(cwd: Path, name: str = "mock-lsp") -> LspClient:
    client = LspClient([sys.executable, str(MOCK_SERVER)], cwd=cwd, name=name, stderr_limit=50)
    await client.start()
    result = await client.request("initialize", {"processId": None, "capabilities": {}}, timeout=20)
    assert result["serverInfo"]["name"] == "mock-lsp"
    client.notify("initialized", {})
    return client


# ---------------------------------------------------------------- 位置转换


@pytest.mark.parametrize(
    "text,expected",
    [
        ("abc", 3),
        ("中文注释", 4),  # 中文是 BMP，每个字符 1 个 UTF-16 单元
        ("😀", 2),  # 补充平面字符占 2 个单元
        ("a😀b", 4),
        ("中文😀", 4),
    ],
)
def test_utf16_length(text, expected):
    assert utf16_len(text) == expected


@pytest.mark.parametrize(
    "text,character,expected",
    [
        ("const int level = 1;", 10, 10),  # 纯 ASCII：两种计数一致
        ("// 中文 level", 8, 8),  # 中文是 BMP：UTF-16 列号 == 字符索引
        ("// 😀中文 level", 8, 7),  # emoji 多占 1 个单元，因此字符索引比列号小 1
    ],
)
def test_utf16_to_codepoint(text, character, expected):
    assert utf16_index_to_codepoint(text, character) == expected
    assert codepoint_to_utf16(text, expected) == character


def test_utf16_roundtrip_is_monotonic_for_non_bmp():
    text = "int 前😀后 = 1;"
    previous = -1
    for character in range(utf16_len(text) + 1):
        index = utf16_index_to_codepoint(text, character)
        assert index >= previous
        previous = index
        assert codepoint_to_utf16(text, index) <= character


def test_split_lines_handles_all_terminators():
    assert split_lines("a\nb\r\nc\rd") == ["a", "b", "c", "d"]
    assert split_lines("a\n") == ["a", ""]


def test_word_at_uses_utf16_columns():
    # "// 😀 setBrightness(level);"：emoji 占 2 个单元，"setBrightness" 起于列 6、止于列 19
    lines = split_lines("// 😀 setBrightness(level);")
    found = word_at(lines, 0, 12)
    assert found is not None
    assert found["word"] == "setBrightness"
    assert (found["start"], found["end"]) == (6, 19)
    assert word_at(lines, 0, 3) is None  # 落在 emoji 上，不是标识符
    assert word_at(lines, 0, 100) is None  # 越界不报错，只是取不到词


def test_range_to_text_across_lines():
    lines = split_lines("int a = 1;\nint b = 2;\nint c = 3;")
    assert range_to_text(lines, 0, 4, 0, 5) == "a"
    assert range_to_text(lines, 0, 11, 2, 6) == "\nint b = 2;\nint c "
    assert column_to_codepoint(lines, 1, 4) == 4
    assert range_to_text(lines, 99, 0, 99, 1) == ""


# ---------------------------------------------------------------- 客户端


def test_initialize_and_definition(tmp_path):
    async def scenario():
        client = await start_client(tmp_path)
        try:
            result = await client.request(
                "textDocument/definition", {"textDocument": {"uri": URI}, "position": {"line": 5, "character": 0}}
            )
            assert result["range"]["start"]["line"] == 1
            assert client.alive is True
            assert list(client.log_messages) == ["mock ready"]
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_definition_can_return_multiple_locations(tmp_path):
    async def scenario():
        client = await start_client(tmp_path)
        try:
            result = await client.request(
                "textDocument/definition",
                {"textDocument": {"uri": URI.replace(".cpp", "ambiguous.cpp")}},
            )
            assert isinstance(result, list) and len(result) == 2
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_empty_and_error_results_are_distinguishable(tmp_path):
    async def scenario():
        client = await start_client(tmp_path)
        try:
            empty = await client.request(
                "textDocument/definition", {"textDocument": {"uri": URI.replace(".cpp", "empty.cpp")}}
            )
            assert empty is None
            with pytest.raises(LspError) as excinfo:
                await client.request(
                    "textDocument/definition", {"textDocument": {"uri": URI.replace(".cpp", "error.cpp")}}
                )
            assert "method not found" in str(excinfo.value)
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_request_timeout_does_not_kill_server(tmp_path):
    async def scenario():
        client = await start_client(tmp_path)
        try:
            with pytest.raises(LspTimeoutError):
                await client.request(
                    "textDocument/definition",
                    {"textDocument": {"uri": URI.replace(".cpp", "slow.cpp")}},
                    timeout=0.4,
                )
            assert client.alive is True
            result = await client.request("textDocument/definition", {"textDocument": {"uri": URI}})
            assert result["range"]["start"]["line"] == 1
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_process_death_fails_pending_requests_with_stderr(tmp_path):
    async def scenario():
        client = await start_client(tmp_path, name="mock-lsp-crash")
        try:
            with pytest.raises(LspProcessError) as excinfo:
                await client.request(
                    "textDocument/definition",
                    {"textDocument": {"uri": URI.replace(".cpp", "crash.cpp")}},
                    timeout=20,
                )
            message = str(excinfo.value)
            assert "mock-lsp-crash" in message
            assert "crash requested" in message  # stderr 尾部被带出来，便于定位
            assert client.alive is False
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_references_round_trip(tmp_path):
    async def scenario():
        client = await start_client(tmp_path)
        try:
            result = await client.request(
                "textDocument/references",
                {"textDocument": {"uri": URI}, "context": {"includeDeclaration": True}},
            )
            assert len(result) == 2
            assert result[1]["range"]["start"]["line"] == 9
        finally:
            await client.shutdown()

    asyncio.run(scenario())
