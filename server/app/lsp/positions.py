"""位置坐标转换。

约定（必须与文档和接口一致）：
- 对外接口（`/api/navigation` 的 position、返回的 range）使用 **0 基行号 + 0 基 UTF-16 code unit 列号**，
  与 LSP 规定一致，也与浏览器 JS 字符串的索引方式一致（前端可以直接把点击位置传上来）。
- Python 字符串索引是 code point：中文是 BMP（1 个 code unit），emoji 等补充平面字符是 2 个 code unit。
  只有在非 BMP 字符附近，UTF-16 列号与 code point 列号才会不一致，所以需要显式转换。
- 检索结果里的 `column` 是 code point 偏移（文本匹配，与 LSP 无关），不要混用。
"""
from __future__ import annotations

IDENTIFIER_EXTRA = "_$"


def utf16_len(text: str) -> int:
    """Python 字符串在 UTF-16 下的长度（LSP 的计数方式）。"""
    total = 0
    for char in text:
        total += 2 if ord(char) > 0xFFFF else 1
    return total


def utf16_index_to_codepoint(text: str, utf16_offset: int) -> int:
    """把 UTF-16 偏移换算成 Python 字符串索引；越界收敛到边界。

    偏移落在代理对中间时返回该字符之前的索引（LSP 不建议这样切，但要能容错）。
    """
    if utf16_offset <= 0:
        return 0
    units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > utf16_offset:
            return index
        units += width
        if units == utf16_offset:
            return index + 1
    return len(text)


def codepoint_to_utf16(text: str, codepoint_index: int) -> int:
    """把 Python 字符串索引换算成 UTF-16 偏移。"""
    if codepoint_index <= 0:
        return 0
    return utf16_len(text[: min(codepoint_index, len(text))])


def split_lines(text: str) -> list[str]:
    """按 LSP 定义切行：\\r\\n、\\n、\\r 都是行分隔符，行内容不含分隔符。"""
    lines: list[str] = []
    current: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\r":
            if index + 1 < length and text[index + 1] == "\n":
                index += 1
            lines.append("".join(current))
            current = []
        elif char == "\n":
            lines.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    lines.append("".join(current))
    return lines


def column_to_codepoint(lines: list[str], line: int, utf16_column: int) -> int | None:
    """(行, UTF-16 列) → 行内 Python 字符索引；行号越界返回 None。"""
    if line < 0 or line >= len(lines) or utf16_column < 0:
        return None
    text = lines[line]
    return utf16_index_to_codepoint(text, min(utf16_column, utf16_len(text)))


def codepoint_column_to_utf16(lines: list[str], line: int, codepoint_column: int) -> int:
    """行内 Python 字符索引 → UTF-16 列。"""
    if line < 0 or line >= len(lines):
        return 0
    return codepoint_to_utf16(lines[line], codepoint_column)


def is_identifier_char(char: str) -> bool:
    return char.isalnum() or char in IDENTIFIER_EXTRA


def word_at(lines: list[str], line: int, character: int) -> dict | None:
    """取该位置上的标识符（按 UTF-16 列定位），返回文本与 UTF-16 列范围。

    只用于生成可读的 evidence 与"点了哪个词"的提示，不参与语义判断——
    语义由语言服务决定。
    """
    if line < 0 or line >= len(lines):
        return None
    text = lines[line]
    index = utf16_index_to_codepoint(text, character)
    if index >= len(text) or not is_identifier_char(text[index]):
        # 光标常在标识符右侧，向前回退一格再判断
        if index > 0 and is_identifier_char(text[index - 1]):
            index -= 1
        else:
            return None
    start = index
    end = index
    while start > 0 and is_identifier_char(text[start - 1]):
        start -= 1
    while end < len(text) and is_identifier_char(text[end]):
        end += 1
    if start == end:
        return None
    return {
        "word": text[start:end],
        "start": codepoint_to_utf16(text, start),
        "end": codepoint_to_utf16(text, end),
    }


def make_range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict:
    """构造对外返回的 range（0 基行号 + UTF-16 列，越界值收敛）。"""
    return {
        "start": {"line": max(0, start_line), "character": max(0, start_character)},
        "end": {"line": max(0, end_line), "character": max(0, end_character)},
        "unit": "utf-16",
    }


def range_to_text(lines: list[str], start_line: int, start_character: int, end_line: int, end_character: int) -> str:
    """把 LSP range 取出对应源码文本（处理跨行与 UTF-16 列）。"""
    if not lines or start_line < 0 or start_line >= len(lines):
        return ""
    last_line = min(end_line, len(lines) - 1)
    if start_line == last_line:
        text = lines[start_line]
        start = utf16_index_to_codepoint(text, start_character)
        end = utf16_index_to_codepoint(text, end_character)
        return text[start:end]
    parts = [lines[start_line][utf16_index_to_codepoint(lines[start_line], start_character) :]]
    for index in range(start_line + 1, last_line):
        parts.append(lines[index])
    tail_text = lines[last_line]
    parts.append(tail_text[: utf16_index_to_codepoint(tail_text, end_character)])
    return "\n".join(parts)
