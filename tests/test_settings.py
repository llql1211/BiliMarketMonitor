"""设置窗口的测试：字段表齐全、控件回填、收集与校验、主题预览。

窗口只依赖 config 和 theme（不认识 MainWindow），所以这里直接造对话框就够，
不用起主窗口。写文件那一步在 test_config.py，接进主窗口那步在 test_app.py。
"""

import pytest
from PyQt5.QtWidgets import QCheckBox, QComboBox, QLineEdit

import config
import settings


@pytest.fixture
def dialog(qapp):
    """造一个设置窗口的工厂，用完收掉。"""

    def _make(**overrides):
        values = dict(config.DEFAULTS)
        values[settings.THEME_KEY] = False
        values.update(overrides)
        made = settings.SettingsDialog(values, False)
        _made.append(made)
        return made

    _made = []
    yield _make
    for item in _made:
        item.close()
        item.deleteLater()
    qapp.processEvents()


# ---------------- 字段表 ----------------


def test_every_config_key_has_exactly_one_field():
    """DEFAULTS 里的每一项都得在窗口里露脸，且只露一次。

    加一项配置忘了写进 FIELDS 时，它会静悄悄地只存在于文件里——
    用户找不到，也没人会注意到，所以这条得钉住。
    """
    keys = [field.key for field in settings.FIELDS]
    assert sorted(keys) == sorted(list(config.DEFAULTS) + [settings.THEME_KEY])
    assert len(keys) == len(set(keys))


def test_fields_are_grouped_into_the_known_sections():
    """每项都归到三页里的一页，页签标题和数量也照着 SECTIONS 来。"""
    names = [name for name, _ in settings.SECTIONS]
    assert names == ["basic", "auto", "notify"]
    for field in settings.FIELDS:
        assert field.section in names


def test_every_field_has_a_short_one_line_tip():
    """「？」上都得有话，而且是一行短句——那是悬停提示，不是说明书。"""
    for field in settings.FIELDS:
        assert field.tip.strip(), f"{field.key} 没有提示"
        assert "\n" not in field.tip, f"{field.key} 的提示换行了"
        assert len(field.tip) <= 45, f"{field.key} 的提示太长：{len(field.tip)} 字"


def test_only_enum_fields_carry_choices():
    """选项是给下拉框准备的，别的控件带上它就是写错了。"""
    for field in settings.FIELDS:
        if field.kind == settings.ENUM:
            assert field.choices, f"{field.key} 是下拉框却没有可选项"
            assert all(len(pair) == 2 for pair in field.choices)
        else:
            assert not field.choices, f"{field.key} 带着没用上的可选项"


def test_scope_choices_match_the_validator():
    """下拉框里的值必须是校验器认的那几个，否则选了也存不进去。"""
    scope = next(f for f in settings.FIELDS if f.key == "auto_poll_scope")
    values = [value for _, value in scope.choices]
    assert values == ["all", "favorite", "sold_out"]
    for value in values:
        assert config.check_value("auto_poll_scope", value)[1] is None


# ---------------- 摆与回填 ----------------


def test_widgets_match_the_field_kinds(dialog):
    """复选框给 true/false，下拉框给几个选项，其余是填空的输入框。"""
    d = dialog()
    for field in settings.FIELDS:
        if field.kind == settings.BOOL:
            assert isinstance(d.checkboxes[field.key], QCheckBox)
            assert field.key not in d.editors
        elif field.kind == settings.ENUM:
            assert isinstance(d.editors[field.key], QComboBox)
        else:
            assert isinstance(d.editors[field.key], QLineEdit)


