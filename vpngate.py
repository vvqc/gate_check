#!/usr/bin/env python3
"""抓取和检测 VPN Gate SSTP 节点，仅生成 edgetunnel 的 nodes.txt。

抓取、分类和节点格式参考 https://github.com/hezhanleiok/gate。
个人配置仅通过环境变量传入；导入本模块不会读取配置或发起网络请求。
"""

import base64
import binascii
import csv
import ipaddress
import math
import os
import re
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests


REPO_DIR = Path(__file__).resolve().parent
VPNGATE_API = "http://www.vpngate.net/api/iphone/"
VPNGATE_MIRROR = (
    "https://raw.githubusercontent.com/fdciabdul/"
    "Vpngate-Scraper-API/main/json/data.json"
)
CONCURRENCY = 32
CHECK_TIMEOUT = 90
HTTP_TIMEOUT = 60
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; gate-checker)"}

DATA_CENTER_ORG_KEYWORDS = (
    "GOOGLE", "AMAZON", "AWS", "MICROSOFT", "OVH", "HETZNER", "DIGITALOCEAN",
    "AKAMAI", "CLOUDFLARE", "FASTLY", "RACKSPACE", "EQUINIX", "LINODE", "VULTR",
    "HURRICANE", "TENCENT", "ALIBABA", "ALIYUN", "LEASWEB",
)
RESIDENTIAL_ORG_KEYWORDS = (
    "NTT EAST", "NTT WEST", "NTT COMMUNICATIONS", "NTT BROADBAND", "KDDI", "DOCOMO",
    "SOFTBANK", "AU COMMUNICATIONS", "J:COM", "JCOM", "OCN", "BIGLOBE",
    "IIJ", "SEIKO", "CLEVER-NET", "AT&T", "COMCAST", "XFINITY", "VERIZON",
    "TELUS", "ROGERS", "BELL CANADA", "VODAFONE", "ORANGE", "DEUTSCHE TELEKOM",
    "BREEZE", "TIM S.P.A", "LIBERO", "FASTWEB", "FREE FRANCE", "BT OPEN",
)
COUNTRY_ZH = {
    "JP": "日本", "KR": "韩国", "US": "美国", "CA": "加拿大", "RU": "俄罗斯",
    "RO": "罗马尼亚", "TH": "泰国", "VN": "越南", "DE": "德国", "FR": "法国",
    "GB": "英国", "UK": "英国", "SG": "新加坡", "TW": "台湾", "HK": "香港",
    "CN": "中国", "AU": "澳大利亚", "NL": "荷兰", "SE": "瑞典", "CH": "瑞士",
    "IT": "意大利", "ES": "西班牙", "PL": "波兰", "IN": "印度", "BR": "巴西",
    "MX": "墨西哥", "ID": "印度尼西亚", "MY": "马来西亚", "PH": "菲律宾",
    "TR": "土耳其", "UA": "乌克兰", "CZ": "捷克", "GR": "希腊", "PT": "葡萄牙",
    "FI": "芬兰", "NO": "挪威", "DK": "丹麦", "IE": "爱尔兰", "BE": "比利时",
    "AT": "奥地利", "HU": "匈牙利", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "NZ": "新西兰", "ZA": "南非", "IL": "以色列", "AE": "阿联酋", "SA": "沙特",
    "EG": "埃及", "HR": "克罗地亚", "BY": "白俄罗斯", "GD": "格林纳达",
    "LV": "拉脱维亚", "EE": "爱沙尼亚", "LT": "立陶宛", "SK": "斯洛伐克",
    "SI": "斯洛文尼亚", "BG": "保加利亚", "RS": "塞尔维亚", "GE": "格鲁吉亚",
    "MD": "摩尔多瓦", "AM": "亚美尼亚", "KZ": "哈萨克斯坦", "UZ": "乌兹别克斯坦",
    "MN": "蒙古", "NP": "尼泊尔", "LK": "斯里兰卡", "MM": "缅甸",
}


class ConfigError(ValueError):
    """配置缺失或格式错误；消息不包含配置值。"""


