# 已归档的 v1 自动点击入口

这里的 `auto_detect.py`、`gui.py` 是 v1（2026-09 之前）的全自动点击方案，已经从主流程中移除。

## 为什么不建议运行

- 它会自动点击游戏窗口，可能违反游戏用户协议，并触发 ACE 等反作弊；
- 坐标/模板依赖 1920×1080 和固定 UI，游戏更新后容易误点；
- 当前主流程 `manual_count.py` 已经改为“只移动光标，所有点击由用户完成”，不需要它。

## 保留原因

- 参考它的窗口定位 / `PrintWindow` / 框选校准实现；
- 如果有需要，档案里仍保留了 `auto_cfg.json` 和 `detail_star.png`。

## 如果确实要跑（自行承担风险）

```bat
pip install -r requirements.txt
pip install pyautogui
python legacy\gui.py
```

注意：`legacy/` 下的脚本不再维护，问题不修；请优先使用根目录的 `manual_count.py`。
