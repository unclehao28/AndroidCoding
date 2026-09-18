"""测试用的最小 LSP 服务器（独立进程，只用标准库自实现分帧）。

故意不复用 app.lsp.protocol：它模拟的是"另一个进程/另一个实现"，
这样客户端的分帧、请求应答、通知、超时、进程死亡等路径才算真正被测到。

行为由请求的 URI 控制（便于测试里按需构造场景）：
- 普通 uri：definition 返回单个位置；references 返回两个位置
- 含 "ambiguous"：definition 返回两个不同文件的位置
- 含 "empty"：definition 返回 null
- 含 "slow"：延迟 5 秒再回复
- 含 "crash"：直接 os._exit(3)
- 含 "error"：返回 JSON-RPC error
initialize 之前会先向客户端发一个 workspace/configuration 请求，
客户端若不回复，initialize 会一直等——这正是 clangd 等真实服务器的行为。
"""
from __future__ import annotations

import json
import os
import sys
import time

PENDING_CONFIG_ID = 9001
_config_reply: dict | None = None


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    return json.loads(sys.stdin.buffer.read(length).decode("utf-8"))


def send(payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def log(text: str) -> None:
    sys.stderr.write(f"[mock-lsp] {text}\n")
    sys.stderr.flush()


def location(uri: str, line: int, character: int) -> dict:
    return {
        "uri": uri,
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": line, "character": character + 4},
        },
    }


def handle_definition(uri: str, params: dict):
    if "crash" in uri:
        log("crash requested")
        os._exit(3)
    if "error" in uri:
        return {"__error__": {"code": -32601, "message": "mock: method not found"}}
    if "slow" in uri:
        time.sleep(5)
    if "empty" in uri:
        return None
    if "ambiguous" in uri:
        return [location(uri, 3, 4), location(uri.replace(".cpp", ".h"), 7, 0)]
    return location(uri, 1, 8)


def handle_references(uri: str, params: dict):
    if "empty" in uri:
        return []
    return [location(uri, 1, 8), location(uri, 9, 12)]


def main() -> int:
    global _config_reply
    log("started")
    while True:
        message = read_message()
        if message is None:
            log("stdin closed")
            return 0
        method = message.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": PENDING_CONFIG_ID, "method": "workspace/configuration",
                  "params": {"items": [{"section": "clangd"}]}})
            deadline = time.monotonic() + 10
            while _config_reply is None and time.monotonic() < deadline:
                reply = read_message()
                if reply is None:
                    return 0
                if reply.get("id") == PENDING_CONFIG_ID:
                    _config_reply = reply
            if _config_reply is None:
                log("client never answered workspace/configuration")
                return 4
            send({"jsonrpc": "2.0", "id": message["id"], "result": {
                "capabilities": {"textDocumentSync": 1, "definitionProvider": True, "referencesProvider": True},
                "serverInfo": {"name": "mock-lsp", "version": "1"},
            }})
            send({"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 3, "message": "mock ready"}})
            continue
        if method == "shutdown":
            send({"jsonrpc": "2.0", "id": message["id"], "result": None})
            continue
        if method == "exit":
            log("exit")
            return 0
        if method == "textDocument/definition":
            params = message.get("params") or {}
            uri = (params.get("textDocument") or {}).get("uri", "")
            result = handle_definition(uri, params)
            if isinstance(result, dict) and "__error__" in result:
                send({"jsonrpc": "2.0", "id": message["id"], "error": result["__error__"]})
            else:
                send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            continue
        if method == "textDocument/references":
            params = message.get("params") or {}
            uri = (params.get("textDocument") or {}).get("uri", "")
            send({"jsonrpc": "2.0", "id": message["id"], "result": handle_references(uri, params)})
            continue
        if "id" in message:
            send({"jsonrpc": "2.0", "id": message["id"], "result": None})
        else:
            log(f"ignoring notification {method}")


if __name__ == "__main__":
    raise SystemExit(main())
