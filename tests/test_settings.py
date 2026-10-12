"""设置窗口的测试：字段表齐全、控件回填与位置、收集与校验。

窗口只依赖 config 和 theme（不认识 MainWindow），所以这里直接造对话框就够，
不用起主窗口。写文件那一步在 test_config.py，接进主窗口那步在 test_app.py。
"""

import pytest
from PyQt5.QtCore import QPoint, QRect
from PyQt5.QtWidgets import QCheckBox, QLineEdit

import config
import settings
import theme


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
            # 用的得是自绘箭头那个（见 theme.ComboBox）：原生 QComboBox 在深色下
            # 右边那一格还是系统画的浅色按钮
            assert isinstance(d.editors[field.key], theme.ComboBox)
            # 登记进 combos，换主题时 SetDark 才轮得到它
            assert d.editors[field.key] in d.combos, "下拉框没登记，换主题时箭头不会跟着换色"
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


def _shown(dialog, qapp, section):
    """翻到某一页并让 Qt 真正摆一遍——没露过面的页签量不到位置。"""
    d = dialog()
    d.show()
    d.tabs.setCurrentIndex(d.section_index[section])
    qapp.processEvents()
    return d


def _rect(widget, parent):
    """控件在 parent 坐标系里的位置（几个控件分属不同的布局，得挪到一起比）。"""
    return QRect(widget.mapTo(parent, QPoint(0, 0)), widget.size())


def test_the_question_badge_sits_left_of_the_control(dialog, qapp):
    """圈「？」紧挨在控件的左边，并跟它在同一行。

    这是用户点名要的位置（原来在右边）。摆错一眼看得出来，所以钉在测试里，
    别哪天顺手又挪回去。
    """
    d = _shown(dialog, qapp, "basic")
    badges = {field.key: d.badges[i] for i, field in enumerate(settings.FIELDS)}
    rows = {
        "basic": [("detail_url_template", d.editors["detail_url_template"])],  # 输入框
        "auto": [("auto_poll_scope", d.editors["auto_poll_scope"])],           # 下拉框
        "notify": [("notify_on_favorite_drop",
                    d.checkboxes["notify_on_favorite_drop"])],                 # 复选框
    }

    for section, section_rows in rows.items():
        d.tabs.setCurrentIndex(d.section_index[section])
        qapp.processEvents()
        for key, control in section_rows:
            badge = _rect(badges[key], d)
            cell = _rect(control, d)
            assert badge.right() < cell.left(), f"{key} 的「？」不在控件左边"
            # 同一行：竖直中线上对得上（差一两个像素是行高不同导致的）
            assert abs(badge.center().y() - cell.center().y()) <= 2, (
                f"{key} 的「？」不在同一行"
            )
            assert badge.left() >= settings.LABEL_WIDTH - 4, f"{key} 的「？」压到标签上了"


def test_controls_of_a_page_line_up_in_one_column(dialog, qapp):
    """同一页里输入框、下拉框、复选框都从同一处起头。

    布尔项的标签本来就是复选框自己的文字，不是单独一格的 QLabel，所以只有
    它们容易跟别的行错开——这里正是把「对齐」钉住的。
    """
    d = _shown(dialog, qapp, "auto")
    pages = {
        "auto": [
            d.checkboxes["auto_poll_enabled"],
            d.editors["auto_poll_interval_minutes"],
            d.editors["auto_poll_scope"],
        ],
        "notify": [
            d.checkboxes["notify_enabled"],
            d.editors["notify_wecom_webhook"],
            d.editors["notify_min_interval_seconds"],
        ],
    }

    for section, row in pages.items():
        d.tabs.setCurrentIndex(d.section_index[section])
        qapp.processEvents()
        assert len({_rect(widget, d).left() for widget in row}) == 1


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


# ---------------- 测试 webhook 按钮 ----------------

# 编的地址（跟 tools/shot.py 里一个规矩）：真地址等于往群里发消息的权限，
# 测试文件里也不留
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"


def _press_test(d):
    """按下「测试 Webhook」，返回这一按交出去的地址（一个都没交就是空的）。

    信号连到列表上而不是连主窗口：这里测的是窗口这一侧——地址收得对不对、
    该不该往外发；真发消息那一段在 tests/test_app.py 里测（见 OnTestWebhook）。
    """
    sent = []
    d.test_webhook_requested.connect(sent.append)
    d.test_button.click()
    return sent


def test_the_webhook_row_has_a_test_button(dialog):
    """那颗按钮挂在 webhook 那一行上，挂着说明它会真的往群里发；结果那行先藏着。"""
    d = dialog()

    assert d.test_button.text() == settings.TEST_BUTTON_TEXT
    assert d.test_button.toolTip().strip()
    assert d.test_label.isHidden()  # 没按过就不占地方


