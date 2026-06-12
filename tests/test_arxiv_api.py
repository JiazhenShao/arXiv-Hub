from __future__ import annotations

import io
import json
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import Mock

from datetime import date

from arxiv_daily.arxiv_api import (
    ArxivClient,
    ArxivError,
    ArxivMetadataError,
    dedupe_papers,
    parse_atom,
)


FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_feed.xml"


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


class FakeResponse:
    def __init__(
        self,
        payload: bytes = b"<feed />",
        *,
        content_type: str = "application/atom+xml",
        url: str = "https://export.arxiv.org/api/query",
    ) -> None:
        self.payload = payload
        self.offset = 0
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(payload)),
        }
        self.url = url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            result = self.payload[self.offset :]
            self.offset = len(self.payload)
            return result
        result = self.payload[self.offset : self.offset + size]
        self.offset += len(result)
        return result

    def geturl(self) -> str:
        return self.url


def http_error(code: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return urllib.error.HTTPError(
        "https://export.arxiv.org/api/query",
        code,
        "temporary failure",
        headers,
        io.BytesIO(),
    )


class ArxivApiTests(unittest.TestCase):
    def test_parse_atom_preserves_verified_metadata(self) -> None:
        papers = parse_atom(FIXTURE.read_bytes())

        self.assertEqual(len(papers), 2)
        first = papers[0]
        self.assertEqual(first.arxiv_id, "2606.00001")
        self.assertEqual(first.versioned_id, "2606.00001v2")
        self.assertEqual(first.title, "Dense Matter in Neutron-Star Mergers")
        self.assertEqual(
            first.abstract,
            "We study dense matter in neutron-star mergers "
            "using a controlled equation of state.",
        )
        self.assertEqual(first.authors, ("Ada Researcher", "Emmy Physicist"))
        self.assertEqual(first.categories, ("nucl-th", "astro-ph.HE"))
        self.assertEqual(first.primary_category, "nucl-th")
        self.assertEqual(first.abs_url, "https://arxiv.org/abs/2606.00001v2")
        self.assertEqual(first.pdf_url, "https://arxiv.org/pdf/2606.00001v2")

    def test_malformed_atom_fails_closed(self) -> None:
        with self.assertRaises(ArxivMetadataError):
            parse_atom(b"<feed><entry>")

    def test_missing_required_metadata_fails_closed(self) -> None:
        payload = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><id>https://arxiv.org/abs/2606.99999v1</id></entry>
        </feed>"""
        with self.assertRaises(ArxivMetadataError):
            parse_atom(payload)

    def test_deduplication_uses_versionless_arxiv_id(self) -> None:
        papers = parse_atom(FIXTURE.read_bytes())
        older = papers[0].with_version("2606.00001v1")

        deduped = dedupe_papers([older, papers[0], papers[1]])

        self.assertEqual([paper.versioned_id for paper in deduped], [
            "2606.00001v2",
            "2606.00002v1",
        ])

    def test_candidate_fetch_paginates_until_total_results_are_read(self) -> None:
        def feed(entries: list[str], total: int) -> bytes:
            body = "".join(
                f"""
                <entry>
                  <id>https://arxiv.org/abs/{arxiv_id}v1</id>
                  <updated>2026-06-08T12:30:00Z</updated>
                  <published>2026-06-08T12:00:00Z</published>
                  <title>Paper {arxiv_id}</title>
                  <summary>Verified abstract {arxiv_id}</summary>
                  <author><name>Author</name></author>
                  <category term="nucl-th"/>
                  <arxiv:primary_category term="nucl-th"/>
                </entry>
                """
                for arxiv_id in entries
            )
            return f"""<?xml version="1.0"?>
            <feed xmlns="http://www.w3.org/2005/Atom"
                  xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
                  xmlns:arxiv="http://arxiv.org/schemas/atom">
              <opensearch:totalResults>{total}</opensearch:totalResults>
              {body}
            </feed>""".encode()

        class PaginatedClient(ArxivClient):
            def __init__(self) -> None:
                super().__init__(
                    ["nucl-th"],
                    user_agent="test",
                    max_results=2,
                )
                self.starts: list[int] = []

            def _request(self, parameters: dict[str, str | int]) -> bytes:
                start = int(parameters["start"])
                self.starts.append(start)
                if start == 0:
                    return feed(["2606.00001", "2606.00002"], 3)
                return feed(["2606.00003"], 3)

        client = PaginatedClient()
        papers = client.fetch_candidates(date(2026, 6, 1), date(2026, 6, 8))

        self.assertEqual(client.starts, [0, 2])
        self.assertEqual(
            [paper.arxiv_id for paper in papers],
            ["2606.00001", "2606.00002", "2606.00003"],
        )

    def test_separate_clients_share_request_pacing_state(self) -> None:
        with TemporaryDirectory() as tmp:
            clock = FakeClock()
            opener = Mock(return_value=FakeResponse())
            first = ArxivClient(
                ["nucl-th"],
                user_agent="test",
                state_dir=Path(tmp),
                clock=clock.time,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
                opener=opener,
            )
            second = ArxivClient(
                ["nucl-th"],
                user_agent="test",
                state_dir=Path(tmp),
                clock=clock.time,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
                opener=opener,
            )

            first._request({"id_list": "2606.00001"})
            second._request({"id_list": "2606.00002"})

            self.assertEqual(clock.sleeps, [3.0])
            throttle = json.loads(
                (Path(tmp) / "arxiv-api-throttle.json").read_text(encoding="utf-8")
            )
            self.assertEqual(throttle["last_request_at"], 103.0)

    def test_rate_limit_honors_retry_after_then_succeeds(self) -> None:
        with TemporaryDirectory() as tmp:
            clock = FakeClock()
            rate_limit = http_error(429, retry_after="5")
            opener = Mock(
                side_effect=[
                    rate_limit,
                    FakeResponse(b"verified"),
                ]
            )
            client = ArxivClient(
                ["nucl-th"],
                user_agent="test",
                state_dir=Path(tmp),
                clock=clock.time,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
                opener=opener,
            )

            payload = client._request({"id_list": "2606.00001"})

            self.assertEqual(payload, b"verified")
            self.assertEqual(clock.sleeps, [5.0])
            self.assertEqual(opener.call_count, 2)
            self.assertTrue(rate_limit.fp.closed)

    def test_temporary_server_error_uses_configured_backoff(self) -> None:
        clock = FakeClock()
        opener = Mock(side_effect=[http_error(503), FakeResponse(b"verified")])
        client = ArxivClient(
            ["nucl-th"],
            user_agent="test",
            min_interval_seconds=0,
            retry_backoffs=(10.0, 20.0, 40.0),
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            opener=opener,
        )

        self.assertEqual(client._request({"id_list": "2606.00001"}), b"verified")
        self.assertEqual(clock.sleeps, [10.0])

    def test_network_failure_is_retried(self) -> None:
        clock = FakeClock()
        opener = Mock(
            side_effect=[
                urllib.error.URLError("temporary DNS failure"),
                FakeResponse(b"verified"),
            ]
        )
        client = ArxivClient(
            ["nucl-th"],
            user_agent="test",
            min_interval_seconds=0,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
            opener=opener,
        )

        self.assertEqual(client._request({"id_list": "2606.00001"}), b"verified")
        self.assertEqual(clock.sleeps, [10.0])

    def test_permanent_http_error_is_not_retried(self) -> None:
        opener = Mock(side_effect=http_error(400))
        client = ArxivClient(
            ["nucl-th"],
            user_agent="test",
            min_interval_seconds=0,
            opener=opener,
        )

        with self.assertRaisesRegex(ArxivError, "HTTP 400"):
            client._request({"id_list": "not-valid"})

        self.assertEqual(opener.call_count, 1)

    def test_retry_deadline_fails_without_sleeping_past_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            clock = FakeClock()
            opener = Mock(side_effect=http_error(429, retry_after="120"))
            client = ArxivClient(
                ["nucl-th"],
                user_agent="test",
                state_dir=Path(tmp),
                retry_deadline_seconds=90.0,
                clock=clock.time,
                monotonic=clock.monotonic,
                sleeper=clock.sleep,
                opener=opener,
            )

            with self.assertRaisesRegex(
                ArxivError,
                "rate limit persisted after 90 seconds",
            ):
                client._request({"id_list": "2606.00001"})

            self.assertEqual(clock.sleeps, [])
            self.assertEqual(opener.call_count, 1)

    def test_corrupt_and_implausibly_future_throttle_state_are_ignored(self) -> None:
        for contents in [
            "not-json",
            '{"last_request_at": 999999999999, "not_before": 999999999999}',
        ]:
            with self.subTest(contents=contents), TemporaryDirectory() as tmp:
                state_dir = Path(tmp)
                (state_dir / "arxiv-api-throttle.json").write_text(
                    contents,
                    encoding="utf-8",
                )
                clock = FakeClock()
                client = ArxivClient(
                    ["nucl-th"],
                    user_agent="test",
                    state_dir=state_dir,
                    clock=clock.time,
                    monotonic=clock.monotonic,
                    sleeper=clock.sleep,
                    opener=Mock(return_value=FakeResponse()),
                )

                client._request({"id_list": "2606.00001"})

                self.assertEqual(clock.sleeps, [])

    def test_local_http_endpoint_recovers_from_rate_limit(self) -> None:
        attempts = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                payload = b"verified"
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ArxivClient(
                ["nucl-th"],
                endpoint=f"http://127.0.0.1:{server.server_port}/api/query",
                user_agent="test",
                min_interval_seconds=0,
            )

            payload = client._request({"id_list": "2606.00001"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(payload, b"verified")
        self.assertEqual(attempts, 2)

    def test_download_pdf_streams_valid_canonical_arxiv_pdf(self) -> None:
        payload = b"%PDF-1.7\nverified"
        opener = Mock(
            return_value=FakeResponse(
                payload,
                content_type="application/pdf",
                url="https://arxiv.org/pdf/2606.00001v1",
            )
        )
        client = ArxivClient(
            [],
            user_agent="test",
            min_interval_seconds=0,
            opener=opener,
        )

        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "paper.tmp"
            client.download_pdf("2606.00001v1", destination)

            self.assertEqual(destination.read_bytes(), payload)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://arxiv.org/pdf/2606.00001v1")
        self.assertEqual(request.headers["Accept"], "application/pdf")

    def test_download_pdf_rejects_unapproved_redirect_content_and_size(self) -> None:
        cases = [
            (
                FakeResponse(
                    b"%PDF-1.7\nverified",
                    content_type="application/pdf",
                    url="https://evil.example/paper.pdf",
                ),
                "redirect",
                100,
            ),
            (
                FakeResponse(
                    b"%PDF-1.7\nverified",
                    content_type="text/html",
                    url="https://arxiv.org/pdf/2606.00001v1",
                ),
                "content type",
                100,
            ),
            (
                FakeResponse(
                    b"<html>not a pdf</html>",
                    content_type="application/pdf",
                    url="https://arxiv.org/pdf/2606.00001v1",
                ),
                "not a valid PDF",
                100,
            ),
            (
                FakeResponse(
                    b"%PDF-" + b"x" * 20,
                    content_type="application/pdf",
                    url="https://arxiv.org/pdf/2606.00001v1",
                ),
                "exceeds",
                10,
            ),
        ]
        for response, message, max_bytes in cases:
            with self.subTest(message=message), TemporaryDirectory() as tmp:
                client = ArxivClient(
                    [],
                    user_agent="test",
                    min_interval_seconds=0,
                    opener=Mock(return_value=response),
                )
                destination = Path(tmp) / "paper.tmp"

                with self.assertRaisesRegex(ArxivError, message):
                    client.download_pdf(
                        "2606.00001v1",
                        destination,
                        max_bytes=max_bytes,
                    )

                self.assertFalse(destination.exists())

    def test_local_pdf_download_recovers_from_rate_limit(self) -> None:
        attempts = 0
        payload = b"%PDF-1.7\nverified"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    self.send_response(429)
                    self.send_header("Retry-After", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = ArxivClient(
                [],
                endpoint=f"http://127.0.0.1:{server.server_port}",
                user_agent="test",
                min_interval_seconds=0,
                allowed_download_hosts={"127.0.0.1"},
                allowed_download_schemes={"http"},
            )
            with TemporaryDirectory() as tmp:
                destination = Path(tmp) / "paper.tmp"
                client.download_pdf_url(
                    f"http://127.0.0.1:{server.server_port}/paper.pdf",
                    destination,
                )
                result = destination.read_bytes()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result, payload)
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
