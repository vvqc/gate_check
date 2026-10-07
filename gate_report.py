"""无敏感地址的运行状态、JSON 产物与 GitHub Actions 摘要。"""

import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPORT_DIR = Path(os.environ.get("GATE_REPORT_DIR", Path(__file__).resolve().parent / "reports"))


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fresh_report():
    return {
        "schema": 1,
        "started_at": timestamp(),
        "status": "running",
        "stage": "startup",
        "source": None,
        "raw_nodes": 0,
        "candidates": 0,
        "checked": 0,
        "available": 0,
        "selected": 0,
        "edge_count": 0,
        "errors": {},
        "retries": {},
        "countries": {},
        "networks": {},
        "edge_probes": [],
        "warnings": [],
    }


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".report-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def summarize_results(report, results, selected):
    report.update(
        checked=len(results),
        available=sum(node.get("success", False) for node in results),
        selected=len(selected),
        errors=dict(Counter(node["error_code"] for node in results if node.get("error_code"))),
        countries=dict(Counter(node["country_code"] for node in selected)),
        networks=dict(Counter(node["residential"] for node in selected)),
    )


def age_hours(value):
    if not value:
        return None
    try:
        return max(
            0, (datetime.now(timezone.utc) - datetime.fromisoformat(value)).total_seconds() / 3600
        )
    except (ValueError, TypeError):
        return None


def markdown(report):
    rows = [
        ("状态", report.get("status", "unknown")),
        ("阶段", report.get("stage", "unknown")),
        ("失败原因", report.get("failure_code", "—")),
        ("数据源", report.get("source") or "—"),
        ("独立入口数", report.get("edge_count", 0)),
        ("候选 / 已检测", f"{report.get('candidates', 0)} / {report.get('checked', 0)}"),
        ("可用 / 筛选后", f"{report.get('available', 0)} / {report.get('selected', 0)}"),
        ("耗时（秒）", report.get("elapsed_seconds", "—")),
        ("发布结果", report.get("publication", "本地运行，未发布")),
        ("上次成功检测并验证", report.get("last_success_at") or "无记录"),
        ("上次部署时间", report.get("last_published_at") or "无记录"),
        (
            "连续失败次数",
            report.get("consecutive_failures")
            if report.get("consecutive_failures") is not None
            else "未知（无完整历史）",
        ),
        ("距上次成功验证（小时）", report.get("hours_since_success", "—")),
    ]
    output = ["## VPN Gate 运行报告", "", "| 项目 | 结果 |", "| --- | --- |"]
    output.extend(f"| {label} | {value} |" for label, value in rows)
    if report.get("step_outcomes"):
        output += [
            "",
            "**工作流步骤**："
            + "、".join(f"{name}: {outcome}" for name, outcome in report["step_outcomes"].items()),
        ]
    for key, title in (
        ("countries", "国家分布"),
        ("networks", "网络类型"),
        ("errors", "失败分类"),
        ("retries", "重试分类"),
    ):
        if report.get(key):
            output += [
                "",
                f"**{title}**："
                + "、".join(f"{label}: {count}" for label, count in sorted(report[key].items())),
            ]
    if report.get("edge_probes"):
        output += [
            "",
            "**入口 TCP 探测（运行服务器视角，不改变清单）**",
            "",
            "| 入口序号 | 状态 | TCP 连接耗时（ms） |",
            "| --- | --- | --- |",
        ]
        output.extend(
            f"| {item['index']} | {item['status']} | {item.get('latency_ms', '—')} |"
            for item in report["edge_probes"]
        )
    for warning in report.get("warnings", []):
        output += ["", f"> {warning}"]
    return "\n".join(output) + "\n"


def save_report(report, directory=REPORT_DIR, summary=False):
    atomic_json(directory / "run.json", report)
    text = markdown(report)
    (directory / "summary.md").write_text(text, encoding="utf-8")
    if summary and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(text)