def test_the_test_button_sends_what_is_typed_in(dialog):
    """按下就交给外层去发：地址去掉两头的空白，按钮先灰上（发着的时候别连点）。"""
    d = dialog()
    d.editors[settings.WEBHOOK_KEY].setText(f"  {WEBHOOK}  ")

    sent = _press_test(d)

    assert sent == [WEBHOOK]
    assert d.test_label.text() == "正在发送…"
    assert not d.test_label.isHidden()
    assert not d.test_button.isEnabled()


def test_the_test_button_refuses_an_address_that_is_not_wecom(dialog):
    """不是企业微信的地址：一个字节都不往外发，就地报在按钮底下。"""
    d = dialog()
    d.editors[settings.WEBHOOK_KEY].setText("https://example.test/hook?key=abc")

    sent = _press_test(d)

    assert sent == []
    assert "发不出去" in d.test_label.text()
    assert d.test_button.isEnabled()  # 拦下来不等于在发，按钮还能再按


def test_the_test_button_wants_an_address_first(dialog):
    """空着就按：得说一声，不能真拿空地址去发。

    「空串合法」是配置那边的规矩（表示还没配，见 config._check_webhook_url），
    按下测试按钮却是另一回事——空着按它没有意义。
    """
    d = dialog()
    d.editors[settings.WEBHOOK_KEY].setText("")

    sent = _press_test(d)

    assert sent == []
    assert "地址" in d.test_label.text()


def test_the_result_line_takes_the_result_colour(dialog):
    """成了绿、没成红、发送中是灰的：一行小字也得看得出结果。"""
    d = dialog()

    d.ShowWebhookTestResult("企业微信推送成功", ok=True)
    assert theme.success_color(False).name() in d.test_label.styleSheet()
    assert d.test_button.isEnabled()

    d.ShowWebhookTestResult("发不出去：网络请求失败", ok=False)
    assert theme.error_color(False).name() in d.test_label.styleSheet()

    d.ShowWebhookTestResult("正在发送…", ok=None)
    assert theme.muted_color(False).name() in d.test_label.styleSheet()


def test_the_result_line_follows_the_theme(dialog):
    """颜色是照主题现算的：深色窗口里的绿是深色那一档。

    结果回来时没人再递一遍主题（结果是按下去之后才到的），所以窗口得自己记着。
    """
    d = dialog()
    d.SetDark(True)

    d.ShowWebhookTestResult("企业微信推送成功", ok=True)

    assert theme.success_color(True).name() in d.test_label.styleSheet()
    assert theme.success_color(False).name() not in d.test_label.styleSheet()


def test_the_result_line_sits_under_the_webhook_box(dialog, qapp):
    """结果那行在输入框底下、左边跟它对齐，且不把窗口顶宽（长原因自己换行）。"""
    d = _shown(dialog, qapp, "notify")
    d.ShowWebhookTestResult(
        "发不出去：网络请求失败: HTTPSConnectionPool(host='qyapi.weixin.qq.com', "
        "port=443): Max retries exceeded with url: /cgi-bin/webhook/send?key=***",
        ok=False,
    )
    qapp.processEvents()

    box = _rect(d.editors[settings.WEBHOOK_KEY], d)
    line = _rect(d.test_label, d)
    button = _rect(d.test_button, d)

    assert line.top() >= box.bottom()               # 在输入框下面，不跟它挤一行
    assert abs(line.left() - box.left()) <= 1       # 左边跟输入框对齐
    assert line.right() <= d.width()                # 长原因留在窗口里
    assert button.left() >= box.right()             # 按钮在输入框右边，没盖住它


# ---------------- 主题与「改过哪些项」 ----------------


def test_toggling_the_theme_waits_for_ok(dialog):
    """勾「深色模式」不预览：窗口还是原来那身颜色，值等按确定才交出去。

    用户要的是「按下确定才生效」，所以勾一下不能把整个界面先变一半——
    取消也就真等于没打开过。
    """
    d = dialog()
    before = d.badges[0].styleSheet()

    d.checkboxes[settings.THEME_KEY].setChecked(True)

    assert d.badges[0].styleSheet() == before  # 圈「？」没跟着换色
    assert d.Values() == {}                    # 还没按确定，什么都还没交出去


def test_switching_the_theme_tells_the_combo_boxes(dialog):
    """下拉框的箭头是自绘的，得知道现在是深色还是浅色。

    漏了这步，深色下箭头还会按浅色那套走（浅色主题里这个控件压根不画箭头），
    框里就只剩一格空白。
    """
    d = dialog()
    d.SetDark(True)

    assert d.combos, "一个下拉框都没登记，这条测试就白跑了"
    for combo in d.combos:
        assert combo._dark is True

    d.SetDark(False)
    for combo in d.combos:
        assert combo._dark is False


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
