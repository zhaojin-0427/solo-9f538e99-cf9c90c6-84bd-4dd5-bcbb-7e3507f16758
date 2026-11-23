# -*- coding: utf-8 -*-
"""试跑核查页路由：上传配置/JSONL、按版号与追问题号分页查异常、按试跑号更正。"""
from __future__ import annotations

import json

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .engine.trial import (
    TrialError,
    normalize_trial_record,
    record_version,
    trial_record_to_json,
)
from .store import TrialStore

bp = Blueprint("trials", __name__, url_prefix="/trials")

PAGE_SIZES = (10, 20, 50)


def store() -> TrialStore:
    return current_app.extensions["trial_store"]


def _read_upload(upload, field: str) -> str:
    try:
        return upload.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise TrialError(f"{field} 不是 UTF-8 编码的文本文件：{exc}") from exc


def _read_text_form(field: str) -> str:
    upload = request.files.get(field + "_file")
    if upload and upload.filename:
        return _read_upload(upload, field)
    return request.form.get(field + "_text", "")


def _filter_args() -> dict:
    version = request.args.get("version", "").strip()
    qid = request.args.get("qid", "").strip()
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    try:
        page_size = int(request.args.get("page_size", "20"))
    except ValueError:
        page_size = 20
    if page_size not in PAGE_SIZES:
        page_size = 20
    return {
        "version": version,
        "qid": qid,
        "page": max(page, 1),
        "page_size": page_size,
    }


def _parse_jsonl(text: str):
    records = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TrialError(
                f"第 {lineno} 行不是合法 JSON：{exc.msg}（第 {exc.colno} 列）"
            ) from exc
        if not isinstance(raw, dict):
            raise TrialError(f"第 {lineno} 行必须是 JSON 对象")
        try:
            version = record_version(raw)
            cfg = store().get_version(version)
            if cfg is None:
                raise TrialError(
                    f"第 {lineno} 行的版号 {version} 尚未上传配置"
                )
            records.append(normalize_trial_record(raw, cfg))
        except TrialError as exc:
            raise TrialError(f"第 {lineno} 行：{exc}") from exc
    if not records:
        raise TrialError("JSONL 中没有可用的试跑记录")
    return records


@bp.route("", methods=["GET"])
def index():
    filters = _filter_args()
    result = store().list_anomalies(
        filters["version"] or None,
        filters["qid"] or None,
        filters["page"],
        filters["page_size"],
    )
    return render_template(
        "trials.html",
        versions=store().list_versions(),
        qids=store().list_qids(),
        filters=filters,
        page_sizes=PAGE_SIZES,
        result=result,
        record_count=store().count_records(),
        expected_mismatch=expected_mismatch,
    )


@bp.route("/versions", methods=["POST"])
def upload_versions():
    text = _read_text_form("versions")
    if not text.strip():
        flash("请粘贴版号配置 JSON 或上传文件。", "error")
        return redirect(url_for("trials.index"))
    try:
        payload = json.loads(text)
        outcome = store().upload_versions(payload)
    except json.JSONDecodeError as exc:
        flash(f"配置 JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）", "error")
        return redirect(url_for("trials.index"))
    except TrialError as exc:
        flash(f"版号配置校验失败：{exc}", "error")
        return redirect(url_for("trials.index"))
    parts = []
    if outcome["created"]:
        parts.append("新增 " + "、".join(outcome["created"]))
    if outcome["updated"]:
        parts.append("更新 " + "、".join(outcome["updated"]))
    if outcome["unchanged"]:
        parts.append("未变 " + "、".join(outcome["unchanged"]))
    flash("版号配置已载入：" + "；".join(parts) + "。", "ok")
    return redirect(url_for("trials.index"))


@bp.route("/records", methods=["POST"])
def upload_records():
    text = _read_text_form("trials")
    if not text.strip():
        flash("请粘贴试跑 JSONL 或上传文件。", "error")
        return redirect(url_for("trials.index"))
    try:
        records = _parse_jsonl(text)
        outcome = store().upload_records(records)
    except TrialError as exc:
        flash(f"试跑记录未写入（原报告保持不变）：{exc}", "error")
        return redirect(url_for("trials.index"))
    msg = f"试跑记录已校验并生效：新增 {len(outcome['inserted'])} 条"
    if outcome["unchanged"]:
        msg += f"，重传相同记录未增计 {len(outcome['unchanged'])} 条"
    flash(msg + "；异常组已同步更新。", "ok")
    return redirect(url_for("trials.index"))


@bp.route("/lookup", methods=["POST"])
def lookup():
    trial_id = request.form.get("trial_id", "").strip()
    if not trial_id:
        flash("请输入试跑号。", "error")
        return redirect(url_for("trials.index"))
    return redirect(url_for("trials.record_detail", trial_id=trial_id))


@bp.route("/records/<trial_id>", methods=["GET"])
def record_detail(trial_id: str):
    record = store().get_record(trial_id)
    if record is None:
        flash(f"没有找到试跑号 {trial_id}。", "error")
        return redirect(url_for("trials.index"))
    version = store().get_version(record["version"])
    return render_template(
        "trial_detail.html",
        record=record,
        version=version,
        record_json=trial_record_to_json(record),
        submitted=None,
        trial_id=trial_id,
    )


@bp.route("/records/<trial_id>/correct", methods=["POST"])
def correct_record(trial_id: str):
    record = store().get_record(trial_id)
    if record is None:
        flash(f"没有找到试跑号 {trial_id}。", "error")
        return redirect(url_for("trials.index"))
    text = request.form.get("record_json", "")
    submitted = text

    def render_form(error: str):
        flash(error, "error")
        return render_template(
            "trial_detail.html",
            record=record,
            version=store().get_version(record["version"]),
            record_json=submitted,
            submitted=submitted,
            trial_id=trial_id,
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return render_form(
            f"更正内容 JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
        )
    try:
        if not isinstance(payload, dict):
            raise TrialError("更正内容必须是 JSON 对象")
        if str(payload.get("trial_id", "")).strip() != trial_id:
            raise TrialError(f"更正内容里的试跑号必须保持为 {trial_id}")
        version = record_version(payload)
        cfg = store().get_version(version)
        if cfg is None:
            raise TrialError(f"版号 {version} 尚未上传配置")
        new_record = normalize_trial_record(payload, cfg)
        outcome = store().correct_record(new_record)
    except TrialError as exc:
        return render_form(f"更正未生效（原记录与异常报告保持不变）：{exc}")

    if not outcome["changed"]:
        flash("更正内容与现有记录完全相同，未做改动。", "ok")
    else:
        flash(
            "试跑号 "
            f"{trial_id} 已更正；只重算了旧组（#{outcome['old_group']}）"
            f"与新组（#{outcome['new_group']}）的异常。",
            "ok",
        )
    return redirect(url_for("trials.record_detail", trial_id=trial_id))


def expected_mismatch(expected_shown: bool, side: str) -> bool:
    """side: 'shown' / 'skipped'；返回该侧证据是否与版号规则不符。"""
    return (side == "shown") != bool(expected_shown)
