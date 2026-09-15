"""监视清单读写：解析 watchlist.txt -> [LinkEntry]，并负责格式化写回。

清单由程序格式化维护，每行形如：

    <clusterId> | <商品名>

商品名未知时只写 clusterId。为了兼容首次导入和手工编辑，
读入时同时接受分享链接（含 share_medium、bbid 等无关参数）和纯数字 ID。
"""

import os
import re
import shutil
from dataclasses import dataclass

# 从链接中解析 clusterId，如 "...&clusterId=10000008780&..."
_CLUSTER_ID_RE = re.compile(r"clusterId=(\d+)")
# 纯数字 ID（可带 "| 名称" 后缀）
_PLAIN_ID_RE = re.compile(r"^(\d+)\s*(?:\|\s*(.*))?$")

_HEADER = (
    "# 监视清单：每行格式 <clusterId> | <商品名>，名称未知时只写 clusterId",
    "# 由程序维护，手工修改后点界面上的「刷新商品列表」同步",
)


@dataclass
class LinkEntry:
    cluster_id: str
    name: str = ""  # 清单里记录的商品名，可能为空（尚未抓取过）
    raw: str = ""   # 原始行，仅用于排查问题


def parse_line(line: str):
    """解析一行，返回 (clusterId, name)；无法识别返回 (None, None)。"""
    text = line.strip()
    if not text or text.startswith("#"):  # 空行 / 注释行
        return None, None

    match = _CLUSTER_ID_RE.search(text)  # 分享链接
    if match:
        name = ""
        if "|" in text:
            name = text.split("|", 1)[1].strip()
        return match.group(1), name

    match = _PLAIN_ID_RE.match(text)  # 纯数字 ID，可带 "| 名称"
    if match:
        return match.group(1), (match.group(2) or "").strip()

    return None, None


def load_links(path: str):
    """读取清单，返回去重后的 LinkEntry 列表（保持文件里的顺序）。

    同名条目以第一次出现的为准；文件里已有的名字会被保留。
    """
    entries = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            cluster_id, name = parse_line(line)
            if cluster_id is None or cluster_id in seen:
                continue
            seen.add(cluster_id)
            entries.append(LinkEntry(cluster_id, name, line.strip()))
    return entries


def save_watchlist(path: str, items):
    """按规范格式原子写回清单。

    items 为 [(clusterId, 商品名)]，顺序即写入顺序；名称沿用文件里已有的值。
    写前留一份 path + ".bak" 备份（只留第一次的），并用临时文件 + 替换避免写坏。
    """
    lines = list(_HEADER) + [""]
    for cluster_id, name in items:
        lines.append(f"{cluster_id} | {name}" if name else str(cluster_id))
    content = "\n".join(lines) + "\n"

    if os.path.exists(path) and not os.path.exists(path + ".bak"):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass  # 备份失败不影响主流程

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp_path, path)


def build_detail_url(cluster_id: str, template: str) -> str:
    """按配置模板拼出详情页链接。"""
    return template.replace("{clusterId}", str(cluster_id))


def default_watchlist_path() -> str:
    """默认的 watchlist.txt 路径（data/ 目录，即 src/ 的上一级下的 data/）。"""
    return os.path.join(_data_dir(), "watchlist.txt")


def _data_dir() -> str:
    """数据文件目录（项目根目录下的 data/）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "data")
