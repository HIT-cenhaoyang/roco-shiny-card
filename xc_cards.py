# -*- coding: utf-8 -*-
"""色卡库、文件名解析、全图匹配与统计色卡渲染。

匹配从 v1 的“160 次 cv2.matchTemplate”改为“预计算归一化特征矩阵 + 一次点积”：
等尺寸模板的 TM_CCOEFF_NORMED 就是相关系数，所以分数与原实现等价，
但单次匹配从约 250 ms 降到 1 ms 以内。

本模块不依赖 Tk，方便离线回放/单元测试。
"""
import glob
import os

import cv2
import numpy as np

try:
    # 颜色兜底：两个主色的提取在 v1 的 xuancai_count.py 里
    from xuancai_count import extract_two_colors
except Exception:  # pragma: no cover - 缺依赖时仍可用全图匹配
    def extract_two_colors(img):
        return None


# 命名约定: 序号_类型_颜色组合_粒子.webp
#   普通: 01_亮X亮_蓝绿_五角星.webp
#   黑白: 1000_黑白_黑白_无.webp
#   赛季: 201_赛季_暗夜拾光_无.webp
PARTICLE_NAMES = ["菱形", "爱心", "五角星", "星星", "四角星"]
BASE_COLORS = ["红", "橙", "黄", "绿", "青", "蓝", "紫", "粉", "白", "黑", "灰", "棕"]
ALL_TOKENS = set(BASE_COLORS + ["浅" + c for c in BASE_COLORS])

CARD_W, CARD_H = 240, 140


def imread_any(path):
    """中文路径 + webp 都能读；返回 BGR，失败返回 None。"""
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


def imwrite_any(path, img):
    """中文路径安全写 PNG（cv2.imwrite 在 Windows 下对 Unicode 路径不可靠）。"""
    try:
        ok, encoded = cv2.imencode(".png", img)
        if not ok:
            return False
        encoded.tofile(path)
        return True
    except Exception:
        return False


def _dist(a, b):
    return sum((int(x) - int(y)) ** 2 for x, y in zip(a, b))


def split_two_colors(s):
    """把 '蓝绿' / '浅蓝绿' / '黑白' 切成两个颜色；切不开则整串作颜色一。"""
    s = str(s).strip()
    for i in range(1, len(s)):
        a, b = s[:i], s[i:]
        if a in ALL_TOKENS and b in ALL_TOKENS:
            return a, b
    return (s, "") if s else ("", "")


def parse_card_name(name):
    """根据色卡文件名解析 {type, c1, c2, particle}；兼容普通/黑白/赛季。"""
    parts = [p for p in str(name).split("_") if p]
    particle = parts[-1] if parts and parts[-1] in PARTICLE_NAMES + ["无"] else ""
    name_part = parts[-2] if len(parts) >= 2 else ""
    card_type = parts[-3] if len(parts) >= 3 else ""
    c1, c2 = split_two_colors(name_part)
    return {"type": card_type, "c1": c1, "c2": c2, "particle": particle}


def combo_label(name):
    """把文件名转成可读组合标签, 如 亮X亮_蓝绿_五角星。"""
    info = parse_card_name(name)
    base = info["c1"] + info["c2"] or info["c1"] or str(name)
    particle = "" if info["particle"] in ("", "无") else f"_{info['particle']}"
    card_type = info["type"]
    return f"{card_type}_{base}{particle}" if card_type else f"{base}{particle}"


def card_fields(name):
    """导出用结构化字段。赛季没有两色，颜色列留空、赛季名放 season。

    兼容 raw 文件名（201_赛季_暗夜拾光_无）、组合标签（赛季_暗夜拾光）
    以及 v1 CSV 里只有标签的旧数据。
    """
    info = parse_card_name(name)
    card_type = info["type"]
    particle = info["particle"] or "无"
    season = ""

    if card_type == "赛季":
        season = info["c1"] or str(name)
        return {"type": "赛季", "c1": "", "c2": "", "particle": particle, "season": season}
    if card_type == "黑白":
        # 黑白炫彩的粒子固定为四角星（卡面文件名按旧约定写“无”，实际卡面是四角星）
        return {"type": "黑白", "c1": info["c1"], "c2": info["c2"],
                "particle": "四角星", "season": ""}

    if not card_type:
        text = str(name)
        if text.startswith("赛季_"):
            return {"type": "赛季", "c1": "", "c2": "", "particle": "无",
                    "season": text[3:]}
        if text.startswith("黑白_"):
            c1, c2 = split_two_colors(text[3:])
            return {"type": "黑白", "c1": c1, "c2": c2, "particle": "四角星", "season": ""}

    return {"type": card_type, "c1": info["c1"], "c2": info["c2"],
            "particle": particle, "season": season}


