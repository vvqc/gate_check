import base64
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import requests

import vpngate as gate

ENV = {
    "EDGE_HOSTS": "a.example.com:443,b.example.com:8443",
    "WORKER_DOMAIN": "private-check.example.com",
    "GITHUB_REPOSITORY": "example-owner/gate_check",
}


def encoded_config(proto="tcp", port=443):
    return base64.b64encode(f"proto {proto}\nremote 192.0.2.1 {port}\n".encode()).decode()


def source_csv(*configs):
    header = "#HostName,IP,Score,Ping,Speed,CountryLong,CountryShort,OpenVPN_ConfigData_Base64\n"
    rows = [
        f"vpn12345,192.0.2.1,100,2,1000000,Japan,JP,{config}"
        for config in (configs or (encoded_config(),))
    ]
    return "*vpn_servers\n" + header + "\n".join(rows) + "\n*\n"


def response(text="", payload=None, error=None):
    result = requests.Response()
    result.status_code = 200
    if error is not None:
        match = re.search(r"[45][0-9]{2}", str(error))
        result.status_code = int(match.group()) if match else 500
    result._content = (json.dumps(payload) if payload is not None else text).encode()
    result._content_consumed = True
    result.encoding = "utf-8"
    return result


def test_config():
    return replace(gate.load_config(ENV), retries=0, probe_edges=False)


def node(host="vpn12345.opengw.net", code="JP", category="residential", latency=10, success=True):
    return {
        "host": host,
        "port": 443,
        "country": "Japan",
        "country_code": code,
        "residential": category,
        "latency_ms": latency,
        "success": success,
        "worker_error": False,
    }


class OfflineTestCase(unittest.TestCase):
    def setUp(self):
        # 意外遗漏 mock 时立即失败，禁止测试访问真实网络。
        network = patch(
            "requests.sessions.Session.request", side_effect=AssertionError("unexpected network")
        )
        network.start()
        self.addCleanup(network.stop)
        reports = patch("vpngate.save_report")
        reports.start()
        self.addCleanup(reports.stop)


