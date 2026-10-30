# -*- coding: utf-8 -*-
"""manual_count.py —— 炫彩精灵手动统计工具（避开 ACE 反作弊）

原理：不点击、不注入。你手动点开某只炫彩精灵的颜色面板，程序抓取面板区域，
与色卡库比对后自动计数；自动流程只负责把光标移到“下一个头像/炫彩星”。

v2 相比 v1 的关键变化
--------------------
- 匹配：预计算归一化特征矩阵 + 一次点积，单次 < 1 ms（v1 约 250 ms）；
- 记录：每只写入 output/session.jsonl，重启可续统计，撤销/清空也落盘；
- 导出：保留原始卡名，黑白/赛季的 CSV 字段不再丢失；
- 窗口：IsWindow + 标题/进程回退，句柄失效会自动重新查找；
- 自动流程：只抓面板/当前格/详情小 ROI，100 ms 轮询，top1-top2 差值保护；
- 辅助：网格进度图、只显示已拥有的统计图、离线回放/评测。

运行：
    python manual_count.py              # GUI
    python manual_count.py --check      # 环境/窗口/色卡检查（不开窗口）
    python manual_count.py --replay DIR # 对目录里保存的面板截图做离线识别
    python manual_count.py --eval [DIR] # 用 output/samples 的目录名做准确率回归

依赖：pip install -r requirements.txt
"""
import argparse
import ctypes
import csv
import glob
import hashlib
import json
import os
import queue
import sys
import time

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except Exception as exc:  # pragma: no cover
    print("缺少 tkinter（Python 安装时需勾选 Tcl/Tk）：", exc)
    raise SystemExit(1)

try:
    import cv2
    import numpy as np
except Exception as exc:  # pragma: no cover
    print("缺少 numpy/opencv-python：", exc)
    print("请先执行：pip install -r requirements.txt")
    raise SystemExit(1)

from xc_cards import (CardLib, card_fields, combo_label, imread_any,
                      imwrite_any, render_stat_card)
from xc_icons import IconLib
from xc_session import SessionStore


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(BASE_DIR, "manual_cfg.json")
LEGACY_RESULT = os.path.join(BASE_DIR, "manual_result.csv")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
RESULT_FILE = os.path.join(OUTPUT_DIR, "manual_result.csv")
JSON_FILE = os.path.join(OUTPUT_DIR, "combo_counts.json")
SESSION_FILE = os.path.join(OUTPUT_DIR, "session.jsonl")
SAMPLE_DIR = os.path.join(OUTPUT_DIR, "samples")
LAST_PANEL = os.path.join(OUTPUT_DIR, "last_panel.png")
STAT_CARD = os.path.join(OUTPUT_DIR, "stat_card.png")
LOG_FILE = os.path.join(OUTPUT_DIR, "app.log")

DEFAULT_CFG = {
    "hwnd": None,
    "window_title": "",
    "card_dir": "",
    "panel_roi": None,          # {ox,oy,w,h}，相对游戏窗口客户区
    "list_region": None,        # 精灵列表网格区
    "star_point": None,         # 炫彩星中心（相对客户区，图标库未命中时的兜底）
    "detail_roi": None,         # 详情页精灵区（可选，用于判断切换）
    "icon_roi": None,           # 详情页右侧图标区（可选；默认取右半屏）
    "ref_w": 1920,              # 上次校准时的客户区宽度（换窗口大小自动按比例缩放）
    "ref_h": 1080,              # 上次校准时的客户区高度
    "icon_dir": "",             # 炫彩星图标模板目录
    "icon_hotkey": "f7",        # 学习当前炫彩星
    "icon_threshold": 0.78,     # 图标模板匹配阈值
    "icon_min_x_ratio": 0.80,   # 图标必须位于详情区右侧 80% 之外，排除左侧徽章
    "use_icon_search": False,   # 默认用固定星位（更快）；星位会漂移时再打开
    "cols": 5,
    "rows": 4,
    "hotkey": "f8",
    "skip_hotkey": "f9",
    "min_similarity": 0.55,     # 全图匹配的最低接受分
    "min_margin": 0.004,        # top1-top2 最小差值（动画面板差值会变小）
    "color_tol": 8000,          # 颜色兜底的距离阈值
    "open_similarity": 0.7,     # 面板区相关度 >= 此值才认为色卡已打开
    "open_margin": 0.004,       # 自动记录时 top1-top2 的最小差值
    "change_diff": 12,          # 头像格/详情区灰度平均差 > 此值认为已切换精灵
    "poll_ms": 60,              # 状态识别轮询间隔
    "capture_dedupe_sec": 0.8,  # 同一面板短时间重复 F8 的去重窗口
    "save_samples": True,       # 把每次面板截图存到 output/samples/<组合>/
}


# ---------------- 配置 ----------------
def load_cfg():
    """读取 manual_cfg.json，缺省字段用 DEFAULT_CFG 补齐。返回 (cfg, error)。"""
    data, error = {}, None
    if os.path.exists(CFG_FILE):
        try:
            with open(CFG_FILE, encoding="utf-8") as fp:
                data = json.load(fp)
        except Exception as exc:
            error = str(exc)
            data = {}
    if not isinstance(data, dict):
        error = "配置根节点不是 JSON 对象"
        data = {}
    cfg = {**DEFAULT_CFG, **data}

    card_dir = str(cfg.get("card_dir") or "")
    if card_dir and not os.path.isabs(card_dir):
        card_dir = os.path.join(BASE_DIR, card_dir)
    if not card_dir or not os.path.isdir(card_dir):
        guess = os.path.join(BASE_DIR, "炫彩")
        if os.path.isdir(guess):
            card_dir = guess
    cfg["card_dir"] = card_dir
    icon_dir = str(cfg.get("icon_dir") or "")
    if icon_dir and not os.path.isabs(icon_dir):
        icon_dir = os.path.join(BASE_DIR, icon_dir)
    if not icon_dir or not os.path.isdir(icon_dir):
        guess = os.path.join(BASE_DIR, "icons")
        if os.path.isdir(guess):
            icon_dir = guess
    cfg["icon_dir"] = icon_dir
    cfg["window_title"] = str(cfg.get("window_title") or "").strip()
    return cfg, error


def save_cfg(cfg):
    with open(CFG_FILE, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


# ---------------- 窗口 ----------------
def find_window(title):
    """按标题精确/前缀/包含匹配，返回面积最大的可见窗口句柄。"""
    try:
        import win32gui
    except Exception:
        return None
    want = str(title or "").strip().casefold()
    if not want:
        return None
    matches = []

    def callback(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            text = win32gui.GetWindowText(hwnd).strip()
            if not text:
                return True
            folded = text.casefold()
            score = 3 if folded == want else 2 if folded.startswith(want) else 1 if want in folded else 0
            if score:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                matches.append((score, (right - left) * (bottom - top), hwnd))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(callback, None)
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], -item[1]))
    return matches[0][2]


def get_client_rect(cfg):
    """定位游戏客户区；句柄失效/标题变化时按标题回退，找不到返回 None。"""
    try:
        import win32gui
    except Exception:
        return None

    want = str(cfg.get("window_title") or "").strip()
    hwnd = None
    raw = cfg.get("hwnd")
    if raw:
        try:
            candidate = int(raw)
            if win32gui.IsWindow(candidate) and win32gui.IsWindowVisible(candidate):
                actual = win32gui.GetWindowText(candidate).strip()
                if not want or not actual or want in actual:
                    hwnd = candidate
        except Exception:
            hwnd = None
    if not hwnd and want:
        hwnd = find_window(want)
    if not hwnd:
        return None

    try:
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        screen_x, screen_y = win32gui.ClientToScreen(hwnd, (0, 0))
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        return None

    width, height = right - left, bottom - top
    if width < 200 or height < 200:
        return None
    return {"left": int(screen_x), "top": int(screen_y), "w": int(width),
            "h": int(height), "hwnd": int(hwnd), "title": title}


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def process_elevated(pid):
    """返回目标进程是否管理员；无法查询返回 None。"""
    if not pid:
        return None
    try:
        import ctypes.wintypes as wt

        class TOKEN_ELEVATION(ctypes.Structure):
            _fields_ = [("TokenIsElevated", wt.DWORD)]

        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            token = wt.HANDLE()
            if not advapi32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):
                return None
            try:
                info = TOKEN_ELEVATION()
                size = wt.DWORD()
                if not advapi32.GetTokenInformation(
                        token, 20, ctypes.byref(info), ctypes.sizeof(info),
                        ctypes.byref(size)):
                    return None
                return bool(info.TokenIsElevated)
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def window_pid(hwnd):
    try:
        import win32process
        return int(win32process.GetWindowThreadProcessId(int(hwnd))[1])
    except Exception:
        return None


