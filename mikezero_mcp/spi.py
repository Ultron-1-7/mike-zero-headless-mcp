"""解析 MIKE 引擎的 SPI（Simulation Progress Info）与 SimStat 文件。

实测（MIKE Zero 2023 / MIKE 11）：
  setup 目录里会生成
      <setup名>._spi_           逐行进度，纯文本（latin-1 可读）
      <setup名>-SimStat.Log     起止时间汇总
      <setup名>.Log             （多为空，错误时才写）
      <setup名>-Info.Log        （多为空）
  `<setup名>._spi_` 的行格式：

      MESSAG Validating Network (857 Kb)
      MESSAG Setup Finished (858 Kb)
      MESSAG Starting time loop (858 Kb)
      STATUS 2000/1/1 0:10:00           <- 当前模拟时刻
      MESSAG 1 of 4464                  <- 步数进度
      PROGRE 0                          <- 进度千分比（0..1000）
      COMSPD 1                          <- 计算速度
      TIMLFT 182983                     <- 预计剩余秒数
      ...
      MESSAG 4464 of 4464
      PROGRE 1000
      MESSAG Completed                  <- 成功标志
      CLOSE  0                          <- 0 = 正常退出

注意：引擎加 `-s`（静默）会把 `._spi_` 清成 0 字节，进度就没了。
      批处理跑只用 `-b` 即可（-b 只在出错时不弹框，进度照写）。
"""

from __future__ import annotations

import os
import re
from typing import Any

# 错误行特征
_ERROR_MARKERS = (
    "error", "fatal", "abort", "failed", "failure", "exception",
    "cannot open", "could not", "not found", "invalid", "halt",
)

_SPI_SUFFIX = "._spi_"
_SIMSTAT_SUFFIX = "-simstat.log"


# ------------------------------------------------------------------ 定位


def spi_path(setup: str) -> str:
    """由 setup 文件推断同目录的 SPI 路径。"""
    d = os.path.dirname(os.path.abspath(setup))
    stem = os.path.splitext(os.path.basename(setup))[0]
    return os.path.join(d, stem + _SPI_SUFFIX)


def simstat_path(setup: str) -> str:
    d = os.path.dirname(os.path.abspath(setup))
    stem = os.path.splitext(os.path.basename(setup))[0]
    return os.path.join(d, stem + _SIMSTAT_SUFFIX)


def _read_text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            t = raw.decode(enc)
            if t.count("\ufffd") <= len(t) * 0.02:
                return t
        except Exception:
            continue
    return raw.decode("latin-1", errors="replace")


# ------------------------------------------------------------------ SPI


