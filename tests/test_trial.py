# -*- coding: utf-8 -*-
"""试跑核查测试：记录去重/拒绝重传、异终端异常判定、更正与分组、分页查询。"""
import json
import os
import shutil
import tempfile
import unittest

from app.engine.trial import TrialError, normalize_trial_record, parse_version_configs
from app.store import TrialStore

try:
    from app.app import create_app

    FLASK_AVAILABLE = True
except Exception:  # pragma: no cover - 本地最小环境
    FLASK_AVAILABLE = False


CONFIG = {
    "versions": [
        {
            "version": "v2.3",
            "items": ["S1", "S2", "S3", "S4"],
            "labels": {"S1": "头盔", "S2": "车灯", "S3": "超30分钟", "S4": "夜骑"},
            "followups": [
                {"qid": "Q1", "condition": "S1 AND NOT S2"},
                {"qid": "Q2", "condition": "S3 OR S1"},
            ],
        },
        {
            "version": "v2.4",
            "items": ["S1", "S2", "S3", "S4"],
            "labels": {"S1": "头盔", "S2": "车灯", "S3": "超30分钟", "S4": "夜骑"},
            "followups": [
                {"qid": "Q1", "condition": "S1 AND NOT S2"},
            ],
        },
    ]
}

# Q1 条件 = S1 AND NOT S2 = 真；Q2 条件 = S3 OR S1 = 真
ANSWERS = {"S1": True, "S2": False, "S3": True, "S4": False}


def rec(trial_id, terminal, q1=True, q2=True, version="v2.3", answers=None):
    return {
        "trial_id": trial_id,
        "terminal": terminal,
        "version": version,
        "answers": dict(ANSWERS if answers is None else answers),
        "followups": (
            [{"qid": "Q1", "shown": q1}, {"qid": "Q2", "shown": q2}]
            if version == "v2.3"
            else [{"qid": "Q1", "shown": q1}]
        ),
    }


class TrialStoreTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = TrialStore(os.path.join(self.dir, "t.db"))
        self.store.init_schema()
        self.store.upload_versions(CONFIG)
        cfg = parse_version_configs(CONFIG)
        self.cfgs = {c.version: c for c in cfg}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def upload(self, *raw_records):
        records = [
            normalize_trial_record(r, self.cfgs[r["version"]]) for r in raw_records
        ]
        return self.store.upload_records(records)

    def anomalies(self, version=None, qid=None):
        return self.store.list_anomalies(version, qid, 1, 50)


class UploadTests(TrialStoreTestBase):
    def test_duplicate_same_content_not_counted(self):
        outcome = self.upload(rec("R1", "T-A"), rec("R2", "T-B", q1=False))
        self.assertEqual(sorted(outcome["inserted"]), ["R1", "R2"])
        outcome2 = self.upload(rec("R1", "T-A"))
        self.assertEqual(outcome2["inserted"], [])
        self.assertEqual(outcome2["unchanged"], ["R1"])
        self.assertEqual(self.store.count_records(), 2)

    def test_duplicate_different_content_rejected_atomically(self):
        self.upload(rec("R1", "T-A"))
        with self.assertRaises(TrialError) as ctx:
            self.upload(rec("R1", "T-A", q1=False), rec("R2", "T-B"))
        self.assertIn("按试跑号更正", str(ctx.exception))
        # 整批回滚：R2 不应被写入
        self.assertEqual(self.store.count_records(), 1)
        self.assertIsNone(self.store.get_record("R2"))

    def test_same_batch_conflicting_duplicate_rejected(self):
        with self.assertRaises(TrialError):
            self.upload(rec("R1", "T-A"), rec("R1", "T-A", q1=False))
        self.assertEqual(self.store.count_records(), 0)

    def test_incomplete_answers_rejected(self):
        bad = rec("R1", "T-A", answers={"S1": True})
        with self.assertRaises(TrialError):
            normalize_trial_record(bad, self.cfgs["v2.3"])

    def test_missing_followup_record_rejected(self):
        bad = rec("R1", "T-A")
        bad["followups"] = [{"qid": "Q1", "shown": True}]
        with self.assertRaises(TrialError):
            normalize_trial_record(bad, self.cfgs["v2.3"])

    def test_unknown_version_rejected(self):
        with self.assertRaises(TrialError):
            normalize_trial_record(rec("R1", "T-A", version="v9"), self.cfgs["v2.3"])

    def test_chinese_aliases_accepted(self):
        raw = {
            "试跑号": "R-中文",
            "终端号": "终端甲",
            "版号": "v2.3",
            "筛查答案": {"S1": "是", "S2": "否", "S3": "是", "S4": "否"},
            "展示跳过记录": [
                {"qid": "Q1", "展示": "展示"},
                {"qid": "Q2", "展示": "跳过"},
            ],
        }
        record = normalize_trial_record(raw, self.cfgs["v2.3"])
        self.assertEqual(record.followups, (("Q1", True), ("Q2", False)))