class ScreenGrabber:
    """复用一个 mss 实例，避免每帧创建上下文。"""

    def __init__(self):
        self._sct = None

    def _ensure(self):
        if self._sct is None:
            import mss
            self._sct = mss.mss()
        return self._sct

    def grab(self, left, top, width, height):
        width = max(1, int(width))
        height = max(1, int(height))
        try:
            shot = self._ensure().grab({"left": int(left), "top": int(top),
                                        "width": width, "height": height})
            return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
        except Exception:
            self.close()
            return None

    def close(self):
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass
            self._sct = None


def grab_region(left, top, width, height, grabber=None):
    own = grabber is None
    g = grabber or ScreenGrabber()
    try:
        return g.grab(left, top, width, height)
    finally:
        if own:
            g.close()


def print_window(cfg):
    """PrintWindow 抓窗口自身画面（被遮挡也能抓），返回客户区 BGR。"""
    hwnd = cfg.get("hwnd")
    if not hwnd:
        return None
    try:
        import win32gui
        import win32ui
    except Exception:
        return None

    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bmp = None
    try:
        window_left, window_top, window_right, window_bottom = win32gui.GetWindowRect(int(hwnd))
        window_w = max(1, window_right - window_left)
        window_h = max(1, window_bottom - window_top)
        hwnd_dc = win32gui.GetWindowDC(int(hwnd))
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, window_w, window_h)
        save_dc.SelectObject(bmp)
        ctypes.windll.user32.PrintWindow(int(hwnd), save_dc.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        arr = np.frombuffer(bmp.GetBitmapBits(True), dtype=np.uint8).reshape(
            (info["bmHeight"], info["bmWidth"], 4))
        off_x = max(0, int(cfg["left"]) - window_left)
        off_y = max(0, int(cfg["top"]) - window_top)
        arr = arr[off_y:off_y + int(cfg["h"]), off_x:off_x + int(cfg["w"])]
        if arr.size == 0:
            return None
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
    except Exception:
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            if hwnd_dc is not None:
                win32gui.ReleaseDC(int(hwnd), hwnd_dc)
        except Exception:
            pass


def grab_window(cfg, grabber=None):
    """优先 mss 区域抓屏；全黑则回退 PrintWindow。"""
    img = grab_region(cfg["left"], cfg["top"], cfg["w"], cfg["h"], grabber)
    if img is not None and float(img.mean()) >= 10:
        return img
    return print_window(cfg)


# ---------------- 几何 / 指纹 ----------------
def cell_rects(cfg, scale=None):
    region = cfg.get("list_region")
    if not region:
        return []
    try:
        cols = max(1, int(cfg.get("cols", 5)))
        rows = max(1, int(cfg.get("rows", 4)))
    except (TypeError, ValueError):
        cols, rows = 5, 4
    if scale:
        sx, sy = scale
        region = {"ox": int(region["ox"] * sx), "oy": int(region["oy"] * sy),
                  "w": max(1, int(region["w"] * sx)),
                  "h": max(1, int(region["h"] * sy))}
    cell_w, cell_h = region["w"] / cols, region["h"] / rows
    return [{"ox": int(region["ox"] + c * cell_w), "oy": int(region["oy"] + r * cell_h),
             "w": int(cell_w), "h": int(cell_h)}
            for r in range(rows) for c in range(cols)]


def cell_centers(cfg, scale=None):
    return [(r["ox"] + r["w"] / 2, r["oy"] + r["h"] / 2)
            for r in cell_rects(cfg, scale)]


def crop_roi(img, roi):
    if img is None or not roi:
        return None
    height, width = img.shape[:2]
    x = max(0, int(roi["ox"]))
    y = max(0, int(roi["oy"]))
    x2 = min(width, x + int(roi["w"]))
    y2 = min(height, y + int(roi["h"]))
    out = img[y:y2, x:x2]
    return out if out.size else None


def fingerprint(img):
    if img is None or img.size == 0:
        return None
    return cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (64, 64)).astype(np.float32)


def fp_diff(a, b):
    return float(np.abs(a - b).mean())


