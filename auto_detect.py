# -*- coding: utf-8 -*-
"""
炫彩精灵全自动检测 v3 —— 网格遍历 + 详情页星标检测
==================================================
流程: 定位游戏窗口 -> 按网格依次点左侧精灵 -> 点详情页上的炫彩星 -> 读颜色一/颜色二 -> 点下一个
依赖: pip install mss opencv-python numpy pillow pyautogui pywin32
      并需同目录 xuancai_count.py

校准(按顺序):
  1) python auto_detect.py list-windows     找游戏窗口标题, 填入 auto_cfg.json 的 window_title
  2) python auto_detect.py capture list     框住左侧精灵列表网格区 -> 记 list_region
     然后在 auto_cfg.json 里填 cols / rows (网格列数/行数, 如 5 和 4)
  3) python auto_detect.py capture star     先点开一只炫彩精灵, 框住"详情页上的那颗炫彩星" -> detail_star.png
  4) python auto_detect.py capture chip     颜色面板显示时, 框住"颜色一+颜色二"两色块 -> chip_roi
运行:
  python auto_detect.py dry-run   在网格上画格子并标出检测到的星, 存 marked.png(不点击, 先核对!)
  python auto_detect.py run       自动遍历点击读色, 生成 result.csv
"""
import os, sys, json, argparse
import numpy as np, cv2, mss, pyautogui
from collections import Counter
try:
    from xuancai_count import extract_two_colors, name_of, grab_screen
except ImportError:
    print("需要同目录 xuancai_count.py"); sys.exit(1)

CFG_FILE = "auto_cfg.json"
DEFAULT_CFG = {
    "window_title": "",
    "list_region": None,        # {ox,oy,w,h}
    "cols": 5,                  # 列表网格列数
    "rows": 4,                  # 列表网格行数
    "star_template": "detail_star.png",
    "chip_roi": None,           # {ox,oy,w,h}
    "match_threshold": 0.8,
    "cell_delay": 1.0,          # 点精灵后等待详情页
    "star_delay": 1.0,          # 点星后等待颜色面板
}

