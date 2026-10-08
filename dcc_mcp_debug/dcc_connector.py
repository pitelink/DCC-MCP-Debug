"""把 `plugin/` 里的 commandPort 连接器装进目标 DCC 的安装目录。

不写死"版本 → 目录"的对照表：直接调目标 DCC 自带的独立解释器
（MotionBuilder 的 `mobupy.exe`、3ds Max 的 `3dsmaxpy.exe`），让它报出真实的
`sys.version_info` 和 `sys.path`。Python 版本决定用哪个 `.pyd`，`sys.path` 决定装到哪一层
site-packages —— MotionBuilder 各版本有两层 site-packages，靠"哪个存在"判断会装错。
"""
import json
import os
import re
import shutil
import subprocess

from .dcc_bridge import DCC_REGISTRY, _NO_WINDOW, list_instances

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin")

# tag -> 该 DCC 自带独立解释器的文件名
INTERPRETER = {"mobu": "mobupy.exe", "max": "3dsmaxpy.exe", "maya": "mayapy.exe"}

# tag -> 安装目录名里包含的字样（用来从可执行文件路径往上认安装根目录）
INSTALL_FOLDER_NAME = {"mobu": "MotionBuilder", "max": "3ds Max", "maya": "Maya"}

# tag -> [(启动脚本源文件名, 目标目录模板, 复制后的文件名), ...]
# 目录模板里 {root}=安装根目录。一个 DCC 可能有多套启动机制，所以是列表。
#
# Maya 用**模块机制**（实测唯一生效的一套）：
#   <安装目录>\modules\commandPort.mod 声明一个模块，模块根是 <安装目录>\plug-ins\commandPort，
#   其中 scripts\userSetup.py 会被 Maya 启动时执行（语法对照 Maya 自带的 modules\sweep.mod / MASH.mod）。
# 试过但**没生效**的：把 userSetup.py 放安装目录 scripts\、往 scripts\startup\ 扔 mel、
#   以及官方文档说的用户脚本目录 %MAYA_APP_DIR%\<版本>\scripts\userSetup.py —— 三者
#   重启后都没执行（探针标记没生成）。所以这里只用模块机制。
STARTUP = {
    "mobu": [("commandPortStartup.py", r"{root}\bin\config\PythonStartup", "commandPortStartup.py")],
    "max": [("commandPortStartup.ms", r"{root}\scripts\Startup", "commandPortStartup.ms")],
    "maya": [("commandPort.mod", r"{root}\modules", "commandPort.mod"),
             ("commandPortStartup.py", r"{root}\plug-ins\commandPort\scripts", "userSetup.py")],
}

# 探测不到 sys.path 时的兜底（来自 dcc_plugin/安装说明.md 的对照表）
FALLBACK_SITE_PACKAGES = {
    "mobu": {2020: r"bin\x64\python\site-packages", 2023: r"bin\x64\python\Lib\site-packages",
             2024: r"bin\x64\python\site-packages", 2026: r"bin\x64\python\Lib\site-packages"},
    "max": {2018: r"python\Lib\site-packages", 2019: r"python\Lib\site-packages", 2020: r"python\Lib\site-packages",
            2021: r"Python37\Lib\site-packages", 2022: r"Python37\Lib\site-packages",
            2023: r"Python\Lib\site-packages", 2024: r"Python\Lib\site-packages", 2025: r"Python\Lib\site-packages"},
}

# Python 版本 -> (plugin 里的源文件名, 复制到目标后的文件名)
# Python 3 的导入机制认带 ABI 标签的文件名，原样复制；2.7 必须改名成不带后缀的 commandPort.pyd。
PYD_BY_PYVER = {
    (2, 7): ("commandPort.pyd", "commandPort.pyd"),
    (3, 7): ("commandPort.cp37-win_amd64.pyd", "commandPort.cp37-win_amd64.pyd"),
    (3, 9): ("commandPort.cp39-win_amd64.pyd", "commandPort.cp39-win_amd64.pyd"),
    (3, 10): ("commandPort.cp310-win_amd64.pyd", "commandPort.cp310-win_amd64.pyd"),
    (3, 11): ("commandPort.cp311-win_amd64.pyd", "commandPort.cp311-win_amd64.pyd"),
}


class ConnectorError(Exception):
    pass


def _probe_interpreter(exe):
    """让目标 DCC 的解释器报出自己的 Python 版本和 sys.path。"""
    code = "import sys, json; print(json.dumps({'v': list(sys.version_info[:3]), 'sp': sys.path}))"
    try:
        out = subprocess.run([exe, "-c", code], capture_output=True, text=True, errors="replace",
                             creationflags=_NO_WINDOW, timeout=90).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise ConnectorError("调用 %s 失败: %s" % (exe, e))
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                info = json.loads(line)
            except ValueError:
                continue
            return tuple(info["v"]), list(info.get("sp") or [])
    raise ConnectorError("%s 没有返回可解析的 Python 信息" % exe)