# ---------------- 界面 ----------------
class App:
    STATE_TEXT = {"IDLE": "未开始", "AVATAR": "等你点头像",
                  "STAR": "等你点炫彩星", "CLOSE": "等你关闭色卡",
                  "PAGE_END": "本页完成"}

    def __init__(self, root):
        self.root = root
        root.title("炫彩统计 · 手动识别（按 F8）")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        os.makedirs(SAMPLE_DIR, exist_ok=True)

        self.cfg, self.cfg_error = load_cfg()
        self.lib = CardLib(min_similarity=float(self.cfg.get("min_similarity", 0.55)),
                           color_tol=float(self.cfg.get("color_tol", 8000)),
                           min_margin=float(self.cfg.get("min_margin", 0.004)))
        self.icon_lib = IconLib(threshold=float(self.cfg.get("icon_threshold", 0.78)))
        self.icon_dir = self.cfg.get("icon_dir") or os.path.join(BASE_DIR, "icons")
        self.current_icon = None
        self.session = SessionStore(SESSION_FILE)
        self.grabber = ScreenGrabber()
        self.queue = queue.Queue()
        self._hotkeys = []
        self._last_action = {}
        self._vk_prev = {}
        self._lbutton_prev = False
        self._avatar_click_pending = False
        self._avatar_click_at = 0.0
        self._star_since = time.time()
        self._star_warned = False

        self.auto_move = tk.BooleanVar(value=False)
        self.only_owned = tk.BooleanVar(value=False)
        self.start_var = tk.StringVar(value="1")
        self.card_dir = self.cfg.get("card_dir") or ""

        self.cell_index = 0
        self.cell_done = set()
        self.state = "IDLE"
        self.ref_fp = None
        self.prev_fp = None
        self.open_name = None
        self.closed_polls = 0
        self.panel_recorded = False
        self._black_polls = 0
        self._win_warned = False
        self._client_cache = None
        self._client_ts = 0.0
        self._last_capture = None
        self._library_labels = set()
        self.page_ref = None
        self.page_prev = None

        self._build_window_section()
        self._build_calibrate_section()
        self._build_card_section()
        self._build_icon_section()
        self._build_run_section()
        self._build_log()
        self._draw_grid()
        self._update_total()

        client = get_client_rect(self.cfg)
        if client:
            self._apply_client(client)
        if self.card_dir and os.path.isdir(self.card_dir):
            self._load_cards(self.card_dir, silent=True)
        os.makedirs(self.icon_dir, exist_ok=True)
        self._load_icons(self.icon_dir, silent=True)

        self._load_session()
        self._install_hotkeys()
        if self.cfg_error:
            self.log_insert(f"[配置] 读取 {CFG_FILE} 失败，已用默认值：{self.cfg_error}\n")

        self._poll_queue()
        self._poll_hotkeys()
        self._poll_state()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- ① 窗口 ----
    def _build_window_section(self):
        f = tk.LabelFrame(self.root, text="① 选择游戏窗口", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="鼠标指向游戏窗口, 3秒后锁定(推荐)",
                  command=self._pick_by_cursor).pack(side="left", padx=4)
        tk.Button(f, text="刷新窗口列表", command=self._refresh_windows).pack(side="left", padx=4)
        tk.Button(f, text="以管理员身份重启",
                  command=self._restart_as_admin).pack(side="left", padx=4)
        self.win_info = tk.Label(f, text="未锁定", fg="#888")
        self.win_info.pack(side="left", padx=8)

    def _set_win_info(self, client):
        if not client:
            return
        self.win_info.config(text=f"已锁定: {client['title']}  客户区 {client['w']}x{client['h']}",
                             fg="#0a0")

    def _apply_client(self, client):
        self.cfg["hwnd"] = client["hwnd"]
        if not self.cfg.get("window_title"):
            self.cfg["window_title"] = str(client["title"]).strip()
        self._ensure_client_scale(client)
        self._client_cache = client
        self._client_ts = time.time()
        self._win_warned = False
        self._save()
        self._set_win_info(client)
        self._check_permissions(client)

    def _check_permissions(self, client):
        if not client:
            return
        pid = window_pid(client["hwnd"])
        game_elevated = process_elevated(pid) if pid else None
        admin = is_admin()
        if game_elevated and not admin:
            self.log_insert(
                "[权限] 游戏以管理员运行，本工具不是；Windows 会挡住 F7/F8/F9。\n"
                "[权限] 请点「以管理员身份重启」，或右键 run_admin.bat / run.bat "
                "选择“以管理员身份运行”。\n")
            self.win_info.config(
                text="游戏是管理员权限：本工具需以管理员运行，否则热键收不到",
                fg="#c00")
        elif admin:
            self.log_insert("[权限] 本工具已是管理员权限，热键/输入可覆盖游戏窗口\n")

    def _restart_as_admin(self):
        try:
            args = [os.path.abspath(__file__)] + sys.argv[1:]
            params = " ".join(f'"{a}"' for a in args)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, BASE_DIR, 1)
            if int(rc) <= 32:
                raise OSError(f"ShellExecuteW returned {rc}")
            self._on_close()
        except Exception as exc:
            messagebox.showerror(
                "提权失败",
                f"请右键 run_admin.bat 选择“以管理员身份运行”。\n{exc}")

    def _pick_by_cursor(self):
        self.win_info.config(text="3 秒后锁定鼠标所在窗口，请把鼠标移到游戏窗口上...", fg="#c80")
        self.root.after(3000, self._lock_window_at_cursor)

    def _lock_window_at_cursor(self):
        hwnd, title = None, ""
        try:
            import win32gui
            x, y = win32gui.GetCursorPos()
            hwnd = win32gui.WindowFromPoint((x, y))
            hwnd = win32gui.GetAncestor(hwnd, 2) if hwnd else None
            if hwnd:
                title = win32gui.GetWindowText(hwnd).strip()
        except Exception:
            hwnd = None
        if not hwnd:
            self.win_info.config(text="没取到窗口，重试一次", fg="#c00")
            return
        self.cfg["hwnd"] = hwnd
        self.cfg["window_title"] = title
        self._save()
        client = get_client_rect(self.cfg)
        if client:
            self._apply_client(client)
        else:
            self.win_info.config(text="窗口已锁定，但取客户区失败", fg="#c00")

    def _refresh_windows(self):
        try:
            import win32gui
        except Exception as exc:
            messagebox.showerror("缺少 pywin32", str(exc))
            return
        names = []

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                text = win32gui.GetWindowText(hwnd)
                if text:
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    names.append(f"{text.strip()}  [{right - left}x{bottom - top}]")
            return True

        win32gui.EnumWindows(callback, None)
        names = sorted(set(names))
        dialog = tk.Toplevel(self.root)
        dialog.title("选择窗口")
        listbox = tk.Listbox(dialog, width=80, height=15)
        listbox.pack(padx=8, pady=8)
        for name in names:
            listbox.insert("end", name)

        def pick():
            selection = listbox.curselection()
            if selection:
                title = names[selection[0]].rsplit("  [", 1)[0].strip()
                self.cfg["window_title"] = title
                self.cfg["hwnd"] = None
                self._save()
                client = get_client_rect(self.cfg)
                if client:
                    self._apply_client(client)
                else:
                    self.win_info.config(text="没找到该窗口的客户区", fg="#c00")
            dialog.destroy()

        tk.Button(dialog, text="确定", command=pick).pack(pady=4)

    def _get_client(self, force=False):
        now = time.time()
        if not force and self._client_cache is not None and now - self._client_ts < 0.5:
            return self._client_cache
        client = get_client_rect(self.cfg)
        if client:
            if self.cfg.get("hwnd") != client["hwnd"]:
                self.cfg["hwnd"] = client["hwnd"]
                if not self.cfg.get("window_title"):
                    self.cfg["window_title"] = str(client["title"]).strip()
                self._save()
            self._ensure_client_scale(client)
            self._client_cache = client
            self._client_ts = now
            if self._win_warned:
                self.log_insert("[窗口] 已重新找到游戏窗口\n")
                self._win_warned = False
            return client

        self._client_cache = None
        if self.auto_move.get():
            self._stop_auto()
            self.log_insert("[自动流程] 找不到游戏窗口，已暂停\n")
        elif not self._win_warned:
            self._win_warned = True
            self.log_insert("[窗口] 未找到游戏窗口，请先锁定\n")
        return None

    def _ensure_client_scale(self, client):
        """窗口尺寸变化时，把上次校准的 ROI/星位按比例换算到当前客户区。"""
        try:
            ref_w = float(self.cfg.get("ref_w") or 0)
            ref_h = float(self.cfg.get("ref_h") or 0)
        except (TypeError, ValueError):
            ref_w = ref_h = 0.0
        if ref_w <= 0 or ref_h <= 0:
            self.cfg["ref_w"] = client["w"]
            self.cfg["ref_h"] = client["h"]
            return
        if abs(client["w"] - ref_w) <= 2 and abs(client["h"] - ref_h) <= 2:
            return
        sx, sy = client["w"] / ref_w, client["h"] / ref_h

        def scale_roi(roi):
            if not roi:
                return roi
            return {"ox": int(roi["ox"] * sx), "oy": int(roi["oy"] * sy),
                    "w": max(1, int(roi["w"] * sx)), "h": max(1, int(roi["h"] * sy))}

        for key in ("panel_roi", "list_region", "detail_roi", "icon_roi"):
            if self.cfg.get(key):
                self.cfg[key] = scale_roi(self.cfg[key])
        point = self.cfg.get("star_point")
        if point:
            self.cfg["star_point"] = {"ox": int(point["ox"] * sx),
                                      "oy": int(point["oy"] * sy)}
        old_w, old_h = int(ref_w), int(ref_h)
        self.cfg["ref_w"] = client["w"]
        self.cfg["ref_h"] = client["h"]
        self._save()
        self.log_insert(
            f"[校准] 窗口 {old_w}x{old_h} -> {client['w']}x{client['h']}，"
            f"ROI 已按比例缩放；如位置不准请重新校准\n")
        self._refresh_pos_info()
    def _scale_factors(self, client):
        try:
            ref_w = float(self.cfg.get("ref_w") or client["w"])
            ref_h = float(self.cfg.get("ref_h") or client["h"])
        except (TypeError, ValueError):
            ref_w, ref_h = client["w"], client["h"]
        return (client["w"] / ref_w if ref_w else 1.0,
                client["h"] / ref_h if ref_h else 1.0)

    def _scaled_roi(self, roi, client):
        if not roi:
            return None
        sx, sy = self._scale_factors(client)
        return {"ox": int(roi["ox"] * sx), "oy": int(roi["oy"] * sy),
                "w": max(1, int(roi["w"] * sx)), "h": max(1, int(roi["h"] * sy))}

    def _scaled_point(self, point, client):
        if not point:
            return None
        sx, sy = self._scale_factors(client)
        return {"ox": int(point["ox"] * sx), "oy": int(point["oy"] * sy)}

    def _cells(self, client):
        return cell_rects(self.cfg, self._scale_factors(client))

    # ---- ② 校准 ----
    def _build_calibrate_section(self):
        f = tk.LabelFrame(self.root, text="② 识别位置（已从 manual_cfg.json 读取）",
                          padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="重新校准(可选)",
                  command=self._open_calibrate_dialog).pack(side="left", padx=4)
        self.pos_info = tk.Label(f, text=self._pos_summary(), fg="#888")
        self.pos_info.pack(side="left", padx=8)

    def _pos_summary(self):
        cfg = self.cfg
        panel = "✓" if cfg.get("panel_roi") else "✗"
        list_ok = "✓" if cfg.get("list_region") else "✗"
        star = "✓" if cfg.get("star_point") else "✗"
        detail = "✓" if cfg.get("detail_roi") else "-"
        return (f"面板{panel}  列表{list_ok}  炫彩星{star}  详情区{detail}  "
                f"网格{cfg.get('cols', '?')}x{cfg.get('rows', '?')}")

    def _refresh_pos_info(self):
        if hasattr(self, "pos_info"):
            self.pos_info.config(text=self._pos_summary())

    def _open_calibrate_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("校准位置(可选)")
        f = tk.Frame(dialog, padx=10, pady=8)
        f.pack()
        tk.Button(f, text="框选炫彩面板区域",
                  command=self._calibrate_panel).pack(pady=2, fill="x")
        tk.Button(f, text="框选精灵列表网格区(自动移动用)",
                  command=self._calibrate_list).pack(pady=2, fill="x")
        tk.Label(f, text="网格列(可改)").pack()
        self.cols_var = tk.StringVar(value=str(self.cfg.get("cols", 5)))
        tk.Entry(f, textvariable=self.cols_var, width=8).pack()
        tk.Label(f, text="网格行(可改)").pack()
        self.rows_var = tk.StringVar(value=str(self.cfg.get("rows", 4)))
        tk.Entry(f, textvariable=self.rows_var, width=8).pack()
        tk.Button(f, text="框选详情页炫彩星位置(自动移动用)",
                  command=self._calibrate_star).pack(pady=2, fill="x")
        tk.Button(f, text="框选详情页精灵名字/立绘区(可选, 提高切换识别)",
                  command=self._calibrate_detail).pack(pady=2, fill="x")
        tk.Button(f, text="保存", command=self._calibrate_save).pack(pady=8)

    def _calibrate_save(self):
        try:
            self.cfg["cols"] = max(1, int(self.cols_var.get()))
            self.cfg["rows"] = max(1, int(self.rows_var.get()))
        except (TypeError, ValueError):
            messagebox.showwarning("提示", "网格列/行必须是整数")
            return
        self._save()
        self._refresh_pos_info()
        self._draw_grid()

    def _calibrate_panel(self):
        client = self._get_client(force=True)
        if not client:
            messagebox.showwarning("提示", "先在①锁定游戏窗口")
            return
        result = self._select_box(client)
        if not result:
            return
        self.cfg["panel_roi"] = result["roi"]
        self._after_calibrate("炫彩面板")

    def _calibrate_list(self):
        client = self._get_client(force=True)
        if not client:
            messagebox.showwarning("提示", "先在①锁定游戏窗口")
            return
        result = self._select_box(client)
        if not result:
            return
        self.cfg["list_region"] = result["roi"]
        self._after_calibrate("列表网格区")

    def _calibrate_star(self):
        client = self._get_client(force=True)
        if not client:
            messagebox.showwarning("提示", "先在①锁定游戏窗口")
            return
        result = self._select_box(client)
        if not result:
            return
        roi = result["roi"]
        self.cfg["star_point"] = {"ox": roi["ox"] + roi["w"] // 2,
                                  "oy": roi["oy"] + roi["h"] // 2}
        self._after_calibrate("炫彩星")

    def _calibrate_detail(self):
        client = self._get_client(force=True)
        if not client:
            messagebox.showwarning("提示", "先在①锁定游戏窗口")
            return
        result = self._select_box(client)
        if not result:
            return
        self.cfg["detail_roi"] = result["roi"]
        self._after_calibrate("详情区")

    def _after_calibrate(self, name):
        self._save()
        self._refresh_pos_info()
        self._draw_grid()
        self.log_insert(f"[校准] 已更新{name}\n")

    def _select_box(self, client):
        """在游戏窗口截图上拖拽框选，返回窗口相对 roi + 裁剪图。"""
        from PIL import Image, ImageTk

        bgr = grab_window(client, self.grabber)
        if bgr is None:
            messagebox.showwarning("提示", "截不到游戏窗口画面，请确认窗口可见且未最小化")
            return None
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        top = tk.Toplevel(self.root)
        top.attributes("-topmost", True)
        top.title("拖拽框选，松开确认；不选直接关窗口")
        scale = min(self.root.winfo_screenwidth() / width,
                    self.root.winfo_screenheight() / height) * 0.98
        scale = max(0.1, scale)
        dw, dh = max(1, int(width * scale)), max(1, int(height * scale))
        canvas = tk.Canvas(top, width=dw, height=dh, cursor="cross", bg="#222")
        canvas.pack()
        photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((dw, dh)))
        canvas.create_image(0, 0, anchor="nw", image=photo)
        top._photo = photo
        rect = {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
        pressed = {"p": False}
        result = {}

        def on_press(event):
            pressed["p"] = True
            rect.update(x0=event.x, y0=event.y, x1=event.x, y1=event.y)

        def on_drag(event):
            if pressed["p"]:
                rect.update(x1=event.x, y1=event.y)
                canvas.delete("r")
                canvas.create_rectangle(rect["x0"], rect["y0"], rect["x1"], rect["y1"],
                                        outline="red", width=2, tags="r")

        def on_release(event):
            pressed["p"] = False
            x0, y0 = min(rect["x0"], rect["x1"]), min(rect["y0"], rect["y1"])
            x1, y1 = max(rect["x0"], rect["x1"]), max(rect["y0"], rect["y1"])
            if x1 - x0 < 5 or y1 - y0 < 5:
                messagebox.showwarning("提示", "框太小了，请重新拖一个框")
                return
            roi = {"ox": int(x0 / scale), "oy": int(y0 / scale),
                   "w": int((x1 - x0) / scale), "h": int((y1 - y0) / scale)}
            result["roi"] = roi
            result["img"] = bgr[roi["oy"]:roi["oy"] + roi["h"],
                                roi["ox"]:roi["ox"] + roi["w"]]
            top.destroy()

        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        self.root.wait_window(top)
        return result or None

    # ---- ③ 色卡 ----
    def _build_card_section(self):
        f = tk.LabelFrame(self.root, text="③ 色卡库（普通/黑白/赛季都放一个文件夹）",
                          padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="选择色卡文件夹", command=self._choose_cards).pack(side="left", padx=4)
        tk.Button(f, text="重新加载",
                  command=lambda: self._load_cards(self.card_dir)).pack(side="left", padx=4)
        self.card_info = tk.Label(f, text="未加载", fg="#888")
        self.card_info.pack(side="left", padx=8)

    def _choose_cards(self):
        folder = filedialog.askdirectory(title="选择炫彩色卡文件夹")
        if folder:
            self._load_cards(folder)

    def _load_cards(self, folder, silent=False):
        folder = str(folder or "").strip()
        if not folder or not os.path.isdir(folder):
            msg = f"色卡目录不存在：{folder or '(空)'}"
            self.card_info.config(text="未加载", fg="#c00")
            if silent:
                self.log_insert(f"[色卡] {msg}\n")
            else:
                messagebox.showwarning("提示", msg)
            return

        try:
            stats = self.lib.load(folder)
        except Exception as exc:
            self.log_insert(f"[色卡] 加载失败：{exc}\n")
            if not silent:
                messagebox.showerror("加载失败", str(exc))
            return

        loaded = stats["loaded"]
        invalid = stats["invalid"]
        self.card_dir = folder
        self.cfg["card_dir"] = folder
        self._save()
        self._library_labels = {c["label"] for c in self.lib.cards}
        text = f"已加载 {loaded} 张"
        if stats["skipped"]:
            text += f" / 跳过 {len(stats['skipped'])}"
        if invalid:
            text += f" / 命名异常 {len(invalid)}"
        self.card_info.config(text=text, fg="#0a0" if loaded and not invalid else "#c80" if loaded else "#c00")
        self.log_insert(
            f"[色卡] {folder}: 加载 {loaded}, 跳过 {len(stats['skipped'])}, "
            f"命名异常 {len(invalid)}, 标签重复 {len(stats['duplicates'])}\n")
        for name in invalid[:8]:
            self.log_insert(f"  命名异常: {name}\n")
        if len(invalid) > 8:
            self.log_insert(f"  ... 还有 {len(invalid) - 8} 个命名异常\n")
        if not loaded and not silent:
            messagebox.showwarning("提示", "没有加载到有效色卡，请检查文件名格式：\n"
                                           "序号_类型_颜色组合_粒子.webp")
        self._update_total()

    # ---- ⑤ 炫彩星图标 ----
    def _build_icon_section(self):
        f = tk.LabelFrame(self.root, text="⑤ 炫彩星图标（悬停最右侧炫彩星，按 F7 学习）",
                          padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="选择图标文件夹", command=self._choose_icon_dir).pack(side="left", padx=4)
        tk.Button(f, text="重新加载",
                  command=lambda: self._load_icons(self.icon_dir)).pack(side="left", padx=4)
        tk.Button(f, text="学习当前炫彩星 (F7)",
                  command=self._learn_icon_delayed).pack(side="left", padx=4)
        self.icon_info = tk.Label(f, text="未加载（可先用 F7 学习）", fg="#888")
        self.icon_info.pack(side="left", padx=8)

    def _choose_icon_dir(self):
        folder = filedialog.askdirectory(title="选择炫彩星图标模板文件夹")
        if folder:
            self._load_icons(folder)

    def _load_icons(self, folder, silent=False):
        folder = str(folder or "").strip()
        if not folder or not os.path.isdir(folder):
            self.icon_info.config(text="未加载", fg="#c80")
            if not silent:
                messagebox.showwarning("提示", f"图标目录不存在：{folder or '(空)'}")
            return
        try:
            count = self.icon_lib.load(folder)
        except Exception as exc:
            self.log_insert(f"[图标] 加载失败：{exc}\n")
            if not silent:
                messagebox.showerror("加载失败", str(exc))
            return
        self.icon_dir = folder
        self.cfg["icon_dir"] = folder
        self._save()
        self.icon_info.config(text=f"已加载 {count} 个炫彩星模板",
                              fg="#0a0" if count else "#c80")
        self.log_insert(f"[图标] {folder}: 加载 {count} 个模板\n")
        if not count and not silent:
            messagebox.showinfo("提示", "图标目录是空的。\n打开一只炫彩的详情页，"
                                        "鼠标悬停最右侧炫彩星，按 F7 学习。")

    def _learn_icon_delayed(self):
        """按钮入口：3 秒后抓鼠标位置，方便把鼠标移回游戏里的炫彩星。"""
        self.icon_info.config(
            text="3 秒后学习，请把鼠标移到最右侧炫彩星...", fg="#c80")
        self.root.after(3000, self._learn_icon)

    def _learn_icon(self):
        """把鼠标当前位置周围 96×96 的炫彩星存成模板。"""
        client = self._get_client()
        if not client:
            self.log_insert("[图标] 未锁定窗口，无法学习\n")
            return
        try:
            import win32gui
            screen_x, screen_y = win32gui.GetCursorPos()
        except Exception as exc:
            self.log_insert(f"[图标] 取鼠标位置失败：{exc}\n")
            return
        rel_x, rel_y = int(screen_x) - client["left"], int(screen_y) - client["top"]
        if not (0 <= rel_x < client["w"] and 0 <= rel_y < client["h"]):
            self.log_insert("[图标] 鼠标不在游戏窗口内\n")
            return
        img = grab_window(client, self.grabber)
        if img is None:
            self.log_insert("[图标] 截不到游戏窗口\n")
            return
        half = 48
        max_x = max(0, img.shape[1] - 2 * half - 1)
        max_y = max(0, img.shape[0] - 2 * half - 1)
        x0 = max(0, min(max_x, rel_x - half))
        y0 = max(0, min(max_y, rel_y - half))
        crop = img[y0:y0 + 2 * half, x0:x0 + 2 * half]
        if crop is None or crop.size == 0:
            self.log_insert("[图标] 截取区域为空\n")
            return
        os.makedirs(self.icon_dir, exist_ok=True)
        name = "learned_" + time.strftime("%Y%m%d_%H%M%S") + f"_{time.time_ns() % 1000:03d}"
        path = os.path.join(self.icon_dir, name + ".png")
        if not imwrite_any(path, crop):
            self.log_insert("[图标] 保存模板失败\n")
            return
        count = self.icon_lib.load(self.icon_dir)
        self.icon_info.config(text=f"已加载 {count} 个炫彩星模板", fg="#0a0")
        self.log_insert(f"[图标] 已学习 {name}.png（共 {count} 个模板）\n")
        messagebox.showinfo("完成", "已学习当前炫彩星模板。\n换一个赛季/异色时再按一次 F7。")

    def _grab_detail(self, client):
        """截取详情页右侧（或配置的 icon_roi），返回 (img, ox, oy)。"""
        roi = self._scaled_roi(self.cfg.get("icon_roi"), client)
        if not roi:
            roi = {"ox": int(client["w"] * 0.52), "oy": int(client["h"] * 0.20),
                   "w": max(1, int(client["w"] * 0.48)),
                   "h": max(1, int(client["h"] * 0.72))}
        img = self._grab_roi(client, roi)
        if img is None:
            full = grab_window(client, self.grabber)
            if full is None:
                return None, 0, 0
            img = crop_roi(full, roi)
        if img is None:
            return None, 0, 0
        return img, int(roi["ox"]), int(roi["oy"])

    # ---- ④ 识别 ----
    def _build_run_section(self):
        f = tk.LabelFrame(self.root, text="④ 识别与鼠标辅助", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)

        top = tk.Frame(f)
        top.pack(fill="x")
        tk.Button(top, text="识别当前面板 (F8)",
                  command=self._on_capture).pack(side="left", padx=3)
        tk.Button(top, text="撤销上一条", command=self._undo).pack(side="left", padx=3)
        tk.Button(top, text="清空统计", command=self._clear).pack(side="left", padx=3)
        tk.Button(top, text="导出数据", command=self._export_json).pack(side="left", padx=3)
        tk.Button(top, text="生成统计色卡",
                  command=self._make_stat_card).pack(side="left", padx=3)
        self.total_info = tk.Label(top, text="已统计 0 只 / 0 种组合", fg="#0a0")
        self.total_info.pack(side="left", padx=8)
        tk.Checkbutton(top, text="统计图只显示已拥有",
                       variable=self.only_owned).pack(side="left", padx=4)

        group = tk.LabelFrame(f, text="自动流程 · 按画面判断下一步（只移动鼠标，点击由你做）",
                              padx=6, pady=4)
        group.pack(fill="x", pady=4)
        row = tk.Frame(group)
        row.pack(fill="x")
        tk.Label(row, text="从第").pack(side="left")
        tk.Entry(row, textvariable=self.start_var, width=4).pack(side="left")
        tk.Label(row, text="格开始").pack(side="left")
        tk.Button(row, text="开始", command=self._start_auto).pack(side="left", padx=4)
        tk.Button(row, text="跳过当前精灵 (F9)",
                  command=self._skip_cell).pack(side="left", padx=4)
        tk.Button(row, text="停止", command=self._stop_auto).pack(side="left", padx=4)
        self.auto_info = tk.Label(row, text="", fg="#888")
        self.auto_info.pack(side="left", padx=6)
        self.state_info = tk.Label(row, text="", fg="#888")
        self.state_info.pack(side="left", padx=6)

        self.grid_canvas = tk.Canvas(group, width=320, height=190, bg="#fafafa",
                                     highlightthickness=1, highlightbackground="#ccc")
        self.grid_canvas.pack(anchor="w", pady=(4, 0))
        self.grid_canvas.bind("<Button-1>", self._on_grid_click)

    def _draw_grid(self):
        if not hasattr(self, "grid_canvas"):
            return
        canvas = self.grid_canvas
        canvas.delete("all")
        try:
            cols = max(1, int(self.cfg.get("cols", 5)))
            rows = max(1, int(self.cfg.get("rows", 4)))
        except (TypeError, ValueError):
            cols, rows = 5, 4
        width, height = 320, 190
        cell_w, cell_h = width / cols, height / rows
        for index in range(cols * rows):
            row, col = divmod(index, cols)
            x0, y0 = col * cell_w + 2, row * cell_h + 2
            x1, y1 = x0 + cell_w - 4, y0 + cell_h - 4
            if index in self.cell_done:
                fill, outline = "#e6e6e6", "#bbb"
            elif index == self.cell_index:
                fill, outline = ("#d9f2d9" if self.auto_move.get() else "#dff0ff"), "#39a"
            else:
                fill, outline = "#ffffff", "#bbb"
            canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline)
            canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=str(index + 1),
                               font=("TkDefaultFont", 8))

    def _on_grid_click(self, event):
        try:
            cols = max(1, int(self.cfg.get("cols", 5)))
            rows = max(1, int(self.cfg.get("rows", 4)))
        except (TypeError, ValueError):
            return
        col, row = int(event.x / (320 / cols)), int(event.y / (190 / rows))
        if 0 <= col < cols and 0 <= row < rows:
            self._start_auto_at(row * cols + col)

    def _start_auto(self):
        try:
            start = max(1, int(self.start_var.get())) - 1
        except (TypeError, ValueError):
            start = 0
        self._start_auto_at(start)

    def _start_auto_at(self, index):
        if not cell_rects(self.cfg):
            messagebox.showwarning("提示", "先②框选「精灵列表网格区」并填 网格列/行")
            return
        if not self.cfg.get("panel_roi"):
            messagebox.showwarning("提示", "先②框选「炫彩面板区域」")
            return
        if not self.lib.cards:
            messagebox.showwarning("提示", "先③加载色卡文件夹（面板是否打开靠色卡比对判断）")
            return
        was_running = self.auto_move.get()
        self.auto_move.set(True)
        if not was_running:
            self.cell_done.clear()
        self._black_polls = 0
        self._move_to_cell(index)

    def _stop_auto(self):
        self.auto_move.set(False)
        self.state = "IDLE"
        self.page_ref = self.page_prev = None
        self._avatar_click_pending = False
        if hasattr(self, "auto_info"):
            self.auto_info.config(text="已停止", fg="#c00")
        if hasattr(self, "state_info"):
            self.state_info.config(text="")
        self._draw_grid()

    def _move_to_cell(self, index):
        client = self._get_client(force=True)
        centers = cell_centers(self.cfg, self._scale_factors(client)) if client else []
        if not centers or not client:
            if hasattr(self, "auto_info"):
                self.auto_info.config(text="未锁定窗口或未校准网格", fg="#c00")
            return
        index = max(0, min(int(index), len(centers) - 1))
        ox, oy = centers[index]
        self._move_cursor(client["left"] + ox, client["top"] + oy)
        self.cell_index = index
        self.state = "AVATAR"
        self.ref_fp = self.prev_fp = self.open_name = None
        self.current_icon = None
        self.panel_recorded = False
        self._avatar_click_pending = False
        self.page_ref = self.page_prev = None
        self._black_polls = 0
        self.auto_info.config(
            text=f"光标已到第 {index + 1}/{len(centers)} 只头像, 请点击", fg="#0a0")
        self._draw_grid()

    @staticmethod
    def _move_cursor(x, y):
        """只移动光标（物理像素），不做任何点击。"""
        try:
            import win32api
            try:
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
            win32api.SetCursorPos((int(x), int(y)))
        except Exception:
            pass

    def _poll_state(self):
        try:
            if self.auto_move.get() and self.state in ("AVATAR", "STAR", "CLOSE", "PAGE_END"):
                self._step_state()
        except Exception as exc:
            self.log_insert(f"[状态识别出错] {exc}\n")
        self.root.after(max(30, int(self.cfg.get("poll_ms", 100))), self._poll_state)

    def _grab_roi(self, client, roi):
        if not client or not roi:
            return None
        ox = max(0, int(roi["ox"]))
        oy = max(0, int(roi["oy"]))
        width = min(int(roi["w"]), client["w"] - ox)
        height = min(int(roi["h"]), client["h"] - oy)
        if width <= 0 or height <= 0:
            return None
        return self.grabber.grab(client["left"] + ox, client["top"] + oy, width, height)

    def _grab_panel(self, client):
        roi = self._scaled_roi(self.cfg.get("panel_roi"), client)
        if not roi:
            return None
        panel = self._grab_roi(client, roi)
        if panel is not None and float(panel.mean()) >= 3:
            return panel
        full = grab_window(client, self.grabber)
        return crop_roi(full, roi) if full is not None else None

    def _grab_state_frame(self):
        client = self._get_client()
        if not client:
            return None
        cells = self._cells(client)
        cell = cells[self.cell_index] if self.cell_index < len(cells) else None
        panel_roi = self._scaled_roi(self.cfg.get("panel_roi"), client)
        detail_roi = self._scaled_roi(self.cfg.get("detail_roi"), client)
        panel = self._grab_roi(client, panel_roi)
        cell_img = self._grab_roi(client, cell)
        detail_img = self._grab_roi(client, detail_roi)
        if panel is None or float(panel.mean()) < 3:
            # mss 偶尔会抓到全黑（窗口刚恢复/被遮挡）；用 PrintWindow 整窗兜底
            full = grab_window(client, self.grabber)
            if full is None:
                return None
            panel = crop_roi(full, panel_roi)
            cell_img = crop_roi(full, cell)
            detail_img = crop_roi(full, detail_roi)
        if panel is None:
            return None
        return {"panel": panel,
                "fp": (fingerprint(cell_img), fingerprint(detail_img))}

    def _step_state(self):
        if self.state == "PAGE_END":
            self._step_page_end()
            return

        # 用户刚点过当前头像格：松开左键后直接跳星，不再等头像/详情动画稳定
        if self.state == "AVATAR" and self._avatar_click_pending:
            if (not self._lbutton_prev
                    and time.time() - self._avatar_click_at >= 0.10):
                self._avatar_click_pending = False
                self.log_insert(f"[点击] 第 {self.cell_index + 1} 格头像，直接跳星\n")
                self._goto_star()
                return

        frame = self._grab_state_frame()
        if frame is None:
            return
        panel = frame["panel"]
        if panel is None:
            return

        # 窗口最小化/被遮挡时 mss 可能全黑；连续 3 帧则暂停，避免盲移光标
        if float(panel.mean()) < 3:
            self._black_polls += 1
            if self._black_polls >= 3 and self.auto_move.get():
                self._stop_auto()
                self.log_insert("[自动流程] 画面全黑/窗口不可见，已暂停\n")
            return
        self._black_polls = 0

        name, score, _second, second_score = self.lib.best_full(panel)
        margin = score - second_score
        open_sim = float(self.cfg.get("open_similarity", 0.7))
        open_margin = float(self.cfg.get("open_margin", 0.004))
        is_open = score >= open_sim
        confident = is_open and (margin >= open_margin or score >= 0.95)
        # 动画面板下 top1/top2 可能每帧轻微变化；分数足够就直接认一帧，
        # 不再要求连续两帧同名，否则会卡在“等你点炫彩星”。
        stable_open = confident and score >= 0.75
        self.open_name = name if confident else None

        fp, prev = frame["fp"], self.prev_fp
        self.prev_fp = fp
        self.state_info.config(
            text=f"状态: {self.STATE_TEXT[self.state]}  相关度 {score:.2f} / 次 {second_score:.2f}  ({name or '-'})")

        if self.state == "STAR" and not self._star_warned:
            if is_open and not confident:
                self.log_insert(
                    f"[识别] 面板已出现但 top1-top2 差值不足：{name} "
                    f"{score:.2f}/{margin:.2f}；可按 F8 手动识别或调低 min_margin\n")
                self._star_warned = True
            elif not is_open and time.time() - self._star_since > 8:
                self.log_insert(
                    "[识别] 等待炫彩星超时：面板未出现。检查是否点到了最右侧炫彩星、"
                    "panel_roi 是否正确；可按 F8 手动识别或 F9 跳过\n")
                self._star_warned = True

        if self.state == "CLOSE":
            # 100 ms 轮询下，2 帧太短，关闭动画可能还没结束；3 帧约 300 ms 更稳
            self.closed_polls = 0 if is_open else self.closed_polls + 1
            if self.closed_polls >= 3:
                self._advance()
            return

        if stable_open:
            # AVATAR 状态下也可能出现：你点得快，两次轮询之间已经点开了色卡
            if self._record_panel(panel, switched=self.state == "STAR"):
                self._enter_close()
            return

        if self.state == "AVATAR":
            threshold = float(self.cfg.get("change_diff", 12))
            settled = prev is not None and all(
                a is None or b is None or fp_diff(a, b) < threshold / 3
                for a, b in zip(fp, prev))
            if self.ref_fp is None:
                if settled:  # 光标悬停效果稳定后才取基准
                    self.ref_fp = fp
                return
            changed = any(a is not None and b is not None and fp_diff(a, b) > threshold
                          for a, b in zip(fp, self.ref_fp))
            if changed and settled:
                self._goto_star()

    def _step_page_end(self):
        client = self._get_client()
        region = self._scaled_roi(self.cfg.get("list_region"), client) if client else None
        if not region or not client:
            return
        frame = self._grab_roi(client, region)
        current = fingerprint(frame)
        if current is None:
            return
        if self.page_ref is None:
            self.page_ref = current
            self.page_prev = current
            return
        diff_ref = fp_diff(current, self.page_ref)
        diff_prev = fp_diff(current, self.page_prev) if self.page_prev is not None else 999.0
        self.page_prev = current
        if (diff_ref > float(self.cfg.get("change_diff", 12)) * 2
                and diff_prev < 2.0):
            self.log_insert("[自动流程] 检测到翻页，回到第 1 格\n")
            self.cell_done.clear()
            self._move_to_cell(0)

    def _goto_star(self):
        self.state = "STAR"
        self._star_since = time.time()
        self._star_warned = False
        self._avatar_click_pending = False
        client = self._get_client()
        star = None
        if (client and self.cfg.get("use_icon_search", False)
                and self.icon_lib.templates):
            detail, offset_x, offset_y = self._grab_detail(client)
            if detail is not None:
                best, _ = self.icon_lib.find(detail)
                min_x = detail.shape[1] * float(self.cfg.get("icon_min_x_ratio", 0.80))
                if best and best["x"] >= min_x:
                    star = {"x": offset_x + best["x"], "y": offset_y + best["y"],
                            "name": best["name"]}
                elif best:
                    self.log_insert(
                        f"[炫彩星] 图标命中位置偏左（{best['x']}/{detail.shape[1]}），"
                        f"可能是左侧徽章，回退固定星位\n")

        if client and star:
            self._move_cursor(client["left"] + star["x"], client["top"] + star["y"])
            self.current_icon = star["name"]
            self.auto_info.config(
                text=f"已定位炫彩星（{star['name']}），请点击；没有炫彩按 F9 跳过",
                fg="#0a0")
        else:
            self.current_icon = None
            if (client and self.cfg.get("use_icon_search", False)
                    and self.icon_lib.templates):
                self.log_insert("[炫彩星] 图标未命中，回退固定星位；可重新按 F7 学习当前类型\n")
            fixed = self._scaled_point(self.cfg.get("star_point"), client) if client else None
            if client and fixed:
                self._move_cursor(client["left"] + fixed["ox"], client["top"] + fixed["oy"])
            self.auto_info.config(
                text=f"第 {self.cell_index + 1} 只已切换, 请点炫彩星(没有炫彩按 F9 跳过)",
                fg="#0a0")
        self._draw_grid()

    def _enter_close(self):
        self.state = "CLOSE"
        self.closed_polls = 0
        self._star_warned = False
        self._avatar_click_pending = False
        self.auto_info.config(text="已记录, 按 ESC 或点空白关闭色卡", fg="#0a0")
        self._draw_grid()

    def _skip_cell(self):
        if not self.auto_move.get():
            self.log_insert("[自动流程未开始] 请先点「开始」\n")
            return
        if self.state == "PAGE_END":
            self.cell_done.clear()
            self._move_to_cell(0)
        else:
            self._advance()

    def _advance(self):
        self.cell_done.add(self.cell_index)
        cells = cell_rects(self.cfg)
        next_index = self.cell_index + 1
        if next_index >= len(cells):
            self.state = "PAGE_END"
            self.page_ref = self.page_prev = None
            self.auto_info.config(
                text="本页完成: 在游戏里翻页后会自动回到第 1 格(也可按 F9)", fg="#c80")
            self.log_insert("[自动流程] 本页已完成\n")
            self._draw_grid()
            return
        self._move_to_cell(next_index)

    # ---- 记录 / 会话 ----
    def _load_session(self):
        stats = self.session.load([RESULT_FILE, LEGACY_RESULT])
        if stats["records"]:
            message = f"[会话] 已恢复 {stats['records']} 条记录 / {stats['types']} 种组合"
            if stats["legacy_imported"]:
                message += f"（其中 {stats['legacy_imported']} 条从旧 CSV 导入）"
            self.log_insert(message + "\n")
            self._save_csv()
        if stats["bad_lines"]:
            self.log_insert(f"[会话] 跳过 {stats['bad_lines']} 行损坏数据\n")
        self._last_capture = None
        self.panel_recorded = False
        self._update_total()

    def _on_capture(self):
        if self.auto_move.get() and self.state == "CLOSE" and self.panel_recorded:
            self.log_insert("[F8] 这张色卡已自动记录, 已忽略; 识别错了请先撤销再按 F8\n")
            return
        client = self._get_client()
        if not client:
            self.log_insert("[错误] 未锁定游戏窗口\n")
            return
        if not self.cfg.get("panel_roi"):
            self.log_insert("[错误] 先做②框选面板区域\n")
            return
        panel = self._grab_panel(client)
        if panel is None:
            self.log_insert("[错误] 截不到窗口/面板区域越界\n")
            return

        now = time.time()
        panel_hash = self._panel_hash(panel)
        dedupe = float(self.cfg.get("capture_dedupe_sec", 0.8))
        if (self._last_capture and panel_hash == self._last_capture[0]
                and now - self._last_capture[1] < dedupe):
            self.log_insert("[F8] 与上次识别相同, 已忽略(防连按; 如需重复计数请稍等或先撤销)\n")
            return

        if self._record_panel(panel) and self.auto_move.get():
            self._enter_close()

    def _record_panel(self, panel, switched=True):
        """识别面板并计数；返回是否成功记录。"""
        if panel is None:
            return False
        try:
            imwrite_any(LAST_PANEL, panel)
        except Exception:
            pass

        name, score, full, margin = self.lib.match(panel)
        if name is None:
            self._save_sample(panel, "_unmatched")
            self.log_insert(f"[未匹配] 相关度={score:.2f} 次优差={margin:.2f}（样本已保存）\n")
            return False

        fields = card_fields(name)
        label = combo_label(name)
        sample = self._save_sample(panel, label)
        try:
            self.session.record(card=name, label=label, score=score,
                                method="全图" if full else "颜色兜底",
                                margin=margin, sample=sample, switched=switched)
        except Exception as exc:
            self.log_insert(f"[错误] 写入 session 失败：{exc}\n")
            return False

        self.panel_recorded = True
        self._last_capture = (self._panel_hash(panel), time.time())
        if fields["type"] == "赛季":
            detail = f"赛季={fields['season']} 粒子={fields['particle']}"
        else:
            detail = (f"颜色一={fields['c1']} 颜色二={fields['c2']} "
                      f"粒子={fields['particle']}")
        warn = "" if switched else "  [未检测到切换精灵, 若重复请撤销]"
        method = "全图" if full else "颜色兜底"
        icon_note = f" 图标={self.current_icon}" if self.current_icon else ""
        self.log_insert(
            f"[{len(self.session.records)}] {label}  ({method} 相关度={score:.2f} "
            f"次优差={margin:.2f})  {detail}{icon_note}{warn}\n")
        self._update_total()
        self._save_csv()
        self._draw_grid()
        return True

    @staticmethod
    def _panel_hash(panel):
        return hashlib.blake2b(panel.tobytes(), digest_size=8).hexdigest()

    def _save_sample(self, panel, label):
        if not self.cfg.get("save_samples", True):
            return ""
        safe = "".join(ch for ch in str(label) if ch not in '\\/:*?"<>|').strip() or "_"
        folder = os.path.join(SAMPLE_DIR, safe)
        try:
            os.makedirs(folder, exist_ok=True)
            filename = f"{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns() % 1_000_000:06d}.png"
            path = os.path.join(folder, filename)
            if not imwrite_any(path, panel):
                return ""
            return os.path.relpath(path, BASE_DIR)
        except Exception:
            return ""

    def _undo(self):
        removed = self.session.undo()
        if not removed:
            self.log_insert("[撤销] 没有可撤销的记录\n")
            return
        # 关键修复：撤销后允许对同一张仍打开的面板重新按 F8
        self.panel_recorded = False
        self._last_capture = None
        self.log_insert(f"[撤销] {removed.get('label', '')}\n")
        self._update_total()
        self._save_csv()
        self._draw_grid()

    def _clear(self):
        if not self.session.records:
            return
        if not messagebox.askyesno("确认清空", "确定清空全部统计吗？\n会写入一条 clear 事件，可继续新统计。"):
            return
        self.session.clear()
        self.panel_recorded = False
        self._last_capture = None
        self.log_insert("[清空] 已清空统计\n")
        self._update_total()
        self._save_csv()
        self._draw_grid()

    def _update_total(self):
        if not hasattr(self, "total_info"):
            return
        total = len(self.session.records)
        types = len(self.session.counts)
        text = f"已统计 {total} 只 / {types} 种组合"
        if self._library_labels:
            seen = sum(1 for label in self.session.counts if label in self._library_labels)
            text += f"  (覆盖 {seen}/{len(self._library_labels)})"
        self.total_info.config(text=text)

    # ---- 导出 ----
    def _save_csv(self):
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(RESULT_FILE, "w", encoding="utf-8-sig", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(["类型", "颜色一", "颜色二", "粒子", "组合", "数量"])
            for label, count in sorted(self.session.counts.items(),
                                       key=lambda item: (-item[1], item[0])):
                card = self.session.card_by_label.get(label, label)
                info = card_fields(card)
                writer.writerow([info["type"], info["c1"], info["c2"],
                                 info["particle"] or "无", label, count])

    def _export_json(self):
        if not self.session.counts:
            messagebox.showinfo("提示", "还没有统计结果")
            return
        rows = []
        for label, count in sorted(self.session.counts.items(),
                                   key=lambda item: (-item[1], item[0])):
            card = self.session.card_by_label.get(label, label)
            info = card_fields(card)
            rows.append({"组合": label, "类型": info["type"],
                         "颜色一": info["c1"], "颜色二": info["c2"],
                         "粒子": info["particle"] or "无", "数量": count})
        library_labels = [c["label"] for c in self.lib.cards]
        missing = sorted(set(library_labels) - set(self.session.counts))
        coverage = (round(len(set(library_labels) & set(self.session.counts))
                          / len(library_labels), 4) if library_labels else None)
        payload = {"total": len(self.session.records), "types": len(self.session.counts),
                   "library_size": len(library_labels), "coverage": coverage,
                   "missing": missing, "data": rows}
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        with open(JSON_FILE, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
        self._save_csv()
        self.log_insert(f"[导出] {JSON_FILE} + {RESULT_FILE}\n")
        messagebox.showinfo("完成", f"已导出：\n{JSON_FILE}\n{RESULT_FILE}")

    def _make_stat_card(self):
        if not self.lib.cards:
            messagebox.showinfo("提示", "先加载色卡文件夹")
            return
        out, count = render_stat_card(self.session.counts, self.lib.cards,
                                      out_path=STAT_CARD,
                                      only_owned=self.only_owned.get())
        if not out:
            messagebox.showinfo("提示", "没有可生成的色卡（只显示已拥有时可能为空）")
            return
        self.log_insert(f"[统计色卡] 已生成 {out}  (共 {count} 格)\n")
        messagebox.showinfo("完成", f"统计色卡已生成：\n{out}\n(共 {count} 格)")

    # ---- 日志 / 生命周期 ----
    def _build_log(self):
        f = tk.LabelFrame(self.root, text="日志 / 统计", padx=8, pady=6)
        f.pack(fill="both", expand=True, padx=8, pady=4)
        scroll = tk.Scrollbar(f)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(f, height=16, wrap="word", yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.config(command=self.log.yview)

    def log_insert(self, message):
        if not hasattr(self, "log"):
            return
        text = str(message)
        if not text.endswith("\n"):
            text += "\n"
        line = time.strftime("[%H:%M:%S] ") + text
        self.log.insert("end", line)
        self.log.see("end")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fp:
                fp.write(time.strftime("%Y-%m-%d ") + line)
        except Exception:
            pass

    def _dispatch(self, action):
        now = time.time()
        if now - self._last_action.get(action, 0.0) < 0.35:
            return
        self._last_action[action] = now
        self.queue.put(action)

    def _poll_queue(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if item == "capture":
                    self.log_insert("[按键] F8\n")
                    self._on_capture()
                elif item == "skip":
                    self.log_insert("[按键] F9\n")
                    self._skip_cell()
                elif item == "learn_icon":
                    self.log_insert("[按键] F7\n")
                    self._learn_icon()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _poll_hotkeys(self):
        """GetAsyncKeyState 全局轮询：游戏窗口聚焦时也能收到 F7/F8/F9。"""
        vk_codes = {f"f{i}": 0x6F + i for i in range(1, 13)}
        mapping = (("capture", self.cfg.get("hotkey", "f8")),
                   ("skip", self.cfg.get("skip_hotkey", "f9")),
                   ("learn_icon", self.cfg.get("icon_hotkey", "f7")))
        user32 = None
        try:
            user32 = ctypes.windll.user32
        except Exception:
            pass
        if user32 is not None:
            for action, key in mapping:
                vk = vk_codes.get(str(key).lower())
                if not vk:
                    continue
                try:
                    down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                except Exception:
                    down = False
                if down and not self._vk_prev.get(action, False):
                    self._dispatch(action)
                self._vk_prev[action] = down
            try:
                state = user32.GetAsyncKeyState(0x01)  # VK_LBUTTON
                down = bool(state & 0x8000) or bool(state & 0x0001)
            except Exception:
                down = False
            if down and not self._lbutton_prev:
                self._on_mouse_down()
            self._lbutton_prev = down
        self.root.after(50, self._poll_hotkeys)

    def _on_mouse_down(self):
        """用户在当前头像格按下左键时，立即（100ms 后）跳炫彩星，不等动画。"""
        if not self.auto_move.get() or self.state != "AVATAR":
            return
        client = self._get_client()
        if not client:
            return
        try:
            import win32gui
            foreground = win32gui.GetForegroundWindow()
            if not foreground or win32gui.GetAncestor(foreground, 2) != client["hwnd"]:
                return
            sx, sy = win32gui.GetCursorPos()
        except Exception:
            return
        rx, ry = sx - client["left"], sy - client["top"]
        cells = self._cells(client)
        if self.cell_index >= len(cells):
            return
        cell = cells[self.cell_index]
        if (cell["ox"] <= rx <= cell["ox"] + cell["w"]
                and cell["oy"] <= ry <= cell["oy"] + cell["h"]):
            self._avatar_click_pending = True
            self._avatar_click_at = time.time()

    def _install_hotkeys(self):
        try:
            import keyboard
        except Exception:
            self.log_insert("[按键] 未安装 keyboard，已启用全局轮询 F7/F8/F9\n")
            return
        try:
            self._hotkeys.append(keyboard.add_hotkey(
                self.cfg.get("hotkey", "f8"), lambda: self._dispatch("capture")))
            self._hotkeys.append(keyboard.add_hotkey(
                self.cfg.get("skip_hotkey", "f9"), lambda: self._dispatch("skip")))
            self._hotkeys.append(keyboard.add_hotkey(
                self.cfg.get("icon_hotkey", "f7"), lambda: self._dispatch("learn_icon")))
        except Exception as exc:
            self.log_insert(f"[按键] keyboard 注册失败：{exc}；仍使用全局轮询\n")
            return
        self.log_insert("[按键] 已启用全局轮询 F7/F8/F9（游戏窗口聚焦也可用）\n")

    def _save(self):
        try:
            save_cfg(self.cfg)
        except Exception as exc:
            if hasattr(self, "log"):
                self.log_insert(f"[配置] 保存失败：{exc}\n")

    def _on_close(self):
        try:
            self.log_insert("[退出] 正在保存...\n")
            self._save()
        except Exception:
            pass
        try:
            import keyboard
            for handle in self._hotkeys:
                try:
                    keyboard.remove_hotkey(handle)
                except Exception:
                    pass
        except Exception:
            pass
        self.grabber.close()
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------- 命令行 ----------------
def missing_deps():
    missing = []
    for module in ("numpy", "cv2", "mss", "PIL", "win32gui", "tkinter"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    return missing


def cli_check():
    missing = missing_deps()
    cfg, error = load_cfg()
    print("依赖:", "OK" if not missing else "缺少 " + ", ".join(missing))
    if error:
        print("配置: 读取失败:", error)
    print("配置:", CFG_FILE)
    print("输出:", OUTPUT_DIR)
    client = get_client_rect(cfg)
    if client:
        print(f"窗口: {client['title']}  {client['w']}x{client['h']}  hwnd={client['hwnd']}")
        pid = window_pid(client["hwnd"])
        game_elevated = process_elevated(pid) if pid else None
        admin = is_admin()
        print(f"权限: 本工具={'管理员' if admin else '普通用户'}; "
              f"游戏={'管理员' if game_elevated else '普通用户' if game_elevated is not None else '未知'}")
        if game_elevated and not admin:
            print("[警告] 游戏以管理员运行；F7/F8/F9 会被 Windows 权限挡住。"
                  "请用 run_admin.bat 或右键 run.bat 以管理员身份运行。")
    else:
        print("窗口: 未找到（先在 GUI 里锁定，或检查 window_title/hwnd）")
        print(f"权限: 本工具={'管理员' if is_admin() else '普通用户'}")

    folder = cfg.get("card_dir") or ""
    if folder and os.path.isdir(folder):
        lib = CardLib(min_similarity=float(cfg.get("min_similarity", 0.55)),
                      color_tol=float(cfg.get("color_tol", 8000)),
                      min_margin=float(cfg.get("min_margin", 0.004)))
        stats = lib.load(folder)
        print(f"色卡: {stats['loaded']} 张, 跳过 {len(stats['skipped'])}, "
              f"命名异常 {len(stats['invalid'])}, 标签重复 {len(stats['duplicates'])}")
        for name in stats["invalid"][:10]:
            print("  命名异常:", name)
    else:
        print("色卡: 目录无效:", folder or "(未配置)")

    icon_dir = cfg.get("icon_dir") or ""
    try:
        icon_count = IconLib(threshold=float(cfg.get("icon_threshold", 0.78))).load(icon_dir)
        print(f"炫彩星图标: {icon_count} 个模板 ({icon_dir})")
    except Exception as exc:
        print("炫彩星图标: 读取失败:", exc)

    if os.path.exists(SESSION_FILE):
        print(f"会话: {SESSION_FILE} ({os.path.getsize(SESSION_FILE)} bytes)")
    else:
        print("会话: 尚无（首次记录时创建）")
    try:
        import keyboard  # noqa: F401
        print("keyboard: OK")
    except Exception:
        print("keyboard: 未安装（仍使用 GetAsyncKeyState 全局轮询）")

    return 0 if not missing else 1


def _iter_images(folder):
    if os.path.isfile(folder):
        return [folder]
    files = []
    for ext in ("png", "jpg", "jpeg", "bmp", "webp"):
        files += glob.glob(os.path.join(folder, "**", "*." + ext), recursive=True)
    return sorted(set(files))


def _load_lib_for_cli():
    cfg, _ = load_cfg()
    folder = cfg.get("card_dir") or ""
    if not folder or not os.path.isdir(folder):
        print("色卡目录无效:", folder or "(未配置)")
        return None, cfg
    lib = CardLib(min_similarity=float(cfg.get("min_similarity", 0.55)),
                  color_tol=float(cfg.get("color_tol", 8000)),
                  min_margin=float(cfg.get("min_margin", 0.004)))
    stats = lib.load(folder)
    print(f"色卡: {stats['loaded']} 张（{folder}）")
    if stats["invalid"]:
        print(f"  命名异常 {len(stats['invalid'])} 张，例如:", stats["invalid"][:5])
    return lib, cfg


def cli_replay(folder):
    lib, _ = _load_lib_for_cli()
    if lib is None or not lib.cards:
        return 1
    files = _iter_images(folder)
    if not files:
        print("目录里没有图片:", folder)
        return 1
    for path in files:
        img = imread_any(path)
        if img is None:
            print("无法读取:", path)
            continue
        name, score, full, margin = lib.match(img)
        print(f"{os.path.basename(path)} -> {name or '(未匹配)'} "
              f"score={score:.3f} margin={margin:.3f} method={'full' if full else 'color/None'}")
    return 0


def cli_eval(folder):
    lib, _ = _load_lib_for_cli()
    if lib is None or not lib.cards:
        return 1
    if not os.path.isdir(folder):
        print("样本目录不存在:", folder)
        return 1
    total = correct = 0
    errors = []
    for entry in sorted(os.listdir(folder)):
        sub = os.path.join(folder, entry)
        if not os.path.isdir(sub) or entry.startswith("_"):
            continue
        for path in _iter_images(sub):
            img = imread_any(path)
            if img is None:
                continue
            name, score, full, margin = lib.match(img)
            got = combo_label(name) if name else None
            total += 1
            if got == entry:
                correct += 1
            else:
                errors.append((os.path.basename(path), entry, got, score, margin))
    if total == 0:
        print("没有可评测的样本:", folder)
        return 1
    print(f"准确率: {correct}/{total} = {correct / total:.1%}")
    for filename, expected, got, score, margin in errors[:20]:
        print(f"  错: {filename}  期望={expected}  得到={got}  "
              f"score={score:.3f} margin={margin:.3f}")
    if len(errors) > 20:
        print(f"  ... 还有 {len(errors) - 20} 个错误")
    return 0


def enable_dpi_awareness():
    """必须在 Tk/窗口坐标读取之前调用，否则 1920 客户区会被报成 1097。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def run_gui():
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


def main():
    enable_dpi_awareness()
    parser = argparse.ArgumentParser(description="炫彩精灵手动统计工具（不点击）")
    parser.add_argument("--check", action="store_true",
                        help="检查依赖/窗口/色卡/会话，不开 GUI")
    parser.add_argument("--replay", metavar="DIR",
                        help="对目录里的面板截图做离线识别")
    parser.add_argument("--eval", nargs="?", const=os.path.join(OUTPUT_DIR, "samples"),
                        metavar="DIR",
                        help="按 output/samples 的子目录名评测准确率")
    args = parser.parse_args()

    if args.check:
        return cli_check()
    if args.replay:
        return cli_replay(args.replay)
    if args.eval is not None:
        return cli_eval(args.eval)
    return run_gui()


if __name__ == "__main__":
    sys.exit(main() or 0)
