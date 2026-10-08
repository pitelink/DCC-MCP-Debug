"""在 DCC 里执行代码的工具集，供 dcc_debug_mcp.py 注册。

执行路径: MCP -> dcc_service.py(常驻, 47863) -> DCC commandPort。
同一个 DCC 可以同时有多个实例(如 MotionBuilder 2020 和 2024)，常驻服务给每个实例各保持一条连接；
这里用常驻服务里的一个变量记住"当前操作的是哪个实例"，切换只改它，不断开任何连接；
状态放在常驻服务而不是本进程，因为 MCP 进程会随会话 resume/重连重启。

参考 pitelink.dcc-utils 扩展的做法: 用 __RETURN__ 回传结果、执行时重定向 stdout、
给末尾表达式自动 print、模块重载靠清 sys.modules、debugpy 用 DCC 自带的 python 可执行文件作适配器。
"""
import json
import os
import re
import subprocess
import sys
import time

from . import dcc_client
from .dcc_bridge import DCC_REGISTRY, running_tags

MAX_EXEC_BYTES = 200000
RETURN_MARK = "<<DCC_RETURN>>"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# 在 DCC 内执行用户代码，Python 2.7 / 3.x 通用(MotionBuilder 2020 是 2.7)。全局变量存在 DCC 内的
# builtins.__mcp_globals__ 里: commandPort 每条连接的 globals 是各自独立的，存在这里才能在连接被重建后保留。
# 输出用自己的 _Sink 收集(py2 的 io.StringIO 不收 str)，traceback 去掉包装器自己的栈帧并补上源码行。
# 返回值取 __RETURN__，没有的话取末尾表达式的值。
EXEC_WRAPPER = '''
import sys as _s, json as _j, ast as _a, io as _io, traceback as _tb
try:
    import builtins as _b
except ImportError:
    import __builtin__ as _b
_T = type(u"")
def _txt(_v):
    if isinstance(_v, bytes):
        return _v.decode("utf-8", "replace")
    if isinstance(_v, _T):
        return _v
    _r = repr(_v)
    return _r.decode("utf-8", "replace") if isinstance(_r, bytes) else _r
class _Sink(object):
    def __init__(self):
        self.parts = []
    def write(self, s):
        self.parts.append(_txt(s) if not isinstance(s, _T) else s)
    def flush(self):
        pass
    def isatty(self):
        return False
    def getvalue(self):
        return u"".join(self.parts)
def _fmt(_ty, _ex, _k, _ls, _fn):
    _fr = []
    for _f in _tb.extract_tb(_k):
        if _f[0] == "<string>" or _f[0].replace(chr(92), "/").endswith("/ast.py"):
            continue
        _ln = _f[3]
        if _f[0] == _fn and _f[1] and 0 < _f[1] <= len(_ls):
            _ln = _ls[_f[1] - 1].strip()
        _fr.append((_f[0], _f[1], _f[2], _ln))
    _p = [u"Traceback (most recent call last):\\n"] + [_txt(x) for x in _tb.format_list(_fr)] + [_txt(x) for x in _tb.format_exception_only(_ty, _ex)]
    return u"".join(_p)
_args = _j.loads(@@ARGS@@)
_g = _b.__dict__.setdefault("__mcp_globals__", {"__builtins__": _b})
_g.pop("__RETURN__", None)
_res, _lines, _fname = {}, [], "<mcp>"
_sink, _old = _Sink(), (_s.stdout, _s.stderr)
_s.stdout = _s.stderr = _sink
try:
    if _args.get("file"):
        _fname = _args["file"]
        with _io.open(_fname, "r", encoding="utf-8") as _fh:
            _code = _fh.read()
        _g["__file__"] = _fname
        if _args.get("name"):
            _g["__name__"] = _args["name"]
        else:
            _g.pop("__name__", None)
    else:
        _code = _args["code"]
    if _s.version_info[0] < 3:
        _ls = _code.split(u"\\n")
        for _i in range(min(2, len(_ls))):
            if "coding" in _ls[_i] and _ls[_i].lstrip().startswith(u"#"):
                _ls[_i] = u"#"
        _code = u"\\n".join(_ls)
    _lines = _code.split(u"\\n")
    _tree = _a.parse(_code, _fname)
    _last = None
    if _tree.body and isinstance(_tree.body[-1], _a.Expr):
        _last = _a.Expression(_tree.body.pop().value)
    exec(compile(_tree, _fname, "exec"), _g)
    if _last is not None:
        _v = eval(compile(_last, _fname, "eval"), _g)
        if _v is not None:
            _g["__RETURN__"] = _v
    if "__RETURN__" in _g:
        _res["ret"] = _txt(_g["__RETURN__"])
except BaseException:
    _t, _v, _k = _s.exc_info()
    _msgs, _seen, _e = [], set(), _v
    while _e is not None and id(_e) not in _seen:
        _seen.add(id(_e))
        _msgs.append(_fmt(type(_e), _e, _k if _e is _v else getattr(_e, "__traceback__", None), _lines, _fname))
        _e = getattr(_e, "__context__", None)
    _res["error"] = u"\\nDuring handling of the above exception, another exception occurred:\\n\\n".join(reversed(_msgs))
finally:
    _s.stdout, _s.stderr = _old
_res["out"] = _sink.getvalue()
__RETURN__ = "@@MARK@@" + _j.dumps(_res)
'''

