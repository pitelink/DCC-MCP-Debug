"""
dcc_bridge.py - connection management core for talking to locally running DCC
software via its command port (TCP port == the DCC process's own PID).

No network-server or CLI code lives here: dcc_service.py wraps this in a
single TCP control server that owns one DCCConnection per registered DCC tag
and routes each request to the right one by tag -- there is no per-DCC
process/port isolation, one server serves every registered DCC. dcc_client.py
wraps that server in a CLI.

This is part of the user's own DCC debugging workflow, not an official
Autodesk feature. See ../SKILL.md for background and known gotchas.
"""
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 47863

# dcc_service.py runs detached with no console of its own, so every `tasklist`
# child process it spawns (via find_pid, on every status/exec call) would
# otherwise make Windows briefly flash a new console window. Suppress that.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log_dir():
    """日志写到用户可写目录（安装到 site-packages 后那里可能只读，也不该被日志污染）。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "DCC-MCP-Debug")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        path = tempfile.gettempdir()
    return path

# tag -> process image name, exactly as `tasklist` prints it. To plug in a new
# DCC that exposes the same "TCP port == PID, exec() the received text"
# commandPort mechanism, add a line here (and an alias in dcc_client.py's
# DCC_ALIASES) -- nothing else needs to change.
DCC_REGISTRY = {
    "max": "3dsmax.exe",
    "mobu": "motionbuilder.exe",
    "maya": "maya.exe",
}


def find_pids(process_name):
    """Full scan: PIDs of every running process whose image name is
    process_name (several installed versions can run side by side)."""
    pids = []
    try:
        out = subprocess.run(["tasklist"], capture_output=True, text=True, errors="replace",
                              creationflags=_NO_WINDOW)
        for line in out.stdout.splitlines():
            if line.lower().startswith(process_name.lower()):
                parts = line.split()
                if len(parts) > 1 and parts[1].isdigit():
                    pids.append(int(parts[1]))
    except Exception:
        pass
    return pids


def find_pid(process_name):
    """First matching PID, or None. Only good for "is it running at all"
    checks -- never use it to pick a connection target when several run."""
    pids = find_pids(process_name)
    return pids[0] if pids else None


def list_instances(process_name):
    """Every running instance as {pid, version, path, started}. Path and start
    time come from CIM; if PowerShell fails we still return the PIDs."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
          "Select-Object ProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress" % process_name)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             errors="replace", creationflags=_NO_WINDOW, timeout=15)
        data = json.loads(out.stdout) if out.stdout.strip() else []
        data = [data] if isinstance(data, dict) else data
        instances = []
        for item in data:
            path = item.get("ExecutablePath") or ""
            started = None
            m = re.search(r"\d{10,}", str(item.get("CreationDate") or ""))
            if m:
                started = time.strftime("%H:%M:%S", time.localtime(int(m.group()) / 1000.0))
            # 不能用 \b(20\d\d)\b：像 "Maya2024" 这种字母数字连写的目录名没有词边界，会匹配不到
            year = re.search(r"(20\d\d)", path)
            instances.append({"pid": item.get("ProcessId"), "version": year.group(1) if year else None,
                              "path": path, "started": started})
        if instances:
            return sorted(instances, key=lambda i: i["pid"])
    except Exception:
        pass
    return [{"pid": pid, "version": None, "path": "", "started": None} for pid in find_pids(process_name)]


def pid_exists(pid):
    """只按 PID 判断进程是否还在（不知道进程名时用）。比 tasklist 便宜，也不用解析本地化的输出文本。
    查不动（权限不足等）时按"还在"处理，避免误杀。"""
    pid = int(pid)
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5   # ERROR_ACCESS_DENIED：进程还在，只是不让查
        try:
            # 只 OpenProcess 不够：已终止但还有句柄没关的进程，OpenProcess 同样会成功。
            # 必须问退出码，259 才表示还在跑。
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


