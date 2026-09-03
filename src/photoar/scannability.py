"""入库可扫性星级：把自匹配分翻成 1~5 星。

自匹配分 `s`（`dedup.self_score`：20 张扰动查询图对自己的内点数中位）是入库时唯一
能预测"真机好不好扫"的量。门槛来自三处实测（见 decisions.md 与 verify.py）：
识别门槛 40、去重混淆门槛约 65、真实照片中位约 100。40~64 的照片是"擦线过闸"——
真机手持时内点系统性比合成低（同一对原图 21 → 查询时 33），这类照片多半扫不出。

只给一个整数星级不给分数：分数对管理员没有参照系，星级有。
"""
from __future__ import annotations

STAR_FLOORS: tuple[tuple[int, int], ...] = ((130, 5), (90, 4), (65, 3), (40, 2))


def stars(self_score: int) -> int:
    for floor, n in STAR_FLOORS:
        if self_score >= floor:
            return n
    return 1
