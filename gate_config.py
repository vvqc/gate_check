"""从 GitHub Secrets / Variables 或本地环境读取配置。"""

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent


class ConfigError(ValueError):
    """配置错误消息仅包含字段名，不包含 Secret 值。"""


@dataclass(frozen=True)
class Config:
    edge_hosts: tuple[str, ...]
    worker_check_url: str
    nodes_url: str
    public_dir: Path = REPO_DIR / "public"
    concurrency: int = 32
    connect_timeout: float = 10
    check_timeout: float = 90
    http_timeout: float = 30
    retries: int = 2
    retry_backoff: float = 1
    run_timeout: float = 900
    max_check_nodes: int = 0
    allowed_countries: tuple[str, ...] = ()
    residential_only: bool = False
    max_latency_ms: float = 0
    max_per_country: int = 0
    probe_edges: bool = True
    stale_hours: float = 6


def valid_domain(value):
    if len(value) > 253 or "." not in value:
        return False
    labels = value.split(".")
    return (
        all(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in labels
        )
        and not labels[-1].isdigit()
    )


def normalize_entry(entry):
    host, separator, port = entry.rpartition(":")
    error = "EDGE_HOSTS 每项必须是域名或 IP 加端口，端口范围为 1–65535"
    if not separator or not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
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


def number(env, name, default, minimum, maximum, integer=False):
    value = env.get(name, "").strip()
    try:
        parsed = (int(value) if integer else float(value)) if value else default
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise ValueError
    except ValueError:
        raise ConfigError(f"{name} 必须是 {minimum}–{maximum} 范围内的有效数值") from None
    return parsed


def boolean(env, name, default):
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    if value not in ("true", "false", "1", "0"):
        raise ConfigError(f"{name} 必须是 true 或 false")
    return value in ("true", "1")


def load_config(environ=None):
    env = os.environ if environ is None else environ
    entries = (part.strip() for part in re.split(r"[,\r\n]+", env.get("EDGE_HOSTS", "")))
    edges = tuple(dict.fromkeys(normalize_entry(part) for part in entries if part))
    if not edges:
        raise ConfigError("缺少 EDGE_HOSTS，请在 Actions Secrets 中填写入口列表")
    domain = env.get("WORKER_DOMAIN", "").strip().lower()
    if not domain:
        raise ConfigError("缺少 WORKER_DOMAIN，请在 Actions Secrets 中填写检测 Worker 域名")
    if not valid_domain(domain):
        raise ConfigError("WORKER_DOMAIN 只填写域名，不包含协议、端口、路径或查询参数")
    parts = env.get("GITHUB_REPOSITORY", "").strip().split("/")
    username_pattern = r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*"
    if (
        len(parts) != 2
        or not re.fullmatch(username_pattern, parts[0])
        or len(parts[0]) > 39
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1])
        or parts[1] in (".", "..")
    ):
        raise ConfigError("GITHUB_REPOSITORY 必须是 用户名/仓库名；Actions 会自动提供")
    username = env.get("NODES_GITHUB_USERNAME", "").strip() or parts[0]
    if len(username) > 39 or not re.fullmatch(username_pattern, username):
        raise ConfigError("NODES_GITHUB_USERNAME 必须是有效 GitHub 用户名")
    countries = tuple(
        dict.fromkeys(
            item.strip().upper()
            for item in re.split(r"[,\s]+", env.get("ALLOWED_COUNTRIES", ""))
            if item.strip()
        )
    )
    if any(not re.fullmatch(r"[A-Z]{2}", country) for country in countries):
        raise ConfigError("ALLOWED_COUNTRIES 使用逗号分隔的两位国家代码，例如 JP,US")
    return Config(
        edge_hosts=edges,
        worker_check_url=f"https://{domain}/check?sstp=vpn:vpn@",
        nodes_url=f"https://{username.lower()}.github.io/{parts[1]}/nodes.txt",
        concurrency=number(env, "CHECK_CONCURRENCY", 32, 1, 64, True),
        connect_timeout=number(env, "CONNECT_TIMEOUT", 10, 1, 60),
        check_timeout=number(env, "CHECK_TIMEOUT", 90, 1, 180),
        http_timeout=number(env, "HTTP_TIMEOUT", 30, 1, 120),
        retries=number(env, "CHECK_RETRIES", 2, 0, 3, True),
        retry_backoff=number(env, "RETRY_BACKOFF", 1, 0, 30),
        run_timeout=number(env, "RUN_TIMEOUT", 900, 10, 1500),
        max_check_nodes=number(env, "MAX_CHECK_NODES", 0, 0, 10000, True),
        allowed_countries=countries,
        residential_only=boolean(env, "RESIDENTIAL_ONLY", False),
        max_latency_ms=number(env, "MAX_LATENCY_MS", 0, 0, 180000),
        max_per_country=number(env, "MAX_PER_COUNTRY", 0, 0, 10000, True),
        probe_edges=boolean(env, "PROBE_EDGES", True),
        stale_hours=number(env, "STALE_HOURS", 6, 1, 720),
    )
