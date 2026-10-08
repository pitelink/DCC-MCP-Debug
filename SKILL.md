---
name: DCC-MCP-Debug
description: 如何在不打断用户 MotionBuilder / 3ds Max / Maya 会话的情况下，把代码改动实际跑起来验证——通过 TCP 连接本地正在运行的 MotionBuilder 或 3ds Max 进程、发送脚本文本执行。该技能已被封装为 DCC-MCP-Debug 服务。
---

# DCC 运行调试手册 (DCC-MCP-Debug)

> 这套方法是**用户自己的工具/工作流**，不是 Autodesk 官方文档记录的功能——
> 不要把它当成"MotionBuilder/3ds Max 内置能力"介绍给别人，也不要在其它机器上假设它一定存在
> （得先用"安装DCC连接器"流程装好）。

## 背景

DCC 软件（MotionBuilder、3ds Max、Maya）通常自带一个内嵌 Python 解释器，版本往往和系统里独立安装的
Python 不一致。如果某些依赖包（比如某个 `.pyd` 编译扩展）是专门编译给 DCC 内嵌 Python 版本的，
在系统独立 Python 下 `import` 会直接失败（`ModuleNotFoundError` 或 ABI 不匹配）。这种情况下，
唯一能"跑起来看效果"的办法就是让 DCC 自己执行代码——这份手册记录的就是怎么从外部（比如这个
agent 所在的 shell）把代码注入到正在运行的 DCC 进程里执行，而不用用户手动去点。

## 原理

DCC 软件装好 commandPort 连接器后，会监听一个本地 TCP 端口，**端口号等于该 DCC 进程自己的
PID**，收到的原始文本会被当成 Python 代码 `exec()` 执行。具体是哪个插件/怎么装的见下面
"安装 DCC 连接器"。

`exec()` 执行的是一段匿名字符串，不是真实文件，所以像 `os.path.dirname(__file__)` 这种写法本来
会报 `NameError: name '__file__' is not defined`——用脚本文件路径调用 `exec` 时默认会自动垫一行
`__file__ = "<脚本绝对路径>"` 绕开这个问题（可以用 `--no-file-shim` 关掉；`-c` 内联代码模式没有
文件路径，不垫这行）。

这个命令端口是个**有状态的 REPL**：一条连接建立时先吐一次 Python 版本 banner，之后同一条连接上
可以连续发多条命令、各自收响应，不需要每条命令都重新连接。`dcc_service.py` 就是利用这一点，给
每个注册的 DCC 各维护一条常驻连接，所有 `exec` 请求都在对应连接上排队执行
（`dcc_bridge.DCCConnection` 内部加了锁）。

## 安装 DCC 连接器（commandPort）

`dcc_mcp_debug/plugin/` 下是 commandPort 插件本身（各 Python 版本的 `.pyd` + 3ds Max 的 `.ms`、
MotionBuilder 的 `.py`、Maya 的 `.mod` 模块声明 + 模块里的 `userSetup.py`）——这就是上面"原理"里说的那个"黑盒 TCP 执行入口"的真身。它**随包分发**，`pip install`
之后就在安装好的包里，不需要另外找文件。

**优先用自动安装**（不用查版本对照表、不用先知道装在哪个盘）：
- MCP 工具 `dcc_install_connector`（先带 `dry_run: true` 预演一次）
- 命令行 `dcc-cli mobu install-connector` / `dcc-cli max install-connector` / `dcc-cli maya install-connector`

它会调用目标 DCC 自带的独立解释器（`mobupy.exe` / `3dsmaxpy.exe` / `mayapy.exe`）问出 **Python 版本**和 **sys.path**，
据此挑对应的 `.pyd`、并选对该装哪一层 site-packages（MotionBuilder 各版本有两层 site-packages 且都在
sys.path 上，靠"哪个目录存在"判断会装错），最后放好启动脚本。目标 DCC 没在运行时用 `--install-dir` /
`install_dir` 指定安装根目录。装进 `Program Files` **需要管理员权限**，权限不足会明确报错；装完**必须
重启该 DCC** 才生效。

手工步骤和版本对照表见 [`dcc_mcp_debug/plugin/安装说明.md`](dcc_mcp_debug/plugin/安装说明.md)，作为兜底
（自动安装失败、或目标版本不在支持范围时）。