class AnomalyTests(TrialStoreTestBase):
    def test_different_terminals_disagree_is_anomaly(self):
        self.upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        result = self.anomalies(qid="Q1")
        self.assertEqual(result["total"], 1)
        row = result["rows"][0]
        self.assertTrue(row["expected_shown"])
        self.assertEqual(row["shown_evidence"]["terminal"], "T-A")
        self.assertEqual(row["skipped_evidence"]["terminal"], "T-B")

    def test_same_terminal_disagreement_not_anomaly(self):
        self.upload(rec("R1", "T-A", q1=True), rec("R2", "T-A", q1=False))
        self.assertEqual(self.anomalies(qid="Q1")["total"], 0)

    def test_agreement_not_anomaly(self):
        self.upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=True))
        self.assertEqual(self.anomalies(qid="Q1")["total"], 0)

    def test_different_answers_are_separate_groups(self):
        other = {"S1": True, "S2": True, "S3": True, "S4": False}
        self.upload(
            rec("R1", "T-A", q1=True),
            rec("R2", "T-B", q1=False),
            rec("R3", "T-A", q1=True, answers=other),
            rec("R4", "T-B", q1=False, answers=other),
        )
        # 两个答案组各自异常
        self.assertEqual(self.anomalies(qid="Q1")["total"], 2)

    def test_different_versions_are_separate_groups(self):
        self.upload(
            rec("R1", "T-A", q1=True),
            rec("R2", "T-B", q1=False),
            rec("R3", "T-A", q1=True, version="v2.4"),
            rec("R4", "T-B", q1=False, version="v2.4"),
        )
        self.assertEqual(self.anomalies(version="v2.3", qid="Q1")["total"], 1)
        self.assertEqual(self.anomalies(version="v2.4", qid="Q1")["total"], 1)

    def test_filter_and_pagination(self):
        for i in range(5):
            answers = {
                "S1": bool(i & 1),
                "S2": bool(i & 2),
                "S3": bool(i & 4),
                "S4": bool(i & 8),
            }
            self.upload(
                rec(f"R{i}a", f"T-A{i}", q1=True, answers=answers),
                rec(f"R{i}b", f"T-B{i}", q1=False, answers=answers),
            )
        first = self.store.list_anomalies(None, "Q1", 1, 2)
        self.assertEqual(first["total"], 5)
        self.assertEqual(first["pages"], 3)
        self.assertEqual(len(first["rows"]), 2)
        last = self.store.list_anomalies(None, "Q1", 3, 2)
        self.assertEqual(len(last["rows"]), 1)
        # 越界页码夹到最后一页，而不是空页
        clamped = self.store.list_anomalies(None, "Q1", 99, 2)
        self.assertEqual(clamped["page"], 3)
        self.assertEqual(len(clamped["rows"]), 1)
        self.assertEqual(self.store.list_anomalies("v2.4", None, 1, 10)["total"], 0)


