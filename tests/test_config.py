"""config 的回归测试：文件优先级、逐项校验拒绝、语法错误兜底与类型归一。"""

import pytest

import config


def _write(data_dir, filename, content):
    (data_dir / filename).write_text(content, encoding="utf-8")


def _remove(data_dir, *filenames):
    for name in filenames:
        target = data_dir / name
        if target.exists():
            target.unlink()


# ---------------- 文件存在性组合 ----------------


def test_both_files_missing_uses_defaults(data_files):
    """两份配置都不存在时全部取代码默认值，并提示未找到 config.toml。"""
    _remove(data_files, "config.example.toml", "config.toml")
    cfg, warnings = config.load_config()
    assert cfg == config.DEFAULTS
    assert any("未找到" in w and "config.toml" in w for w in warnings)


def test_example_only_applies_example_values(data_files):
    """只有 example 时生效的是 example 的值，同时仍有未找到提示。"""
    _remove(data_files, "config.toml")
    cfg, warnings = config.load_config()
    assert cfg["poll_interval_seconds"] == 0.01
    assert cfg["detail_url_template"] == "https://example.test/detail?clusterId={clusterId}"
    assert any("未找到" in w for w in warnings)


def test_user_config_overrides_example(data_files):
    """两份都在时 config.toml 覆盖 example 的同名项。"""
    _write(data_files, "config.toml", 'poll_interval_seconds = 7\ndetail_url_template = "https://mine.test/p?clusterId={clusterId}"\n')
    cfg, _ = config.load_config()
    assert cfg["poll_interval_seconds"] == 7.0
    assert cfg["detail_url_template"] == "https://mine.test/p?clusterId={clusterId}"
    # example 里改过而 config.toml 没提的项保持 example 的值
    assert cfg["retry_interval_seconds"] == 0.01


# ---------------- 无效值逐项拒绝 ----------------


@pytest.mark.parametrize(
    "raw_value, expected_keyword",
    [
        ("0", "必须大于 0"),
        ("-1", "必须大于 0"),
        ('"2"', "不是数字"),
        ("true", "不是数字"),
    ],
)
def test_invalid_poll_interval_rejected(data_files, raw_value, expected_keyword):
    """poll_interval_seconds 的非法值被拒绝并退回默认值 2.0。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"poll_interval_seconds = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["poll_interval_seconds"] == config.DEFAULTS["poll_interval_seconds"]
    assert any(expected_keyword in w for w in warnings)


@pytest.mark.parametrize(
    "raw_value, expected_keyword",
    [
        ("5", "不是有效字符串"),
        ('""', "不是有效字符串"),
        ('"https://x.test/no-placeholder"', "缺少"),
    ],
)
def test_invalid_template_rejected(data_files, raw_value, expected_keyword):
    """detail_url_template 的非法值被拒绝并退回默认模板。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"detail_url_template = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["detail_url_template"] == config.DEFAULTS["detail_url_template"]
    assert any(expected_keyword in w for w in warnings)


# ---------------- TOML 语法错误 ----------------


def test_broken_user_config_ignored_example_still_applies(data_files):
    """config.toml 语法错误时整个文件被忽略，example 仍然生效。"""
    _write(data_files, "config.toml", "this is = not toml [\n")
    cfg, warnings = config.load_config()
    assert cfg["poll_interval_seconds"] == 0.01
    assert any("读取失败" in w and "config.toml" in w for w in warnings)


def test_broken_example_config_falls_back_to_defaults(data_files):
    """example 语法错误时退回代码默认值，且记一条读取失败警告。"""
    (data_files / "config.example.toml").write_text("= broken [\n", encoding="utf-8")
    cfg, warnings = config.load_config()
    assert cfg == config.DEFAULTS
    assert any("读取失败" in w for w in warnings)


# ---------------- 未知项 / 类型归一 / strip ----------------


