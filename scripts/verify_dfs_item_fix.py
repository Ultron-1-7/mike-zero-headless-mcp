"""验证 dfs.read / dfs.plot 的 item 解析：名字 / int 序号 / 数字字符串 / 越界 / 无匹配。

跑法（用 venv）：
    .venv/Scripts/python.exe scripts/verify_dfs_item_fix.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from mikezero_mcp import dfs  # noqa: E402

RF = os.path.join(HERE, "demo", "fem3d_basin", "Sim1.m3fm - Result Files")
DFS0 = os.path.join(RF, "Sim1_HD.dfs0")
DFSU = os.path.join(RF, "Sim1_ST.dfsu")
OUT = os.path.join(HERE, "state", "probe", "dfs_fix")

os.makedirs(OUT, exist_ok=True)

results: list[tuple[str, bool, str]] = []


def record(label: str, r: dict, detail: str = "", expect_ok: bool = True) -> None:
    ok = bool(r.get("ok"))
    good = ok is expect_ok
    results.append((label, good, detail or str(r.get("error") or "")))
    tag = "PASS" if good else "FAIL"
    print(f"[{tag}] {label}")
    if expect_ok:
        if not ok:
            print(f"        error: {r.get('error')}")
        elif detail:
            print(f"        {detail}")
    else:
        # 期望失败：确认返回的是友好错误、且带可用的候选信息
        print(f"        error: {r.get('error')}")


print("=== info ===")
i0 = dfs.info(DFS0)
iu = dfs.info(DFSU)
print(f"dfs0 items: {i0['n_items']}  {i0['items'] if 'items' in i0 else i0.get('names')}")
print(f"dfsu items: {iu['n_items']}")

print("\n=== read ===")
r = dfs.read(DFS0, item=1, max_points=10)
record(
    "read item=1 (int)",
    r,
    f"selected={r.get('selected_items')} n_time={len(r.get('time', []))} "
    f"time_sampled={r.get('time_sampled')} max={r.get('statistics', [{}])[0].get('max')}",
)

r = dfs.read(DFS0, item="0", max_points=10)
record("read item='0' (digit str)", r, f"selected={r.get('selected_items')}")

r = dfs.read(DFSU, item=5, max_points=10)
record("read item=5 on dfsu", r, f"selected={r.get('selected_items')}")

r = dfs.read(DFS0, item="Point 2: Surface elevation", max_points=10)
record("read item=<name>", r, f"selected={r.get('selected_items')}")

r = dfs.read(DFS0)
record("read item=None", r, f"selected={r.get('selected_items')}")

r = dfs.read(DFS0, item=9)
record("read item=9 (out of range)", r, expect_ok=False)

r = dfs.read(DFS0, item="Nope")
record("read item='Nope'", r, expect_ok=False)

print("\n=== plot ===")
r = dfs.plot(DFS0, item=0, out_png=os.path.join(OUT, "p_item0.png"))
record("plot item=0 (int)", r, f"{r.get('path')} {r.get('size_bytes')}B")

r = dfs.plot(DFS0, item="1", out_png=os.path.join(OUT, "p_item1.png"))
record("plot item='1' (digit str)", r, f"{r.get('size_bytes')}B")

r = dfs.plot(DFS0, out_png=os.path.join(OUT, "p_none.png"))
record("plot item=None", r, f"{r.get('size_bytes')}B")

r = dfs.plot(DFS0, item="Point 2: Surface elevation", out_png=os.path.join(OUT, "p_name.png"))
record("plot item=<name>", r, f"{r.get('size_bytes')}B")

r = dfs.plot(DFSU, item=5, out_png=os.path.join(OUT, "p_dfsu5.png"))
record("plot item=5 on dfsu", r, f"{r.get('size_bytes')}B")

r = dfs.plot(DFS0, item=99, out_png=os.path.join(OUT, "p_bad.png"))
record("plot item=99 (out of range)", r, expect_ok=False)

failed = [x for x in results if not x[1]]
print(f"\n===== {len(results) - len(failed)}/{len(results)} passed =====")
if failed:
    print("FAILED:", [x[0] for x in failed])
    sys.exit(1)