## 用法（推荐）：统一 client + 统一 server

客户端 `dcc_mcp_debug/dcc_client.py` 和服务端 `dcc_mcp_debug/dcc_service.py` 都是**统一、不分 DCC 的单一进程**：
只有一个常驻服务、一个控制端口（`127.0.0.1:47863`），内部按 `dcc_bridge.DCC_REGISTRY`
（`{"max": "3dsmax.exe", "mobu": "motionbuilder.exe"}`）给每个 DCC 各开一条连接。客户端每次请求
都带一个 `dcc` 标签（`max`/`mobu`），服务端照标签查到对应连接转发过去——两个 DCC 之间不做进程/
端口隔离，谁在跑就用谁的连接，互不影响。
统一客户端支持**显式指定目标 DCC** 或在单软件运行时**自动嗅探目标 DCC**。

```bash
# 1. 自动检测（当本地只有 3ds Max / MotionBuilder / Maya 之一运行时，无需输入目标名称）
dcc-cli status
dcc-cli exec -c "<Python代码字符串>"

# 2. 显式指定 3ds Max (max / 3dsmax)
dcc-cli max status
dcc-cli max exec -c "<Python代码字符串>"
dcc-cli max exec "<要跑的脚本路径.py>"

# 3. 显式指定 MotionBuilder (mobu / motionbuilder)
dcc-cli mobu select <PID>          # 多个实例时记住"当前操作的"(状态存常驻服务，跨会话不丢)
dcc-cli mobu status
dcc-cli mobu exec -c "<Python代码字符串>"
dcc-cli mobu exec "<要跑的脚本路径.py>"

# 4. 显式指定 Maya (maya)
dcc-cli maya select <PID>          # 多个实例时记住"当前操作的"
dcc-cli maya status
dcc-cli maya exec -c "<Python代码字符串>"
dcc-cli maya exec "<要跑的脚本路径.py>"
```

> `dcc-cli` 是 pip 装出来的命令行入口（= 本包 `dcc_mcp_debug/dcc_client.py` 的 `run_cli`）。
> 没装包、只想用目录里的脚本时，等价写法是 `python -m dcc_mcp_debug.dcc_client ...`。

**执行模式支持**：
- `exec -c "<code>"`：直接传入单行或多行 Python 代码字符串，不读写磁盘（推荐在交互式调试中优先使用）。
- `exec <script.py>`：执行本地现有文件。
- `exec -`：从标准输入 stdin 读取代码流。

`exec` 可以直接发送**未经修改**的脚本文件。`exec` 返回结果直接打印到 stdout，就是 DCC 命令端口的
原始响应文本（连接时的 Python banner 会被常驻服务在建连接那一步内部消费掉，不会混进 `exec` 的
输出里）：
- 只有 `>>>` → 脚本执行到底、没有抛异常。
- 带 `Traceback (most recent call last):...` → 照常规 Python 报错读 traceback 定位问题。

### 调用优化指南（AI 助手重点遵循）

底层是直接的 socket 请求/响应，不涉及磁盘 I/O，单次调用很快；如果把一次代码验证拆解成"先写临时
文件 -> 再调 client 执行 -> 再删临时文件"三步，会白白多出两轮工具调用/决策延时，且没有必要。

- 即时性指令、测试代码或快速操作，**优先用 `exec -c "<代码>"` 单步直发**，不要写临时文件。
- 只有在执行现有工程脚本、或大段已有文件内容时，才用 `exec <文件路径>`。
- 需要把少量数据从 DCC 内部传回来，走 `raise ValueError(...)` / 普通异常通道（见下面"复用要点"里
  关于 `SystemExit` 的禁令）。

`status` 打印一段 JSON，字段含义：`running`（对应 DCC 进程是否还在跑）、`pid`、`connected`（常驻
服务当前是否有一条打通的连接）、`connected_pid`、`connected_at`、`last_error`；`dcc_client.py` 会
再加一个顶层 `dcc` 字段标注这条状态是哪个标签的（不带标签、且当时有多个 DCC 在跑时，`status` 会
直接返回 `{"max": {...}, "mobu": {...}}` 这样按标签分组的整体状态）。