def test_unknown_key_warned(data_files):
    """配置里的未知键被忽略，警告里出现「未知项」和键名。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", "mystery_key = 1\n")
    cfg, warnings = config.load_config()
    assert "mystery_key" not in cfg
    assert any("未知项" in w and "mystery_key" in w for w in warnings)


def test_integer_converted_to_float(data_files):
    """合法的整数间隔会被归一化成 float。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", "poll_interval_seconds = 5\n")
    cfg, _ = config.load_config()
    assert cfg["poll_interval_seconds"] == 5.0
    assert isinstance(cfg["poll_interval_seconds"], float)


# ---------------- inf / nan 与非链接模板 ----------------


@pytest.mark.parametrize("raw_value", ["inf", "-inf", "nan"])
@pytest.mark.parametrize(
    "key",
    ["poll_interval_seconds", "retry_interval_seconds", "request_timeout_seconds"],
)
def test_non_finite_number_rejected(data_files, key, raw_value):
    """TOML 能直接写 inf/nan：间隔是 inf 会让抓取线程卡死，nan 会让间隔判断全部失效。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"{key} = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg[key] == config.DEFAULTS[key]
    assert any("有限数字" in w and key in w for w in warnings)


@pytest.mark.parametrize("raw_value", ["1e400", "-1e400"])
def test_overflow_literal_rejected(data_files, raw_value):
    """大到溢出的字面量（TOML 里按 inf 解析）同样拒绝。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"poll_interval_seconds = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["poll_interval_seconds"] == config.DEFAULTS["poll_interval_seconds"]
    assert any("有限数字" in w for w in warnings)


@pytest.mark.parametrize(
    "template",
    [
        "file:///C:/x.html?clusterId={clusterId}",
        "C:/x.html?clusterId={clusterId}",
        "not-a-url?clusterId={clusterId}",
    ],
)
def test_non_http_template_rejected(data_files, template):
    """模板不是 http(s) 链接时拒绝：点「打开」不能跳到文件系统或其他协议上。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f'detail_url_template = "{template}"\n')
    cfg, warnings = config.load_config()
    assert cfg["detail_url_template"] == config.DEFAULTS["detail_url_template"]
    assert any("http" in w for w in warnings)


def test_http_plain_template_accepted(data_files):
    """http://（非 https）也算合法链接。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", 'detail_url_template = "http://x.test/p?clusterId={clusterId}"\n')
    cfg, _ = config.load_config()
    assert cfg["detail_url_template"] == "http://x.test/p?clusterId={clusterId}"


# ---------------- 编码坏了也不能崩 ----------------


def test_gbk_config_ignored_not_crashed(data_files):
    """config.toml 被存成 GBK 时：UnicodeDecodeError 不是 OSError，必须自己兜住。"""
    (data_files / "config.toml").write_bytes(
        '# 中文注释\npoll_interval_seconds = 5\n'.encode("gbk")
    )
    cfg, warnings = config.load_config()
    assert cfg["poll_interval_seconds"] == 0.01  # example 仍然生效
    assert any("读取失败" in w and "config.toml" in w for w in warnings)


def test_merge_ignores_non_dict_document():
    """_merge 收到非表的内容时记一条警告就返回，不抛 AttributeError。"""
    cfg = dict(config.DEFAULTS)
    warnings = []
    config._merge(cfg, ["poll_interval_seconds"], "用户", warnings)
    assert cfg == config.DEFAULTS
    assert warnings and "TOML 表" in warnings[0]


def test_template_value_is_stripped(data_files):
    """字符串模板两端的空白被 strip 掉。"""
    _remove(data_files, "config.example.toml")
    _write(
        data_files,
        "config.toml",
        'detail_url_template = "  https://x.test/p?clusterId={clusterId}  "\n',
    )
    cfg, _ = config.load_config()
    assert cfg["detail_url_template"] == "https://x.test/p?clusterId={clusterId}"


# ---------------- 校验规则登记 ----------------
#
# 校验以前是按默认值类型分支的（str 就套 URL 模板规则），加了第二个字符串项
# 就会被那条规则一律拒掉，所以改成按 key 显式登记。下面这条守住「别漏登记」。


