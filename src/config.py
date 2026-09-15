"""配置读取：优先 data/config.toml（用户自定义，不入库），无效/缺失时用
data/config.example.toml 兜底。

两份文件都在 data/ 目录；config.example.toml 随仓库分发，既是默认值也是格式参考。
注释用 TOML 原生的 #，解析时自动忽略，不需要额外的约定。
"""

import os
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
    except (OSError, tomllib.TOMLDecodeError) as err:
        return None, str(err)
    return data, None


def _merge(config, data, label, warnings):
    for key, value in data.items():
        if key not in DEFAULTS:
            warnings.append(f"{label}配置里的未知项 {key} 已忽略")
            continue
        checked = _validate(key, value, label, warnings)
        if checked is not None:
            config[key] = checked


def _validate(key, value, label, warnings):
    default = DEFAULTS[key]
    if isinstance(default, str):
        if not isinstance(value, str) or not value.strip():
            warnings.append(f"{label}配置的 {key} 不是有效字符串，已忽略")
            return None
        if "{clusterId}" not in value:
            warnings.append(f"{label}配置的 {key} 缺少 {{clusterId}} 占位符，已忽略")
            return None
        return value.strip()

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"{label}配置的 {key} 不是数字，已忽略")
        return None
    if value <= 0:
        warnings.append(f"{label}配置的 {key} 必须大于 0，已忽略")
        return None
    return float(value)
