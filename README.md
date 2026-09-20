# mikezero-mcp

用 MCP 无界面（headless）驱动 **DHI MIKE Zero** 的 MCP Server。

## 先说清楚一件事

**DHI 官方并没有叫 "MIKE Zero Commander" 的组件。** 我们验证过：

- 整个 `MIKE Zero 2023` 安装树里，文件名含 `commander` 的数量 = **0**
- GitHub / npm / PyPI / Gitee / DHI 官方文档里搜 `MIKE Zero Commander` = **0 命中**

> **名字大概是这么来的**：本机 `~/.workbuddy/mcp.json` 里原本就有一个 HEC-RAS 的
> MCP —— `ras-commander-mcp 0.3.2`（底层是 `ras-commander 0.102.0`，用于自动化
> HEC-RAS）。所以「MIKE Zero 的 Commander MCP」是**类推**出来的东西，
> 真实世界里并不存在。本工程就是它的 MIKE Zero 对应物。

MIKE Zero 真正的无界面入口是这三个（都在 `...\DHI\MIKE Zero\<年份>\bin\x64\`）：

| 程序 | 作用 | 替代了 GUI 里的 |
|---|---|---|
| `MzLaunch.exe` | 从 setup 文件启动模拟，支持 MPI / GPU / 优先级 | Launch Simulation Engine |
| `ToolboxShell.exe` | 运行 Toolbox setup（`.mzt/.21t/.3t/.lpkt/.mst`） | MIKE Zero Toolbox |
| `MzPlotCompApp.exe` | Plot Composer 批量出图 | Plot Composer |

本 server 就是把这套东西 + DHI 官方开源库 `mikeio`（读 dfs 结果）封装成 MCP 工具。

---

## 安装

已经装好了。如果要在别的机器上重建：

```bash
# 1. 建虚拟环境（mikeio 需要 Python >= 3.12）
<python> -m venv .venv