如果 client 本身报错说连不上/拉不起服务，可以直接看 `%LOCALAPPDATA%\DCC-MCP-Debug\dcc_service.log` 排查（只有这一份日志，
因为只有一个常驻服务进程）。

## MCP 服务：`DCC-MCP-Debug`

把上面这套能力包成了一个 MCP 服务（`dcc_mcp_debug/dcc_debug_mcp.py`），名字叫 **`DCC-MCP-Debug`**，只依赖 Python 标准库，没有 `mcp` 包也能跑。
它不依赖当前目录，任何位置、任何支持 stdio MCP 的 AI 客户端都能用。

### 方式一：pip 安装（推荐，别人拿去用最省事）

目录里有 `pyproject.toml`，装完得到两个命令行入口：`dcc-mcp-debug`（MCP 服务）和 `dcc-cli`（即原来的 `dcc_client.py`）。

```bash
pip install git+<仓库地址>
```

改脚本时建议用可编辑安装，避免 site-packages 里出现一份和包目录不一致的副本：

```bash
python -m pip install -e "<本包目录>"
```

装好后，AI 客户端的配置只需要写命令名，不用关心脚本路径和 Python 路径：

```json
{
  "mcpServers": {
    "DCC-MCP-Debug": { "command": "dcc-mcp-debug" }
  }
}
```

GUI 客户端不一定继承 PATH；如果找不到 `dcc-mcp-debug`，把 `command` 换成 pip 那个 Python 的 Scripts 目录里的绝对路径，
例如 `"<Python 安装目录>\\Scripts\\dcc-mcp-debug.exe"`，仍然不用写脚本路径。

**Claude Code**：

```bash
claude mcp add --scope user DCC-MCP-Debug -- dcc-mcp-debug
```

### 方式二：不安装，直接用目录里的脚本

`command` 写 Python 绝对路径（GUI 客户端不一定继承 PATH），`args` 用 `-m` 指向包模块；
把整个 `DCC-MCP-Debug` 目录放在哪里都行，只要那个目录的**父目录**能被 `-m` 找到：

```json
{
  "mcpServers": {
    "DCC-MCP-Debug": {
      "command": "<Python 安装目录>\\python.exe",
      "args": ["-m", "dcc_mcp_debug.dcc_debug_mcp"],
      "env": { "PYTHONPATH": "<本包目录>" }
    }
  }
}
```

不管哪种方式，只需给客户端这几个工具的描述就能用；`dcc_exec` / `dcc_debug_read` 的工具描述里已经内置了关键注意事项（禁用 `SystemExit`、3ds Max 要 `redrawViews()`、同步输出走返回值而异步回调报错走调试通道），
所以外部 AI 客户端**不必**依赖客户端的 `CLAUDE.md` 说明也能按规矩用它。

换一台机器时：整个 `DCC-MCP-Debug` 目录拷过去（`dcc_mcp_debug/` 里的模块互相依赖、`plugin/` 是连接器文件，都在包里），再把路径改掉。

**工具一览**（12 个）：
- 执行：`dcc_exec`（`code` 或 `file`）、`dcc_reload_modules`
- 实例：`dcc_instances`、`dcc_select`、`dcc_status`
- 连接器：`dcc_install_connector`
- 调试：`dcc_debug_start_server`、`dcc_debug_connect`、`dcc_debug_read`、`dcc_debug_status`、`dcc_debug_clear`、`dcc_debug_disconnect`

**连接即启用调试**：`dcc_select` 选定实例时，会自动在该实例里启动 debugpy（已有端口就复用）并接入；没调过 `dcc_select`、直接对某实例第一次 `dcc_exec` 时也一样。
自动接入失败不影响命令执行，只在返回里多一句说明，60 秒内不重试。`dcc_select(debug=false)` 可以只连命令通道（比如要把调试端口留给 VSCode）。
手动 `dcc_debug_disconnect` 过的实例不会再自动重连，要再接入就重新 `dcc_select` 或 `dcc_debug_connect`。多个 DCC 同时在跑时，不带 `dcc` 参数的调用沿用最近 `dcc_select` 选过的那个。

