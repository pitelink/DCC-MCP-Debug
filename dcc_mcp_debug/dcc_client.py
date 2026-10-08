r"""
dcc_client.py - unified CLI for every registered DCC (see DCC_REGISTRY in
dcc_bridge.py). Talks to the single persistent dcc_service.py daemon, which
keeps one reused connection per DCC open and routes each request by the "dcc"
tag carried in it -- client and server are not split/isolated per DCC, it's
one client script and one server process for all of them. Auto-starts the
daemon (detached, survives this process exiting) the first time it isn't
reachable and at least one registered DCC is actually running.

Installed as the console script `dcc-cli`.

Usage:
    # Explicit target:
    dcc-cli max status
    dcc-cli max exec -c "import pymxs; print(pymxs.runtime.objects)"
    dcc-cli max exec <script.py>

    dcc-cli mobu status [--pid N]
    dcc-cli mobu exec -c "import pyfbsdk; print(len(pyfbsdk.FBSystem().Scene.Components))"
    dcc-cli mobu exec <script.py>

    # Several instances of one DCC running (e.g. MotionBuilder 2020 + 2024):
    dcc-cli mobu instances
    dcc-cli mobu select <PID>          # 记住"当前操作的实例"（状态存在常驻服务里）
    dcc-cli mobu exec -c "..."        # 不带 --pid 就用上面选中的那个
    dcc-cli mobu exec --pid <PID> -c "..."

    # Install the commandPort connector into a DCC's install dir:
    dcc-cli mobu install-connector --dry-run
    dcc-cli max install-connector --install-dir "<3ds Max 安装目录>"

    # Auto-detect (only unambiguous when exactly one registered DCC is running):
    dcc-cli status
    dcc-cli exec -c "..."

See ../SKILL.md for background, including why `sys.exit()`/`SystemExit` must
never be used in code sent through `exec`.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time

from .dcc_bridge import CONTROL_HOST, CONTROL_PORT, DCC_REGISTRY, running_tags

SERVICE_MODULE = "dcc_mcp_debug.dcc_service"


def release_client(timeout=5):
    """告诉常驻服务"我要走了"：释放本进程占用的当前实例选择和调试会话。"""
    return _request({"cmd": "release"}, timeout=timeout)

DCC_ALIASES = {
    "max": "max",
    "3dsmax": "max",
    "3dmax": "max",
    "mobu": "mobu",
    "motionbuilder": "mobu",
    "maya": "maya",
}


def _request(req, timeout=8):
    # 带上自己的 PID：常驻服务据此判断"这个客户端还活着吗"，客户端一退出就把它占的资源
    # （当前实例选择、debug 会话）释放掉，免得下一个会话继承、或者调试端口一直被占。
    req.setdefault("client_pid", os.getpid())
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((CONTROL_HOST, CONTROL_PORT))
    s.sendall((json.dumps(req) + "\n").encode("utf-8"))
    f = s.makefile("rb")
    line = f.readline()
    s.close()
    return json.loads(line.decode("utf-8"))


def _start_service():
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "-m", SERVICE_MODULE], **kwargs)


def ensure_service(start_timeout=5):
    try:
        socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=0.3).close()
        return True
    except OSError:
        pass
    print("service not reachable on port %d, starting it..." % CONTROL_PORT, file=sys.stderr)
    _start_service()
    deadline = time.time() + start_timeout
    while time.time() < deadline:
        try:
            socket.create_connection((CONTROL_HOST, CONTROL_PORT), timeout=0.3).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


def _build_exec_parser():
    p = argparse.ArgumentParser(prog="dcc_client.py exec")
    p.add_argument("script", nargs="?", default=None, help="path to a .py script, or '-' for stdin")
    p.add_argument("-c", "--code", help="inline Python code string to execute directly (no disk round-trip)")
    p.add_argument("--no-file-shim", action="store_true", help="don't auto-inject a __file__ shim line")
    return p


def run_cli(dcc_override=None, raw_args=None):
    if raw_args is None:
        raw_args = sys.argv[1:]
    args_to_parse = list(raw_args)

    target_tag = dcc_override
    if not target_tag and args_to_parse and args_to_parse[0].lower() in DCC_ALIASES:
        target_tag = DCC_ALIASES[args_to_parse[0].lower()]
        args_to_parse = args_to_parse[1:]

    if not args_to_parse:
        print("usage: dcc-cli [%s] <status|exec|instances|select|install-connector> ..." % "|".join(DCC_REGISTRY), file=sys.stderr)
        return 2
    command, rest = args_to_parse[0], args_to_parse[1:]
    if command not in ("status", "exec", "instances", "select", "install-connector"):
        print("unknown command: %r (expected status/exec/instances/select/install-connector)" % command, file=sys.stderr)
        return 2

    # --pid N (anywhere after the command) picks one instance; without it the
    # service uses the only running instance and refuses to guess between several.
    pid = None
    if "--pid" in rest:
        i = rest.index("--pid")
        if i + 1 >= len(rest) or not rest[i + 1].isdigit():
            print("usage: --pid <PID>", file=sys.stderr)
            return 2
        pid = int(rest[i + 1])
        rest = rest[:i] + rest[i + 2:]

    install_dir = None
    if "--install-dir" in rest:
        i = rest.index("--install-dir")
        if i + 1 >= len(rest):
            print("usage: --install-dir <DCC 安装目录>", file=sys.stderr)
            return 2
        install_dir = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    dry_run = "--dry-run" in rest
    rest = [a for a in rest if a != "--dry-run"]

    if command == "install-connector":
        from . import dcc_connector
        try:
            print(dcc_connector.install(tag=target_tag, pid=pid, install_dir=install_dir, dry_run=dry_run))
            return 0
        except dcc_connector.ConnectorError as e:
            print("错误: %s" % e, file=sys.stderr)
            return 1

    if target_tag is not None and target_tag not in DCC_REGISTRY:
        print("未知 DCC 目标: %r，可选: %s" % (target_tag, ", ".join(DCC_REGISTRY)), file=sys.stderr)
        return 1

    # Precheck via tasklist before ever touching the service: an unstarted DCC
    # means nothing to connect to, so don't spin up the bridge service for it.
    running = running_tags()

    if target_tag is None:
        if len(running) == 1:
            target_tag = next(iter(running))
        elif len(running) == 0:
            if command == "status":
                print(json.dumps({tag: {"running": False} for tag in DCC_REGISTRY}, indent=2, ensure_ascii=False))
                return 0
            print("错误: 没有已注册的 DCC 在运行 (%s)" % ", ".join(DCC_REGISTRY.values()), file=sys.stderr)
            return 1
        elif command == "status":
            # Multiple running and read-only status -- just report all of them.
            if not ensure_service():
                print("failed to start/reach dcc_service.py within timeout", file=sys.stderr)
                return 1
            resp = _request({"cmd": "status"})
            print(json.dumps(resp, indent=2, ensure_ascii=False))
            return 0
        else:
            print("错误: 多个 DCC 同时在运行 (%s)，请显式指定目标: %s"
                  % (", ".join(running), "/".join(DCC_REGISTRY)), file=sys.stderr)
            return 1

    if target_tag not in running:
        if command == "status":
            print(json.dumps({"dcc": target_tag, "running": False}, indent=2, ensure_ascii=False))
            return 0
        print("错误: %s (%s) 未启动，请先启动软件" % (target_tag, DCC_REGISTRY[target_tag]), file=sys.stderr)
        return 1

    if not ensure_service():
        print("failed to start/reach dcc_service.py within timeout", file=sys.stderr)
        return 1

    if command == "status":
        resp = _request({"cmd": "status", "dcc": target_tag, "pid": pid})
        resp["dcc"] = target_tag
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0

    if command == "instances":
        print(json.dumps(_request({"cmd": "instances", "dcc": target_tag}, timeout=20), indent=2, ensure_ascii=False))
        return 0

    if command == "select":
        # 位置参数和 --pid 都行；都不给 = 取消当前选择
        if pid is None and rest and rest[0].isdigit():
            pid = int(rest[0])
        reconnect = "--reconnect" in rest
        resp = _request({"cmd": "set_current", "dcc": target_tag, "pid": pid}, timeout=60)
        if not resp.get("ok"):
            print("错误: %s" % resp.get("error"), file=sys.stderr)
            return 1
        if reconnect and pid is not None:
            again = _request({"cmd": "reconnect", "dcc": target_tag, "pid": pid}, timeout=60)
            if not again.get("ok"):
                print("错误: %s" % again.get("error"), file=sys.stderr)
                return 1
            resp["reconnected"] = again
        print(json.dumps(resp, indent=2, ensure_ascii=False))
        return 0

    # exec
    parsed = _build_exec_parser().parse_args(rest)
    if parsed.code is not None:
        text, file_shim = parsed.code, None
    elif parsed.script == "-":
        text, file_shim = sys.stdin.read(), None
    elif parsed.script:
        with open(parsed.script, "r", encoding="utf-8") as f:
            text = f.read()
        file_shim = os.path.abspath(parsed.script)
    else:
        print("error: either script path, '-' for stdin, or -c/--code must be provided", file=sys.stderr)
        return 2

    req = {"cmd": "exec", "dcc": target_tag, "pid": pid, "script": text}
    if file_shim and not parsed.no_file_shim:
        req["file_shim_path"] = file_shim
    resp = _request(req, timeout=70)
    if not resp.get("ok"):
        print("error: %s" % resp.get("error"), file=sys.stderr)
        return 1
    print(resp["response"])
    return 0


if __name__ == "__main__":
    sys.exit(run_cli())