# 2. 装依赖：mcp 必需；mikeio / mikeio1d 可选（只有读结果才需要）
.venv/Scripts/python.exe -m pip install -e .
.venv/Scripts/python.exe -m pip install -e ".[results]"   # mikeio：dfs0/1/2/3/dfsu
.venv/Scripts/python.exe -m pip install mikeio1d          # .res1d / .res11（MIKE 11）
```

本机实际装好的版本：`mcp 2.2.0` + `mikeio 3.3.0` + `mikeio1d 1.3.1`（Python 3.13.14）。

> 注意 `mcp` 2.x 把 `FastMCP` 改名为 `MCPServer`；`mikezero_mcp/server.py` 里做了
> 两代 API 的兼容适配，1.x / 2.x 都能跑。

## 注册到 WorkBuddy

编辑 `~/.workbuddy/mcp.json`（注意不是 `~/.workbuddy/.mcp.json`）：

```json
{
  "mcpServers": {
    "mikezero": {
      "type": "stdio",
      "command": "C:\\Users\\<你>\\.workbuddy\\mcp-servers\\mikezero-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-X", "utf8", "-m", "mikezero_mcp.server"],
      "timeout": 120000,
      "disabled": false
    }
  }
}
```

### ⚠️ 千万不要写 `env` 字段（本工程踩过的最隐蔽的坑）

MCP 宿主（Electron/Node）用 `child_process.spawn(cmd, { env })` 启动子进程，
而 Node 的 `env` 是**整体替换**语义，**不是合并**。只要 `mcp.json` 里写了 `env`，
`SystemRoot` 就会丢失；Windows 上 Python 首次 `import asyncio` 会连带
`import _overlapped`，winsock 初始化拿不到 SystemRoot 直接抛：

```
OSError: [WinError 10106] 无法加载或初始化请求的服务提供程序
```

于是 server 在**导入阶段**就崩掉，宿主侧只显示「连接失败 / 出错了」，毫无线索。
本机实测（同一台机器、同一份代码，只改启动环境）：

| 启动时的环境 | 结果 |
|---|---|
| 完整继承父进程环境 | ✅ 握手成功 |
| 只给 `{PYTHONPATH, PYTHONUTF8, PYTHONIOENCODING}` | ❌ `WinError 10106`，rc=1 |
| 只给 `{PYTHONPATH, SystemRoot}` | ✅ 握手成功 |
| 完全空环境（连 SystemRoot 都没有） | ✅ 握手成功（靠下面的自愈代码） |

所以本工程做了两件事，让启动与宿主的 env 语义**完全无关**：

1. `mikezero_mcp/__init__.py` 在导入任何第三方库之前，用
   `os.environ.setdefault()` 把 `SystemRoot` / `windir` / `TEMP` / `TMP`
   补回去 —— 包初始化一定早于 `server.py` 里的 `import mcp`。
2. **不依赖 `PYTHONPATH`**：靠 `pip install -e .` 的可编辑安装让
   `mikezero_mcp` 可被导入；编码需求改用命令行开关 `-X utf8` 表达
   （argv 不受 env 语义影响，环境变量才会）。

验证脚本：`scripts/host_sim_test.py` —— 它直接从 `mcp.json` 读配置，
分别用「继承 / 替换 / 空环境」三种基环境启动并真实握手。

写完后**不会自动生效**：到连接器管理页右上角的「自定义连接器」入口，对新出现的
server 点「信任」才会启动。注意：**改动过 `mcp.json` 内容后，之前的信任记录
会因配置指纹变化而失效，需要重新点一次。**

---

## 工具一览（15 个）

### 环境与勘察

| 工具 | 作用 |
|---|---|
| `mz_env` | 探测安装位置、bin/x64、三个无界面入口、引擎数量、mikeio 可用性 |
| `mz_engines` | 列出 `MzEngines.cfg` 的 扩展名 → 引擎 exe 映射（如 `.m21fm` → `FemEngineHD.exe`） |
| `mz_scan` | 扫描工程目录，按类型归类 setup 文件与结果文件 |
| `mz_read_setup` | 解析 setup 文件（PFS 格式）：引擎、段结构、输入文件引用、关键参数 |

### 运行模拟

| 工具 | 作用 |
|---|---|
| `mz_run` | 无界面启动单个模拟，返回作业 id（默认后台跑） |
| `mz_run_batch` | 批量顺序执行多个 setup，生成 `.bat`（DHI 官方做法）并后台运行 |
| `mz_toolbox` | 运行 Toolbox setup |

### 作业跟踪

| 工具 | 作用 |
|---|---|
| `mz_jobs` | 列出最近作业 |
| `mz_progress` | **实时进度**：百分比、当前模拟时刻、步数、速度、预计剩余时间、完成标志 |
| `mz_job_status` | 进程是否存活、耗时、日志文件、报错行、**新增的结果文件**（递归扫描结果子目录） |
| `mz_job_log` | 读日志末尾 N 行（优先给 `._spi_`） |
| `mz_job_kill` | 终止作业（`taskkill /T`，连带子进程） |

### 读结果（需要 mikeio / mikeio1d）

| 工具 | 作用 |
|---|---|
| `mz_dfs_info` | 元信息：类型、条目名/单位、时间轴、网格规模；1D 结果额外给出河段与节点 |
| `mz_dfs_read` | 读数据：最大/最小/均值 + 抽样序列（自动截断，不撑爆上下文） |
| `mz_dfs_plot` | 画成 PNG |

支持的格式：

| 格式 | 库 | 说明 |
|---|---|---|
| `.dfs0/.dfs1/.dfs2/.dfs3/.dfsu` | `mikeio` | 时间序列 / 栅格 / 非结构网格 |
| `.res1d/.res11` | `mikeio1d` | MIKE 11 / 1D 结果，列名形如 `Water Level:CALI:0`（量:河段:里程） |

---

## 用法示例

**1. 先看环境**

```
mz_env()
mz_engines(extension=".m21fm")
```

**2. 勘察工程目录**

```
mz_scan(root="D:\\Projects\\Odense")
mz_read_setup(setup="D:\\Projects\\Odense\\odense.m3fm")
```

**3. 跑一个模拟（8 子域 MPI）**

```
mz_run(setup="D:\\Projects\\Odense\\odense.m3fm", mpi=8)
→ {"ok": true, "runner": "mzlaunch",
   "job": {"id": "a1b2c3d4e5f6", "status": "running", ...}}