def test_every_default_has_a_validator():
    """DEFAULTS 与 _VALIDATORS 必须一一对应，两个方向漏都会出问题。"""
    assert set(config._VALIDATORS) == set(config.DEFAULTS)


# ---------------- 成交新鲜度高亮 ----------------
#
# 阈值 0 是「关闭高亮」，所以这一项允许 0；其余数值项照旧必须大于 0。


def test_highlight_defaults(data_files):
    """两份配置都没写时高亮取代码默认值：24 小时、橙色。"""
    _remove(data_files, "config.example.toml", "config.toml")
    cfg, _ = config.load_config()
    assert cfg["deal_highlight_within_hours"] == 24.0
    assert cfg["deal_highlight_color"] == "#e07000"


@pytest.mark.parametrize("raw_value, expected", [("6", 6.0), ("0.5", 0.5), ("0", 0.0)])
def test_highlight_hours_accepted(data_files, raw_value, expected):
    """合法的阈值被接受并归一成 float；0 是合法的（关闭高亮）。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"deal_highlight_within_hours = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["deal_highlight_within_hours"] == expected
    assert not any("deal_highlight_within_hours" in w for w in warnings)


@pytest.mark.parametrize(
    "raw_value, expected_keyword",
    [
        ("-1", "必须不小于 0"),
        ('"6"', "不是数字"),
        ("true", "不是数字"),
        ("inf", "有限数字"),
        ("nan", "有限数字"),
    ],
)
def test_highlight_hours_rejected(data_files, raw_value, expected_keyword):
    """非法阈值退回默认 24 小时并记一条警告。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"deal_highlight_within_hours = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["deal_highlight_within_hours"] == config.DEFAULTS["deal_highlight_within_hours"]
    assert any(expected_keyword in w and "deal_highlight_within_hours" in w for w in warnings)


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ('"#e07000"', "#e07000"),
        ('"#abc"', "#abc"),          # 三位简写
        ('"#ff8c00ff"', "#ff8c00ff"),  # 带 alpha
        ('"orange"', "orange"),      # 颜色名
        ('"  orange  "', "orange"),  # 两端空白先剥掉
    ],
)
def test_highlight_color_accepted(data_files, raw_value, expected):
    """颜色只校验形状：#rrggbb 系和纯字母的颜色名都放行。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"deal_highlight_color = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["deal_highlight_color"] == expected
    assert not any("deal_highlight_color" in w for w in warnings)


@pytest.mark.parametrize(
    "raw_value, expected_keyword",
    [
        ("5", "不是有效字符串"),
        ('""', "不是有效字符串"),
        ('"   "', "不是有效字符串"),
        ('"#12345"', "不是合法颜色"),      # 位数不对
        ('"orange juice"', "不是合法颜色"),  # 带空格的颜色名
        ('"rgb(1,2,3)"', "不是合法颜色"),
        ('"#gggggg"', "不是合法颜色"),       # 非十六进制字符
    ],
)
def test_highlight_color_rejected(data_files, raw_value, expected_keyword):
    """认不出来的颜色退回默认色并记一条警告。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"deal_highlight_color = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["deal_highlight_color"] == config.DEFAULTS["deal_highlight_color"]
    assert any(expected_keyword in w and "deal_highlight_color" in w for w in warnings)


