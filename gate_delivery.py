"""Pages 内容比较、发布验证和跨运行报告；不向公开站点写入状态文件。"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from urllib.parse import quote

from gate_config import REPO_DIR, Config, normalize_entry, valid_domain
from gate_http import HttpClient, RequestFailure
from gate_report import (
    REPORT_DIR,
    age_hours,
    atomic_json,
    fresh_report,
    read_json,
    save_report,
    timestamp,
)


def validate_nodes(content):
    """校验独立入口与 edgetunnel SSTP 行的共同格式。"""
    text = content.decode("utf-8")
    entries = chains = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if "$sstp://" not in line:
            normalize_entry(line)
            entries += 1
            continue
        entry_label, proxy = line.split("$sstp://")
        entry, label = entry_label.split("#", 1)
        normalize_entry(entry)
        if not label or not proxy.startswith("vpn:vpn@"):
            raise ValueError("invalid_nodes_format")
        host, separator, port = proxy[8:].rpartition(":")
        if (
            not separator
            or not valid_domain(host)
            or not re.fullmatch(r"[0-9]{1,5}", port)
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError("invalid_nodes_format")
        chains += 1
    if not entries or not chains:
        raise ValueError("nodes_require_entries_and_sstp")
    return entries, chains


def delivery_client():
    return HttpClient(
        Config((), "", "", connect_timeout=5, check_timeout=15, retries=1, run_timeout=180)
    )


def history(directory=REPORT_DIR):
    result = {"known": True, "previous": {}, "warnings": []}
    repository = os.environ["GITHUB_REPOSITORY"]
    branch = quote(os.environ.get("GITHUB_REF_NAME", "main"), safe="")
    current_run = os.environ.get("GITHUB_RUN_ID")
    token = os.environ.get("GH_TOKEN", "")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base = f"https://api.github.com/repos/{repository}"
    try:
        with delivery_client() as client:
            runs = client.get(
                f"{base}/actions/workflows/check.yml/runs?branch={branch}&status=completed&per_page=20",
                headers=headers,
            ).json()["workflow_runs"]
            missing_reports = 0
            for run in runs:
                if str(run["id"]) == current_run:
                    continue
                artifacts = client.get(
                    f"{base}/actions/runs/{run['id']}/artifacts", headers=headers
                ).json()["artifacts"]
                candidates = [
                    item
                    for item in artifacts
                    if not item["expired"] and item["name"].startswith(f"gate-report-{run['id']}-")
                ]
                for artifact in sorted(candidates, key=lambda item: item["id"], reverse=True):
                    archive = client.get(
                        f"{base}/actions/artifacts/{artifact['id']}/zip",
                        headers=headers,
                        limit=2 * 1024 * 1024,
                    ).content
                    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                        if bundle.getinfo("run.json").file_size > 1024 * 1024:
                            raise ValueError("oversize_report")
                        previous = json.loads(bundle.read("run.json"))
                    if previous.get("schema") != 1:
                        continue
                    # 仅恢复明确的历史状态字段，不复用外部报告里的文字或配置。
                    safe = {}
                    for key in ("last_success_at", "last_published_at"):
                        value = previous.get(key)
                        safe[key] = value if age_hours(value) is not None else None
                    count = previous.get("consecutive_failures")
                    safe["consecutive_failures"] = (
                        count if isinstance(count, int) and 0 <= count <= 100000 else None
                    )
                    result["previous"] = safe
                    if missing_reports:
                        result["known"] = False
                        result["warnings"].append("近期有运行缺少报告，连续失败次数无法完整还原")
                    atomic_json(directory / "history.json", result)
                    return result
                missing_reports += 1
            if missing_reports:
                result.update(known=False, warnings=["历史运行没有可用报告，本轮起开始记录"])
    except (RequestFailure, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        result.update(known=False, warnings=["读取历史报告失败，旧清单时效与连续失败次数可能未知"])
    atomic_json(directory / "history.json", result)
    return result


def live_url():
    base = os.environ["PAGES_BASE_URL"].rstrip("/")
    if not base.startswith("https://"):
        raise ValueError("invalid_pages_url")
    return f"{base}/nodes.txt"


def fetch_live(client, url):
    separator = "&" if "?" in url else "?"
    return client.get(
        f"{url}{separator}gate_check={time.time_ns()}",
        headers={"Cache-Control": "no-cache"},
        limit=16 * 1024 * 1024,
    ).content


def compare(path=REPO_DIR / "public" / "nodes.txt", directory=REPORT_DIR):
    expected = path.read_bytes()
    validate_nodes(expected)
    result = {"changed": True, "sha256": hashlib.sha256(expected).hexdigest()}
    try:
        with delivery_client() as client:
            actual = fetch_live(client, live_url())
            result["changed"] = actual != expected
    except RequestFailure as exc:
        result["comparison_error"] = exc.code
    if not result["changed"]:
        result["verified_at"] = timestamp()
    atomic_json(directory / "publication.json", result)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"changed={str(result['changed']).lower()}\n")
    print(
        "[发布] 内容有变化或旧文件不可用" if result["changed"] else "[发布] 线上内容一致，跳过部署"
    )
    return result


def verify(
    path=REPO_DIR / "public" / "nodes.txt", directory=REPORT_DIR, attempts=6, sleep=time.sleep
):
    expected = path.read_bytes()
    validate_nodes(expected)
    result = read_json(directory / "publication.json", {"changed": True})
    failure = "content_mismatch"
    with delivery_client() as client:
        for attempt in range(attempts):
            try:
                if fetch_live(client, live_url()) == expected:
                    result.update(
                        verified_at=timestamp(), sha256=hashlib.sha256(expected).hexdigest()
                    )
                    result.pop("verification_error", None)
                    atomic_json(directory / "publication.json", result)
                    print("[验证] 线上 nodes.txt 与本轮生成内容一致")
                    return True
                failure = "content_mismatch"
            except RequestFailure as exc:
                failure = exc.code
            if attempt + 1 < attempts:
                sleep(3)
    result["verification_error"] = failure
    atomic_json(directory / "publication.json", result)
    print(f"[验证失败] {failure}", file=sys.stderr)
    return False


def finalize(directory=REPORT_DIR, environ=None):
    env = os.environ if environ is None else environ
    report = read_json(directory / "run.json", fresh_report())
    historical = read_json(directory / "history.json", {"known": False, "previous": {}})
    previous = historical.get("previous", {})
    publication = read_json(directory / "publication.json")
    report["step_outcomes"] = {
        name: env.get(key, "unknown")
        for name, key in (
            ("检测", "CHECK_OUTCOME"),
            ("Pages 配置", "PAGES_OUTCOME"),
            ("内容比较", "COMPARE_OUTCOME"),
            ("部署", "DEPLOY_OUTCOME"),
            ("线上验证", "VERIFY_OUTCOME"),
        )
    }
    check_ok = env.get("CHECK_OUTCOME") == "success" and report.get("status") == "success"
    compared = env.get("COMPARE_OUTCOME") == "success"
    deployed = env.get("DEPLOY_OUTCOME") == "success"
    verified = env.get("VERIFY_OUTCOME") == "success"
    unchanged = (
        compared and publication.get("changed") is False and bool(publication.get("verified_at"))
    )
    success = check_ok and (
        unchanged or (deployed and verified and bool(publication.get("verified_at")))
    )
    report["last_success_at"] = (
        publication.get("verified_at") if success else previous.get("last_success_at")
    )
    report["last_published_at"] = timestamp() if deployed else previous.get("last_published_at")
    previous_count = previous.get("consecutive_failures", 0)
    report["consecutive_failures"] = (
        0
        if success
        else previous_count + 1
        if historical.get("known") and isinstance(previous_count, int)
        else None
    )
    report["status"] = "success" if success else "failed"
    report["publication"] = (
        "内容相同，已验证并跳过部署"
        if success and unchanged
        else "已部署且内容验证通过"
        if success
        else "已部署但内容验证未通过"
        if deployed
        else "未完成发布，保留此前站点"
    )
    report["warnings"].extend(historical.get("warnings", []))
    if publication.get("verification_error"):
        report["warnings"].append(f"发布验证失败：{publication['verification_error']}")
    if not report["last_success_at"]:
        report["warnings"].append("没有成功检测并验证的历史记录")
    stale = age_hours(report["last_success_at"])
    try:
        threshold = float(env.get("STALE_HOURS") or 6)
    except ValueError:
        threshold = 6
    if stale is not None:
        report["hours_since_success"] = round(stale, 1)
        if stale >= threshold:
            report["warnings"].append(f"距上次成功检测并验证已 {stale:.1f} 小时，清单可能已过期")
    report["finished_at"] = timestamp()
    save_report(report, directory, summary=True)
    return success


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("history", "compare", "verify", "finalize"))
    args = parser.parse_args()
    try:
        if args.command == "history":
            history()
        elif args.command == "compare":
            compare()
        elif args.command == "verify":
            return 0 if verify() else 1
        else:
            return 0 if finalize() else 1
    except Exception as exc:
        print(f"[发布失败] {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
