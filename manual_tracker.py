# -*- coding: utf-8 -*-
"""手动炫彩统计器。

本模块不访问游戏窗口、不截图、不发送鼠标输入，也不识别图标。
用户在游戏中手动查看详情后，在这里记录炫彩类型。
"""
import csv
import json
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from collections import Counter

DATA_FILE = "manual_counts.json"
RESULT_FILE = "result.csv"
NORMAL_COMBOS = 39
PARTICLES = 4


class ManualTracker:
    def __init__(self, root):
        self.root = root
        self.root.title("炫彩精灵统计 · 手动记录")
        self.root.geometry("700x600")
        self.root.minsize(620, 520)
        self.counts = Counter()
        self.history = []
        self._build_ui()
        self._load()
        self._refresh_summary()

    def _build_ui(self):
        intro = tk.LabelFrame(self.root, text="使用方式", padx=10, pady=8)
        intro.pack(fill="x", padx=10, pady=(10, 5))
        tk.Label(
            intro,
            text="请在游戏中手动点开精灵详情并确认炫彩类型，然后在本窗口选择对应项目并点击“记录 1 只”。",
            anchor="w", justify="left", wraplength=650,
        ).pack(fill="x")
        tk.Label(
            intro,
            text="本工具不会控制游戏鼠标，也不会读取窗口、截图或识别炫彩图标。",
            fg="#666", anchor="w",
        ).pack(fill="x", pady=(4, 0))

        form = tk.LabelFrame(self.root, text="记录当前精灵", padx=10, pady=8)
        form.pack(fill="x", padx=10, pady=5)
        self.category = tk.StringVar(value="普通炫彩")
        for col, text in enumerate(("普通炫彩", "黑白炫彩", "赛季炫彩")):
            tk.Radiobutton(
                form, text=text, variable=self.category, value=text,
                command=self._update_form,
            ).grid(row=0, column=col, sticky="w", padx=(0, 18))

        self.normal_frame = tk.Frame(form)
        self.normal_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(9, 0))
        tk.Label(self.normal_frame, text="颜色组合编号 (1-39)").grid(row=0, column=0, sticky="w")
        self.combo_var = tk.StringVar(value="1")
        self.combo_box = ttk.Combobox(
            self.normal_frame, textvariable=self.combo_var,
            values=[str(i) for i in range(1, NORMAL_COMBOS + 1)], width=8,
            state="readonly",
        )
        self.combo_box.grid(row=0, column=1, padx=(8, 20))
        tk.Label(self.normal_frame, text="粒子效果 (1-4)").grid(row=0, column=2, sticky="w")
        self.particle_var = tk.StringVar(value="1")
        ttk.Combobox(
            self.normal_frame, textvariable=self.particle_var,
            values=[str(i) for i in range(1, PARTICLES + 1)], width=8,
            state="readonly",
        ).grid(row=0, column=3, padx=8)
        tk.Label(
            self.normal_frame, text="普通炫彩共 39 × 4 = 156 种",
            fg="#666",
        ).grid(row=0, column=4, padx=(12, 0))

        self.season_frame = tk.Frame(form)
        self.season_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(9, 0))
        tk.Label(self.season_frame, text="赛季名称").grid(row=0, column=0, sticky="w")
        self.season_var = tk.StringVar()
        tk.Entry(self.season_frame, textvariable=self.season_var, width=28).grid(row=0, column=1, padx=8)
        tk.Label(self.season_frame, text="可直接输入新赛季，自动扩展", fg="#666").grid(row=0, column=2)

        tk.Button(form, text="记录 1 只", command=self._record, width=14).grid(
            row=2, column=0, columnspan=3, pady=(12, 0), sticky="w"
        )
        self._update_form()

        summary = tk.LabelFrame(self.root, text="当前统计", padx=10, pady=8)
        summary.pack(fill="both", expand=True, padx=10, pady=5)
        self.summary = tk.Text(summary, height=16, width=80, state="disabled", wrap="none")
        self.summary.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(summary, orient="vertical", command=self.summary.yview)
        scroll.pack(side="right", fill="y")
        self.summary.configure(yscrollcommand=scroll.set)

        actions = tk.Frame(self.root)
        actions.pack(fill="x", padx=10, pady=(0, 10))
        tk.Button(actions, text="撤销上一条", command=self._undo).pack(side="left")
        tk.Button(actions, text="导出 result.csv", command=self._export).pack(side="left", padx=8)
        tk.Button(actions, text="清空统计", command=self._clear).pack(side="right")
        self.status = tk.Label(actions, text="", fg="#555", anchor="e")
        self.status.pack(side="right", padx=12)

    def _update_form(self):
        is_normal = self.category.get() == "普通炫彩"
        is_season = self.category.get() == "赛季炫彩"
        self.normal_frame.grid() if is_normal else self.normal_frame.grid_remove()
        self.season_frame.grid() if is_season else self.season_frame.grid_remove()

    def _current_key(self):
        category = self.category.get()
        if category == "普通炫彩":
            return (category, f"组合{self.combo_var.get()}", f"粒子{self.particle_var.get()}")
        if category == "黑白炫彩":
            return (category, "黑白炫彩", "无")
        season = self.season_var.get().strip()
        if not season:
            messagebox.showwarning("缺少赛季名称", "请填写赛季名称，例如：赛季 1。")
            return None
        return (category, season, "无")

    def _record(self):
        key = self._current_key()
        if key is None:
            return
        self.counts[key] += 1
        self.history.append(key)
        self._save()
        self._refresh_summary()
        self.status.config(text=f"已记录 {sum(self.counts.values())} 只")

    def _undo(self):
        if not self.history:
            return
        key = self.history.pop()
        self.counts[key] -= 1
        if self.counts[key] <= 0:
            del self.counts[key]
        self._save()
        self._refresh_summary()
        self.status.config(text=f"已撤销，当前 {sum(self.counts.values())} 只")

    def _clear(self):
        if not self.counts or not messagebox.askyesno("确认清空", "确定清空全部手动统计吗？"):
            return
        self.counts.clear()
        self.history.clear()
        self._save()
        self._refresh_summary()
        self.status.config(text="统计已清空")

    def _refresh_summary(self):
        normal = sum(n for (category, _, _), n in self.counts.items() if category == "普通炫彩")
        black = sum(n for (category, _, _), n in self.counts.items() if category == "黑白炫彩")
        season = sum(n for (category, _, _), n in self.counts.items() if category == "赛季炫彩")
        lines = [
            f"普通炫彩: {normal} 只    黑白炫彩: {black} 只    赛季炫彩: {season} 只",
            f"合计: {normal + black + season} 只",
            "",
            "大类\t组合/赛季\t粒子\t数量",
            "-" * 48,
        ]
        for key, amount in sorted(self.counts.items()):
            lines.append("\t".join((*key, str(amount))))
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join(lines))
        self.summary.configure(state="disabled")

    def _save(self):
        payload = {"counts": {"\t".join(key): value for key, value in self.counts.items()}}
        with open(DATA_FILE, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)

    def _load(self):
        if not os.path.exists(DATA_FILE):
            return
        try:
            with open(DATA_FILE, encoding="utf-8") as source:
                payload = json.load(source)
            for raw_key, value in payload.get("counts", {}).items():
                key = tuple(raw_key.split("\t"))
                if len(key) == 3:
                    self.counts[key] = int(value)
        except (OSError, ValueError, TypeError):
            messagebox.showwarning("读取失败", f"无法读取 {DATA_FILE}，将从空统计开始。")

    def _export(self):
        with open(RESULT_FILE, "w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["大类", "组合/赛季", "粒子", "数量"])
            for key, amount in sorted(self.counts.items()):
                writer.writerow([*key, amount])
        self.status.config(text=f"已导出 {RESULT_FILE}")


def main():
    root = tk.Tk()
    ManualTracker(root)
    root.mainloop()


if __name__ == "__main__":
    main()
