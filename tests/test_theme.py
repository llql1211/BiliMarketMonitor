"""theme 的回归测试：样式表选择、占位文字配色、系统深浅色判断。

system_uses_dark 依赖 Windows 注册表，这里用假的 winreg 模块覆盖三种情形，
免得测试结果随本机主题设置变化。
"""

import re
import sys
import types

import pytest
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import QComboBox, QDialog, QPlainTextEdit, QStyle

import theme


# ---------------- 工具 ----------------


def _luminance(color):
    """WCAG 相对亮度，用来验算配色对比度。"""
    channels = []
    for value in (color.redF(), color.greenF(), color.blueF()):
        channels.append(
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(fg, bg):
    high, low = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


# ---------------- 样式表 ----------------


def test_qss_follows_flag():
    """暗色用 DARK_QSS，浅色基本交给 Qt 默认样式。"""
    assert theme.qss_for(True) == theme.DARK_QSS
    assert theme.qss_for(False) == theme.LIGHT_QSS


def test_light_qss_stays_minimal():
    """浅色主题只补必要的那几条，不趁机做整体装饰。

    现在唯一的例外是序号槽尾部的底色（见 theme.LIGHT_QSS 注释）。
    """
    assert "QHeaderView" in theme.LIGHT_QSS
    assert "QTableWidget" not in theme.LIGHT_QSS
    assert "QPushButton" not in theme.LIGHT_QSS


@pytest.mark.parametrize("dark, table_bg", [(True, "#26272b"), (False, "#ffffff")])
def test_row_number_gutter_matches_the_table_background(dark, table_bg):
    """序号槽尾部要和表格空白区同色，否则槽会拖一条深色竖条到底部。

    这是量出来的：暗色槽尾 #1e1f22 对表格 #26272b，浅色 #f0f0f0 对 #ffffff。
    浅色的 #ffffff 是 Qt 默认表格底色（LIGHT_QSS 里没写 QTableWidget）。
    """
    header_bg = re.search(r"QHeaderView\s*\{[^}]*?background-color:\s*(\S+);",
                          theme.qss_for(dark))
    assert header_bg is not None, "序号槽底色没显式指定，尾部会露馅"
    assert header_bg.group(1) == table_bg


def test_dark_qss_paints_the_summary_editor_like_the_input_box():
    """抓取总结的明细是 QTextEdit：暗色下得跟输入框同底色，不然是一块白。"""
    assert re.search(
        r"QPlainTextEdit\s*,\s*QTextEdit\s*\{[^}]*background-color:\s*#26272b",
        theme.DARK_QSS,
    ), "QTextEdit 没跟着 QPlainTextEdit 一起配色"


def test_dark_qss_paints_the_tab_bar_itself():
    """设置窗口的页签得自己上色，不能交给原生样式。

    页签是子控件：样式表里不写它，就由原生样式（Windows 那套）用自己的浅色
    画底，而字色照 QWidget 那层走 #e3e3e3——深色下成了白底白字，看不清。
    量过：写不写这几条，同一个窗口在 Fusion 与 windowsvista 下画出来的颜色不一样。
    """
    rules = {
        "没选中的页签": r"QTabBar::tab\s*\{[^}]*background-color:\s*#2f3136",
        "选中的页签": r"QTabBar::tab:selected\s*\{[^}]*background-color:\s*#1e1f22",
        "页签条那一圈边框": r"QTabWidget::pane\s*\{[^}]*border:",
    }
    for what, pattern in rules.items():
        assert re.search(pattern, theme.DARK_QSS), f"{what}没自己上色，会由原生样式画"


def test_dark_qss_paints_the_combo_box_itself():
    """设置窗口的下拉框得自己上色，不能交给原生样式。

    同页签那个毛病：不写这几条，框身就由原生样式（Windows 那套）画成白底，
    字色却照 QWidget 那层走 #e3e3e3。量过：不写时同一个框在 Fusion 与
    windowsvista 下底色不一样（45 对 34），写了才都是 #26272b。
    """
    rules = {
        "框身": r"QComboBox\s*\{[^}]*background-color:\s*#26272b",
        "右边那一格": r"QComboBox::drop-down\s*\{[^}]*width:\s*20px",
        "弹出来的列表": r"QComboBox QAbstractItemView\s*\{[^}]*background-color:\s*#26272b",
    }
    for what, pattern in rules.items():
        assert re.search(pattern, theme.DARK_QSS), f"下拉框的{what}没自己上色，会由原生样式画"


def _combo_image(widget):
    """把下拉框画出来，取最右边那一格（自己画的箭头只会出现在这里）。"""
    widget.addItem("全部商品")
    widget.resize(200, 26)
    image = widget.grab().toImage()
    return image, [image.pixelColor(x, y)
                   for y in range(image.height())
                   for x in range(image.width() - 20, image.width() - 2)]


def test_combo_box_draws_its_own_arrow_in_dark_mode(qapp):
    """暗色下箭头由 theme.ComboBox 自己画。

    QSS 写了 QComboBox::drop-down，原生样式就整格交权（这是甩掉它那块浅色按钮
    的唯一办法），连箭头也不画了——这一颗得自己补上，否则这个控件看不出能点。
    """
    theme.apply_theme(qapp, True)
    _, ours = _combo_image(theme.ComboBox(dark=True))
    _, native = _combo_image(QComboBox())

    arrow = QColor(theme.COMBO_ARROW_COLOR)
    assert arrow in ours, "右边那一格里没有自绘的箭头"
    assert arrow not in native, "原生样式本来就画箭头的话，加的这颗就是多余的"


def test_combo_box_leaves_the_light_theme_to_the_native_style(qapp):
    """浅色下不画：那边 LIGHT_QSS 没有下拉框规则，仍是原生样式，箭头原生就有，
    再画一颗就成了两颗。"""
    theme.apply_theme(qapp, False)
    ours, _ = _combo_image(theme.ComboBox(dark=False))
    native, _ = _combo_image(QComboBox())
    assert ours == native


@pytest.mark.parametrize("dark", [True, False])
def test_apply_theme_sets_app_stylesheet(qapp, dark):
    """apply_theme 把对应 QSS 落到整个应用上。"""
    theme.apply_theme(qapp, dark)
    assert qapp.styleSheet() == theme.qss_for(dark)

    theme.apply_theme(qapp, not dark)
    assert qapp.styleSheet() == theme.qss_for(not dark)


# ---------------- 原生标题栏 ----------------
#
# DWM 只在真实 Windows 上有意义，测试跑在 offscreen 上，所以这里把
# 「是不是 Windows」和「调 DWM」都换成假的，只验调用路径与兜底。


@pytest.mark.parametrize("dark, value", [(True, 1), (False, 0)])
def test_dark_title_bar_sets_the_theme_value(monkeypatch, dark, value):
    """暗色传 1、浅色传 0；先试 Win10 2004+ 的属性 20。"""
    calls = []
    monkeypatch.setattr(theme, "_is_windows", lambda: True)
    monkeypatch.setattr(
        theme, "_set_dwm_attribute",
        lambda hwnd, attribute, val: calls.append((hwnd, attribute, val)) or True,
    )

    class _Window:
        def winId(self):
            return 123

    assert theme.apply_dark_title_bar(_Window(), dark) is True
    assert calls == [(123, 20, value)]


def test_dark_title_bar_falls_back_to_the_older_attribute(monkeypatch):
    """老 Win10 不认属性 20，接着试 19；成功一次就停。"""
    calls = []
    monkeypatch.setattr(theme, "_is_windows", lambda: True)
    monkeypatch.setattr(
        theme, "_set_dwm_attribute",
        lambda hwnd, attribute, value: calls.append((hwnd, attribute, value)) or attribute == 19,
    )

    class _Window:
        def winId(self):
            return 123

    assert theme.apply_dark_title_bar(_Window(), True) is True
    assert calls == [(123, 20, 1), (123, 19, 1)]


def test_dark_title_bar_is_a_noop_outside_windows(monkeypatch):
    """非 Windows 平台连句柄都不该碰（offscreen 测试也走这条）。"""
    monkeypatch.setattr(theme, "_is_windows", lambda: False)

    class _Window:
        def winId(self):
            raise AssertionError("非 Windows 不该取句柄")

    assert theme.apply_dark_title_bar(_Window(), True) is False


def test_dark_title_bar_survives_a_deleted_window(monkeypatch):
    """窗口已经销毁时只放弃配色，不让换主题把程序带崩。"""
    monkeypatch.setattr(theme, "_is_windows", lambda: True)

    class _Window:
        def winId(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    assert theme.apply_dark_title_bar(_Window(), True) is False


def test_dark_title_bar_filter_colors_dialogs_when_they_show(qapp, monkeypatch):
    """Show 事件到的时候原生句柄已经在了，过滤器就在这一刻补色。"""
    seen = []
    monkeypatch.setattr(
        theme, "apply_dark_title_bar",
        lambda window, dark: seen.append((window, dark)),
    )
    event_filter = theme._DarkTitleBarFilter()
    event_filter.dark = True
    qapp.installEventFilter(event_filter)
    dialog = QDialog()
    try:
        dialog.show()
        qapp.processEvents()
        assert any(window is dialog and dark is True for window, dark in seen)
    finally:
        qapp.removeEventFilter(event_filter)
        dialog.close()


def test_apply_theme_keeps_the_title_bar_filter_in_sync(qapp, monkeypatch):
    """换主题时也要把已开窗口的标题栏翻过来，不能只记在过滤器上。"""
    monkeypatch.setattr(theme, "_is_windows", lambda: True)
    monkeypatch.setattr(theme, "apply_dark_title_bar", lambda window, dark: True)

    theme.apply_theme(qapp, True)
    assert theme._dark_title_bar_filter is not None
    assert theme._dark_title_bar_filter.dark is True

    theme.apply_theme(qapp, False)
    assert theme._dark_title_bar_filter.dark is False


# ---------------- 占位文字配色 ----------------


@pytest.mark.parametrize("dark, expected", [(True, "#7a7d85"), (False, "#9aa0a6")])
def test_style_placeholder_colors(qapp, dark, expected):
    """占位文字走 QPalette.PlaceholderText，样式表管不到，必须显式设色。"""
    editor = QPlainTextEdit()
    theme.style_placeholder(editor, dark)

    color = editor.palette().color(QPalette.PlaceholderText).name()
    assert color == expected


# ---------------- 成交高亮色 ----------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("#e07000", "#e07000"),
        ("#ABC", "#aabbcc"),        # 三位简写，QColor 会展开
        ("orange", "#ffa500"),      # 颜色名也认
        ("  #e07000  ", "#e07000"),  # 两端空白先剥掉
    ],
)
def test_deal_highlight_color_parses(text, expected):
    """配置里的颜色文本解析成 QColor。"""
    assert theme.deal_highlight_color(text).name() == expected


