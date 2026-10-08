// DCC-MCP-Debug: 以 Maya 模块形式加载 commandPort 连接器。
// 放在 <Maya 安装目录>\modules\ 下；同目录树里 plug-ins\commandPort\scripts\userSetup.py
// 会在 Maya 启动时执行（`scripts:` 把它挂进 MAYA_SCRIPT_PATH，`PYTHONPATH+:=` 再挂一份到 sys.path）。
// 语法对照 Maya 自带的 modules\sweep.mod / MASH.mod。
// 卸载：删掉本文件、plug-ins\commandPort 目录，以及 site-packages 里的 commandPort.*.pyd。
+ commandPort 1.0 ../plug-ins/commandPort
scripts: scripts
PYTHONPATH+:=scripts
