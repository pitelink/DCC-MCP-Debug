# DCC-MCP-Debug

> 一个**attach 到正在运行的 MotionBuilder / 3ds Max / Maya 进程**，直接往 DCC 内嵌 Python 里注入代码、并能断点调试的轻量 MCP 工具。

通过 DCC 自带的 `commandPort`（TCP 端口号 = DCC 进程 PID），从外部 shell / AI agent 把 Python 代码**送进正在运行的 DCC 进程**执行，拿到 stdout 和 traceback；同时内置 debugpy/DAP，能在 DCC 进程里直接下断点。

**不需要重启 DCC、不需要在 DCC 里挂常驻 host、不需要 Rust、不需要装 `mcp` 包**——纯 Python 标准库，`pip install` 完配一行 MCP 客户端配置就能用。

---

## 它解决什么问题

写 DCC 工具 / 绑定 / Pipeline 脚本时最烦的循环是：

> 改一行 Python → 切到 DCC → 手动 reload → 跑一遍 → 看报错 → 切回编辑器。

如果还想断点，得另起一个 VSCode 调试配置，attach 到 DCC 内嵌解释器，配路径映射，经常半天连不上。

**DCC-MCP-Debug 把这个循环压缩成一次工具调用**：

```bash
dcc-cli mobu exec -c "import my_module; my_module.run()"
```

报错直接以 traceback 形式回到终端 / AI 对话里，行号、源码、变量状态都在。需要断点时：

```bash
dcc-cli mobu select <PID>   # 自动在该进程起 debugpy，VSCode 直接连
```

---

## 它是什么

**DCC-MCP-Debug 让 AI 通过自然语言对话直接操作你正在跑的 DCC**：装好一次连接器后，AI 自动发现本机已启动的 DCC 实例，把你说的代码 / 脚本直接发进去执行，拿回 stdout 和 traceback；出问题时 AI 能直接在 DCC 进程里下断点调试。

不重启 DCC、不挂常驻 host、不依赖重型运行时——纯 Python 标准库，`pip install` 完配一行 MCP 配置就能让 Claude Code / Cursor 等 AI 客户端接管。

---

## 核心特性

### 1. 运行时 attach，不重启 DCC

连接器装好一次之后，**任何时候**只要 DCC 进程在跑，就能从外部把代码送进去。

- 端口号 = DCC 进程 PID，不需要配 IP、不需要手动开监听。
- DCC 重启后 PID 变了，下次连接自动重新定位，配置零改动。
- 用户正在 DCC 里调动画、摆场景时，你也能往里面试代码——不打断他。

### 2. 内置 debugpy 实时调试

不是"能跑"，是"能在 DCC 进程里断点"。

- `dcc_select` 选定实例时**自动在该进程启动 debugpy**，已有端口就复用。
- 每个实例独立调试端口，多开几个 DCC 窗口同时调也不抢。
- 客户端退出自动释放端口，不会一直占着导致 VSCode 连不上。
- attach 自动带 `redirectOutput: True`，DCC 里的 `print` 能正常进调试通道。

### 3. 零依赖，纯 Python 标准库

- **不依赖 `mcp` 包**、不依赖 Rust、不依赖 numpy / pydantic。
- 装完就是两个命令行入口：`dcc-mcp-debug`（MCP server）和 `dcc-cli`（直接命令行用）。
- 连不了 MCP 客户端时，`dcc-cli exec` 自己就能干活。

### 4. 一个 daemon，多 DCC 路由

- 常驻服务只监听 `127.0.0.1:47863` 一个端口。
- 请求带 `{"dcc": "max" | "mobu" | "maya"}` 标签，服务端按标签转发到对应连接。
- 多实例（同 DCC 多个窗口 / 多个版本）按 PID 区分，`dcc_select(pid)` 记住"当前操作哪个"，切换不断开任何连接。
- 状态住在 daemon 里，但**生命周期跟着客户端走**：新会话不继承上一个会话的选择，不会出现"莫名其妙连到另一个窗口"。

### 5. 一堆真实踩过的坑，都修了