`dcc_debug_disconnect` 里必须先 `shutdown` 再 `close` 套接字：读取线程持有 `makefile()`，只 `close()` 的话底层连接不会真关，服务端会一直认为客户端还在，
重连报 `Server[pid=N] is already being debugged`，VSCode 也接不上。

## 多实例：全部保持连接，只记住"当前操作哪个"

同一个 DCC 可能同时开着多个版本/窗口（例如 MotionBuilder 2020 和 2024，PID 不同）。常驻服务给**每个实例各保持一条命令连接**
（`InstancePool`，按 PID 懒创建，进程退出时清理），并用一个变量记住当前操作的是哪个实例。
**状态放在常驻服务里，但生命周期跟着客户端走**：不放 MCP 进程（MCP 进程会随会话 resume/重连被重启，放在它内存里等于随机丢）；
  放常驻服务是为了让它跨 MCP 进程重启存活，但**客户端一退出就释放**——常驻服务会记下每样东西是哪个客户端进程占的，
  客户端优雅退出时主动告知（MCP 的 atexit），被强杀时由服务端每 5 秒扫一次进程存活兜底。
  **新会话不会继承上一个会话的选择和调试连接**，需要重新说"连接DCC"。

- 切换实例（`dcc_select(pid)`；命令行 `dcc-cli mobu select <pid>`，不带 pid = 取消选择）只改这个变量，**不断开任何连接**；
  连接状态可疑时用 `dcc_select(pid, reconnect=True)`（命令行 `dcc-cli mobu select <pid> --reconnect`）强制把该实例的命令连接丢掉重建——
  只影响这一个实例，别的不动；每个实例在 DCC 里的全局变量（`dcc_exec` 的跨调用状态）也各自保留。
- 用户要求"连接 DCC"、或要操作哪个不明确时：先 `dcc_instances` 看实例。**只有一个**就直接用；**有多个**用 `AskUserQuestion` 问用户，
  选项写清 版本 + PID + 启动时间（如"MotionBuilder 2024 · PID <PID> · 启动时间"），再 `dcc_select`。不要自己按版本号、启动先后去猜。
- 没选择又有多个实例时，`dcc_exec` 会直接报错并列出 PID（故意的，不要绕过）。`CURRENT` 指向的实例退出后也会报错并清除选择，不会悄悄换到另一个。
- 任何调用都可以带 `pid` 参数临时打到某个实例，不改变 `CURRENT`。命令行同理：`dcc_client.py mobu instances`、`dcc_client.py mobu exec --pid <PID> -c "..."`。
- **调试端口按实例各自保存，不再固定 4345**：`dcc_debug_start_server` 让目标实例在随机空闲端口启动 debugpy（该实例已有就复用），
  `dcc_debug_connect` 自动从该实例的 debugpy 适配器子进程命令行（`--port N`）找端口，所以连接器插件/VSCode 扩展启动的端口也能找到。
  每个实例各有独立的调试连接和事件缓冲区，可以同时连着多个，切换当前实例时互不影响。
  **调试会话同样住在常驻服务里**（`dcc_service.sessions`，实现见 `dcc_dap.py`），不放 MCP 进程，理由同上；
  客户端退出时连会话一起断开，**调试端口随之释放**（否则会被一直占着，VSCode 之类接不上）。
  MCP 只负责转发，并把常驻服务里新到的事件轮询转成客户端通知（`notifications/message`）。
- `dcc_debug_connect` 的 attach 请求必须带 `"redirectOutput": True`，否则 debugpy 不会接管 `sys.stdout`，`print` 到不了调试通道（MotionBuilder 的
  `sys.stdout` 默认是它自己的 `_ToListener`）。经 `dcc_exec` 执行的代码 stdout 被包装器重定向回结果里，也不会进调试通道；调试通道抓的是工具自己运行时
  （比如你在界面点"执行"）的输出，以及 `dcc_client.py exec` 直发代码的输出。`dcc_exec` 里抛的异常走命令响应，不在调试通道里。