class ConfigTests(OfflineTestCase):
    def test_compose_urls_and_use_repository_owner(self):
        config = gate.load_config(ENV)
        self.assertEqual(
            config.worker_check_url, "https://private-check.example.com/check?sstp=vpn:vpn@"
        )
        self.assertEqual(config.nodes_url, "https://example-owner.github.io/gate_check/nodes.txt")
        self.assertEqual(config.edge_hosts, ("a.example.com:443", "b.example.com:8443"))

    def test_username_override_and_repository_rename(self):
        config = gate.load_config(
            dict(
                ENV,
                NODES_GITHUB_USERNAME=" Another-Owner ",
                GITHUB_REPOSITORY="original/Renamed-Repo",
            )
        )
        self.assertEqual(config.nodes_url, "https://another-owner.github.io/Renamed-Repo/nodes.txt")

    def test_blank_optional_username_uses_owner(self):
        self.assertEqual(
            gate.load_config(dict(ENV, NODES_GITHUB_USERNAME=" \n")).nodes_url,
            "https://example-owner.github.io/gate_check/nodes.txt",
        )

    def test_multiline_entries_are_normalized_and_deduplicated_in_order(self):
        env = dict(
            ENV,
            EDGE_HOSTS=" A.EXAMPLE.COM:443,\r\nb.example.com:8443\na.example.com:0443, ,\n",
            WORKER_DOMAIN=" Private-Check.Example.COM \n",
        )
        config = gate.load_config(env)
        self.assertEqual(config.edge_hosts, ("a.example.com:443", "b.example.com:8443"))
        self.assertTrue(config.worker_check_url.startswith("https://private-check.example.com/"))

    def test_ip_entries(self):
        config = gate.load_config(dict(ENV, EDGE_HOSTS="192.0.2.1:443,[2001:db8::1]:8443"))
        self.assertEqual(config.edge_hosts, ("192.0.2.1:443", "[2001:db8::1]:8443"))

    def test_required_settings(self):
        for key in ("EDGE_HOSTS", "WORKER_DOMAIN", "GITHUB_REPOSITORY"):
            for value in (None, "", " \n"):
                with self.subTest(key=key, value=value):
                    env = dict(ENV)
                    if value is None:
                        del env[key]
                    else:
                        env[key] = value
                    with self.assertRaisesRegex(gate.ConfigError, key):
                        gate.load_config(env)

    def test_invalid_entry_formats(self):
        for value in (
            ",\n,",
            "a.example.com",
            "a.example.com:0",
            "a.example.com:65536",
            "a.example.com:abc",
            "https://a.example.com:443",
            "a.example.com/x:443",
            "a.example.com#x:443",
            "a.example.com$sstp:443",
            "user@a.example.com:443",
            "-bad.example.com:443",
            "[not-ipv6]:443",
            "2001:db8::1:443",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(gate.ConfigError, "EDGE_HOSTS"):
                gate.load_config(dict(ENV, EDGE_HOSTS=value))

    def test_worker_domain_only(self):
        for value in (
            "https://private-check.example.com",
            "private-check.example.com/",
            "private-check.example.com:443",
            "private-check.example.com/check?sstp=",
            "user@private-check.example.com",
            "bad_domain.example.com",
            "192.0.2.1",
        ):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(gate.ConfigError, "WORKER_DOMAIN"),
            ):
                gate.load_config(dict(ENV, WORKER_DOMAIN=value))

    def test_invalid_username_or_repository(self):
        for key, value in (
            ("NODES_GITHUB_USERNAME", "https://github.com/someone"),
            ("NODES_GITHUB_USERNAME", "owner/repo"),
            ("NODES_GITHUB_USERNAME", "owner--name"),
            ("GITHUB_REPOSITORY", "owner"),
            ("GITHUB_REPOSITORY", "owner/../repo"),
            ("GITHUB_REPOSITORY", "owner/.."),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(gate.ConfigError):
                gate.load_config(dict(ENV, **{key: value}))

    def test_invalid_config_exits_before_network_and_omits_value(self):
        secret = "https://private-check.example.com/check?private-value"
        with patch("requests.Session.get") as get, redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(gate.main(dict(ENV, WORKER_DOMAIN=secret)), 1)
        get.assert_not_called()
        self.assertIn("WORKER_DOMAIN", stderr.getvalue())
        self.assertNotIn(secret, stderr.getvalue())

    def test_no_legacy_config_fallback(self):
        legacy = {
            "CHECK_WORKER": "https://legacy.example/check",
            "HOSTS_ENTRY": "old.example:443",
            "NODES_URL": "https://legacy.example/nodes.txt",
        }
        with self.assertRaisesRegex(gate.ConfigError, "EDGE_HOSTS"):
            gate.load_config(legacy)
        self.assertEqual(gate.load_config(dict(ENV, **legacy)), gate.load_config(ENV))

    def test_cli_missing_config_has_nonzero_exit(self):
        # 保留解释器需要的 PATH/动态库环境，只清除本项目的配置。
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in (*ENV, "NODES_GITHUB_USERNAME")
        }
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env["GATE_REPORT_DIR"] = directory.name
        result = subprocess.run(
            [sys.executable, str(gate.REPO_DIR / "vpngate.py")],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("EDGE_HOSTS", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class SourceTests(OfflineTestCase):
    def test_csv_tcp_filtering_and_deduplication(self):
        rows = gate.parse_csv(
            source_csv(
                encoded_config(),
                encoded_config(),
                encoded_config("udp"),
                encoded_config("tcp6", 8443),
                "not valid base64",
            )
        )
        nodes = gate.dedupe(gate.to_sstp_nodes(rows))
        self.assertEqual(
            [(n["host"], n["port"]) for n in nodes],
            [("vpn12345.opengw.net", 443), ("vpn12345.opengw.net", 8443)],
        )

    def test_malformed_rows_and_out_of_range_ports_are_skipped(self):
        text = source_csv(encoded_config(port=0), encoded_config(port=65536))
        self.assertEqual(gate.to_sstp_nodes(gate.parse_csv(text + "short-row\n")), [])

    def test_mirror_list_wrapper_and_field_aliases(self):
        server = {
            "host": "vpn12345",
            "ip": "192.0.2.1",
            "country_long": "Japan",
            "country_short": "JP",
            "config_b64": encoded_config(),
        }
        for data in ([server], {"servers": [server, None]}, [{"servers": [server]}], server):
            with self.subTest(data=data):
                rows = gate.parse_mirror_json(data)
                self.assertEqual(len(rows), 1)
                self.assertEqual(gate.to_sstp_nodes(rows)[0]["host"], "vpn12345.opengw.net")

    def test_official_failure_empty_and_invalid_data_fall_back_to_mirror(self):
        mirror = response(
            payload=[
                {
                    "hostname": "vpn12345",
                    "ip": "192.0.2.1",
                    "openvpn_configdata_base64": encoded_config(),
                }
            ]
        )
        failures = (
            requests.Timeout("timeout"),
            response(text=source_csv().split("vpn12345")[0]),
            response(text="<html>not csv</html>"),
            response(error=requests.HTTPError("503")),
        )
        for failure in failures:
            with self.subTest(failure=failure), redirect_stdout(io.StringIO()):
                with patch("requests.Session.get", side_effect=[failure, mirror]) as get:
                    rows = gate.fetch_vpngate()
                self.assertEqual(rows[0]["host"], "vpn12345")
                self.assertEqual(
                    [call.args[0] for call in get.call_args_list],
                    [gate.VPNGATE_API, gate.VPNGATE_MIRROR],
                )

    def test_both_sources_fail(self):
        with (
            patch("requests.Session.get", side_effect=requests.Timeout),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(gate.PipelineError, "官方 API 与镜像均不可用"):
                gate.fetch_vpngate()


class WorkerTests(OfflineTestCase):
    def test_request_url_and_success_response(self):
        reply = response(
            payload={
                "success": True,
                "responseTime": "25.5",
                "exit": {"is_datacenter": False, "asn": {"org": "Example"}},
            }
        )
        config = gate.load_config(ENV)
        with patch("requests.Session.get", return_value=reply) as get:
            result = gate.check_one(node(), config)
        self.assertEqual(
            get.call_args.args[0],
            "https://private-check.example.com/check?sstp=vpn:vpn@vpn12345.opengw.net%3A443",
        )
        self.assertEqual(get.call_args.kwargs["timeout"], (10, 90))
        self.assertTrue(result["success"])
        self.assertFalse(result["worker_error"])
        self.assertEqual(result["residential"], "residential")
        self.assertEqual(result["latency_ms"], 25.5)

    def test_unavailable_node_is_not_worker_failure(self):
        with patch("requests.Session.get", return_value=response(payload={"success": False})):
            result = gate.check_one(node(), test_config())
        self.assertFalse(result["success"])
        self.assertFalse(result["worker_error"])

    def test_http_timeout_and_malformed_worker_responses(self):
        invalid_json = response(text="not JSON")
        replies = (
            requests.Timeout("private url"),
            response(error=requests.HTTPError("502")),
            invalid_json,
            response(payload=[]),
            response(payload={"success": "false"}),
            response(payload={"message": "oops"}),
        )
        for reply in replies:
            with self.subTest(reply=reply), patch("requests.Session.get", side_effect=[reply]):
                result = gate.check_one(node(), test_config())
                self.assertFalse(result["success"])
                self.assertTrue(result["worker_error"])

    def test_optional_latency_and_exit_metadata(self):
        for latency in (None, "unknown", "NaN", float("inf"), -1):
            with self.subTest(latency=latency):
                reply = response(payload={"success": True, "responseTime": latency, "exit": []})
                with patch("requests.Session.get", return_value=reply):
                    result = gate.check_one(node(), test_config())
                self.assertTrue(result["success"])
                self.assertIsNone(result["latency_ms"])

    def test_network_classification(self):
        self.assertEqual(gate.classify_network("vpn12345", "AWS", False), "residential")
        self.assertEqual(gate.classify_network("vpn12345", "NTT", True), "datacenter")
        self.assertEqual(gate.classify_network("other", "Amazon AWS"), "datacenter")
        self.assertEqual(gate.classify_network("other", "COMCAST"), "residential")
        self.assertEqual(gate.classify_network("public-vpn1", ""), "datacenter")
        self.assertEqual(gate.classify_network("other", ""), "unknown")


class OutputTests(OfflineTestCase):
    def test_country_category_latency_sorting_rotation_and_exact_format(self):
        nodes = [
            node("us.opengw.net", "US"),
            node("slow.opengw.net", latency=100),
            node("dc.opengw.net", category="datacenter", latency=1),
            node("fast.opengw.net", latency=0),
            node("failed.opengw.net", success=False),
        ]
        text = gate.build_nodes_text(nodes, ("a.example.com:443", "b.example.com:8443"))
        self.assertEqual(
            text,
            "\n".join(
                [
                    "a.example.com:443",
                    "b.example.com:8443",
                    "a.example.com:443#日本-住宅-01$sstp://vpn:vpn@fast.opengw.net:443",
                    "b.example.com:8443#日本-住宅-02$sstp://vpn:vpn@slow.opengw.net:443",
                    "a.example.com:443#日本-机房-01$sstp://vpn:vpn@dc.opengw.net:443",
                    "b.example.com:8443#美国-住宅-01$sstp://vpn:vpn@us.opengw.net:443",
                ]
            )
            + "\n",
        )
        self.assertEqual(
            text,
            gate.build_nodes_text(
                list(reversed(nodes)), ("a.example.com:443", "b.example.com:8443")
            ),
        )

    def test_all_configured_entries_are_listed_even_with_fewer_sstp_nodes(self):
        config = gate.load_config(
            dict(
                ENV,
                EDGE_HOSTS="a.example.com:443,b.example.com:8443\na.example.com:443,c.example.com:443",
            )
        )
        text = gate.build_nodes_text([node()], config.edge_hosts)
        self.assertEqual(
            text.splitlines(),
            [
                "a.example.com:443",
                "b.example.com:8443",
                "c.example.com:443",
                "a.example.com:443#日本-住宅-01$sstp://vpn:vpn@vpn12345.opengw.net:443",
            ],
        )

    def test_missing_latency_sorts_last_and_unknown_uses_distinct_group(self):
        text = gate.build_nodes_text(
            [
                node("missing.opengw.net", latency=None),
                node("known.opengw.net", latency=20),
                node("unknown.opengw.net", category="unknown"),
            ],
            ("a.example.com:443",),
        )
        lines = text.splitlines()[1:]
        self.assertIn("@known.opengw.net", lines[0])
        self.assertIn("@missing.opengw.net", lines[1])
        self.assertIn("#日本-未识别-01", lines[2])

    def test_zero_success_cannot_generate_empty_output(self):
        for results in ([], [node(success=False)]):
            with self.subTest(results=results), self.assertRaises(gate.PipelineError):
                gate.build_nodes_text(results, ("a.example.com:443",))

    def test_failed_replace_preserves_previous_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            public = Path(directory)
            previous = public / "nodes.txt"
            previous.write_text("previous\n", encoding="utf-8")
            with patch("vpngate.os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    gate.write_nodes("new\n", public)
            self.assertEqual(previous.read_text(encoding="utf-8"), "previous\n")
            self.assertEqual(list(public.iterdir()), [previous])


class PipelineTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.public = Path(directory.name) / "public"
        self.config = replace(test_config(), public_dir=self.public)

    def test_success_generates_only_nodes_with_no_worker_disclosure(self):
        replies = [
            response(text=source_csv()),
            response(payload={"success": True, "responseTime": 12}),
        ]
        with (
            patch("requests.Session.get", side_effect=replies),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            destination = gate.run(self.config)
        self.assertEqual(list(self.public.iterdir()), [self.public / "nodes.txt"])
        self.assertEqual(
            destination.read_text(encoding="utf-8"),
            "a.example.com:443\nb.example.com:8443\n"
            "a.example.com:443#日本-住宅-01$sstp://vpn:vpn@vpn12345.opengw.net:443\n",
        )
        self.assertNotIn(
            ENV["WORKER_DOMAIN"], destination.read_text(encoding="utf-8") + stdout.getvalue()
        )
        self.assertIn(self.config.nodes_url, stdout.getvalue())

    def test_partial_success_publishes_only_available_nodes(self):
        def get(url, **kwargs):
            if url == gate.VPNGATE_API:
                return response(
                    text=source_csv(encoded_config(port=443), encoded_config(port=8443))
                )
            return response(payload={"success": url.endswith("%3A443"), "responseTime": 10})

        with patch("requests.Session.get", side_effect=get), redirect_stdout(io.StringIO()):
            destination = gate.run(self.config)
        lines = destination.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[:2], ["a.example.com:443", "b.example.com:8443"])
        self.assertEqual(len(lines), 3)
        self.assertNotIn("@vpn12345.opengw.net:8443", destination.read_text(encoding="utf-8"))

    def test_failure_paths_preserve_old_file(self):
        scenarios = {
            "source": [requests.Timeout(), requests.Timeout()],
            "no_tcp": [response(text=source_csv(encoded_config("udp"))), response(payload=[])],
            "worker": [response(text=source_csv()), requests.Timeout("private url")],
            "zero_success": [response(text=source_csv()), response(payload={"success": False})],
        }
        self.public.mkdir()
        previous = self.public / "nodes.txt"
        previous.write_text("previous\n", encoding="utf-8")
        for name, replies in scenarios.items():
            with self.subTest(name=name), patch("requests.Session.get", side_effect=replies):
                with redirect_stdout(io.StringIO()), self.assertRaises(gate.PipelineError):
                    gate.run(self.config)
                self.assertEqual(previous.read_text(encoding="utf-8"), "previous\n")
                self.assertEqual(list(self.public.iterdir()), [previous])

    def test_first_failure_creates_no_artifacts(self):
        replies = [response(text=source_csv()), response(payload={"success": False})]
        with patch("requests.Session.get", side_effect=replies), redirect_stdout(io.StringIO()):
            with self.assertRaises(gate.PipelineError):
                gate.run(self.config)
        self.assertFalse(self.public.exists())

    def test_main_failure_returns_nonzero_without_disclosing_worker_url(self):
        replies = [response(text=source_csv()), requests.Timeout(self.config.worker_check_url)]
        with patch("requests.Session.get", side_effect=replies):
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(gate.main(dict(ENV, CHECK_RETRIES="0", PROBE_EDGES="false")), 1)
        logs = stdout.getvalue() + stderr.getvalue()
        self.assertIn("Worker 全部请求异常", logs)
        self.assertNotIn(ENV["WORKER_DOMAIN"], logs)


if __name__ == "__main__":
    unittest.main()
