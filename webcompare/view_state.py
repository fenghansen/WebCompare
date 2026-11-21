"""【模块】显示序列 — 按来源归属切换整条图片序列的阅读顺序

功能说明：把已登记图片按来源优先或序号优先排列，供分页、分享与导出
共用；不修改来源、手调顺序或清单，也不读取本机和远端文件。

## 流程
所属流程：显示序列（webcompare/view_state.py）

1. **来源归属**：优先使用清单中的来源资产表，旧清单按字符串路径兼容 → 见函数 [source_lanes]
2. **显示排序**：在整条序列上切换拼接与交错，再交给布局正常分页 → 见函数 [display_images]
"""
from __future__ import annotations

import re


def _path_key(value: str) -> str:
    if not value or value.startswith("ssh://"):
        return value
    value = value.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", value):
        value = value.lower()
    return value


def source_lanes(manifest: dict) -> tuple[list[list[str | None]], list[str | None]]:
    """功能说明：按来源先后分配当前序列，另行保留无法归属的图片。

    调用时机：普通比较要求显示转置时，由 [display_images] 调用。

    关键步骤：
    1. 使用 source_images 首次命中归属，来源重叠只归最先一路，保留既有重复与空项；
    2. 缺少归属时按资产路径与来源路径的精确/目录前缀匹配，不访问文件系统；
    3. 遍历当前 images 保留各路相对顺序，未知归属项按原顺序留待末尾追加。

    输出/副作用：返回 (来源序列, 未知序列) 的新列表；不修改清单。
    """
    sources = manifest.get("sources", [])
    metadata = manifest.get("source_images")
    owners = {}
    paths = [_path_key(str(source.get("path") or "")) for source in sources]
    lanes = [[] for _ in sources]
    if isinstance(metadata, list) and len(metadata) == len(sources):
        for index, images in enumerate(metadata):
            if isinstance(images, list):
                for image in images:
                    if isinstance(image, str):
                        owners.setdefault(image, index)
    unknown = []
    assets = manifest.get("assets", {})
    for image in manifest.get("images", []):
        index = owners.get(image)
        if index is None and paths:
            path = _path_key(str(assets.get(image, {}).get("path") or ""))
            if path:
                index = next((index for index, source in enumerate(paths)
                    if source and (path == source or path.startswith(source.rstrip("/") + "/"))), None)
        if index is None:
            unknown.append(image)
        else:
            lanes[index].append(image)
    return lanes, unknown


def _ordered(manifest: dict, images: list[str | None], enabled: bool) -> list[str | None]:
    if not enabled:
        return list(images)

    def key(image):
        asset = manifest.get("assets", {}).get(image, {})
        name = str(asset.get("name") or asset.get("path") or image).lower()
        return tuple((1, len(part.lstrip("0") or "0"), part.lstrip("0") or "0")
            if part.isascii() and part.isdigit() else (0, part) for part in re.split(r"([0-9]+)", name))

    values = iter(sorted((image for image in images if image), key=key))
    return [next(values) if image else image for image in images]


def display_images(manifest: dict, state: dict) -> list[str | None]:
    """功能说明：计算普通比较用于显示的完整序列，布局与底层清单保持原样。

    调用时机：[page_images] 在按布局切页之前调用；分享、模板与导出复用该入口。

    关键步骤：
    1. 未转置时沿用已有自然排序或手调序列；分组清单不改变既有分组语义；
    2. 转置把 concat 改为逐位交错，把 interleave 改为按来源串联；
    3. 自然排序只在每路内部进行，耗尽来源跳过，未知归属项追加末尾。

    输出/副作用：返回完整显示列表；不切换布局，不写回任何清单或来源字段。
    """
    if manifest.get("groups"):
        return list(manifest.get("images", []))
    natural = bool(state.get("natural_sort"))
    if not state.get("transposed"):
        return _ordered(manifest, manifest.get("images", []), natural)
    lanes, unknown = source_lanes(manifest)
    lanes = [_ordered(manifest, lane, natural) for lane in lanes]
    if manifest.get("combine", "concat") == "concat":
        maximum = max((len(lane) for lane in lanes), default=0)
        images = [lane[index] for index in range(maximum) for lane in lanes if index < len(lane)]
    else:
        images = [image for lane in lanes for image in lane]
    return images + unknown
