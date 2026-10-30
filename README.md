# 炫彩精灵颜色统计工具

针对《洛克王国：世界》炫彩精灵的手动统计工具。**不做任何自动点击/注入**：你负责点头像、点炫彩星、按 ESC，程序只负责识别当前面板、计数和把光标移到下一只。

> 旧的 v1 自动点击脚本（`auto_detect.py` / `gui.py`）已归档到 [`legacy/`](legacy/README.md)，不要再用。

## 快速开始

建议 Python 3.10 – 3.13。当前机器上的 `python` 是 3.14 且未装依赖；Anaconda 的 `kingdom` 环境（3.10）已具备 numpy/cv2/mss/pywin32/keyboard。

```bat
cd C:\Users\maxcen\kingdom
py -3.13 -m venv .venv
:: 如果 py -3.13 不可用，改用本机已有的 Anaconda：
:: C:\Users\maxcen\anaconda3\python.exe -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python manual_count.py
```

或者用已有的 Anaconda `kingdom` 环境：

```bat
conda activate kingdom
python -m pip install -r requirements.txt
python manual_count.py
```

如果不想记路径，直接双击 [`run.bat`](run.bat)：它会优先用 `.venv`，其次用 `%USERPROFILE%\anaconda3\envs\kingdom\python.exe`，再退回基础 Anaconda 的 `python`。

> **重要：** 当前游戏 `NRC-Win64-Shipping.exe` 是以管理员权限运行的。Windows 会阻止普通权限程序接收游戏窗口的 F7/F8/F9，所以请用 [`run_admin.bat`](run_admin.bat)（会弹 UAC），或右键 `run.bat` →「以管理员身份运行」。`--check` 会打印“本工具/游戏”的权限状态。

先做一次自检（不打开窗口）：

```bat
python manual_count.py --check
```

它会检查依赖、游戏窗口、色卡命名（160 张是否完整）和会话文件。

> **本地资源说明：** 色卡 `炫彩/`、图标模板 `icons/` 和测试 `tests/` 属于本机数据/开发内容，当前不随仓库推送（已在 `.gitignore` 中排除）。克隆仓库后需要自行放入色卡和图标模板；测试目录保留在本地即可。

## 使用流程

1. 游戏固定为 **1920×1080 窗口模式**，把鼠标移到游戏窗口上，点「3 秒后锁定」。
2. 首次使用点「重新校准」：
   - 框选炫彩面板区域（颜色一/颜色二/粒子所在的那张卡）；
   - 框选精灵列表网格区，填列数/行数；
   - 框选详情页炫彩星位置（可选，图标库未命中时兜底）；
   - 可选：框选详情页精灵区，切换识别更稳。
3. 色卡文件夹选 `炫彩`，加载后顶部会显示 `已加载 160 张`。新赛季把卡丢进文件夹，点「重新加载」即可；命名不合规会在日志里标出。
4. **学炫彩星（每个赛季/异色一次）**：打开一只炫彩精灵的详情页，鼠标悬停**最右侧**的炫彩星（不要悬停左侧徽章），按 **F7**；工具会存一个图标模板。换普通/异色/新赛季时再学一次。顶部「⑤ 炫彩星图标」会显示模板数。
5. 点「开始」（从第 N 格），然后按游戏正常节奏操作：
   - 点一下当前头像 → 程序检测到左键点击/画面切换后，**立刻**把光标移到炫彩星（默认固定星位，约 0.1–0.2 s）；
   - 点炫彩星 → 面板出现并稳定后自动计数；
   - 按 ESC / 点空白关闭 → 程序把光标移到下一只头像；
   - 没有炫彩的精灵按 **F9** 跳过。
6. 识别错了点「撤销上一条」；重启程序会自动恢复之前的统计。
7. 需要分享时点「导出数据」/「生成统计色卡」。

### 快捷键

| 键 | 作用 |
|---|---|
| F8 | 手动识别当前面板（自动流程的兜底；连按会去重） |
| F9 | 跳过当前精灵 / 本页完成后从第 1 格继续 |
| F7 | 学习当前鼠标位置的炫彩星图标（悬停最右侧炫彩星时按） |

> 如果游戏以管理员身份运行（当前就是），Windows 会挡住普通权限程序的 F7/F8/F9；请用 `run_admin.bat` 以管理员运行本工具，或直接用界面按钮。

## 识别与数据

- 匹配：160 张色卡预计算归一化特征矩阵，一次点积得到 top1/top2；单次约 1 ms（v1 是 160 次 `matchTemplate`，约 250 ms）。
- 只有 top1 分数 ≥ `open_similarity` 且与 top2 拉开 `open_margin` 才自动记录；差距太小会走“两个主色”兜底，仍失败则存为未匹配样本。
- 每记录一只追加一行到 `output/session.jsonl`，重启按事件回放；撤销/清空也是事件，不会丢历史。
- 每次面板截图保存在 `output/samples/<组合>/`，可用于复盘和阈值回归。

### 炫彩构成（重要）

- 普通炫彩 = **39 种颜色组合 × 4 种粒子** = 156 张卡；色卡库里的 `亮X亮` / `亮X暗` 就是这些。
- 黑白炫彩 = 固定黑白 + 固定 **四角星**（文件名里的 `_无` 是旧命名，导出时按四角星处理）。
- 赛季炫彩 = 每个赛季一张专属卡（暗夜拾光 / 狂欢怪谈 / 铅字绘梦），有自己的炫彩星图标。
- **异色是独立属性**：只改精灵基础配色，没有粒子/发光，也**不会改变炫彩的 39×4 颜色/粒子组合**。所以色卡库不需要异色卡，本工具只按炫彩组合统计，不把异色当独立类别。
- **炫彩星不是单一图标**：普通/异色/每个赛季都可能不同。工具用 `icons/` 模板库在详情页右侧找“最右侧”的炫彩星；左侧星章是徽章，不参与。模板用 F7 学习，新赛季加一张即可。