class PipelineError(RuntimeError):
    """本轮没有可发布的结果。"""


@dataclass(frozen=True)
class Config:
    edge_hosts: tuple[str, ...]
    worker_check_url: str
    nodes_url: str
    public_dir: Path = REPO_DIR / "public"


def valid_domain(value):
    if len(value) > 253 or "." not in value:
        return False
    labels = value.split(".")
    if not all(re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
               for label in labels):
        return False
    # 域名的最后一段不能全为数字，避免把错误的 IP 当作域名。
    return not labels[-1].isdigit()


def normalize_entry(entry):
    host, separator, port = entry.rpartition(":")
    error = "EDGE_HOSTS 每项必须是域名或 IP 加端口，端口范围为 1–65535"
    if not separator or not re.fullmatch(r"[0-9]{1,5}", port):
        raise ConfigError(error)
    if not 1 <= int(port) <= 65535:
        raise ConfigError(error)
    if host.startswith("[") and host.endswith("]"):
        try:
            host = f"[{ipaddress.IPv6Address(host[1:-1])}]"
        except ValueError:
            raise ConfigError(error) from None
    else:
        try:
            host = str(ipaddress.IPv4Address(host))
        except ValueError:
            if not valid_domain(host):
                raise ConfigError(error) from None
            host = host.lower()
    return f"{host}:{int(port)}"


def load_config(environ=None):
    env = os.environ if environ is None else environ
    entries = [part.strip() for part in re.split(r"[,\r\n]+", env.get("EDGE_HOSTS", ""))]
    edge_hosts = tuple(dict.fromkeys(normalize_entry(part) for part in entries if part))
    if not edge_hosts:
        raise ConfigError("缺少 EDGE_HOSTS，请在 Actions Secrets 中填写入口列表")

    domain = env.get("WORKER_DOMAIN", "").strip().lower()
    if not domain:
        raise ConfigError("缺少 WORKER_DOMAIN，请在 Actions Secrets 中填写检测 Worker 域名")
    if not valid_domain(domain):
        raise ConfigError("WORKER_DOMAIN 只填写域名，不包含协议、端口、路径或查询参数")

    repository = env.get("GITHUB_REPOSITORY", "").strip()
    parts = repository.split("/")
    if (len(parts) != 2 or not parts[0]
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1])
            or parts[1] in (".", "..")):
        raise ConfigError("GITHUB_REPOSITORY 必须是 用户名/仓库名；Actions 会自动提供")
    owner, repo_name = parts
    username = env.get("NODES_GITHUB_USERNAME", "").strip() or owner
    if len(username) > 39 or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", username):
        raise ConfigError("NODES_GITHUB_USERNAME 或 GITHUB_REPOSITORY 中的所有者必须是有效 GitHub 用户名")

    return Config(
        edge_hosts=edge_hosts,
        worker_check_url=f"https://{domain}/check?sstp=vpn:vpn@",
        nodes_url=f"https://{username.lower()}.github.io/{quote(repo_name, safe='')}/nodes.txt",
    )


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
        fields = {key.strip().lower(): (value or "").strip()
                  for key, value in record.items() if key is not None}
        if not fields.get("hostname") or not fields.get("ip"):
            continue
        rows.append({
            "host": fields["hostname"],
            "ip": fields["ip"],
            "country": fields.get("countrylong", ""),
            "country_code": fields.get("countryshort", ""),
            "config_b64": fields.get("openvpn_configdata_base64", ""),
        })
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
        rows.append({
            "host": host,
            "ip": ip,
            "country": str(server.get("countrylong") or server.get("country_long")
                           or server.get("country") or "").strip(),
            "country_code": str(server.get("countryshort") or server.get("country_short") or "").strip(),
            "config_b64": str(server.get("openvpn_configdata_base64") or server.get("config_b64") or "").strip(),
        })
    return rows


