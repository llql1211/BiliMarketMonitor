"""界面主题：暗色/浅色样式表，以及系统深浅色判断。

主题是纯装饰：任何一步失败都只该"这层样式没上成"，不能影响程序启动。
所以系统主题判断、样式表应用、占位色设置全都自带兜底。
"""

from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import QProxyStyle, QStyle

# 浅色基本用 Qt 默认样式。这一条是例外：序号槽（垂直表头）在最后一行下方的
# 空白区，Qt 默认刷的是表头那种浅灰，表格空白区是白的，于是序号槽会拖一条
# 更深色的竖条到底部。显式跟表格对齐，接缝就没了。
# 水平表头铺满列宽（商品名那列是 Stretch），用不到这条。
LIGHT_QSS = """
QHeaderView {
    background-color: #ffffff;
}
"""

TOOLTIP_DELAY_MS = 2000  # 悬停多久才弹提示（Qt 默认约 0.7 秒）

# 近期成交高亮色的兜底。与 config.DEFAULTS["deal_highlight_color"] 同值：
# 那边是「默认配置」，这边是「配置给的色认不出来时的最后一道」，各守一层。
DEAL_HIGHLIGHT_COLOR = "#e07000"

# 次要信息的弱化色（「原价」列用它跟现价拉开层次）。深浅各一档：
# 白底上要比正文浅但仍读得出，暗底上反过来要比正文暗一档但不能糊成一团。
MUTED_COLOR_LIGHT = "#6e7781"
MUTED_COLOR_DARK = "#85878e"

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
QHeaderView {
    /* 序号槽在最后一行下方的空白区：不显式指定的话会刷成 QWidget 那层底色，
       比表格空白区深，于是序号槽拖一条深色竖条到底部 */
    background-color: #26272b;
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
    """判断系统当前是否为暗色主题（Windows 读注册表，其他系统/失败时按浅色）。

    注册表项缺失、被裁剪、值被写成别的类型都可能出现，
    这里只认 0 / 1 两种取值，认不出来就当浅色——启动路径不能因为主题判断崩掉。
    """
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
    except OSError:  # 键不存在 / 没权限（老系统、被裁剪的精简版）
        return False
    except Exception as err:  # 注册表被写坏时可能抛别的（如类型错误）
        print(f"[theme] 读取系统主题失败：{err}")
        return False

    if isinstance(value, bool):  # bool 是 int 的子类，先按布尔语义走
        return not value
    if isinstance(value, int):
        return value == 0
    return False  # 字符串等认不出来的值一律按浅色


def qss_for(dark: bool) -> str:
    return DARK_QSS if dark else LIGHT_QSS


def deal_highlight_color(text) -> QColor:
    """把配置里的「近期成交」高亮色解析成 QColor；认不出来退回默认色。

    config 那边只校验了「长得像个颜色」（#rrggbb 或纯字母），像
    "orangejuice" 这种过了形状校验、其实并不存在的颜色名归这里兜住——
    否则表格里会静默少掉一处高亮，用户还以为是功能没生效。
    """
    if isinstance(text, str):
        color = QColor(text.strip())
        if color.isValid():
            return color
    print(f"[theme] 无法识别的成交高亮色 {text!r}，已退回 {DEAL_HIGHLIGHT_COLOR}")
    return QColor(DEAL_HIGHLIGHT_COLOR)


def muted_color(dark: bool) -> QColor:
    """次要信息的弱化色（「原价」这类"有就行、别抢眼"的内容用它）。"""
    return QColor(MUTED_COLOR_DARK if dark else MUTED_COLOR_LIGHT)


def style_placeholder(widget, dark: bool):
    """给输入框的占位文字（灰字）上色。

    占位文字走的是 QPalette.PlaceholderText，样式表管不到；
    不显式设的话暗色主题下会跟着正文色走，几乎看不出是提示。
    """
    if widget is None or not hasattr(widget, "setPalette"):
        return  # 传进来的不是控件（None / 假对象）时静静跳过
    color = "#7a7d85" if dark else "#9aa0a6"  # 暗色这档与禁用按钮文字同色
    palette = widget.palette()
    palette.setColor(QPalette.PlaceholderText, QColor(color))
    widget.setPalette(palette)


class _ToolTipDelayStyle(QProxyStyle):
    """只改「悬停多久弹提示」，其余样式提示全部交还原来的样式。

    Qt 把这类延时放在 QStyle 的样式提示里（不是样式表能管的东西），
    唯一的下手处就是套一层代理样式。
    """

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return TOOLTIP_DELAY_MS
        return super().styleHint(hint, option, widget, returnData)


_installed_style = None  # 装上的代理样式，只用来记住「装过了」（所有权归 QApplication）


def install_tooltip_delay(app):
    """拉长整个应用的悬停提示延时（拿不到 QApplication 时静默跳过）。

    和本模块其他函数一样：装不上只是「延时不生效」，不影响程序运行。

    装过没有只能自己记：设了样式表之后，Qt 会在外层再套一个内部的
    QStyleSheetStyle，app.style() 拿到的已经不是这里装的那层了。
    """
    global _installed_style

    if app is None or not hasattr(app, "setStyle") or _installed_style is not None:
        return  # 已经装过了：再包一层只会让代理链越套越长
    try:
        style = _ToolTipDelayStyle(app.style())
        app.setStyle(style)
        _installed_style = style
    except Exception as err:  # 对象已销毁（RuntimeError）等
        print(f"[theme] 设置悬停延时失败：{err}")


def apply_theme(app, dark: bool):
    """把主题应用到整个应用（拿不到 QApplication 时静默跳过）。"""
    if app is None or not hasattr(app, "setStyleSheet"):
        return
    try:
        app.setStyleSheet(qss_for(dark))
    except Exception as err:  # 对象已销毁（RuntimeError）等：样式没上成也不影响数据
        print(f"[theme] 应用主题失败：{err}")
