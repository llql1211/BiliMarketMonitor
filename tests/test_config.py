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
