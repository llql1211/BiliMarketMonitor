"""配置读取：优先 data/config.toml（用户自定义，不入库），无效/缺失时用
data/config.example.toml 兜底。

两份文件都在 data/ 目录；config.example.toml 随仓库分发，既是默认值也是格式参考。
注释用 TOML 原生的 #，解析时自动忽略，不需要额外的约定。

配置是用户手写的文件，什么值都可能出现：TOML 里能直接写 inf / nan，
间隔写成 inf 会让抓取线程卡死，写成 nan 会让间隔判断全部失效（高频打接口）。
所以每个值都单独校验，不合格的退回默认值并记一条 warning，不影响其他项。
"""

import math
import os
import re
import tomllib

CONFIG_FILENAME = "config.toml"
EXAMPLE_FILENAME = "config.example.toml"

# 代码内的兜底默认值（example 文件也缺失时生效）
DEFAULTS = {
    "detail_url_template": (
        "https://mall.bilibili.com/neul-next/resell/detail.html?clusterId={clusterId}"
    ),
    "poll_interval_seconds": 2.0,
    "retry_interval_seconds": 1.0,
    "request_timeout_seconds": 10.0,
    # 成交高亮：最近一次成交在多少小时内就加粗上色（0 = 关闭）
    "deal_highlight_within_hours": 24.0,
    "deal_highlight_color": "#e07000",
}


def data_dir() -> str:
    """数据文件目录（项目根目录下的 data/）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data")


def user_config_path() -> str:
    return os.path.join(data_dir(), CONFIG_FILENAME)


def example_config_path() -> str:
    return os.path.join(data_dir(), EXAMPLE_FILENAME)


def load_config():
    """返回 (config, warnings)。

    优先级：代码默认值 < config.example.toml < config.toml 中通过校验的键。
    """
    config = dict(DEFAULTS)
    warnings = []
    for path, label in ((example_config_path(), "example"), (user_config_path(), "用户")):
        data, error = _read_toml(path)
        if error:
            warnings.append(f"{os.path.basename(path)} 读取失败（{error}），已忽略")
            continue
        if data is None:
            continue  # 文件不存在，静默跳过
        _merge(config, data, label, warnings)

    if not os.path.exists(user_config_path()):
        warnings.append(
            f"未找到 {CONFIG_FILENAME}，当前使用 {EXAMPLE_FILENAME} 的默认配置"
        )
    return config, warnings


def _read_toml(path):
    """读 TOML，返回 (data, error)；文件不存在返回 (None, None)。"""
    try:
        with open(path, "rb") as f:  # tomllib 只接受二进制模式
            data = tomllib.load(f)
    except FileNotFoundError:
        return None, None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as err:
        # UnicodeDecodeError 不是 OSError：配置文件被存成 GBK 时它才出现，
        # 漏掉就是启动即崩，所以单独列出来
        return None, str(err)
    return data, None


def _merge(config, data, label, warnings):
    if not isinstance(data, dict):  # 解析结果不是表（正常 TOML 不会出现）
        warnings.append(f"{label}配置的内容不是一个 TOML 表，已忽略")
        return
    for key, value in data.items():
        if key not in DEFAULTS:
            warnings.append(f"{label}配置里的未知项 {key} 已忽略")
            continue
        checked = _validate(key, value, label, warnings)
        if checked is not None:
            config[key] = checked


def _check_url_template(value, key, label, warnings):
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{label}配置的 {key} 不是有效字符串，已忽略")
        return None
    value = value.strip()
    if "{clusterId}" not in value:
        warnings.append(f"{label}配置的 {key} 缺少 {{clusterId}} 占位符，已忽略")
        return None
    if not value.startswith(("http://", "https://")):
        # 模板不是链接的话，点「打开」会跳到乱七八糟的地方
        warnings.append(f"{label}配置的 {key} 不是 http(s) 链接，已忽略")
        return None
    return value


_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _check_color(value, key, label, warnings):
    """颜色只校验形状（#rrggbb 或颜色名）。

    能不能真当颜色用由 theme 层的 QColor 说了算——把 QColor 拉进来校验会把
    本模块（纯数据，不依赖 Qt）绑到 GUI 上，这里放行、那边兜底更划算。
    """
    if not isinstance(value, str) or not value.strip():
        warnings.append(f"{label}配置的 {key} 不是有效字符串，已忽略")
        return None
    value = value.strip()
    if _HEX_COLOR.match(value) or value.isalpha():
        return value
    warnings.append(f"{label}配置的 {key} 不是合法颜色（#rrggbb 或颜色名），已忽略")
    return None


def _check_positive_number(value, key, label, warnings):
    return _check_number(value, key, label, warnings, allow_zero=False)


def _check_non_negative_number(value, key, label, warnings):
    """允许 0：高亮阈值为 0 就是「关闭高亮」。"""
    return _check_number(value, key, label, warnings, allow_zero=True)


def _check_number(value, key, label, warnings, allow_zero):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"{label}配置的 {key} 不是数字，已忽略")
        return None
    if not math.isfinite(value):
        # TOML 允许写 inf / nan，直接放过会让间隔失效（卡死或高频请求）
        warnings.append(f"{label}配置的 {key} 必须是有限数字，已忽略")
        return None
    if value < 0 or (value == 0 and not allow_zero):
        bound = "不小于" if allow_zero else "大于"
        warnings.append(f"{label}配置的 {key} 必须{bound} 0，已忽略")
        return None
    return float(value)


# 每个配置项各自的校验规则（新增配置项必须在这里登记，漏登记会被 _validate 拒掉）
_VALIDATORS = {
    "detail_url_template": _check_url_template,
    "poll_interval_seconds": _check_positive_number,
    "retry_interval_seconds": _check_positive_number,
    "request_timeout_seconds": _check_positive_number,
    "deal_highlight_within_hours": _check_non_negative_number,
    "deal_highlight_color": _check_color,
}


def _validate(key, value, label, warnings):
    """按 key 分发到各自的校验函数。

    以前是按默认值的类型分支（str 就套 URL 模板规则），但那条规则是给
    详情页模板量身定做的——再加一个字符串项（高亮色）就会被它一律拒掉。
    """
    checker = _VALIDATORS.get(key)
    if checker is None:  # 有默认值却没登记校验：宁可拒绝，也不放没校验过的值进来
        warnings.append(f"{label}配置的 {key} 没有校验规则，已忽略")
        return None
    return checker(value, key, label, warnings)
