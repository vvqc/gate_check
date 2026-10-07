import io
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

import requests
from test_vpngate import (
    ENV,
    OfflineTestCase,
    encoded_config,
    node,
    response,
    source_csv,
    test_config,
)
from urllib3.exceptions import ReadTimeoutError

import vpngate as gate
from gate_config import ConfigError, load_config
from gate_http import HttpClient, RequestFailure, classify_exception, retry_delay


class ExtendedConfigTests(OfflineTestCase):
    def test_optional_variables_and_blank_defaults(self):
        config = load_config(
            dict(
                ENV,
                CHECK_CONCURRENCY="4",
                CHECK_TIMEOUT="12.5",
                CHECK_RETRIES="1",
                ALLOWED_COUNTRIES="jp,US\nJP",
                RESIDENTIAL_ONLY="true",
                PROBE_EDGES="false",
                MAX_LATENCY_MS="200",
                MAX_PER_COUNTRY="2",
            )
        )
        self.assertEqual(config.concurrency, 4)
        self.assertEqual(config.check_timeout, 12.5)
        self.assertEqual(config.allowed_countries, ("JP", "US"))
        self.assertTrue(config.residential_only)
        self.assertFalse(config.probe_edges)
        self.assertEqual(load_config(dict(ENV, CHECK_CONCURRENCY="")).concurrency, 32)

    def test_invalid_limits_fail_without_network(self):
        for key, value in (
            ("CHECK_CONCURRENCY", "0"),
            ("CHECK_RETRIES", "4"),
            ("CHECK_TIMEOUT", "nan"),
            ("HTTP_TIMEOUT", "inf"),
            ("MAX_CHECK_NODES", "1.5"),
            ("RUN_TIMEOUT", "1600"),
            ("RESIDENTIAL_ONLY", "maybe"),
            ("ALLOWED_COUNTRIES", "Japan"),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(ConfigError, key):
                load_config(dict(ENV, **{key: value}))


class HttpTests(OfflineTestCase):
    def test_transient_error_retries_and_reuses_session(self):
        retried = []
        sleep = Mock()
        with patch(
            "requests.Session.get",
            side_effect=[requests.ConnectTimeout("secret"), response(text="ok")],
        ) as get:
            with HttpClient(
                replace(test_config(), retries=2), on_retry=retried.append, sleep=sleep
            ) as client:
                result = client.get("https://example.com")
                self.assertEqual(len(client.sessions), 1)
        self.assertEqual(result.text, "ok")
        self.assertEqual(get.call_count, 2)
        self.assertEqual(retried, ["connect_timeout"])
        sleep.assert_called_once_with(1)

    def test_retry_after_is_used_for_rate_limit(self):
        limited = response(error=requests.HTTPError("429"))
        limited.headers["Retry-After"] = "7"
        sleep = Mock()
        with patch("requests.Session.get", side_effect=[limited, response(text="ok")]):
            with HttpClient(replace(test_config(), retries=1), sleep=sleep) as client:
                client.get("https://example.com")
        sleep.assert_called_once_with(7)
        self.assertEqual(retry_delay("invalid", 2), 2)
        self.assertEqual(retry_delay("120", 2), 120)
        self.assertEqual(retry_delay("nan", 2), 2)

    def test_stream_read_timeout_is_classified_correctly(self):
        error = requests.ConnectionError(ReadTimeoutError(None, "hidden-url", "timeout"))
        self.assertEqual(classify_exception(error).code, "read_timeout")

    def test_permanent_errors_are_not_retried(self):
        cases = [
            (response(error=requests.HTTPError("401")), "http_401"),
            (requests.exceptions.SSLError("secret"), "tls_error"),
        ]
        for failure, code in cases:
            with (
                self.subTest(code=code),
                patch("requests.Session.get", side_effect=[failure]) as get,
            ):
                with HttpClient(replace(test_config(), retries=3)) as client:
                    with self.assertRaisesRegex(RequestFailure, code):
                        client.get("https://example.com")
                self.assertEqual(get.call_count, 1)

    def test_retry_limit_is_respected(self):
        with patch("requests.Session.get", side_effect=requests.ReadTimeout) as get:
            with HttpClient(
                replace(test_config(), retries=2), sleep=lambda seconds: None
            ) as client:
                with self.assertRaisesRegex(RequestFailure, "read_timeout"):
                    client.get("https://example.com")
        self.assertEqual(get.call_count, 3)

    def test_large_response_is_rejected(self):
        with patch("requests.Session.get", return_value=response(text="123456")):
            with HttpClient(test_config()) as client:
                with self.assertRaisesRegex(RequestFailure, "response_too_large"):
                    client.get("https://example.com", limit=5)

    def test_expired_deadline_prevents_new_requests(self):
        now = [0]
        with HttpClient(test_config(), clock=lambda: now[0]) as client:
            now[0] = 1000
            with patch("requests.Session.get") as get:
                with self.assertRaisesRegex(RequestFailure, "deadline_exceeded"):
                    client.get("https://example.com")
                get.assert_not_called()

    def test_sessions_are_thread_local_and_closed(self):
        sessions = [Mock(), Mock()]
        with patch("requests.Session", side_effect=sessions):
            with HttpClient(test_config()) as client:
                self.assertIs(client.session(), client.session())
                with ThreadPoolExecutor(max_workers=1) as pool:
                    remote = pool.submit(client.session).result()
                    self.assertIs(pool.submit(client.session).result(), remote)
                self.assertIsNot(client.session(), remote)
        for session in sessions:
            session.close.assert_called_once()


class PipelineEnhancementTests(OfflineTestCase):
    def test_nonempty_source_without_tcp_nodes_uses_mirror(self):
        mirror = [{"host": "vpn12345", "ip": "192.0.2.1", "config_b64": encoded_config()}]
        with patch(
            "requests.Session.get",
            side_effect=[
                response(text=source_csv(encoded_config("udp"))),
                response(payload=mirror),
            ],
        ) as get:
            with redirect_stdout(io.StringIO()):
                rows = gate.fetch_vpngate(test_config())
        self.assertEqual(len(gate.to_sstp_nodes(rows)), 1)
        self.assertEqual(get.call_args.args[0], gate.VPNGATE_MIRROR)

    def test_filters_and_per_country_limit(self):
        candidates = [
            node("slow.opengw.net", latency=100),
            node("fast.opengw.net", latency=10),
            node("dc.opengw.net", category="datacenter", latency=1),
            node("us.opengw.net", code="US"),
            node("missing.opengw.net", latency=None),
        ]
        config = replace(
            test_config(),
            allowed_countries=("JP",),
            residential_only=True,
            max_latency_ms=50,
            max_per_country=1,
        )
        self.assertEqual(
            [item["host"] for item in gate.select_nodes(candidates, config)], ["fast.opengw.net"]
        )
        self.assertEqual(
            gate.select_nodes(candidates, replace(config, allowed_countries=("KR",))), []
        )

    def test_default_filter_retains_all_successes(self):
        candidates = [
            node("unknown.opengw.net", category="unknown", latency=None),
            node("success.opengw.net"),
            node("failure.opengw.net", success=False),
        ]
        self.assertEqual(len(gate.select_nodes(candidates, test_config())), 2)

    def test_max_check_nodes_limits_network_work(self):
        config = replace(test_config(), max_check_nodes=1)
        rows = gate.parse_csv(source_csv(encoded_config(port=443), encoded_config(port=8443)))
        with patch("vpngate.fetch_vpngate", return_value=rows), redirect_stdout(io.StringIO()):
            with patch("vpngate.check_all", return_value=[node()]) as check:
                with patch("vpngate.write_nodes", return_value=gate.Path("nodes.txt")):
                    gate.run(config)
        self.assertEqual(len(check.call_args.args[0]), 1)

    def test_probe_is_report_only_and_omits_secret_addresses(self):
        config = replace(test_config(), edge_hosts=("a.example.com:443",), probe_edges=True)
        with patch("socket.create_connection", return_value=MagicMock()) as connection:
            results = gate.probe_edges(config)
        self.assertEqual(results[0]["status"], "tcp_ok")
        connection.assert_called_once_with(("a.example.com", 443), timeout=5)
        self.assertNotIn("example.com", str(results))
        with patch("socket.create_connection", side_effect=TimeoutError):
            self.assertEqual(gate.probe_edges(config)[0]["status"], "connect_timeout")

    def test_invalid_remote_port_does_not_abort_whole_source(self):
        rows = gate.parse_csv(
            source_csv(
                encoded_config(port="443.5"),
                encoded_config(port="9" * 5000),
                encoded_config(port=443),
            )
        )
        self.assertEqual(len(gate.to_sstp_nodes(rows)), 1)

    def test_process_budget_stops_stalled_child(self):
        started = time.monotonic()
        code = gate.supervise([sys.executable, "-c", "import time; time.sleep(30)"], 0.15)
        self.assertEqual(code, 124)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