mz_progress(job_id="a1b2c3d4e5f6")
→ {"progress_percent": 42.5, "current_sim_time": "2000/1/13 4:20:00",
   "step": 1897, "total_steps": 4464, "time_left_seconds": 510, "completed": false}

mz_job_status(job_id="a1b2c3d4e5f6")
mz_job_log(job_id="a1b2c3d4e5f6", lines=100)
```

**3b. 直调引擎（跑完保留 `._spi_`）**

```
mz_run(setup="D:\\P\\Test1.sim11", direct=True)
→ {"ok": true, "runner": "direct:MIKE11.exe",
   "job": {"cmd": ["...\\MIKE11.exe", "-b", "...\\Test1.sim11"], ...}}
```

`direct=True` 会按 `MzEngines.cfg` 从扩展名找引擎，并套用该引擎自己的开关
（MIKE 11 用 `-b`，MIKE SHE 用 `/Silent`，FemEngine 不加）。

**4. 批量跑三个工况**

```
mz_run_batch(setups=[
  "D:\\P\\Scenario1.m21fm",
  "D:\\P\\Scenario2.m21fm",
  "D:\\P\\Scenario3.m21fm"
])
```

**5. 读结果**

```
mz_dfs_info(path="D:\\P\\Scenario1_Result.dfsu")
mz_dfs_read(path="D:\\P\\Result.dfs0", item="Water Level", max_points=300)
mz_dfs_plot(path="D:\\P\\Result.dfs0")
```

**6. 读 MIKE 11 结果（1D）**

```
mz_dfs_info(path="D:\\P\\CALI-HD.res11")
# → quantities: ['Water Level', 'Discharge'], n_reaches: 3, n_nodes: 4

mz_dfs_read(path="D:\\P\\CALI-HD.res11", item="Water Level")      # 按量名
mz_dfs_read(path="D:\\P\\CALI-HD.res11", item="Water Level:CALI:0")  # 按列名
mz_dfs_plot(path="D:\\P\\CALI-HD.res11")
```

---

## 命令行参数对照（实测自 exe 内嵌 usage）

### `MzLaunch.exe`（`mz_run` 默认走这条）

> `Usage: MzLaunch [model-setup-file] [Options]`

| 参数 | 含义 | 对应本服务参数 |
|---|---|---|
| `-x` | 模拟结束后退出 MzLaunch | `exit_when_done=True`（默认） |
| `-e <engine>` | 引擎 exe 完整路径 | `engine=` / `engine_flag=` |
| `-z <mzp\|folder>` | 结果目录 | `result_dir=` |
| `-y 1\|2\|3\|4` | CPU 优先级（1 低，2 低于正常，3 正常，4 高于正常） | `priority=` |
| `-mpi N` | N 个子域做 MPI 并行 | `mpi=` |
| `-gpu N` / `-gpusp N` | 双精度 / 单精度 GPU，N 为子域数 | `gpu=` |
| `-x86` | 在 64 位系统上用 32 位引擎 | — |
| `-r <ms>` | 扫描模拟日志的刷新间隔，默认 200 ms | — |
| `-m <email>` | 模拟结束发邮件通知 | — |
| `-h` | 显示帮助 —— **弹对话框**，不是打印到控制台 | — |
| `-run` | ⚠️ **未在官方 usage 中列出**。实测加不加都能正常跑，本服务默认**不加** | `silent=` |

### 引擎直调（`mz_run(direct=True)`）—— 每个引擎的开关都不一样

| 引擎 | 官方 usage | 批处理/静默开关 |
|---|---|---|
| `MIKE11.exe` | `Syntax: MIKE11 [Options] SimulationFileName` | `-b` 批处理（出错不弹框，**进度照写**）<br>`-s` 全静默（**会把 `._spi_` 清成 0 字节**）<br>`-v` 仅校验 · `-w` 独立进度窗 · `-lx` 语言 |
| `MSHE_Simulation.exe` | `Usage: MShe_Simulation.exe [/Silent] [/PP] [/WM] [/WQ] pfs` | `/Silent`（斜杠风格） |
| `FemEngineHD.exe` | `Usage: <pfs-file>` | 无静默开关；可选 `-gpu` / `-gpusp` |

所以 `direct=True` 时本服务按 **exe 名查表**选开关，不会拿 `-run` 或 `-b` 乱套。

---

## 日志与进度（实测规律）

跑一次 MIKE 11 之后，setup 目录里会出现这些文件：

| 文件 | 内容 | 用途 |
|---|---|---|
| `<setup名>._spi_` | 逐行进度纯文本 | **最佳进度来源**（`mz_progress` 就读它） |
| `<setup名>-SimStat.Log` | `// Simulation Started : <时间>` / `Simulation Ended : <时间>` | 判断是否跑完 |
| `<setup名>.Log` | 通常为空，出错时才有内容 | 查错 |
| `<setup名>-Info.Log` | 通常为空 | 查错 |