RELOAD_CODE = '''
import sys, os
_roots = [os.path.normpath(p).lower() for p in %(roots)s]
_names = [k for k, m in list(sys.modules.items())
          if getattr(m, "__file__", None) and any(os.path.normpath(m.__file__).lower().startswith(r) for r in _roots)]
for _n in _names:
    sys.modules.pop(_n, None)
__RETURN__ = "reloaded %%d modules" %% len(_names)
'''

# port=0 表示在 DCC 里随机挑一个空闲端口。适配器用 DCC 自带的 python 可执行文件
# (MotionBuilder: mobupy.exe, 3ds Max: 3dsmaxpy.exe)。
START_DEBUGPY_CODE = '''
import sys, os, socket
try:
    import debugpy
except ImportError:
    __RETURN__ = "ERR debugpy 未安装"
else:
    _port = %(port)d
    if _port == 0:
        _s = socket.socket()
        _s.bind(("127.0.0.1", 0))
        _port = _s.getsockname()[1]
        _s.close()
    _py = {"motionbuilder.exe": "mobupy.exe", "3dsmax.exe": "3dsmaxpy.exe",
           "maya.exe": "mayapy.exe", "mayapy.exe": "mayapy.exe"}.get(os.path.basename(sys.executable).lower())
    _exe = os.path.join(os.path.dirname(sys.executable), _py) if _py else None
    if _exe and os.path.exists(_exe):
        debugpy.configure(python=_exe)
    try:
        debugpy.listen(_port)
        __RETURN__ = "PORT %%d" %% _port
    except Exception as _e:
        __RETURN__ = "ERR %%s" %% _e
'''


class ToolResult(str):
    def __new__(cls, text, is_error=False):
        obj = super().__new__(cls, text)
        obj.is_error = is_error
        return obj


# ---------------------------------------------------------------- 目标实例解析

def _service_request(req, timeout):
    if not dcc_client.ensure_service():
        raise RuntimeError("无法启动/连接 dcc_service.py")
    return dcc_client._request(req, timeout=timeout)


_SCAN_TTL = 3.0
_SCAN_CACHE = {}


def _cached(key, fresh, fn):
    """进程扫描(tasklist / PowerShell CIM)每次要 300ms 左右，而真正的命令往返只要 ~30ms。
    短时间内复用扫描结果；要看最新状态的工具传 fresh=True，实例退出时清空缓存。"""
    hit = _SCAN_CACHE.get(key)
    if hit is not None and not fresh and time.time() - hit[0] < _SCAN_TTL:
        return hit[1]
    value = fn()
    _SCAN_CACHE[key] = (time.time(), value)
    return value


def _running(fresh=False):
    return _cached("running", fresh, running_tags)


def get_current(fresh=False):
    """当前操作的实例 {tag: pid}。存在常驻服务里，跨 MCP 进程/会话存活。"""
    resp = _service_request({"cmd": "get_current"}, 20)
    return (resp or {}).get("current") or {}