def test_unknown_key_still_rejected_with_registry(data_files):
    """改成登记制之后，未登记的键照旧被拒——别把校验放宽成「什么都放行」。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", "mystery_key = 1\n")
    cfg, warnings = config.load_config()
    assert "mystery_key" not in cfg
    assert any("未知项" in w for w in warnings)


# ---------------- 自动抓取 ----------------
#
# 默认必须是关着的：升级到这一版的人没做任何配置时，程序不该自己开始反复打接口。


def test_auto_poll_defaults_are_off(data_files):
    """两份配置都没写时的默认值：不自动抓、30 分钟、全部、不弹总结。"""
    _remove(data_files, "config.example.toml", "config.toml")
    cfg, _ = config.load_config()
    assert cfg["auto_poll_enabled"] is False
    assert cfg["auto_poll_interval_minutes"] == 30.0
    assert cfg["auto_poll_scope"] == "all"
    assert cfg["auto_poll_show_summary"] is False


def test_auto_poll_user_config_overrides_example(data_files):
    """四项都能被 config.toml 覆盖，且压过 example 里的值。"""
    _write(data_files, "config.example.toml", (
        "auto_poll_enabled = false\n"
        "auto_poll_interval_minutes = 60\n"
        'auto_poll_scope = "all"\n'
        "auto_poll_show_summary = false\n"
    ))
    _write(data_files, "config.toml", (
        "auto_poll_enabled = true\n"
        "auto_poll_interval_minutes = 5\n"
        'auto_poll_scope = "favorite"\n'
        "auto_poll_show_summary = true\n"
    ))
    cfg, warnings = config.load_config()
    assert cfg["auto_poll_enabled"] is True
    assert cfg["auto_poll_interval_minutes"] == 5.0
    assert cfg["auto_poll_scope"] == "favorite"
    assert cfg["auto_poll_show_summary"] is True
    assert not [w for w in warnings if "auto_poll" in w]


@pytest.mark.parametrize("raw_value, expected", [
    ('"all"', "all"),
    ('"favorite"', "favorite"),
    ('"sold_out"', "sold_out"),
    ('"  favorite  "', "favorite"),  # 顺手去掉前后空格，省得为这点空白报一条警告
])
def test_auto_poll_scope_accepted(data_files, raw_value, expected):
    """三个合法范围都收，前后带空格的写法也认。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"auto_poll_scope = {raw_value}\n")
    cfg, _ = config.load_config()
    assert cfg["auto_poll_scope"] == expected


@pytest.mark.parametrize(
    "raw_value",
    ['"favarite"',   # 拼错
     '"FAVORITE"',   # 大小写不对：写错就得说一声，不能悄悄当成另一回事
     '"全部"',
     '""',
     '"all,favorite"',
     "1",
     "true"],
)
def test_auto_poll_scope_falls_back_to_default(data_files, raw_value):
    """认不出的范围退回 all 并记警告。

    这里宁可退回默认值也不照字面用：写 `"favarite"` 的本意是「只抓收藏」，
    退回"全部"只是多抓几件，而按字面走会一件都不抓（谁也不等于这个字符串）。
    """
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"auto_poll_scope = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["auto_poll_scope"] == "all"
    assert any("auto_poll_scope" in w for w in warnings)


@pytest.mark.parametrize(
    "raw_value, expected_keyword",
    [
        ("0", "必须大于 0"),
        ("-5", "必须大于 0"),
        ("inf", "必须是有限数字"),
        ("nan", "必须是有限数字"),
        ('"30"', "不是数字"),
        ("true", "不是数字"),
    ],
)
def test_auto_poll_interval_rejected(data_files, raw_value, expected_keyword):
    """间隔的非法值退回 30 分钟：0 或 nan 会让定时器空转，把接口打爆。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"auto_poll_interval_minutes = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["auto_poll_interval_minutes"] == config.DEFAULTS["auto_poll_interval_minutes"]
    assert any(expected_keyword in w and "auto_poll_interval_minutes" in w for w in warnings)


@pytest.mark.parametrize(
    "key", ["auto_poll_enabled", "auto_poll_show_summary"]
)
@pytest.mark.parametrize("raw_value", ["1", "0", '"true"', '"false"', '"yes"'])
def test_auto_poll_bool_rejected(data_files, key, raw_value):
    """只认真布尔。

    `"false"` 这种写法必须拒掉：非空字符串在 Python 里是真值，照它走会把
    「关掉自动抓取」读成「打开」——正好和用户的本意相反。
    """
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"{key} = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg[key] is False
    assert any(key in w and "true / false" in w for w in warnings)


@pytest.mark.parametrize("key", ["auto_poll_enabled", "auto_poll_show_summary"])
@pytest.mark.parametrize("raw_value, expected", [("true", True), ("false", False)])
def test_auto_poll_bool_accepted(data_files, key, raw_value, expected):
    """真布尔照收。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"{key} = {raw_value}\n")
    cfg, _ = config.load_config()
    assert cfg[key] is expected