class CorrectionTests(TrialStoreTestBase):
    def correct(self, raw, old_version=None):
        old = self.store.get_record(raw["trial_id"])
        if old is None:
            raise TrialError(f"试跑号 {raw['trial_id']} 不存在，无法更正")
        cfg = self.cfgs[raw.get("version", old["version"])]
        return self.store.correct_record(normalize_trial_record(raw, cfg))

    def test_correction_replaces_evidence_and_keeps_anomaly(self):
        self.upload(
            rec("R1", "T-A", q1=True),
            rec("R2", "T-B", q1=False),
            rec("R3", "T-C", q1=False),
        )
        self.correct(rec("R2", "T-B", q1=True))
        result = self.anomalies(qid="Q1")
        row = result["rows"][0]
        self.assertEqual(row["shown_evidence"]["trial_id"], "R1")
        self.assertEqual(row["skipped_evidence"]["trial_id"], "R3")

    def test_correction_removes_last_evidence_withdraws_anomaly(self):
        self.upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        self.correct(rec("R2", "T-B", q1=True))
        self.assertEqual(self.anomalies(qid="Q1")["total"], 0)

    def test_correction_moves_record_to_new_answer_group(self):
        self.upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        new_answers = {"S1": True, "S2": True, "S3": True, "S4": False}
        outcome = self.correct(rec("R2", "T-B", q1=False, answers=new_answers))
        self.assertTrue(outcome["changed"])
        self.assertNotEqual(outcome["old_group"], outcome["new_group"])
        # 原组唯一跳过证据被移走，异常撤下；新组没有展示侧，也不异常
        self.assertEqual(self.anomalies(qid="Q1")["total"], 0)
        moved = self.store.get_record("R2")
        self.assertEqual(moved["answers"], new_answers)
        self.assertEqual(moved["group_id"], outcome["new_group"])

    def test_correction_replaces_representative_when_same_terminal_has_more(self):
        # T-A 有两份展示记录，代表取 trial_id 较小者 R1；删掉 R1 后代表应换成 R3
        self.upload(
            rec("R1", "T-A", q1=True),
            rec("R2", "T-B", q1=False),
            rec("R3", "T-A", q1=True),
        )
        before = self.anomalies(qid="Q1")["rows"][0]
        self.assertEqual(before["shown_evidence"]["trial_id"], "R1")
        # 把 R1 改到另一个答案组，离开原组；同终端 T-A 仍由 R3 代表展示侧
        self.correct(
            rec("R1", "T-A", q1=True, answers={"S1": True, "S2": True, "S3": True, "S4": False})
        )
        after = self.anomalies(qid="Q1")["rows"][0]
        self.assertEqual(after["shown_evidence"]["trial_id"], "R3")
        self.assertEqual(after["skipped_evidence"]["trial_id"], "R2")
        self.assertEqual(after["shown_count"], 1)
        with self.store._read() as conn:
            row = conn.execute(
                "SELECT cnt, trial_id FROM group_side WHERE group_id=?"
                " AND qid='Q1' AND shown=1 AND terminal='T-A'",
                (after["group_id"],),
            ).fetchone()
        self.assertEqual((row["cnt"], row["trial_id"]), (1, "R3"))

    def test_correction_keeps_still_valid_evidence_pair(self):
        self.upload(
            rec("R1", "T-A", q1=True),
            rec("R2", "T-B", q1=False),
            rec("R3", "T-C", q1=False),
        )
        before = self.anomalies(qid="Q1")["rows"][0]
        # 更正一条非证据记录，证据对应保持不变
        self.correct(rec("R3", "T-C", q1=False, q2=False))
        after = self.anomalies(qid="Q1")["rows"][0]
        self.assertEqual(
            before["shown_evidence"]["trial_id"],
            after["shown_evidence"]["trial_id"],
        )
        self.assertEqual(
            before["skipped_evidence"]["trial_id"],
            after["skipped_evidence"]["trial_id"],
        )

    def test_correction_with_same_content_is_noop(self):
        self.upload(rec("R1", "T-A"), rec("R2", "T-B"))
        outcome = self.correct(rec("R1", "T-A"))
        self.assertFalse(outcome["changed"])

    def test_correction_into_empty_old_group_deletes_group(self):
        self.upload(rec("R1", "T-A", q1=True))
        self.correct(rec("R1", "T-A", q1=True, answers={"S1": True, "S2": True, "S3": True, "S4": False}))
        result = self.anomalies()
        self.assertEqual(result["total"], 0)
        with self.store._read() as conn:
            count = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
        self.assertEqual(count, 1)

    def test_correction_of_unknown_trial_rejected(self):
        with self.assertRaises(TrialError):
            self.correct(rec("NOPE", "T-A"))
        self.assertEqual(self.store.count_records(), 0)

    def test_only_affected_groups_recomputed(self):
        self.upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        calls = []
        original = TrialStore._recompute_group

        def spy(store_self, conn, gid, versions):
            calls.append(gid)
            return original(store_self, conn, gid, versions)

        TrialStore._recompute_group = spy
        try:
            self.correct(rec("R2", "T-B", q1=True))
        finally:
            TrialStore._recompute_group = original
        # 同组更正：只重算该组一次，不逐份扫描
        self.assertEqual(len(calls), 1)


class VersionConfigTests(TrialStoreTestBase):
    def test_cannot_remove_qid_when_records_exist(self):
        self.upload(rec("R1", "T-A"))
        broken = json.loads(json.dumps(CONFIG))
        broken["versions"][0]["followups"] = broken["versions"][0]["followups"][:1]
        with self.assertRaises(TrialError):
            self.store.upload_versions(broken)

    def test_condition_change_recomputes_expected(self):
        # S2=True 时 Q1 条件 S1 AND NOT S2 = 假；先按“本应展示”的假设造异常
        answers = {"S1": True, "S2": True, "S3": True, "S4": False}
        self.upload(
            rec("R1", "T-A", q1=True, answers=answers),
            rec("R2", "T-B", q1=False, answers=answers),
        )
        row = self.anomalies(qid="Q1")["rows"][0]
        self.assertFalse(row["expected_shown"])

        changed = json.loads(json.dumps(CONFIG))
        changed["versions"][0]["followups"][0]["condition"] = "S1 OR S2"
        self.store.upload_versions(changed)
        row = self.anomalies(qid="Q1")["rows"][0]
        self.assertTrue(row["expected_shown"])


