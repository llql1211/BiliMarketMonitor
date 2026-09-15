"""链接文件读写：解析 txt -> [LinkEntry]，去重、容错。"""

import os
import re
from dataclasses import dataclass

# 从链接中解析 clusterId，如 "...&clusterId=10000008780&..."
_CLUSTER_ID_RE = re.compile(r"clusterId=(\d+)")


@dataclass
class LinkEntry:
    cluster_id: str  # 纯数字字符串
    raw: str         # 原始行（可能是完整链接，也可能就是纯数字 ID）


def parse_cluster_id(line: str):
    """从一行文本中解析出 clusterId，失败返回 None。

    兼容两种输入：
    1. 携带 clusterId=xxx 的分享/详情链接（忽略 share_medium、bbid 等无关参数）
    2. 用户直接粘贴的纯数字 ID
    """
    text = line.strip()
    if not text or text.startswith("#"):  # 空行 / 注释行
        return None
    match = _CLUSTER_ID_RE.search(text)
    if match:
        return match.group(1)
    if text.isdigit():
        return text
    return None


def load_links(path: str):
    """读取链接文件，返回去重后的 LinkEntry 列表（保持出现顺序）。"""
    entries = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            cluster_id = parse_cluster_id(line)
            if cluster_id is None or cluster_id in seen:
                continue
            seen.add(cluster_id)
            entries.append(LinkEntry(cluster_id, line.strip()))
    return entries


def build_detail_url(cluster_id: str) -> str:
    """由 clusterID 拼接可在浏览器打开的详情页地址。"""
    return (
        "https://mall.bilibili.com/neul-next/index.html"
        f"?page=magic-market_detail&noTitleBar=1&clusterId={cluster_id}&from=market_index"
    )


def get_detail_url(entry: LinkEntry) -> str:
    """得到可在浏览器打开的详情页链接。

    原始行本身是链接就直接用；纯数字 ID 则拼接详情页地址。
    """
    if entry.raw.startswith("http"):
        return entry.raw
    return build_detail_url(entry.cluster_id)


def default_watchlist_path() -> str:
    """默认的 watchlist.txt 路径（项目根目录，即 src/ 的上一级）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "watchlist.txt")