# ---------------- 企业微信推送 ----------------
#
# 默认推不出去：没配 webhook 就不该有任何一条消息发出去。webhook 是敏感信息，
# 这里的用例都拿假地址，别把真的写进仓库。

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc-123"

NOTIFY_BOOL_KEYS = [
    "notify_on_restock",
    "notify_on_favorite_target",
    "notify_on_favorite_lowest",
    "notify_on_favorite_drop",
    "notify_on_any_target",
]


def test_notify_defaults(data_files):
    """两份配置都没写时的默认值：推送关着、没地址、限流 60 秒。"""
    _remove(data_files, "config.example.toml", "config.toml")
    cfg, _ = config.load_config()
    assert cfg["notify_enabled"] is False
    assert cfg["notify_wecom_webhook"] == ""
    assert cfg["notify_min_interval_seconds"] == 60.0
    # 补货和收藏到价默认开着（这两件事是要盯的），其余三种默认关掉
    assert cfg["notify_on_restock"] is True
    assert cfg["notify_on_favorite_target"] is True
    assert cfg["notify_on_favorite_lowest"] is False
    assert cfg["notify_on_favorite_drop"] is False
    assert cfg["notify_on_any_target"] is False


def test_notify_user_config_overrides_example(data_files):
    """八项都能被 config.toml 覆盖，且压过 example 里的值。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", (
        "notify_enabled = true\n"
        f'notify_wecom_webhook = "{WEBHOOK}"\n'
        "notify_min_interval_seconds = 300\n"
        "notify_on_restock = false\n"
        "notify_on_favorite_target = false\n"
        "notify_on_favorite_lowest = true\n"
        "notify_on_favorite_drop = true\n"
        "notify_on_any_target = true\n"
    ))
    cfg, warnings = config.load_config()
    assert cfg["notify_enabled"] is True
    assert cfg["notify_wecom_webhook"] == WEBHOOK
    assert cfg["notify_min_interval_seconds"] == 300.0
    assert cfg["notify_on_restock"] is False
    assert cfg["notify_on_favorite_target"] is False
    assert cfg["notify_on_favorite_lowest"] is True
    assert cfg["notify_on_favorite_drop"] is True
    assert cfg["notify_on_any_target"] is True
    assert not [w for w in warnings if "notify" in w]


def test_notify_webhook_empty_is_allowed(data_files):
    """空串放行：表示「推送开着但地址还没填」，不能当错误拒掉。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", 'notify_wecom_webhook = ""\n')
    cfg, warnings = config.load_config()
    assert cfg["notify_wecom_webhook"] == ""
    assert not [w for w in warnings if "notify_wecom_webhook" in w]


def test_notify_webhook_is_stripped(data_files):
    """地址两头的手写空格自己剪掉。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f'notify_wecom_webhook = "  {WEBHOOK}  "\n')
    cfg, _ = config.load_config()
    assert cfg["notify_wecom_webhook"] == WEBHOOK


@pytest.mark.parametrize(
    "raw_value",
    [
        "123",                                       # 不是字符串
        "true",
        '"http://qyapi.weixin.qq.com/hook?key=x"',    # 不是 https
        '"https://example.com/hook?key=x"',           # 别的域名
        '"https://qyapi.weixin.qq.com.evil.test/h"',  # 前缀看着像，域名不是
        '"qyapi.weixin.qq.com/cgi-bin/webhook"',      # 漏了协议头
    ],
)
def test_notify_webhook_rejected(data_files, raw_value):
    """只认企业微信机器人的地址：推给别人的群比不推糟糕得多。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"notify_wecom_webhook = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["notify_wecom_webhook"] == ""
    assert any("notify_wecom_webhook" in w for w in warnings)