@unittest.skipUnless(FLASK_AVAILABLE, "Flask 未安装")
class TrialWebTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["TRIAL_DB_PATH"] = os.path.join(self.dir, "web.db")
        app = create_app()
        app.config.update(TESTING=True)
        self.client = app.test_client()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _post(self, url, **data):
        return self.client.post(url, data=data, follow_redirects=True)

    def _setup(self):
        return self._post(
            "/trials/versions",
            versions_text=json.dumps(CONFIG, ensure_ascii=False),
        )

    def _upload(self, *records):
        text = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
        return self._post("/trials/records", trials_text=text)

    def test_anomaly_page_lists_pair_and_mismatch(self):
        self._setup()
        page = self._upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        html = page.get_data(as_text=True)
        self.assertIn("异常", html)
        self.assertIn("R1", html)
        self.assertIn("R2", html)
        self.assertIn("T-A", html)
        self.assertIn("T-B", html)
        self.assertIn("不符预期", html)
        self.assertIn("按该版规则本应", html)

    def test_reupload_same_records_does_not_increase(self):
        self._setup()
        self._upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        page = self._upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        self.assertIn("未增计 2 条", page.get_data(as_text=True))

    def test_reupload_different_content_must_correct(self):
        self._setup()
        self._upload(rec("R1", "T-A", q1=True))
        page = self._upload(rec("R1", "T-A", q1=False))
        self.assertIn("按试跑号更正", page.get_data(as_text=True))

    def test_correct_via_web_withdraws_anomaly(self):
        self._setup()
        self._upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        page = self._post(
            "/trials/records/R2/correct",
            record_json=json.dumps(rec("R2", "T-B", q1=True), ensure_ascii=False),
        )
        html = page.get_data(as_text=True)
        self.assertIn("已更正", html)
        listing = self.client.get("/trials").get_data(as_text=True)
        self.assertNotIn('class="anomaly"', listing)

    def test_bad_correction_keeps_old_record_and_report(self):
        self._setup()
        self._upload(rec("R1", "T-A", q1=True), rec("R2", "T-B", q1=False))
        # 非法 JSON
        page = self._post("/trials/records/R2/correct", record_json="{not json")
        self.assertIn("JSON 解析失败", page.get_data(as_text=True))
        # 合法 JSON 但试跑号被改动 => 拒绝
        changed_id = rec("R9", "T-B", q1=True)
        page = self._post(
            "/trials/records/R2/correct",
            record_json=json.dumps(changed_id, ensure_ascii=False),
        )
        self.assertIn("更正未生效", page.get_data(as_text=True))
        listing = self.client.get("/trials").get_data(as_text=True)
        self.assertIn('class="anomaly"', listing)
        self.assertIn("R2", listing)
        stored = self.client.application.extensions["trial_store"].get_record("R2")
        self.assertEqual([tuple(x) for x in stored["followups"]], [("Q1", False), ("Q2", True)])

    def test_bad_jsonl_rolls_back_whole_upload(self):
        self._setup()
        good = json.dumps(rec("R1", "T-A", q1=True), ensure_ascii=False)
        page = self._post("/trials/records", trials_text=good + "\n{bad line")
        self.assertIn("第 2 行", page.get_data(as_text=True))
        self.assertEqual(self.client.application.extensions["trial_store"].count_records(), 0)

    def test_detail_page_shows_rule_mismatch(self):
        self._setup()
        self._upload(rec("R2", "T-B", q1=False))
        html = self.client.get("/trials/records/R2").get_data(as_text=True)
        self.assertIn("该版规则预期", html)
        self.assertIn("不符", html)

    def test_pagination_links_keep_filters(self):
        self._setup()
        for i in range(12):
            answers = {
                "S1": bool(i & 1),
                "S2": bool(i & 2),
                "S3": bool(i & 4),
                "S4": bool(i & 8),
            }
            self._upload(
                rec(f"R{i}a", f"T-A{i}", q1=True, answers=answers),
                rec(f"R{i}b", f"T-B{i}", q1=False, answers=answers),
            )
        html = self.client.get("/trials?page_size=10&version=v2.3&qid=Q1").get_data(as_text=True)
        self.assertIn("下一页", html)
        self.assertIn("version=v2.3", html)
        self.assertIn("qid=Q1", html)
        page2 = self.client.get("/trials?page=2&page_size=10&version=v2.3&qid=Q1")
        self.assertEqual(page2.status_code, 200)


if __name__ == "__main__":
    unittest.main()
