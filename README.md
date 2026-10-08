# DCC-MCP-Debug

> 一个**attach 到正在运行的 MotionBuilder / 3ds Max / Maya 进程**，直接往 DCC 内嵌 Python 里注入代码、并能断点调试的轻量 MCP 工具。

通过 DCC 自带的 `commandPort`（TCP 端口号 = DCC 进程 PID），从外部 shell / AI agent 把 Python 代码**送进正在运行的 DCC 进程**执行，拿到 stdout 和 traceback；同时内置 debugpy/DAP，能在 DCC 进程里直接下断点。

**不需要重启 DCC、不需要在 DCC 里挂常驻 host、不需要 Rust、不需要装 `mcp` 包**——纯 Python 标准库，`pip install` 完配一行 MCP 客户端配置就能用。

---

## 和 [dcc-mcp/dcc-mcp-core](https://github.com/dcc-mcp/dcc-mcp-core) 的关系

[dcc-mcp-core](https://github.com/dcc-mcp/dcc-mcp-core) 是一个**面向工作室的多 DCC 生态控制面**：38 个 host adapter、Rust/PyO3 内核、主线程调度、声明式 Recipes、Marketplace、网关路由——它解决的是"怎么让 AI 统一操作一整套 DCC"。

**DCC-MCP-Debug 不重复造这部分**。它解决的是另一件事：

> 已经打开的 DCC 进程（用户正在里面调动画、摆场景），怎么从外部把一段代码实际跑进去看结果、并能在出问题时下断点。

| 维度 | dcc-mcp-core | DCC-MCP-Debug |
|---|---|---|
| **典型用法** | AI 通过 host adapter 操作 DCC，host 随 DCC 启动加载 | AI attach 到**已经在跑**的 DCC 进程，发代码、读结果 |
| **接入方式** | 每个 DCC 一个 adapter / 网关，随 DCC 启动 | 安装一次 commandPort 连接器，之后随时连 |
| **要不要重启 DCC** | 新 adapter / 新工具通常要重启 DCC 加载 | **完全不需要**，连上就能发 |
| **实时断点调试** | 无 | **内置 debugpy/DAP attach**，多实例各占端口 |
| **语言 / 依赖** | Rust + PyO3 + Python + `mcp` 包 | **纯 Python 标准库**，不装 `mcp` 包也能跑 |
| **MotionBuilder 2020（Py2.7）** | 不支持 | 完整支持，Py2.7 / Py3 包装器同源 |
| **架构重量** | 网关 + 多 adapter + Recipe runtime | 一个常驻 daemon（127.0.0.1:47863）+ 一个 CLI |
| **不用 MCP 能不能用** | 基本必须走 MCP host | `dcc-cli exec -c "..."` 命令行直接用 |
| **覆盖 DCC 数量** | 38+（Maya/Blender/Houdini/C4D/Nuke/ZBrush…） | MotionBuilder / 3ds Max / Maya（commandPort 系） |
| **典型场景** | 工作室管线、批量资产处理、host 标准化 | 个人 TA / 工具开发时"改完代码立刻跑进去看报错" |

**一句话**：core 是"AI 操作 DCC 的高速公路"；DCC-MCP-Debug 是"你正在 DCC 里干活时，AI 把你刚改的脚本直接送进去试跑、还能断点"。两者可以共存——core 做生产管线，这个做日常开发验证。

---

## 为什么单独做这个

dcc-mcp-core 这类 host 框架在"启动期加载、主线程调度、工具标准化"上很强，但有几个场景它不直接覆盖：

1. **用户已经开着 DCC 在干活**，不想为了让 AI 试一段脚本就重启 DCC、重新加载场景。
2. **MotionBuilder 2020 还是 Python 2.7**，core 这种现代 host 框架基本不覆盖；而 commandPort 是 Mobu 原生能力。
3. **需要断点调试** DCC 内嵌解释器里的代码——core 没有 DAP 通道。
4. **轻量**：不想为了跑 3 行 `pymxs.runtime.box()` 就起一个 Rust 网关。

这套工具就是针对这四个坑做的，每个坑都对应到代码里的具体设计。

---

## 特性

### 1. 运行时 attach，不重启 DCC

- 连接器装好后，DCC 进程监听一个 TCP 端口，**端口号 = 进程 PID**。
- 外部发一段文本过去，DCC 内嵌 Python 直接 `exec()`。
- DCC 重启后 PID 变了，下次连接自动重新定位，不需要手动改配置。

### 2. 内置 debugpy 调试（DAP）

- `dcc_select` 选定实例时自动在该进程启动 debugpy，attach 后 VSCode / 调试客户端直接连。
- 每个实例独立调试端口，多实例互不抢。
- 客户端退出自动释放端口，不会被一直占着。

### 3. 零依赖、纯标准库

- 不依赖 `mcp` 包，不依赖 Rust，不依赖 numpy。
- `pip install git+...` 完直接能用。
- MCP server 模式和纯 CLI 模式同一份代码。

### 4. 一个 daemon，多 DCC 路由

- 常驻服务只监听 `127.0.0.1:47863` 一个端口。
- 请求带 `{"dcc": "max" | "mobu" | "maya"}` 标签，服务端按标签转发到对应连接。
- 多实例（同 DCC 多个窗口）用 PID 区分，`dcc_select(pid)` 记住当前操作哪个。

### 5. 一堆真实踩过的坑（都修了）

- **连接活性检测**：先在 socket 上发 1 字节 `"\n"` ping（几毫秒），失败才去 `tasklist` 找 PID——避免每次 exec 都起子进程。
- **Py2.7 / Py3 兼容**：`__file__` shim、stdout 收集器按版本分支、traceback 去包装器栈帧。
- **`SystemExit` 禁入**：工具描述里直接写明，防止把 commandPort 监听线程搞死。
- **3ds Max 专属**：改完场景自动提示要 `redrawViews()`。
- **进程扫描缓存 3 秒**：`tasklist` 单次 300ms，缓存后 exec 从 570ms 降到 27ms。
- **静默自动装连接器**：连接失败时自动用 mobupy/3dsmaxpy/mayapy 问出版本、挑对应 `.pyd`。

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
```

MCP 客户端配置（Claude Code / Cursor / 任意 stdio MCP 客户端）：

```json
{
  "mcpServers": {
    "DCC-MCP-Debug": { "command": "dcc-mcp-debug" }
  }
}
```

或一行命令注册到 Claude Code：

```bash
claude mcp add --scope user DCC-MCP-Debug -- dcc-mcp-debug
```

装好后 AI 客户端直接拿到 12 个工具：`dcc_exec` / `dcc_reload_modules` / `dcc_instances` / `dcc_select` / `dcc_status` / `dcc_install_connector` / `dcc_debug_start_server` / `dcc_debug_connect` / `dcc_debug_read` / `dcc_debug_status` / `dcc_debug_clear` / `dcc_debug_disconnect`。

---

## 支持的 DCC

| DCC | 连接器 | Python 版本 |
|---|---|---|
| 3ds Max | commandPort（`.pyd` + `.ms`） | 3.7 / 3.9 / 3.10 / 3.11 |
| MotionBuilder | commandPort（`.pyd` + `.py`） | 2.7（Mobu 2020）/ 3.10+ |
| Maya | commandPort（`.mod` + `userSetup.py`） | 3.7 / 3.9 / 3.10 / 3.11 |

新 DCC 只要走"TCP 端口 = PID、收到文本 `exec()`"这种 commandPort 机制，在 `dcc_bridge.DCC_REGISTRY` 加一行标签映射就能接入。

---

## 边界（不是什么）

- 不做"AI 操作 DCC 的 host 框架"——那是 [dcc-mcp-core](https://github.com/dcc-mcp/dcc-mcp-core) 的事。
- 不做批量资产管线、不做主线程调度、不做 Recipe/Marketplace。
- 只解决一件事：**你正在 DCC 里干活时，让外部代码立刻跑进去、拿到结果、能断点**。

---

## License

MIT