def install_root(exe_path, tag):
    """从 DCC 可执行文件路径推出安装根目录。"""
    name = INSTALL_FOLDER_NAME.get(tag, tag)
    path = os.path.dirname(exe_path or "")
    for _ in range(4):
        if os.path.basename(path).lower().startswith(name.lower()):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    raise ConnectorError("从 %s 里认不出 %s 的安装目录" % (exe_path, name))


def _version_year(root):
    m = re.search(r"(20\d\d)", os.path.basename(root))
    return int(m.group(1)) if m else None


def _pick_site_packages(tag, version_year, sys_path, root):
    """先复用已经装过 commandPort 的那一层（原地升级），否则按 install 文档的对照表，再否则用
    sys.path 里的第一个 site-packages。"""
    candidates = []
    for p in sys_path:
        if p and os.path.basename(p.rstrip("\\/")).lower() == "site-packages" and os.path.isdir(p):
            if p not in candidates:
                candidates.append(p)
    for path in candidates:
        if _find_installed_pyd(path):
            return path, "该层已装过 commandPort，原地升级"
    doc_rel = FALLBACK_SITE_PACKAGES.get(tag, {}).get(version_year)
    if doc_rel:
        doc_path = os.path.join(root, doc_rel)
        if os.path.isdir(doc_path):
            return doc_path, "按安装文档的对照表"
    if candidates:
        return candidates[0], "取 sys.path 里的第一个 site-packages"
    if doc_rel:
        return os.path.join(root, doc_rel), "按安装文档的对照表（目标目录尚不存在，将创建）"
    raise ConnectorError("认不出该版本该用哪一层 site-packages")


def _find_installed_pyd(site_packages):
    try:
        for name in os.listdir(site_packages):
            if name.lower().startswith("commandport") and name.lower().endswith(".pyd"):
                return name
    except OSError:
        pass
    return None


def plan(tag, exe_path):
    """算出要复制什么、复制到哪，不做任何写入。"""
    if tag not in INTERPRETER:
        raise ConnectorError("未知 DCC: %r" % tag)
    root = install_root(exe_path, tag)
    interp = os.path.join(os.path.dirname(exe_path), INTERPRETER[tag])
    if not os.path.isfile(interp):
        raise ConnectorError("找不到 %s（该版本可能没有内嵌 Python）" % interp)

    py_ver, sys_path = _probe_interpreter(interp)
    key = (py_ver[0], py_ver[1])
    if key not in PYD_BY_PYVER:
        raise ConnectorError("没有匹配 Python %d.%d 的 commandPort，可用: %s" % (
            py_ver[0], py_ver[1], ", ".join("%d.%d" % k for k in sorted(PYD_BY_PYVER))))
    pyd_src_name, pyd_dst_name = PYD_BY_PYVER[key]

    version_year = _version_year(root)
    site_packages, why = _pick_site_packages(tag, version_year, sys_path, root)
    pyd_src = os.path.join(PLUGIN_DIR, pyd_src_name)
    if not os.path.isfile(pyd_src):
        raise ConnectorError("包内缺少 %s" % pyd_src)

    startups = []
    for src_name, dir_tmpl, dst_name in STARTUP[tag]:
        src = os.path.join(PLUGIN_DIR, src_name)
        if not os.path.isfile(src):
            raise ConnectorError("包内缺少 %s" % src)
        dst = os.path.join(dir_tmpl.format(root=root), dst_name)
        startups.append({"src": src, "dst": dst, "exists": os.path.isfile(dst),
                         "is_ours": _is_our_startup(dst), "identical": _same_file(src, dst)})

    pyd_dst = os.path.join(site_packages, pyd_dst_name)
    return {
        "dcc": tag, "install_root": root, "version": version_year,
        "python": "%d.%d.%d" % py_ver, "interpreter": interp,
        "pyd_src": pyd_src, "pyd_dst": pyd_dst,
        "site_packages": site_packages, "site_packages_reason": why,
        "startups": startups,
        "existing_pyd": _find_installed_pyd(site_packages),
        "pyd_identical": _same_file(pyd_src, pyd_dst),
    }


def _same_file(a, b):
    """两个文件内容是否完全一样（先比大小，再比内容）。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def _is_our_startup(path):
    """Maya 的 userSetup.py 可能是用户自己的脚本，别覆盖别人的：只有内容里带 commandPort 才算我们的。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "commandPort" in f.read()
    except OSError:
        return False


