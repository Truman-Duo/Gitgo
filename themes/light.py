"""Contrast Light — 2026-05-13 确认基准主题

核心原则：
- 两个 UI 区域需看起来不同 → 至少 8 L* 点差距或明显色相偏移
- 白色内容区 vs 蓝灰镶边：9 L* 点差距
- 边框可见：bdr 比 bg 深 16 点，bdr2 深 35 点
- 语义色使用 Tailwind 100 级别，明显可识别
"""

LIGHT_COLORS = {
    # ── 基底 ──
    "bg": "#ffffff",          # L* 100 — 纯白内容区
    "bg2": "#e2e8f0",         # L* 91  — 蓝灰镶边（action bar, sidebar, log bar）
    "bg3": "#cbd5e1",         # L* 84  — 深蓝灰（pressed, merged, disabled）

    # ── 文本 ──
    "txt": "#0f172a",         # L* 9   — 近乎黑主文本
    "txt2": "#475569",        # L* 36  — 深灰蓝次要文本
    "txt3": "#64748b",        # L* 48  — 中灰三级文本

    # ── 边框 ──
    "bdr": "#cbd5e1",         # L* 84  — 卡片边框、面板接缝（= bg3）
    "bdr2": "#94a3b8",        # L* 65  — 输入框轮廓、悬停边框

    # ── 强调 ──
    "accent": "#2563eb",
    "bg_input": "#ffffff",

    # ── 别名（共享色值）──
    "sidebar_bg": "#e2e8f0",   # = bg2
    "sidebar_bdr": "#cbd5e1",  # = bg3
    "btn_bg": "#f1f5f9",
    "btn_bdr": "#cbd5e1",      # = bdr

    # ── 蓝色系 ──
    "blue_bg": "#dbeafe",      # blue-100
    "blue": "#2563eb",         # = accent
    "blue_txt": "#1d4ed8",
    "blue_bdr": "#93bbfd",

    # ── 绿色系 ──
    "success_bg": "#dcfce7",   # green-100
    "success": "#16a34a",
    "success_txt": "#15803d",

    # ── 红色系 ──
    "danger_bg": "#fee2e2",    # red-100
    "danger": "#ef4444",
    "danger_txt": "#b91c1c",

    # ── 青绿系（release 节点）──
    "teal": "#0d9488",
    "teal_bg": "#ccfbf1",      # teal-100
    "teal_txt": "#0f766e",

    # ── 琥珀系（trial 节点）──
    "amber": "#f59e0b",
    "amber_bg": "#fef3c7",     # amber-100
    "amber_txt": "#92400e",

    # ── 药丸（pill）──
    "pill_idle_bg": "#dcfce7",  # = success_bg
    "pill_idle_fg": "#15803d",  # = success_txt
    "pill_busy_bg": "#dbeafe",  # = blue_bg
    "pill_busy_fg": "#1d4ed8",  # = blue_txt
}
