# -*- coding: utf-8 -*-
"""
炫彩精灵统计 图形界面版 (gui.py)
========================================
一个简单的 Tkinter 界面, 把"窗口选择 + 位置校准 + 色卡匹配 + 自动识别"集中起来。
依赖: pip install mss opencv-python numpy pillow pyautogui pywin32
      需同目录 xuancai_count.py / auto_detect.py
运行: python gui.py
"""
# 已归档（legacy）：v1 的自动点击 GUI，可能触发游戏反作弊，默认不要运行。
# 主流程请用项目根目录的 manual_count.py（只移动鼠标、不点击）。
import os, sys, json, glob, time, threading, queue
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
from tkinter import messagebox, filedialog
import numpy as np, cv2, pyautogui

try:
    from xuancai_count import extract_two_colors, name_of, grab_screen
    from auto_detect import (get_client_rect, cell_centers, find_star_points,
                             crop_client, grab_client, DEFAULT_CFG, CFG_FILE)
except ImportError as e:
    print("缺少依赖文件:", e); sys.exit(1)

from collections import Counter

# ---------------- 工具 ----------------
def dist(a, b):
    return sum((int(x) - int(y)) ** 2 for x, y in zip(a, b))

def imread_any(path):
    """读图(中文路径/webp 都支持): 先 PIL(对 unicode 路径最稳), 失败再 cv2.imdecode 兜底"""
    try:
        from PIL import Image
        pil = Image.open(path).convert("RGB")
        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        pass
    try:
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if img is not None:
            if img.ndim == 2:
                return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            if img.shape[2] == 4:
                return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            return img
    except Exception:
        pass
    return None

def load_cards(folder):
    """加载文件夹里的色卡(含 webp), 每张抽取两个主色作为签名"""
    cards = []
    for ext in ("png", "jpg", "jpeg", "bmp", "webp"):
        cards += glob.glob(os.path.join(folder, "*." + ext))
    out = []
    for f in cards:
        img = imread_any(f)
        if img is None:
            continue
        res = extract_two_colors(img)
        if res:
            out.append((os.path.splitext(os.path.basename(f))[0], res[0], res[1]))
    return out

def match_card(cards, c1, c2, tol=8000):
    if not cards:
        return None
    best, bestd = None, 1e18
    for name, a, b in cards:
        d = min(dist(c1, a) + dist(c2, b), dist(c1, b) + dist(c2, a))
        if d < bestd:
            bestd, best = d, name
    return best if bestd < tol else None


