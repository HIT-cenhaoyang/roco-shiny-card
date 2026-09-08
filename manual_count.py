# -*- coding: utf-8 -*-
"""
manual_count.py —— 炫彩精灵手动统计工具(避开 ACE 反作弊)
==========================================================
原理: 不做任何点击注入。你手动点开某只炫彩精灵的颜色面板 -> 按 F8(或点按钮),
      程序抓取游戏窗口 -> 裁出面板区域 -> 与色卡库全图比对 -> 自动计数。

三种大类不用区分: 普通炫彩(颜色一+颜色二+粒子)、黑白炫彩、赛季炫彩,
只要把它们的色卡都放进同一个文件夹即可; 以后新赛季色卡丢进文件夹点"重新加载"即扩展。

依赖: pip install mss opencv-python numpy pillow pywin32 keyboard
运行: python manual_count.py
"""
import os, sys, json, glob, time, queue
import tkinter as tk
from tkinter import messagebox, filedialog
import numpy as np, cv2

try:
    from xuancai_count import extract_two_colors
except ImportError:
    def extract_two_colors(img):  # 退化: 无颜色兜底
        return None

CFG_FILE = "manual_cfg.json"
DEFAULT_CFG = {
    "hwnd": None,              # 锁定后的游戏窗口句柄
    "window_title": "",
    "panel_roi": None,         # 炫彩面板区域 {ox,oy,w,h}, 相对游戏窗口客户区
    "hotkey": "f8",
    "min_similarity": 0.55,    # 全图比对相关度阈值; 低于则用"两个主色"兜底
    "color_tol": 8000,         # 颜色兜底的距离阈值
}


# ---------------- 读图/截图/窗口 ----------------
def imread_any(path):
    """中文路径 + webp 都能读: 先 PIL, 再 cv2.imdecode 兜底, 返回 BGR"""
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


def grab_region(left, top, w, h):
    try:
        import mss
        with mss.mss() as sct:
            shot = sct.grab({"left": int(left), "top": int(top),
                             "width": int(w), "height": int(h)})
            return cv2.cvtColor(np.array(shot), cv2.COLOR_BGRA2BGR)
    except Exception:
        return None


def print_window(c):
    """PrintWindow 抓窗口自身画面(被遮挡也能抓), 返回客户区 BGR"""
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
        ctypes.windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)
        info = bmp.GetInfo()
        arr = np.frombuffer(bmp.GetBitmapBits(True), dtype=np.uint8).reshape(
            (info["bmHeight"], info["bmWidth"], 4))
        off_x, off_y = c["left"] - wl, c["top"] - wt
        arr = arr[off_y:off_y + c["h"], off_x:off_x + c["w"]]
        bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
        win32gui.DeleteObject(bmp.GetHandle()); saveDC.DeleteDC(); mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)
        return bgr
    except Exception:
        return None


def grab_window(c):
    """优先 mss 区域抓屏; 全黑则回退 PrintWindow"""
    img = grab_region(c["left"], c["top"], c["w"], c["h"])
    if img is not None and float(img.mean()) >= 10:
        return img
    return print_window(c)


def get_client_rect(cfg):
    import win32gui
    hwnd = None
    if cfg.get("hwnd"):
        hwnd = int(cfg["hwnd"])
    if not hwnd and cfg.get("window_title"):
        hwnd = win32gui.FindWindow(None, cfg["window_title"])
    if not hwnd:
        best, area = None, 0
        def cb(h, _):
            nonlocal best, area
            if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h):
                l, t, r, b = win32gui.GetWindowRect(h); a = (r - l) * (b - t)
                if a > area: best, area = h, a
            return True
        win32gui.EnumWindows(cb, None); hwnd = best
    if not hwnd:
        return None
    try:
        l, t, r, b = win32gui.GetClientRect(hwnd)
        cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    except Exception:
        return None
    return {"left": cx, "top": cy, "w": r - l, "h": b - t,
            "hwnd": hwnd, "title": win32gui.GetWindowText(hwnd)}


# ---------------- 色卡库 ----------------
def _dist(a, b):
    return sum((int(x) - int(y)) ** 2 for x, y in zip(a, b))


