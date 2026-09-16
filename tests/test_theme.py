"""theme 的回归测试：样式表选择、占位文字配色、系统深浅色判断。

system_uses_dark 依赖 Windows 注册表，这里用假的 winreg 模块覆盖三种情形，
免得测试结果随本机主题设置变化。
"""

import sys
import types

import pytest
from PyQt5.QtGui import QPalette
from PyQt5.QtWidgets import QPlainTextEdit

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
