"""MIKE Zero MCP Server —— 用 MCP 无界面驱动 DHI MIKE Zero。

运行：
    python -m mikezero_mcp.server

说明：MIKE Zero 官方并没有叫 "Commander" 的组件；真正的无界面入口是
MzLaunch.exe（模拟）、ToolboxShell.exe（工具箱）和 MzPlotCompApp.exe（出图）。
本 server 把这三个入口 + mikeio 结果读取封装成 MCP 工具。
"""

from __future__ import annotations

import os
from typing import Any

from . import dfs, jobs, pfs
from . import __version__
from .locate import Installation, find_installations, pick_installation

try:
    # mcp >= 2.0 把 FastMCP 改名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:
    try:
        # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "缺少 mcp SDK。请先安装：\n"
            "  <工程>/.venv/Scripts/python.exe -m pip install mcp"
        ) from exc

try:
    mcp = _Server("mikezero", version=__version__)
except TypeError:  # 老版本 SDK 不接受 version 参数
    mcp = _Server("mikezero")

_SUMMARY_KEYS = (
    "ok", "job", "id", "kind", "status", "setup", "exit_code", "elapsed_seconds",
    "note", "error", "error_lines", "new_results", "log_files", "cmd", "pid",
)


# ------------------------------------------------------------------ 内部工具


def _resolve_install(year: str | None = None) -> Installation | None:
    """取出一个 MIKE Zero 安装，并把 setup 扩展名注册给文件引用识别。"""
    inst = pick_installation(year)
    if inst is None:
        return None
    try:
        engine_map = pfs.engine_map_from_file(inst.engines_cfg) if inst.engines_cfg else {}
        pfs.register_setup_extensions(set(engine_map))
    except Exception:
        pass
    return inst


def _no_install_error(year: str | None) -> dict:
    return {
        "ok": False,
        "error": "未在本机找到 MIKE Zero 安装"
        + (f"（指定年份 {year}）" if year else ""),
        "hint": "确认已安装 MIKE Zero，且环境变量 DHI_MIKE_<年份> 指向 ...\\bin\\x64。",
        "checked": [i.to_dict() for i in find_installations()],
    }


def _engine_map(inst: Installation) -> dict:
    if not inst.engines_cfg:
        return {}
    try:
        return pfs.engine_map_from_file(inst.engines_cfg)
    except Exception:
        return {}


# ------------------------------------------------------------------ 工具


@mcp.tool()
def mz_env(year: str | None = None) -> dict:
    """探测本机 MIKE Zero 环境：安装位置、bin/x64、无界面入口、引擎数量、mikeio 可用性。

    Args:
        year: 可选，指定 MIKE Zero 年份（如 "2023"）；不传则取最新安装。
    """
    installs = find_installations()
    chosen = pick_installation(year) or (installs[0] if installs else None)

    payload: dict[str, Any] = {
        "ok": True,
        "installations": [i.to_dict() for i in installs],
        "selected": chosen.to_dict() if chosen else None,
        "mikeio": dfs.available(),
        "state_dir": str(jobs.STATE_DIR),
    }

    if chosen:
        engine_map = _engine_map(chosen)
        payload["engines"] = {
            "total": len(engine_map),
            "extensions": sorted(engine_map),
        }
        payload["headless_entry_points"] = {
            k: v for k, v in chosen.binaries.items()
        }
        examples = os.path.join(chosen.root, "Examples")
        if os.path.isdir(examples):
            payload["examples_dir"] = examples
    else:
        payload["ok"] = False
        payload["error"] = "未找到 MIKE Zero 安装"

    return payload


@mcp.tool()
def mz_engines(extension: str | None = None, year: str | None = None) -> dict:
    """列出 MIKE Zero 的 扩展名 -> 引擎 映射（来自 MzEngines.cfg）。

    Args:
        extension: 可选，只看某个扩展名，如 ".m21fm"。
        year: 可选，指定 MIKE Zero 年份。
    """
    inst = _resolve_install(year)
    if inst is None:
        return _no_install_error(year)

    engine_map = _engine_map(inst)
    if not engine_map:
        return {"ok": False, "error": f"未能解析 {inst.engines_cfg}"}

    norm = extension.lower() if extension else None
    if norm and not norm.startswith("."):
        norm = "." + norm

    entries = [
        e.to_dict() for ext, e in sorted(engine_map.items())
        if norm is None or ext == norm
    ]
    return {
        "ok": True,
        "year": inst.year,
        "config": inst.engines_cfg,
        "count": len(entries),
        "entries": entries,
    }