class App:
    def __init__(self, root):
        self.root = root
        root.title("炫彩精灵统计 · 校准与识别")
        self.cfg = json.load(open(CFG_FILE, encoding="utf-8")) if os.path.exists(CFG_FILE) else dict(DEFAULT_CFG)
        self.cards = []
        self.queue = queue.Queue()

        self._build_window_section()
        self._build_calibrate_section()
        self._build_param_section()
        self._build_card_section()
        self._build_run_section()
        self._build_log()

        self._poll_queue()

    # ---- 窗口选择 ----
    def _build_window_section(self):
        f = tk.LabelFrame(self.root, text="① 选择游戏窗口", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        self.win_var = tk.StringVar(value=self.cfg.get("window_title", ""))
        tk.Entry(f, textvariable=self.win_var, width=48).grid(row=0, column=0, padx=4)
        tk.Button(f, text="刷新窗口列表", command=self._refresh_windows).grid(row=0, column=1, padx=4)
        tk.Button(f, text="设为当前窗口", command=self._set_window).grid(row=0, column=2, padx=4)
        tk.Button(f, text="鼠标指向游戏窗口,3秒后锁定(推荐)", command=self._pick_by_cursor).grid(row=1, column=0, columnspan=2, sticky="w", padx=4)
        tk.Label(f, text="或从列表选/手填标题").grid(row=1, column=2, sticky="w", padx=4)
        self.win_info = tk.Label(f, text="", fg="#888")
        self.win_info.grid(row=2, column=0, columnspan=3, sticky="w", padx=4)

    def _refresh_windows(self):
        import win32gui
        names = []
        def cb(h, _):
            if win32gui.IsWindowVisible(h):
                t = win32gui.GetWindowText(h)
                if t:
                    l, top, r, b = win32gui.GetWindowRect(h)
                    names.append(f"{t}  [{r-l}x{b-top}]")
            return True
        win32gui.EnumWindows(cb, None)
        self.win_var.set("")
        self._win_list = names
        # 弹出选择
        dlg = tk.Toplevel(self.root); dlg.title("选择窗口")
        lb = tk.Listbox(dlg, width=80, height=15)
        lb.pack(padx=8, pady=8)
        for n in names:
            lb.insert("end", n)
        def pick():
            sel = lb.curselection()
            if sel:
                raw = names[sel[0]]
                title = raw.rsplit("  [", 1)[0]
                self.win_var.set(title)
            dlg.destroy()
        tk.Button(dlg, text="确定", command=pick).pack(pady=4)

    def _set_window(self):
        self.cfg["window_title"] = self.win_var.get().strip()
        self._save()
        c = get_client_rect(self.cfg)
        if c:
            self.cfg["hwnd"] = c["hwnd"]   # 锁定窗口句柄
            self._save()
            msg = f"已定位: {c['title']}  客户区 {c['w']}x{c['h']}"
            if self._is_fullscreen(c):
                msg += "  ⚠ 像是全屏/无边框模式, 请在游戏内切成「窗口模式」"
            self.win_info.config(text=msg, fg="#0a0")
        else:
            self.win_info.config(text="找不到该窗口", fg="#c00")

    def _pick_by_cursor(self):
        """3 秒后锁定鼠标所在的顶层窗口, 最不容易选错"""
        self.win_info.config(text="3 秒后锁定鼠标所在窗口, 请把鼠标移到游戏窗口上...", fg="#c80")
        self.root.after(3000, self._lock_window_at_cursor)

    def _lock_window_at_cursor(self):
        try:
            import win32gui
            x, y = win32gui.GetCursorPos()
            hwnd = win32gui.WindowFromPoint((x, y))
            hwnd = win32gui.GetAncestor(hwnd, 2) if hwnd else None   # GA_ROOT: 顶层窗口
            if hwnd:
                self.cfg["hwnd"] = hwnd
                self.cfg["window_title"] = win32gui.GetWindowText(hwnd)
                self.win_var.set(self.cfg["window_title"])
                self._save()
        except Exception:
            hwnd = None
        if not hwnd:
            self.win_info.config(text="没取到窗口, 重试一次", fg="#c00"); return
        c = get_client_rect(self.cfg)
        if c:
            msg = f"已锁定: {c['title']}  客户区 {c['w']}x{c['h']}"
            if self._is_fullscreen(c):
                msg += "  ⚠ 像是全屏/无边框模式, 请在游戏内切成「窗口模式」"
            self.win_info.config(text=msg, fg="#0a0")
        else:
            self.win_info.config(text="锁定失败", fg="#c00")

    def _is_fullscreen(self, c):
        """客户区几乎占满显示器 => 判定为全屏/无边框"""
        try:
            import win32api
            mon = win32api.MonitorFromWindow(c["hwnd"], 1)
            ml, mt, mr, mb = win32api.GetMonitorInfo(mon)["Monitor"]
            return c["w"] >= (mr - ml) - 20 and c["h"] >= (mb - mt) - 20
        except Exception:
            return False

    # ---- 校准 ----
    def _build_calibrate_section(self):
        f = tk.LabelFrame(self.root, text="② 位置校准(点按钮后, 在弹出截图上直接框选)", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        self.cal_info = {}
        rows = [("精灵列表网格区", "list", "list_region"),
                ("详情页炫彩星", "star", None),
                ("颜色一/颜色二色块", "chip", "chip_roi")]
        for r, (label, key, cfgkey) in enumerate(rows):
            tk.Button(f, text=f"框选{label}", command=lambda k=key: self._calibrate(k)).grid(row=r, column=0, padx=4, pady=3, sticky="w")
            self.cal_info[key] = tk.Label(f, text="未设置", fg="#888")
            self.cal_info[key].grid(row=r, column=1, padx=4, sticky="w")
        self._refresh_cal_labels()

    def _calibrate(self, which):
        c = get_client_rect(self.cfg)
        if not c:
            messagebox.showwarning("提示", "请先在①里定位到游戏窗口"); return
        roi_img = self._select_box(c, which)
        if not roi_img:
            return
        # roi 已是"相对游戏窗口客户区"的坐标
        if which == "list":
            self.cfg["list_region"] = roi_img["roi"]
            print("列表区:", self.cfg["list_region"])
        elif which == "chip":
            self.cfg["chip_roi"] = roi_img["roi"]
            print("色块区:", self.cfg["chip_roi"])
        elif which == "star":
            cv2.imwrite(self.cfg["star_template"], roi_img["img"])
            print("已保存星模板:", self.cfg["star_template"])
        self._save(); self._refresh_cal_labels()

    def _select_box(self, client, which):
        """只抓游戏窗口客户区的一张图, 在上面拖拽框选。返回窗口相对坐标 {ox,oy,w,h} 和裁剪图"""
        from PIL import Image, ImageTk
        bgr = self._grab_window(client)
        if bgr is None:
            messagebox.showwarning("提示", "截不到游戏窗口画面，请确认游戏窗口在前台且未被最小化")
            return None
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        H, W = rgb.shape[:2]
        top = tk.Toplevel(self.root); top.attributes("-topmost", True)
        top.title("拖拽框选, 松开确认")
        scale = min(self.root.winfo_screenwidth() / W, self.root.winfo_screenheight() / H) * 0.98
        dw, dh = int(W * scale), int(H * scale)
        canvas = tk.Canvas(top, width=dw, height=dh, cursor="cross", bg="#222")
        canvas.pack()
        photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((dw, dh)))
        canvas.create_image(0, 0, anchor="nw", image=photo)
        top._photo = photo   # 关键: 保持对 PhotoImage 的引用, 否则会被 GC 导致黑屏
        rect = {"x0": 0, "y0": 0, "x1": 0, "y1": 0}; pressed = {"p": False}; result = {}
        def on_press(e): pressed["p"] = True; rect.update(x0=e.x, y0=e.y, x1=e.x, y1=e.y)
        def on_drag(e):
            if pressed["p"]:
                rect.update(x1=e.x, y1=e.y)
                canvas.delete("r"); canvas.create_rectangle(rect["x0"], rect["y0"], rect["x1"], rect["y1"], outline="red", width=2, tags="r")
        def on_release(e):
            x0, y0 = min(rect["x0"], rect["x1"]), min(rect["y0"], rect["y1"])
            x1, y1 = max(rect["x0"], rect["x1"]), max(rect["y0"], rect["y1"])
            roi = {"ox": int(x0 / scale), "oy": int(y0 / scale),
                   "w": int((x1 - x0) / scale), "h": int((y1 - y0) / scale)}
            result["roi"] = roi
            result["img"] = bgr[roi["oy"]:roi["oy"] + roi["h"], roi["ox"]:roi["ox"] + roi["w"]]
            top.destroy()
        canvas.bind("<ButtonPress-1>", on_press)
        canvas.bind("<B1-Motion>", on_drag)
        canvas.bind("<ButtonRelease-1>", on_release)
        self.root.wait_window(top)
        return result or None

    def _grab_window(self, c):
        """优先用系统 PrintWindow 抓窗口自身画面(和被遮挡无关, 挪动/DPI 都准);
           若太白/太黑则回退到 mss 窗口区域抓图"""
        img = self._print_window(c)
        if img is not None and 10 <= float(img.mean()) <= 245:
            return img
        return self._grab_region(c["left"], c["top"], c["w"], c["h"])

    @staticmethod
    def _grab_region(left, top, w, h):
        try:
            import mss
            with mss.mss() as sct:
                shot = sct.grab({"left": int(left), "top": int(top), "width": int(w), "height": int(h)})
                arr = np.array(shot)
                return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        except Exception:
            return None

    @staticmethod
    def _print_window(c):
        """用 Win32 PrintWindow 抓窗口自身画面(即使被遮挡也能抓), 返回客户区 BGR"""
        try:
            import win32gui, win32ui, ctypes
            hwnd = c["hwnd"]
            wl, wt, wr, wb = win32gui.GetWindowRect(hwnd)
            ww, wh = max(1, wr - wl), max(1, wb - wt)
            hwndDC = win32gui.GetWindowDC(hwnd)
            mfcDC = win32ui.CreateDCFromHandle(hwndDC)
            saveDC = mfcDC.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap(); bmp.CreateCompatibleBitmap(mfcDC, ww, wh)
            saveDC.SelectObject(bmp)
            ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
            info = bmp.GetInfo()
            data = bmp.GetBitmapBits(True)
            arr = np.frombuffer(data, dtype=np.uint8).reshape((info["bmHeight"], info["bmWidth"], 4))
            off_x, off_y = c["left"] - wl, c["top"] - wt
            arr = arr[off_y:off_y + c["h"], off_x:off_x + c["w"]]
            bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
            win32gui.DeleteObject(bmp.GetHandle()); saveDC.DeleteDC(); mfcDC.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwndDC)
            return bgr
        except Exception:
            return None

    @staticmethod
    def _click(x, y):
        """物理点击: SetCursorPos 移动 + SendInput 点击; 返回 (移动是否成功, 移动后光标位置)"""
        moved = False
        pos = None
        debug = [f"target=({int(x)},{int(y)})"]
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            moved = bool(user32.SetCursorPos(int(x), int(y)))   # 物理像素, 直接到位
            debug.append(f"SetCursorPos={moved}")
            time.sleep(0.12)
            try:
                import win32api
                pos = win32api.GetCursorPos()   # 点击前立刻读回, 看是否被弹回
                debug.append(f"cursor={pos}")
            except Exception:
                debug.append("cursor=unavailable")

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                            ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                            ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

            class INPUTUNION(ctypes.Union):
                _fields_ = [("mi", MOUSEINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

            send_input = user32.SendInput
            send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
            send_input.restype = wintypes.UINT

            def send(flags):
                inp = INPUT()
                inp.type = 0  # INPUT_MOUSE
                inp.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, None)
                result = send_input(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
                debug.append(f"SendInput flags=0x{flags:04x} result={result}")
                if result != 1:
                    raise ctypes.WinError()

            def jiggle():
                # 模拟真人"先轻轻挪一下再点", 有些游戏只认带移动事件的点击
                for dx in (2, -2, 1, -1):
                    inp = INPUT()
                    inp.type = 0
                    inp.u.mi = MOUSEINPUT(dx, 0, 0, 0x0001, 0, None)  # MOUSEEVENTF_MOVE
                    result = send_input(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
                    debug.append(f"SendInput move={dx} result={result}")
                    if result != 1:
                        raise ctypes.WinError()
                    time.sleep(0.03)

            jiggle()
            send(0x0002); time.sleep(0.06)   # 按下(在当前光标处)
            send(0x0004); time.sleep(0.05)   # 抬起
            debug.append("method=SendInput")
        except Exception as exc:
            debug.append(f"SendInput exception={exc!r}")
            try:
                pyautogui.moveTo(int(x), int(y), duration=0.2)
                pyautogui.click()
                debug.append("method=pyautogui")
            except Exception as fallback_exc:
                debug.append(f"pyautogui exception={fallback_exc!r}")
        try:
            with open("click_debug.log", "a", encoding="utf-8") as log:
                log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " | " + " | ".join(debug) + "\n")
        except Exception:
            pass
        return moved, pos

    @staticmethod
    def _compat_click(x, y):
        """兼容点击: 使用旧版 mouse_event, 某些游戏不会响应 SendInput。"""
        debug = [f"target=({int(x)},{int(y)})"]
        try:
            import ctypes
            import win32api
            import win32con
            moved = bool(ctypes.windll.user32.SetCursorPos(int(x), int(y)))
            pos = win32api.GetCursorPos()
            debug.append(f"SetCursorPos={moved}")
            debug.append(f"cursor={pos}")
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.08)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            debug.append("method=mouse_event")
        except Exception as exc:
            debug.append(f"mouse_event exception={exc!r}")
        try:
            with open("click_debug.log", "a", encoding="utf-8") as log:
                log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " | " + " | ".join(debug) + "\n")
        except Exception:
            pass
        return debug

    @staticmethod
    def _window_message_click(hwnd, x, y):
        """窗口消息点击: 不移动真实鼠标, 直接向客户区发送左键消息。"""
        debug = [f"parent_hwnd={hwnd}", f"parent_client=({int(x)},{int(y)})"]
        try:
            import win32api
            import win32con
            import win32gui
            screen_point = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
            target_hwnd = win32gui.WindowFromPoint(screen_point) or hwnd
            target_point = win32gui.ScreenToClient(target_hwnd, screen_point)
            point = win32api.MAKELONG(int(target_point[0]), int(target_point[1]))
            debug.append(f"target_hwnd={target_hwnd}")
            debug.append(f"target_client={target_point}")
            win32api.PostMessage(target_hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, point)
            time.sleep(0.08)
            win32api.PostMessage(target_hwnd, win32con.WM_LBUTTONUP, 0, point)
            debug.append("method=PostMessage")
        except Exception as exc:
            debug.append(f"PostMessage exception={exc!r}")
        try:
            with open("click_debug.log", "a", encoding="utf-8") as log:
                log.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " | " + " | ".join(debug) + "\n")
        except Exception:
            pass
        return debug

    @staticmethod
    def _activate_game(hwnd):
        """物理点击前激活游戏窗口; 失败不报错(游戏可能已在前台)"""
        try:
            import win32gui, win32con
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.25)
        except Exception:
            pass

    def _refresh_cal_labels(self):
        self.cal_info["list"].config(
            text="已设置" if self.cfg.get("list_region") else "未设置",
            fg="#0a0" if self.cfg.get("list_region") else "#888")
        self.cal_info["chip"].config(
            text="已设置" if self.cfg.get("chip_roi") else "未设置",
            fg="#0a0" if self.cfg.get("chip_roi") else "#888")
        self.cal_info["star"].config(
            text="已设置" if os.path.exists(self.cfg.get("star_template", "")) else "未设置",
            fg="#0a0" if os.path.exists(self.cfg.get("star_template", "")) else "#888")

    # ---- 参数 ----
    def _build_param_section(self):
        f = tk.LabelFrame(self.root, text="③ 参数(网格列/行数, 阈值等)", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        self.param_vars = {}
        fields = [("网格列数 cols", "cols"), ("网格行数 rows", "rows"),
                  ("匹配阈值", "match_threshold"), ("点精灵等待", "cell_delay"), ("点星等待", "star_delay")]
        for r, (label, key) in enumerate(fields):
            tk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=2)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.param_vars[key] = v
            tk.Entry(f, textvariable=v, width=8).grid(row=r, column=1, padx=4, sticky="w")
        tk.Button(f, text="保存参数", command=self._save_params).grid(row=0, column=2, rowspan=5, padx=8)

    def _save_params(self):
        for key, v in self.param_vars.items():
            try:
                self.cfg[key] = float(v.get()) if key in ("match_threshold", "cell_delay", "star_delay") else int(float(v.get()))
            except ValueError:
                pass
        self._save()
        messagebox.showinfo("完成", "参数已保存")

    # ---- 色卡 ----
    def _build_card_section(self):
        f = tk.LabelFrame(self.root, text="④ 炫彩色卡库(用于识别出规范颜色名)", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="选择色卡文件夹", command=self._choose_cards).grid(row=0, column=0, padx=4)
        tk.Button(f, text="重新加载", command=lambda: self._load_cards(self.card_dir if hasattr(self, "card_dir") else "")).grid(row=0, column=1, padx=4)
        self.card_info = tk.Label(f, text="未加载", fg="#888")
        self.card_info.grid(row=0, column=2, padx=4, sticky="w")
        self.card_dir = ""

    def _choose_cards(self):
        d = filedialog.askdirectory(title="选择炫彩色卡文件夹")
        if d:
            self.card_dir = d
            self._load_cards(d)

    def _load_cards(self, folder):
        if not folder:
            messagebox.showwarning("提示", "先选一个色卡文件夹"); return
        self.cards = load_cards(folder)
        self.card_info.config(text=f"已加载 {len(self.cards)} 张色卡", fg="#0a0" if self.cards else "#c00")

    # ---- 运行 ----
    def _build_run_section(self):
        f = tk.LabelFrame(self.root, text="⑤ 预览与识别", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="预览网格(不点击)", command=self._preview).grid(row=0, column=0, padx=4)
        tk.Button(f, text="测试首格点击", command=self._test_click).grid(row=0, column=1, padx=4)
        tk.Button(f, text="测试兼容点击", command=self._test_compat_click).grid(row=0, column=2, padx=4)
        tk.Button(f, text="测试窗口消息", command=self._test_window_message_click).grid(row=0, column=3, padx=4)
        tk.Button(f, text="开始识别", command=self._start_run).grid(row=0, column=4, padx=4)
        tk.Button(f, text="停止", command=self._stop).grid(row=0, column=5, padx=4)

    def _preview(self):
        c = get_client_rect(self.cfg)
        if not c:
            messagebox.showwarning("提示", "先定位窗口"); return
        if not self.cfg.get("list_region"):
            messagebox.showwarning("提示", "先框选列表网格区"); return
        # 只截游戏窗口客户区(实时重取窗口位置, 窗口挪了也能对准)
        img = self._grab_window(c)
        if img is None:
            messagebox.showwarning("提示", "截不到游戏窗口画面，请确认窗口在前台且未最小化"); return
        for (ox, oy) in cell_centers(self.cfg):
            cv2.circle(img, (int(ox), int(oy)), 8, (0, 255, 0), 2)
        cv2.imwrite("marked.png", img)
        self._show_image("marked.png")

    def _test_click(self):
        """点击校准网格的第一个中心, 用于单独验证鼠标输入。"""
        c = get_client_rect(self.cfg)
        if not c:
            messagebox.showwarning("提示", "先定位窗口"); return
        if not self.cfg.get("list_region"):
            messagebox.showwarning("提示", "先框选列表网格区"); return
        ox, oy = cell_centers(self.cfg)[0]
        sx, sy = c["left"] + ox, c["top"] + oy
        self.log_insert(f"测试点击目标: ({sx:.0f}, {sy:.0f})\n")
        self._activate_game(c["hwnd"])
        moved, pos = self._click(sx, sy)
        self.log_insert(f"测试完成: SetCursorPos={moved}, 点击前光标={pos}, 详情见 click_debug.log\n")

    def _test_compat_click(self):
        """点击校准网格的第一个中心, 验证旧版 mouse_event 输入。"""
        c = get_client_rect(self.cfg)
        if not c:
            messagebox.showwarning("提示", "先定位窗口"); return
        if not self.cfg.get("list_region"):
            messagebox.showwarning("提示", "先框选列表网格区"); return
        ox, oy = cell_centers(self.cfg)[0]
        sx, sy = c["left"] + ox, c["top"] + oy
        self.log_insert(f"兼容点击目标: ({sx:.0f}, {sy:.0f})\n")
        self._activate_game(c["hwnd"])
        self._compat_click(sx, sy)
        self.log_insert("兼容点击完成, 详情见 click_debug.log\n")

    def _test_window_message_click(self):
        """向游戏窗口客户区发送窗口鼠标消息。"""
        c = get_client_rect(self.cfg)
        if not c:
            messagebox.showwarning("提示", "先定位窗口"); return
        if not self.cfg.get("list_region"):
            messagebox.showwarning("提示", "先框选列表网格区"); return
        ox, oy = cell_centers(self.cfg)[0]
        self.log_insert(f"窗口消息点击目标: 客户区({ox:.0f}, {oy:.0f})\n")
        self._window_message_click(c["hwnd"], ox, oy)
        self.log_insert("窗口消息点击完成, 详情见 click_debug.log\n")

    def _show_image(self, path):
        from PIL import Image, ImageTk
        img = Image.open(path)
        top = tk.Toplevel(self.root); top.title("预览")
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        scale = min(sw / img.width, sh / img.height) * 0.9
        img = img.resize((int(img.width * scale), int(img.height * scale)))
        photo = ImageTk.PhotoImage(img)
        tk.Label(top, image=photo).pack()
        top._photo = photo   # 保持引用, 避免黑屏

    def _start_run(self):
        self._save(); self.running = True
        self.root.attributes("-topmost", True)   # 置顶但不激活, 方便看日志
        # 把 GUI 挪到不挡游戏窗口的屏幕角落(只挪位置, 不改窗口大小)
        c = get_client_rect(self.cfg)
        if c:
            try:
                import win32api
                sw, sh = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
                self.root.update_idletasks()
                w = max(self.root.winfo_width(), 480)
                h = max(self.root.winfo_height(), 480)
                corners = [(0, 0), (sw - w, 0), (0, sh - h), (sw - w, sh - h)]
                def overlaps(cx, cy):
                    return not (cx + w < c["left"] or cx > c["left"] + c["w"] or
                                cy + h < c["top"] or cy > c["top"] + c["h"])
                pos = (0, 0)
                for cx, cy in corners:
                    if not overlaps(cx, cy):
                        pos = (cx, cy); break
                self.root.geometry(f"+{pos[0]}+{pos[1]}")
            except Exception:
                pass
        try:
            import keyboard
            keyboard.add_hotkey("f12", self._stop)   # 全局 F12 停止兜底
        except Exception:
            pass
        self.log_insert("开始倒计时 3 秒: 请用鼠标点击一下游戏窗口(激活它)...\n")
        self._countdown(3)

    def _countdown(self, n):
        if not getattr(self, "running", True):
            self.log_insert("已取消\n"); return
        if n <= 0:
            self.log_insert("倒计时结束, 开始识别!\n")
            threading.Thread(target=self._run_worker, args=(self.cfg,), daemon=True).start()
            return
        self.log_insert(f"{n}...\n")
        self.root.after(1000, lambda: self._countdown(n - 1))

    def _stop(self):
        self.running = False
        self.queue.put(("log", "已请求停止\n"))

    def _restore(self):
        try:
            import keyboard
            keyboard.remove_hotkey("f12")
        except Exception:
            pass
        try:
            self.root.attributes("-topmost", False)
            self.root.lift()
        except Exception:
            pass

    def _run_worker(self, cfg):
        client = get_client_rect(cfg)
        if not client:
            self.queue.put(("err", "找不到游戏窗口")); return
        if not cfg["list_region"] or not cfg["chip_roi"] or not os.path.exists(cfg["star_template"]):
            self.queue.put(("err", "请先完成②校准")); return
        template = cv2.imread(cfg["star_template"])
        if template is None:
            self.queue.put(("err", "星模板读取失败")); return
        try:
            import win32gui; win32gui.SetForegroundWindow(client["hwnd"])
        except Exception:
            pass
        centers = cell_centers(cfg)
        counter = Counter(); skipped = 0
        self.queue.put(("log", f"窗口: {client['title']}  遍历 {len(centers)} 格\n"))
        for i, (ox, oy) in enumerate(centers, 1):
            if not getattr(self, "running", True):
                break
            # 窗口可能被 DPI/多屏系统重新定位, 每轮使用最新客户区坐标
            client = get_client_rect(cfg) or client
            # 激活游戏窗口后再物理点击(每次点前都激活, 防被切走)
            self._activate_game(client["hwnd"])
            sx, sy = client["left"] + ox, client["top"] + oy
            if i == 1:
                moved, pos = self._click(sx, sy)
                try:
                    import ctypes, win32gui
                    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
                    under = win32gui.WindowFromPoint((int(sx), int(sy)))
                    under = win32gui.GetAncestor(under, 2) or under
                    t_under = win32gui.GetWindowText(under)
                    t_fg = win32gui.GetWindowText(win32gui.GetForegroundWindow())
                except Exception:
                    admin, t_under, t_fg = None, "?", "?"
                self.queue.put(("log", f"[调试] 目标({sx},{sy}) 光标={pos} 坐标处窗口={t_under!r} 前台窗口={t_fg!r} 管理员={admin}\n"))
            else:
                self._click(sx, sy)
            time.sleep(cfg["cell_delay"])
            # 只截游戏窗口客户区(实时重取窗口位置)
            client_img = self._grab_window(client)
            if client_img is None:
                self.queue.put(("log", f"[{i}] 截不到窗口, 跳过\n")); continue
            pts = find_star_points(client_img, template, cfg["match_threshold"])
            if not pts:
                skipped += 1
                self.queue.put(("log", f"[{i}] 无炫彩星(跳过)\n")); continue
            self._activate_game(client["hwnd"])
            px, py = client["left"] + pts[0][0], client["top"] + pts[0][1]
            self._click(px, py); time.sleep(cfg["star_delay"])
            # 点星后再截一次, 取颜色一/颜色二(窗口相对坐标)
            client_img2 = self._grab_window(client)
            if client_img2 is None:
                self.queue.put(("log", f"[{i}] 截不到窗口, 跳过\n")); continue
            roi = cfg["chip_roi"]
            res = extract_two_colors(client_img2[roi["oy"]:roi["oy"] + roi["h"],
                                                 roi["ox"]:roi["ox"] + roi["w"]])
            if res is None:
                self.queue.put(("log", f"[{i}] 点了星但未识别到色块\n"))
            else:
                c1, c2 = res
                combo = match_card(self.cards, c1, c2) or f"{name_of(c1)}{c1}+{name_of(c2)}{c2}"
                counter[(c1, c2)] += 1
                self.queue.put(("log", f"[{i}] {combo}\n"))
        # 汇总
        lines = ["\n=== 统计 ===\n"]
        total = 0
        for (c1, c2), n in counter.most_common():
            total += n
            combo = match_card(self.cards, c1, c2) or f"{name_of(c1)}{c1}+{name_of(c2)}{c2}"
            lines.append(f"{combo} : {n} 只\n")
        lines.append(f"跳过(无炫彩/空): {skipped}\n共 {len(counter)} 种, {total} 只\n")
        # 写 CSV
        with open("result.csv", "w", encoding="utf-8-sig") as fp:
            fp.write("颜色组合,数量\n")
            for (c1, c2), n in counter.most_common():
                combo = match_card(self.cards, c1, c2) or f"{name_of(c1)}{c1}+{name_of(c2)}{c2}"
                fp.write(f"{combo},{n}\n")
        self.queue.put(("log", "".join(lines)))
        self.queue.put(("done", "已保存 result.csv"))

    # ---- 日志 ----
    def _build_log(self):
        f = tk.LabelFrame(self.root, text="日志 / 结果", padx=8, pady=6)
        f.pack(fill="both", expand=True, padx=8, pady=4)
        self.log = tk.Text(f, height=14, wrap="word")
        self.log.pack(fill="both", expand=True)

    def log_insert(self, msg):
        self.log.insert("end", msg); self.log.see("end")

    def _poll_queue(self):
        try:
            while True:
                kind, msg = self.queue.get_nowait()
                if kind == "log": self.log_insert(msg)
                elif kind == "err":
                    self.log_insert("[错误] " + msg + "\n"); self._restore()
                elif kind == "done":
                    self.log_insert(msg + "\n"); self._restore()
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _save(self):
        json.dump(self.cfg, open(CFG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
    # 让进程感知 DPI, 避免窗口在不同缩放屏之间移动时坐标漂移
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