### 输出文件

| 文件 | 内容 |
|---|---|
| `output/session.jsonl` | 事件流（record/undo/clear），断点续统计的依据 |
| `output/manual_result.csv` | 类型,颜色一,颜色二,粒子,组合,数量（Excel 可直接打开） |
| `output/combo_counts.json` | 结构化导出 + 覆盖率 + 缺失组合 |
| `output/samples/` | 每次识别的面板截图（按组合分文件夹） |
| `output/stat_card.png` | 统计色卡长图 |
| `output/app.log` | 运行日志 |

`output/` 已在 `.gitignore` 中；色卡库 `炫彩/` 和 `xuancai2_mapping.csv` 现在可以纳入版本管理/备份。

## 命令行工具

```bat
python manual_count.py --check
python manual_count.py --replay output\legacy\last_panel.png
python manual_count.py --eval output\samples
```

- `--check`：环境/窗口/色卡/会话检查，不启动 GUI。
- `--replay DIR`：对目录（或单张图）里的面板截图做离线识别，输出 top1、分数、top1-top2 差值。
- `--eval [DIR]`：把 `output/samples/<组合>/` 的目录名当答案，统计准确率；调阈值时先跑这个。

## 配置

`manual_cfg.json` 是本机配置（已加入 `.gitignore`），首次使用可参考 [`manual_cfg.example.json`](manual_cfg.example.json)。

| 字段 | 说明 |
|---|---|
| `hwnd` / `window_title` | 窗口句柄 + 标题；句柄失效会按标题重新查找 |
| `card_dir` | 色卡目录，支持相对项目根目录 |
| `icon_dir` | 炫彩星图标模板目录（默认 `icons`） |
| `icon_hotkey` / `icon_threshold` | 学习快捷键（默认 F7）/ 图标匹配阈值 |
| `use_icon_search` | 默认 `false`，直接用 `star_point`（最快）；星位会漂移时再打开图标搜索 |
| `ref_w` / `ref_h` | 上次校准时的客户区尺寸；换窗口大小时按比例缩放 ROI |
| `panel_roi` / `list_region` / `star_point` / `detail_roi` / `icon_roi` | 校准区域（相对客户区）；`icon_roi` 是找炫彩星的右侧区域，默认右半屏 |
| `cols` / `rows` | 列表网格列/行 |
| `min_similarity` / `min_margin` | 全图匹配最低分 / top1-top2 最小差值 |
| `open_similarity` / `open_margin` | 判定“色卡已打开”的阈值 |
| `poll_ms` | 状态轮询间隔，默认 60 ms |
| `capture_dedupe_sec` | 同面板 F8 去重窗口，默认 0.8 s |
| `save_samples` | 是否保存每次面板截图 |

## 测试

> `tests/` 目前不随仓库推送；本地保留测试目录后执行：

```bat
python -m unittest discover -s tests -v
```

覆盖：文件名解析、黑白/赛季字段、JSONL 回放/撤销/清空/旧 CSV 导入、快速匹配（含无效文件名过滤）、炫彩星图标“取最右侧”匹配。

## 常见问题

- **F8/F9/F7 没反应**：当前游戏以管理员运行，Windows 会挡住普通权限程序的全局热键。请用 [`run_admin.bat`](run_admin.bat)，或右键 [`run.bat`](run.bat)「以管理员身份运行」；`--check` 会打印两边权限。本版也加了 `GetAsyncKeyState` 全局轮询，但跨管理员窗口仍需提权。
- **炫彩星位置不对**：确认学的是最右侧图标，不是左侧徽章；换分辨率/UI 缩放后重新按 F7 学。
- **换了窗口大小后识别错位**：程序会按 `ref_w/ref_h` 自动缩放；如果仍不准，点「重新校准」框选面板/网格/星位。
- **找不到窗口**：重新锁定；`window_title` 要包含游戏标题的一部分；`--check` 会打印当前配置。
- **识别分数低**：看 `output/samples/_unmatched/`，确认面板区域是否框准；跑 `--eval` 看阈值是否合适。
- **统计图太大**：勾选「统计图只显示已拥有」。
- **窗口移动/缩放后**：重新校准；建议固定 1920×1080 窗口模式。

## 文件说明

| 文件 | 作用 |
|---|---|
| `manual_count.py` | 主程序：GUI、窗口/截图、状态机、会话、CLI |
| `xc_cards.py` | 色卡解析、快速匹配、统计色卡渲染 |
| `xc_icons.py` | 炫彩星图标模板库（多尺度 + 取最右侧） |
| `xc_session.py` | JSONL 会话持久化/回放/旧 CSV 导入 |
| `xuancai_count.py` | 颜色提取库（`extract_two_colors`）及旧版 CLI |
| `manual_tracker.py` | 备用纯手动记录器（不截图，按组合编号记录），与主程序二选一 |
| `炫彩/` | 160 张色卡（156 普通 + 1 黑白 + 3 赛季） |
| `icons/` | 炫彩星模板（F7 学习；普通/异色/赛季可各一张） |
| `xuancai2_mapping.csv` | 色卡来源/命名对照（UTF-8 BOM，Excel 可直接打开） |
| `legacy/` | v1 自动点击入口，已归档，不建议运行 |
| `run.bat` | 普通权限启动 |
| `run_admin.bat` | 自提权启动；游戏是管理员时用（当前推荐） |
