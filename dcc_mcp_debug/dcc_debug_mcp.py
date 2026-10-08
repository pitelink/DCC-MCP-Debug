"""MCP (stdio) server，仅依赖标准库：
- dcc_exec 等：经常驻 dcc_service.py 在 DCC 里执行代码（见 dcc_exec_tools.py）
- dcc_debug_*：转发给常驻服务。真正连 DCC 的 debugpy、收集 print/stderr/异常的是常驻服务
  （见 dcc_dap.py）——会话放在常驻服务里，MCP 进程随会话 resume/重连被重建也不会丢调试连接和
  已收集的事件。常驻服务没有通往 MCP 客户端的 stdout，所以主动通知由这里的轮询线程转发。
"""
import atexit
import json
import os
import sys
import threading
import time

from . import dcc_client, dcc_connector, dcc_exec_tools
from .dcc_bridge import log_dir

LOG_PATH = os.path.join(log_dir(), "dcc_debug_mcp.log")


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


STDOUT_LOCK = threading.Lock()


def reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    with STDOUT_LOCK:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def notify(method, params=None):
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    with STDOUT_LOCK:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


PID_PROP = {"type": "integer", "description": "目标实例 PID；省略则用 dcc_select 选的当前实例"}

DEBUG_TOOLS = [
    {"name": "dcc_debug_connect",
     "description": "连接目标实例的 debugpy 调试端口并开始收集 print/stderr/异常。端口自动从该实例找，没有就自动启动。"
                    "dcc_select 和第一次对某实例 dcc_exec 时已经自动做过这一步，一般只在手动断开后重新接入时才需要调用。"
                    "调试会话保存在常驻服务里（跨 MCP 进程/会话重启存活）；同一个实例同一时间只能有一个调试客户端。",
     "inputSchema": {"type": "object", "properties": {"pid": PID_PROP, "dcc": {"type": "string"},
                                                    "port": {"type": "integer", "description": "手动指定端口，省略则自动查找"}}}},
    {"name": "dcc_debug_read",
     "description": "按游标读取目标实例已收集的事件(print/stderr/异常)。首次 since=0；之后用返回的 next_since 继续。wait_seconds>0 时没有新事件会阻塞等待(最多60秒)。"
                    "主要用途：捕获工具运行时产生的异步报错(比如点击界面按钮后的回调报错)或 print 调试信息。dcc_exec 自带的输出不会出现在这里。"
                    "only: all 全部 / stderr 仅 stderr 与调试器状态事件 / errors 额外包含 stdout 里含 Traceback/Error 的行。",
     "inputSchema": {"type": "object", "properties": {"pid": PID_PROP, "dcc": {"type": "string"},
                                                    "since": {"type": "integer", "default": 0}, "limit": {"type": "integer", "default": 200},
                                                    "only": {"type": "string", "enum": ["all", "stderr", "errors"], "default": "all"},
                                                    "wait_seconds": {"type": "number", "default": 0}}}},
    {"name": "dcc_debug_status",
     "description": "查看各实例的调试连接状态(端口、是否已连接、缓冲区大小)，以及哪个是当前操作的实例。",
     "inputSchema": {"type": "object", "properties": {"dcc": {"type": "string"}}}},
    {"name": "dcc_debug_clear", "description": "清空目标实例的事件缓冲区(游标继续递增)。",
     "inputSchema": {"type": "object", "properties": {"pid": PID_PROP, "dcc": {"type": "string"}}}},
    {"name": "dcc_debug_disconnect", "description": "断开目标实例的调试连接，释放它的调试端口给其它客户端(如 VSCode)。",
     "inputSchema": {"type": "object", "properties": {"pid": PID_PROP, "dcc": {"type": "string"}}}},
]
ACTION_TOOLS = dcc_exec_tools.TOOLS + dcc_connector.TOOLS
EXEC_HANDLERS = {t["name"]: t["handler"] for t in ACTION_TOOLS}
TOOLS = [{k: v for k, v in t.items() if k != "handler"} for t in ACTION_TOOLS] + DEBUG_TOOLS
TOOL_NAMES = {t["name"] for t in TOOLS}