def fetch_vpngate():
    for label, url, parser in (
        ("官方 API", VPNGATE_API, lambda response: parse_csv(response.text)),
        ("镜像", VPNGATE_MIRROR, lambda response: parse_mirror_json(response.json())),
    ):
        try:
            response = requests.get(url, timeout=HTTP_TIMEOUT, headers=HEADERS)
            response.raise_for_status()
            rows = parser(response)
            if rows:
                print(f"[数据源] {label} 获取到 {len(rows)} 个原始节点", flush=True)
                return rows
            print(f"[数据源] {label} 返回空数据", flush=True)
        except (requests.RequestException, ValueError, csv.Error) as exc:
            print(f"[数据源] {label} 获取失败（{type(exc).__name__}）", flush=True)
    raise PipelineError("官方 API 与镜像均不可用，保留上次清单")


def to_sstp_nodes(rows):
    nodes = []
    for row in rows:
        try:
            config_text = base64.b64decode(row["config_b64"]).decode("utf-8", "replace")
        except (ValueError, binascii.Error):
            continue
        if not re.search(r"^proto\s+(?:tcp|tcp4|tcp6)(?:-client)?\s*$", config_text, re.M):
            continue
        remote = re.search(r"^remote\s+\S+\s+([0-9]+)\b", config_text, re.M)
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
        nodes.append({
            "host": host,
            "port": port,
            "country": row["country"],
            "country_code": code if re.fullmatch(r"[A-Z]{2}", code) else "?",
        })
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


def check_one(node, config):
    result = dict(node, success=False, worker_error=False, residential="unknown", latency_ms=None)
    url = config.worker_check_url + quote(f"{node['host']}:{node['port']}", safe="")
    try:
        response = requests.get(url, timeout=CHECK_TIMEOUT, headers=HEADERS)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("success"), bool):
            raise ValueError("检测 Worker 响应缺少布尔类型 success")
        result["success"] = payload["success"]
        if not result["success"]:
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
    except (requests.RequestException, ValueError):
        result["success"] = False
        result["worker_error"] = True
    return result


def check_all(nodes, config):
    # 每个请求独立管理连接，避免在线程间共享可变的 requests.Session。
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        return list(pool.map(lambda node: check_one(node, config), nodes))


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
        ordered = sorted(nodes, key=lambda node: (
            node.get("residential") != "residential",
            node.get("latency_ms") is None,
            node.get("latency_ms") or 0,
            node["host"],
            node["port"],
        ))
        for node in ordered:
            # 与参考项目一致：未识别的节点归入机房组，分类仅为估算。
            category = "住宅" if node.get("residential") == "residential" else "机房"
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
            mode="w", encoding="utf-8", newline="\n", dir=public_dir,
            prefix=".nodes-", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def run(config):
    rows = fetch_vpngate()
    nodes = dedupe(to_sstp_nodes(rows))
    if not nodes:
        raise PipelineError("没有解析出 SSTP(TCP) 节点，保留上次清单")
    print(f"[检测] 去重后 {len(nodes)} 个节点，并发 {CONCURRENCY}，超时 {CHECK_TIMEOUT}s", flush=True)
    started = time.monotonic()
    results = check_all(nodes, config)
    success_count = sum(result["success"] for result in results)
    error_count = sum(result["worker_error"] for result in results)
    print(
        f"[检测] 可用 {success_count}/{len(results)}，Worker 异常 {error_count}，"
        f"耗时 {time.monotonic() - started:.1f}s", flush=True,
    )
    if error_count == len(results):
        raise PipelineError("Worker 全部请求异常，保留上次清单")
    text = build_nodes_text(results, config.edge_hosts)
    destination = write_nodes(text, config.public_dir)
    print(
        f"[生成] {destination.name}，入口 {len(config.edge_hosts)} 个，SSTP 节点 {success_count} 个",
        flush=True,
    )
    print(f"[订阅] {config.nodes_url}", flush=True)
    return destination


def main(environ=None):
    try:
        config = load_config(environ)
        run(config)
    except (ConfigError, PipelineError) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # 第三方异常可能带有请求 URL，不将其原文或配置值输出到日志。
        print(f"[失败] 程序异常（{type(exc).__name__}），未完成发布", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
