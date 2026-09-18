"""LSP 基础协议：stdio 上的 `Content-Length` 分帧 + JSON-RPC 2.0。

只实现我们真正需要的最小集合，但对以下情况保持宽容（真实服务器行为各异）：
- 头部大小写不敏感、允许出现 `Content-Type` 等额外头部；
- 增量读取（一次 read 可能拿到半个消息或多个消息）；
- 非 UTF-8 或非法 JSON 时抛出明确异常而不是静默丢弃。
"""
from __future__ import annotations

import json

HEADER_SEPARATOR = b"\r\n\r\n"


class LspProtocolError(Exception):
    """语言服务返回了无法解析的内容。"""


def encode_message(payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


class _Incomplete(Exception):
    pass


def _parse_one(buffer: bytes) -> tuple[dict, bytes]:
    index = buffer.find(HEADER_SEPARATOR)
    if index < 0:
        raise _Incomplete
    header_block = buffer[:index].decode("ascii", "replace")
    length: int | None = None
    for line in header_block.split("\r\n"):
        if not line.strip():
            continue
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise LspProtocolError(f"Content-Length 不是整数：{value!r}") from exc
    if length is None:
        raise LspProtocolError(f"缺少 Content-Length 头部：{header_block!r}")
    body_start = index + len(HEADER_SEPARATOR)
    body_end = body_start + length
    if len(buffer) < body_end:
        raise _Incomplete
    body = buffer[body_start:body_end]
    try:
        message = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LspProtocolError(f"消息体不是合法 JSON：{exc}") from exc
    if not isinstance(message, dict):
        raise LspProtocolError("消息体必须是 JSON 对象")
    return message, buffer[body_end:]


class MessageBuffer:
    """增量解析器：喂入字节，吐出完整的 JSON-RPC 消息。"""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> list[dict]:
        self._buffer += data
        messages: list[dict] = []
        while True:
            try:
                message, rest = _parse_one(self._buffer)
            except _Incomplete:
                break
            messages.append(message)
            self._buffer = rest
        return messages

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)