def _debug(cmd, timeout, **fields):
    """转发一条调试命令给常驻服务（会话在那边）。"""
    req = {"cmd": cmd}
    req.update(fields)
    resp = dcc_exec_tools._service_request(req, timeout)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error"))
    return resp


def _sessions():
    return _debug("debug_status", 20).get("sessions", {})


NO_AUTO_DEBUG = set()   # 手动断开过调试的实例：不再自动重连，直到下次 dcc_select / dcc_debug_connect
_AUTO_FAILED = {}       # pid -> 上次自动接入失败的时间，60 秒内不重试，避免每次调用都白等超时
AUTO_RETRY_SECONDS = 60


def ensure_debug(tag, pid):
    """连接实例时自动启用调试：该实例没有 debugpy 端口就启动一个，再由常驻服务接入。
    失败不影响命令执行，只返回一句说明；已经接着的（会话在常驻服务里）直接跳过。"""
    if pid in NO_AUTO_DEBUG:
        return None
    try:
        if str(pid) in _sessions():
            return None
    except Exception:
        pass
    if time.time() - _AUTO_FAILED.get(pid, 0) < AUTO_RETRY_SECONDS:
        return None
    try:
        _, port, _note = dcc_exec_tools.dcc_start_debug_server({"dcc": tag, "pid": pid})
        _debug("debug_connect", 90, pid=pid, port=port)
        _AUTO_FAILED.pop(pid, None)
        _start_poller()
        return "调试已接入: 实例 PID %d, debugpy 端口 %d" % (pid, port)
    except Exception as e:
        _AUTO_FAILED[pid] = time.time()
        return "调试未接入 (%s: %s)，命令执行不受影响；可手动 dcc_debug_connect" % (type(e).__name__, e)


def _touch_instance(args):
    """dcc_exec 等命令第一次碰到某个实例时，顺带把调试接上。解析不出目标时不管，交给命令自己报错。"""
    try:
        tag, pid = dcc_exec_tools.resolve_target(args)
    except Exception:
        return
    ensure_debug(tag, pid)


_POLL_STOP = threading.Event()
_CURSORS = {}           # pid -> 已经通知到的事件游标
_poller = None


def _start_poller():
    """把常驻服务里新收集到的调试事件转成主动通知（notifications/message）。
    游标从接手时的当前位置起步，不重放旧事件。"""
    global _poller
    if _poller is not None and _poller.is_alive():
        return
    _poller = threading.Thread(target=_poll_loop, daemon=True)
    _poller.start()


def _poll_loop():
    while not _POLL_STOP.is_set():
        try:
            for key, info in _sessions().items():
                pid = int(key)
                if not info.get("connected"):
                    continue
                since = _CURSORS.setdefault(pid, info.get("next_index", 0))
                resp = _debug("debug_read", 40, pid=pid, since=since, limit=100, wait_seconds=0)
                for _i, kind, text in resp.get("events", []):
                    level = "error" if kind in ("output:stderr", "exception", "stopped", "disconnected") else "info"
                    notify("notifications/message", {"level": level,
                                                     "data": "[PID %d] %s" % (pid, text),
                                                     "logger": "DCC-MCP-Debug"})
                _CURSORS[pid] = resp.get("next_since", since)
        except Exception:
            pass
        _POLL_STOP.wait(1.5)