`._spi_` 的真实内容（节选）：

```
MESSAG Setup Finished (858 Kb)
MESSAG Starting time loop (858 Kb)
STATUS 2000/1/1 0:10:00        <- 当前模拟时刻
MESSAG 1 of 4464               <- 步数进度
PROGRE 0                       <- 进度千分比（1000 = 完成）
COMSPD 1                       <- 计算速度
TIMLFT 182983                  <- 预计剩余秒数
...
MESSAG 4464 of 4464
PROGRE 1000
MESSAG Completed               <- 完成标志
CLOSE  0                       <- 0 = 正常退出
```

**FemEngine 的 SPI 字段名不一样**（MIKE 21/3 FM 系列，实测 `.m3fm`）：

```
MESSAG Initializing
STALBL Simulation date and time [YYYY-MM-DD hh:mm:ss]
STATUS 2004-01-01 00:00:00     <- 当前模拟时刻
MESLBL Simulation time step
MESSAG Time step: 0            <- 步数进度（不是 MIKE 11 的 "N of M"）
PROGRE          0              <- 千分比
COMSPD      14329              <- 计算速度
```

即：里程碑标签是 `STALBL` / `MESLBL`，步数写成 `Time step: N`，
**没有 `TIMLFT`**（预计剩余时间）—— 所以 FemEngine 上 `time_left_seconds`
恒为 0，别把它当成异常。

**三个必须记住的坑：**

1. `-s`（全静默）会把 `._spi_` **清成 0 字节**，进度就没了。
   后台跑又要进度 → 用 `-b`，不要用 `-s`。
2. **经 MzLaunch 启动时，模拟一结束 `._spi_` 就被删掉**；直调引擎则保留。
   本服务会自动把 SPI 归档到 `state/jobs/<作业id>.spi`，跑完仍可回溯。
3. **MzLaunch 模式下，作业结束后 `mz_progress` 会停在归档的最后一帧**
   （实测 81.4% / `Time step: 391`），因为引擎收尾写的那几帧随 SPI 一起被删了。
   这是归档机制的必然结果，**不是卡住** —— 判断是否跑完请以 `status`
   （`finished`）和 `exit_code`（`0`）为准，别盯着百分比等 100%。
   直调引擎模式下 SPI 保留，能看到完整的 100%。

---

## 结果文件在哪

**不在 setup 根目录**，而在 `<setup 文件名> - Result Files\` 子目录里：

```
demo/mike11_smoke/
├── Test1.sim11                        ← setup
├── Test1._spi_                        ← 进度
├── Test1-SimStat.Log                  ← 起止时间
└── Test1.sim11 - Result Files/
    └── Test1.res11   (987,772 B)      ← 结果在这里
