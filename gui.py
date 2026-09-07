# -*- coding: utf-8 -*-
"""手动炫彩统计器的启动入口。

游戏会拦截自动化点击，因此 GUI 不再控制游戏窗口或做图标识别；
实际界面和统计逻辑位于 manual_tracker.py。
"""
from manual_tracker import main


if __name__ == "__main__":
    main()
