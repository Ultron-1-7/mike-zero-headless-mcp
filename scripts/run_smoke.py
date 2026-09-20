"""真实跑一次 MIKE 11 模拟，摸清「日志 / 进度 / 结果」的生成规律。

模式:
  A  MIKE11.exe -b -s <setup>        批处理 + 全静默
  C  MIKE11.exe -b    <setup>        批处理（保留进度输出）
  B  MzLaunch.exe <setup> -run -x    MCP 当前默认
  D  MzLaunch.exe <setup> -x         不带 -run

用法:
    .venv/Scripts/python.exe scripts/run_smoke.py [A|C|B|D]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

BIN = r"D:\Program Files (x86)\DHI\MIKE Zero\2023\bin\x64"
COMMON = r"C:\Program Files (x86)\Common Files\DHI\bin"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(HERE, "demo", "mike11_smoke")
SETUP = os.path.join(WORK, "Test1.sim11")
RF_DIR = os.path.join(WORK, "Test1.sim11 - Result Files")
PROBE = os.path.join(HERE, "state", "probe")   # 探测输出统一放这里，别脏了算例目录

LOGS = ("Test1.Log", "Test1-Info.Log", "Test1-SimStat.Log", "Test1._spi_")

MODES = {
    "A": ("MIKE11.exe", ["-b", "-s", SETUP], "MIKE11.exe -b -s  (批处理+全静默)"),
    "C": ("MIKE11.exe", ["-b", SETUP],       "MIKE11.exe -b     (批处理，保留进度)"),
    "B": ("MzLaunch.exe", [SETUP, "-run", "-x"], "MzLaunch.exe -run -x  (MCP 当前默认)"),
    "D": ("MzLaunch.exe", [SETUP, "-x"],         "MzLaunch.exe -x       (不带 -run)"),
}


def _env() -> dict:
    e = dict(os.environ)
    e["PATH"] = os.pathsep.join([BIN, COMMON, e.get("PATH", "")])
    return e


def _clean() -> None:
    for n in LOGS + ("Test1.res11",):
        p = os.path.join(WORK, n)
        if os.path.isfile(p):
            os.remove(p)
    if os.path.isdir(RF_DIR):
        shutil.rmtree(RF_DIR)


def _snap() -> dict:
    """快照：setup 目录根下 + 结果子目录（递归一层）。"""
    out: dict[str, int] = {}
    for n in LOGS + ("Test1.res11", "Test1.dfs0"):
        p = os.path.join(WORK, n)
        if os.path.isfile(p):
            out[n] = os.path.getsize(p)
    if os.path.isdir(RF_DIR):
        for f in sorted(os.listdir(RF_DIR)):
            fp = os.path.join(RF_DIR, f)
            if os.path.isfile(fp):
                out[f"RF/{f}"] = os.path.getsize(fp)
    return out


def _spi_tail(n: int = 6) -> list[str]:
    p = os.path.join(WORK, "Test1._spi_")
    if not os.path.isfile(p):
        return []
    try:
        t = open(p, "rb").read().decode("latin-1")
    except OSError:
        return []
    return [ln.strip() for ln in t.splitlines() if ln.strip()][-n:]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "A").upper()
    if mode not in MODES:
        print("模式必须是 A / C / B / D"); return 2
    exe_name, args, desc = MODES[mode]
    exe = os.path.join(BIN, exe_name)
    cmd = [exe] + args

    print("=" * 74)
    print(f"[模式 {mode}] {desc}")
    print("命令:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    print("=" * 74)

    _clean()
    before = _snap()
    print("\n[启动前]", before or "(空)")

    t0 = time.time()
    os.makedirs(PROBE, exist_ok=True)
    so_path = os.path.join(PROBE, f"run_smoke_{mode}.stdout.txt")
    with open(so_path, "wb") as fh:
        proc = subprocess.Popen(
            cmd, cwd=WORK, env=_env(),
            stdout=fh, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        print(f"PID={proc.pid}  已启动\n")

        deadline = t0 + 600
        last, lastsz = "", -1
        while True:
            rc = proc.poll()
            tail = _spi_tail(3)
            cur = " | ".join(tail)
            spi = os.path.join(WORK, "Test1._spi_")
            sz = os.path.getsize(spi) if os.path.isfile(spi) else -1
            if cur != last or sz != lastsz:
                print(f"  [{time.time()-t0:6.1f}s] spi={sz:>7}B  {cur[:140]}")
                last, lastsz = cur, sz
            if rc is not None:
                print(f"\n>>> 进程退出 rc={rc}  总耗时 {time.time()-t0:.1f}s")
                break
            if time.time() > deadline:
                print("\n>>> 超过 600s，强制终止")
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True)
                break
            time.sleep(1.2)

    time.sleep(1.0)
    after = _snap()

    print("\n" + "=" * 74)
    print("文件变化（启动前 -> 之后）")
    print("=" * 74)
    for k in sorted(set(before) | set(after)):
        b, a = before.get(k, 0), after.get(k, 0)
        flag = "   << 新/变" if b != a else ""
        print(f"  {k:34s} {b:>10} -> {a:>10}{flag}")

    for name in ("Test1-SimStat.Log", "Test1.Log", "Test1-Info.Log"):
        p = os.path.join(WORK, name)
        if os.path.isfile(p):
            body = open(p, "rb").read().decode("latin-1")
            print(f"\n=== {name} ({os.path.getsize(p)} B) ===")
            print(body[:1200] if body.strip() else "   (空)")

    spi = os.path.join(WORK, "Test1._spi_")
    if os.path.isfile(spi) and os.path.getsize(spi):
        print(f"\n=== Test1._spi_ ({os.path.getsize(spi)} B) 尾部 20 行 ===")
        for ln in _spi_tail(20):
            print("   ", ln)

    if os.path.isdir(RF_DIR):
        print("\n=== 结果目录 Test1.sim11 - Result Files ===")
        for f in sorted(os.listdir(RF_DIR)):
            print(f"   {os.path.getsize(os.path.join(RF_DIR,f)):10,d}  {f}")

    if os.path.isfile(so_path) and os.path.getsize(so_path):
        print(f"\n=== 进程输出 ({os.path.getsize(so_path)} B) ===")
        print(open(so_path, "rb").read().decode("latin-1")[:1200])
    else:
        print(f"\n=== 进程输出: 空（{exe_name} 无 stdout）===")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
