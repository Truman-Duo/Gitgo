"""CommitCanvas — 统一提交区 canvas，左侧 ws 右侧 fm，贝塞尔连接线，内置标题行"""
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


class CommitCanvas(QWidget):
    """统一的提交工作区 canvas — 左侧 workspace，右侧 formal，中间无分隔。

    ws/fm commit box 作为子 widget 在 canvas 内水平布局。
    paintEvent 绘制从 ws 卡片右边缘到 fm 卡片左边缘的贝塞尔连接线。
    connections: list[tuple[ws_y, fm_y, color_hex, dashed]]
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("commit_canvas")
        self.connections: list[tuple[int, int, str, bool]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # 标题行 — 与下方 columns 共享相同列宽，天然对齐
        hdrs = QHBoxLayout()
        hdrs.setContentsMargins(0, 0, 0, 0)
        hdrs.setSpacing(52)
        self.ws_hdr = QLabel("WS")
        self.ws_hdr.setObjectName("ws_hdr")
        self.ws_hdr.setFixedWidth(148)
        self.ws_hdr.setVisible(False)
        hdrs.addWidget(self.ws_hdr, 0)
        self.fm_hdr = QLabel("FM")
        self.fm_hdr.setObjectName("fm_hdr")
        self.fm_hdr.setVisible(False)
        hdrs.addWidget(self.fm_hdr, 1)
        outer.addLayout(hdrs)

        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(52)

        self.ws_column = QWidget()
        self.ws_column.setFixedWidth(148)
        self.ws_layout = QVBoxLayout(self.ws_column)
        self.ws_layout.setSpacing(4)
        self.ws_layout.setContentsMargins(0, 0, 0, 0)
        self.ws_layout.addStretch()
        columns.addWidget(self.ws_column, 0)

        self.fm_column = QWidget()
        self.fm_layout = QVBoxLayout(self.fm_column)
        self.fm_layout.setSpacing(4)
        self.fm_layout.setContentsMargins(0, 0, 0, 0)
        self.fm_layout.addStretch()
        columns.addWidget(self.fm_column, 1)

        outer.addLayout(columns, 1)

    def set_lines(self, lines: list[tuple[int, int, str, bool]]):
        self.connections = lines
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cb = getattr(self, '_line_refresh_cb', None)
        if cb is not None:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, cb)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.connections or not self.isVisible():
            return
        if self.ws_column.geometry().width() == 0:
            return
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)

            x0 = self.ws_column.geometry().right()
            x1 = self.fm_column.geometry().left()

            for ws_y, fm_y, color, dashed in self.connections:
                pen = QPen(QColor(color))
                pen.setWidthF(1.5)
                if dashed:
                    pen.setStyle(Qt.PenStyle.CustomDashLine)
                    pen.setDashPattern([4, 2])
                else:
                    pen.setStyle(Qt.PenStyle.SolidLine)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)

                path = QPainterPath()
                cx = (x0 + x1) // 2
                path.moveTo(x0, ws_y)
                path.cubicTo(cx, ws_y, cx, fm_y, x1, fm_y)
                p.drawPath(path)

                p.setBrush(QColor(color))
                p.setPen(Qt.PenStyle.NoPen)
                arrow = QPolygonF([
                    QPointF(x1 - 6, fm_y - 3),
                    QPointF(x1,     fm_y),
                    QPointF(x1 - 6, fm_y + 3),
                ])
                p.drawPolygon(arrow)
        finally:
            p.end()