- `dcc_exec` 的包装器（`dcc_exec_tools.py` 的 `EXEC_WRAPPER`）Python 2.7（MotionBuilder 2020）和 3.x 通用，实测两个版本 13 个用例结果一致。设计依据：
  - 全局变量存在 DCC 内的 `builtins.__mcp_globals__`（2.7 里是 `__builtin__`）。**commandPort 每条连接的 globals 是各自独立的**，超时后连接被我们重建，
    变量存在 commandPort 自己的 globals 里就丢了，存在 builtins 里才能保留。
  - stdout 用自己的 `_Sink` 收集，不用 `io.StringIO`（py2 里它只收 unicode，`print` 写 `str` 会报错，pitelink.dcc-utils 扩展就是为此按版本分支导入）。
  - traceback 去掉包装器自己和 `ast.py` 的栈帧、补上源码行，py3 下带异常链（对应扩展的 `format_exception`）；py2 下带 coding 声明的代码会先把声明行换成 `#`。
  - `file` 参数执行磁盘上的 .py（utf-8），会设置 `__file__`，traceback 显示真实文件名和行号；`name` 参数设置 `__name__`，不传则不设置
    （此时 `__name__` 是 `builtins`/`__builtin__`，`setup.py` 的 `if __name__ in ("__main__","builtins")` 就是依赖这点）。
  - 命令长度没有限制（扩展里那个 1025 字节的检查是它自己加的，commandPort 本身 5000 字节也正常）。
- 进程扫描（`tasklist` / PowerShell CIM）每次约 300ms，而真正的命令往返只要约 30ms，所以 MCP 里把扫描结果缓存 3 秒；`dcc_instances` / `dcc_select` /
  `dcc_status` / `dcc_debug_status` 强制刷新，服务报"实例已退出"时清缓存。`dcc_exec` 因此从约 570ms 降到约 27ms。

## 架构

- `dcc_mcp_debug/dcc_bridge.py`：连接管理核心。`DCC_REGISTRY` 是"标签 → 进程名"的字典（
  `{"max": "3dsmax.exe", "mobu": "motionbuilder.exe"}`），`DCCConnection(process_name=...)` 是通用的
  "按进程名找 PID、连 PID 端口、发送/接收、断线重连、查状态"逻辑，不知道也不关心自己具体是哪个 DCC，
  不含任何网络服务/CLI 代码。所有 `subprocess.run(["tasklist", ...])` 调用都带了
  `creationflags=CREATE_NO_WINDOW`，否则 `dcc_service.py` 这种没有控制台的 detached 进程每次查
  PID 都会在屏幕上闪一下黑色控制台窗口。

  连接活性检测分两层，成本从低到高：
  1. **`DCCConnection._ping()`**（已有连接时的默认路径）：在现有 socket 上发一个字节的 `"\n"`
     （等价于 `exec("\n")`，空语句零副作用），用专门的短超时 `PING_TIMEOUT = 2` 秒等回包。
     不起子进程，纯本地回环收发，通常几毫秒内出结果；ping 通过就直接复用连接执行真正的代码
     （用完整的 `self.timeout`，不会因为脚本本身跑得久而被误判断线）。ping 失败（超时/报错）
     才判定连接已死，转去重连。这层测的是"这条连接现在到底还能不能正常收发"，比查进程列表更
     直接、也更快发现"进程没死但主线程卡死"这种 `tasklist` 测不出来的情况。
  2. **`find_pid()` / `pid_alive()`**（只在没有连接、或 ping 判定连接已死需要重连时才用）：
     `pid_alive(pid, name)` 用 `tasklist /FI "PID eq <pid>" /FI "IMAGENAME eq <name>"` 只查一个
     已知 PID，比 `find_pid()` 全量拉 `tasklist` 再逐行找快；`DCCConnection._current_pid()` 封装了
     "有已保存 PID 就先走 `pid_alive` 快路径，查不到/没有已保存 PID 才 fallback 到 `find_pid()`
     全量扫"这套逻辑。这两个函数都要起 `tasklist.exe` 子进程，开销比 `_ping()` 大一个数量级以上
     （进程创建本身的成本），所以只在必须重新定位 PID 时才用。