@pytest.mark.parametrize(
    "text", [None, "", "   ", "orangejuice", "#12345", 42, {"a": 1}, ["orange"]]
)
def test_deal_highlight_color_falls_back(text):
    """认不出来的一律退回默认色并保持有效。

    config 那边只校验「长得像颜色」，像 "orangejuice" 这种过了形状校验、
    其实并不存在的颜色名落到这里——不兜住的话表格会静默少掉一处高亮。
    """
    color = theme.deal_highlight_color(text)
    assert color.isValid()
    assert color.name() == theme.DEAL_HIGHLIGHT_COLOR


def test_deal_highlight_default_color_is_readable_on_both_themes():
    """默认高亮色要在深浅两种主题的表格底色下都够亮/够暗（对比度 >= 3:1）。

    这是选默认值的硬约束：#ff8c00 在白底上只有 2.3:1，偏看不清，所以换成了 #e07000。
    """
    color = theme.deal_highlight_color(theme.DEAL_HIGHLIGHT_COLOR)
    for background in ("#ffffff", "#26272b"):  # 浅色主题表格 / 暗色主题表格
        assert _contrast(color, QColor(background)) >= 3.0


# ---------------- 涨跌配色 ----------------


@pytest.mark.parametrize(
    "change, dark, color",
    [
        (1, False, theme.PRICE_UP_COLOR_LIGHT),
        (-1, False, theme.PRICE_DOWN_COLOR_LIGHT),
        (1, True, theme.PRICE_UP_COLOR_DARK),
        (-1, True, theme.PRICE_DOWN_COLOR_DARK),
    ],
)
def test_price_delta_color_direction_and_theme(change, dark, color):
    """红涨绿跌，且按主题取色。"""
    assert theme.price_delta_color(change, dark).name() == color


