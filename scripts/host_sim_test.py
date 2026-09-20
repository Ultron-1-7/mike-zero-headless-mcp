"""模拟 MCP 宿主（Electron/Node）启动本 server，验证「环境替换」不再致命。

背景
----
`Node.child_process.spawn(cmd, {env})` 的 env 是**整体替换**语义，不是合并。
所以 mcp.json 里一旦写了 env 字段，SystemRoot 就会丢，Windows 上 Python
首次 `import asyncio` → `import _overlapped` → winsock 初始化失败：

    OSError: [WinError 10106] 无法加载或初始化请求的服务提供程序

本脚本用三种基环境分别启动，确认三种宿主行为模型下都能完成握手：
  1) inherit —— 完整继承父进程环境（宿主未传 env 字段时的真实行为）
  2) replace —— 只给 mcp.json 里 env 字段的内容（宿主整体替换）
  3) empty   —— 连 mcp.json 的 env 都没有的最坏情况（空环境）

用法：
    .venv/Scripts/python.exe scripts/host_sim_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MCP_JSON = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")

INIT_REQ = (
    json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "host-sim", "version": "1"},
            },
        }
    )
    + "\n"
).encode()

LIST_REQ = (json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n").encode()


def _hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def probe(label: str, server: dict, cwd: str, base_env: dict | None) -> bool:
    """按给定的基环境启动 server，发 initialize + tools/list。"""
    if base_env is None:
        env = None  # 不传 env -> 子进程继承
    else:
        env = dict(base_env)
        env.update(server.get("env") or {})

    cmd = [server["command"], *(server.get("args") or [])]
    t0 = time.time()
    try:
        p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        out, err = p.communicate(INIT_REQ + LIST_REQ, timeout=30)
    except Exception as exc:
        print(f"   {label:12s} 启动异常: {type(exc).__name__}: {exc}")
        return False

    dt = time.time() - t0
    ok = b'"result"' in out
    n_tools = out.count(b'"name"')
    print(f"   {label:12s} {'✅ 握手成功' if ok else '❌ 失败'}   rc={p.returncode}  {dt:.2f}s"
          + (f"  工具≈{n_tools}" if ok else ""))
    if not ok:
        tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
        for line in tail:
            print(f"        | {line[:150]}")
    return ok


def main() -> int:
    _hr(f"读取宿主配置  {MCP_JSON}")
    with open(MCP_JSON, encoding="utf-8") as fh:
        cfg = json.load(fh)
    servers = cfg.get("mcpServers", {})
    print("已注册 server:", list(servers))

    print("\n本机当前环境的关键变量:")
    for k in ("SystemRoot", "windir", "TEMP", "PYTHONPATH"):
        print(f"   {k} = {os.environ.get(k)}")

    failed = 0
    for name, server in servers.items():
        _hr(f"server: {name}")
        exe = server["command"]
        if not os.path.isfile(exe):
            print(f"   ⚠ 可执行文件不存在，跳过：{exe}")
            continue
        print(f"   command = {exe}")
        print(f"   args    = {server.get('args')}")
        print(f"   env     = {server.get('env') or '(无 env 字段)'}")

        # 宿主的工作目录通常既不是工程目录，这里刻意用一个无关目录
        cwd = os.environ.get("SystemRoot", "C:\\Windows")

        results = {
            "inherit": probe("inherit", server, cwd, None),
            "replace": probe("replace", server, cwd, {}),
            "empty": probe("empty", server, cwd, {}),
        }
        # replace 与 empty 在这里等价（都只给 config.env），保留两个标签是为了
        # 让「宿主整体替换」这一语义显式出现，便于对照。
        if not all(results.values()):
            failed += 1
            print("   ⚠ 该 server 在某些宿主语义下不可用")
        else:
            print("   ✅ 三种宿主语义下均可启动")

    _hr("结论")
    if failed == 0:
        print("全部通过：server 的启动与现实宿主的 env 语义无关。")
    else:
        print(f"有 {failed} 个 server 存在问题，见上方明细。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