def call_tool(name, args):
    if name == "dcc_select":
        result = EXEC_HANDLERS[name](args)
        if getattr(result, "is_error", False) or args.get("pid") is None:
            return result
        pid = int(args["pid"])
        if args.get("debug") is False:
            NO_AUTO_DEBUG.add(pid)
            return result
        NO_AUTO_DEBUG.discard(pid)
        _AUTO_FAILED.pop(pid, None)
        tag = next((t for t, p in dcc_exec_tools.get_current().items() if p == pid), None)
        note = ensure_debug(tag, pid) if tag else None
        return result + ("\n" + note if note else "")
    if name in EXEC_HANDLERS:
        if name in ("dcc_exec", "dcc_reload_modules"):
            _touch_instance(args)
        return EXEC_HANDLERS[name](args)
    if name == "dcc_debug_connect":
        tag, pid = dcc_exec_tools.resolve_target(args)
        NO_AUTO_DEBUG.discard(pid)
        if args.get("port"):
            port = int(args["port"])
        else:
            port = dcc_exec_tools.find_debug_port(pid) or dcc_exec_tools.dcc_start_debug_server({"dcc": tag, "pid": pid})[1]
        text = _debug("debug_connect", 90, pid=pid, port=port)["text"]
        _CURSORS.pop(pid, None)
        _start_poller()
        return "实例 PID %d: %s" % (pid, text)
    if name == "dcc_debug_read":
        _, pid = dcc_exec_tools.resolve_target(args)
        resp = _debug("debug_read", 90, pid=pid, since=int(args.get("since", 0)),
                      limit=int(args.get("limit", 200)), only=args.get("only", "all"),
                      wait_seconds=min(float(args.get("wait_seconds", 0)), 60))
        return "[PID %d] " % pid + resp["text"]
    if name == "dcc_debug_status":
        tag = dcc_exec_tools.resolve_dcc(args.get("dcc"), fresh=True)
        cur = dcc_exec_tools.get_current().get(tag)
        sessions = _sessions()
        rows = []
        for item in dcc_exec_tools._instances(tag, fresh=True):
            rows.append({"pid": item["pid"], "version": item.get("version"), "current": item["pid"] == cur,
                         "debug_port": dcc_exec_tools.find_debug_port(item["pid"]),
                         "debug": sessions.get(str(item["pid"]))})
        return json.dumps({"dcc": tag, "instances": rows}, ensure_ascii=False, indent=1)
    if name == "dcc_debug_clear":
        _, pid = dcc_exec_tools.resolve_target(args)
        return "[PID %d] " % pid + _debug("debug_clear", 30, pid=pid)["text"]
    if name == "dcc_debug_disconnect":
        _, pid = dcc_exec_tools.resolve_target(args)
        _debug("debug_disconnect", 30, pid=pid)
        NO_AUTO_DEBUG.add(pid)
        return "实例 PID %d 的调试连接已断开（不会自动重连，dcc_select 或 dcc_debug_connect 可重新接入）" % pid
    raise KeyError(name)


def handle(msg):
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
        reply(msg_id, {"protocolVersion": version, "capabilities": {"tools": {}},
                       "serverInfo": {"name": "DCC-MCP-Debug", "version": "0.1.0"}})
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") not in TOOL_NAMES:
            reply(msg_id, error={"code": -32602, "message": "unknown tool: %s" % params.get("name")})
            return
        try:
            text = call_tool(params.get("name"), params.get("arguments") or {})
            is_error = getattr(text, "is_error", False)
        except Exception as e:
            log("tool %s failed: %r" % (params.get("name"), e))
            text, is_error = "%s: %s" % (type(e).__name__, e), True
        reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})
    else:
        reply(msg_id, error={"code": -32601, "message": "method not found: %s" % method})


def _release_on_exit():
    """本进程退出时，把常驻服务里属于它的当前实例选择和调试会话放掉——
    常驻服务不是永生的仓库，否则调试端口会被一直占着（VSCode 接不上），
    下一个会话还会继承上一个会话的连接状态。被强杀时来不及跑这里，由服务端定期扫描兜底。"""
    try:
        resp = dcc_client.release_client()
        if resp.get("released"):
            log("released on exit: %s" % resp["released"])
    except Exception:
        pass


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    atexit.register(_release_on_exit)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:
            log("bad message: %r" % e)


if __name__ == "__main__":
    main()
