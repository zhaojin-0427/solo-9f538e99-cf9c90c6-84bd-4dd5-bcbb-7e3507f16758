# -*- coding: utf-8 -*-
"""Flask 核查页应用工厂。

会话数据（存于签名 cookie session）：
- config_json：静态配置（筛查项、追问题条件、纳入条件）
- answers：当前已确认的筛查项答案（含旧答卷已答项 + 后续电话补问项）
- last_submission：最近一次被拒绝的提交，用于“保留提交前结果 + 冲突提示”
"""
from __future__ import annotations

import json
import os

from flask import Flask, flash, redirect, render_template, request, session, url_for

from .engine import (
    AnalysisConfig,
    ConfigError,
    analyze,
    build_config,
    config_to_jsonable,
    replace_followups,
    validate_archive,
)


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me-in-production")

    def get_state():
        cfg_json = session.get("config_json")
        cfg = build_config(cfg_json) if cfg_json else None
        answers = session.get("answers", {})
        return cfg, answers

    def require_state():
        """供 POST 路由使用：会话损坏时清空并返回 None。"""
        try:
            cfg, answers = get_state()
        except ConfigError as exc:
            session.clear()
            flash(f"会话中的配置已损坏，已重置：{exc}", "error")
            return None, {}
        if cfg is None:
            flash("请先建立核查配置。", "error")
        return cfg, answers

    def save_config(cfg: AnalysisConfig):
        session["config_json"] = config_to_jsonable(cfg)

    def read_upload(upload, field: str) -> str:
        try:
            return upload.read().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{field} 不是 UTF-8 编码的文本文件：{exc}") from exc

    @app.route("/")
    def index():
        cfg = None
        if session.get("config_json"):
            try:
                cfg, answers = get_state()
            except ConfigError as exc:
                session.clear()
                flash(f"会话中的配置已损坏，已重置：{exc}", "error")
                cfg = None
        if cfg is None:
            answers, result = {}, None
        else:
            result = analyze(cfg, answers)
        return render_template(
            "index.html",
            cfg=cfg,
            answers=answers,
            result=result,
            last_submission=session.get("last_submission"),
        )

    @app.route("/setup", methods=["POST"])
    def setup():
        """接收配置文本（JSON）或上传文件，建立核查任务并可附带旧答卷。"""
        raw_text = request.form.get("config_text", "").strip()
        upload = request.files.get("config_file")
        if upload and upload.filename:
            try:
                raw_text = read_upload(upload, "配置文件")
            except ConfigError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))
        if not raw_text:
            flash("请粘贴配置 JSON 或上传文件。", "error")
            return redirect(url_for("index"))
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            flash(f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）", "error")
            return redirect(url_for("index"))
        try:
            # 兼容两种载荷：纯配置，或 {config: {...}, archive: {...}}
            if isinstance(payload, dict) and "config" in payload:
                config_raw = payload["config"]
                archive_raw = payload.get("archive")
            else:
                config_raw = payload
                archive_raw = None
            cfg = build_config(config_raw)
            answers = {}
            if archive_raw is not None:
                answers, rebuilt = validate_archive(archive_raw, cfg)
                cfg = replace_followups(cfg, rebuilt)
        except ConfigError as exc:
            flash(f"配置/答卷校验失败：{exc}", "error")
            return redirect(url_for("index"))

        session.clear()
        save_config(cfg)
        session["answers"] = answers
        flash("核查任务已建立。", "ok")
        return redirect(url_for("index"))

    @app.route("/archive", methods=["POST"])
    def upload_archive():
        """在已有配置上单独上传/替换旧答卷。"""
        cfg, _ = require_state()
        if cfg is None:
            return redirect(url_for("index"))
        upload = request.files.get("archive_file")
        raw_text = request.form.get("archive_text", "").strip()
        if upload and upload.filename:
            try:
                raw_text = read_upload(upload, "答卷文件")
            except ConfigError as exc:
                flash(str(exc), "error")
                return redirect(url_for("index"))
        if not raw_text:
            flash("请粘贴答卷 JSON 或上传文件。", "error")
            return redirect(url_for("index"))
        try:
            archive_raw = json.loads(raw_text)
            answers, rebuilt = validate_archive(archive_raw, cfg)
        except json.JSONDecodeError as exc:
            flash(f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）", "error")
            return redirect(url_for("index"))
        except ConfigError as exc:
            flash(f"答卷校验失败：{exc}", "error")
            return redirect(url_for("index"))
        cfg = replace_followups(cfg, rebuilt)
        save_config(cfg)
        session["answers"] = answers
        session.pop("last_submission", None)
        flash("旧答卷已载入，跳题记录与答案已用于约束判定。", "ok")
        return redirect(url_for("index"))

    @app.route("/answer", methods=["POST"])
    def submit_answer():
        """补问任一未答筛查项；冲突则拒绝并保留提交前结果。"""
        cfg, answers = require_state()
        if cfg is None:
            return redirect(url_for("index"))
        name = request.form.get("item", "").strip()
        value_raw = request.form.get("value", "").strip().lower()
        if name not in cfg.items:
            flash(f"筛查项 {name} 不在配置内。", "error")
            return redirect(url_for("index"))
        if name in answers:
            flash(f"筛查项 {cfg.labels.get(name, name)} 已有答案，不能重复补问。", "error")
            return redirect(url_for("index"))
        if value_raw not in ("true", "false"):
            flash("请选择“是”或“否”。", "error")
            return redirect(url_for("index"))
        value = value_raw == "true"

        trial = dict(answers)
        trial[name] = value
        result = analyze(cfg, trial)
        if not result.satisfiable:
            session["last_submission"] = {
                "item": name,
                "value": value,
                "conflicts": [c.description for c in result.conflicting],
            }
            flash(
                f"该回答与旧档记录冲突，已拒绝并保留提交前结果："
                f"{cfg.labels.get(name, name)} = {'是' if value else '否'}。",
                "error",
            )
            return redirect(url_for("index"))

        session["answers"] = trial
        session.pop("last_submission", None)
        flash(f"已记录补问答案：{cfg.labels.get(name, name)} = {'是' if value else '否'}。", "ok")
        return redirect(url_for("index"))

    @app.route("/reset", methods=["POST"])
    def reset():
        session.clear()
        flash("会话已重置。", "ok")
        return redirect(url_for("index"))

    @app.route("/reset-answers", methods=["POST"])
    def reset_answers():
        cfg, _ = require_state()
        if cfg is None:
            return redirect(url_for("index"))
        session["answers"] = {}
        session.pop("last_submission", None)
        flash("已清空全部筛查项答案（保留配置与跳题记录）。", "ok")
        return redirect(url_for("index"))

    @app.template_filter("yn")
    def yn(value):
        return "是" if value else "否"

    return app
