"""Saturated Bold Light — 2026-06-11

核心原则：
- 基底暖杏色系，有色彩感不灰暗
- 语义背景色全部提到 300 级，真正看得出颜色
- 边框加深，元素边界锐利
- 文本纯黑灰高对比
"""

LIGHT_COLORS = {
    # ── 基底（暖杏色系，层间色差 >= 10 L* 点）──
    "bg": "#ffffff",          # 纯白内容区
    "bg2": "#e8ddd0",         # 暖杏色（action bar, sidebar, toolbar）
    "bg3": "#d9cbbb",         # 深杏色（pressed, merged, 最外层框架）
    "bg_input": "#ffffff",    # 输入框纯白

    # ── 文本（纯黑灰）──
    "txt": "#0f172a",         # 主文字，接近纯黑
    "txt2": "#334155",        # 次要文字
    "txt3": "#475569",        # 三级文字

    # ── 边框（深色可见）──
    "bdr": "#ccc0b0",         # 卡片边框（比 bg3 深，边界可见）
    "bdr2": "#a0988c",        # 输入框/悬停边框，深灰褐

    # ── 强调 ──
    "accent": "#2563eb",      # blue-600

    # ── 别名（共享色值）──
    "sidebar_bg": "#e8ddd0",   # = bg2
    "sidebar_bdr": "#ccc0b0",  # = bdr
    "btn_bg": "#e8ddd0",       # = bg2
    "btn_bdr": "#a0988c",      # = bdr2

    # ── 蓝色系（300 级背景，一眼看得出是蓝色）──
    "blue_bg": "#93c5fd",      # blue-300
    "blue": "#2563eb",         # blue-600
    "blue_txt": "#1e40af",     # blue-800
    "blue_bdr": "#60a5fa",     # blue-400

    # ── 绿色系（300 级背景，鲜绿色）──
    "success_bg": "#86efac",   # green-300
    "success": "#16a34a",      # green-600
    "success_txt": "#14532d",  # green-900

    # ── 红色系（300 级背景，明显的红色）──
    "danger_bg": "#fca5a5",    # red-300
    "danger": "#dc2626",       # red-600
    "danger_txt": "#7f1d1d",   # red-900

    # ── 青绿系（release 节点，300 级背景）──
    "teal": "#0d9488",         # teal-600
    "teal_bg": "#5eead4",      # teal-300
    "teal_txt": "#134e4a",     # teal-900

    # ── 琥珀系（trial 节点，300 级背景）──
    "amber": "#d97706",        # amber-600
    "amber_bg": "#fcd34d",     # amber-300
    "amber_txt": "#78350f",    # amber-900

    # ── 药丸（pill）──
    "pill_idle_bg": "#86efac",  # = success_bg
    "pill_idle_fg": "#14532d",  # = success_txt
    "pill_busy_bg": "#93c5fd",  # = blue_bg
    "pill_busy_fg": "#1e40af",  # = blue_txt
}
