import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from test_vpngate import OfflineTestCase, response

import gate_delivery as delivery
from gate_http import RequestFailure
from gate_report import atomic_json, fresh_report, markdown, read_json, timestamp

NODES = (
    "a.example.com:443\na.example.com:443#日本-住宅-01$sstp://vpn:vpn@vpn12345.opengw.net:443\n"
).encode()


class DeliveryTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.nodes = self.directory / "nodes.txt"
        self.nodes.write_bytes(NODES)
        env = patch.dict(
            os.environ,
            {
                "PAGES_BASE_URL": "https://owner.github.io/project",
                "GITHUB_STEP_SUMMARY": str(self.directory / "step-summary.md"),
            },
        )
        env.start()
        self.addCleanup(env.stop)

    def test_validate_both_edgetunnel_line_types(self):
        self.assertEqual(delivery.validate_nodes(NODES), (1, 1))
        for content in (
            b"",
            b"a.example.com:443\n",
            b"<html>error</html>",
            NODES.replace(b"vpn:vpn@", b"bad@"),
            NODES.replace(b":443\n", b":0\n"),
        ):
            with self.subTest(content=content), self.assertRaises(ValueError):
                delivery.validate_nodes(content)

    def test_unchanged_content_skips_deployment(self):
        output = self.directory / "outputs"
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}):
            with (
                patch("gate_delivery.fetch_live", return_value=NODES),
                redirect_stdout(io.StringIO()),
            ):
                result = delivery.compare(self.nodes, self.directory)
        self.assertFalse(result["changed"])
        self.assertIn("verified_at", result)
        self.assertEqual(output.read_text(), "changed=false\n")

    def test_missing_or_changed_live_content_requests_deployment(self):
        for actual in (b"previous", RequestFailure("http_404")):
            with self.subTest(actual=actual):
                with (
                    patch("gate_delivery.fetch_live", side_effect=[actual]),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertTrue(delivery.compare(self.nodes, self.directory)["changed"])

    def test_verification_waits_for_cdn_content(self):
        with (
            patch("gate_delivery.fetch_live", side_effect=[b"old", NODES]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(delivery.verify(self.nodes, self.directory, sleep=lambda seconds: None))
        self.assertIn("verified_at", read_json(self.directory / "publication.json"))

    def test_verification_failure_is_not_success(self):
        with patch("gate_delivery.fetch_live", return_value=b"old"):
            self.assertFalse(
                delivery.verify(self.nodes, self.directory, attempts=2, sleep=lambda seconds: None)
            )
        self.assertEqual(
            read_json(self.directory / "publication.json")["verification_error"], "content_mismatch"
        )

    def initial_state(self, failures=2, age=12):
        previous = (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()
        atomic_json(
            self.directory / "history.json",
            {
                "known": True,
                "previous": {
                    "last_success_at": previous,
                    "last_published_at": previous,
                    "consecutive_failures": failures,
                },
            },
        )
        report = fresh_report()
        report.update(status="success", checked_at=timestamp())
        atomic_json(self.directory / "run.json", report)
        return previous

    def test_failure_preserves_timestamps_and_increments_failures(self):
        previous = self.initial_state()
        self.assertFalse(delivery.finalize(self.directory, {"CHECK_OUTCOME": "failure"}))
        result = read_json(self.directory / "run.json")
        self.assertEqual(result["last_success_at"], previous)
        self.assertEqual(result["last_published_at"], previous)
        self.assertEqual(result["consecutive_failures"], 3)
        self.assertTrue(any("过期" in warning for warning in result["warnings"]))

    def test_unchanged_verified_run_updates_check_time_but_not_publication_time(self):
        previous = self.initial_state()
        now = timestamp()
        atomic_json(self.directory / "publication.json", {"changed": False, "verified_at": now})
        self.assertTrue(
            delivery.finalize(
                self.directory,
                {
                    "CHECK_OUTCOME": "success",
                    "COMPARE_OUTCOME": "success",
                    "DEPLOY_OUTCOME": "skipped",
                    "VERIFY_OUTCOME": "skipped",
                },
            )
        )
        result = read_json(self.directory / "run.json")
        self.assertEqual(result["last_success_at"], now)
        self.assertEqual(result["last_published_at"], previous)
        self.assertEqual(result["consecutive_failures"], 0)

    def test_deployment_requires_successful_verification(self):
        previous = self.initial_state()
        atomic_json(self.directory / "publication.json", {"changed": True})
        self.assertFalse(
            delivery.finalize(
                self.directory,
                {
                    "CHECK_OUTCOME": "success",
                    "COMPARE_OUTCOME": "success",
                    "DEPLOY_OUTCOME": "success",
                    "VERIFY_OUTCOME": "failure",
                },
            )
        )
        result = read_json(self.directory / "run.json")
        self.assertEqual(result["last_success_at"], previous)
        self.assertIn("验证未通过", result["publication"])

    def test_unknown_history_is_not_reported_as_zero_failures(self):
        atomic_json(self.directory / "history.json", {"known": False})
        delivery.finalize(self.directory, {"CHECK_OUTCOME": "failure"})
        self.assertIsNone(read_json(self.directory / "run.json")["consecutive_failures"])

    def test_successful_deploy_records_both_timestamps(self):
        self.initial_state()
        now = timestamp()
        atomic_json(self.directory / "publication.json", {"changed": True, "verified_at": now})
        self.assertTrue(
            delivery.finalize(
                self.directory,
                {
                    "CHECK_OUTCOME": "success",
                    "COMPARE_OUTCOME": "success",
                    "DEPLOY_OUTCOME": "success",
                    "VERIFY_OUTCOME": "success",
                },
            )
        )
        report = read_json(self.directory / "run.json")
        self.assertEqual(report["last_success_at"], now)
        self.assertEqual(report["consecutive_failures"], 0)

    def test_recover_latest_report_from_workflow_artifact(self):
        previous = {"schema": 1, "last_success_at": timestamp(), "consecutive_failures": 3}
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("run.json", json.dumps(previous))
        zipped = response()
        zipped._content = bundle.getvalue()
        responses = [
            response(payload={"workflow_runs": [{"id": 123}]}),
            response(
                payload={"artifacts": [{"id": 456, "expired": False, "name": "gate-report-123-1"}]}
            ),
            zipped,
        ]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "124"}):
            with patch("gate_http.HttpClient.get", side_effect=responses):
                result = delivery.history(self.directory)
        self.assertTrue(result["known"])
        self.assertEqual(result["previous"]["consecutive_failures"], 3)

    def test_missing_history_api_does_not_abort_detection(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"}):
            with patch("gate_http.HttpClient.get", side_effect=RequestFailure("http_403")):
                result = delivery.history(self.directory)
        self.assertFalse(result["known"])
        self.assertTrue(result["warnings"])

    def test_report_includes_breakdowns_without_public_site_files(self):
        report = fresh_report()
        report.update(
            errors={"http_503": 2},
            retries={"read_timeout": 1},
            countries={"JP": 3},
            edge_probes=[{"index": 1, "status": "connect_timeout"}],
        )
        text = markdown(report)
        self.assertIn("http_503: 2", text)
        self.assertIn("read_timeout: 1", text)
        self.assertIn("入口 TCP 探测", text)
        self.assertNotIn("WORKER_DOMAIN", text)


if __name__ == "__main__":
    unittest.main()
