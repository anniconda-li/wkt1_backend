"""Local visual profile matching regression tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.vision_service import VisualDescription
from services.visual_match_service import VisualMatchService


class VisualMatchServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = VisualMatchService()

    def test_yuying_matches_when_vision_calls_material_stone(self) -> None:
        desc = VisualDescription(
            category="石器",
            visual_description=(
                "图像显示一件雕刻的石质文物，整体呈对称的横向展开形态，"
                "类似一只张开翅膀的鸟或某种抽象动物形象。两侧为弧形翼状结构，"
                "表面刻有平行的凹槽纹饰，中间部分为身体和头部区域，较为凸起。"
                "表面光滑且有明显高光反射，颜色为浅米黄色至淡橙色。"
            ),
            shape_features=[
                "对称展开形态",
                "翼状结构",
                "中间凸起的身体和头部",
                "平行凹槽纹饰",
                "光滑表面",
            ],
            decoration_features=[
                "两侧翼部的平行凹槽纹饰",
                "中间头部区域的圆形凸起",
                "整体光滑无其他复杂装饰",
            ],
            color_material=["浅米黄色至淡橙色", "石材质地", "光滑且有光泽"],
            search_keywords=["石雕", "翼状文物", "平行纹饰", "对称雕刻", "鸟类造型", "古代石器"],
            is_clear=True,
            confidence=0.95,
        )

        match = self.service.match(desc)

        self.assertEqual(match.match_id, "yingguo_yuying")
        self.assertTrue(match.is_matched)
        self.assertIn("组合特征:应国玉鹰", match.evidence)

    def test_denggong_gui_matches_shape_description_without_name(self) -> None:
        desc = VisualDescription(
            category="青铜器",
            visual_description=(
                "图像展示了一件放置在白色展台上的古代青铜器，整体呈圆润的球形，"
                "顶部为半球形盖子，盖顶中央有一个方形凸起。器身由多层横向凸起的"
                "环带构成，两侧各有一个对称的环形耳状结构。"
            ),
            shape_features=[
                "球形主体",
                "半球形盖子",
                "方形盖顶凸起",
                "多层横向环带",
                "两侧对称环形耳",
                "底部略收窄的基座",
            ],
            decoration_features=["连续卷曲线条纹饰", "云雷纹或几何图案", "横向环带分割线", "盖顶方形凸起"],
            color_material=["青铜本色（暗银色）", "绿色铜锈", "金属光泽", "表面有反光"],
            search_keywords=["青铜器", "球形盖罐", "环带纹饰", "双耳", "铜锈", "古代工艺品"],
            is_clear=True,
            confidence=0.95,
        )

        match = self.service.match(desc)

        self.assertEqual(match.match_id, "denggong_gui")
        self.assertTrue(match.is_matched)
        self.assertIn("组合特征:邓公簋", match.evidence)


if __name__ == "__main__":
    unittest.main()
