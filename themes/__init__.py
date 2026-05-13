"""主题系统 — 集中管理颜色令牌和 QSS"""
from .light import LIGHT_COLORS
from .dark import DARK_COLORS
from .qss import build_qss


class ThemeColors:
    """主题颜色令牌容器，支持 dict 访问和属性访问"""

    def __init__(self, colors: dict):
        self._c = colors

    def __getitem__(self, key: str) -> str:
        return self._c.get(key, "")

    def get(self, key: str, default: str = "") -> str:
        return self._c.get(key, default)

    @property
    def bg(self): return self._c["bg"]
    @property
    def bg2(self): return self._c["bg2"]
    @property
    def bg3(self): return self._c["bg3"]
    @property
    def txt(self): return self._c["txt"]
    @property
    def txt2(self): return self._c["txt2"]
    @property
    def txt3(self): return self._c["txt3"]
    @property
    def bdr(self): return self._c["bdr"]
    @property
    def bdr2(self): return self._c["bdr2"]
    @property
    def accent(self): return self._c["accent"]
    @property
    def blue_bg(self): return self._c["blue_bg"]
    @property
    def blue(self): return self._c["blue"]
    @property
    def blue_txt(self): return self._c["blue_txt"]
    @property
    def bg_input(self): return self._c["bg_input"]
    @property
    def success_bg(self): return self._c["success_bg"]
    @property
    def success(self): return self._c["success"]
    @property
    def success_txt(self): return self._c["success_txt"]
    @property
    def danger_bg(self): return self._c["danger_bg"]
    @property
    def danger(self): return self._c["danger"]
    @property
    def danger_txt(self): return self._c["danger_txt"]
    @property
    def blue_bdr(self): return self._c["blue_bdr"]
    @property
    def teal(self): return self._c["teal"]
    @property
    def teal_bg(self): return self._c["teal_bg"]
    @property
    def teal_txt(self): return self._c["teal_txt"]
    @property
    def amber(self): return self._c["amber"]
    @property
    def amber_bg(self): return self._c["amber_bg"]
    @property
    def amber_txt(self): return self._c["amber_txt"]


# 全局单例
_theme: ThemeColors = ThemeColors(DARK_COLORS)


class SemanticTokens:
    """语义令牌 — 脱离具体色值的 UI 语义层。

    用法: s = get_semantic(); s.surface.primary; s.commit.ws_line
    """

    class Surface:
        def __init__(self, t): self._t = t
        @property
        def primary(self):   return self._t.bg
        @property
        def secondary(self): return self._t.bg2
        @property
        def tertiary(self):  return self._t.bg3
        @property
        def input_bg(self):  return self._t.bg_input

    class Text:
        def __init__(self, t): self._t = t
        @property
        def primary(self):   return self._t.txt
        @property
        def secondary(self): return self._t.txt2
        @property
        def tertiary(self):  return self._t.txt3

    class Border:
        def __init__(self, t): self._t = t
        @property
        def subtle(self): return self._t.bdr
        @property
        def normal(self): return self._t.bdr2

    class Status:
        def __init__(self, t): self._t = t
        @property
        def success_bg(self):  return self._t.success_bg
        @property
        def success(self):     return self._t.success
        @property
        def success_txt(self): return self._t.success_txt
        @property
        def danger_bg(self):   return self._t.danger_bg
        @property
        def danger(self):      return self._t.danger
        @property
        def danger_txt(self):  return self._t.danger_txt
        @property
        def warning_bg(self):  return self._t.amber_bg
        @property
        def warning(self):     return self._t.amber
        @property
        def warning_txt(self): return self._t.amber_txt

    class Commit:
        def __init__(self, t): self._t = t
        @property
        def ws_line(self):    return self._t.blue          # workspace 竖线
        @property
        def ws_bg(self):      return self._t.blue_bg        # workspace 选中背景
        @property
        def ws_txt(self):     return self._t.blue_txt       # workspace 文字
        @property
        def fm_default(self): return self._t.blue            # formal 默认竖线
        @property
        def fm_synced(self):  return self._t.success         # formal 已同步
        @property
        def fm_pushed(self):  return self._t.success_txt     # formal 已推送
        @property
        def incoming(self):   return self._t.amber           # incoming 竖线/线
        @property
        def merged(self):     return self._t.bg3             # merged 背景

    class Node:
        def __init__(self, t): self._t = t
        @property
        def workspace(self): return self._t.blue
        @property
        def release(self):   return self._t.teal
        @property
        def trial(self):     return self._t.amber

    def __init__(self):
        self._t = get_theme()

    @property
    def surface(self): return self.Surface(self._t)
    @property
    def text(self):    return self.Text(self._t)
    @property
    def border(self):  return self.Border(self._t)
    @property
    def status(self):  return self.Status(self._t)
    @property
    def commit(self):  return self.Commit(self._t)
    @property
    def node(self):    return self.Node(self._t)
    # 直接透传原始令牌（向后兼容）
    @property
    def raw(self): return self._t


def get_theme() -> ThemeColors:
    return _theme


def get_semantic() -> SemanticTokens:
    return SemanticTokens()


def set_theme(name: str):
    """切换主题: 'dark' | 'light' | 'system' → 根据系统检测"""
    global _theme
    if name == "light":
        _theme = ThemeColors(LIGHT_COLORS)
    else:
        _theme = ThemeColors(DARK_COLORS)


def get_qss(name: str) -> str:
    """获取指定主题的 QSS 字符串（动态插值）"""
    t = ThemeColors(LIGHT_COLORS) if name == "light" else ThemeColors(DARK_COLORS)
    return build_qss(t)