def test_price_delta_color_flat_is_muted():
    """持平不用红绿——没有方向可指时上色只会让人以为涨了或跌了。"""
    for dark in (False, True):
        assert theme.price_delta_color(0, dark).name() == theme.muted_color(dark).name()


@pytest.mark.parametrize("change", [0, 1, -1, 7])
def test_price_delta_colors_are_readable_on_their_theme(change):
    """涨跌色要压得住自家主题的表格底色（对比度 >= 3:1），别糊进背景里。

    暗色那套红绿就是照着这个挑的：直接用浅色主题的 #c62828 在 #26272b 上
    只有 2.2:1，跟背景糊成一片。
    """
    up = theme.price_delta_color(change, dark=True)
    down = theme.price_delta_color(change, dark=False)
    assert _contrast(up, QColor("#26272b")) >= 3.0
    assert _contrast(down, QColor("#ffffff")) >= 3.0


def test_price_delta_colors_differ_from_each_other_and_across_themes():
    """两套主题的红绿得各是各的，抄错一份就白搭。"""
    light_up = theme.price_delta_color(1, dark=False).name()
    dark_up = theme.price_delta_color(1, dark=True).name()
    light_down = theme.price_delta_color(-1, dark=False).name()
    dark_down = theme.price_delta_color(-1, dark=True).name()
    assert len({light_up, dark_up, light_down, dark_down}) == 4


