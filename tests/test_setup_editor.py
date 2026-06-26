from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from arxiv_daily.setup import (
    apply_profile_editor_payload,
    CategoryPreference,
    ProfileEditorState,
    TopicPreference,
    load_profile_editor,
    render_profile_editor,
    save_profile_with_backup,
)
from arxiv_daily.config import ProfileConfig


PROFILE = """
[paths]
record_dir = "/tmp/reports"
active_library_dir = "/tmp/papers"
archive_library_dir = "/tmp/archive"

[source]
user_agent = "existing-agent"
min_interval_seconds = 3.0
retry_backoffs = [10.0, 20.0, 40.0]
retry_deadline_seconds = 90.0
timeout_seconds = 60.0

[categories]
"nucl-th" = 1.0
"hep-ph" = 0.9

[ranking]
report_limit = 20
relevance_threshold = 0.6
backfill_days = 7
announcement_max_age_days = 1
history_report_limit = 100
history_library_limit = 100
recency_decay_days = 180.0
mmr_lambda = 0.75

[model]
base_model = "base"
base_revision = "base-revision"
adapter_model = "adapter"
adapter_revision = "adapter-revision"
batch_size = 12

[[topics]]
name = "nuclear astrophysics"
weight = 1.0
phrases = ["dense matter", "r-process"]

[[topics]]
name = "field theory"
weight = 0.68
phrases = ["renormalization group"]
"""


class ProfileEditorTests(unittest.TestCase):
    def test_loads_existing_profile_without_changing_weights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.toml"
            path.write_text(PROFILE, encoding="utf-8")

            state = load_profile_editor(path)

        self.assertEqual(state.timezone, "America/Chicago")
        self.assertEqual(state.search_time, "20:00")
        self.assertIsNone(state.seed_library_dir)
        self.assertEqual(state.seed_library_limit, 100)
        self.assertEqual(
            state.categories,
            (
                CategoryPreference("nucl-th", 1.0),
                CategoryPreference("hep-ph", 0.9),
            ),
        )
        self.assertEqual(
            state.topics[1],
            TopicPreference(
                "field theory",
                0.68,
                ("renormalization group",),
            ),
        )
        self.assertEqual(state.source["user_agent"], "existing-agent")

    def test_structured_round_trip_adds_seed_folder_and_preserves_settings(self) -> None:
        state = ProfileEditorState(
            record_dir=Path("/tmp/reports"),
            active_library_dir=Path("/tmp/papers"),
            archive_library_dir=Path("/tmp/archive"),
            seed_library_dir=Path("/tmp/seed papers"),
            seed_library_limit=125,
            timezone="America/New_York",
            search_time="21:30",
            categories=(
                CategoryPreference("astro-ph.EP", 0.95),
                CategoryPreference("physics.atm-clus", 0.55),
            ),
            topics=(
                TopicPreference(
                    "exoplanet atmospheres",
                    1.0,
                    ("atmospheric retrieval", "jwst"),
                ),
            ),
            ranking={
                "report_limit": 18,
                "relevance_threshold": 0.62,
                "backfill_days": 7,
                "announcement_max_age_days": 1,
                "history_report_limit": 100,
                "history_library_limit": 100,
                "recency_decay_days": 180.0,
                "mmr_lambda": 0.75,
            },
            model={
                "base_model": "base",
                "base_revision": "base-revision",
                "adapter_model": "adapter",
                "adapter_revision": "adapter-revision",
                "batch_size": 12,
            },
            source={
                "user_agent": "existing-agent",
                "min_interval_seconds": 3.0,
                "retry_backoffs": [10.0, 20.0, 40.0],
                "retry_deadline_seconds": 90.0,
                "timeout_seconds": 60.0,
            },
        )

        rendered = render_profile_editor(state)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.toml"
            path.write_text(rendered, encoding="utf-8")
            config = ProfileConfig.load(path)

        self.assertEqual(config.seed_library_dir, Path("/tmp/seed papers"))
        self.assertEqual(config.seed_library_limit, 125)
        self.assertEqual(config.categories["astro-ph.EP"], 0.95)
        self.assertEqual(config.topics[0].phrases, ("atmospheric retrieval", "jwst"))
        self.assertEqual(config.report_limit, 18)
        self.assertEqual(config.user_agent, "existing-agent")

    def test_save_creates_timestamped_backup_before_replacing_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.toml"
            path.write_text("old profile\n", encoding="utf-8")

            backup = save_profile_with_backup(
                path,
                "new profile\n",
                now=datetime(2026, 6, 12, 14, 30, 45),
            )

            self.assertEqual(path.read_text(encoding="utf-8"), "new profile\n")
            self.assertEqual(
                backup.name,
                "profile.toml.backup-20260612-143045",
            )
            self.assertEqual(
                backup.read_text(encoding="utf-8"),
                "old profile\n",
            )

    def test_browser_payload_updates_only_editable_profile_fields(self) -> None:
        original = load_profile_editor_from_text(PROFILE)
        updated = apply_profile_editor_payload(
            original,
            {
                "record_dir": "/tmp/new-reports",
                "active_library_dir": "/tmp/new-papers",
                "archive_library_dir": "/tmp/new-archive",
                "seed_library_dir": "/tmp/seed",
                "seed_library_limit": 150,
                "timezone": "Europe/Paris",
                "search_time": "19:45",
                "categories": [
                    {"name": "astro-ph.EP", "weight": 0.91},
                ],
                "topics": [
                    {
                        "name": "exoplanets",
                        "weight": 0.88,
                        "phrases": ["atmosphere", "transit spectroscopy"],
                    }
                ],
            },
        )

        self.assertEqual(updated.record_dir, Path("/tmp/new-reports"))
        self.assertEqual(updated.seed_library_dir, Path("/tmp/seed"))
        self.assertEqual(updated.seed_library_limit, 150)
        self.assertEqual(
            updated.categories,
            (CategoryPreference("astro-ph.EP", 0.91),),
        )
        self.assertEqual(updated.source, original.source)
        self.assertEqual(updated.model, original.model)
        self.assertEqual(updated.ranking, original.ranking)


def load_profile_editor_from_text(text: str) -> ProfileEditorState:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "profile.toml"
        path.write_text(text, encoding="utf-8")
        return load_profile_editor(path)


if __name__ == "__main__":
    unittest.main()