@mcp.tool()
def mz_scan(root: str, max_files: int = 5000, year: str | None = None) -> dict:
    """扫描一个工程目录，按类型归类 MIKE Zero 的 setup 文件与结果文件。

    Args:
        root: 要扫描的目录（绝对路径）。
        max_files: 最多扫描多少个文件，默认 5000。
        year: 可选，指定 MIKE Zero 年份以确定 setup 扩展名。
    """
    inst = _resolve_install(year)
    if inst is None:
        return _no_install_error(year)

    engine_map = _engine_map(inst)
    setup_exts = set(engine_map) | {
        ".mzp", ".pfs", ".lyt", ".bw", ".m21c", ".st2", ".mhydro", ".sim11",
    }
    result = pfs.scan_directory(root, setup_exts, max_files=max_files)
    if result.get("ok"):
        result["year"] = inst.year
    return result


@mcp.tool()
def mz_read_setup(setup: str, year: str | None = None) -> dict:
    """解析一个 MIKE Zero setup 文件（PFS 格式），返回引擎、段、文件引用、关键参数。

    Args:
        setup: setup 文件绝对路径，例如 .m21fm / .sim11 / .she / .couple / .mzt。
        year: 可选，指定 MIKE Zero 年份。
    """
    inst = _resolve_install(year)
    engine_map = _engine_map(inst) if inst else {}
    return pfs.summarize_setup(setup, engine_map)


@mcp.tool()
def mz_run(
    setup: str,
    engine: str | None = None,
    direct: bool = False,
    engine_flag: str = "-e",
    silent: bool = False,
    exit_when_done: bool = True,
    mpi: int | None = None,
    gpu: int | None = None,
    priority: int | None = None,
    extra_args: list[str] | None = None,
    result_dir: str | None = None,
    year: str | None = None,
    wait: bool = False,
    timeout: float | None = None,
) -> dict:
    """无界面启动一次 MIKE Zero 模拟，返回作业 id。

    默认后台运行，用 mz_progress / mz_job_status / mz_job_log 跟踪进度。

    两种入口（实测 MIKE Zero 2023）：
      * direct=False（默认）走 MzLaunch.exe：支持 -e/-mpi/-gpu/-y/-z，
        但**模拟结束后会删掉 `._spi_` 进度文件**（本服务会先归档）。
      * direct=True 直接调引擎 exe（如 MIKE11.exe / FemEngineHD.exe）：开关按引擎
        自己的语法拼装，`._spi_` 跑完仍保留。不支持 -mpi/-gpu/-z。

    Args:
        setup: setup 文件绝对路径。
        engine: 可选，显式指定引擎 exe（如 MIKE11.exe）；不传则由 MzEngines.cfg
            按扩展名自动选择。
        direct: True 时绕过 MzLaunch，直接调用引擎。
        engine_flag: 走 MzLaunch 时指定引擎的参数名，默认 "-e"。
        silent: 走 MzLaunch 时传未文档化的 -run（实测可省，非必要不要开）。
        exit_when_done: 走 MzLaunch 时传 -x，模拟结束后退出启动器。
        mpi: 子域数，传 -mpi N（需该引擎支持 MPI）。
        gpu: GPU 卡数，传 -gpu N。
        priority: CPU 优先级，传 -y 1..4（1 低，4 高于正常）。
        extra_args: 追加的原始命令行参数。
        result_dir: 走 MzLaunch 时传 -z 指定结果目录。
        year: 可选，指定 MIKE Zero 年份。
        wait: 是否同步等待结束（长时间模拟建议保持 False）。
        timeout: wait=True 时的等待上限（秒）。
    """
    inst = _resolve_install(year)
    if inst is None:
        return _no_install_error(year)
    if not direct and not inst.binaries.get("mzlaunch"):
        return {"ok": False, "error": f"在 {inst.bin64} 未找到 MzLaunch.exe"}

    return jobs.registry.start_simulation(
        inst, setup, engine=engine, direct=direct, engine_flag=engine_flag,
        silent=silent, exit_when_done=exit_when_done, mpi=mpi, gpu=gpu,
        priority=priority, extra_args=extra_args, result_dir=result_dir,
        wait=wait, timeout=timeout,
    )


@mcp.tool()
def mz_progress(job_id: str) -> dict:
    """读某个作业的实时进度：百分比、当前模拟时刻、步数、计算速度、剩余时间。

    数据来自引擎写的 `<setup名>._spi_`（例如 `PROGRE 1000` 表示完成）。
    若原文件已被 MzLaunch 删除，则读本服务归档的副本。

    Args:
        job_id: mz_run 返回的作业 id。
    """
    return jobs.registry.progress(job_id)