```

所以 `mz_job_status` 的目录快照是**递归的**（只扫根目录会把结果全漏掉）。

---

## 已知限制

1. **`MzLaunch.exe` 是 GUI 子系统程序，没有可用 stdout**（`-h` 也会弹对话框）。
   本 server 不依赖 stdout，进度全部来自引擎写的 `._spi_` 与 `-SimStat.Log`。
2. **许可**：读结果（`mz_dfs_*`）免许可；**跑模拟需要有效的 DHI 许可**，
   否则 MIKE Zero 会以 DEMO 模式启动并限制模型规模。
3. **许可通常是单机独占的 → 同一时刻只跑一个模拟。** 上一个作业的进程还没
   完全退出就启动下一个，引擎会因拿不到许可快速失败（退出码非 0、`._spi_`
   不生成）。实测：紧接 MzLaunch 之后再起一个引擎，耗时 16.9 s 后 rc=1；
   等进程退出后再起，1.6 s 就 rc=0。
   `mz_run` 检测到其他作业仍在运行时会返回 `warning` 提醒。
   要多工况请用 `mz_run_batch`（内部顺序执行），不要手工并发；
   单个模型内部并行用 `-mpi`。
4. `mikeio` 需要 **Python >= 3.12**；`mikeplus`（MIKE+ 专用）才要求 3.9–3.11，
   两者不要混。
5. `mikeio1d` 依赖 `pythonnet` + .NET 运行时。本机已装通，换机器时若导入失败，
   先确认 .NET 运行时可用。
6. `.res11` 读的是 MIKE 11 结果；**MIKE 11 / MIKE FLOOD 自 2024 版起已并入 MIKE+**，
   2023 及更早版本仍可正常使用。
7. 各引擎的静默开关**不统一**（`-b` / `/Silent` / 无），跨引擎切换时注意。
   表里没有的引擎，本服务不会擅自加开关，可用 `extra_args` 手工补。
8. **启动环境**：`mcp.json` 里不要写 `env`（原理见上文「注册到 WorkBuddy」），
   否则可能以 `OSError: [WinError 10106]` 在导入阶段崩溃。代码已做自愈，
   但仍建议保持配置干净。

## 冒烟 / 集成测试

不用启动 MCP 客户端，直接跑脚本：

```bash
# 1) 环境 / 解析 / 扫描（快，不跑模拟）
.venv/Scripts/python.exe scripts/smoke_test.py

# 2) 真跑一次 MIKE 11 模拟，对照四种入口
.venv/Scripts/python.exe scripts/run_smoke.py A   # MIKE11.exe -b -s
.venv/Scripts/python.exe scripts/run_smoke.py C   # MIKE11.exe -b（保留进度）
.venv/Scripts/python.exe scripts/run_smoke.py B   # MzLaunch -run -x
.venv/Scripts/python.exe scripts/run_smoke.py D   # MzLaunch -x

# 3) 作业 / 进度 / 结果 全链路集成测试
.venv/Scripts/python.exe scripts/test_run_integration.py

# 4) 排查用：单步观察状态/进度/归档
.venv/Scripts/python.exe scripts/diag_direct.py

