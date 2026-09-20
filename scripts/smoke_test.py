"""冒烟测试：不依赖 MCP 客户端，直接调用工具函数验证链路。

运行：
    .venv/Scripts/python.exe scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mikezero_mcp import server  # noqa: E402

PASS, FAIL = "  [OK]  ", "  [FAIL]"


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{PASS if condition else FAIL} {label}{('  -> ' + detail) if detail else ''}")


def main() -> int:
    failures = 0

    section("1. 工具清单")
    tools = server.mcp.list_tools()
    if hasattr(tools, "__await__"):
        import asyncio
        tools = asyncio.run(tools)
    names = sorted(getattr(t, "name", str(t)) for t in tools)
    print(f"  共 {len(names)} 个工具：")
    for n in names:
        print(f"    - {n}")
    if len(names) < 14:
        failures += 1
    check("工具数量 >= 14", len(names) >= 14, str(len(names)))

    section("2. 环境探测 mz_env")
    env = server.mz_env()
    check("ok", env.get("ok") is True, str(env.get("error", "")))
    installs = env.get("installations") or []
    print(f"  发现 {len(installs)} 个安装：")
    for i in installs:
        print(f"    - MIKE Zero {i['year']}  {i['bin64']}  (来源 {i['source']}, 引擎 {i['engine_count']})")
    if not installs:
        print("\n  未发现 MIKE Zero 安装，跳过后续依赖安装的测试。")
        return 1 if failures else 0

    sel = env["selected"]
    print("  选中的安装：")
    for k, v in (sel.get("binaries") or {}).items():
        print(f"    {k:14s} {os.path.basename(v)}")
    check("找到 MzLaunch.exe", "mzlaunch" in (sel.get("binaries") or {}))
    check("找到 ToolboxShell.exe", "toolboxshell" in (sel.get("binaries") or {}))
    check("找到 MzEngines.cfg", "engines_cfg" in (sel.get("configs") or {}))
    mikeio_state = env.get("mikeio") or {}
    check("mikeio 可用", mikeio_state.get("available") is True, str(mikeio_state.get("version", mikeio_state.get("reason", ""))))
    if not (sel.get("binaries") or {}).get("mzlaunch"):
        failures += 1

    section("3. 引擎映射 mz_engines")
    eng = server.mz_engines()
    check("ok", eng.get("ok") is True, str(eng.get("error", "")))
    print(f"  解析到 {eng.get('count')} 条扩展名映射，抽样：")
    for e in eng.get("entries", []):
        if e["extension"] in (".m21fm", ".m3fm", ".sim11", ".she", ".couple", ".mzt", ".sw", ".mfm"):
            print(f"    {e['extension']:9s} -> {e['engine']:32s} MPI={e['mpi']}")
    check("解析出引擎映射", (eng.get("count") or 0) > 10, str(eng.get("count")))
    one = server.mz_engines(extension=".m21fm")
    check(".m21fm 命中 1 条", one.get("count") == 1)
    if one.get("entries"):
        print(f"    .m21fm -> {one['entries'][0]['engine']}")

    section("4. setup 解析 mz_read_setup（对真实官方算例）")
    year = sel["year"]
    examples = os.path.join(sel["root"], "Examples")
    ok_any = False
    if os.path.isdir(examples):
        targets = []
        for ext in (".m21fm", ".m3fm", ".sim11", ".she", ".couple", ".mzt", ".sw"):
            for dirpath, _dirnames, filenames in os.walk(examples):
                hit = [f for f in filenames if f.lower().endswith(ext)]
                if hit:
                    targets.append(os.path.join(dirpath, hit[0]))
                    break
        for t in targets[:5]:
            res = server.mz_read_setup(t, year=year)
            if res.get("ok"):
                ok_any = True
                eng_name = (res.get("engine") or {}).get("engine")
                print(
                    f"    {os.path.basename(t)[:44]:46s} "
                    f"段={res['section_count']:4d} 赋值={res['assignment_count']:5d} "
                    f"文件引用={len(res['file_references']):3d} 引擎={eng_name}"
                )
    check("至少成功解析一个官方算例", ok_any)
    if not ok_any:
        failures += 1

    section("5. 目录扫描 mz_scan")
    if os.path.isdir(examples):
        scan = server.mz_scan(root=examples, max_files=20000, year=year)
        check("ok", scan.get("ok") is True, str(scan.get("error", "")))
        print(f"    扫描文件 {scan.get('scanned_files')} 个，截断={scan.get('truncated')}")
        print(f"    setup 文件 {scan.get('setup_count')} 个，结果文件 {scan.get('result_count')} 个")
        for s in (scan.get("setups") or [])[:5]:
            print(f"      setup: {s['path']}")
        for r in (scan.get("results") or [])[:3]:
            print(f"      result: {r['path']} ({r['size_bytes']} B)")
    else:
        print("    未找到 Examples 目录，跳过")

    section("6. 作业注册表")
    listing = server.mz_jobs()
    check("ok", listing.get("ok") is True, str(listing.get("error", "")))
    print(f"    当前作业数：{listing.get('count')}")
    missing = server.mz_job_status("no-such-job")
    check("未知作业返回结构化错误", missing.get("ok") is False)

    section("7. 参数拼装（不真正启动，只校验命令构造）")
    from mikezero_mcp.locate import pick_installation
    from mikezero_mcp import jobs
    inst = pick_installation(year)
    fake = os.path.join(jobs.STATE_DIR, "_probe.m21fm")
    Path(fake).write_text("[Simulation]\n  Name = 'probe'\n", encoding="utf-8")
    captured: dict = {}

    original = jobs.JobRegistry._launch

    def spy(self, kind, setup, cmd, cwd, watch_dirs, options, wait, timeout, validate_binary=True):
        captured.update({"kind": kind, "cmd": cmd, "cwd": cwd})
        return {"ok": True, "job": {"id": "dry-run"}}

    jobs.JobRegistry._launch = spy
    try:
        inst_cmd = jobs.registry.start_simulation(
            inst, fake, mpi=8, gpu=2, priority=2, engine="FemEngineHD.exe"
        )
        print("    mz_run 命令：")
        print("      " + " ".join(f'"{c}"' if " " in c else c for c in captured.get("cmd", [])))
        check("包含 -run", "-run" in captured.get("cmd", []))
        check("包含 -x", "-x" in captured.get("cmd", []))
        check("包含 -mpi 8", "-mpi" in captured.get("cmd", []) and "8" in captured.get("cmd", []))
        check("包含 -gpu 2", "-gpu" in captured.get("cmd", []))
        check("包含 -y 2", "-y" in captured.get("cmd", []))

        captured.clear()
        jobs.registry.start_batch(inst, [fake], workdir=str(jobs.STATE_DIR))
        bat = captured.get("cmd", [None, None, None])[-1]
        print(f"    批量 .bat：{bat}")
        if bat and os.path.isfile(bat):
            print("    ---- .bat 内容 ----")
            for line in Path(bat).read_text(encoding="utf-8").splitlines():
                print("      " + line)
            check(".bat 生成成功", True)
        else:
            check(".bat 生成成功", False)
            failures += 1
    finally:
        jobs.JobRegistry._launch = original
        try:
            Path(fake).unlink()
        except OSError:
            pass

    section("结论")
    if failures:
        print(f"  {failures} 项未通过，请查看上面的 [FAIL] 行。")
    else:
        print("  全部通过。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
