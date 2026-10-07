#!/usr/bin/env python3
"""VPN Gate SSTP 检测和清单生成；个人配置仅从环境变量读取。

抓取、分类和节点格式参考 https://github.com/hezhanleiok/gate。
"""

import argparse
import base64
import binascii
import csv
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from gate_config import REPO_DIR as REPO_DIR
from gate_config import Config as Config
from gate_config import ConfigError as ConfigError
from gate_config import load_config as load_config
from gate_config import normalize_entry as normalize_entry
from gate_config import valid_domain as valid_domain
from gate_data import COUNTRY_ZH, DATA_CENTER_ORG_KEYWORDS, RESIDENTIAL_ORG_KEYWORDS
from gate_http import HttpClient, RequestFailure
from gate_report import (
    REPORT_DIR,
    fresh_report,
    read_json,
    save_report,
    summarize_results,
    timestamp,
)

VPNGATE_API = "http://www.vpngate.net/api/iphone/"
VPNGATE_MIRROR = (
    "https://raw.githubusercontent.com/fdciabdul/Vpngate-Scraper-API/main/json/data.json"
)


class PipelineError(RuntimeError):
    """本轮不可发布；错误仅使用可公开的原因。"""


def parse_csv(text):
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if line.lstrip("#").lower().startswith("hostname,"):
            lines[index] = line.lstrip("#")
            reader = csv.DictReader(lines[index:])
            break
    else:
        raise ValueError("找不到 VPN Gate CSV 表头")

    rows = []
    for record in reader:
        fields = {
            key.strip().lower(): (value or "").strip()
            for key, value in record.items()
            if key is not None
        }
        if not fields.get("hostname") or not fields.get("ip"):
            continue
        rows.append(
            {
                "host": fields["hostname"],
                "ip": fields["ip"],
                "country": fields.get("countrylong", ""),
                "country_code": fields.get("countryshort", ""),
                "config_b64": fields.get("openvpn_configdata_base64", ""),
            }
        )
    return rows


def parse_mirror_json(data):
    servers = []
    for item in data if isinstance(data, list) else [data]:
        if isinstance(item, dict):
            if isinstance(item.get("servers"), list):
                servers.extend(item["servers"])
            else:
                servers.append(item)
    rows = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        host = str(server.get("hostname") or server.get("host") or "").strip()
        ip = str(server.get("ip") or "").strip()
        if not host or not ip:
            continue
        rows.append(
            {
                "host": host,
                "ip": ip,
                "country": str(
                    server.get("countrylong")
                    or server.get("country_long")
                    or server.get("country")
                    or ""
                ).strip(),
                "country_code": str(
                    server.get("countryshort") or server.get("country_short") or ""
                ).strip(),
                "config_b64": str(
                    server.get("openvpn_configdata_base64") or server.get("config_b64") or ""
                ).strip(),
            }
        )
    return rows


def fetch_vpngate(config=None, client=None, report=None):
    config = config or Config((), "", "", retries=0)
    if client is None:
        with HttpClient(config) as own_client:
            return fetch_vpngate(config, own_client, report)
    for label, url, parser in (
        ("official", VPNGATE_API, lambda response: parse_csv(response.text)),
        ("mirror", VPNGATE_MIRROR, lambda response: parse_mirror_json(response.json())),
    ):
        try:
            response = client.get(url, read_timeout=config.http_timeout, limit=16 * 1024 * 1024)
            rows = parser(response)
            # 有数据但无法解析出候选节点也要尝试镜像。
            if rows and to_sstp_nodes(rows):
                if report is not None:
                    report.update(source=label, raw_nodes=len(rows))
                print(f"[数据源] {label} 获取到 {len(rows)} 个原始节点", flush=True)
                return rows
            print(f"[数据源] {label} 无有效 SSTP 候选节点", flush=True)
        except (RequestFailure, ValueError, csv.Error) as exc:
            code = exc.code if isinstance(exc, RequestFailure) else "invalid_source"
            if report is not None:
                report["warnings"].append(f"数据源 {label}: {code}")
            print(f"[数据源] {label} 失败（{code}）", flush=True)
            if code == "deadline_exceeded":
                raise
    raise PipelineError("官方 API 与镜像均不可用或无有效候选节点，保留上次清单")


def to_sstp_nodes(rows):
    nodes = []
    for row in rows:
        try:
            config_text = base64.b64decode(row["config_b64"]).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            continue
        if not re.search(r"^proto\s+(?:tcp|tcp4|tcp6)(?:-client)?\s*$", config_text, re.M):
            continue
        remote = re.search(r"^remote\s+\S+\s+([0-9]{1,5})(?=\s|$)", config_text, re.M)
        if not remote:
            continue
        port = int(remote.group(1))
        if not 1 <= port <= 65535:
            continue
        host = row["host"].lower()
        if not host.endswith(".opengw.net"):
            host += ".opengw.net"
        if not valid_domain(host):
            continue
        code = row["country_code"].upper()
        nodes.append(
            {
                "host": host,
                "port": port,
                "country": row["country"],
                "country_code": code if re.fullmatch(r"[A-Z]{2}", code) else "?",
            }
        )
    return nodes