# 5) 启动链路：模拟宿主（继承 / 替换 / 空环境）启动并真实握手
.venv/Scripts/python.exe scripts/host_sim_test.py
```

### 本机实测结果

**环境与解析**：15 个工具；识别到 MIKE Zero 2023；解析出 30 条引擎映射；
成功解析 5 个真实官方算例（`.m21fm/.m3fm/.sim11/.she/.couple`）；
扫描 Examples 1684 个文件（392 setup + 776 结果）。

**真实跑通模拟** —— 用的是官方算例
`MIKE_ZERO\AutoCal\MIKE_11\Example1\Setup\Test1.sim11` 的副本
（MIKE 11 HD，2000-01-01 → 2000-02-01，步长 10 s，共 4464 步）：

| 入口 | 命令 | 耗时 | 退出码 | 结果 | `._spi_` |
|---|---|---|---|---|---|
| 直调·全静默 | `MIKE11.exe -b -s <setup>` | 3.0 s | 0 | `Test1.res11` 987,772 B | **被清成 0 字节** |
| 直调·保留进度 | `MIKE11.exe -b <setup>` | 2.4 s | 0 | 同上 | 1,666 B（完整进度） |
| MzLaunch | `MzLaunch.exe <setup> -run -x` | 3.6 s | 0 | 同上 | **结束后被删** |
| MzLaunch | `MzLaunch.exe <setup> -x` | 2.4 s | 0 | 同上 | 同上 |

`mz_progress` 的实时轨迹（0.8 s 采样）：

```
[0.0s] status=running   None%    spi 尚未生成
[0.8s] status=running    0.0%   t=2000/1/1 0:10:00  step=1/4464     speed=1       left=182983s
[1.6s] status=finished 100.0%   t=2000/2/1          step=4464/4464  speed=239820  left=0s  done=True
```

**结果读取**：`mz_dfs_read` 从 `CALI-HD.res11` 读出水位 min **56.272** / max **58.610**；
`bathy.dfs2` 读出 -10.0 m；`Waves_Constant.dfsu` 读出有效波高。

**FemEngine 回归（MIKE 3 FM / `.m3fm`）** —— 算例
`Examples\MIKE_3\FlowModel_FM\ST\Basin\Sim1.m3fm` 的副本
（3D 水动力 + 输沙，12 线程，480 步 × 10 s = 4800 s，网格 174 单元）：

| 入口 | 命令 | 耗时 | 退出码 | 结果 | `._spi_` |
|---|---|---|---|---|---|
| MzLaunch | `MzLaunch.exe <setup> -x` | 16.3 s | 0 | 4 个 dfs 文件 | **结束后被删** |
| 直调引擎 | `FemEngineHD.exe <setup>` | 23.0 s | 0 | 同上 | **保留**（2,158 B） |

日志尾部（`Sim1.log`）：`Calculation` 32.4 s CPU / 3.3 s elapsed，峰值内存
**61.7 MB**，`Number of warnings: 0`，`Normal run completion`。
产出：`Sim1_HD.dfs0`（2 点位水位）、`Sim1_HD.dfs1`、`Sim1_ST.dfs1`、
`Sim1_ST.dfsu`（Dfsu2DH，7 个条目）。

`mz_progress` 在**直调**模式下可用（MzLaunch 会删 SPI，只能读归档）：

```
status=running    81.8%   t=2004-01-01 01:05:30   Time step: 393   speed=12634
status=running    99.9%   t=2004-01-01 01:20:00   Time step: 480   speed=11794
status=finished   exit=0  elapsed=23.0s
```

读出的量：`Point 1 水位` 481 步（min **-0.0172** / max **0.0** / mean -0.0144 m）；
`Depth average U velocity` 41×174（min **-0.00355** / max **0.00483** m/s）。

> 注意：该 setup 里写着 `number_of_domains = 16`，**实际只起了 1 个子域**
> （日志明确 `Number of subdomains: 1`，12 线程/子域）—— 分域数由许可与启动方式
> 决定，不能只看 setup 里的声明值。

**MCP 协议**：真实 stdio 握手成功（protocol 2025-11-25），15 个工具可见可调。

**启动环境语义**（`scripts/host_sim_test.py`，从 `mcp.json` 读配置真实启动）：
`mikezero` 与 `hecras` 两个 server 在「继承 / 替换 / 空环境」三种基环境下
均能完成握手 —— 即本 server 的启动与宿主的 env 语义无关。

### 启动失败（宿主里显示「出错了」）的排查顺序

遇到宿主侧加载失败，按这个顺序查，**别急着怀疑代码**：

1. **先证明 server 本身是好的**：直接跑 `scripts/host_sim_test.py`。
   它通过 → 问题在宿主侧的环境/配置，不在 server。
2. **看宿主日志**：`~/.workbuddy/logs/daemon.log`、`main.log`，
   按 server 名 grep；MCP app 目录扫描在 `mcp-apps-diag.log`
   （`catalog.refresh scanned=… accepted=…`）。
3. **确认配置真的被读**：`~/.workbuddy/mcp-approvals.json` 里有该 server 的
   指纹记录，说明宿主识别到了这条配置。
4. **最常见的两个原因**：
   - `env` 字段导致 `WinError 10106`（见上文，已自愈）；
   - 改完 `mcp.json` 后**没重启宿主 / 没重新点信任**（指纹变了）。

### 真跑之后才发现的坑（都已修）

1. **结果在 `- Result Files` 子目录** —— 目录快照原本是非递归的，结果全被漏掉。
2. **`-s` 会清空 `._spi_`** —— 想后台跑又要进度，只能用 `-b`。
3. **MzLaunch 结束后删 `._spi_`** —— 所以本服务会归档到 `state/jobs/<id>.spi`。
4. **上一轮遗留的 SPI / SimStat 会污染新作业** —— 新作业开局就被判「已完成」。
   现在用「启动时指纹 + mtime ≥ started_at」双重过滤。
5. **各引擎开关不统一** —— `-b` / `/Silent` / 无，必须按 exe 名查表。
6. **`mz_progress` 与 `mz_job_status` 的状态判断必须共用一套逻辑** —— 否则一个说
   `running` 一个说 `finished`。现已抽成 `JobRegistry._sync()`。
7. **退出码会永远拿不到** —— 原实现只在状态还是 `running` 时才 `poll()`；一旦状态
   被 SPI 判定为 `finished`，就再也不取退出码，`exit_code` 恒为 `null`。
   现在只要还没拿到码就继续 poll。
8. **许可证单机独占** —— 两个模拟挨着跑，第二个会因拿不到许可而 `rc=1`
   （实测耗 16.9 s 才失败）。`mz_run` 现在检测到并发会返回 `warning`。
9. **`mz_dfs_read` / `mz_dfs_plot` 的「整数序号」其实是坏的**（FemEngine 回归时抓到）：
   - `plot` 把 `item` 直接透传给 `mikeio.read(items=...)`，传 `0` 会被当成名字，
     报 `KeyError: Selected item name not found`；
   - `read` 虽然写了 `isinstance(item, int)` 分支，但**实测 item 抵达时是字符串**
     （宿主会把 JSON 里的整数序号序列化成 `"1"`），于是走进「按名字查」的分支，
     同样 KeyError，且静默降级成 `series["1"]["error"]`。

   修法：新增 `_coerce_index()` 把纯数字串转成 int，四条路径（`read`/`plot`/
   `_read1d`/`_plot1d`）统一走它；`plot` 改为先读全部再按名字取；
   越界/无匹配返回带候选清单的友好错误。
   顺带把 `read` 返回的 `time` 也做了抽样（原先 481 步的 dfs0 会把上下文灌满）。
   回归脚本：`scripts/verify_dfs_item_fix.py`（13/13 通过）。

## 目录结构

```
mikezero-mcp/
├── mikezero_mcp/
│   ├── locate.py    定位安装、bin/x64、无界面入口
│   ├── pfs.py       MzEngines.cfg + PFS setup 解析
│   ├── spi.py       解析 ._spi_ 进度与 -SimStat.Log
│   ├── jobs.py      启动模拟（MzLaunch / 直调引擎）、作业注册表、SPI 归档
│   ├── dfs.py       mikeio / mikeio1d 读结果（可选依赖）
│   └── server.py    MCP server 与 15 个工具
├── scripts/
│   ├── smoke_test.py             环境 / 解析 / 扫描 冒烟
│   ├── run_smoke.py              真跑一次模拟（A/C/B/D 四种入口对照）
│   ├── test_run_integration.py   作业 / 进度 / 结果 全链路集成测试
│   ├── diag_direct.py            单步观察状态 / 进度 / 归档
│   ├── verify_dfs_item_fix.py    结果读取入参回归（名字 / 序号 / 越界）
│   └── host_sim_test.py          模拟宿主启动（继承 / 替换 / 空环境）
├── demo/                         【本地目录，不入库】官方算例副本，见下方说明
├── state/                        【本地目录，不入库】作业元数据 + SPI 归档
├── pyproject.toml
├── .gitignore
└── README.md
```

> **`demo/` 与 `state/` 为什么被 `.gitignore` 排除？**
> `state/` 是运行时数据，含本机绝对路径；`demo/` 放的是从 MIKE Zero 安装目录
> 复制出来的**官方算例**，版权属 DHI，随包分发会侵权。
> 想复现本文档里的实测，按「冒烟 / 集成测试」一节的说明，自己把算例拷进
> `demo/` 即可 —— 目录名对上就行。
