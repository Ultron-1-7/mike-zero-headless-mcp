"""基于 mikeio 读取 MIKE Zero 的 dfs 结果文件（可选依赖）。

mikeio 是 DHI 官方开源库，读 dfs0/1/2/3/dfsu/res1d 都免许可证。
未安装时所有函数返回结构化错误，不影响其它工具的可用性。
"""

from __future__ import annotations

import os
from typing import Any

_MIKEO_ERROR: str | None = None


def _mikeio():
    """惰性导入 mikeio，失败时抛出带说明的 ImportError。"""
    global _MIKEO_ERROR
    try:
        import mikeio  # noqa: PLC0415
        return mikeio
    except Exception as exc:  # pragma: no cover
        _MIKEO_ERROR = str(exc)
        raise ImportError(
            "未安装 mikeio，无法读取 dfs 结果。安装方式："
            "'<工程>/.venv/Scripts/python.exe -m pip install mikeio'"
        ) from exc


def available() -> dict:
    """探测 mikeio / mikeio1d 是否可用及其版本。"""
    out: dict[str, Any] = {"ok": True}
    try:
        mikeio = _mikeio()
        out["available"] = True
        out["version"] = getattr(mikeio, "__version__", "unknown")
    except ImportError as exc:
        out.update({"available": False, "reason": str(exc), "detail": _MIKEO_ERROR})
    try:
        import mikeio1d  # noqa: PLC0415
        out["mikeio1d_available"] = True
        out["mikeio1d_version"] = getattr(mikeio1d, "__version__", "unknown")
    except Exception:
        out["mikeio1d_available"] = False
    return out


def _open(path: str):
    mikeio = _mikeio()
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    for opener in ("open", "read"):
        fn = getattr(mikeio, opener, None)
        if callable(fn):
            return fn(path)
    raise RuntimeError("mikeio 版本异常：既没有 open() 也没有 read()")


# mikeio 支持的 dfs 系列；MIKE 11/1D 结果要用 mikeio1d
_MIKEO_EXT = {".dfs0", ".dfs1", ".dfs2", ".dfs3", ".dfsu", ".mesh"}
_MIKEO1D_EXT = {".res1d", ".res11", ".resx", ".prf", ".xns11"}