@mcp.tool()
def mz_run_batch(
    setups: list[str],
    workdir: str | None = None,
    engine: str | None = None,
    engine_flag: str = "-e",
    mpi: int | None = None,
    gpu: int | None = None,
    priority: int | None = None,
    extra_args: list[str] | None = None,
    year: str | None = None,
    wait: bool = False,
    timeout: float | None = None,
) -> dict:
    """批量顺序执行多个 setup：生成 .bat（DHI 官方做法）并后台运行。

    生成的 .bat 会保留在 workdir 里，可自行查看或手工重跑。

    Args:
        setups: setup 文件绝对路径列表，按顺序执行。
        workdir: 工作目录，默认取第一个 setup 所在目录。
        engine / engine_flag / mpi / gpu / priority / extra_args: 同 mz_run。
        year: 可选，指定 MIKE Zero 年份。
        wait: 是否同步等待整批结束。
        timeout: wait=True 时的等待上限（秒）。
    """
    inst = _resolve_install(year)
    if inst is None:
        return _no_install_error(year)

    return jobs.registry.start_batch(
        inst, setups, workdir=workdir, engine=engine, engine_flag=engine_flag,
        mpi=mpi, gpu=gpu, priority=priority, extra_args=extra_args,
        wait=wait, timeout=timeout,
    )


@mcp.tool()
def mz_toolbox(
    setup: str,
    extra_args: list[str] | None = None,
    year: str | None = None,
    wait: bool = False,
    timeout: float | None = None,
) -> dict:
    """用 ToolboxShell.exe 无界面运行一个 Toolbox setup（.mzt / .21t / .3t / .lpkt / .mst）。

    Args:
        setup: toolbox setup 文件绝对路径。
        extra_args: 追加参数。
        year: 可选，指定 MIKE Zero 年份。
        wait: 是否同步等待。
        timeout: wait=True 时的等待上限（秒）。
    """
    inst = _resolve_install(year)
    if inst is None:
        return _no_install_error(year)
    if not inst.binaries.get("toolboxshell"):
        return {"ok": False, "error": f"在 {inst.bin64} 未找到 ToolboxShell.exe"}

    return jobs.registry.start_toolbox(inst, setup, extra_args=extra_args, wait=wait, timeout=timeout)


@mcp.tool()
def mz_jobs(limit: int = 20) -> dict:
    """列出最近的作业（模拟 / 批量 / 工具箱）。

    Args:
        limit: 最多返回多少条，默认 20。
    """
    return jobs.registry.list_jobs(limit=limit)


@mcp.tool()
def mz_job_status(job_id: str) -> dict:
    """查询作业状态：进程是否存活、耗时、日志文件、报错行、新增结果文件。

    Args:
        job_id: mz_run / mz_run_batch / mz_toolbox 返回的作业 id。
    """
    return jobs.registry.get(job_id)


@mcp.tool()
def mz_job_log(job_id: str, lines: int = 200) -> dict:
    """读取作业日志末尾若干行（MIKE Zero 把进度写在结果目录的 .log 里）。

    Args:
        job_id: 作业 id。
        lines: 读取末尾行数，默认 200。
    """
    return jobs.registry.tail(job_id, lines=lines)


@mcp.tool()
def mz_job_kill(job_id: str, force: bool = True) -> dict:
    """终止一个作业（连同其子进程，用 taskkill /T）。

    Args:
        job_id: 作业 id。
        force: 是否强制结束，默认 True。
    """
    return jobs.registry.kill(job_id, force=force)


@mcp.tool()
def mz_dfs_info(path: str) -> dict:
    """读取结果文件元信息：类型、条目名称/单位、时间轴、网格规模。

    支持 dfs 系列（.dfs0/.dfs1/.dfs2/.dfs3/.dfsu，走 mikeio）
    以及 MIKE 11 / 1D 结果（.res1d/.res11，走 mikeio1d）。

    Args:
        path: 结果文件绝对路径。
    """
    return dfs.info(path)


@mcp.tool()
def mz_dfs_read(
    path: str,
    item: str | int | None = None,
    start: str | None = None,
    end: str | None = None,
    max_points: int = 500,
) -> dict:
    """读取结果数据：统计量（最大/最小/均值）与抽样后的序列。

    数据量大时自动抽样到 max_points 个点，避免撑爆上下文。
    对 1D 结果（.res1d/.res11），item 可传量名（如 "Water Level"，返回该量的前几列）
    或具体列名（如 "Water Level:CALI:0"，即 量:河段:里程）。

    Args:
        path: 结果文件绝对路径。
        item: dfs：条目名或序号。1D：量名 / 列名 / 序号。不传则取第一项。
        start: 起始时间，如 "1989-07-27 00:00"。
        end: 结束时间。
        max_points: 序列抽样点数上限，默认 500。
    """
    return dfs.read(path, item=item, start=start, end=end, max_points=max_points)


@mcp.tool()
def mz_dfs_plot(path: str, item: str | int | None = None, out_png: str | None = None) -> dict:
    """把结果序列画成 PNG 图片，返回图片路径。支持 dfs 系列与 1D 结果。

    Args:
        path: 结果文件绝对路径。
        item: 条目名 / 量名 / 序号；不传则取第一项。
        out_png: 输出 PNG 路径；不传则与结果文件同目录同名。
    """
    return dfs.plot(path, item=item, out_png=out_png)


def main() -> None:
    """以 stdio 方式启动 MCP server。"""
    jobs.STATE_DIR.mkdir(parents=True, exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
