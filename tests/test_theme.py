"""theme 的回归测试：样式表选择、占位文字配色、系统深浅色判断。

system_uses_dark 依赖 Windows 注册表，这里用假的 winreg 模块覆盖三种情形，
免得测试结果随本机主题设置变化。
"""

import sys
import types

import pytest
from PyQt5.QtGui import QColor, QPalette
from PyQt5.QtWidgets import QPlainTextEdit, QStyle

import theme


# ---------------- 样式表 ----------------


def test_qss_follows_flag():
    """暗色用 DARK_QSS，浅色交给 Qt 默认样式（空串）。"""
    assert theme.qss_for(True) == theme.DARK_QSS
    assert theme.qss_for(False) == theme.LIGHT_QSS == ""


@pytest.mark.parametrize("dark", [True, False])
def test_apply_theme_sets_app_stylesheet(qapp, dark):
    """apply_theme 把对应 QSS 落到整个应用上。"""
    theme.apply_theme(qapp, dark)
    assert qapp.styleSheet() == theme.qss_for(dark)

    theme.apply_theme(qapp, not dark)
    assert qapp.styleSheet() == theme.qss_for(not dark)


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

    def _luminance(color):
        channels = []
        for value in (color.redF(), color.greenF(), color.blueF()):
            channels.append(
                value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            )
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    def _contrast(fg, bg):
        high, low = sorted((_luminance(fg), _luminance(bg)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    color = theme.deal_highlight_color(theme.DEAL_HIGHLIGHT_COLOR)
    for background in ("#ffffff", "#26272b"):  # 浅色主题表格 / 暗色主题表格
        assert _contrast(color, QColor(background)) >= 3.0


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