@pytest.mark.parametrize(
    "raw_value, expected_keyword",
    [
        ("0", "必须大于 0"),
        ("-5", "必须大于 0"),
        ("inf", "必须是有限数字"),
        ("nan", "必须是有限数字"),
        ('"60"', "不是数字"),
        ("true", "不是数字"),
    ],
)
def test_notify_min_interval_rejected(data_files, raw_value, expected_keyword):
    """限流间隔的非法值退回 60 秒：0 或 nan 等于没有限流，一轮能轰出好多条。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"notify_min_interval_seconds = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg["notify_min_interval_seconds"] == 60.0
    assert any(
        expected_keyword in w and "notify_min_interval_seconds" in w for w in warnings
    )


@pytest.mark.parametrize("key", NOTIFY_BOOL_KEYS)
@pytest.mark.parametrize("raw_value", ["1", "0", '"true"', '"false"', '"yes"'])
def test_notify_bool_rejected(data_files, key, raw_value):
    """五个触发开关跟其他布尔项一个待遇：只认真布尔。

    `"false"` 必须拒掉——非空字符串是真值，照它走会把「别推这个」读成「推」。
    """
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"{key} = {raw_value}\n")
    cfg, warnings = config.load_config()
    assert cfg[key] is config.DEFAULTS[key]  # 退回默认值（默认值本身可能是 true）
    assert any(key in w and "true / false" in w for w in warnings)


@pytest.mark.parametrize("key", NOTIFY_BOOL_KEYS)
@pytest.mark.parametrize("raw_value, expected", [("true", True), ("false", False)])
def test_notify_bool_accepted(data_files, key, raw_value, expected):
    """真布尔照收：五个开关各自独立，改一个不影响别的。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", f"{key} = {raw_value}\n")
    cfg, _ = config.load_config()
    assert cfg[key] is expected


@pytest.mark.parametrize(
    "enabled, webhook, expected",
    [
        ("true", '""', True),      # 开了推送却没地址：这条最该提醒
        ("true", f'"{WEBHOOK}"', False),
        ("false", '""', False),    # 没开推送、没填地址，本来就该是常态
        ("false", f'"{WEBHOOK}"', False),
    ],
)
def test_notify_enabled_without_webhook_warns(data_files, enabled, webhook, expected):
    """「开了推送但没填地址」要单独提醒一句。

    逐键校验够不着这个状态（单看哪一项都合法），合起来才是「一条也发不出去」。
    提醒归提醒，配置照收——用户可能正打算回头填。
    """
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", (
        f"notify_enabled = {enabled}\n"
        f"notify_wecom_webhook = {webhook}\n"
    ))
    cfg, warnings = config.load_config()
    assert any("notify_wecom_webhook" in w for w in warnings) is expected
    assert cfg["notify_enabled"] is (enabled == "true")


# ---------------- 写回配置（设置窗口用） ----------------


def _read(data_dir, filename="config.toml"):
    return (data_dir / filename).read_text(encoding="utf-8")


def test_save_config_replaces_only_the_given_keys(data_files):
    """只动传进来的项：别的键、注释、用户自己排的顺序一个字都不改。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", (
        "# 我自己写的注释，别冲掉\n"
        "poll_interval_seconds = 2\n"
        'auto_poll_scope = "favorite"\n'
    ))

    written = config.save_config({"poll_interval_seconds": 5.0})

    assert written == ["poll_interval_seconds"]
    text = _read(data_files)
    assert "# 我自己写的注释，别冲掉" in text
    assert "poll_interval_seconds = 5.0" in text
    assert 'auto_poll_scope = "favorite"' in text
    assert "poll_interval_seconds = 2" not in text


def test_save_config_appends_keys_the_file_lacks(data_files):
    """文件里没有的项补在末尾，补上去的照样能被读回来。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", "poll_interval_seconds = 2\n")

    written = config.save_config({"notify_enabled": True, "auto_poll_scope": "sold_out"})

    # 按 DEFAULTS 的顺序写，不是字典的插入顺序
    assert written == ["auto_poll_scope", "notify_enabled"]
    text = _read(data_files)
    assert text.startswith("poll_interval_seconds = 2\n\n")  # 原有内容在最前，补的在后
    cfg, _ = config.load_config()
    assert cfg["notify_enabled"] is True
    assert cfg["auto_poll_scope"] == "sold_out"
    assert cfg["poll_interval_seconds"] == 2.0