def load_cfg():
    if not os.path.exists(CFG_FILE):
        json.dump(DEFAULT_CFG, open(CFG_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2)
        print("已生成", CFG_FILE, "请先执行 capture 校准"); sys.exit(1)
    return {**DEFAULT_CFG, **json.load(open(CFG_FILE,encoding="utf-8"))}
def save_cfg(cfg):
    json.dump(cfg, open(CFG_FILE,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

# ---------- 游戏窗口 ----------
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
                l,t,r,b = win32gui.GetWindowRect(h); a=(r-l)*(b-t)
                if a>area: best, area = h, a
            return True
        win32gui.EnumWindows(cb, None); hwnd = best
    if not hwnd: return None
    try:
        l,t,r,b = win32gui.GetClientRect(hwnd)
        cx,cy = win32gui.ClientToScreen(hwnd,(0,0))
    except Exception:
        return None
    return {"left":cx,"top":cy,"w":r-l,"h":b-t,"hwnd":hwnd,"title":win32gui.GetWindowText(hwnd)}

def crop_client(client, region):
    x=max(client["left"]+region["ox"], client["left"]); y=max(client["top"]+region["oy"], client["top"])
    w=min(region["w"], client["left"]+client["w"]-x); h=min(region["h"], client["top"]+client["h"]-y)
    return int(x), int(y), int(w), int(h)

def grab_client(client, region=None):
    img,_ = grab_screen()
    if region is None:
        x,y,w,h = crop_client(client, {"ox":0,"oy":0,"w":client["w"],"h":client["h"]})
    else:
        x,y,w,h = crop_client(client, region)
    return img[y:y+h, x:x+w], (x, y)

def client_crop_pts(pts, cx, cy):
    """把[crop内坐标]转回[屏幕坐标]"""l
    return [(cx+px, cy+py) for px,py in pts]

def physical_click(x, y):
    """优先使用 Win32 物理输入，失败时回退到 pyautogui。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        if not user32.SetCursorPos(int(x), int(y)):
            raise ctypes.WinError()

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", INPUTUNION)]

        send_input = user32.SendInput
        send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        send_input.restype = wintypes.UINT

        def send(flags):
            event = INPUT()
            event.type = 0
            event.u.mi = MOUSEINPUT(0, 0, 0, flags, 0, None)
            if send_input(1, ctypes.byref(event), ctypes.sizeof(INPUT)) != 1:
                raise ctypes.WinError()

        time.sleep(0.08)
        send(0x0002)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.06)
        send(0x0004)  # MOUSEEVENTF_LEFTUP
        return "SendInput"
    except Exception:
        try:
            import win32api, win32con
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.06)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return "mouse_event"
        except Exception:
            pyautogui.click(int(x), int(y))
            return "pyautogui"

# ---------- 框选 ----------
def capture_box(title):
    import tkinter as tk
    from PIL import Image, ImageTk
    bgr, mon = grab_screen(); rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    H,W = rgb.shape[:2]
    root = tk.Tk(); root.title(title)
    scale = min(root.winfo_screenwidth()/W, root.winfo_screenheight()/H)*0.95
    dw,dh = int(W*scale), int(H*scale)
    canvas = tk.Canvas(root, width=dw, height=dh, cursor="cross", bg="#222"); canvas.pack()
    canvas.create_image(0,0,anchor="nw", image=ImageTk.PhotoImage(Image.fromarray(rgb).resize((dw,dh))))
    rect={"x0":0,"y0":0,"x1":0,"y1":0}; pressed={"p":False}; result={}
    def on_press(e): pressed["p"]=True; rect.update(x0=e.x,y0=e.y,x1=e.x,y1=e.y)
    def on_drag(e):
        if pressed["p"]:
            rect.update(x1=e.x,y1=e.y)
            canvas.delete("r"); canvas.create_rectangle(rect["x0"],rect["y0"],rect["x1"],rect["y1"],outline="red",width=2,tags="r")
    def on_release(e):
        x0,y0=min(rect["x0"],rect["x1"]),min(rect["y0"],rect["y1"]); x1,y1=max(rect["x0"],rect["x1"]),max(rect["y0"],rect["y1"])
        roi={"left":mon["left"]+int(x0/scale),"top":mon["top"]+int(y0/scale),"width":int((x1-x0)/scale),"height":int((y1-y0)/scale)}
        result["roi"]=roi
        result["img"]=bgr[roi["top"]-mon["top"]:roi["top"]-mon["top"]+roi["height"], roi["left"]-mon["left"]:roi["left"]-mon["left"]+roi["width"]]
        root.destroy()
    canvas.bind("<ButtonPress-1>",on_press); canvas.bind("<B1-Motion>",on_drag); canvas.bind("<ButtonRelease-1>",on_release)
    root.mainloop(); return result

def to_region(client, roi):
    return {"ox":roi["left"]-client["left"],"oy":roi["top"]-client["top"],"w":roi["width"],"h":roi["height"]}

# ---------- 星标定位 ----------
def find_star_points(crop, template, threshold, nms=15):
    res = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
    ys,xs = np.where(res>=threshold)
    pts = sorted(zip(xs,ys), key=lambda p: res[p[1],p[0]], reverse=True)
    kept=[]; tw,th=template.shape[1],template.shape[0]
    for x,y in pts:
        cx,cy=x+tw//2,y+th//2
        if all(abs(cx-kx)>nms or abs(cy-ky)>nms for kx,ky in kept): kept.append((cx,cy))
    return kept

# ---------- 运行 ----------
def cell_centers(cfg):
    R = cfg["list_region"]; cols, rows = cfg["cols"], cfg["rows"]
    cw, ch = R["w"]/cols, R["h"]/rows
    pts=[]
    for r in range(rows):
        for c in range(cols):
            pts.append((R["ox"]+(c+0.5)*cw, R["oy"]+(r+0.5)*ch))
    return pts

def run_auto(cfg, dry_run):
    client = get_client_rect(cfg)
    if not client: print("找不到游戏窗口, 用 list-windows 找标题填 window_title"); return
    print("游戏窗口:", client["title"], "客户区:", (client["w"],client["h"]))
    if not cfg["list_region"] or not cfg["chip_roi"] or not os.path.exists(cfg["star_template"]):
        print("请先完成 capture list/star/chip 校准, 并填 cols/rows"); return
    template = cv2.imread(cfg["star_template"])
    if template is None: print("模板读取失败"); return

    centers = cell_centers(cfg)
    if dry_run:
        img,_ = grab_client(client)
        for (ox,oy) in centers:
            cv2.circle(img,(int(ox),int(oy)),8,(0,255,0),2)
        cv2.imwrite("marked.png", img)
        print(f"网格 {cfg['cols']}x{cfg['rows']}={len(centers)} 格, 绿圈即点击点, 已存 marked.png 核对"); return

    try:
        import win32gui; win32gui.SetForegroundWindow(client["hwnd"])
    except Exception: pass
    print(f"遍历 {len(centers)} 格, 按 Ctrl+C 停止...")
    counter=Counter(); skipped=0
    for i,(ox,oy) in enumerate(centers,1):
        sx, sy = client["left"]+ox, client["top"]+oy
        method = physical_click(sx, sy); pyautogui.sleep(cfg["cell_delay"])
        # 在整个客户区找详情页炫彩星
        client_img, (cx,cy) = grab_client(client)
        star_pts = find_star_points(client_img, template, cfg["match_threshold"])
        if not star_pts:
            skipped+=1
            print(f"[{i}] 无炫彩星(跳过) 点({sx},{sy})"); continue
        # 点第一颗星的屏幕坐标
        px,py = client_crop_pts(star_pts, cx, cy)[0]
        method = physical_click(px, py); pyautogui.sleep(cfg["star_delay"])
        img,_ = grab_screen(); x,y,w,h = crop_client(client, cfg["chip_roi"])
        res = extract_two_colors(img[y:y+h, x:x+w])
        if res is None:
            print(f"[{i}] 点了星但未识别到色块 (可能非炫彩或面板未出)")
        else:
            c1,c2=res; counter[(c1,c2)]+=1
            print(f"[{i}] {name_of(c1)}{c1} + {name_of(c2)}{c2}")
    print("\n=== 统计 ===")
    for (c1,c2),n in counter.most_common(): print(f"{name_of(c1)}{c1} + {name_of(c2)}{c2} : {n} 只")
    print(f"跳过(无炫彩/空): {skipped}")
    with open("result.csv","w",encoding="utf-8-sig") as f:
        f.write("颜色一_RGB,颜色二_RGB,数量\n")
        for (c1,c2),n in counter.most_common():
            f.write(f"#{c1[2]:02x}{c1[1]:02x}{c1[0]:02x},#{c2[2]:02x}{c2[1]:02x}{c2[0]:02x},{n}\n")
    print("已保存 result.csv")

def list_windows():
    import win32gui
    def cb(h,_):
        if win32gui.IsWindowVisible(h):
            t=win32gui.GetWindowText(h)
            if t:
                l,t2,r,b=win32gui.GetWindowRect(h); print(f"{r-l}x{b-t2}  {t}")
        return True
    win32gui.EnumWindows(cb,None)

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="mode")
    sub.add_parser("list-windows")
    sub.add_parser("capture").add_argument("what", choices=["list","star","chip"])
    sub.add_parser("dry-run"); sub.add_parser("run")
    args=ap.parse_args()
    if args.mode=="list-windows": list_windows()
    elif args.mode=="capture":
        cfg=load_cfg(); client=get_client_rect(cfg)
        if not client: print("请先用 list-windows 找游戏标题填 window_title"); return
        print("定位到窗口:", client["title"])
        if args.what=="list":
            r=capture_box("框住左侧精灵列表网格区"); cfg["list_region"]=to_region(client,r["roi"]); save_cfg(cfg)
            print("列表区:",cfg["list_region"],"  记得在 auto_cfg.json 填 cols/rows")
        elif args.what=="star":
            r=capture_box("点开一只炫彩精灵后, 框住「详情页那颗炫彩星」"); cv2.imwrite(cfg["star_template"], r.get("img")); print("已保存星模板")
        elif args.what=="chip":
            r=capture_box("颜色面板显示时, 框住「颜色一+颜色二」两色块"); cfg["chip_roi"]=to_region(client,r["roi"]); save_cfg(cfg); print("色块区:",cfg["chip_roi"])
    elif args.mode=="dry-run": run_auto(load_cfg(), dry_run=True)
    elif args.mode=="run": run_auto(load_cfg(), dry_run=False)
    else: ap.print_help()

if __name__=="__main__": main()
