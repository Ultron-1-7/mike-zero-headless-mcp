"""启动 MIKE Zero 模拟（MzLaunch 或直调引擎），并管理作业。

实测结论（MIKE Zero 2023）：
  * `MzLaunch.exe` 是 GUI 子系统程序，**没有可用 stdout**，它的进度来自读引擎写的
    SPI 文件（`-r` 参数就是设这个扫描间隔，默认 200 ms）。
  * 引擎会把进度写到 setup 同目录的 `<setup名>._spi_`（纯文本），
    形如 `PROGRE 0` / `STATUS 2000/1/1 0:10:00` / `MESSAG 1 of 4464` /
    `MESSAG Completed` / `CLOSE  0`。详见 spi.py。
  * **结果不在 setup 根目录**，而是在 `<setup 文件名> - Result Files\\` 子目录里。
    所以快照必须递归，否则看不到产物。
  * 经 MzLaunch 启动时，模拟结束后 `._spi_` 会被删除；直调引擎则保留。
    因此这里会把 SPI 归档到 state/jobs/<id>.spi，跑完仍可回溯。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import spi
from .locate import Installation
from .pfs import engine_map_from_file

STATE_DIR = Path(os.environ.get("MIKEZERO_MCP_STATE") or (Path(__file__).resolve().parent.parent / "state"))
JOBS_DIR = STATE_DIR / "jobs"

# 关心的日志/结果扩展名（`._spi_` 是引擎的逐行进度文件）
LOG_SUFFIXES = (".log", ".out", ".txt", "._spi_")
RESULT_SUFFIXES = (".dfs0", ".dfs1", ".dfs2", ".dfs3", ".dfsu", ".res1d", ".res11", ".nc")

# 引擎把结果写进 "<setup 文件名> - Result Files" 子目录（实测 MIKE 11）
RESULT_DIR_MARKER = "- result files"

# 各引擎自己的批处理/静默开关（实测自 exe 内嵌 usage 文本）
#   MIKE11.exe           Syntax: MIKE11 [Options] SimulationFileName
#                          -b 批处理（出错不弹框，进度照写）
#                          -s 全静默 —— 会把 ._spi_ 清成 0 字节，别用于后台跑
#                          -v 仅校验  -w 进度窗口  -lx 语言
#   MSHE_Simulation.exe  Usage: MShe_Simulation.exe [/Silent] [/PP] [/WM] [/WQ] pfs
#   FemEngineHD.exe      Usage: <pfs-file>  （无静默开关，可选 -gpu / -gpusp）
ENGINE_BATCH_FLAGS: dict[str, list[str]] = {
    "mike11.exe": ["-b"],
    "mshe_simulation.exe": ["/Silent"],
    "femenginehd.exe": [],
    "femenginewd.exe": [],
    "femenginewq.exe": [],
    "femenginest.exe": [],
}

_ERROR_MARKERS = ("error", "fatal", "abort", "failed", "exception", "cannot open", "not found")

# 保存当前会话的进程句柄：job_id -> Popen
_LIVE: dict[str, subprocess.Popen] = {}


@dataclass
class Job:
    id: str
    kind: str                       # simulation | toolbox
    setup: str
    cmd: list[str]
    cwd: str
    pid: int | None
    started_at: float
    status: str = "starting"        # starting | running | finished | failed | killed | unknown
    finished_at: float | None = None
    exit_code: int | None = None
    stdout_path: str | None = None
    options: dict = field(default_factory=dict)
    watch_dirs: list[str] = field(default_factory=list)
    pre_state: dict = field(default_factory=dict)
    spi_at_start: list | None = None   # 启动时 ._spi_ 的 [大小, mtime]，用于识别遗留文件
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def elapsed_seconds(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 1)


# ---------------------------------------------------------------- 工具函数


def _snapshot(dirs: list[str], depth: int = 2, max_files: int = 8000) -> dict:
    """记录这些目录下文件的 路径 -> [大小, 修改时间]。

    **递归 depth 层**：MIKE 引擎把结果写进 "<setup> - Result Files" 子目录，
    只扫一层根目录会把 res11/dfsu 这类产物全部漏掉。
    """
    snap: dict[str, list] = {}
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        base = os.path.normpath(d).rstrip("\\/").count(os.sep)
        for dirpath, dirnames, filenames in os.walk(d):
            if os.path.normpath(dirpath).rstrip("\\/").count(os.sep) - base >= depth:
                dirnames[:] = []
            for name in filenames:
                if len(snap) >= max_files:
                    return snap
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                snap[p] = [st.st_size, st.st_mtime]
    return snap


def _result_dirs(job: "Job") -> list[str]:
    """找出作业相关的 "<setup 文件名> - Result Files" 结果子目录。"""
    out: list[str] = []
    search = list(job.watch_dirs) + [os.path.dirname(job.setup)]
    for d in search:
        if not d or not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(RESULT_DIR_MARKER):
                continue
            p = os.path.join(d, name)
            if os.path.isdir(p) and p not in out:
                out.append(p)
    return out


def _spi_fingerprint(setup: str) -> list | None:
    """返回 setup 同目录下 `._spi_` 的 [大小, mtime]；不存在则 None。"""
    p = spi.spi_path(setup)
    if not os.path.isfile(p):
        return None
    try:
        st = os.stat(p)
    except OSError:
        return None
    return [st.st_size, st.st_mtime]


def _archive_spi(job: "Job") -> str | None:
    """把当前 `._spi_` 快照到 state/jobs/<id>.spi。

    经 MzLaunch 启动时，模拟一结束 `._spi_` 就被删掉；归档后仍可回溯。

    只在 SPI 相对**启动时发生了变化**才归档：否则会把上一轮遗留的完整 SPI
    当成本次进度，导致新作业被判为「瞬时完成」。文件为空则不归档（说明引擎
    被 `-s` 静默了）。
    """
    src = spi.spi_path(job.setup)
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        return None
    if job.spi_at_start is not None and _spi_fingerprint(job.setup) == job.spi_at_start:
        return None
    dst = STATE_DIR / "jobs" / f"{job.id}.spi"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return str(dst)
    except OSError:
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=20,
        )
        return str(pid) in (out.stdout or "")
    except Exception:
        # 拿不准时，保守假定还活着，避免把正在跑的作业误判为结束
        return True


def _read_tail(path: str, lines: int = 200) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, max(8192, lines * 400))
            fh.seek(size - block)
            raw = fh.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if text.count("\ufffd") > len(text) * 0.05:
        text = raw.decode("utf-16-le", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _guess_logs(job: Job) -> list[str]:
    """返回该作业相关的日志文件，按「信息量 + 新鲜度」排序。

    优先级：`._spi_`（逐行进度，最有价值）> `-SimStat.Log`（起止汇总）
            > `<setup>.Log` > 其他同族文件。
    """
    candidates: dict[str, float] = {}
    for d in list(job.watch_dirs) + _result_dirs(job):
        if not os.path.isdir(d):
            continue
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries:
            for e in entries:
                if e.is_file() and e.name.lower().endswith(LOG_SUFFIXES):
                    try:
                        candidates[e.path] = e.stat().st_mtime
                    except OSError:
                        continue
    stem = os.path.splitext(os.path.basename(job.setup))[0].lower()

    def rank(path: str) -> int:
        low = os.path.basename(path).lower()
        if low.endswith("._spi_"):
            return 0
        if "simstat" in low:
            return 1
        if low == stem + ".log":
            return 2
        if stem in low:
            return 3
        return 4

    matching = [p for p in candidates if stem in os.path.basename(p).lower()]
    pool = matching or list(candidates)
    return sorted(pool, key=lambda p: (rank(p), -candidates[p]))


def _changed_files(job: Job) -> dict:
    """对比启动前后的快照，找出新增/变化的日志与结果文件。"""
    now = _snapshot(job.watch_dirs)
    produced, changed = [], []
    for path, (size, mtime) in now.items():
        low = path.lower()
        is_result = low.endswith(RESULT_SUFFIXES)
        prev = job.pre_state.get(path)
        if prev is None:
            (produced if is_result else changed).append(
                {"path": path, "size_bytes": size, "kind": "result" if is_result else "log"}
            )
        elif prev[1] != mtime:
            changed.append({"path": path, "size_bytes": size, "kind": "result" if is_result else "log"})
    return {"new_results": produced, "changed": changed}


# ---------------------------------------------------------------- 作业注册表


class JobRegistry:
    """作业元数据落盘到 state/jobs/*.json，便于 MCP server 重启后继续跟踪。"""

    def __init__(self, state_dir: Path = STATE_DIR) -> None:
        self.dir = state_dir / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------- 内部

    def _path(self, job_id: str) -> Path:
        return self.dir / f"{job_id}.json"

    def _save(self, job: Job) -> None:
        tmp = self._path(job.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(job.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path(job.id))

    def _load(self, job_id: str) -> Job | None:
        p = self._path(job_id)
        if not p.is_file():
            return None
        try:
            return Job(**json.loads(p.read_text(encoding="utf-8")))
        except (OSError, TypeError, json.JSONDecodeError):
            return None

    # -------------------------------------------------- 启动

    def _launch(
        self,
        kind: str,
        setup: str,
        cmd: list[str],
        cwd: str,
        watch_dirs: list[str],
        options: dict,
        wait: bool,
        timeout: float | None,
        validate_binary: bool = True,
    ) -> dict:
        job_id = uuid.uuid4().hex[:12]
        out_path = self.dir / f"{job_id}.out"
        job = Job(
            id=job_id,
            kind=kind,
            setup=os.path.abspath(setup),
            cmd=cmd,
            cwd=cwd,
            pid=None,
            started_at=time.time(),
            stdout_path=str(out_path),
            options=options,
            watch_dirs=[os.path.normpath(d) for d in watch_dirs if d],
            pre_state=_snapshot([os.path.normpath(d) for d in watch_dirs if d]),
            spi_at_start=_spi_fingerprint(os.path.abspath(setup)),
        )

        if validate_binary and not os.path.isfile(cmd[0]):
            job.status = "failed"
            job.note = f"入口程序不存在: {cmd[0]}"
            job.finished_at = time.time()
            self._save(job)
            return {"ok": False, "job": job.to_dict()}

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            with open(out_path, "wb") as fh:
                if wait:
                    proc = subprocess.Popen(
                        cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                        creationflags=creationflags,
                    )
                    _LIVE[job_id] = proc
                    job.pid = proc.pid
                    job.status = "running"
                    self._save(job)
                    try:
                        job.exit_code = proc.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        job.note = f"等待超时（{timeout}s），作业仍在后台运行"
                        return {"ok": True, "job": self.refresh(job_id)["job"]}
                    job.status = "finished" if job.exit_code == 0 else "failed"
                    job.finished_at = time.time()
                    self._save(job)
                    return {"ok": True, "job": job.to_dict()}

                proc = subprocess.Popen(
                    cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                )
        except OSError as exc:
            job.status = "failed"
            job.note = f"启动失败: {exc}"
            job.finished_at = time.time()
            self._save(job)
            return {"ok": False, "job": job.to_dict()}

        _LIVE[job_id] = proc
        job.pid = proc.pid
        job.status = "running"
        self._save(job)
        return {"ok": True, "job": job.to_dict()}

    # -------------------------------------------------- 对外 API

    def _build_direct_cmd(
        self,
        install: Installation,
        setup: str,
        engine: str | None,
        extra_args: list[str] | None,
    ) -> dict:
        """直调引擎：从 MzEngines.cfg 按扩展名找引擎 exe，并用**引擎自己的**开关。

        各引擎开关并不统一（实测）：MIKE11 用 `-b`，MSHE 用 `/Silent`，
        FemEngine 无静默开关。所以这里按 exe 名查表。
        """
        ext = os.path.splitext(setup)[1].lower()
        names: list[str] = []
        if engine:
            names.append(engine)
        if install.engines_cfg:
            try:
                entry = engine_map_from_file(install.engines_cfg).get(ext)
            except Exception:
                entry = None
            if entry and entry.engine:
                names.append(entry.engine)

        eng_path = None
        for n in names:
            cand = n if os.path.isabs(n) else os.path.join(install.bin64, n)
            if os.path.isfile(cand):
                eng_path = cand
                break
        if eng_path is None:
            return {
                "ok": False,
                "error": f"未找到 {ext} 对应的引擎可执行文件（候选: {names or '无'}）",
                "hint": "可改用 direct=False 走 MzLaunch，或显式传 engine=<引擎文件名>。",
            }

        flags = list(ENGINE_BATCH_FLAGS.get(os.path.basename(eng_path).lower(), []))
        cmd = [eng_path] + flags + [setup] + list(extra_args or [])
        return {"ok": True, "cmd": cmd, "engine_path": eng_path, "flags": flags}

    def start_simulation(
        self,
        install: Installation,
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
        cwd: str | None = None,
        wait: bool = False,
        timeout: float | None = None,
        result_dir: str | None = None,
    ) -> dict:
        """启动一次模拟。

        Args:
            direct: True 时绕过 MzLaunch，直接调用引擎 exe（用引擎自己的开关）。
                    直调的好处是 `._spi_` 进度文件跑完不会被删；MzLaunch 会删。
            silent: 仅对 MzLaunch 有效，传未文档化的 `-run`（实测可省）。
        """
        setup = os.path.abspath(setup)
        if not os.path.isfile(setup):
            return {"ok": False, "error": f"setup 文件不存在: {setup}"}

        if direct:
            built = self._build_direct_cmd(install, setup, engine, extra_args)
            if not built.get("ok"):
                return built
            cmd = built["cmd"]
            runner = f"direct:{os.path.basename(built['engine_path'])}"
            options = {
                "direct": True,
                "engine_path": built["engine_path"],
                "engine_flags": built["flags"],
                "extra_args": extra_args or [],
            }
        else:
            exe = install.binaries.get("mzlaunch")
            if not exe:
                return {"ok": False, "error": "未找到 MzLaunch.exe，无法启动模拟"}

            cmd = [exe, setup]
            if engine:
                cmd += [engine_flag, engine]
            if silent:
                cmd.append("-run")
            if exit_when_done:
                cmd.append("-x")
            if mpi:
                cmd += ["-mpi", str(int(mpi))]
            if gpu:
                cmd += ["-gpu", str(int(gpu))]
            if priority is not None:
                cmd += ["-y", str(int(priority))]
            if result_dir:
                cmd += ["-z", os.path.abspath(result_dir)]
            if extra_args:
                cmd += list(extra_args)

            runner = "mzlaunch"
            options = {
                "direct": False, "engine": engine, "engine_flag": engine_flag,
                "silent": silent, "exit_when_done": exit_when_done,
                "mpi": mpi, "gpu": gpu, "priority": priority,
                "result_dir": result_dir, "extra_args": extra_args or [],
            }

        run_cwd = cwd or os.path.dirname(setup)
        watch = [os.path.dirname(setup), result_dir, run_cwd]

        result = self._launch("simulation", setup, cmd, run_cwd, watch, options, wait, timeout)
        if result.get("job"):
            result["runner"] = runner
            warn = self._concurrency_warning(result["job"]["id"])
            if warn:
                result["warning"] = warn
        return result

    def start_toolbox(
        self,
        install: Installation,
        setup: str,
        cwd: str | None = None,
        extra_args: list[str] | None = None,
        wait: bool = False,
        timeout: float | None = None,
    ) -> dict:
        exe = install.binaries.get("toolboxshell")
        if not exe:
            return {"ok": False, "error": "未找到 ToolboxShell.exe"}

        setup = os.path.abspath(setup)
        if not os.path.isfile(setup):
            return {"ok": False, "error": f"toolbox setup 不存在: {setup}"}

        # 注意：Mz* 系列启动器并不识别 -run（对 MzLaunch 实测加不加都能跑，
        # 且它也不在官方 usage 表里），这里一律不传。
        cmd = [exe, setup] + list(extra_args or [])
        run_cwd = cwd or os.path.dirname(setup)
        return self._launch(
            "toolbox", setup, cmd, run_cwd, [os.path.dirname(setup), run_cwd],
            {"extra_args": extra_args or []}, wait, timeout,
        )

    def start_batch(
        self,
        install: Installation,
        setups: list[str],
        workdir: str | None = None,
        engine: str | None = None,
        engine_flag: str = "-e",
        mpi: int | None = None,
        gpu: int | None = None,
        priority: int | None = None,
        extra_args: list[str] | None = None,
        wait: bool = False,
        timeout: float | None = None,
    ) -> dict:
        """按 DHI 官方批处理做法生成 .bat 并顺序执行多个 setup。

        生成的 .bat 会保留在 workdir（默认第一个 setup 所在目录），
        用户可以自行查看或手工再次执行。
        """
        exe = install.binaries.get("mzlaunch")
        if not exe:
            return {"ok": False, "error": "未找到 MzLaunch.exe"}
        if not setups:
            return {"ok": False, "error": "setups 不能为空"}

        missing = [s for s in setups if not os.path.isfile(s)]
        if missing:
            return {"ok": False, "error": f"以下 setup 文件不存在: {missing}"}

        setups = [os.path.abspath(s) for s in setups]
        wd = os.path.abspath(workdir or os.path.dirname(setups[0]))
        os.makedirs(wd, exist_ok=True)

        batch_path = os.path.join(wd, f"mz_batch_{time.strftime('%Y%m%d_%H%M%S')}.bat")

        lines = [
            "@echo off",
            "setlocal",
            f'set "PATH=%PATH%;{install.bin64}"',
            f'cd /d "{wd}"',
            "",
        ]
        for s in setups:
            name = os.path.basename(s)
            flag = f" {engine_flag} {engine}" if engine else ""
            opts = f"{flag} -x"  # 不传 -run：MzLaunch 不识别该开关（实测可省）
            if mpi:
                opts += f" -mpi {int(mpi)}"
            if gpu:
                opts += f" -gpu {int(gpu)}"
            if priority is not None:
                opts += f" -y {int(priority)}"
            if extra_args:
                opts += " " + " ".join(extra_args)
            lines += [
                f'echo === START {name} ===',
                f'"{exe}" "{s}"{opts}',
                f'echo === END {name} rc=%errorlevel% ===',
                "",
            ]
        lines.append("echo === BATCH DONE ===")

        with open(batch_path, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("\n".join(lines))

        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        watch = [wd] + [os.path.dirname(s) for s in setups]

        result = self._launch(
            "batch", batch_path, [comspec, "/c", batch_path], wd, watch,
            {
                "engine": engine, "engine_flag": engine_flag, "mpi": mpi, "gpu": gpu,
                "priority": priority, "extra_args": extra_args or [],
                "setups": setups, "batch_file": batch_path,
            },
            wait, timeout, validate_binary=False,
        )
        if result.get("job"):
            result["batch_file"] = batch_path
            result["setup_count"] = len(setups)
            warn = self._concurrency_warning(result["job"]["id"])
            if warn:
                result["warning"] = warn
        return result

    # -------------------------------------------------- 状态同步

    def _running_jobs(self) -> list[dict]:
        """当前真正还活着的作业（按进程句柄 / PID 判断，不看可能过期的 json 状态）。"""
        out: list[dict] = []
        if not self.dir.is_dir():
            return out
        for p in self.dir.glob("*.json"):
            j = self._load(p.stem)
            if not j or not j.pid:
                continue
            proc = _LIVE.get(j.id)
            alive = proc.poll() is None if proc is not None else _pid_alive(j.pid)
            if alive:
                out.append({
                    "id": j.id, "kind": j.kind, "pid": j.pid,
                    "setup": os.path.basename(j.setup),
                })
        return out

    def _concurrency_warning(self, exclude_id: str | None) -> str | None:
        """MIKE Zero 的许可证一般是**单机独占**的；并发跑多个模拟极易因拿不到许可而失败。"""
        others = [j for j in self._running_jobs() if j["id"] != exclude_id]
        if not others:
            return None
        names = ", ".join(f"{j['kind']}:{j['setup']}(pid {j['pid']})" for j in others[:4])
        return (
            f"检测到另外 {len(others)} 个作业仍在运行：{names}。"
            "MIKE Zero 的许可证通常是单机独占的，并发跑多个模拟很可能因拿不到许可而"
            "失败（表现为引擎快速退出、退出码非 0、SPI 不生成）。"
            "建议等前一个作业的进程完全退出后再启动下一个。"
        )

    def _archive_path(self, job: Job) -> str | None:
        p = STATE_DIR / "jobs" / f"{job.id}.spi"
        return str(p) if p.is_file() else None

    def _sync(self, job: Job) -> dict:
        """刷新作业状态、纳入结果目录、归档 SPI，并返回最新进度。

        `refresh` 与 `progress` 共用这段逻辑，避免两条路径对状态的判断不一致。
        """
        # 结果子目录在启动时还不存在，这里动态纳入监视
        for rd in _result_dirs(job):
            if rd not in job.watch_dirs:
                job.watch_dirs.append(rd)

        # 趁 SPI 还在先归档：经 MzLaunch 启动时，模拟一结束它就被删
        _archive_spi(job)

        # 只要还没拿到退出码就继续 poll：状态可能已经被引擎进度判定为 finished，
        # 但进程句柄的退出码仍然要补上，否则 exit_code 会永远是 None。
        proc = _LIVE.get(job.id)
        if proc is not None and job.exit_code is None:
            code = proc.poll()
            if code is not None:
                job.exit_code = code

        if job.status in ("running", "starting"):
            if job.exit_code is not None:
                job.status = "finished" if job.exit_code == 0 else "failed"
                job.finished_at = job.finished_at or time.time()
            elif proc is None and not _pid_alive(job.pid):
                job.status = "finished"
                job.finished_at = job.finished_at or time.time()
                job.note = job.note or "进程已退出（退出码不可用，请核对日志与结果文件）"

        # 归档路径是固定的，不能只用「本次归档」的返回值——
        # 最后一次调用时原文件早已被删，会读成空。
        # since 用于剔除上一轮遗留的 SPI / SimStat。
        prog = spi.progress(
            job.setup, fallback_spi=self._archive_path(job), since=job.started_at
        )

        # 用引擎自己写的证据校正状态
        if job.status in ("running", "starting", "unknown") and prog.get("completed"):
            job.status = "finished"
            job.finished_at = job.finished_at or time.time()
            job.note = job.note or "引擎报告模拟已完成（MESSAG Completed / SimStat Ended）"

        if job.status == "finished":
            if prog.get("error_lines"):
                job.status = "failed"
                job.note = "引擎日志里出现错误行，详见 error_lines"
            elif not prog.get("completed") and not (prog.get("spi") or {}).get("present"):
                job.note = job.note or (
                    "进程已退出但找不到 ._spi_ 进度文件——引擎可能带 -s（静默）启动，"
                    "或 setup 根本没跑起来；请核对 -SimStat.Log 与结果目录。"
                )

        return prog

    # -------------------------------------------------- 查询

    def refresh(self, job_id: str) -> dict:
        job = self._load(job_id)
        if job is None:
            return {"ok": False, "error": f"未知作业: {job_id}"}

        prog = self._sync(job)

        changes = _changed_files(job)
        logs = _guess_logs(job)
        tail = _read_tail(logs[0], 60) if logs else ""
        errors = [
            line for line in tail.splitlines()
            if any(m in line.lower() for m in _ERROR_MARKERS)
        ][-12:]
        if not errors:
            errors = list(prog.get("error_lines") or [])[-12:]

        self._save(job)

        return {
            "ok": True,
            "job": {
                **job.to_dict(),
                "elapsed_seconds": job.elapsed_seconds,
                "log_files": logs[:5],
                "result_dirs": _result_dirs(job),
                "progress": prog,
                "error_lines": errors,
                **changes,
            },
        }

    def progress(self, job_id: str) -> dict:
        """只看进度：SPI 千分比、当前模拟时刻、步数、剩余时间、完成标志。"""
        job = self._load(job_id)
        if job is None:
            return {"ok": False, "error": f"未知作业: {job_id}"}

        prog = self._sync(job)
        self._save(job)

        out = dict(prog)
        out.update({
            "job_id": job_id,
            "status": job.status,
            "exit_code": job.exit_code,
            "elapsed_seconds": job.elapsed_seconds,
            "result_dirs": _result_dirs(job),
            "spi_archive": self._archive_path(job),
        })
        return out

    def get(self, job_id: str) -> dict:
        return self.refresh(job_id)

    def list_jobs(self, limit: int = 20) -> dict:
        jobs = []
        for p in sorted(self.dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]:
            job = self._load(p.stem)
            if job:
                jobs.append({
                    "id": job.id, "kind": job.kind, "status": job.status,
                    "setup": os.path.basename(job.setup),
                    "setup_path": job.setup,
                    "started_at": job.started_at,
                    "elapsed_seconds": job.elapsed_seconds,
                    "exit_code": job.exit_code,
                })
        return {"ok": True, "count": len(jobs), "jobs": jobs}

    def tail(self, job_id: str, lines: int = 200) -> dict:
        job = self._load(job_id)
        if job is None:
            return {"ok": False, "error": f"未知作业: {job_id}"}
        logs = _guess_logs(job)
        if not logs:
            return {
                "ok": True, "job_id": job_id, "log_files": [],
                "tail": "", "note": "暂未发现日志文件；MIKE Zero 可能在 setup 目录生成 <setup名>.log",
            }
        return {
            "ok": True,
            "job_id": job_id,
            "log_files": logs[:5],
            "shown": os.path.basename(logs[0]),
            "tail": _read_tail(logs[0], lines),
            "process_output": _read_tail(job.stdout_path, 60),
        }

    def kill(self, job_id: str, force: bool = True) -> dict:
        job = self._load(job_id)
        if job is None:
            return {"ok": False, "error": f"未知作业: {job_id}"}
        if not job.pid:
            return {"ok": False, "error": "该作业没有记录到进程号"}

        cmd = ["taskkill", "/PID", str(job.pid), "/T"]
        if force:
            cmd.append("/F")
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            job.status = "killed"
            job.finished_at = time.time()
            job.note = "已请求终止"
            self._save(job)
            return {
                "ok": res.returncode == 0,
                "job_id": job_id,
                "taskkill_stdout": (res.stdout or "").strip(),
                "taskkill_stderr": (res.stderr or "").strip(),
            }
        except Exception as exc:
            return {"ok": False, "error": f"终止失败: {exc}"}


registry = JobRegistry()