def dedupe(nodes):
    unique = {}
    for node in nodes:
        unique.setdefault((node["host"].lower(), node["port"]), node)
    return list(unique.values())


def classify_network(host, exit_org, is_datacenter=None):
    if is_datacenter is True:
        return "datacenter"
    if is_datacenter is False:
        return "residential"
    org = (exit_org or "").upper()
    if any(keyword in org for keyword in DATA_CENTER_ORG_KEYWORDS):
        return "datacenter"
    if any(keyword in org for keyword in RESIDENTIAL_ORG_KEYWORDS):
        return "residential"
    if host.lower().startswith("public-vpn"):
        return "datacenter"
    if re.match(r"^(?:vpn[0-9]{5,}|vpnv[0-9]+)", host.lower()):
        return "residential"
    return "unknown"


def check_one(node, config, client=None):
    if client is None:
        with HttpClient(config) as own_client:
            return check_one(node, config, own_client)
    result = dict(node, success=False, worker_error=False, residential="unknown", latency_ms=None)
    url = config.worker_check_url + quote(f"{node['host']}:{node['port']}", safe="")
    try:
        response = client.get(url)
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            raise ValueError("invalid_worker_response")
        result["success"] = payload["success"]
        if not result["success"]:
            result["error_code"] = "node_unavailable"
            return result
        try:
            latency = float(payload.get("responseTime"))
            if math.isfinite(latency) and latency >= 0:
                result["latency_ms"] = latency
        except (ValueError, TypeError):
            pass
        exit_info = payload.get("exit")
        exit_info = exit_info if isinstance(exit_info, dict) else {}
        asn = exit_info.get("asn")
        asn = asn if isinstance(asn, dict) else {}
        org = str(asn.get("org") or asn.get("name") or "")
        result["residential"] = classify_network(node["host"], org, exit_info.get("is_datacenter"))
    except (RequestFailure, ValueError) as exc:
        result.update(
            success=False,
            worker_error=True,
            error_code=exc.code if isinstance(exc, RequestFailure) else "invalid_worker_response",
        )
    return result


def check_all(nodes, config, client=None, report=None):
    if client is None:
        with HttpClient(config) as own_client:
            return check_all(nodes, config, own_client, report)
    results = []
    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        pending = {pool.submit(check_one, node, config, client): node for node in nodes}
        for future in as_completed(pending):
            results.append(future.result())
            if report is not None:
                summarize_results(report, results, [])
                save_report(report)
    return results


def select_nodes(results, config):
    selected = []
    for node in results:
        if not node.get("success"):
            continue
        if config.allowed_countries and node["country_code"] not in config.allowed_countries:
            continue
        if config.residential_only and node.get("residential") != "residential":
            continue
        if config.max_latency_ms and (
            node.get("latency_ms") is None or node["latency_ms"] > config.max_latency_ms
        ):
            continue
        selected.append(node)
    selected.sort(
        key=lambda node: (
            node.get("residential") != "residential",
            node.get("latency_ms") is None,
            node.get("latency_ms") or 0,
            node["host"],
            node["port"],
        )
    )
    counts = Counter()
    limited = []
    for node in selected:
        code = node["country_code"]
        if config.max_per_country and counts[code] >= config.max_per_country:
            continue
        limited.append(node)
        counts[code] += 1
    return limited


def probe_edges(config):
    def probe(item):
        index, entry = item
        host, _, port = entry.rpartition(":")
        started = time.monotonic()
        try:
            with socket.create_connection(
                (host.strip("[]"), int(port)), timeout=min(config.connect_timeout, 5)
            ):
                return {
                    "index": index,
                    "status": "tcp_ok",
                    "latency_ms": round((time.monotonic() - started) * 1000, 1),
                }
        except socket.gaierror:
            return {"index": index, "status": "dns_error"}
        except TimeoutError:
            return {"index": index, "status": "connect_timeout"}
        except OSError:
            return {"index": index, "status": "connection_error"}

    with ThreadPoolExecutor(max_workers=min(config.concurrency, 8)) as pool:
        return list(pool.map(probe, enumerate(config.edge_hosts, 1)))