def parse_spi(path: str, max_messages: int = 12, since: float | None = None) -> dict[str, Any]:
    """解析 `._spi_`，抽出进度、当前时刻、完成标志、错误行。

    Args:
        since: 本次作业的启动时间戳。若文件早于它，说明是上一轮残留，直接忽略——
               否则上一次跑完留下的 SPI 会让新作业「瞬时完成」。
    """
    if not path or not os.path.isfile(path):
        return {"ok": True, "present": False, "path": path, "note": "SPI 文件尚未生成"}

    if since is not None:
        try:
            if os.path.getmtime(path) < since:
                return {
                    "ok": True, "present": True, "stale": True, "path": path,
                    "size_bytes": os.path.getsize(path), "completed": False,
                    "note": "该 SPI 早于本次作业启动时间，判定为上一轮遗留，已忽略。",
                }
        except OSError:
            pass

    text = _read_text(path)
    size = os.path.getsize(path)

    if size == 0:
        return {
            "ok": True,
            "present": True,
            "path": path,
            "size_bytes": 0,
            "note": "SPI 为 0 字节——引擎可能带 `-s`（静默）启动，静默会关闭进度输出；"
                    "批处理场景请只用 `-b`。",
        }

    progress: int | None = None       # PROGRE，0..1000
    sim_time: str | None = None       # STATUS
    step: int | None = None
    total_steps: int | None = None
    speed: int | None = None
    time_left: int | None = None
    close_code: int | None = None
    completed = False
    messages: list[str] = []
    stages: list[str] = []
    errors: list[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()

        if up.startswith("PROGRE"):
            m = re.search(r"(-?\d+)", line)
            if m:
                progress = int(m.group(1))
        elif up.startswith("STATUS"):
            sim_time = line.split(None, 1)[1].strip() if len(line.split(None, 1)) > 1 else ""
        elif up.startswith("COMSPD"):
            m = re.search(r"(-?\d+)", line)
            if m:
                speed = int(m.group(1))
        elif up.startswith("TIMLFT"):
            m = re.search(r"(-?\d+)", line)
            if m:
                time_left = int(m.group(1))
        elif up.startswith("CLOSE"):
            m = re.search(r"(-?\d+)", line)
            if m:
                close_code = int(m.group(1))
        elif up.startswith("MESSAG"):
            body = line[6:].strip()
            body = re.sub(r"\([\d\s]+Kb\)\s*$", "", body).strip()
            messages.append(body)
            if body and not re.fullmatch(r"\d+\s+of\s+\d+", body):
                stages.append(body)
            m = re.fullmatch(r"(\d+)\s+of\s+(\d+)", body)
            if m:
                step, total_steps = int(m.group(1)), int(m.group(2))
            if body.lower().startswith(("completed", "finished")):
                completed = True

        low = line.lower()
        if any(mk in low for mk in _ERROR_MARKERS):
            errors.append(line)

    if progress is not None and progress >= 1000:
        completed = True
    if close_code is not None and close_code == 0 and (progress or 0) >= 1000:
        completed = True

    percent = None
    if progress is not None:
        percent = round(min(max(progress, 0), 1000) / 10.0, 1)
    elif step is not None and total_steps:
        percent = round(step / total_steps * 100.0, 1)

    return {
        "ok": True,
        "present": True,
        "path": path,
        "size_bytes": size,
        "progress_percent": percent,
        "current_sim_time": sim_time,
        "step": step,
        "total_steps": total_steps,
        "speed": speed,
        "time_left_seconds": time_left,
        "completed": completed,
        "close_code": close_code,
        "stage": stages[-1] if stages else None,
        "recent_messages": messages[-max_messages:],
        "error_lines": errors[-10:],
    }


# ------------------------------------------------------------------ SimStat


def parse_simstat(path: str, since: float | None = None) -> dict[str, Any]:
    """解析 `<setup名>-SimStat.Log`：起止时间。

    实测内容形如::

         1   // Simulation Started  :  2026/9/20  13:13:36
         2   // Simulation Ended    :  2026/9/20  13:13:37

    Args:
        since: 作业启动时间戳；文件更旧则视为上一轮残留，`completed` 记 False。
    """
    if not path or not os.path.isfile(path):
        return {"ok": True, "present": False, "path": path}

    if since is not None:
        try:
            if os.path.getmtime(path) < since:
                return {
                    "ok": True, "present": True, "stale": True, "path": path,
                    "completed": False,
                    "note": "该 SimStat 早于本次作业启动时间，判定为上一轮遗留，已忽略。",
                }
        except OSError:
            pass

    text = _read_text(path)
    started = ended = None
    for line in text.splitlines():
        low = line.lower()
        if "simulation started" in low:
            started = line.split(":", 1)[1].strip() if ":" in line else line.strip()
        elif "simulation ended" in low:
            ended = line.split(":", 1)[1].strip() if ":" in line else line.strip()

    return {
        "ok": True,
        "present": True,
        "path": path,
        "size_bytes": os.path.getsize(path),
        "started": started,
        "ended": ended,
        "completed": bool(ended),
        "raw": text.strip()[:400],
    }


# ------------------------------------------------------------------ 汇总


def progress(
    setup: str,
    fallback_spi: str | None = None,
    since: float | None = None,
) -> dict[str, Any]:
    """把 SPI + SimStat 合成一份进度报告。

    Args:
        setup: setup 文件路径。
        fallback_spi: 原 SPI 被删（MzLaunch 结束后会删）时的归档副本路径。
        since: 作业启动时间戳，用于剔除上一轮残留文件。
    """
    sp = parse_spi(spi_path(setup), since=since)
    if not sp.get("present") and fallback_spi and os.path.isfile(fallback_spi):
        # 归档副本由本服务在本次运行期间写入，无需再判新旧
        sp = parse_spi(fallback_spi)
        sp["archived_copy"] = True
    ss = parse_simstat(simstat_path(setup), since=since)

    completed = bool(sp.get("completed")) or bool(ss.get("completed"))
    errors = list(sp.get("error_lines") or [])

    return {
        "ok": True,
        "setup": os.path.abspath(setup),
        "progress_percent": sp.get("progress_percent"),
        "current_sim_time": sp.get("current_sim_time"),
        "step": sp.get("step"),
        "total_steps": sp.get("total_steps"),
        "speed": sp.get("speed"),
        "time_left_seconds": sp.get("time_left_seconds"),
        "stage": sp.get("stage"),
        "completed": completed,
        "close_code": sp.get("close_code"),
        "started": ss.get("started"),
        "ended": ss.get("ended"),
        "error_lines": errors,
        "spi": sp,
        "simstat": ss,
    }