这些不是 README 宣传语，是代码里实打实的设计：

- **连接活性检测两层**：先在 socket 上发 1 字节 `"\n"` ping（几毫秒），失败才去 `tasklist` 找 PID——exec 从 570ms 降到 27ms。
- **MotionBuilder 2020 还是 Python 2.7**：exec 包装器 Py2 / Py3 同源，`builtins.__mcp_globals__` 跨连接保状态，stdout 收集器按版本分支，traceback 去包装器栈帧。
- **禁用 `SystemExit`**：工具描述里直接写明——用 `sys.exit()` 会把 commandPort 监听线程搞死，DCC 不崩但端口再也连不上。
- **3ds Max 专属**：改完场景必须 `redrawViews()`，工具描述内置提醒。
- **自动装连接器**：连接失败时自动用 `mobupy` / `3dsmaxpy` / `mayapy` 问出版本和 sys.path，挑对应 `.pyd`、选对 site-packages 层——不用查版本对照表。
- **单实例 daemon**：端口已占用时直接退出，不会有多个实例互相打架。

---

## 快速上手

```bash
pip install git+https://github.com/pitelink/DCC-MCP-Debug.git
```

CLI（不装 MCP 也能用）：

```bash
# 自动嗅探当前在跑的 DCC
dcc-cli status

# 直接在 3ds Max 里跑一行 Python
dcc-cli max exec -c "import pymxs; pymxs.runtime.box()"

# 跑一个脚本文件
dcc-cli mobu exec "C:/path/to/script.py"

# 从 stdin 读代码（方便管道）
cat my_script.py | dcc-cli mobu exec -
```

MCP 客户端配置（Claude Code / Cursor / 任意 stdio MCP 客户端）：

```json
{
  "mcpServers": {
    "DCC-MCP-Debug": { "command": "dcc-mcp-debug" }
  }
}
```

一行注册到 Claude Code：

```bash
claude mcp add --scope user DCC-MCP-Debug -- dcc-mcp-debug
```

装好后 AI 客户端直接拿到 12 个工具：

| 类别 | 工具 |
|---|---|
| 执行 | `dcc_exec`（code / file / stdin 三种模式）、`dcc_reload_modules` |
| 实例 | `dcc_instances`、`dcc_select`、`dcc_status` |
| 连接器 | `dcc_install_connector` |
| 调试 | `dcc_debug_start_server`、`dcc_debug_connect`、`dcc_debug_read`、`dcc_debug_status`、`dcc_debug_clear`、`dcc_debug_disconnect` |

---

## 支持的 DCC

| DCC | 连接器 | Python 版本 |
|---|---|---|
| 3ds Max | commandPort（`.pyd` + `.ms`） | 3.7 / 3.9 / 3.10 / 3.11 |
| MotionBuilder | commandPort（`.pyd` + `.py`） | **2.7（Mobu 2020）** / 3.10+ |
| Maya | commandPort（`.mod` + `userSetup.py`） | 3.7 / 3.9 / 3.10 / 3.11 |

连接器 `.pyd` 随包分发，`pip install` 完就在 site-packages 里，不需要自己去找文件。自动安装脚本会调目标 DCC 自带解释器问版本、挑对应 `.pyd`、选对 site-packages 层。

接入新 DCC（只要它也走"TCP 端口 = PID、收到文本 `exec()`"这种 commandPort 机制），在 `dcc_bridge.DCC_REGISTRY` 加一行标签映射就能接入，不用碰连接管理本身。

---

## 路线图

其他 DCC 软件（Blender、Houdini 等）还在适配中。

---

## 边界（诚实说明）

- 这是**个人 TA / 工具开发时的"改完立刻跑进去看报错"工具**，不是工作室级的多 DCC host 框架。
- 不做批量资产管线、不做主线程调度封装、不做 Recipe / Marketplace。
- 视觉效果（弹窗对不对、控件排布好不好看）还是要人眼看——这个工具只能告诉你"代码跑没跑、报了什么错"。

---

## License

MIT
