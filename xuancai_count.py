# -*- coding: utf-8 -*-
"""
炫彩精灵颜色统计工具（截图识别版）—— 不碰网络 / 不碰内存，最安全
================================================================
安装依赖(在 Windows 命令行执行):
    pip install mss opencv-python numpy pillow keyboard

三种用法:
  A. 校准区域   python xuancai_count.py calibrate
     弹出程序窗口 -> 用鼠标在游戏画面里"框住"颜色一和颜色二两个色块
     (框一个刚好包住这两颗色块的竖长条) -> 松开鼠标自动保存 roi.json

  B. 批量统计   python xuancai_count.py batch --dir sprites
     sprites 文件夹里放每只炫彩精灵的【全屏截图】，脚本一次统计全部
     (保持和校准时的窗口位置/大小不变，截图用全屏截图)

  C. 实时统计   python xuancai_count.py live       (Windows 需以管理员身份运行)
     打开精灵面板后按 F8，脚本截当前画面并记录；Ctrl+C 结束
"""
import os
import sys
import json
import glob
import argparse
import numpy as np
import cv2
import mss
from collections import Counter

ROI_FILE = "roi.json"

# 颜色名 -> BGR(OpenCV顺序)。换成你游戏的真实色板可让名字更准；统计本身用 RGB 量化值，名字只是好看。
NAMED_COLORS = {
    "红":   (0, 0, 255),
    "橙":   (0, 128, 255),
    "黄":   (0, 255, 255),
    "绿":   (0, 255, 0),
    "青":   (255, 255, 0),     # 青/蓝绿
    "蓝":   (255, 0, 0),
    "蓝紫": (220, 60, 90),     # 常见炫彩颜色一
    "紫":   (255, 0, 128),
    "粉":   (180, 105, 255),
    "白":   (255, 255, 255),
    "黑":   (0, 0, 0),
    "棕":   (0, 64, 128),
}


def quantize(bgr, step=16):
    """把颜色量化到 step 的整数倍，消除微小噪声，避免把同一个颜色数成两种"""
    return tuple(int(max(0, min(255, round(float(v) / step) * step))) for v in bgr)


def name_of(bgr):
    q = quantize(bgr)
    return min(NAMED_COLORS,
               key=lambda n: sum((a - b) ** 2 for a, b in zip(q, NAMED_COLORS[n])))


# ---------------- 颜色提取 ----------------
def extract_two_colors(bgr_roi):
    """
    在"颜色一/颜色二色块区域"里，用高饱和像素聚类出两个主色。
    返回 (bgr1, bgr2) 已量化的颜色；识别不到返回 None。
    """
    if bgr_roi.size == 0:
        return None
    hsv = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2HSV)
    # 高饱和 + 亮度适中 = 纯色块(排除白色背景/黑色文字)
    mask = (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 70)
    pts = bgr_roi[mask].astype(np.float32)
    if len(pts) < 60:
        return None

    k = 2 if len(pts) > 200 else max(1, len(pts) // 60)
    k = max(1, min(k, 2))
    if len(pts) < k * 3:
        k = 1

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.5)
    _, labels, centers = cv2.kmeans(pts, k, None, criteria, 10, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.flatten(), minlength=k)
    order = np.argsort(-counts)
    cols = [quantize(tuple(int(x) for x in centers[i])) for i in order]
    if len(cols) == 1:
        return cols[0], cols[0]
    return cols[0], cols[1]


# ---------------- 截屏 ----------------
def grab_screen():
    """截取整个屏幕，返回 BGR 图像"""
    with mss.mss() as sct:
        mon = sct.monitors[1]            # 主显示器
        shot = np.array(sct.grab(mon))
        return cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR), mon


def crop_roi(bgr_img, roi):
    """按 roi 裁剪，越界自动截断"""
    h, w = bgr_img.shape[:2]
    x = max(0, int(roi["left"]))
    y = max(0, int(roi["top"]))
    x2 = min(w, x + int(roi["width"]))
    y2 = min(h, y + int(roi["height"]))
    return bgr_img[y:y2, x:x2]