def install(tag=None, pid=None, install_dir=None, dry_run=False):
    """把连接器装到目标 DCC。返回给人看的报告文本。"""
    tag, exe_path = _resolve_target(tag, pid, install_dir)
    p = plan(tag, exe_path)

    lines = ["%s 安装连接器%s" % (DCC_REGISTRY[tag], "（预演，未写入）" if dry_run else "") + ":",
             "  安装目录: %s%s" % (p["install_root"], "  (%s)" % p["version"] if p["version"] else ""),
             "  内嵌 Python: %s  (%s)" % (p["python"], p["interpreter"]),
             "  .pyd   -> %s" % p["pyd_dst"],
             "            [%s]" % p["site_packages_reason"]]
    lines += ["  启动脚本 -> %s" % s["dst"] for s in p["startups"]]
    if p["existing_pyd"]:
        lines.append("  pyd: %s" % ("已是最新，无需重写" if p["pyd_identical"]
                                    else "已有 %s，将被覆盖" % p["existing_pyd"]))
    if p["python"].startswith("2.7"):
        lines.append("  注意: Python 2.7 必须用不带后缀的 commandPort.pyd（已自动改名）")
    for s in p["startups"]:
        if s["exists"] and not s["is_ours"]:
            raise ConnectorError("启动脚本 %s 已存在且不是本工具写的（内容里没有 commandPort），"
                                 "不覆盖它。请手动在里面加一行 import commandPort。" % s["dst"])
        if s["exists"]:
            lines.append("  启动脚本: %s" % ("已是最新，无需重写" if s["identical"] else "将被覆盖"))

    if dry_run:
        return "\n".join(lines)

    copied, skipped, failed = [], [], []
    targets = [("pyd", p["pyd_src"], p["pyd_dst"], p["pyd_identical"])]
    targets += [("startup", s["src"], s["dst"], s["identical"]) for s in p["startups"]]
    for _kind, src, dst, identical in targets:
        if identical:
            skipped.append(dst)          # 已经一样就别写：DCC 开着时 pyd 被占用，写它必然失败
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(dst)
        except OSError as e:
            failed.append((dst, e))

    for dst in skipped:
        lines.append("  跳过（已是最新）: %s" % dst)
    if failed:
        detail = "；".join("%s (%s)" % (d, e) for d, e in failed)
        raise ConnectorError(
            "以下文件写入失败: %s\n"
            "多半是该 DCC 正开着、文件被占用（Windows 不允许覆盖已加载的 .pyd）："
            "先关掉它再装，或用管理员身份重试。" % detail)

    if copied:
        lines.append("  完成。**必须重启该 DCC** 后 `import commandPort` 才会生效（启动时才执行一次）。")
    else:
        lines.append("  完成。文件都已是最新，没有写入任何东西，不需要重启。")
    return "\n".join(lines)


def _resolve_target(tag, pid, install_dir):
    """返回 (tag, exe_path)。优先用显式 pid 对应的实例；否则用安装目录推断；否则要求只有一个实例在跑。"""
    tags = [tag] if tag else list(DCC_REGISTRY)
    if install_dir:
        tag = _pick_tag(tag, tags)
        exe = _exe_in(install_dir, tag)
        if not os.path.isfile(exe):
            raise ConnectorError("%s 下没找到 %s" % (install_dir, os.path.basename(exe)))
        return tag, exe

    if pid is not None:
        for t in tags:
            for inst in list_instances(DCC_REGISTRY[t]):
                if inst["pid"] == int(pid):
                    return t, inst["path"]

    running = []
    for t in tags:
        for inst in list_instances(DCC_REGISTRY[t]):
            running.append((t, inst))
    if len(running) == 1:
        return running[0][0], running[0][1]["path"]
    if not running:
        raise ConnectorError("没有在运行的 DCC。请启动目标 DCC，或用 install_dir 直接指定安装目录")
    raise ConnectorError("有多个 DCC 实例在运行 (%s)，请用 pid 指定，或用 install_dir 指定目录" % (
        ", ".join("%s %s PID %d" % (t, i.get("version") or "?", i["pid"]) for t, i in running)))


def _pick_tag(tag, tags):
    if tag:
        return tag
    if len(tags) == 1:
        return tags[0]
    raise ConnectorError("请指定 dcc（%s）" % "/".join(tags))


def _exe_in(install_dir, tag):
    if tag == "mobu":
        return os.path.join(install_dir, "bin", "x64", "motionbuilder.exe")
    if tag == "maya":
        return os.path.join(install_dir, "bin", "maya.exe")
    return os.path.join(install_dir, "3dsmax.exe")


def _tool_handler(args):
    return install(tag=args.get("dcc"), pid=args.get("pid"),
                   install_dir=args.get("install_dir"), dry_run=bool(args.get("dry_run")))


TOOLS = [
    {"name": "dcc_install_connector",
     "description": "把 commandPort 连接器装进目标 DCC 的安装目录（没有它就没法用 TCP 连上那个 DCC）。"
                    "会调用目标 DCC 自带的独立解释器问出 Python 版本和 sys.path，据此挑对应的 .pyd 并选对 site-packages 层"
                    "（MotionBuilder 各版本有两层 site-packages，装错层不生效），再放好启动脚本。"
                    "需要该 DCC 正在运行、或用 install_dir 指定安装目录；装进 Program Files 需要管理员权限，权限不足会明确报错。"
                    "装完必须重启该 DCC 才生效。先用 dry_run 预演。",
     "inputSchema": {"type": "object", "properties": {
         "dcc": {"type": "string", "description": "mobu/max；只有一个 DCC 在跑时可省略"},
         "pid": {"type": "integer", "description": "用哪个运行实例的安装目录（同版本多实例时一般不需要）"},
         "install_dir": {"type": "string", "description": "DCC 安装根目录；DCC 没在运行时用这个直接指定"},
         "dry_run": {"type": "boolean", "default": False, "description": "true = 只报告将要做什么，不写任何文件"}}},
     "handler": _tool_handler},
]
