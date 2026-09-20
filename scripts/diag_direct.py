"""诊断：为什么 direct=True 的作业会「瞬间完成」且结果目录丢失。

逐拍打印：状态 / 进度 / SPI 是否存在 / 归档路径 / SimStat。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mikezero_mcp import jobs, server, spi  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(HERE, "demo", "mike11_smoke")
SETUP = os.path.join(WORK, "Test1.sim11")
TRASH = os.path.join(HERE, "demo", "_trash")


def retire(path: str) -> None:
    if not os.path.exists(path):
        return
    os.makedirs(TRASH, exist_ok=True)
    try:
        os.replace(path, os.path.join(TRASH, f"{os.path.basename(path)}.{time.time_ns()}"))
    except OSError as e:
        print("   retire 失败:", e)


print("=== 清场 ===")
for n in ("Test1.Log", "Test1-Info.Log", "Test1-SimStat.Log", "Test1._spi_",
          "Test1.sim11.apv.log", "Test1VolumeBalance.HTML"):
    retire(os.path.join(WORK, n))
retire(os.path.join(WORK, "Test1.sim11 - Result Files"))
jd = os.path.join(HERE, "state", "jobs")
for f in os.listdir(jd) if os.path.isdir(jd) else []:
    if f.endswith((".json", ".spi", ".out")):
        retire(os.path.join(jd, f))

print("\n清场后 setup 目录:")
print("  ", sorted(os.listdir(WORK)))
print("  _LIVE 内已有条目:", list(jobs._LIVE))

sp = spi.spi_path(SETUP)
ss = spi.simstat_path(SETUP)
print(f"  SPI 路径: {sp}  存在={os.path.isfile(sp)}")
print(f"  SimStat : {ss}  存在={os.path.isfile(ss)}")

print("\n=== 启动 direct=True ===")
t0 = time.time()
r = server.mz_run(setup=SETUP, direct=True)
jid = r["job"]["id"]
print("job_id:", jid, " runner:", r.get("runner"))
print("cmd:", r["job"]["cmd"])
print("启动返回耗时: %.3fs" % (time.time() - t0))
print("_LIVE 内条目:", list(jobs._LIVE))

print("\n=== 逐拍观察 ===")
for i in range(8):
    p = server.mz_progress(jid)
    raw = jobs.registry._load(jid)
    print(f"[{i}] t+{time.time()-t0:5.2f}s "
          f"status={p['status']:<9} pct={str(p['progress_percent']):>6} "
          f"done={str(p['completed']):<5} elapsed={p['elapsed_seconds']:<6} "
          f"spi_exist={os.path.isfile(sp)} spi_sz="
          f"{(os.path.getsize(sp) if os.path.isfile(sp) else -1):>5} "
          f"simstat={os.path.isfile(ss)} "
          f"arch={os.path.basename(p['spi_archive']) if p.get('spi_archive') else None} "
          f"fin_at={'set' if raw and raw.finished_at else '-'}")
    if p.get("completed") and p["status"] in ("finished", "failed") and i >= 2:
        break
    time.sleep(0.6)

print("\n=== 最终 ===")
fin = jobs.registry._load(jid)
print(json.dumps({
    "status": fin.status, "exit_code": fin.exit_code,
    "started_at": fin.started_at, "finished_at": fin.finished_at,
    "elapsed": (fin.finished_at or 0) - fin.started_at if fin.finished_at else None,
    "note": fin.note, "watch_dirs": fin.watch_dirs,
    "pre_state_n": len(fin.pre_state),
}, ensure_ascii=False, indent=2))

print("\n=== 现在的事发现场 ===")
print("setup 目录:")
for n in sorted(os.listdir(WORK)):
    print(f"   {os.path.getsize(os.path.join(WORK,n)):>10,d}  {n}")
rf = os.path.join(WORK, "Test1.sim11 - Result Files")
print("结果目录:", rf, "->", os.listdir(rf) if os.path.isdir(rf) else "不存在")

print("\n=== 再调一次 mz_job_status ===")
s = server.mz_job_status(jid)["job"]
print("  status:", s["status"], " elapsed:", s["elapsed_seconds"])
print("  result_dirs:", s["result_dirs"])
print("  new_results:", [(x["size_bytes"], os.path.basename(x["path"])) for x in s["new_results"]])
print("  changed:", [(x["kind"], x["size_bytes"], os.path.basename(x["path"])) for x in s["changed"]])
