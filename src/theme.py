"""界面主题：暗色/浅色样式表，以及系统深浅色判断。"""

from PyQt5.QtGui import QColor, QPalette

LIGHT_QSS = ""  # 浅色用 Qt 默认样式，不做额外装饰

DARK_QSS = """
QWidget {
    background-color: #1e1f22;
    color: #e3e3e3;
}
QTableWidget {
    background-color: #26272b;
    alternate-background-color: #2b2d31;
    gridline-color: #3a3c42;
    selection-background-color: #3d5a80;
    selection-color: #ffffff;
}
QTableWidget::item {
    padding: 2px;
}
QHeaderView::section {
    background-color: #2f3136;
    color: #dcdcdc;
    padding: 4px;
    border: none;
    border-right: 1px solid #3a3c42;
    border-bottom: 1px solid #3a3c42;
}
QTableCornerButton::section {
    background-color: #2f3136;
    border: none;
}
QPushButton {
    background-color: #33353a;
    color: #e3e3e3;
    border: 1px solid #4a4d55;
    border-radius: 4px;
    padding: 5px 14px;
}
QPushButton:hover {
    background-color: #3d4046;
}
QPushButton:pressed {
    background-color: #2a2c31;
}
QPushButton:disabled {
    color: #7a7d85;
    border-color: #3a3c42;
}
QLabel {
    background: transparent;
}
QPlainTextEdit {
    background-color: #26272b;
    color: #e3e3e3;
    border: 1px solid #4a4d55;
    border-radius: 4px;
    padding: 4px;
}
QToolTip {
    background-color: #2f3136;
    color: #e3e3e3;
    border: 1px solid #4a4d55;
}
QScrollBar:vertical {
    background: #26272b;
    width: 12px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #4a4d55;
    border-radius: 6px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #5a5e66;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: #26272b;
    height: 12px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background: #4a4d55;
    border-radius: 6px;
    min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
"""


def system_uses_dark() -> bool:
    """判断系统当前是否为暗色主题（Windows 读注册表，其他系统/失败时按浅色）。"""
    try:
        import winreg
    except ImportError:
        return False

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        with key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except OSError:
        return False


def qss_for(dark: bool) -> str:
    return DARK_QSS if dark else LIGHT_QSS


def style_placeholder(widget, dark: bool):
    """给输入框的占位文字（灰字）上色。

    占位文字走的是 QPalette.PlaceholderText，样式表管不到；
    不显式设的话暗色主题下会跟着正文色走，几乎看不出是提示。
    """
    color = "#7a7d85" if dark else "#9aa0a6"  # 暗色这档与禁用按钮文字同色
    palette = widget.palette()
    palette.setColor(QPalette.PlaceholderText, QColor(color))
    widget.setPalette(palette)


def apply_theme(app, dark: bool):
    """把主题应用到整个应用。"""
    app.setStyleSheet(qss_for(dark))