# ---------------- 从色卡文件名解析 颜色一/颜色二/粒子 ----------------
# 命名约定(统一): 序号_类型_颜色组合_粒子.webp
#   普通:  01_亮X亮_蓝绿_菱形.webp         粒子=菱形/爱心/五角星/星星
#   黑白:  100_黑白_黑白_无.webp           只有1张, 粒子填"无"
#   赛季:  200_赛季_狂欢怪谈_无.webp       每赛季1张, 粒子填"无"; 新赛季序号递增即可
PARTICLE_NAMES = ["菱形", "爱心", "五角星", "星星", "四角星"]
BASE_COLORS = ["红", "橙", "黄", "绿", "青", "蓝", "紫", "粉", "白", "黑", "灰", "棕"]
ALL_TOKENS = set(BASE_COLORS + ["浅" + c for c in BASE_COLORS])


def split_two_colors(s):
    """把 '蓝绿' / '浅蓝绿' / '黑白' 切成两个颜色; 切不开则整串作颜色一"""
    s = s.strip()
    for i in range(1, len(s)):
        a, b = s[:i], s[i:]
        if a in ALL_TOKENS and b in ALL_TOKENS:
            return a, b
    return (s, "") if s else ("", "")


def parse_card_name(name):
    """根据色卡文件名解析出 {type, c1, c2, particle}; 兼容普通/黑白/赛季"""
    parts = [p for p in name.split("_") if p]
    particle = parts[-1] if parts and parts[-1] in PARTICLE_NAMES + ["无"] else ""
    name_part = parts[-2] if len(parts) >= 2 else ""
    card_type = parts[-3] if len(parts) >= 3 else ""
    c1, c2 = split_two_colors(name_part)
    return {"type": card_type, "c1": c1, "c2": c2, "particle": particle}


def combo_label(name):
    """把文件名转成可读组合标签, 如 亮X亮_蓝+绿_菱形"""
    info = parse_card_name(name)
    base = info["c1"] + info["c2"]
    base = base or info["c1"] or name
    p = "" if info["particle"] in ("", "无") else f"_{info['particle']}"
    t = info["type"]
    return f"{t}_{base}{p}" if t else f"{base}{p}"


class CardLib:
    W, H = 240, 140   # 全图比对的统一尺寸

    def __init__(self):
        self.cards = []   # {name, img(统一尺寸), sig(两主色)}

    def load(self, folder):
        self.cards = []
        files = []
        for ext in ("png", "jpg", "jpeg", "bmp", "webp"):
            files += glob.glob(os.path.join(folder, "*." + ext))
        for f in files:
            img = imread_any(f)
            if img is None:
                continue
            small = cv2.resize(img, (self.W, self.H))
            self.cards.append({
                "name": os.path.splitext(os.path.basename(f))[0],
                "img": small,
                "sig": extract_two_colors(img),
            })
        return len(self.cards)

    def match(self, panel):
        """全图归一化相关比对; 低置信度时用两个主色兜底。返回 (name, score, 是否全图命中)"""
        p = cv2.resize(panel, (self.W, self.H))
        best_name, best_score, best_sig = None, -2.0, None
        for c in self.cards:
            score = cv2.matchTemplate(p, c["img"], cv2.TM_CCOEFF_NORMED)[0][0]
            if score > best_score:
                best_score, best_name, best_sig = score, c["name"], c["sig"]
        if best_name is not None and best_score >= MIN_SIM:
            return best_name, float(best_score), True
        # 颜色兜底
        res = extract_two_colors(panel)
        if res:
            c1, c2 = res
            bname, bd = None, 1e18
            for c in self.cards:
                if not c["sig"]:
                    continue
                a, b = c["sig"]
                d = min(_dist(c1, a) + _dist(c2, b), _dist(c1, b) + _dist(c2, a))
                if d < bd:
                    bd, bname = d, c["name"]
            if bname and bd <= COLOR_TOL:
                return bname, float(best_score), False
        return None, float(best_score), False


MIN_SIM = 0.55
COLOR_TOL = 8000