def build_nodes_text(results, edge_hosts):
    """先列出全部配置入口，再追加使用这些入口轮换的 SSTP 节点。"""
    countries = defaultdict(list)
    for node in results:
        if node.get("success"):
            countries[node["country_code"]].append(node)
    if not countries:
        raise PipelineError("没有可用节点，保留上次清单")

    lines = []
    for code, nodes in sorted(countries.items(), key=lambda item: (-len(item[1]), item[0])):
        country = COUNTRY_ZH.get(code, code if code != "?" else "未知")
        counters = defaultdict(int)
        ordered = sorted(
            nodes,
            key=lambda node: (
                node.get("residential") != "residential",
                node.get("latency_ms") is None,
                node.get("latency_ms") or 0,
                node["host"],
                node["port"],
            ),
        )
        for node in ordered:
            category = {"residential": "住宅", "datacenter": "机房"}.get(
                node.get("residential"), "未识别"
            )
            counters[category] += 1
            entry = edge_hosts[len(lines) % len(edge_hosts)]
            lines.append(
                f"{entry}#{country}-{category}-{counters[category]:02d}"
                f"$sstp://vpn:vpn@{node['host']}:{node['port']}"
            )
    return "\n".join([*edge_hosts, *lines]) + "\n"


def write_nodes(text, public_dir):
    if not text.strip():
        raise PipelineError("拒绝写入空清单，保留上次清单")
    public_dir.mkdir(parents=True, exist_ok=True)
    destination = public_dir / "nodes.txt"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=public_dir,
            prefix=".nodes-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def run(config, report=None):
    current = report if report is not None else fresh_report()
    current.update(edge_count=len(config.edge_hosts), stage="source")
    if report is not None:
        save_report(current)
    started = time.monotonic()
    retry_counts = Counter()
    retry_lock = threading.Lock()

    def on_retry(code):
        with retry_lock:
            retry_counts[code] += 1
            current["retries"] = dict(retry_counts)

    try:
        with HttpClient(config, on_retry=on_retry) as client:
            rows = fetch_vpngate(config, client, current)
            nodes = dedupe(to_sstp_nodes(rows))
            if config.max_check_nodes:
                nodes = nodes[: config.max_check_nodes]
            current.update(candidates=len(nodes), stage="worker")
            if report is not None:
                save_report(current)
            print(f"[检测] {len(nodes)} 个候选节点，并发 {config.concurrency}", flush=True)
            results = check_all(nodes, config, client, current if report is not None else None)
            selected = select_nodes(results, config)
            summarize_results(current, results, selected)
            if results and all(result.get("worker_error") for result in results):
                raise PipelineError("Worker 全部请求异常，保留上次清单")
            if not selected:
                raise PipelineError("没有符合筛选条件的可用 SSTP 节点，保留上次清单")
            if config.probe_edges:
                current["stage"] = "edge_probes"
                if report is not None:
                    save_report(current)
                current["edge_probes"] = probe_edges(config)
            client.remaining()
        current["stage"] = "generate"
        destination = write_nodes(build_nodes_text(selected, config.edge_hosts), config.public_dir)
        current.update(status="success", stage="generated", checked_at=timestamp())
        print(
            f"[生成] {destination.name}，入口 {len(config.edge_hosts)} 个，SSTP 节点 {len(selected)} 个",
            flush=True,
        )
        print(f"[订阅] {config.nodes_url}", flush=True)
        return destination
    finally:
        current["elapsed_seconds"] = round(time.monotonic() - started, 2)
        if report is not None:
            save_report(current)


def main(environ=None):
    report = fresh_report()
    try:
        config = load_config(environ)
        run(config, report)
    except (ConfigError, PipelineError, RequestFailure) as exc:
        report.update(status="failed", failure_code=str(exc))
        report["warnings"].append(str(exc))
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        report.update(status="failed", failure_code=type(exc).__name__)
        print(f"[失败] 程序异常（{type(exc).__name__}），未完成发布", file=sys.stderr)
        return 1
    finally:
        report["finished_at"] = timestamp()
        save_report(report)
    return 0


def supervise(command, timeout):
    """独立子进程执行检测，期限到达后连同其线程一并终止。"""
    with subprocess.Popen(command, start_new_session=(os.name == "posix")) as process:
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            return 124


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true", help="仅校验配置，不联网")
    parser.add_argument("--execute", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.execute:
        return main()
    try:
        config = load_config()
    except ConfigError as exc:
        report = fresh_report()
        report.update(status="failed", failure_code="configuration", warnings=[str(exc)])
        save_report(report)
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    if args.check_config:
        print(f"[配置] 校验通过，共 {len(config.edge_hosts)} 个入口；未发起网络请求")
        return 0
    save_report(fresh_report())
    started = time.monotonic()
    code = supervise(
        [sys.executable, str(Path(__file__).resolve()), "--execute"], config.run_timeout
    )
    report = read_json(REPORT_DIR / "run.json", fresh_report())
    if code == 124:
        report.update(
            status="failed",
            failure_code="deadline_exceeded",
            finished_at=timestamp(),
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        report["warnings"].append("本轮总时间预算耗尽，已停止检测，不发布新清单")
    elif code and report.get("status") != "failed":
        report.update(status="failed", failure_code="process_exit")
    save_report(report)
    return code


if __name__ == "__main__":
    sys.exit(cli())