def test_save_config_leaves_commented_lines_alone(data_files):
    """被注释掉的同名行不算数：那是用户自己留的记录，另起一行写新值。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", "# poll_interval_seconds = 2\n")

    config.save_config({"poll_interval_seconds": 5.0})

    assert _read(data_files) == "# poll_interval_seconds = 2\n\npoll_interval_seconds = 5.0\n"


def test_save_config_builds_on_the_example_file(data_files):
    """没有 config.toml 时拿 example 当底稿：它的注释就是说明书。"""
    _remove(data_files, "config.toml")
    _write(data_files, "config.example.toml", (
        "# 抓取间隔，秒\n"
        "poll_interval_seconds = 2\n"
    ))

    config.save_config({"poll_interval_seconds": 5.0})

    text = _read(data_files)
    assert "# 抓取间隔，秒" in text
    assert "poll_interval_seconds = 5.0" in text


def test_save_config_works_without_any_base_file(data_files):
    """两份文件都没有也能写：从零起一份。"""
    _remove(data_files, "config.example.toml", "config.toml")

    written = config.save_config({"auto_poll_enabled": True})

    assert written == ["auto_poll_enabled"]
    assert _read(data_files) == "auto_poll_enabled = true\n"


def test_save_config_keeps_one_backup(data_files):
    """写前留一份 .bak，只留第一次那份；之后再写不改它。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", "poll_interval_seconds = 2\n")

    config.save_config({"poll_interval_seconds": 5.0})
    assert _read(data_files, "config.toml.bak") == "poll_interval_seconds = 2\n"

    config.save_config({"poll_interval_seconds": 7.0})
    assert _read(data_files, "config.toml.bak") == "poll_interval_seconds = 2\n"
    assert "poll_interval_seconds = 7.0" in _read(data_files)


def test_save_config_quotes_awkward_strings(data_files):
    """引号、反斜杠、换行都得处理掉：写进去的必须还是一份能读的 TOML。"""
    _remove(data_files, "config.example.toml", "config.toml")
    value = 'https://x.test/p?q="a"\\b&clusterId={clusterId}\nnotify_enabled = true'

    config.save_config({"detail_url_template": value})

    cfg, warnings = config.load_config()
    assert warnings == []
    # 换行被去掉，其余原样；关键是没多写出一行配置来
    assert "\n" not in cfg["detail_url_template"]
    assert cfg["notify_enabled"] is False  # 没被那行假配置改掉
    assert "\\" in cfg["detail_url_template"] and '"a"' in cfg["detail_url_template"]


@pytest.mark.parametrize(
    "key, value",
    [
        ("poll_interval_seconds", 3.5),
        ("auto_poll_interval_minutes", 15),
        ("auto_poll_scope", "sold_out"),
        ("notify_enabled", False),
        ("notify_min_interval_seconds", 60),
        ("deal_highlight_color", "#123456"),
    ],
)
def test_save_config_round_trips(data_files, key, value):
    """写下去再读回来是同一个值，且不惊动校验（没有警告）。"""
    _remove(data_files, "config.example.toml")

    config.save_config({key: value})

    cfg, warnings = config.load_config()
    assert cfg[key] == value
    assert warnings == []  # config.toml 在，也没有被忽略的项


def test_save_config_ignores_unknown_keys(data_files):
    """不认识的键直接跳过：写进去也是下次启动被警告一遍。"""
    _remove(data_files, "config.example.toml")

    assert config.save_config({"nope": 1}) == []
    assert not (data_files / "config.toml").exists()


def test_save_config_with_no_changes_does_not_touch_the_file(data_files):
    """没什么可写的就不落盘：连 .bak 都不该冒出来。"""
    _remove(data_files, "config.example.toml")
    _write(data_files, "config.toml", "poll_interval_seconds = 2")

    assert config.save_config({}) == []
    assert _read(data_files) == "poll_interval_seconds = 2"
    assert not (data_files / "config.toml.bak").exists()