def pid_alive(pid, process_name):
    """Fast path: ask tasklist to filter down to this one PID + image name,
    instead of listing every process. True if that exact (pid, process_name)
    pair still exists."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FI", "IMAGENAME eq %s" % process_name],
            capture_output=True, text=True, errors="replace", creationflags=_NO_WINDOW,
        )
    except Exception:
        return False
    return any(line.lower().startswith(process_name.lower()) for line in out.stdout.splitlines())


def running_tags():
    """tag -> pid, for every registered DCC that currently has a running process."""
    result = {}
    for tag, process_name in DCC_REGISTRY.items():
        pid = find_pid(process_name)
        if pid is not None:
            result[tag] = pid
    return result


def _recv(sock, timeout, timed_out=None):
    """Read one full response: accumulate chunks until the REPL prompt (">>>")
    arrives. A single recv() caps at 64KB and leaves the rest in the socket,
    which then shows up as the *next* command's response. Returns decoded
    text, or None on timeout / EOF; on timeout, appends True to `timed_out`
    (if given) so callers can tell "still running" apart from "dead"."""
    deadline = time.time() + timeout
    chunks = []
    while True:
        left = deadline - time.time()
        if left <= 0:
            if timed_out is not None:
                timed_out.append(True)
            return None
        sock.settimeout(left)
        try:
            data = sock.recv(65536)
        except socket.timeout:
            if timed_out is not None:
                timed_out.append(True)
            return None
        if data == b"":
            return None
        chunks.append(data)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        if text.rstrip().endswith(">>>"):
            return text


class DCCConnection:
    """One TCP connection to one DCC's commandPort. Identified purely by the
    target process's image name (e.g. "3dsmax.exe") -- it has no idea which
    DCC that is or what tag it's registered under; that mapping lives in
    DCC_REGISTRY / dcc_service.py."""

    # Timeout for the liveness ping only -- deliberately much shorter than
    # self.timeout (which has to accommodate legitimately slow/long-running
    # user scripts). A local loopback round-trip for a no-op statement should
    # come back in milliseconds; if it doesn't within PING_TIMEOUT, treat the
    # connection as dead rather than waiting out the full exec timeout.
    PING_TIMEOUT = 2

    def __init__(self, process_name, pid, timeout=30):
        self.process_name = process_name
        self.target_pid = pid          # this connection only ever talks to this one instance
        self.timeout = timeout
        self.lock = threading.Lock()
        self.sock = None
        self.pid = None
        self.banner = None
        self.connected_at = None
        self.last_error = None

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.pid = None
        self.banner = None
        self.connected_at = None

    def _current_pid(self):
        """The bound PID if that exact process still exists, else None.
        Never falls back to another instance."""
        if pid_alive(self.target_pid, self.process_name):
            return self.target_pid
        self.last_error = "实例 PID %d 已退出" % self.target_pid
        return None

    def close(self):
        with self.lock:
            self._close()

    def _ping(self):
        """Cheapest possible liveness check on an already-open connection:
        send a single no-op newline (exec()s to an empty statement -- zero
        side effects, zero output) and wait, with a short timeout, for the
        prompt it produces. No subprocess involved at all, just one
        send+recv on the existing loopback socket -- far cheaper than asking
        the OS for the process list, and it tests the thing that actually
        matters (this exact connection is still being serviced) instead of a
        proxy signal (the process still exists but may be stuck)."""
        try:
            self.sock.sendall(b"\n")
            data = _recv(self.sock, self.PING_TIMEOUT)
        except OSError:
            return False
        return data is not None

    def ensure_connected(self, pid_now=None):
        # Fast path: if we already have a connection, a cheap ping tells us
        # whether it's still actually alive -- no tasklist call needed, and
        # it fails fast (PING_TIMEOUT) instead of only being discovered dead
        # after a real command sits waiting for the full exec timeout.
        if self.sock is not None:
            if self._ping():
                return True
            self._close()

        if pid_now is None:
            pid_now = self._current_pid()
        if pid_now is None:
            return False
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect((CONTROL_HOST, pid_now))
            banner = _recv(s, self.timeout)
        except OSError as e:
            self.last_error = str(e)
            return False
        if banner is None:
            s.close()
            self.last_error = "connected but got no banner (process not responding)"
            return False
        self.sock, self.pid, self.banner = s, pid_now, banner
        self.connected_at = time.time()
        self.last_error = None
        return True

    def _try_exec(self, text, timeout=None):
        """One send+recv attempt. Returns a result dict, or None if the
        connection looks dead (send failed, or EOF before any response)."""
        timeout = timeout or self.timeout
        timed_out = []
        try:
            self.sock.sendall(text.encode("utf-8"))
            data = _recv(self.sock, timeout, timed_out)
        except OSError as e:
            self.last_error = str(e)
            return None
        if timed_out:
            # The command may still be running inside the DCC: never re-send
            # it (would execute twice), and drop this socket so its late reply
            # can't be read as the answer to the next command.
            self.last_error = "no response within %ss" % timeout
            return {"ok": False, "timed_out": True,
                    "error": "DCC 在 %s 秒内没有返回，命令可能仍在执行（未重试，避免重复执行）" % timeout}
        if data is None:
            self.last_error = "no response from DCC (connection appears lost)"
            return None
        return {"ok": True, "response": data}

    def exec_script(self, text, timeout=None):
        with self.lock:
            if not self.ensure_connected():
                return {"ok": False, "error": self.last_error}
            result = self._try_exec(text, timeout)
            if result is None:
                # looked dead -- drop it, reconnect once, retry this same command
                self._close()
                if not self.ensure_connected():
                    return {"ok": False, "error": self.last_error}
                result = self._try_exec(text, timeout)
                if result is None:
                    self._close()
                    return {"ok": False, "error": self.last_error}
            if result.get("timed_out"):
                self._close()
            return result

    def status(self):
        with self.lock:
            pid_now = self._current_pid()
            if pid_now is not None:
                self.ensure_connected(pid_now)
            return {
                "running": pid_now is not None,
                "pid": self.target_pid,
                "connected": self.sock is not None and self.pid == pid_now,
                "connected_pid": self.pid,
                "connected_at": self.connected_at,
                "last_error": self.last_error,
            }


class InstancePool:
    """Every running instance of one DCC, each with its own persistent
    connection. Connections are created lazily per PID and dropped when that
    process exits; switching between instances never disconnects the others."""

    def __init__(self, process_name):
        self.process_name = process_name
        self.lock = threading.Lock()
        self.conns = {}

    def get(self, pid):
        """Connection for this PID, or None if no such instance is running."""
        with self.lock:
            conn = self.conns.get(pid)
            if conn is None:
                if not pid_alive(pid, self.process_name):
                    return None
                conn = self.conns[pid] = DCCConnection(self.process_name, pid)
            return conn

    def prune(self, alive_pids):
        with self.lock:
            for pid in [p for p in self.conns if p not in alive_pids]:
                self.conns.pop(pid).close()

    def drop(self, pid):
        """丢掉某个实例现有的连接（下次用它时会自动重建）。用于强制重连。"""
        with self.lock:
            conn = self.conns.pop(pid, None)
        if conn is not None:
            conn.close()
        return conn is not None

    def instances(self):
        """list_instances() plus whether each one currently has a live connection."""
        found = list_instances(self.process_name)
        self.prune({i["pid"] for i in found})
        for item in found:
            conn = self.conns.get(item["pid"])
            item["connected"] = conn is not None and conn.sock is not None
        return found