def render_stat_card(counts, cards, out_path="stat_card.png", cols=7):
    """把每张色卡 + 数量拼成一张「统计色卡」长图(按 类型_颜色1颜色2_粒子 组合去重)"""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype("msyh.ttc", 18)
    except Exception:
        font = ImageFont.load_default()
    # 卡片名 -> 组合标签; 组合标签 -> 缩略图
    labels, label2img = [], {}
    for c in cards:
        lab = combo_label(c["name"])
        if lab not in labels:
            labels.append(lab)
        label2img.setdefault(lab, c["img"])
    labels += [n for n in counts if n not in labels]
    if not labels:
        return None, 0
    cell_w, cell_h, pad = 210, 168, 8
    rows = (len(labels) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * (cell_w + pad) + pad, rows * (cell_h + pad) + pad),
                       (250, 246, 240))
    draw = ImageDraw.Draw(canvas)
    for i, lab in enumerate(labels):
        r, cc = divmod(i, cols)
        x, y = pad + cc * (cell_w + pad), pad + r * (cell_h + pad)
        bgr = label2img.get(lab)
        if bgr is not None:
            thumb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            thumb.thumbnail((cell_w - 12, cell_h - 52))
            canvas.paste(thumb, (x + (cell_w - thumb.width) // 2, y + 4))
        n = counts.get(lab, 0)
        draw.rectangle([x + 4, y + cell_h - 46, x + cell_w - 4, y + cell_h - 4], fill=(255, 255, 255))
        label = (lab[:14] + "…") if len(lab) > 14 else lab
        draw.text((x + 8, y + cell_h - 42), label, fill=(90, 90, 90), font=font)
        draw.text((x + 8, y + cell_h - 22), f"x {n}", fill=(200, 0, 0), font=font)
    canvas.save(out_path)
    return out_path, len(labels)


# ---------------- 界面 ----------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("炫彩统计 · 手动识别(按 F8)")
        self.cfg = json.load(open(CFG_FILE, encoding="utf-8")) if os.path.exists(CFG_FILE) else dict(DEFAULT_CFG)
        global MIN_SIM, COLOR_TOL
        MIN_SIM = float(self.cfg.get("min_similarity", 0.55))
        COLOR_TOL = float(self.cfg.get("color_tol", 8000))
        self.lib = CardLib()
        self.queue = queue.Queue()
        self.records = []          # 已识别的色卡名列表(用于撤销)
        self.counts = {}           # name -> n

        self._build_window_section()
        self._build_calibrate_section()
        self._build_card_section()
        self._build_run_section()
        self._build_log()

        try:
            import keyboard
            keyboard.add_hotkey(self.cfg.get("hotkey", "f8"), lambda: self.queue.put("capture"))
        except Exception:
            self.log_insert("[提示] 未安装 keyboard, 只能点按钮; pip install keyboard\n")
        self._poll_queue()

    # ---- ① 窗口 ----
    def _build_window_section(self):
        f = tk.LabelFrame(self.root, text="① 选择游戏窗口", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="鼠标指向游戏窗口, 3秒后锁定(推荐)", command=self._pick_by_cursor).pack(side="left", padx=4)
        tk.Button(f, text="刷新窗口列表", command=self._refresh_windows).pack(side="left", padx=4)
        self.win_info = tk.Label(f, text="未锁定", fg="#888")
        self.win_info.pack(side="left", padx=8)

    def _pick_by_cursor(self):
        self.win_info.config(text="3 秒后锁定鼠标所在窗口, 请把鼠标移到游戏窗口上...", fg="#c80")
        self.root.after(3000, self._lock_window_at_cursor)

    def _lock_window_at_cursor(self):
        try:
            import win32gui
            x, y = win32gui.GetCursorPos()
            hwnd = win32gui.WindowFromPoint((x, y))
            hwnd = win32gui.GetAncestor(hwnd, 2) if hwnd else None
            if hwnd:
                self.cfg["hwnd"] = hwnd
                self.cfg["window_title"] = win32gui.GetWindowText(hwnd)
                self._save()
        except Exception:
            hwnd = None
        if not hwnd:
            self.win_info.config(text="没取到窗口, 重试一次", fg="#c00"); return
        c = get_client_rect(self.cfg)
        if c:
            self.win_info.config(text=f"已锁定: {c['title']}  客户区 {c['w']}x{c['h']}", fg="#0a0")
        else:
            self.win_info.config(text="锁定失败", fg="#c00")

    def _refresh_windows(self):
        import win32gui
        names = []
        def cb(h, _):
            if win32gui.IsWindowVisible(h):
                t = win32gui.GetWindowText(h)
                if t:
                    l, tp, r, b = win32gui.GetWindowRect(h)
                    names.append(f"{t}  [{r-l}x{b-tp}]")
            return True
        win32gui.EnumWindows(cb, None)
        names = sorted(set(names))
        dlg = tk.Toplevel(self.root); dlg.title("选择窗口")
        lb = tk.Listbox(dlg, width=80, height=15); lb.pack(padx=8, pady=8)
        for n in names:
            lb.insert("end", n)
        def pick():
            sel = lb.curselection()
            if sel:
                self.cfg["window_title"] = names[sel[0]].rsplit("  [", 1)[0]
                self.cfg["hwnd"] = None
                self._save()
                c = get_client_rect(self.cfg)
                if c:
                    self.cfg["hwnd"] = c["hwnd"]; self._save()
                    self.win_info.config(text=f"已锁定: {c['title']}  客户区 {c['w']}x{c['h']}", fg="#0a0")
            dlg.destroy()
        tk.Button(dlg, text="确定", command=pick).pack(pady=4)

    # ---- ② 校准 ----
    def _build_calibrate_section(self):
        f = tk.LabelFrame(self.root, text="② 校准: 框选「炫彩颜色面板」整块(和色卡同布局的那块)", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="框选面板区域", command=self._calibrate).pack(side="left", padx=4)
        self.cal_info = tk.Label(f, text="未设置", fg="#888")
        self.cal_info.pack(side="left", padx=8)

    def _calibrate(self):
        c = get_client_rect(self.cfg)
        if not c:
            messagebox.showwarning("提示", "先在①锁定游戏窗口"); return
        result = self._select_box(c)
        if not result:
            return
        self.cfg["panel_roi"] = result["roi"]
        self._save()
        self.cal_info.config(text=f"已设置 {result['roi']['w']}x{result['roi']['h']}", fg="#0a0")

    def _select_box(self, c):
        """在游戏窗口截图(客户区)上拖拽框选, 返回窗口相对 roi + 裁剪图"""
        from PIL import Image, ImageTk
        bgr = grab_window(c)
        if bgr is None:
            messagebox.showwarning("提示", "截不到游戏窗口画面, 请确认窗口可见且未最小化"); return None
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        H, W = rgb.shape[:2]
        top = tk.Toplevel(self.root); top.attributes("-topmost", True)
        top.title("拖拽框住炫彩面板, 松开确认")
        scale = min(self.root.winfo_screenwidth() / W, self.root.winfo_screenheight() / H) * 0.98
        dw, dh = int(W * scale), int(H * scale)
        canvas = tk.Canvas(top, width=dw, height=dh, cursor="cross", bg="#222")
        canvas.pack()
        photo = ImageTk.PhotoImage(Image.fromarray(rgb).resize((dw, dh)))
        canvas.create_image(0, 0, anchor="nw", image=photo)
        top._photo = photo
        rect = {"x0": 0, "y0": 0, "x1": 0, "y1": 0}; pressed = {"p": False}; result = {}
        def on_press(e): pressed["p"] = True; rect.update(x0=e.x, y0=e.y, x1=e.x, y1=e.y)
        def on_drag(e):
            if pressed["p"]:
                rect.update(x1=e.x, y1=e.y); canvas.delete("r")
                canvas.create_rectangle(rect["x0"], rect["y0"], rect["x1"], rect["y1"], outline="red", width=2, tags="r")
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

    # ---- ③ 色卡 ----
    def _build_card_section(self):
        f = tk.LabelFrame(self.root, text="③ 色卡库(普通/黑白/赛季都放一个文件夹, 新赛季丢进去点重新加载即可)", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="选择色卡文件夹", command=self._choose_cards).pack(side="left", padx=4)
        tk.Button(f, text="重新加载", command=lambda: self._load_cards(getattr(self, "card_dir", ""))).pack(side="left", padx=4)
        self.card_info = tk.Label(f, text="未加载", fg="#888")
        self.card_info.pack(side="left", padx=8)
        self.card_dir = ""

    def _choose_cards(self):
        d = filedialog.askdirectory(title="选择炫彩色卡文件夹")
        if d:
            self.card_dir = d
            self._load_cards(d)

    def _load_cards(self, folder):
        if not folder:
            messagebox.showwarning("提示", "先选一个色卡文件夹"); return
        n = self.lib.load(folder)
        self.card_info.config(text=f"已加载 {n} 张色卡", fg="#0a0" if n else "#c00")

    # ---- ④ 识别 ----
    def _build_run_section(self):
        f = tk.LabelFrame(self.root, text="④ 识别: 打开炫彩面板后按 F8(或点按钮)", padx=8, pady=6)
        f.pack(fill="x", padx=8, pady=4)
        tk.Button(f, text="识别当前面板 (F8)", command=lambda: self._on_capture()).pack(side="left", padx=4)
        tk.Button(f, text="撤销上一条", command=self._undo).pack(side="left", padx=4)
        tk.Button(f, text="导出数据", command=self._export_json).pack(side="left", padx=4)
        tk.Button(f, text="生成统计色卡", command=self._make_stat_card).pack(side="left", padx=4)
        self.total_info = tk.Label(f, text="已统计 0 只 / 0 种组合", fg="#0a0")
        self.total_info.pack(side="left", padx=8)

    def _on_capture(self):
        c = get_client_rect(self.cfg)
        if not c:
            self.log_insert("[错误] 未锁定游戏窗口\n"); return
        roi = self.cfg.get("panel_roi")
        if not roi:
            self.log_insert("[错误] 先做②框选面板区域\n"); return
        img = grab_window(c)
        if img is None:
            self.log_insert("[错误] 截不到窗口\n"); return
        h, w = img.shape[:2]
        ox, oy = roi["ox"], roi["oy"]
        panel = img[max(0, oy):min(h, oy + roi["h"]), max(0, ox):min(w, ox + roi["w"])]
        if panel.size == 0:
            self.log_insert("[错误] 面板区域越界\n"); return
        cv2.imwrite("last_panel.png", panel)   # 存最近一次面板, 便于核对
        name, score, full = self.lib.match(panel)
        if name is None:
            self.log_insert(f"[未匹配] 相关度={score:.2f} (低于阈值, 已存 last_panel.png 供核对)\n")
            return
        tag = "全图" if full else "颜色兜底"
        info = parse_card_name(name)
        label = combo_label(name)
        self.records.append(label)
        self.counts[label] = self.counts.get(label, 0) + 1
        self.log_insert(f"[{len(self.records)}] {label}  ({tag} 相关度={score:.2f})  颜色一={info['c1']} 颜色二={info['c2']} 粒子={info['particle'] or '无'}\n")
        self._update_total()
        self._save_csv()

    def _undo(self):
        if not self.records:
            return
        name = self.records.pop()
        self.counts[name] -= 1
        if self.counts[name] <= 0:
            del self.counts[name]
        self.log_insert(f"[撤销] {name}\n")
        self._update_total()
        self._save_csv()

    def _update_total(self):
        self.total_info.config(
            text=f"已统计 {len(self.records)} 只 / {len(self.counts)} 种组合")

    def _export(self):
        if not self.counts:
            messagebox.showinfo("提示", "还没有统计结果"); return
        self._save_csv()
        messagebox.showinfo("完成", "已保存 manual_result.csv")

    def _export_json(self):
        """导出结构化数据: 每条 组合/类型/颜色一/颜色二/粒子/数量"""
        if not self.counts:
            messagebox.showinfo("提示", "还没有统计结果"); return
        rows = []
        for label, n in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            info = parse_card_name(label)
            rows.append({"组合": label, "类型": info["type"], "颜色一": info["c1"],
                         "颜色二": info["c2"], "粒子": info["particle"] or "无", "数量": n})
        data = {"total": len(self.records), "types": len(self.counts), "data": rows}
        with open("combo_counts.json", "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        self._save_csv()
        self.log_insert("[导出] combo_counts.json + manual_result.csv\n")
        messagebox.showinfo("完成", "已导出 combo_counts.json(结构化) 和 manual_result.csv")

    def _make_stat_card(self):
        """用当前色卡库 + 统计结果, 生成统计色卡长图 stat_card.png"""
        if not self.lib.cards:
            messagebox.showinfo("提示", "先加载色卡文件夹"); return
        out, n = render_stat_card(self.counts, self.lib.cards)
        if not out:
            messagebox.showinfo("提示", "没有可生成的色卡"); return
        self.log_insert(f"[统计色卡] 已生成 {out}  (共 {n} 格)\n")
        messagebox.showinfo("完成", f"统计色卡已生成:\n{out}\n(共 {n} 格)")

    def _save_csv(self):
        with open("manual_result.csv", "w", encoding="utf-8-sig") as fp:
            fp.write("类型,颜色一,颜色二,粒子,组合,数量\n")
            for label, n in sorted(self.counts.items(), key=lambda kv: -kv[1]):
                # 从组合标签反推字段(标签已是 类型_颜色1颜色2_粒子)
                info = parse_card_name(label)
                fp.write(f"{info['type']},{info['c1']},{info['c2']},{info['particle'] or '无'},{label},{n}\n")

    # ---- 日志 ----
    def _build_log(self):
        f = tk.LabelFrame(self.root, text="日志 / 统计", padx=8, pady=6)
        f.pack(fill="both", expand=True, padx=8, pady=4)
        self.log = tk.Text(f, height=16, wrap="word")
        self.log.pack(fill="both", expand=True)

    def log_insert(self, msg):
        self.log.insert("end", msg); self.log.see("end")

    def _poll_queue(self):
        try:
            while True:
                kind = self.queue.get_nowait()
                if kind == "capture":
                    self._on_capture()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _save(self):
        json.dump(self.cfg, open(CFG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)


def main():
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
