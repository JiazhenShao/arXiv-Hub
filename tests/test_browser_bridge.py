from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from scripts.browser_bridge import (
    atomic_write_destination,
    create_bridge_server,
    handoff_url,
    read_destination,
)


class BrowserBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temp.name) / "destination.json"
        self.server = create_bridge_server(
            token="bridge-secret",
            state_path=self.state_path,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def get_json(self, after: int) -> tuple[int, dict[str, object]]:
        url = (
            f"{self.base}/api/destination"
            f"?token=bridge-secret&after={after}"
        )
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_waiting_page_reuses_current_tab(self) -> None:
        with urllib.request.urlopen(
            f"{self.base}/?token=bridge-secret&after=4",
            timeout=2,
        ) as response:
            page = response.read().decode("utf-8")

        self.assertIn("Switching arXiv Hub", page)
        self.assertIn("window.location.replace(result.url)", page)
        self.assertIn('"after": 4', page)
        self.assertNotIn("window.open", page)

    def test_destination_is_ready_only_after_requested_generation(self) -> None:
        atomic_write_destination(
            self.state_path,
            generation=3,
            url="http://127.0.0.1:8123/?token=viewer",
        )

        _, stale = self.get_json(after=3)
        _, ready = self.get_json(after=2)

        self.assertEqual(stale, {"ready": False})
        self.assertEqual(
            ready,
            {
                "ready": True,
                "generation": 3,
                "url": "http://127.0.0.1:8123/?token=viewer",
            },
        )

    def test_invalid_token_is_not_found(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"{self.base}/api/destination?token=wrong&after=0",
                timeout=2,
            )

        self.assertEqual(caught.exception.code, 404)

    def test_destination_file_is_atomic_and_loopback_only(self) -> None:
        atomic_write_destination(
            self.state_path,
            generation=8,
            url="http://127.0.0.1:9000/report?token=child",
        )

        destination = read_destination(self.state_path)

        self.assertIsNotNone(destination)
        assert destination is not None
        self.assertEqual(destination.generation, 8)
        self.assertEqual(
            destination.url,
            "http://127.0.0.1:9000/report?token=child",
        )
        self.assertEqual(list(self.state_path.parent.glob("*.tmp")), [])
        with self.assertRaises(ValueError):
            atomic_write_destination(
                self.state_path,
                generation=9,
                url="https://example.com/",
            )

    def test_handoff_url_contains_token_and_generation(self) -> None:
        self.assertEqual(
            handoff_url(8124, "secret token", after=6),
            "http://127.0.0.1:8124/?token=secret%20token&after=6",
        )


if __name__ == "__main__":
    unittest.main()
