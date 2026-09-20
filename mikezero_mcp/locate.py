"""定位 DHI MIKE Zero 安装、bin/x64 目录与各无界面（headless）入口。

探测顺序：
    1. 环境变量 DHI_MIKE_<YYYY>（指向 bin\\x64）与 DHI_MZ_APP（指向 MzShell.exe）
    2. 各盘符下的 <Program Files (x86)|Program Files>\\DHI\\MIKE Zero\\<YYYY>
"""

from __future__ import annotations

import os
import re
import string
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 无界面（headless）入口可执行文件：逻辑名 -> 文件名
HEADLESS_BINARIES: dict[str, str] = {
    "mzlaunch": "MzLaunch.exe",          # 从 setup 文件启动模拟（官方批处理入口）
    "toolboxshell": "ToolboxShell.exe",  # 运行 Toolbox setup（.mzt/.21t/.3t/.lpkt/.mst）
    "mzplotcomp": "MzPlotCompApp.exe",   # Plot Composer，批量出图
    "mzshell": "MzShell.exe",            # MIKE Zero GUI 主壳
}

CONFIG_FILES: dict[str, str] = {
    "engines_cfg": "MzEngines.cfg",            # 扩展名 -> 引擎/编辑器/MPI 支持
    "batch_cfg": "BatchSimulation.cfg",        # 批处理模拟定义
    "graphics_cfg": "MzGraphicsTools.cfg",
    "tool_cfg": "MzTool.cfg",
    "shell_cfg": "MzShell.cfg",
}

_ENV_YEAR_RE = re.compile(r"^DHI_MIKE(?:_ZERO)?_?(\d{4})$", re.IGNORECASE)
_INSTALL_SUBPATHS = (
    os.path.join("Program Files (x86)", "DHI", "MIKE Zero"),
    os.path.join("Program Files", "DHI", "MIKE Zero"),
)


@dataclass
class Installation:
    """一个 MIKE Zero 安装实例。"""

    year: str
    root: str
    bin64: str
    source: str
    binaries: dict[str, str] = field(default_factory=dict)
    configs: dict[str, str] = field(default_factory=dict)
    engine_count: int = 0
    exists: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def mzlaunch(self) -> str | None:
        return self.binaries.get("mzlaunch")

    @property
    def engines_cfg(self) -> str | None:
        return self.configs.get("engines_cfg")


def _existing_drives() -> list[str]:
    """返回当前存在的盘符列表，例如 ['C:', 'D:']。"""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(root)
    return drives


def _candidate_roots() -> list[tuple[str, str]]:
    """产出 (安装根目录, 来源说明) 候选列表。"""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []

    def add(path: str, source: str) -> None:
        key = os.path.normcase(os.path.normpath(path))
        if key in seen:
            return
        seen.add(key)
        out.append((path, source))

    # 1) 环境变量：DHI_MIKE_2023 -> ...\MIKE Zero\2023\bin\x64\
    for name, value in os.environ.items():
        m = _ENV_YEAR_RE.match(name)
        if not m or not value.strip():
            continue
        bin64 = Path(value.strip().rstrip("\\/"))
        year = m.group(1)
        if bin64.name.lower() == "x64" and bin64.parent.name.lower() == "bin":
            add(str(bin64.parent.parent), f"env:{name}")
        else:
            add(str(bin64), f"env:{name}")

    # 2) DHI_MZ_APP -> ...\MIKE Zero\2023\bin\x64\MzShell.exe
    mzapp = os.environ.get("DHI_MZ_APP", "").strip()
    if mzapp:
        app = Path(mzapp)
        if app.parent.name.lower() == "x64" and app.parent.parent.name.lower() == "bin":
            add(str(app.parent.parent.parent), "env:DHI_MZ_APP")

    # 3) 常见安装路径
    for drive in _existing_drives():
        for sub in _INSTALL_SUBPATHS:
            base = os.path.join(drive, sub)
            if not os.path.isdir(base):
                continue
            try:
                children = sorted(os.listdir(base))
            except OSError:
                continue
            for child in children:
                full = os.path.join(base, child)
                if child.isdigit() and os.path.isdir(full):
                    add(full, "scan:default-path")
            if not any(c.isdigit() for c in children):
                add(base, "scan:default-path")

    return out


def _probe(root: str, source: str) -> Installation | None:
    """检查一个候选根目录是否为有效的 MIKE Zero 安装。"""
    bin64 = os.path.join(root, "bin", "x64")
    if not os.path.isdir(bin64):
        # 环境变量有时直接给到 bin\x64
        if os.path.basename(os.path.normpath(root)).lower() == "x64":
            bin64 = os.path.normpath(root)
            root = os.path.dirname(os.path.dirname(bin64))
        else:
            return None

    if not os.path.isfile(os.path.join(bin64, "MzShell.exe")) and not os.path.isfile(
        os.path.join(bin64, "MzLaunch.exe")
    ):
        return None

    year = os.path.basename(os.path.normpath(root)) or "unknown"

    binaries = {
        key: p for key, name in HEADLESS_BINARIES.items() if os.path.isfile(p := os.path.join(bin64, name))
    }
    configs = {
        key: p for key, name in CONFIG_FILES.items() if os.path.isfile(p := os.path.join(bin64, name))
    }

    engine_count = 0
    engines_path = configs.get("engines_cfg")
    if engines_path:
        try:
            with open(engines_path, "r", encoding="utf-8-sig", errors="replace") as fh:
                engine_count = sum(
                    1 for line in fh if line.strip().startswith(".") and ";" in line
                )
        except OSError:
            engine_count = 0

    return Installation(
        year=year,
        root=os.path.normpath(root),
        bin64=os.path.normpath(bin64),
        source=source,
        binaries=binaries,
        configs=configs,
        engine_count=engine_count,
        exists=True,
    )


def find_installations() -> list[Installation]:
    """发现本机所有 MIKE Zero 安装，按年份降序排列。"""
    found: dict[str, Installation] = {}
    for root, source in _candidate_roots():
        inst = _probe(root, source)
        if inst is None:
            continue
        key = os.path.normcase(inst.bin64)
        if key not in found:
            found[key] = inst
    return sorted(found.values(), key=lambda i: i.year, reverse=True)


def pick_installation(year: str | None = None) -> Installation | None:
    """挑选一个安装；给定 year 时精确匹配，否则取最新。"""
    installs = find_installations()
    if not installs:
        return None
    if year:
        for inst in installs:
            if inst.year == str(year):
                return inst
        return None
    return installs[0]
