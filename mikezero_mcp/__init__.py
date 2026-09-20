"""mikezero-mcp — 用 MCP 无界面驱动 DHI MIKE Zero。

模块划分：
    locate  定位 MIKE Zero 安装、bin/x64 与各无界面入口
    pfs     解析 MzEngines.cfg 与 PFS 格式的 setup 文件
    jobs    通过 MzLaunch.exe 启动模拟并管理作业
    dfs     基于 mikeio 读取 dfs 结果（可选依赖）
    server  MCP server 与工具集
"""

# --------------------------------------------------------------------------
# Windows 环境自愈（必须在导入任何第三方库之前执行）
# --------------------------------------------------------------------------
# MCP 宿主（Electron/Node）用 spawn(cmd, {env}) 启动 stdio server 时，传入的
# env 是【整体替换】而不是【合并】——这是 Node.child_process.spawn 的既定语义。
# 所以只要 mcp.json 里写了 env 字段，SystemRoot 就会丢失；而 Windows 上
# Python 第一次 import asyncio 会连带 import _overlapped，winsock 初始化拿不到
# SystemRoot 就抛：
#     OSError: [WinError 10106] 无法加载或初始化请求的服务提供程序
# 整个 server 在导入阶段直接崩掉，宿主侧的表现就是“连接失败 / 出错了”。
#
# 这里在包初始化时（即 `python -m mikezero_mcp.server` 的最早时刻）把关键系统
# 变量补回去，使 server 的启动与「宿主是否替换环境」完全无关。
import os as _os

if _os.name == "nt":  # pragma: no cover - 仅在 Windows 生效
    _sysdrive = (_os.environ.get("SystemDrive") or "C:").rstrip("\\/")
    _win = _sysdrive + "\\Windows"
    _os.environ.setdefault("SystemRoot", _win)
    _os.environ.setdefault("windir", _win)
    _os.environ.setdefault("TEMP", _win + "\\Temp")
    _os.environ.setdefault("TMP", _os.environ["TEMP"])
    del _sysdrive, _win
del _os

__version__ = "1.0.1"