class CardLib:
    W, H = CARD_W, CARD_H

    def __init__(self, min_similarity=0.55, color_tol=8000, min_margin=0.02):
        self.cards = []       # {name, img, sig, label}
        self.names = []
        self.matrix = None    # (n, W*H*3) 已去均值 + L2 归一化
        self.min_similarity = float(min_similarity)
        self.color_tol = float(color_tol)
        self.min_margin = float(min_margin)

    # ---------------- 加载 ----------------
    def load(self, folder):
        self.cards = []
        self.names = []
        self.matrix = None
        skipped, invalid, duplicates = [], [], []
        files = []
        for ext in ("png", "jpg", "jpeg", "bmp", "webp"):
            files += glob.glob(os.path.join(folder, "*." + ext))
        files = sorted(set(files))

        seen_labels = set()
        for path in files:
            img = imread_any(path)
            if img is None or img.size == 0:
                skipped.append(os.path.basename(path))
                continue
            name = os.path.splitext(os.path.basename(path))[0]
            info = parse_card_name(name)
            valid = bool(info["type"] and info["particle"])
            if info["type"] not in ("赛季", "黑白") and not (info["c1"] and info["c2"]):
                valid = False
            if info["type"] == "赛季" and not info["c1"]:
                valid = False
            if not valid:
                invalid.append(name)
                continue

            small = cv2.resize(img, (self.W, self.H))
            label = combo_label(name)
            if label in seen_labels:
                duplicates.append(name)
            seen_labels.add(label)
            self.cards.append({"name": name, "img": small, "label": label,
                               "sig": extract_two_colors(img)})
            self.names.append(name)

        if self.cards:
            self.matrix = np.stack([self._feature(c["img"]) for c in self.cards])
        return {"loaded": len(self.cards), "skipped": skipped,
                "invalid": invalid, "duplicates": duplicates}

    # ---------------- 匹配 ----------------
    @classmethod
    def _feature(cls, img):
        small = cv2.resize(img, (cls.W, cls.H)).astype(np.float32).reshape(-1)
        small -= float(small.mean())
        norm = float(np.linalg.norm(small))
        if norm > 1e-9:
            small /= norm
        return small

    def best_full(self, panel):
        """返回 (最佳名, 最佳分, 次佳名, 次佳分)。"""
        if panel is None or panel.size == 0 or not self.cards or self.matrix is None:
            return None, -2.0, None, -2.0
        query = self._feature(panel)
        scores = self.matrix @ query
        best = int(np.argmax(scores))
        if len(scores) > 1:
            masked = scores.copy()
            masked[best] = -2.0
            second = int(np.argmax(masked))
            return self.names[best], float(scores[best]), self.names[second], float(scores[second])
        return self.names[best], float(scores[best]), None, -2.0

    def match(self, panel):
        """返回 (name, score, 是否全图命中, top1-top2 差值)。

        全图分数够高且与次优拉开差距才直接接受；差距太小时用两个主色兜底。
        """
        name, score, _second, second_score = self.best_full(panel)
        margin = score - second_score
        if name is not None and score >= self.min_similarity:
            if margin >= self.min_margin or score >= 0.95:
                return name, score, True, margin

        res = extract_two_colors(panel) if panel is not None else None
        if res:
            c1, c2 = res
            best_name, best_dist = None, 1e18
            for card in self.cards:
                if not card.get("sig"):
                    continue
                a, b = card["sig"]
                d = min(_dist(c1, a) + _dist(c2, b), _dist(c1, b) + _dist(c2, a))
                if d < best_dist:
                    best_dist, best_name = d, card["name"]
            if best_name and best_dist <= self.color_tol:
                return best_name, score, False, margin

        return None, score, False, margin


# ---------------- 统计色卡 ----------------
def render_stat_card(counts, cards, out_path="output/stat_card.png", cols=7, only_owned=False):
    """把每张色卡 + 数量拼成一张长图；only_owned=True 时只画数量 > 0 的组合。"""
    from PIL import Image, ImageDraw, ImageFont

    counts = counts or {}
    labels, label2img = [], {}
    for card in cards:
        label = card.get("label") or combo_label(card["name"])
        if label not in label2img:
            labels.append(label)
            label2img[label] = card["img"]
    for label in counts:
        if label not in label2img:
            labels.append(label)

    if only_owned:
        labels = [label for label in labels if counts.get(label, 0) > 0]
    if not labels:
        return None, 0

    font = None
    for font_path in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
                      "C:/Windows/Fonts/simhei.ttf"):
        try:
            font = ImageFont.truetype(font_path, 18)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.truetype("msyh.ttc", 18)
        except Exception:
            font = ImageFont.load_default()

    cell_w, cell_h, pad = 210, 168, 8
    cols = max(1, int(cols))
    rows = (len(labels) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * (cell_w + pad) + pad,
                              rows * (cell_h + pad) + pad), (250, 246, 240))
    draw = ImageDraw.Draw(canvas)
    for index, label in enumerate(labels):
        row, col = divmod(index, cols)
        x = pad + col * (cell_w + pad)
        y = pad + row * (cell_h + pad)
        bgr = label2img.get(label)
        if bgr is not None:
            thumb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            thumb.thumbnail((cell_w - 12, cell_h - 52))
            canvas.paste(thumb, (x + (cell_w - thumb.width) // 2, y + 4))
        n = counts.get(label, 0)
        draw.rectangle([x + 4, y + cell_h - 46, x + cell_w - 4, y + cell_h - 4],
                       fill=(255, 255, 255))
        text = (label[:14] + "…") if len(label) > 14 else label
        draw.text((x + 8, y + cell_h - 42), text, fill=(90, 90, 90), font=font)
        draw.text((x + 8, y + cell_h - 22), f"x {n}", fill=(200, 0, 0), font=font)

    folder = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(folder, exist_ok=True)
    canvas.save(out_path)
    return out_path, len(labels)