# ---------------- 校准区域 ----------------
def calibrate():
    try:
        import tkinter as tk
        from PIL import Image, ImageTk
    except ImportError:
        print("需要 pillow 和 tkinter: pip install pillow")
        sys.exit(1)

    bgr_img, mon = grab_screen()
    rgb = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
    H, W = rgb.shape[:2]
    pic = Image.fromarray(rgb)

    root = tk.Tk()
    root.title("框住 颜色一 + 颜色二 两个色块，然后松开鼠标")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    scale = min(sw / W, sh / H) * 0.95
    dw, dh = int(W * scale), int(H * scale)
    pic = pic.resize((dw, dh))
    canvas = tk.Canvas(root, width=dw, height=dh, cursor="cross", bg="#222")
    canvas.pack()
    photo = ImageTk.PhotoImage(pic)
    canvas.create_image(0, 0, anchor="nw", image=photo)

    rect = {"x0": 0, "y0": 0, "x1": 0, "y1": 0}
    pressed = {"p": False}

    def on_press(e):
        pressed["p"] = True
        rect["x0"], rect["y0"] = e.x, e.y
        rect["x1"], rect["y1"] = e.x, e.y

    def on_drag(e):
        if pressed["p"]:
            rect["x1"], rect["y1"] = e.x, e.y
            canvas.delete("r")
            canvas.create_rectangle(rect["x0"], rect["y0"], rect["x1"], rect["y1"],
                                    outline="red", width=2, tags="r")

    def on_release(e):
        pressed["p"] = False
        x0, y0 = min(rect["x0"], rect["x1"]), min(rect["y0"], rect["y1"])
        x1, y1 = max(rect["x0"], rect["x1"]), max(rect["y0"], rect["y1"])
        if x1 - x0 < 5 or y1 - y0 < 5:
            message = "框太小了，请重新拖一个包含两个色块的框"
            return
        roi = {"left": mon["left"] + int(x0 / scale),
               "top": mon["top"] + int(y0 / scale),
               "width": int((x1 - x0) / scale),
               "height": int((y1 - y0) / scale)}
        with open(ROI_FILE, "w", encoding="utf-8") as f:
            json.dump(roi, f)
        print("已保存区域:", roi)
        root.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    print("弹出窗口里：起点按鼠标左键拖动，终点松开即可。")
    root.mainloop()


# ---------------- 统计主逻辑 ----------------
def load_roi():
    if not os.path.exists(ROI_FILE):
        print(f"先运行校准: python xuancai_count.py calibrate")
        sys.exit(1)
    with open(ROI_FILE, encoding="utf-8") as f:
        return json.load(f)


def summarize(counter):
    print("\n=== 炫彩颜色组合统计 ===")
    print("颜色一 + 颜色二 : 数量")
    total = 0
    for (c1, c2), n in counter.most_common():
        total += n
        print(f"{name_of(c1)} {c1}  +  {name_of(c2)} {c2}  :  {n} 只")
    print(f"\n共 {len(counter)} 种组合，{total} 只炫彩精灵")


def save_csv(counter, path="xuancai_result.csv"):
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("颜色一_RGB,颜色二_RGB,数量\n")
        for (c1, c2), n in counter.most_common():
            f.write(f"#{c1[2]:02x}{c1[1]:02x}{c1[0]:02x},"
                    f"#{c2[2]:02x}{c2[1]:02x}{c2[0]:02x},{n}\n")
    print("已保存:", path)


def run_batch(img_dir):
    roi = load_roi()
    files = sorted(glob.glob(os.path.join(img_dir, "*.png")) +
                   glob.glob(os.path.join(img_dir, "*.jpg")) +
                   glob.glob(os.path.join(img_dir, "*.jpeg")))
    files = [f for f in files if not f.endswith("xuancai_result.csv")]
    if not files:
        print("文件夹里没有图片:", img_dir)
        return

    counter = Counter()
    for f in files:
        img = cv2.imread(f)
        if img is None:
            print("跳过(无法读取):", f)
            continue
        crop = crop_roi(img, roi)
        res = extract_two_colors(crop)
        if res is None:
            print("未识别到色块, 请检查区域或截图:", os.path.basename(f))
            continue
        c1, c2 = res
        counter[(c1, c2)] += 1
        print(f"{os.path.basename(f)}  ->  {name_of(c1)}{c1} + {name_of(c2)}{c2}")

    summarize(counter)
    save_csv(counter)


def run_live():
    roi = load_roi()
    try:
        import keyboard
    except ImportError:
        print("pip install keyboard  (实时模式需管理员运行)")
        sys.exit(1)

    counter = Counter()

    def on_f8():
        img, _ = grab_screen()
        crop = crop_roi(img, roi)
        res = extract_two_colors(crop)
        if res is None:
            print("未识别到色块")
            return
        c1, c2 = res
        counter[(c1, c2)] += 1
        print(f"->  {name_of(c1)}{c1} + {name_of(c2)}{c2}  (已累计 {sum(counter.values())} 只)")
        print("当前:", dict(counter))

    keyboard.add_hotkey("f8", on_f8)
    print("实时模式: 打开精灵面板后按 F8 记录; 在控制台按 Ctrl+C 结束")
    try:
        keyboard.wait()
    except KeyboardInterrupt:
        pass
    finally:
        summarize(counter)
        save_csv(counter)


def main():
    ap = argparse.ArgumentParser(description="炫彩精灵颜色统计")
    sub = ap.add_subparsers(dest="mode")
    sub.add_parser("calibrate")
    p_batch = sub.add_parser("batch")
    p_batch.add_argument("--dir", required=True)
    sub.add_parser("live")
    args = ap.parse_args()

    if args.mode == "calibrate":
        calibrate()
    elif args.mode == "batch":
        run_batch(args.dir)
    elif args.mode == "live":
        run_live()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