def set_current(tag, pid, reconnect=False):
    """切换当前操作的实例（只改常驻服务里的一个变量，不断开任何连接）。
    reconnect=True 时额外把该实例现有的命令连接丢掉重建。"""
    resp = _service_request({"cmd": "set_current", "dcc": tag, "pid": pid}, 60)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error"))
    if reconnect and pid is not None:
        again = _service_request({"cmd": "reconnect", "dcc": tag, "pid": pid}, 60)
        if not again.get("ok"):
            raise RuntimeError(again.get("error"))
        resp["reconnected"] = again
    return resp


def _instances(tag, fresh=False):
    return _cached(("instances", tag), fresh,
                   lambda: _service_request({"cmd": "instances", "dcc": tag}, 20).get("instances", []))


def resolve_dcc(tag=None, fresh=False):
    running = _running(fresh)
    if tag:
        tag = dcc_client.DCC_ALIASES.get(str(tag).lower(), tag)
        if tag not in DCC_REGISTRY:
            raise ValueError("未知 DCC: %r，可选: %s" % (tag, ", ".join(DCC_REGISTRY)))
        if tag not in running:
            raise RuntimeError("%s (%s) 未启动" % (tag, DCC_REGISTRY[tag]))
        return tag
    if len(running) == 1:
        return next(iter(running))
    if not running:
        raise RuntimeError("没有已注册的 DCC 在运行")
    selected = [t for t in get_current() if t in running]
    if len(selected) == 1:   # 多个 DCC 在跑时，沿用 dcc_select 选过的那个
        return selected[0]
    raise RuntimeError("多个 DCC 同时在运行 (%s)，请指定 dcc 参数或先 dcc_select" % ", ".join(running))


def _describe(instances):
    return ", ".join("PID %s (%s)" % (i["pid"], i.get("version") or "?") for i in instances)


def _tag_of_pid(pid, fresh=False):
    for tag in _running(fresh):
        if any(i["pid"] == pid for i in _instances(tag, fresh)):
            return tag
    raise RuntimeError("PID %s 不是任何已注册 DCC 的运行实例" % pid)


def resolve_target(args):
    """(tag, pid): 显式 pid > 当前操作的实例 > 唯一在运行的实例。
    多个实例又没指定时报错，绝不猜。"""
    pid = args.get("pid")
    if pid is not None:
        pid = int(pid)
        tag = resolve_dcc(args.get("dcc")) if args.get("dcc") else _tag_of_pid(pid)
        return tag, pid
    tag = resolve_dcc(args.get("dcc"))
    cur = get_current().get(tag)
    if cur is not None:
        return tag, cur
    found = _instances(tag)
    if len(found) == 1:
        return tag, found[0]["pid"]
    if not found:
        raise RuntimeError("%s 没有在运行的实例" % tag)
    raise RuntimeError("有 %d 个 %s 实例在运行: %s。请先 dcc_select 选一个，或在参数里传 pid" % (len(found), tag, _describe(found)))


def exec_raw(tag, pid, code, timeout=30):
    resp = _service_request({"cmd": "exec", "dcc": tag, "pid": pid, "script": code, "timeout": timeout}, timeout + 10)
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error"))
    return resp["response"]


def _clean(raw):
    text = raw.rstrip()
    if text.endswith(">>>"):
        text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------- 调试端口

def find_debug_port(pid):
    """从该实例的子进程(debugpy adapter, 命令行带 --port N)找出它的调试端口，没有则返回 None。
    不管端口是谁启动的(连接器插件、VSCode 扩展还是 dcc_debug_start_server)都能找到。"""
    ps = ("Get-CimInstance Win32_Process -Filter 'ParentProcessId=%d' | "
          "Select-Object -ExpandProperty CommandLine" % int(pid))
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             errors="replace", creationflags=_NO_WINDOW, timeout=15).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "debugpy" in line:
            m = re.search(r"--port\s+(\d+)", line)
            if m:
                return int(m.group(1))
    return None


# ---------------------------------------------------------------- 工具