def _unsupported_hint(path: str) -> str | None:
    """对 mikeio 不支持的格式给出可执行的下一步提示。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in _MIKEO1D_EXT:
        return (
            f"{ext} 是 MIKE 11 / 1D 结果格式，mikeio 不支持，需要 DHI 的 mikeio1d："
            "  <venv>/Scripts/python.exe -m pip install mikeio1d"
        )
    if ext and ext not in _MIKEO_EXT:
        return f"{ext} 不是 mikeio 支持的格式；支持：{', '.join(sorted(_MIKEO_EXT))}"
    return None


def _safe(obj: Any, name: str, default: Any = None) -> Any:
    """安全取属性。

    注意：mikeio 的 `obj.time` 在不支持的时轴类型上会抛 ValueError，而不是
    AttributeError，所以 `getattr(obj, 'time', None)` 并不能兜住，必须显式 try。
    """
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _time_bounds(obj: Any) -> dict:
    out: dict[str, Any] = {}
    time = _safe(obj, "time")
    if time is None:
        return out
    try:
        out["start"] = str(time[0])
        out["end"] = str(time[-1])
        out["n_timesteps"] = int(len(time))
    except Exception:
        pass
    return out


def _fail(path: str, exc: BaseException, **extra: Any) -> dict:
    """统一的失败返回，附带格式提示。"""
    out: dict[str, Any] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    hint = _unsupported_hint(path)
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def _is_1d(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _MIKEO1D_EXT


def _open1d(path: str):
    """用 mikeio1d 打开 MIKE 11 / 1D 结果（.res1d / .res11）。"""
    try:
        import mikeio1d  # noqa: PLC0415
    except Exception as exc:
        raise ImportError(
            "读取 .res1d/.res11 需要 mikeio1d："
            "  <venv>/Scripts/python.exe -m pip install mikeio1d"
        ) from exc
    abspath = os.path.abspath(path)
    if not os.path.isfile(abspath):
        raise FileNotFoundError(abspath)
    return mikeio1d.Res1D(abspath)


def _info1d(path: str) -> dict:
    try:
        res = _open1d(path)
    except Exception as exc:
        return _fail(path, exc)

    ti = _safe(res, "time_index")
    payload: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "type": type(res).__name__,
        "reader": "mikeio1d",
        "size_bytes": os.path.getsize(path),
        "quantities": [str(q) for q in (_safe(res, "quantities", []) or [])],
        "reaches": [str(r) for r in (_safe(res, "reaches", []) or [])],
        "nodes": [str(n) for n in (_safe(res, "nodes", []) or [])],
    }
    payload["n_items"] = len(payload["quantities"])
    payload["items"] = [{"index": i, "name": q, "unit": "", "type": ""}
                        for i, q in enumerate(payload["quantities"])]
    payload["n_reaches"] = len(payload["reaches"])
    payload["n_nodes"] = len(payload["nodes"])

    if ti is not None:
        try:
            payload["start"] = str(ti[0])
            payload["end"] = str(ti[-1])
            payload["n_timesteps"] = int(len(ti))
        except Exception:
            pass
    return payload


def _coerce_index(item: Any) -> Any:
    """把 "3" / " +3 " 这类纯数字字符串转成 int，其余原样返回。

    坑：MCP 客户端可能把 JSON 里的整数序号序列化成字符串（本机 WorkBuddy
    宿主实测如此），所以「item 是序号吗」不能只靠 isinstance(item, int)。
    不转换的话，item=1 会变成去按名字找 "1"，直接 KeyError。
    """
    if isinstance(item, str):
        s = item.strip()
        if s.lstrip("+-").isdigit():
            return int(s)
    return item


def _read1d(
    path: str,
    item: str | int | None = None,
    start: str | None = None,
    end: str | None = None,
    max_points: int = 500,
    max_columns: int = 6,
) -> dict:
    """读取 1D 结果。列名形如 "Water Level:CALI:0"（量:河段:里程）。"""
    try:
        res = _open1d(path)
        df = res.read()
    except Exception as exc:
        return _fail(path, exc)

    if df is None or not getattr(df, "columns", None) is not None:
        return {"ok": False, "error": "mikeio1d 未返回数据"}

    all_columns = [str(c) for c in df.columns]

    if start or end:
        try:
            df = df.loc[start:end]
        except Exception as exc:
            return {"ok": False, "error": f"时间筛选失败: {exc}", "available": all_columns[:20]}

    # 选列：按数量名整体选中，否则按列名匹配
    if item is None:
        first_q = all_columns[0].split(":")[0] if all_columns else None
        selected = [c for c in all_columns if c.split(":")[0] == first_q][:max_columns]
    elif isinstance(item, int):
        selected = all_columns[item : item + max_columns]
    else:
        needle = str(item)
        exact = [c for c in all_columns if c == needle]
        if exact:
            selected = exact
        else:
            by_q = [c for c in all_columns if c.split(":")[0].lower() == needle.lower()]
            selected = (by_q or [c for c in all_columns if needle.lower() in c.lower()])[:max_columns]

    if not selected:
        return {
            "ok": False,
            "error": f"未匹配到列: {item}",
            "available_quantities": sorted({c.split(":")[0] for c in all_columns}),
            "example_columns": all_columns[:10],
        }

    def _sample(series) -> list:
        try:
            vals = series.to_numpy()
        except Exception:
            return []
        n = len(vals)
        if n > max_points:
            step = max(1, n // max_points)
            vals = vals[::step]
        out = []
        for v in vals:
            try:
                f = float(v)
                out.append(None if f != f else round(f, 6))
            except (TypeError, ValueError):
                out.append(None)
        return out

    statistics, series_map = [], {}
    for col in selected:
        try:
            s = df[col]
            statistics.append({
                "name": col,
                "shape": [len(s)],
                "n_total": int(len(s)),
                "n_finite": int(s.notna().sum()),
                "min": float(s.min()) if s.notna().any() else None,
                "max": float(s.max()) if s.notna().any() else None,
                "mean": float(s.mean()) if s.notna().any() else None,
            })
            series_map[col] = {"sampled": _sample(s), "sampled_from": int(len(s))}
        except Exception as exc:
            series_map[col] = {"error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "path": os.path.abspath(path),
        "reader": "mikeio1d",
        "n_items": len(all_columns),
        "names": all_columns[:60],
        "truncated_names": len(all_columns) > 60,
        "selected_items": selected,
        "start": str(df.index[0]) if len(df.index) else None,
        "end": str(df.index[-1]) if len(df.index) else None,
        "n_timesteps": int(len(df.index)),
        "time": [str(t) for t in df.index],
        "statistics": statistics,
        "series": series_map,
    }


def _plot1d(path: str, item: str | int | None, out_png: str) -> dict:
    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415

        res = _open1d(path)
        df = res.read()
        all_columns = [str(c) for c in df.columns]
        item = _coerce_index(item)
        if item is None:
            q = all_columns[0].split(":")[0]
            selected = [c for c in all_columns if c.split(":")[0] == q][:4]
        elif isinstance(item, int):
            selected = all_columns[item : item + 4]
        else:
            needle = str(item)
            selected = [c for c in all_columns if needle.lower() in c.lower()][:4] or all_columns[:2]

        fig, ax = plt.subplots(figsize=(11, 4.5), dpi=130)
        for col in selected:
            ax.plot(df.index, df[col].to_numpy(), lw=1.1, label=col)
        ax.set_title(os.path.basename(path))
        ax.set_xlabel("time")
        ax.grid(alpha=0.3)
        if len(selected) > 1:
            ax.legend(fontsize=8, ncol=2)
        fig.autofmt_xdate()
        fig.tight_layout()
        fig.savefig(out_png)
        plt.close(fig)
        return {"ok": True, "path": out_png, "size_bytes": os.path.getsize(out_png), "plotted": selected}
    except Exception as exc:
        return _fail(path, exc, output=out_png)


def info(path: str) -> dict:
    """读取 dfs / res 文件的元信息：类型、条目、时间轴、形状。"""
    if _is_1d(path):
        return _info1d(path)
    try:
        obj = _open(path)
    except ImportError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        return _fail(path, exc)

    result: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "type": type(obj).__name__,
        "size_bytes": os.path.getsize(path),
    }

    items = _safe(obj, "items")
    if items is not None:
        try:
            result["items"] = [
                {
                    "index": i,
                    "name": _safe(it, "name", str(it)),
                    "unit": _safe(_safe(it, "unit"), "name") or str(_safe(it, "unit", "") or ""),
                    "type": _safe(_safe(it, "type"), "name") or str(_safe(it, "type", "") or ""),
                }
                for i, it in enumerate(items)
            ]
            result["n_items"] = len(result["items"])
        except Exception as exc:
            result["items_error"] = f"{type(exc).__name__}: {exc}"

    # 时间轴：先问 reader，reader 不认时轴时退回到实际读一次数据
    bounds = _time_bounds(obj)
    if "n_timesteps" not in bounds:
        try:
            mikeio = _mikeio()
            ds = mikeio.read(os.path.abspath(path))
            bounds = _time_bounds(ds)
            if bounds:
                result["time_axis_source"] = "read"
        except Exception as exc:
            result["time_axis_note"] = f"时轴不可用: {type(exc).__name__}: {exc}"
    result.update(bounds)

    for attr in ("n_elements", "n_nodes", "n_points", "n_layers", "shape", "geometry"):
        value = _safe(obj, attr)
        if value is None:
            continue
        if attr == "geometry":
            result["geometry_type"] = type(value).__name__
        else:
            result[attr] = value if isinstance(value, (int, float, str, list, tuple)) else str(value)

    return result


def read(
    path: str,
    item: str | int | None = None,
    start: str | None = None,
    end: str | None = None,
    max_points: int = 500,
) -> dict:
    """读取一个 dfs / res 文件，返回统计量与（截断后的）序列。"""
    if _is_1d(path):
        return _read1d(path, item=item, start=start, end=end, max_points=max_points)
    try:
        mikeio = _mikeio()
    except ImportError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        kwargs: dict[str, Any] = {}
        if start or end:
            try:
                import pandas as pd  # noqa: PLC0415
                kwargs["time"] = (
                    pd.Timestamp(start) if start else None,
                    pd.Timestamp(end) if end else None,
                )
            except Exception:
                if start and end:
                    kwargs["time"] = (start, end)

        ds = mikeio.read(os.path.abspath(path), **kwargs)
    except Exception as exc:
        return _fail(path, exc)

    names = list(_safe(ds, "names", []) or [])
    time = _safe(ds, "time")
    try:
        n_steps = len(time) if time is not None else 0
        first = str(time[0]) if n_steps else None
        last = str(time[-1]) if n_steps else None
    except Exception:
        n_steps, first, last = 0, None, None

    payload: dict[str, Any] = {
        "ok": True,
        "path": os.path.abspath(path),
        "n_items": len(names),
        "names": names,
        "start": first,
        "end": last,
        "n_timesteps": n_steps,
    }

    # 选定要读的条目（支持 名字 / 序号 / 数字字符串）
    item = _coerce_index(item)
    if item is None:
        selected = names[:1]
    elif isinstance(item, int):
        if not names:
            return {**payload, "ok": False, "error": "结果文件没有条目"}
        if not -len(names) <= item < len(names):
            return {
                **payload,
                "ok": False,
                "error": f"条目序号越界: {item}（共 {len(names)} 项，合法范围 0..{len(names) - 1}）",
                "available_items": names,
            }
        selected = names[item : item + 1]
    else:
        selected = [item]
        missing = [n for n in selected if n not in names]
        if missing:
            return {
                **payload,
                "ok": False,
                "error": f"未匹配到条目: {', '.join(missing)}",
                "available_items": names,
            }
    payload["selected_items"] = selected

    series: dict[str, Any] = {}
    stats: list[dict] = []
    for name in selected:
        try:
            da = ds[name]
        except Exception as exc:
            series[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue

        try:
            values = da.to_numpy()
        except Exception:
            values = getattr(da, "values", None)

        try:
            import numpy as np  # noqa: PLC0415
            flat = np.asarray(values)
            n_total = int(flat.size)
            finite = flat[np.isfinite(flat)] if flat.dtype.kind == "f" else flat
            stats.append({
                "name": name,
                "shape": list(flat.shape),
                "n_total": n_total,
                "n_finite": int(finite.size),
                "min": float(np.min(finite)) if finite.size else None,
                "max": float(np.max(finite)) if finite.size else None,
                "mean": float(np.mean(finite)) if finite.size else None,
            })
            # 抽样，避免把巨量数据灌进上下文
            if n_total > max_points:
                idx = np.linspace(0, n_total - 1, max_points).astype(int)
                sampled = [None if not np.isfinite(v) else round(float(v), 6) for v in flat.reshape(-1)[idx]]
            else:
                sampled = [None if not np.isfinite(v) else round(float(v), 6) for v in flat.reshape(-1)]
            series[name] = {"sampled": sampled, "sampled_from": n_total}
        except Exception as exc:
            series[name] = {"error": f"{type(exc).__name__}: {exc}"}

    # 时间轴同样抽样，否则 481 步的 dfs0 会把上下文灌满
    try:
        stamps = [str(t) for t in ds.time]
    except Exception:
        stamps = []
    if len(stamps) > max_points:
        step = max(1, len(stamps) // max_points)
        payload["time"] = stamps[::step]
        payload["time_sampled"] = {"from": len(stamps), "step": step}
    else:
        payload["time"] = stamps

    payload["statistics"] = stats
    payload["series"] = series
    return payload


def plot(path: str, item: str | int | None = None, out_png: str | None = None) -> dict:
    """把 dfs / res 序列画成 PNG。"""
    if not out_png:
        out_png = os.path.splitext(os.path.abspath(path))[0] + "_plot.png"

    if _is_1d(path):
        return _plot1d(path, item, out_png)

    try:
        mikeio = _mikeio()
    except ImportError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        import matplotlib  # noqa: PLC0415
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # noqa: PLC0415

        ds = mikeio.read(os.path.abspath(path))
        names = [str(n) for n in (_safe(ds, "names", []) or [])]
        idx = _coerce_index(item)
        if idx is None:
            if not names:
                return {"ok": False, "error": "结果文件没有条目", "output": out_png}
            target = names[0]
        elif isinstance(idx, int):
            if not names:
                return {"ok": False, "error": "结果文件没有条目", "output": out_png}
            if not -len(names) <= idx < len(names):
                return {
                    "ok": False,
                    "error": f"条目序号越界: {idx}（共 {len(names)} 项，合法范围 0..{len(names) - 1}）",
                    "output": out_png,
                    "available_items": names,
                }
            target = names[idx]
        else:
            target = str(idx)
            if target not in names:
                return {
                    "ok": False,
                    "error": f"未匹配到条目: {target}",
                    "output": out_png,
                    "available_items": names,
                }
        da = ds[target]
        fig, ax = plt.subplots(figsize=(10, 4.5), dpi=130)
        try:
            da.plot(ax=ax)
        except Exception:
            ax.plot(da.to_numpy().reshape(-1))
            ax.set_xlabel("index")
        ax.set_title(f"{os.path.basename(path)} — {getattr(da, 'name', '')}")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_png)
        plt.close(fig)
        return {"ok": True, "path": out_png, "size_bytes": os.path.getsize(out_png)}
    except Exception as exc:
        return _fail(path, exc, output=out_png)
