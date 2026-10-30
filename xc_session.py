# -*- coding: utf-8 -*-
"""会话持久化（JSONL 追加事件）。

设计目标：
- 每识别一只就落盘，程序崩溃/断电也不丢计数；
- 撤销、清空也写入事件，重启后按顺序回放；
- 兼容 v1 的 manual_result.csv，首次启动时自动导入。

事件类型::

    {"type": "record", "card": "...", "label": "...", "score": 0.99,
     "method": "全图", "margin": 0.09, "sample": "output/samples/...", "ts": 1729...}
    {"type": "undo", "ts": 1729..., "label": "..."}
    {"type": "clear", "ts": 1729...}

这里不依赖 Tk / OpenCV，方便单元测试。
"""
import csv
import json
import os
import time


class SessionStore:
    def __init__(self, path):
        self.path = path
        self.records = []          # [{card,label,score,method,margin,sample,ts,switched}]
        self.counts = {}           # label -> n
        self.card_by_label = {}    # label -> 原始色卡文件名
        self.bad_lines = 0
        self.legacy_imported = 0

    # ---------------- 读取 / 回放 ----------------
    def load(self, legacy_csvs=None):
        """回放已有 session；没有 session 时尝试导入旧 CSV。

        返回统计信息，便于 UI 显示“恢复了多少条”。
        """
        self.records.clear()
        self.counts.clear()
        self.card_by_label.clear()
        self.bad_lines = 0
        self.legacy_imported = 0

        if os.path.isfile(self.path) and os.path.getsize(self.path) > 0:
            self._replay()
        else:
            for legacy in legacy_csvs or []:
                if legacy and os.path.exists(legacy):
                    self._import_csv(legacy)
                    break

        return {"records": len(self.records), "types": len(self.counts),
                "bad_lines": self.bad_lines, "legacy_imported": self.legacy_imported}

    def _replay(self):
        with open(self.path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    self.bad_lines += 1
                    continue
                if isinstance(event, dict):
                    self._apply(event)

    def _import_csv(self, path):
        """把 v1 的 manual_result.csv 转成 record 事件。

        旧 CSV 列：类型,颜色一,颜色二,粒子,组合,数量。
        这里只把“组合”当作 label，具体字段在导出时由 card_fields 兼容。
        """
        with open(path, encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            if not reader.fieldnames or "组合" not in reader.fieldnames or "数量" not in reader.fieldnames:
                return
            rows = list(reader)
        mtime = os.path.getmtime(path)
        for row_index, row in enumerate(rows):
            try:
                n = int(float((row.get("数量") or "0").strip()))
            except (TypeError, ValueError):
                continue
            label = (row.get("组合") or "").strip()
            if not label or n <= 0:
                continue
            for k in range(n):
                event = {"type": "record", "card": label, "label": label,
                         "score": 0.0, "method": "legacy-csv", "margin": 0.0,
                         "sample": "", "switched": True,
                         "ts": mtime + row_index * 0.001 + k * 1e-6}
                self._apply(event)
                self._append(event)
                self.legacy_imported += 1

    # ---------------- 写入 ----------------
    def record(self, card, label, score=0.0, method="", margin=0.0,
               sample="", switched=True):
        event = {"type": "record", "card": card, "label": label,
                 "score": float(score), "method": method, "margin": float(margin),
                 "sample": sample, "switched": bool(switched), "ts": time.time()}
        self._apply(event)
        self._append(event)
        return event

    def undo(self):
        """撤销最后一条记录；没有可撤销时返回 None。"""
        if not self.records:
            return None
        removed = self.records[-1]
        event = {"type": "undo", "ts": time.time(), "label": removed.get("label")}
        self._apply(event)
        self._append(event)
        return removed

    def clear(self):
        event = {"type": "clear", "ts": time.time()}
        self._apply(event)
        self._append(event)

    def _append(self, event):
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        # 上次崩溃可能留下没有换行的半行；先补换行，避免新事件和残行粘在一起
        if os.path.isfile(self.path) and os.path.getsize(self.path) > 0:
            with open(self.path, "rb") as fp:
                fp.seek(-1, os.SEEK_END)
                last = fp.read(1)
            if last not in (b"\n", b"\r"):
                with open(self.path, "a", encoding="utf-8") as fp:
                    fp.write("\n")
        with open(self.path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            fp.flush()

    # ---------------- 状态维护 ----------------
    def _apply(self, event):
        kind = event.get("type")
        if kind == "record":
            label = event.get("label") or event.get("card") or ""
            if not label:
                return
            card = event.get("card") or label
            self.records.append({
                "card": card,
                "label": label,
                "score": event.get("score", 0.0),
                "method": event.get("method", ""),
                "margin": event.get("margin", 0.0),
                "sample": event.get("sample", ""),
                "switched": event.get("switched", True),
                "ts": event.get("ts", 0.0),
            })
            self.counts[label] = self.counts.get(label, 0) + 1
            self.card_by_label[label] = card
        elif kind == "undo":
            if not self.records:
                return
            removed = self.records.pop()
            label = removed.get("label")
            left = self.counts.get(label, 0) - 1
            if left > 0:
                self.counts[label] = left
            else:
                self.counts.pop(label, None)
                if not any(r.get("label") == label for r in self.records):
                    self.card_by_label.pop(label, None)
        elif kind == "clear":
            self.records.clear()
            self.counts.clear()
            self.card_by_label.clear()
