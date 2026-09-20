"""集成测试：通过 MCP 工具函数真实跑模拟，验证作业/进度/结果全链路。

覆盖：
  1. 工具清单（async）
  2. mz_run  direct=False（MzLaunch）→ mz_progress 轮询 → mz_job_status
  3. mz_run  direct=True （直调 MIKE11.exe）→ mz_progress 轮询
  4. SPI / SimStat 解析正确性、结果目录发现
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mikezero_mcp import server, spi  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(HERE, "demo", "mike11_smoke")
SETUP = os.path.join(WORK, "Test1.sim11")
RF_DIR = os.path.join(WORK, "Test1.sim11 - Result Files")
TRASH = os.path.join(HERE, "demo", "_trash")


def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def _retire(path: str) -> None:
    """把文件/目录挪进 _trash。

    不用 os.remove/os.rmtree：某些环境给它们挂了 safe-delete shim（改成回收站操作，
    可能直接抛 OSError）。重命名不受影响。
    """
    if not os.path.exists(path):
        return
    os.makedirs(TRASH, exist_ok=True)
    dst = os.path.join(TRASH, f"{os.path.basename(path)}.{time.time_ns()}")
    try:
        os.replace(path, dst)
    except OSError:
        pass


def clean() -> None:
    for n in ("Test1.Log", "Test1-Info.Log", "Test1-SimStat.Log", "Test1._spi_",
              "Test1.res11", "Test1.sim11.apv.log", "Test1VolumeBalance.HTML"):
        _retire(os.path.join(WORK, n))
    _retire(RF_DIR)
    # 清掉历史作业记录，避免混淆
    jd = os.path.join(HERE, "state", "jobs")
    if os.path.isdir(jd):
        for f in os.listdir(jd):
            if f.endswith((".json", ".spi", ".out")):
                _retire(os.path.join(jd, f))


def wait_job(job_id: str, limit: float = 120.0) -> dict:
    """轮询到完成，打印进度轨迹。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < limit:
        p = server.mz_progress(job_id)
        key = (p.get("progress_percent"), p.get("current_sim_time"), p.get("completed"))
        if key != last:
            print(f"  [{time.time()-t0:5.1f}s] "
                  f"status={p.get('status'):9s} "
                  f"{str(p.get('progress_percent')):>6}%  "
                  f"t={p.get('current_sim_time')}  "
                  f"step={p.get('step')}/{p.get('total_steps')}  "
                  f"speed={p.get('speed')}  left={p.get('time_left_seconds')}s  "
                  f"done={p.get('completed')}")
            last = key
        if p.get("status") in ("finished", "failed", "killed"):
            break
        time.sleep(0.8)
    return server.mz_progress(job_id)


def wait_process_gone(job_id: str, limit: float = 30.0) -> None:
    """等进程句柄报告退出码。

    MIKE Zero 的许可证是单机独占的：上一轮启动器还没完全退出就起下一次，
    引擎会因拿不到许可证快速失败（rc != 0、SPI 不生成）。
    """
    t0 = time.time()
    while time.time() - t0 < limit:
        job = server.mz_job_status(job_id).get("job", {})
        if job.get("exit_code") is not None:
            print(f"  进程已完全退出  exit_code={job['exit_code']}  "
                  f"（等待 {time.time()-t0:.1f}s）")
            return
        time.sleep(0.5)
    print(f"  警告：{limit}s 内未取到退出码，继续下一步（可能撞许可证）")


def show_status(job_id: str) -> dict:
    s = server.mz_job_status(job_id)
    job = s.get("job", {})
    keep = {k: job.get(k) for k in
            ("id", "kind", "status", "exit_code", "elapsed_seconds", "note",
             "log_files", "result_dirs")}
    print("  --- 作业摘要 ---")
    print("  " + json.dumps(keep, ensure_ascii=False, indent=2).replace("\n", "\n  "))

    nr = job.get("new_results") or []
    print(f"  --- 新增结果文件 {len(nr)} 个 ---")
    for r in nr[:8]:
        print(f"     {r['size_bytes']:>12,d} B  {os.path.basename(r['path'])}")
    ch = job.get("changed") or []
    print(f"  --- 变化文件 {len(ch)} 个 ---")
    for c in ch[:8]:
        print(f"     {c['kind']:6s} {c['size_bytes']:>10,d} B  {os.path.basename(c['path'])}")
    if job.get("error_lines"):
        print("  --- 错误行 ---")
        for e in job["error_lines"][:5]:
            print("     ", e[:110])
    return s


async def list_tools() -> int:
    tools = await server.mcp.list_tools()
    print(f"工具数: {len(tools)}")
    for t in tools:
        first = (t.description or "").splitlines()[0]
        print(f"  - {t.name:16s} {first[:56]}")
    return len(tools)


def run_one(label: str, **kw) -> None:
    hr(label)
    clean()
    r = server.mz_run(setup=SETUP, **kw)
    if not r.get("ok"):
        print("启动失败:", json.dumps(r, ensure_ascii=False)[:800])
        return
    job_id = r["job"]["id"]
    print(f"job_id={job_id}  runner={r.get('runner')}")
    print("命令:", " ".join(f'"{c}"' if " " in c else c for c in r["job"]["cmd"]))
    print("PID:", r["job"]["pid"])
    if r.get("warning"):
        print("⚠️ ", r["warning"])

    fin = wait_job(job_id)
    print(f"\n  最终: status={fin.get('status')}  {fin.get('progress_percent')}%  "
          f"completed={fin.get('completed')}  close_code={fin.get('close_code')}  "
          f"started={fin.get('started')}  ended={fin.get('ended')}")

    chk = spi.parse_spi(os.path.join(WORK, "Test1._spi_"))
    print(f"  SPI 原文件: present={chk.get('present')} size={chk.get('size_bytes')} "
          f"completed={chk.get('completed')} stage={chk.get('stage')}")
    print(f"  SPI 归档: {fin.get('spi_archive')}")

    show_status(job_id)

    # 关键：等上一个作业的进程完全退出，否则下一个会撞许可证
    wait_process_gone(job_id)


def main() -> int:
    hr("1. MCP 工具清单")
    asyncio.run(list_tools())

    run_one("2. mz_run  direct=False （MzLaunch.exe）")

    run_one("3. mz_run  direct=True  （直调 MIKE11.exe，开关 -b）", direct=True)

    hr("4. SPI / SimStat 解析复核（direct 跑完留下原文件）")
    p = spi.progress(SETUP)
    print(json.dumps({k: v for k, v in p.items()
                      if k not in ("spi", "simstat")}, ensure_ascii=False, indent=2))
    print("\n  spi.recent_messages 尾部 6:")
    for m in (p.get("spi") or {}).get("recent_messages", [])[-6:]:
        print("     ", m)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