def dcc_exec(args):
    if not args.get("code") and not args.get("file"):
        return ToolResult("需要 code 或 file 其中之一", True)
    tag, pid = resolve_target(args)
    timeout = max(1.0, min(float(args.get("timeout", 30)), 600.0))
    payload = {"code": args.get("code"), "file": args.get("file"), "name": args.get("name")}
    wrapped = EXEC_WRAPPER.replace("@@ARGS@@", repr(json.dumps(payload))).replace("@@MARK@@", RETURN_MARK)
    size = len(wrapped.encode("utf-8"))
    if size > MAX_EXEC_BYTES:
        return ToolResult("代码太大 (%d 字节)，请改用 file 参数执行脚本文件" % size, True)
    raw = exec_raw(tag, pid, wrapped, timeout)
    if RETURN_MARK not in raw:
        return ToolResult(_clean(raw), True)
    try:
        res = json.loads(_clean(raw.split(RETURN_MARK, 1)[1]))
    except ValueError:
        return ToolResult(_clean(raw), True)
    parts = []
    if res.get("out"):
        parts.append("[stdout]\n" + res["out"].rstrip())
    if res.get("ret") is not None:
        parts.append("[return]\n" + res["ret"])
    if res.get("error"):
        parts.append("[error]\n" + res["error"].rstrip())
    return ToolResult("\n".join(parts) or "(执行完成，无输出)", bool(res.get("error")))


def dcc_reload_modules(args):
    tag, pid = resolve_target(args)
    return _clean(exec_raw(tag, pid, RELOAD_CODE % {"roots": repr(list(args["roots"]))}))


def dcc_start_debug_server(args):
    """确保目标实例有 debugpy 监听端口，返回 (pid, port, 说明)。已有就直接用，没有就在 DCC 里随机挑空闲端口启动。"""
    tag, pid = resolve_target(args)
    port = find_debug_port(pid)
    if port:
        return pid, port, "实例 PID %d 的调试端口已存在: %d" % (pid, port)
    want = int(args.get("port") or 0)
    out = _clean(exec_raw(tag, pid, START_DEBUGPY_CODE % {"port": want}))
    if not out.startswith("PORT "):
        raise RuntimeError(out)
    port = int(out.split()[1])
    return pid, port, "已在实例 PID %d 启动 debugpy，端口 %d" % (pid, port)


def dcc_debug_start_server(args):
    return dcc_start_debug_server(args)[2]


def dcc_instances(args):
    tag = resolve_dcc(args.get("dcc"), fresh=True)
    found = [dict(i) for i in _instances(tag, fresh=True)]
    cur = get_current().get(tag)
    for item in found:
        item["current"] = cur == item["pid"]
        item["debug_port"] = find_debug_port(item["pid"])
    return json.dumps({"dcc": tag, "current_pid": cur, "instances": found}, ensure_ascii=False, indent=1)


def dcc_select(args):
    """切换当前操作的实例：只改常驻服务里的一个变量，其它实例的命令连接和调试连接都保持不动。"""
    pid = args.get("pid")
    if pid is None:
        tag = resolve_dcc(args.get("dcc"), fresh=True)
        set_current(tag, None)
        return "已取消 %s 的当前实例选择（只有一个实例在运行时会自动使用它）" % tag
    pid = int(pid)
    tag = resolve_dcc(args.get("dcc"), fresh=True) if args.get("dcc") else _tag_of_pid(pid, fresh=True)
    try:
        resp = set_current(tag, pid, reconnect=bool(args.get("reconnect")))
    except RuntimeError as e:
        return ToolResult(str(e), True)
    connected = (resp.get("reconnected") or resp).get("connected")
    found = {i["pid"]: i for i in _instances(tag, fresh=True)}
    info = found.get(pid, {})
    msg = "当前操作的 %s 实例: PID %d（%s，%s 启动），命令连接%s" % (
        tag, pid, info.get("version") or "?", info.get("started") or "?",
        "已建立" if connected else "未建立")
    if resp.get("reconnected") is not None:
        msg += "；已丢弃旧连接并重建"
    return msg


def dcc_status(args):
    if not _running(fresh=True):
        return json.dumps({tag: {"running": False} for tag in DCC_REGISTRY}, ensure_ascii=False)
    return json.dumps({"current": get_current(), "dcc": _service_request({"cmd": "instances"}, 30)}, ensure_ascii=False, indent=1)


_PID_PROP = {"type": "integer", "description": "目标实例 PID；省略则用 dcc_select 选的当前实例，只有一个实例时自动用它"}

