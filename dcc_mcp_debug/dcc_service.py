"""
dcc_service.py - single persistent local daemon that keeps one reused TCP
connection per registered DCC (see DCC_REGISTRY in dcc_bridge.py) and exposes
all of them to short-lived clients (dcc_client.py) over ONE fixed local
control port. Every request carries a "dcc" tag; this service looks up the
matching DCCConnection and forwards to it. There is deliberately no per-DCC
process or port isolation -- one server, one port, routes by tag.

Normally you don't run this directly - dcc_client.py auto-starts it (detached)
the first time it can't reach the control port. See ../SKILL.md for background.
"""
import json
import os
import socketserver
import sys
import threading
import time
from datetime import datetime

from .dcc_bridge import CONTROL_HOST, CONTROL_PORT, DCC_REGISTRY, InstancePool, log_dir, pid_exists
from .dcc_dap import DEFAULT_PORT as DEBUG_DEFAULT_PORT, DebugSession

LOG_PATH = os.path.join(log_dir(), "dcc_service.log")
pools = {tag: InstancePool(process_name) for tag, process_name in DCC_REGISTRY.items()}
# tag -> 当前操作的实例 PID。放在常驻服务里而不是 MCP 进程里：常驻服务跨会话存活，
# MCP 进程在会话 resume/重连时会重启，内存里的状态会丢。
current = {}
# 实例 PID -> 调试会话（同一个理由：MCP 进程重启后调试连接和已收集的事件都还在）
sessions = {}
# ("current", tag) / ("session", 实例PID) -> 占用它的客户端进程 PID。
# 常驻服务不是永生的仓库：客户端（MCP 进程）一退出，它占的东西就该放掉，
# 否则调试端口会被一直占着（VSCode 之类接不上），下一个会话还会继承上一个会话的选择。
owners = {}
SWEEP_SECONDS = 5


def release_client(client_pid):
    """放掉某个客户端占用的当前实例选择和调试会话（含 debug 连接）。"""
    if client_pid is None:
        return {"ok": False, "error": "release 需要 client_pid"}
    client_pid = int(client_pid)
    released = []
    for key in [k for k, owner in owners.items() if owner == client_pid]:
        owners.pop(key, None)
        kind, value = key
        if kind == "current":
            pid = current.pop(value, None)
            if pid is not None:
                pools[value].drop(pid)
            released.append("current:%s" % value)
        elif kind == "session":
            session = sessions.pop(value, None)
            if session is not None:
                session.disconnect()
            released.append("debug:%s" % value)
    return {"ok": True, "released": released}


def _sweep_clients():
    """兜底：客户端进程被强杀时来不及说 release，定期检查它还在不在。"""
    while True:
        time.sleep(SWEEP_SECONDS)
        try:
            for client_pid in set(owners.values()):
                if client_pid is None:      # 老版本客户端不带 client_pid，无从判断，跳过
                    continue
                if not pid_exists(client_pid):
                    result = release_client(client_pid)
                    log("client %s 已退出，释放: %s" % (client_pid, result["released"]))
        except Exception as e:
            log("sweeper error: %s" % e)


def handle_debug(cmd, req):
    pid = req.get("pid")
    pid = int(pid) if pid is not None else None
    session = sessions.get(pid)
    if cmd == "debug_connect":
        if pid is None:
            return {"ok": False, "error": "debug_connect 需要 pid"}
        if session is None:
            session = sessions[pid] = DebugSession(pid)
        try:
            text = session.connect(req.get("port") or DEBUG_DEFAULT_PORT, req.get("timeout") or 15)
        except Exception as e:
            # 接入失败就别留下一个连不上的空会话，否则后续会以为"已经接上了"而跳过重试
            sessions.pop(pid, None)
            session.disconnect()
            return {"ok": False, "error": str(e)}
        if req.get("client_pid") is not None:
            owners[("session", pid)] = req.get("client_pid")
        return {"ok": True, "text": text}
    if cmd == "debug_status":
        return {"ok": True, "sessions": dict((str(p), s.info()) for p, s in sessions.items())}
    if session is None:
        return {"ok": False, "error": "实例 PID %s 还没有调试连接，先 dcc_debug_connect" % pid}
    if cmd == "debug_disconnect":
        session.disconnect()
        return {"ok": True, "text": "disconnected"}
    if cmd == "debug_clear":
        return {"ok": True, "text": session.clear()}
    if cmd == "debug_read":
        text, next_since, events = session.read_data(
            int(req.get("since", 0)), int(req.get("limit", 200)), req.get("only", "all"),
            min(float(req.get("wait_seconds", 0)), 60))
        return {"ok": True, "text": text, "next_since": next_since,
                "events": [list(e) for e in events], "connected": session.connected}
    return {"ok": False, "error": "unknown debug cmd: %r" % cmd}


