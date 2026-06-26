from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from arxiv_daily.preferences import PreferenceEvidence, PreferenceSignalStore


class PreferenceSignalTests(unittest.TestCase):
    def evidence(
        self,
        source: str,
        weight: float,
        changed_at: datetime,
        fingerprint: str,
        *,
        explicit: bool = True,
    ) -> PreferenceEvidence:
        return PreferenceEvidence(
            source=source,
            weight=weight,
            explicit=explicit,
            fingerprint=fingerprint,
            fallback_changed_at=changed_at,
        )

    def test_newest_explicit_signal_wins_and_report_wins_ties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PreferenceSignalStore(Path(tmp) / "signals.json")
            older = datetime(2026, 6, 1, tzinfo=timezone.utc)
            newer = datetime(2026, 6, 2, tzinfo=timezone.utc)

            result = store.resolve(
                "2606.00001",
                report=self.evidence("report", -3.0, older, "report:skip"),
                library=self.evidence("library", 4.0, newer, "file:!!!"),
            )
            self.assertEqual((result.source, result.weight), ("library", 4.0))

            tied = store.resolve(
                "2606.00002",
                report=self.evidence("report", 2.0, newer, "report:medium"),
                library=self.evidence("library", -3.0, newer, "file:skip"),
            )
            self.assertEqual((tied.source, tied.weight), ("report", 2.0))

    def test_unmarked_library_never_overrides_explicit_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PreferenceSignalStore(Path(tmp) / "signals.json")
            report = self.evidence(
                "report",
                -3.0,
                datetime(2026, 6, 1, tzinfo=timezone.utc),
                "report:skip",
            )
            library = self.evidence(
                "library",
                1.0,
                datetime(2026, 6, 3, tzinfo=timezone.utc),
                "file:unmarked",
                explicit=False,
            )

            result = store.resolve("2606.00001", report=report, library=library)

            self.assertEqual((result.source, result.weight), ("report", -3.0))

    def test_changed_fingerprint_records_observation_time_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signals.json"
            first_seen = datetime(2026, 6, 1, tzinfo=timezone.utc)
            changed = datetime(2026, 6, 5, tzinfo=timezone.utc)
            store = PreferenceSignalStore(path, now=lambda: changed)
            store.resolve(
                "2606.00001",
                report=self.evidence("report", 4.0, first_seen, "report:high"),
                library=None,
            )
            store.save()

            reloaded = PreferenceSignalStore(path, now=lambda: changed)
            result = reloaded.resolve(
                "2606.00001",
                report=self.evidence("report", -3.0, first_seen, "report:skip"),
                library=None,
            )
            reloaded.save()

            self.assertEqual(result.changed_at, changed)
            self.assertEqual(result.weight, -3.0)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertFalse(path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