def test_values_are_filled_in(dialog):
    """窗口打开时照着当前生效值回填：勾选、数字、下拉都对准。"""
    d = dialog(
        auto_poll_enabled=True,
        poll_interval_seconds=0.5,
        auto_poll_scope="favorite",
        notify_enabled=True,
        notify_on_favorite_lowest=True,
    )

    assert d.checkboxes["auto_poll_enabled"].isChecked()
    assert d.checkboxes["notify_enabled"].isChecked()
    assert d.checkboxes["notify_on_favorite_lowest"].isChecked()
    assert not d.checkboxes["notify_on_favorite_drop"].isChecked()
    assert d.editors["poll_interval_seconds"].text() == "0.5"  # 不留 ".0" 的尾巴
    assert d.editors["auto_poll_scope"].currentData() == "favorite"


def test_every_row_has_a_question_badge_with_a_tip(dialog):
    """一行一个圈「？」，每个都挂着悬停提示。"""
    d = dialog()

    assert len(d.badges) == len(settings.FIELDS)
    for badge in d.badges:
        assert badge.text() == "?"
        assert badge.toolTip().strip()


def test_number_text_drops_the_trailing_zero():
    assert settings.number_text(2.0) == "2"
    assert settings.number_text(0.5) == "0.5"
    assert settings.number_text("") == ""
    assert settings.number_text(None) == ""


# ---------------- 收值 ----------------


def test_untouched_dialog_collects_the_same_values(dialog):
    """什么都没改：收上来的值跟递进去的一模一样（数字仍是浮点）。"""
    values = dict(config.DEFAULTS)
    values[settings.THEME_KEY] = False

    d = dialog()
    d.OnAccept()

    assert d.result() == d.Accepted
    assert d.Values() == values


def test_edits_are_collected(dialog):
    """改了的地方按控件的值收上来。"""
    d = dialog()
    d.editors["poll_interval_seconds"].setText(" 3.5 ")
    d.checkboxes["notify_on_any_target"].setChecked(True)
    d.editors["auto_poll_scope"].setCurrentIndex(2)

    d.OnAccept()

    values = d.Values()
    assert values["poll_interval_seconds"] == 3.5
    assert values["notify_on_any_target"] is True
    assert values["auto_poll_scope"] == "sold_out"


def test_cancel_leaves_no_values(dialog):
    """取消：一个值都不收（主窗口据此判断不写文件）。"""
    d = dialog()
    d.reject()

    assert d.Values() == {}


# ---------------- 校验 ----------------


def test_a_value_that_is_not_a_number_is_refused(dialog):
    """输入框里填的不是数字：当场拦下，窗口不关。"""
    d = dialog()
    d.editors["poll_interval_seconds"].setText("两秒")

    d.OnAccept()

    assert d.result() != d.Accepted
    assert "得填个数字" in d.error_label.text()
    assert d.error_label.isVisibleTo(d)


def test_an_out_of_range_value_is_refused_with_the_validator_reason(dialog):
    """范围、格式那类规则走 config 的校验器，窗口上显示的就是它给的原因。"""
    d = dialog()
    d.editors["auto_poll_interval_minutes"].setText("-5")

    d.OnAccept()

    assert d.result() != d.Accepted
    assert "间隔（分钟）：必须大于 0" in d.error_label.text()


def test_bad_values_do_not_leak_into_the_result(dialog):
    """一项不合格就整份不收：不能写出一份"一半新一半旧"的配置。"""
    d = dialog()
    d.editors["request_timeout_seconds"].setText("abc")
    d.checkboxes["auto_poll_enabled"].setChecked(True)

    d.OnAccept()

    assert d.Values() == {}


def test_the_error_line_says_which_tab_and_switches_to_it(dialog):
    """出错提示带页签名，并翻到那一页——三页并排时用户看不见错在哪儿。"""
    d = dialog()
    d.tabs.setCurrentIndex(0)
    d.editors["notify_min_interval_seconds"].setText("0")

    d.OnAccept()

    assert "企业微信推送" in d.error_label.text()
    assert d.tabs.currentIndex() == d.section_index["notify"]


