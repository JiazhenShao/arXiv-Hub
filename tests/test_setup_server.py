from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from arxiv_daily.setup import load_profile_editor
from scripts.setup_profile import (
    create_setup_server,
    default_editor_state,
    editor_page,
)
from tests.test_setup_editor import PROFILE


class SetupPageTests(unittest.TestCase):
    def test_page_shows_guided_editor_and_pdf_folder_with_current_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile.toml"
            profile.write_text(PROFILE, encoding="utf-8")
            state = load_profile_editor(profile)

            page = editor_page(
                "secret",
                state,
                supervisor_handoff_url=(
                    "http://127.0.0.1:9001/?token=bridge&after=2"
                ),
            )

        self.assertIn("Edit my interests", page)
        self.assertIn("Learn from a PDF folder", page)
        self.assertIn("Scan PDF Library", page)
        self.assertIn("nuclear astrophysics", page)
        self.assertIn("dense matter", page)
        self.assertIn("nucl-th", page)
        self.assertNotIn("Edit TOML carefully", page)
        self.assertIn(
            '"saveHandoffUrl": '
            '"http://127.0.0.1:9001/?token=bridge&after=2"',
            page,
        )
        self.assertIn(
            '"closeHandoffUrl": '
            '"http://127.0.0.1:9001/?token=bridge&after=2"',
            page,
        )
        self.assertEqual(
            page.count("window.location.replace("),
            2,
        )
        self.assertNotIn("window.open(config.", page)


class SetupServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.toml"
        self.profile.write_text(PROFILE, encoding="utf-8")
        self.scan_called = threading.Event()

        def scan_runner(folder: Path, limit: int) -> dict[str, object]:
            self.scan_called.set()
            return {
                "counts": {"verified": 2, "unrecognized": 1},
                "papers": [
                    {"arxiv_id": "2501.00001", "title": "Dense matter"},
                    {"arxiv_id": "2501.00002", "title": "Neutron stars"},
                ],
                "suggestion": {
                    "categories": [{"name": "nucl-th", "weight": 1.0}],
                    "topics": [
                        {
                            "name": "neutron star",
                            "weight": 1.0,
                            "phrases": ["neutron star", "dense matter"],
                        }
                    ],
                },
            }

        self.server = create_setup_server(
            profile_path=self.profile,
            token="test-token",
            scan_runner=scan_runner,
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(
        self,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        origin: str | None = None,
    ) -> tuple[int, dict[str, object] | str]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={
                **({"Content-Type": "application/json"} if data else {}),
                **({"Origin": origin} if origin else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                text = response.read().decode("utf-8")
                if response.headers.get_content_type() == "application/json":
                    return response.status, json.loads(text)
                return response.status, text
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8")
            try:
                body: dict[str, object] | str = json.loads(text)
            except json.JSONDecodeError:
                body = text
            return exc.code, body

    def test_scan_runs_in_background_and_returns_suggestions(self) -> None:
        seed = self.root / "seed"
        seed.mkdir()
        status, started = self.request(
            "/api/seed/scan?token=test-token",
            payload={"folder": str(seed), "limit": 100},
            origin=self.base,
        )

        self.assertEqual(status, 202)
        self.assertTrue(started["ok"])
        self.assertTrue(self.scan_called.wait(timeout=2))
        for _ in range(50):
            _, result = self.request(
                "/api/seed/status?token=test-token",
            )
            if result["state"] != "running":
                break
            time.sleep(0.01)

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["counts"]["verified"], 2)
        self.assertEqual(
            result["suggestion"]["topics"][0]["name"],
            "neutron star",
        )

    def test_save_requires_same_origin_and_creates_backup(self) -> None:
        state = load_profile_editor(self.profile)
        payload = {
            "record_dir": str(state.record_dir),
            "active_library_dir": str(state.active_library_dir),
            "archive_library_dir": str(state.archive_library_dir),
            "seed_library_dir": "",
            "seed_library_limit": 100,
            "timezone": state.timezone,
            "search_time": state.search_time,
            "categories": [
                {"name": item.name, "weight": item.weight}
                for item in state.categories
            ],
            "topics": [
                {
                    "name": item.name,
                    "weight": item.weight,
                    "phrases": list(item.phrases),
                }
                for item in state.topics
            ],
        }

        forbidden, _ = self.request(
            "/api/setup?token=test-token",
            payload=payload,
            origin="http://evil.example",
        )
        saved, result = self.request(
            "/api/setup?token=test-token",
            payload=payload,
            origin=self.base,
        )

        self.assertEqual(forbidden, 403)
        self.assertEqual(saved, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(len(list(self.root.glob("profile.toml.backup-*"))), 1)
        round_trip = load_profile_editor(self.profile)
        self.assertEqual(round_trip.categories, state.categories)
        self.assertEqual(round_trip.topics, state.topics)


class FirstRunSetupServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.toml"
        self.state = default_editor_state(
            Path(__file__).resolve().parents[1]
            / "config"
            / "nuclear-particle.toml",
            home=self.root,
        )
        self.server = create_setup_server(
            profile_path=self.profile,
            token="test-token",
            initial_state=self.state,
            supervisor_handoff_url=(
                "http://127.0.0.1:9001/?token=bridge&after=1"
            ),
            return_to_viewer_on_close=False,
            scan_runner=lambda folder, limit: {
                "counts": {},
                "papers": [],
                "suggestion": {"categories": [], "topics": []},
            },
        )
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def post(self, path: str, payload: dict[str, object]) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}?token=test-token",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Origin": self.base,
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, json.loads(response.read())

    def payload(self) -> dict[str, object]:
        return {
            "record_dir": str(self.state.record_dir),
            "active_library_dir": str(self.state.active_library_dir),
            "archive_library_dir": str(self.state.archive_library_dir),
            "seed_library_dir": "",
            "seed_library_limit": self.state.seed_library_limit,
            "timezone": self.state.timezone,
            "search_time": self.state.search_time,
            "categories": [
                {"name": item.name, "weight": item.weight}
                for item in self.state.categories
            ],
            "topics": [
                {
                    "name": item.name,
                    "weight": item.weight,
                    "phrases": list(item.phrases),
                }
                for item in self.state.topics
            ],
        }

    def test_first_run_page_does_not_create_profile_until_save(self) -> None:
        with urllib.request.urlopen(
            f"{self.base}/?token=test-token",
            timeout=3,
        ) as response:
            page = response.read().decode("utf-8")

        self.assertIn("Shape your", page)
        self.assertIn(
            '"saveHandoffUrl": '
            '"http://127.0.0.1:9001/?token=bridge&after=1"',
            page,
        )
        self.assertIn('"closeHandoffUrl": null', page)
        self.assertIn("if (config.saveHandoffUrl)", page)
        self.assertIn("if (config.closeHandoffUrl)", page)
        self.assertFalse(self.profile.exists())

    def test_first_run_save_creates_profile_without_backup_and_stops(self) -> None:
        status, result = self.post("/api/setup", self.payload())

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertTrue(self.profile.is_file())
        self.assertEqual(
            list(self.root.glob("profile.toml.backup-*")),
            [],
        )
        self.assertTrue(self.state.record_dir.is_dir())
        self.assertTrue(self.state.active_library_dir.is_dir())
        self.assertTrue(self.state.archive_library_dir.is_dir())

    def test_first_run_close_stops_without_creating_profile(self) -> None:
        status, result = self.post("/api/shutdown", {})

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse(self.profile.exists())


if __name__ == "__main__":
    unittest.main()