TOOLS = [
    {"name": "dcc_instances",
     "description": "列出某个 DCC 当前所有在运行的实例(PID、版本、路径、启动时间、是否是当前操作的实例、调试端口)。同时开着多个版本(如 MotionBuilder 2020 和 2024)时先用它看清楚。",
     "inputSchema": {"type": "object", "properties": {"dcc": {"type": "string", "description": "mobu/max；只有一个 DCC 在跑时可省略"}}},
     "handler": dcc_instances},
    {"name": "dcc_select",
     "description": "连接并切换当前操作的实例(只改一个变量，不断开任何连接，每个实例的命令连接和调试连接都保持着)。连接时自动启动并接入该实例的 debugpy，"
                    "之后运行过程中的 print/报错用 dcc_debug_read 读取。之后 dcc_exec / dcc_debug_* 默认都针对它。不传 pid = 取消选择。",
     "inputSchema": {"type": "object", "properties": {"pid": {"type": "integer"}, "dcc": {"type": "string"},
                                                    "reconnect": {"type": "boolean", "default": False,
                                                                  "description": "true = 丢开该实例现有的命令连接并重新建立（连接状态异常时用）"},
                                                    "debug": {"type": "boolean", "default": True, "description": "false = 只连命令通道，不启用调试(比如要留着调试端口给 VSCode)"}}},
     "handler": dcc_select},
    {"name": "dcc_exec",
     "description": "在正在运行的 MotionBuilder / 3ds Max 里执行 Python 代码(Python 2.7 和 3.x 的实例都能用)。返回 stdout、返回值(__RETURN__ 或末尾表达式的值)和 traceback。"
                    "全局变量跨调用保留(每个实例各自一份，连接重建后也在)。可以传 code 直接执行一段代码，也可以传 file 执行磁盘上的 .py 文件(会设置 __file__，traceback 显示真实文件名和行号)。"
                    "重要约定与限制：1. 严禁使用 SystemExit / sys.exit()，会弄死 DCC 的监听端口；2. 3ds Max 里的场景改动需要加 pymxs.runtime.redrawViews() 才会刷新视口；"
                    "3. 代码 stdout 被重定向到返回结果中，不会出现在调试通道；4. 只有命令连接的主线程执行过程被捕获，工具运行时的异步回调报错请查看 dcc_debug_read。",
     "inputSchema": {"type": "object", "properties": {"code": {"type": "string", "description": "要执行的代码；与 file 二选一"},
                                                    "file": {"type": "string", "description": "要执行的 .py 文件绝对路径(utf-8)；与 code 二选一"},
                                                    "name": {"type": "string", "description": "执行 file 时设置特 __name__，省略则不设置(此时 __name__ 是 builtins/__builtin__)"},
                                                    "dcc": {"type": "string", "description": "mobu/max；只有一个 DCC 在跑时可省略"},
                                                    "pid": _PID_PROP, "timeout": {"type": "number", "default": 30}}},
     "handler": dcc_exec},
    {"name": "dcc_reload_modules",
     "description": "清掉 DCC 里 sys.modules 中位于给定目录下的模块，下次 import 会重新加载磁盘上的新代码(不会重启 DCC)。",
     "inputSchema": {"type": "object", "properties": {"roots": {"type": "array", "items": {"type": "string"}, "description": "模块所在目录列表"},
                                                    "dcc": {"type": "string"}, "pid": _PID_PROP}, "required": ["roots"]},
     "handler": dcc_reload_modules},
    {"name": "dcc_debug_start_server",
     "description": "让目标实例的 debugpy 开始监听(该实例已有调试端口就直接复用，没有就随机挑一个空闲端口)，端口按实例各自保存，不再固定 4345。之后用 dcc_debug_connect 接入。",
     "inputSchema": {"type": "object", "properties": {"port": {"type": "integer", "description": "指定端口；省略则自动选空闲端口"},
                                                    "dcc": {"type": "string"}, "pid": _PID_PROP}},
     "handler": dcc_debug_start_server},
    {"name": "dcc_status",
     "description": "查看所有已注册 DCC 的运行实例和当前操作的是哪个实例。",
     "inputSchema": {"type": "object", "properties": {}},
     "handler": dcc_status},
]