def test_several_bad_values_are_listed_together(dialog):
    """错几项就列几项，别修一个报一个。"""
    d = dialog()
    d.editors["poll_interval_seconds"].setText("x")
    d.editors["notify_min_interval_seconds"].setText("0")

    d.OnAccept()

    text = d.error_label.text()
    assert "；" in text  # 几项之间分得开
    assert "抓取间隔（秒）" in text
    assert "两次推送最小间隔（秒）" in text


def test_fixing_the_value_lets_it_through(dialog):
    """改对了就能确定——出错提示是拦路，不是拦死。"""
    d = dialog()
    d.editors["poll_interval_seconds"].setText("0")
    d.OnAccept()
    assert d.result() != d.Accepted

    d.editors["poll_interval_seconds"].setText("2")
    d.OnAccept()

    assert d.result() == d.Accepted
    assert d.Values()["poll_interval_seconds"] == 2.0


def test_empty_webhook_is_allowed(dialog):
    """webhook 留空是「还没配」，不是错——推着总开关开着也照收。"""
    d = dialog(notify_enabled=True)
    d.editors["notify_wecom_webhook"].setText("")

    d.OnAccept()

    assert d.Values()["notify_wecom_webhook"] == ""


def test_a_webhook_that_is_not_wecom_is_refused(dialog):
    """地址不是企业微信的：这个必须拦住，写岔了就是把消息推给别人。"""
    d = dialog()
    d.editors["notify_wecom_webhook"].setText("https://example.test/hook?key=abc")

    d.OnAccept()

    assert d.result() != d.Accepted
    assert "不是企业微信机器人的地址" in d.error_label.text()


# ---------------- 主题与「改过哪些项」 ----------------


def test_theme_checkbox_previews_and_recolors(dialog):
    """勾上深色模式当场发信号（主窗口据此换主题），自己的自绘颜色也换掉。"""
    d = dialog()
    seen = []
    d.theme_previewed.connect(seen.append)
    before = d.badges[0].styleSheet()

    d.checkboxes[settings.THEME_KEY].setChecked(True)

    assert seen == [True]
    assert d.badges[0].styleSheet() != before  # 圈「？」跟着换色


def test_theme_is_carried_in_the_values(dialog):
    """主题也收进值里：主窗口要拿它决定记不记进设置。"""
    d = dialog()
    d.checkboxes[settings.THEME_KEY].setChecked(True)

    d.OnAccept()

    assert d.Values()[settings.THEME_KEY] is True


def test_changed_values_reports_only_edits():
    """只有改过的项才写回文件：没动过的项留着原来的行（含注释）。"""
    original = dict(config.DEFAULTS)
    original[settings.THEME_KEY] = False
    values = dict(original)
    values["poll_interval_seconds"] = 5.0
    values[settings.THEME_KEY] = True  # 主题不是配置项，永远不写回文件

    changed = settings.changed_values(values, original)

    assert changed == {"poll_interval_seconds": 5.0}


def test_changed_values_ignores_the_value_type_wobble():
    """输入框给的是浮点、配置里也是浮点：2 和 2.0 不该算改动。"""
    original = dict(config.DEFAULTS)
    values = dict(original)
    values["poll_interval_seconds"] = 2.0  # DEFAULTS 里就是 2.0

    assert settings.changed_values(values, original) == {}


def test_a_color_name_that_does_not_exist_is_refused(dialog):
    """形状像颜色但根本不存在（"深蓝"）：config 会放行、theme 会悄悄退回默认色，
    窗口里能当场问 QColor，就当场拦住，别让用户以为填了没生效。"""
    d = dialog()
    d.editors["deal_highlight_color"].setText("深蓝")

    d.OnAccept()

    assert d.result() != d.Accepted
    assert "认不出这个颜色" in d.error_label.text()


def test_a_real_color_name_goes_through(dialog):
    """认得出的颜色名照收（不要求非写 #rrggbb 不可）。"""
    d = dialog()
    d.editors["deal_highlight_color"].setText("orange")

    d.OnAccept()

    assert d.Values()["deal_highlight_color"] == "orange"