# ---------------- 系统深浅色 ----------------


class _FakeKey:
    """假的注册表键句柄：只要求支持 with 语句。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _install_fake_winreg(monkeypatch, value=None, error=None):
    """往 sys.modules 里塞一个假 winreg；error 非空时 OpenKey 直接抛它。

    注意 QueryValueEx 是 winreg 的模块级函数（不是键对象的方法），
    所以假模块也要按模块级函数提供。
    """
    module = types.SimpleNamespace(HKEY_CURRENT_USER=object())

    def _open_key(root, path):
        if error is not None:
            raise error
        return _FakeKey()

    module.OpenKey = _open_key
    module.QueryValueEx = lambda key, name: (value, 1)
    monkeypatch.setitem(sys.modules, "winreg", module)


def test_system_uses_dark_without_winreg(monkeypatch):
    """非 Windows（import winreg 失败）时一律按浅色。"""
    monkeypatch.setitem(sys.modules, "winreg", None)  # None 会让 import 抛 ImportError
    assert theme.system_uses_dark() is False


def test_system_uses_dark_reads_registry(monkeypatch):
    """AppsUseLightTheme == 0 表示系统在用暗色。"""
    _install_fake_winreg(monkeypatch, value=0)
    assert theme.system_uses_dark() is True


def test_system_uses_light_when_registry_says_light(monkeypatch):
    """AppsUseLightTheme == 1 表示浅色。"""
    _install_fake_winreg(monkeypatch, value=1)
    assert theme.system_uses_dark() is False


def test_system_uses_dark_falls_back_on_registry_error(monkeypatch):
    """注册表项读不到（老系统/被裁剪）时不报错，按浅色处理。"""
    _install_fake_winreg(monkeypatch, error=FileNotFoundError("没有这个键"))
    assert theme.system_uses_dark() is False


@pytest.mark.parametrize(
    "error", [PermissionError("没权限"), OSError("注册表被锁"), TypeError("读法不对")]
)
def test_system_uses_dark_survives_any_registry_failure(monkeypatch, error):
    """注册表读取失败不止 OSError 一种（还被其他程序写坏过），都不能崩启动路径。"""
    _install_fake_winreg(monkeypatch, error=error)
    assert theme.system_uses_dark() is False


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, True),        # 0 = 系统在用暗色
        (1, False),       # 1 = 浅色
        (False, True),    # 布尔值按同样的语义走（False 就是 0）
        (True, False),
        ("dark", False),  # 认不出来的值一律浅色：别猜，样式是纯装饰
        (None, False),
        ([], False),
        (2, False),
        (-1, False),
        (0.0, False),     # float 不是预期的类型，不按 0 处理
    ],
)
def test_system_uses_dark_only_trusts_known_values(monkeypatch, value, expected):
    """注册表里只有 0/1 有意义；其他类型（被别的程序写坏）按浅色处理。"""
    _install_fake_winreg(monkeypatch, value=value)
    assert theme.system_uses_dark() is expected


# ---------------- 空对象也不能崩 ----------------


@pytest.mark.parametrize("app", [None, object(), "not-an-app"])
def test_apply_theme_without_app_does_nothing(app):
    """拿不到 QApplication 时（None/假对象）安静跳过，不抛异常。"""
    theme.apply_theme(app, True)


def test_apply_theme_tolerates_deleted_app(qapp):
    """setStyleSheet 抛异常时只打印一句，不让主题把程序带崩。"""

    class _Broken:
        def setStyleSheet(self, qss):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    theme.apply_theme(_Broken(), True)


@pytest.mark.parametrize("widget", [None, object()])
def test_style_placeholder_with_bad_widget(widget):
    """占位色设置遇到 None/假对象时安静跳过。"""
    theme.style_placeholder(widget, True)


# ---------------- 悬停提示延时 ----------------
#
# 延时是 QStyle 的样式提示（SH_ToolTip_WakeUpDelay），样式表管不到，
# 只能套一层代理样式。装上是全局生效的，下面的用例都不还原样式
# ——代理除了这一处延时之外和原样式完全一致，留在会话里无害。


def test_install_tooltip_delay_lengthens_hover(qapp):
    """装上之后，悬停唤醒延时变成 TOOLTIP_DELAY_MS（Qt 默认约 0.7 秒）。"""
    theme.install_tooltip_delay(qapp)

    # 从 app.style() 问，而不是直接问代理：设过样式表之后 Qt 会在外层再套一个
    # 内部的 QStyleSheetStyle，真正被读到的就是这个值（它会把提示转给代理）。
    hint = qapp.style().styleHint(QStyle.SH_ToolTip_WakeUpDelay, None, None)
    assert hint == theme.TOOLTIP_DELAY_MS
    assert theme.TOOLTIP_DELAY_MS > 700  # 不比 Qt 默认值长的话，这功能没意义


def test_install_tooltip_delay_leaves_other_hints_alone(qapp):
    """除延时外的样式提示照旧交回原样式，界面其余部分不受影响。"""
    theme.install_tooltip_delay(qapp)

    style = theme._installed_style
    base = style.baseStyle()
    for hint in (QStyle.SH_UnderlineShortcut, QStyle.SH_ItemView_ShowDecorationSelected):
        assert style.styleHint(hint) == base.styleHint(hint)


def test_install_tooltip_delay_installs_only_once(qapp):
    """重复调用不再套一层：代理链越套越长会白白多绕几层。"""
    theme.install_tooltip_delay(qapp)
    first = theme._installed_style

    theme.install_tooltip_delay(qapp)
    assert theme._installed_style is first
    assert not isinstance(first.baseStyle(), theme._ToolTipDelayStyle)


@pytest.mark.parametrize("app", [None, object(), "not-an-app"])
def test_install_tooltip_delay_without_app(app):
    """拿不到 QApplication 时安静跳过，不抛异常。"""
    theme.install_tooltip_delay(app)


def test_install_tooltip_delay_tolerates_deleted_app(qapp, monkeypatch):
    """取样式就抛异常时只打印一句，不让延时把程序带崩。"""
    monkeypatch.setattr(theme, "_installed_style", None)  # 假装还没装过

    class _Broken:
        def style(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    theme.install_tooltip_delay(_Broken())
