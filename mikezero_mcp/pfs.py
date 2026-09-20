"""解析 MzEngines.cfg（扩展名 -> 引擎映射）与 MIKE Zero 的 PFS setup 文件。

MIKE Zero 的 setup 文件是 PFS（Parameter File Setup）格式：类 INI，
用 `[段名]` 分层、`关键字 = 值` 赋值，注释以 `//` 或 `!'` 开头，
文件编码可能是 UTF-8-BOM / UTF-16LE / UTF-16BE。
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

# ---------------------------------------------------------------- 编码

_ENCODINGS = ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin-1")


def read_text(path: str) -> tuple[str, str]:
    """读取文本文件，自动试探编码，返回 (文本, 实际编码)。"""
    with open(path, "rb") as fh:
        raw = fh.read()

    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace"), "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace"), "utf-16-be"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig"

    for enc in _ENCODINGS:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.count("\ufffd") > len(text) * 0.01:
            continue
        # UTF-16 误判成单字节编码时会读出一堆空字节
        if enc.startswith(("utf-16", "cp1252", "latin-1")) and text.count("\x00") > 0:
            continue
        return text, enc

    return raw.decode("latin-1", errors="replace"), "latin-1"


# ---------------------------------------------------------------- PFS

_COMMENT_PREFIXES = ("//", "!'", ";", "*")
_SECTION_RE = re.compile(r"^\[(.+?)\]\s*$")
_FILE_LIKE_RE = re.compile(r"^[^<>\|\*\?]+\.([A-Za-z0-9]{1,6})$")


@dataclass
class Assignment:
    """PFS 中的一条 关键字 = 值，附带其所在的段路径。"""

    path: list[str]
    key: str
    value: str
    line: int

    def to_dict(self) -> dict:
        return asdict(self)


def _strip_inline_comment(value: str) -> str:
    """去掉值后面的行内注释；引号内的内容原样保留。"""
    value = value.strip()
    if not value:
        return value

    if value[0] in "'\"":
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]

    best = len(value)
    for marker in (" //", "\t//", " !'", "\t!'"):
        idx = value.find(marker)
        if idx != -1:
            best = min(best, idx)
    return value[:best].strip()


def parse_pfs(text: str) -> tuple[list[Assignment], list[str]]:
    """解析 PFS 文本，返回 (赋值列表, 遇到的段名列表)。"""
    assignments: list[Assignment] = []
    sections: list[str] = []
    stack: list[tuple[int, str]] = []

    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith(_COMMENT_PREFIXES):
            continue

        indent = len(line) - len(line.lstrip())

        m = _SECTION_RE.match(stripped)
        if m:
            name = m.group(1).strip()
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, name))
            sections.append(name)
            continue

        if "=" not in stripped:
            continue

        key, _, raw_value = stripped.partition("=")
        assignments.append(
            Assignment(
                path=[name for _, name in stack],
                key=key.strip(),
                value=_strip_inline_comment(raw_value),
                line=lineno,
            )
        )

    return assignments, sections


# ---------------------------------------------------------------- MzEngines.cfg


@dataclass
class EngineEntry:
    """MzEngines.cfg 中的一行映射。"""

    extension: str
    engine: str
    editor: str = ""
    mpi: bool = False
    line: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def parse_mz_engines(text: str) -> list[EngineEntry]:
    """解析 MzEngines.cfg。行格式：.ext; engine.exe; editor.dll; mpi"""
    entries: list[EngineEntry] = []
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith(_COMMENT_PREFIXES):
            continue

        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2:
            continue

        ext = parts[0]
        if not ext.startswith("."):
            continue

        mpi_raw = parts[3].lower() if len(parts) > 3 else ""
        entries.append(
            EngineEntry(
                extension=ext.lower(),
                engine=parts[1],
                editor=parts[2] if len(parts) > 2 else "",
                mpi=mpi_raw in ("true", "1", "yes"),
                line=lineno,
            )
        )
    return entries


def engine_map_from_file(path: str) -> dict[str, EngineEntry]:
    """读取 MzEngines.cfg，返回 {扩展名: EngineEntry}。"""
    text, _ = read_text(path)
    return {e.extension: e for e in parse_mz_engines(text)}


# ---------------------------------------------------------------- setup 概览

# 结果 / 数据文件的扩展名（用于从 setup 中捡出文件引用）
RESULT_EXTENSIONS = {
    ".dfs0", ".dfs1", ".dfs2", ".dfs3", ".dfsu",
    ".res1d", ".res11", ".resx", ".nc", ".xyz", ".grd", ".mesh",
    ".xyz", ".asc", ".tif", ".shp", ".bmp", ".png",
}

# 关心的关键字（不区分大小写，子串匹配 -> 人类可读标签）
_KEY_INTERESTS: tuple[tuple[str, str], ...] = (
    ("simulation_period", "模拟时段"),
    ("start_time", "开始时间"),
    ("end_time", "结束时间"),
    ("timestep", "计算步长"),
    ("time_step", "计算步长"),
    ("result", "结果设置"),
    ("resultfolder", "结果目录"),
    ("inputfile", "输入文件"),
    ("file_name", "输入文件"),
    ("simulation_type", "模拟类型"),
    ("number_of_domains", "子域数"),
    ("processors", "进程数"),
    ("model", "模型"),
)


def collect_file_references(assignments: list[Assignment]) -> list[str]:
    """从赋值中挑出像文件路径的值，去重后返回。"""
    seen: dict[str, None] = {}
    for a in assignments:
        value = a.value.strip()
        if not value or len(value) > 512:
            continue
        if any(ch in value for ch in "\n\r"):
            continue
        m = _FILE_LIKE_RE.match(os.path.basename(value.replace("\\", "/")))
        if not m:
            continue
        ext = "." + m.group(1).lower()
        if ext in RESULT_EXTENSIONS or ext in _known_setup_extensions():
            seen.setdefault(value, None)
    return list(seen)


_SETUP_EXT_CACHE: set[str] = set()


def _known_setup_extensions() -> set[str]:
    return _SETUP_EXT_CACHE


def register_setup_extensions(extensions: set[str]) -> None:
    """由调用方注入 MzEngines.cfg 里的扩展名，供文件引用识别使用。"""
    _SETUP_EXT_CACHE.clear()
    _SETUP_EXT_CACHE.update(e.lower() for e in extensions)


def summarize_setup(path: str, engine_map: dict[str, EngineEntry] | None = None) -> dict:
    """解析一个 setup 文件，返回结构化概览。"""
    abspath = os.path.abspath(path)
    if not os.path.isfile(abspath):
        return {"ok": False, "error": f"文件不存在: {abspath}"}

    text, encoding = read_text(abspath)
    assignments, sections = parse_pfs(text)

    ext = os.path.splitext(abspath)[1].lower()
    engine = (engine_map or {}).get(ext)

    highlights: list[dict] = []
    for a in assignments:
        key_l = a.key.lower()
        for needle, label in _KEY_INTERESTS:
            if needle in key_l and a.value:
                highlights.append(
                    {
                        "label": label,
                        "section": " / ".join(a.path),
                        "key": a.key,
                        "value": a.value,
                        "line": a.line,
                    }
                )
                break

    return {
        "ok": True,
        "path": abspath,
        "name": os.path.basename(abspath),
        "extension": ext,
        "encoding": encoding,
        "size_bytes": os.path.getsize(abspath),
        "engine": engine.to_dict() if engine else None,
        "default_engine_exe": engine.engine if engine else None,
        "mpi_supported": engine.mpi if engine else None,
        "sections": sections,
        "section_count": len(sections),
        "assignment_count": len(assignments),
        "file_references": collect_file_references(assignments),
        "highlights": highlights[:40],
    }


def scan_directory(root: str, setup_extensions: set[str], max_files: int = 5000) -> dict:
    """扫描目录树，按扩展名归类 setup 文件与结果文件。"""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}

    setups: list[dict] = []
    results: list[dict] = []
    other_counts: dict[str, int] = {}
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            scanned += 1
            if scanned > max_files:
                break
            ext = os.path.splitext(name)[1].lower()
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            record = {
                "path": os.path.relpath(full, root),
                "abs_path": full,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            }
            if ext in setup_extensions:
                setups.append(record)
            elif ext in RESULT_EXTENSIONS:
                results.append(record)
            else:
                other_counts[ext] = other_counts.get(ext, 0) + 1
        if scanned > max_files:
            break

    setups.sort(key=lambda r: r["path"])
    results.sort(key=lambda r: (-r["size_bytes"]))

    return {
        "ok": True,
        "root": root,
        "scanned_files": min(scanned, max_files),
        "truncated": scanned > max_files,
        "setups": setups,
        "setup_count": len(setups),
        "results": results[:200],
        "result_count": len(results),
        "other_extension_counts": dict(
            sorted(other_counts.items(), key=lambda kv: -kv[1])[:20]
        ),
    }