- `dcc_mcp_debug/dcc_service.py`：**单个**常驻 daemon，按 `DCC_REGISTRY` 给每个标签各开一个
  `DCCConnection`（存在一个 `{tag: DCCConnection}` 字典里），对外只监听**一个**本地固定端口
  `127.0.0.1:47863`。收一行 JSON 请求（`{"cmd":"status","dcc":"max"}` / `{"cmd":"exec","dcc":"mobu",
  "script":...}`，`status` 的 `dcc` 可省略表示查全部），按请求里的 `dcc` 标签查字典转发给对应连接，
  返回一行 JSON 响应。日志在 `%LOCALAPPDATA%\DCC-MCP-Debug\dcc_service.log`。两个 DCC 共用一个进程、一个端口，彼此之间
  没有隔离，仅靠请求里的标签区分。
- `dcc_mcp_debug/dcc_client.py`：统一 CLI 入口（命令行入口名 `dcc-cli`），找不到常驻服务就自动用 `subprocess.Popen`（detached，
  不随本次命令退出被杀）拉起 `dcc_service.py`，再带着 `dcc` 标签发请求。命令行第一个参数如果是
  `max`/`mobu`（或别名 `3dsmax`/`motionbuilder`）就作为显式标签；省略时会看 `tasklist` 自动嗅探
  （只有一个在跑就用那个，多个在跑则 `status` 汇总展示、`exec` 报错要求显式指定，一个都没跑则直接
  报"未运行"、不会去拉起常驻服务）。

要接入新的 DCC（只要它也走"TCP 端口 = 进程 PID，收到文本 exec()"这种 commandPort 机制），只需要在
`dcc_bridge.DCC_REGISTRY` 里加一行标签映射、在 `dcc_client.DCC_ALIASES` 里加对应别名，`dcc_service.py`
会自动为新标签开一个连接，不用碰连接管理逻辑本身。

评估过要不要直接做成 MCP server（Claude Code 原生管理生命周期、结构化工具调用），但那需要装
`mcp` 依赖、改 Claude Code 配置、且新注册的 MCP server 要重启会话才生效，所以先做成上面这套本地
daemon + client 的方案。核心逻辑已经拆进独立的 `dcc_bridge.py`，以后真要包一层 MCP server，只是
外面再套一层，不用重写连接管理。

## 复用要点

- DCC 重启后 PID 会变，旧连接的 `_ping()` 会失败（进程没了，socket 直接断），`dcc_service.py`
  会在下一次 `exec`/`status` 请求时自动走重连路径、用 `find_pid()` 找到新 PID，不需要手动重启
  常驻服务。
- 常驻服务是单实例的：`dcc_service.py` 启动时如果发现自己的控制端口 `47863` 已被占用（说明已有
  一个在跑），会直接退出，不会有多个实例互相打架。
- 这个方法只能验证"代码跑起来会不会报错、日志/print 输出是什么"，UI 视觉效果（弹窗
  是否正常显示、控件排布好不好看）还是要用户自己盯着 DCC 窗口确认。
- **发进去的代码里绝对不要用 `raise SystemExit(...)` / `sys.exit(...)` 之类退出调用来"把结果带出来"**
  （比如想偷懒把要看的值塞进异常信息里传回来）。会把 commandPort 的监听/处理线程直接弄死：DCC 本体
  进程不会崩、还在正常跑，但那之后这个 TCP 端口就再也连不上了（`ConnectionRefusedError`），必须
  重启 DCC、让启动脚本重新 `import commandPort` 才能恢复。想要回传结果，用
  `raise ValueError(str(...))`（或其它普通 Exception）走 traceback 通道带回来，不要用 SystemExit。
- 如果 Bash 命令偶发报一个跟具体命令内容无关、看起来是环境本身的 `add_item ... fatal error`，
  跟这套脚本无关，重跑一次通常就好了。
- **3ds Max 专属**：改动场景（建物体、改属性、改材质等）的 `max exec` 命令结尾要加一条刷新视口的
  命令（`pymxs.runtime.redrawViews()`），否则改动要等下一次视口重绘才会显示出来，容易让人误以为
  命令没生效。
- **连接失败自愈**：进程在运行但连不上，大概率是这个实例没装 commandPort——静默按
  [`dcc_mcp_debug/plugin/安装说明.md`](dcc_mcp_debug/plugin/安装说明.md) 补装（不问、不额外通知），装完不用在同一个
  任务里重试连接，直接告诉用户"连接失败，请重启 <DCC 名称>"。
