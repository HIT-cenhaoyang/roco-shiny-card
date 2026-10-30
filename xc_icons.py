# -*- coding: utf-8 -*-
"""炫彩星图标模板库。

炫彩星不是单一图标：
- 普通炫彩：右侧彩色星；
- 异色炫彩：右侧另一种星（例如金星球）；
- 赛季炫彩：每个赛季一个专属图标；
- 左侧的紫色/金色图标是精灵徽章，不是炫彩星，不能点。

用法：
    lib = IconLib()
    lib.load("icons")
    best, all = lib.find(detail_bgr)
    # best = {"name", "score", "x", "y"}，坐标相对传入的 detail_bgr
"""
import glob
import os

import cv2

from xc_cards import imread_any


class IconLib:
    def __init__(self, threshold=0.78, scales=(0.5, 0.6, 0.7, 0.85, 1.0, 1.15, 1.3)):
        self.threshold = float(threshold)
        self.scales = tuple(scales)
        self.templates = []  # [{"name": str, "img": BGR}]

    def load(self, folder):
        self.templates = []
        if not folder or not os.path.isdir(folder):
            return 0
        files = []
        for ext in ("png", "jpg", "jpeg", "bmp", "webp"):
            files += glob.glob(os.path.join(folder, "*." + ext))
        for path in sorted(set(files)):
            img = imread_any(path)
            if img is None or img.size == 0:
                continue
            name = os.path.splitext(os.path.basename(path))[0]
            self.templates.append({"name": name, "img": img})
        return len(self.templates)

    def find(self, image, rightmost=True):
        """在 image 中找所有匹配，返回 (best, all)。

        炫彩星在详情页的最右侧；同分/多处匹配时默认取最右边，避开左侧徽章。
        """
        if image is None or image.size == 0 or not self.templates:
            return None, []
        height, width = image.shape[:2]
        matches = []

        for template in self.templates:
            base = template["img"]
            base_h, base_w = base.shape[:2]
            for scale in self.scales:
                tw, th = int(base_w * scale), int(base_h * scale)
                if tw < 12 or th < 12 or tw > width or th > height:
                    continue
                resized = cv2.resize(base, (tw, th), interpolation=cv2.INTER_AREA)
                result = cv2.matchTemplate(image, resized, cv2.TM_CCOEFF_NORMED)
                _, max_value, _, max_loc = cv2.minMaxLoc(result)
                if max_value >= self.threshold:
                    matches.append({
                        "name": template["name"],
                        "score": float(max_value),
                        "x": int(max_loc[0] + tw // 2),
                        "y": int(max_loc[1] + th // 2),
                    })

        if not matches:
            return None, []

        # 同一图标的多个尺度会重叠，做一次简单 NMS
        matches.sort(key=lambda m: -m["score"])
        kept = []
        for match in matches:
            if all(abs(match["x"] - other["x"]) > 18 or abs(match["y"] - other["y"]) > 18
                   for other in kept):
                kept.append(match)

        if rightmost:
            kept.sort(key=lambda m: (-m["x"], -m["score"]))
        else:
            kept.sort(key=lambda m: (-m["score"], -m["x"]))
        return kept[0], kept
