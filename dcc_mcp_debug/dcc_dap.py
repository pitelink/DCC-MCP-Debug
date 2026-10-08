"""DAP 客户端：连 DCC 的 debugpy，收集 print/stderr/异常。

原来住在 MCP 进程里，但 MCP 进程会随会话 restart/resume 被重建，内存里的会话就没了；
现在由常驻服务 dcc_service 持有（它跨会话存活），MCP 只是转发。
"""
import collections
import json
import socket
import threading
import time

from .dcc_bridge import log_dir

import os as _os

DEFAULT_PORT = 4345
MAX_EVENTS = 5000
_LOG_PATH = _os.path.join(log_dir(), "dcc_debug_mcp.log")


def log(msg):
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg))
    except OSError:
        pass


class DebugSession:
    def __init__(self, pid=None):
        self.pid = pid
        self.cond = threading.Condition()
        self.send_lock = threading.Lock()
        self.events = collections.deque(maxlen=MAX_EVENTS)
        self.next_index = 0
        self.partial = {}
        self.responses = {}
        self.initialized = threading.Event()
        self.sock = None
        self.seq = 0
        self.connected = False
        self.port = None
        self.connected_at = None
        self.last_error = None

    def _add(self, kind, text):
        with self.cond:
            self.events.append({"i": self.next_index, "t": time.time(), "kind": kind, "text": text})
            self.next_index += 1
            self.cond.notify_all()

    def _add_output(self, category, text):
        buf = self.partial.get(category, "") + text
        lines = buf.split("\n")
        self.partial[category] = lines.pop()
        for line in lines:
            self._add("output:" + category, line.rstrip("\r"))

    def _send(self, command, arguments=None, sock=None):
        with self.send_lock:
            self.seq += 1
            seq = self.seq
            msg = {"seq": seq, "type": "request", "command": command}
            if arguments is not None:
                msg["arguments"] = arguments
            body = json.dumps(msg).encode("utf-8")
            (sock or self.sock).sendall(("Content-Length: %d\r\n\r\n" % len(body)).encode("ascii") + body)
            return seq

    def _wait(self, seq, timeout):
        deadline = time.time() + timeout
        with self.cond:
            while seq not in self.responses:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)
            return self.responses.pop(seq)

    def _peek(self, seq):
        """看一眼某个请求的响应在不在（不弹出），用于失败时把真实原因带出来。"""
        with self.cond:
            return self.responses.get(seq)

    def _dispatch(self, msg, sock):
        kind = msg.get("type")
        if kind == "response":
            with self.cond:
                self.responses[msg["request_seq"]] = msg
                self.cond.notify_all()
        elif kind == "event":
            name, body = msg.get("event"), msg.get("body") or {}
            if name == "output":
                category = body.get("category", "console")
                if category != "telemetry":
                    self._add_output(category, body.get("output", ""))
            elif name == "initialized":
                self.initialized.set()
            elif name == "stopped":
                self._add("stopped", json.dumps(body, ensure_ascii=False))
                # 不让 MotionBuilder 被调试器暂停住
                try:
                    self._send("continue", {"threadId": body.get("threadId", 1)}, sock)
                except OSError:
                    pass
            elif name in ("exited", "terminated", "exception"):
                self._add(name, json.dumps(body, ensure_ascii=False))
        elif kind == "request":
            try:
                reply = {"seq": 0, "type": "response", "request_seq": msg.get("seq"), "success": False,
                         "command": msg.get("command"), "message": "unsupported"}
                data = json.dumps(reply).encode("utf-8")
                with self.send_lock:
                    sock.sendall(("Content-Length: %d\r\n\r\n" % len(data)).encode("ascii") + data)
            except OSError:
                pass

    def _read_loop(self, sock, f):
        try:
            while True:
                length = None
                while True:
                    line = f.readline()
                    if not line:
                        raise EOFError("debugpy closed the connection")
                    line = line.strip()
                    if not line:
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1])
                self._dispatch(json.loads(f.read(length).decode("utf-8")), sock)
        except Exception as e:
            log("read loop ended: %r" % e)
            if self.sock is sock:
                self.last_error = repr(e)
        finally:
            if self.sock is sock:
                self.connected = False
                self._add("disconnected", self.last_error or "")

    def connect(self, port=DEFAULT_PORT, timeout=15):
        if self.connected:
            return "already connected (port %s)" % self.port
        self.disconnect()
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        sock.settimeout(None)
        self.sock, self.port, self.last_error = sock, port, None
        self.initialized.clear()
        self.responses.clear()
        threading.Thread(target=self._read_loop, args=(sock, sock.makefile("rb")), daemon=True).start()
        self.connected, self.connected_at = True, time.time()
        try:
            resp = self._wait(self._send("initialize", {"clientID": "claude-mcp", "adapterID": "debugpy", "pathFormat": "path",
                                                         "linesStartAt1": True, "columnsStartAt1": True}), timeout)
            if not resp or not resp.get("success"):
                raise RuntimeError("initialize failed: %s" % resp)
            # attach 必须带完整参数，空参数 debugpy 会报 missing 'arguments'；其响应要到 configurationDone 之后才会返回
            attach_seq = self._send("attach", {"name": "Python: Attach", "type": "python", "request": "attach",
                                               "connect": {"host": "127.0.0.1", "port": port}, "justMyCode": False,
                                               "redirectOutput": True})
            if not self.initialized.wait(timeout):
                # debugpy 拒绝 attach 时（典型：别的客户端已经占着，message 是
                # "Server[pid=N] is already being debugged."）不会发 initialized 事件，
                # 只回一个 success=false 的响应——把它带出来，别让人以为是握手超时。
                resp = self._peek(attach_seq) or {}
                raise RuntimeError("attach 被拒绝: %s" % (resp.get("message") or "debugpy 没有回应 initialized 事件"))
            self._wait(self._send("setExceptionBreakpoints", {"filters": []}), timeout)
            self._wait(self._send("configurationDone", {}), timeout)
            resp = self._wait(attach_seq, timeout)
            if not resp or not resp.get("success"):
                raise RuntimeError("attach failed: %s" % resp)
        except Exception:
            self.disconnect()
            raise
        return "connected to debugpy on port %d" % port

    def disconnect(self):
        sock, self.sock = self.sock, None
        self.connected = False
        if sock:
            # 读取线程持有 sock.makefile()，只 close() 的话底层连接不会真正关闭(服务端会一直认为客户端还在，
            # 重连报 "already being debugged"，VSCode 也接不上)，必须先 shutdown，同时也会让阻塞的读取线程退出。
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def read_data(self, since=0, limit=200, only="all", wait_seconds=0):
        """返回 (给工具看的文本, next_since, [(序号, 类型, 文本), ...])。
        结构化那份给主动通知用（常驻服务没有通往 MCP 客户端的 stdout，通知得由 MCP 侧发）。"""
        deadline = time.time() + wait_seconds
        with self.cond:
            while wait_seconds > 0 and self.next_index <= since:
                left = deadline - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
            events = [e for e in self.events if e["i"] >= since]
        if only != "all":
            events = [e for e in events if self._is_error(e, only)]
        truncated = len(events) > limit
        events = events[:limit]
        next_since = events[-1]["i"] + 1 if truncated and events else self.next_index
        lines = ["[%d] %s %s: %s" % (e["i"], time.strftime("%H:%M:%S", time.localtime(e["t"])), e["kind"], e["text"]) for e in events]
        for category, rest in self.partial.items():
            if rest:
                lines.append("(未换行) %s: %s" % (category, rest))
        head = "next_since=%d connected=%s%s" % (next_since, self.connected, " (还有更多，用 next_since 继续读)" if truncated else "")
        text = head + ("\n" + "\n".join(lines) if lines else "\n(无新事件)")
        return text, next_since, [(e["i"], e["kind"], e["text"]) for e in events]

    def read(self, since=0, limit=200, only="all", wait_seconds=0):
        return self.read_data(since, limit, only, wait_seconds)[0]

    @staticmethod
    def _is_error(event, only):
        if event["kind"] in ("output:stderr", "stopped", "exited", "terminated", "exception", "disconnected"):
            return True
        return only == "errors" and event["kind"] == "output:stdout" and ("Traceback" in event["text"] or "Error" in event["text"])

    def clear(self):
        with self.cond:
            self.events.clear()
            self.partial.clear()
        return "buffer cleared (next index stays %d)" % self.next_index

    def info(self):
        return {"connected": self.connected, "port": self.port,
                "connected_since": time.strftime("%H:%M:%S", time.localtime(self.connected_at)) if self.connected_at else None,
                "buffered_events": len(self.events), "next_index": self.next_index, "last_error": self.last_error}
