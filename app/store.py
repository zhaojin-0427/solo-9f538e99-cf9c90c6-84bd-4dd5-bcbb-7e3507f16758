# -*- coding: utf-8 -*-
"""试跑核查持久层：版号配置、试跑记录与异常组（仅依赖标准库 sqlite3）。

关键约束（与页面需求一一对应）：

- 试跑号重传**相同内容**不增计（records.trial_id 主键 + content_hash 去重）；
- 试跑号重传**不同内容**被拒绝，必须在网页按试跑号更正；
- 同版号 + 同完整答案的一组试跑，只有“来自不同终端”且“一份展示、一份跳过”
  才判为异常；异常组在 group_qid 中按追问题号落库；
- 更正时只重算**旧记录与新记录涉及的答案组**，组内通过按终端的聚合表
  （group_side）判断证据，不逐份扫描记录；
- 查询只读取 group_qid 中 anomalous=1 的异常组，并按页取证据记录；
- 记录写入与异常重算在同一事务内完成，失败整体回滚，旧报告仍可查询。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .engine.trial import (
    TrialError,
    TrialRecord,
    TrialVersion,
    parse_version_configs,
    version_config_to_jsonable,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version     TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    group_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    version     TEXT NOT NULL,
    answers_key TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    UNIQUE (version, answers_key)
);

CREATE TABLE IF NOT EXISTS records (
    trial_id    TEXT PRIMARY KEY,
    version     TEXT NOT NULL,
    terminal    TEXT NOT NULL,
    group_id    INTEGER NOT NULL REFERENCES groups(group_id),
    answers_key TEXT NOT NULL,
    answers_json TEXT NOT NULL,
    followups_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS records_group_terminal ON records(group_id, terminal);

CREATE TABLE IF NOT EXISTS record_followups (
    trial_id TEXT NOT NULL REFERENCES records(trial_id) ON DELETE CASCADE,
    qid      TEXT NOT NULL,
    shown    INTEGER NOT NULL,
    PRIMARY KEY (trial_id, qid)
);
CREATE INDEX IF NOT EXISTS rf_side ON record_followups(qid, shown);

CREATE TABLE IF NOT EXISTS group_qid (
    group_id     INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    qid          TEXT NOT NULL,
    expected_shown INTEGER NOT NULL,
    shown_count  INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    anomalous    INTEGER NOT NULL DEFAULT 0,
    evidence_shown_trial   TEXT,
    evidence_skipped_trial TEXT,
    PRIMARY KEY (group_id, qid)
);
CREATE INDEX IF NOT EXISTS gq_anomaly ON group_qid(anomalous, qid);

CREATE TABLE IF NOT EXISTS group_side (
    group_id INTEGER NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    qid      TEXT NOT NULL,
    shown    INTEGER NOT NULL,
    terminal TEXT NOT NULL,
    cnt      INTEGER NOT NULL,
    trial_id TEXT NOT NULL,
    PRIMARY KEY (group_id, qid, shown, terminal)
);
CREATE INDEX IF NOT EXISTS gs_lookup ON group_side(group_id, qid, shown, terminal);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def answers_key(answers: Dict[str, bool]) -> str:
    """答案组键：只看筛查项取值，与上传顺序无关。"""
    return json.dumps(answers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TrialStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    # ------------------------------------------------------------------
    # 连接与事务
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def init_schema(self) -> None:
        # 多 worker（gunicorn）同时启动时，DDL/PRAGMA 可能短暂争锁，重试即可。
        last_error: Optional[Exception] = None
        for _ in range(20):
            conn = self.connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                return
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
            finally:
                conn.close()
            time.sleep(0.1)
        raise last_error  # type: ignore[misc]

    @contextmanager
    def _write(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def _read(self):
        conn = self.connect()
        try:
            # 单条连接内开只读事务，保证“异常组 + 证据”读到同一快照
            conn.execute("BEGIN")
            yield conn
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            conn.close()

    # ------------------------------------------------------------------
    # 版号配置
    # ------------------------------------------------------------------
    def list_versions(self) -> List[dict]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT version, config_hash, updated_at FROM versions ORDER BY version"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_version(self, version: str) -> Optional[TrialVersion]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT config_json FROM versions WHERE version = ?", (version,)
            ).fetchone()
        if row is None:
            return None
        return parse_version_configs(json.loads(row["config_json"]))[0]

    def _versions_in(self, conn) -> Dict[str, TrialVersion]:
        out: Dict[str, TrialVersion] = {}
        for row in conn.execute("SELECT config_json FROM versions"):
            cfg = parse_version_configs(json.loads(row["config_json"]))[0]
            out[cfg.version] = cfg
        return out

    def upload_versions(self, raw) -> dict:
        """上传一份或多份版号配置；已有记录时禁止改动筛查项 / 追问题号集合。"""
        configs = parse_version_configs(raw)
        created, updated, unchanged = [], [], []
        with self._write() as conn:
            existing = self._versions_in(conn)
            for cfg in configs:
                if cfg.version not in existing:
                    conn.execute(
                        "INSERT INTO versions(version, config_json, config_hash, updated_at)"
                        " VALUES (?, ?, ?, ?)",
                        (cfg.version, cfg.config_json, cfg.config_hash, _now()),
                    )
                    created.append(cfg.version)
                    continue
                old = existing[cfg.version]
                if old.config_hash == cfg.config_hash:
                    unchanged.append(cfg.version)
                    continue
                record_count = conn.execute(
                    "SELECT COUNT(*) FROM records WHERE version = ?", (cfg.version,)
                ).fetchone()[0]
                if record_count:
                    if set(old.items) != set(cfg.items):
                        raise TrialError(
                            f"版号 {cfg.version} 已有试跑记录，不能改动筛查项集合"
                        )
                    if set(old.qids) != set(cfg.qids):
                        raise TrialError(
                            f"版号 {cfg.version} 已有试跑记录，不能增删追问题号"
                        )
                conn.execute(
                    "UPDATE versions SET config_json=?, config_hash=?, updated_at=?"
                    " WHERE version=?",
                    (cfg.config_json, cfg.config_hash, _now(), cfg.version),
                )
                updated.append(cfg.version)
                # 条件变化会影响全部同版组件的预期，显式上传配置时整体重算一次。
                if record_count:
                    group_ids = [
                        r["group_id"]
                        for r in conn.execute(
                            "SELECT group_id FROM groups WHERE version=?", (cfg.version,)
                        )
                    ]
                    for gid in group_ids:
                        self._recompute_group(conn, gid, {cfg.version: cfg})
        return {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
        }

    # ------------------------------------------------------------------
    # 试跑记录上传
    # ------------------------------------------------------------------
    def upload_records(self, records: Sequence[TrialRecord]) -> dict:
        """原子写入一批试跑记录，并重算受影响组的异常。

        同一试跑号：内容相同 => 跳过（不增计）；内容不同 => 整批拒绝，
        提示改在网页按试跑号更正。
        """
        inserted, unchanged = [], []
        with self._write() as conn:
            versions = self._versions_in(conn)
            for rec in records:
                if rec.version not in versions:
                    raise TrialError(f"版号 {rec.version} 尚未上传配置")
            # 先做重传冲突检查，避免半批写入
            conflicts = []
            for rec in records:
                row = conn.execute(
                    "SELECT content_hash FROM records WHERE trial_id=?", (rec.trial_id,)
                ).fetchone()
                if row is None:
                    continue
                if row["content_hash"] == rec.content_hash:
                    unchanged.append(rec.trial_id)
                else:
                    conflicts.append(rec.trial_id)
            if conflicts:
                raise TrialError(
                    "以下试跑号已有不同内容的记录，重传不会覆盖，请在网页按试跑号更正："
                    + "、".join(sorted(set(conflicts)))
                )

            seen_new: Dict[str, str] = {}
            for rec in records:
                if rec.trial_id in seen_new:
                    if seen_new[rec.trial_id] != rec.content_hash:
                        raise TrialError(
                            f"本次上传中试跑号 {rec.trial_id} 出现两次且内容不同，"
                            "请改为按试跑号更正"
                        )
                    continue
                seen_new[rec.trial_id] = rec.content_hash
                row = conn.execute(
                    "SELECT content_hash FROM records WHERE trial_id=?", (rec.trial_id,)
                ).fetchone()
                if row is not None:
                    continue  # 同内容重传，不增计
                gid = self._ensure_group(conn, rec, versions[rec.version])
                self._insert_record(conn, rec, gid)
                inserted.append(rec.trial_id)

            for gid in sorted({self._group_of(conn, tid) for tid in inserted}):
                self._recompute_group(conn, gid, versions)
        return {"inserted": inserted, "unchanged": unchanged}

    # ------------------------------------------------------------------
    # 按试跑号更正
    # ------------------------------------------------------------------
    def get_record(self, trial_id: str) -> Optional[dict]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE trial_id=?", (trial_id,)
            ).fetchone()
        return _record_row_to_dict(row) if row else None

    def correct_record(self, new: TrialRecord) -> dict:
        """用新内容替换同试跑号记录，只重算旧/新记录涉及的答案组。"""
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM records WHERE trial_id=?", (new.trial_id,)
            ).fetchone()
            if row is None:
                raise TrialError(f"试跑号 {new.trial_id} 不存在，无法更正")
            versions = self._versions_in(conn)
            if new.version not in versions:
                raise TrialError(f"版号 {new.version} 尚未上传配置")
            old = _record_row_to_dict(row)
            if old["content_hash"] == new.content_hash:
                return {"changed": False, "old_group": old["group_id"],
                        "new_group": old["group_id"]}

            old_gid = old["group_id"]
            self._remove_record(conn, old)
            new_gid = self._ensure_group(conn, new, versions[new.version])
            self._insert_record(conn, new, new_gid)

            affected = {new_gid}
            if self._group_is_empty(conn, old_gid):
                conn.execute("DELETE FROM groups WHERE group_id=?", (old_gid,))
            else:
                affected.add(old_gid)

            for gid in sorted(affected):
                self._recompute_group(conn, gid, versions)
        return {"changed": True, "old_group": old_gid, "new_group": new_gid}

    # ------------------------------------------------------------------
    # 异常查询
    # ------------------------------------------------------------------
    def list_anomalies(
        self,
        version: Optional[str],
        qid: Optional[str],
        page: int,
        page_size: int,
    ) -> dict:
        where = ["gq.anomalous = 1"]
        params: List[object] = []
        if version:
            where.append("g.version = ?")
            params.append(version)
        if qid:
            where.append("gq.qid = ?")
            params.append(qid)
        clause = " WHERE " + " AND ".join(where)
        with self._read() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM group_qid gq JOIN groups g"
                " ON g.group_id = gq.group_id" + clause,
                params,
            ).fetchone()[0]
            pages = max((total + page_size - 1) // page_size, 1)
            if total and page > pages:
                page = pages
            offset = max(page - 1, 0) * page_size
            rows = conn.execute(
                "SELECT gq.group_id, gq.qid, gq.expected_shown,"
                " gq.shown_count, gq.skipped_count,"
                " gq.evidence_shown_trial, gq.evidence_skipped_trial,"
                " g.version, g.answers_json"
                " FROM group_qid gq JOIN groups g ON g.group_id = gq.group_id"
                + clause
                + " ORDER BY g.version, g.answers_key, gq.qid LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
            evidence_ids = []
            for r in rows:
                evidence_ids.extend([r["evidence_shown_trial"],
                                     r["evidence_skipped_trial"]])
            evidence: Dict[str, dict] = {}
            if evidence_ids:
                marks = ",".join("?" for _ in evidence_ids)
                for r in conn.execute(
                    "SELECT * FROM records WHERE trial_id IN (" + marks + ")",
                    evidence_ids,
                ):
                    evidence[r["trial_id"]] = _record_row_to_dict(r)
            versions: Dict[str, TrialVersion] = {}
            for r in rows:
                if r["version"] not in versions:
                    vrow = conn.execute(
                        "SELECT config_json FROM versions WHERE version=?",
                        (r["version"],),
                    ).fetchone()
                    versions[r["version"]] = parse_version_configs(
                        json.loads(vrow["config_json"])
                    )[0]
        display_rows = []
        for r in rows:
            cfg = versions[r["version"]]
            fmap = cfg.followup_map()
            shown_ev = evidence.get(r["evidence_shown_trial"])
            skipped_ev = evidence.get(r["evidence_skipped_trial"])
            expected = bool(r["expected_shown"])
            mismatch = skipped_ev if expected else shown_ev
            display_rows.append({
                "group_id": r["group_id"],
                "qid": r["qid"],
                "version": r["version"],
                "answers": json.loads(r["answers_json"]),
                "expected_shown": expected,
                "expected_text": "展示" if expected else "跳过",
                "shown_count": r["shown_count"],
                "skipped_count": r["skipped_count"],
                "condition_text": fmap[r["qid"]].condition_text if r["qid"] in fmap else "",
                "labels": cfg.labels,
                "shown_evidence": shown_ev,
                "skipped_evidence": skipped_ev,
                "mismatch_evidence": mismatch,
                "mismatch_actual_text": "跳过" if expected else "展示",
            })
        return {
            "rows": display_rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def count_records(self) -> int:
        with self._read() as conn:
            return conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def list_qids(self) -> List[str]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT qid FROM group_qid GROUP BY qid ORDER BY qid"
            ).fetchall()
        return [r["qid"] for r in rows]

    # ------------------------------------------------------------------
    # 内部：记录/组维护
    # ------------------------------------------------------------------
    def _ensure_group(self, conn, rec: TrialRecord, cfg: TrialVersion) -> int:
        key = answers_key(rec.answers)
        answers_json = json.dumps(rec.answers, ensure_ascii=False, sort_keys=True)
        row = conn.execute(
            "SELECT group_id FROM groups WHERE version=? AND answers_key=?",
            (rec.version, key),
        ).fetchone()
        if row is not None:
            gid = row["group_id"]
        else:
            cur = conn.execute(
                "INSERT INTO groups(version, answers_key, answers_json) VALUES (?, ?, ?)",
                (rec.version, key, answers_json),
            )
            gid = cur.lastrowid
        for qid in cfg.qids:
            expected = int(cfg.expected_shown(qid, rec.answers))
            conn.execute(
                "INSERT OR IGNORE INTO group_qid(group_id, qid, expected_shown)"
                " VALUES (?, ?, ?)",
                (gid, qid, expected),
            )
        return gid

    def _insert_record(self, conn, rec: TrialRecord, gid: int) -> None:
        now = _now()
        conn.execute(
            "INSERT INTO records(trial_id, version, terminal, group_id, answers_key,"
            " answers_json, followups_json, content_hash, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.trial_id,
                rec.version,
                rec.terminal,
                gid,
                answers_key(rec.answers),
                rec.answers_json,
                rec.followups_json,
                rec.content_hash,
                now,
                now,
            ),
        )
        for qid, shown in rec.followups:
            conn.execute(
                "INSERT INTO record_followups(trial_id, qid, shown) VALUES (?, ?, ?)",
                (rec.trial_id, qid, int(shown)),
            )
            conn.execute(
                "INSERT INTO group_side(group_id, qid, shown, terminal, cnt, trial_id)"
                " VALUES (?, ?, ?, ?, 1, ?)"
                " ON CONFLICT(group_id, qid, shown, terminal) DO UPDATE SET"
                " cnt = cnt + 1,"
                " trial_id = CASE WHEN excluded.trial_id < trial_id"
                "   THEN excluded.trial_id ELSE trial_id END",
                (gid, qid, int(shown), rec.terminal, rec.trial_id),
            )

    def _remove_record(self, conn, old: dict) -> None:
        trial_id = old["trial_id"]
        gid = old["group_id"]
        conn.execute("DELETE FROM records WHERE trial_id=?", (trial_id,))
        for qid, shown in old["followups"]:
            side = conn.execute(
                "SELECT cnt, trial_id FROM group_side WHERE group_id=? AND qid=?"
                " AND shown=? AND terminal=?",
                (gid, qid, int(shown), old["terminal"]),
            ).fetchone()
            if side is None:
                continue
            if side["cnt"] <= 1:
                conn.execute(
                    "DELETE FROM group_side WHERE group_id=? AND qid=?"
                    " AND shown=? AND terminal=?",
                    (gid, qid, int(shown), old["terminal"]),
                )
            elif side["trial_id"] == trial_id:
                # 仅在该终端内补一个代表，不做整组逐份扫描
                replacement = conn.execute(
                    "SELECT rf.trial_id FROM record_followups rf"
                    " JOIN records r ON r.trial_id = rf.trial_id"
                    " WHERE r.group_id=? AND r.terminal=? AND rf.qid=? AND rf.shown=?"
                    " ORDER BY rf.trial_id LIMIT 1",
                    (gid, old["terminal"], qid, int(shown)),
                ).fetchone()
                conn.execute(
                    "UPDATE group_side SET cnt = cnt - 1, trial_id=?"
                    " WHERE group_id=? AND qid=? AND shown=? AND terminal=?",
                    (replacement["trial_id"], gid, qid, int(shown), old["terminal"]),
                )
            else:
                conn.execute(
                    "UPDATE group_side SET cnt = cnt - 1"
                    " WHERE group_id=? AND qid=? AND shown=? AND terminal=?",
                    (gid, qid, int(shown), old["terminal"]),
                )

    def _group_of(self, conn, trial_id: str) -> int:
        return conn.execute(
            "SELECT group_id FROM records WHERE trial_id=?", (trial_id,)
        ).fetchone()["group_id"]

    def _group_is_empty(self, conn, gid: int) -> bool:
        return conn.execute(
            "SELECT COUNT(*) FROM records WHERE group_id=?", (gid,)
        ).fetchone()[0] == 0

    # ------------------------------------------------------------------
    # 内部：异常重算（只针对受影响组）
    # ------------------------------------------------------------------
    def _recompute_group(
        self, conn, gid: int, versions: Dict[str, TrialVersion]
    ) -> None:
        group = conn.execute(
            "SELECT version, answers_json FROM groups WHERE group_id=?", (gid,)
        ).fetchone()
        if group is None:
            return
        cfg = versions.get(group["version"])
        if cfg is None:
            cfg = parse_version_configs(
                json.loads(conn.execute(
                    "SELECT config_json FROM versions WHERE version=?",
                    (group["version"],),
                ).fetchone()["config_json"])
            )[0]
            versions[group["version"]] = cfg
        answers = json.loads(group["answers_json"])

        for qid in cfg.qids:
            expected = cfg.expected_shown(qid, answers)
            sides = {"shown": [], "skipped": []}
            for r in conn.execute(
                "SELECT shown, terminal, cnt, trial_id FROM group_side"
                " WHERE group_id=? AND qid=? ORDER BY terminal",
                (gid, qid),
            ):
                sides["shown" if r["shown"] else "skipped"].append(dict(r))
            shown_rows = sides["shown"]
            skipped_rows = sides["skipped"]
            prev = conn.execute(
                "SELECT evidence_shown_trial, evidence_skipped_trial FROM group_qid"
                " WHERE group_id=? AND qid=?",
                (gid, qid),
            ).fetchone()
            pair = _pick_pair(
                shown_rows,
                skipped_rows,
                keep_shown=prev["evidence_shown_trial"] if prev else None,
                keep_skipped=prev["evidence_skipped_trial"] if prev else None,
            )
            if pair is None:
                ev_shown = ev_skipped = None
                anomalous = 0
            else:
                ev_shown, ev_skipped = pair
                anomalous = 1
            conn.execute(
                "UPDATE group_qid SET expected_shown=?, shown_count=?, skipped_count=?,"
                " anomalous=?, evidence_shown_trial=?, evidence_skipped_trial=?"
                " WHERE group_id=? AND qid=?",
                (
                    int(expected),
                    sum(r["cnt"] for r in shown_rows),
                    sum(r["cnt"] for r in skipped_rows),
                    anomalous,
                    ev_shown["trial_id"] if ev_shown else None,
                    ev_skipped["trial_id"] if ev_skipped else None,
                    gid,
                    qid,
                ),
            )


def _pick_pair(
    shown_rows: List[dict],
    skipped_rows: List[dict],
    keep_shown: Optional[str] = None,
    keep_skipped: Optional[str] = None,
):
    """从按终端聚合的展示/跳过侧各取一条，终端必须不同。

    优先沿用仍然成立的旧证据（keep_*），这样更正一条记录时，另一侧的
    证据对不会无谓跳动；旧证据失效再换一对。
    """
    if not shown_rows or not skipped_rows:
        return None
    shown_by_id = {r["trial_id"]: r for r in shown_rows}
    skipped_by_id = {r["trial_id"]: r for r in skipped_rows}

    kept_s = shown_by_id.get(keep_shown)
    kept_k = skipped_by_id.get(keep_skipped)
    if kept_s and kept_k and kept_s["terminal"] != kept_k["terminal"]:
        return kept_s, kept_k
    if kept_s:
        k = next((x for x in skipped_rows if x["terminal"] != kept_s["terminal"]), None)
        if k is not None:
            return kept_s, k
    if kept_k:
        s = next((x for x in shown_rows if x["terminal"] != kept_k["terminal"]), None)
        if s is not None:
            return s, kept_k

    s0 = shown_rows[0]
    k = next((x for x in skipped_rows if x["terminal"] != s0["terminal"]), None)
    if k is not None:
        return s0, k
    k0 = skipped_rows[0]
    s = next((x for x in shown_rows if x["terminal"] != k0["terminal"]), None)
    if s is not None:
        return s, k0
    return None


def _record_row_to_dict(row) -> dict:
    return {
        "trial_id": row["trial_id"],
        "version": row["version"],
        "terminal": row["terminal"],
        "group_id": row["group_id"],
        "answers": json.loads(row["answers_json"]),
        "followups": [
            (f["qid"], bool(f["shown"])) for f in json.loads(row["followups_json"])
        ],
        "content_hash": row["content_hash"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
