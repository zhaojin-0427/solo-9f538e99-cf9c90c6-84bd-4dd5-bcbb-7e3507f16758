# -*- coding: utf-8 -*-
"""Flask 路由集成测试（无 Flask 环境时自动跳过；容器内会完整执行）。"""
import json
import unittest

try:
    from app.app import create_app

    FLASK_AVAILABLE = True
except Exception:  # pragma: no cover - 本地最小环境
    FLASK_AVAILABLE = False


CONFIG = {
    "items": ["S1", "S2", "S3"],
    "labels": {"S1": "头盔", "S2": "车灯", "S3": "超30分钟"},
    "followups": [
        {"qid": "Q1", "condition": "S1 AND NOT S2", "shown": True},
        {"qid": "Q2", "condition": "S3 OR S1", "shown": True},
    ],
    "inclusion": "S1 AND (S2 OR S3)",
}

# 与配置相容的答卷：Q1 展示 => S1=1,S2=0；Q2 展示由 S1=1 满足
ARCHIVE = {
    "answers": {"S1": True},
    "followups": [
        {"qid": "Q1", "shown": True},
        {"qid": "Q2", "shown": True},
    ],
}


@unittest.skipUnless(FLASK_AVAILABLE, "Flask 未安装")
class WebFlowTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()

    def _setup(self, payload):
        return self.client.post(
            "/setup", data={"config_text": json.dumps(payload, ensure_ascii=False)}
        )

    def test_full_flow_uncertain_to_include(self):
        resp = self._setup({"config": CONFIG, "archive": ARCHIVE})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("结论有分歧", page)
        self.assertIn("从当前起最坏还要问几题", page)
        # 推荐 S3（S2 被跳题约束隐含为假）
        self.assertIn("S3", page)

        # 补问 S3=是 => 纳入
        resp = self.client.post(
            "/answer", data={"item": "S3", "value": "true"}
        )
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("纳入新版夜骑分析", page)

    def test_conflicting_answer_is_rejected_and_state_kept(self):
        self._setup({"config": CONFIG, "archive": ARCHIVE})
        before = self.client.get("/").get_data(as_text=True)
        self.assertIn("结论有分歧", before)

        # S2 被 Q1 展示约束隐含为假，答“是”必然冲突
        resp = self.client.post(
            "/answer", data={"item": "S2", "value": "true"}
        )
        self.assertEqual(resp.status_code, 302)
        after = self.client.get("/").get_data(as_text=True)
        self.assertIn("与旧档记录冲突", after)
        self.assertIn("上次提交因冲突被拒绝", after)
        # 提交前结果保留：仍是分歧状态；S2 仍为被隐含的未答项，未写入答案
        self.assertIn("结论有分歧", after)
        self.assertIn("上次提交因冲突被拒绝", after)
        # S1 是唯一已确认行；S2 行仍显示“隐含”而非已确认
        self.assertEqual(after.count("已确认"), 1)
        s2_row = after.split('<td><code>S2</code></td>', 1)[1].split("</tr>", 1)[0]
        self.assertIn("隐含", s2_row)

    def test_contradictory_archive_shows_conflict_page(self):
        bad_archive = {
            "answers": {"S1": True},
            "followups": [
                {"qid": "Q1", "shown": True},
                {"qid": "Q2", "shown": False},  # S1=真时 Q2 条件必真，矛盾
            ],
        }
        self._setup({"config": CONFIG, "archive": bad_archive})
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("旧档矛盾", page)
        self.assertIn("最小矛盾核", page)

    def test_invalid_json_flashes_error(self):
        resp = self.client.post("/setup", data={"config_text": "{not json"})
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("JSON 解析失败", page)

    def test_duplicate_answer_rejected(self):
        self._setup({"config": CONFIG, "archive": ARCHIVE})
        resp = self.client.post(
            "/answer", data={"item": "S1", "value": "false"}
        )
        self.assertEqual(resp.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("已有答案", page)

    def test_reset_answers(self):
        self._setup({"config": CONFIG, "archive": ARCHIVE})
        self.client.post("/reset-answers")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("未答", page)


if __name__ == "__main__":
    unittest.main()