def log(msg):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().isoformat(timespec="seconds"), msg))


def resolve(tag, pool, pid):
    """(connection, None) for the requested PID, else for the tag's currently
    selected instance, else for the only running instance; (None, error)
    otherwise. Never silently switches to a different instance than the one
    selected -- it reports the selection died instead."""
    if pid is not None:
        conn = pool.get(int(pid))
        return (conn, None) if conn else (None, "PID %s 不是运行中的 %s" % (pid, pool.process_name))
    cur = current.get(tag)
    if cur is not None:
        conn = pool.get(cur)
        if conn is not None:
            return conn, None
        current.pop(tag, None)
        return None, "当前选定的实例 PID %s 已退出，选择已清除；请重新 dcc_select" % cur
    found = pool.instances()
    if len(found) == 1:
        return pool.get(found[0]["pid"]), None
    if not found:
        return None, "no running process found matching: %s" % pool.process_name
    return None, "有 %d 个 %s 实例在运行 (PID %s)，请先 dcc_select 选一个，或指定 pid" % (
        len(found), pool.process_name, ", ".join(str(i["pid"]) for i in found))


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline()
            if not line:
                return
            req = json.loads(line.decode("utf-8"))
            cmd = req.get("cmd")
            tag = req.get("dcc")

            pool = pools.get(tag) if tag else None
            if cmd == "release":
                resp = release_client(req.get("client_pid"))
                log("release client=%s -> %s" % (req.get("client_pid"), resp.get("released")))
            elif cmd.startswith("debug_"):
                resp = handle_debug(cmd, req)
            elif cmd == "reconnect" and pool is not None:
                pid = req.get("pid")
                if pid is None:
                    resp = {"ok": False, "error": "reconnect 需要 pid"}
                else:
                    dropped = pool.drop(int(pid))
                    conn = pool.get(int(pid))
                    if conn is None:
                        resp = {"ok": False, "error": "PID %s 不是运行中的 %s" % (pid, pool.process_name)}
                    else:
                        st = conn.status()
                        resp = {"ok": True, "pid": int(pid), "dropped": dropped,
                                "connected": st["connected"]}
                    log("reconnect dcc=%s pid=%s -> %s" % (tag, pid, resp))
            elif cmd == "get_current":
                resp = {"ok": True, "current": dict(current)}
            elif cmd == "set_current" and pool is not None:
                if req.get("pid") is None:
                    current.pop(tag, None)
                    owners.pop(("current", tag), None)
                    resp = {"ok": True, "current": None, "message": "已取消当前实例选择"}
                else:
                    pid = int(req["pid"])
                    conn = pool.get(pid)
                    if conn is None:
                        resp = {"ok": False, "error": "PID %s 不是运行中的 %s" % (pid, pool.process_name)}
                    else:
                        current[tag] = pid
                        if req.get("client_pid") is not None:
                            owners[("current", tag)] = req.get("client_pid")
                        resp = {"ok": True, "current": pid, "connected": conn.status()["connected"]}
                log("set_current dcc=%s pid=%s -> %s" % (tag, req.get("pid"), resp))
            elif cmd in ("status", "instances") and not tag:
                resp = {t: {"instances": pl.instances()} for t, pl in pools.items()}
            elif pool is None:
                resp = {"ok": False, "error": "%s requires a known dcc tag (got %r), one of: %s" % (cmd, tag, list(pools))}
            elif cmd == "instances":
                found = pool.instances()
                cur = current.get(tag)
                for item in found:
                    item["current"] = item["pid"] == cur
                resp = {"ok": True, "instances": found, "current": cur}
            elif cmd in ("status", "exec"):
                conn, err = resolve(tag, pool, req.get("pid"))
                if conn is None:
                    resp = {"ok": False, "error": err}
                elif cmd == "status":
                    resp = conn.status()
                else:
                    text = req.get("script", "")
                    shim_path = req.get("file_shim_path")
                    if shim_path:
                        text = "__file__ = %r" % shim_path + chr(10) + text
                    resp = conn.exec_script(text, req.get("timeout"))
            else:
                resp = {"ok": False, "error": "unknown cmd: %r" % cmd}
        except Exception as e:
            resp = {"ok": False, "error": "service error: %s" % e}
            log("error handling request: %s" % e)
        self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))


def main():
    try:
        server = socketserver.ThreadingTCPServer((CONTROL_HOST, CONTROL_PORT), Handler)
    except OSError as e:
        log("bind failed, assuming another instance is already running: %s" % e)
        return
    server.daemon_threads = True
    threading.Thread(target=_sweep_clients, daemon=True).start()
    log("service started, control port %d, registry: %s" % (CONTROL_PORT, DCC_REGISTRY))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log("service stopped")


if __name__ == "__main__":
    main()
