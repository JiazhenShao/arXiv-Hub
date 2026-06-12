from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from arxiv_daily.downloader import (
    DownloadError,
    DownloadManifest,
    PaperDownloader,
    collect_effective_papers,
    managed_filename,
)


def report_entry(
    *,
    arxiv_id: str = "2606.11959",
    versioned_id: str = "2606.11959v1",
    title: str = "Identifiability of g-mode Resonances in Binary Neutron Stars",
    rating: str = "high",
    matched: str = "neutron stars, nucl-th",
    categories: tuple[str, ...] = ("astro-ph.HE", "nucl-th"),
) -> str:
    metadata = json.dumps(
        {
            "arxiv_id": arxiv_id,
            "versioned_id": versioned_id,
            "title": title,
            "abstract": "Verified abstract.",
            "authors": ["A. Author"],
            "categories": list(categories),
            "primary_category": categories[0],
            "published": "2026-06-10T00:00:00+00:00",
            "updated": "2026-06-10T00:00:00+00:00",
            "abs_url": f"https://arxiv.org/abs/{versioned_id}",
            "pdf_url": f"https://arxiv.org/pdf/{versioned_id}",
        },
        separators=(",", ":"),
    )
    return (
        f"<!-- arxiv-record:{metadata} -->\n"
        f"- **Matched interests:** {matched}\n"
        f"- **Interest:** {rating}\n"
    )


class EffectivePaperTests(unittest.TestCase):
    def test_newest_report_rating_wins_for_each_arxiv_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-09.md").write_text(
                report_entry(rating="high"),
                encoding="utf-8",
            )
            (root / "2026-06-10.md").write_text(
                report_entry(rating="skip"),
                encoding="utf-8",
            )

            papers = collect_effective_papers(root)

        self.assertEqual(papers["2606.11959"].rating, "skip")
        self.assertEqual(papers["2606.11959"].report_date, "2026-06-10")

    def test_filename_uses_lowercase_tags_and_keeps_versioned_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-10.md").write_text(
                report_entry(),
                encoding="utf-8",
            )
            candidate = collect_effective_papers(root)["2606.11959"]

            filename = managed_filename(candidate)

        self.assertEqual(
            filename,
            "[!!!][NS] "
            "Identifiability of g-mode Resonances in Binary Neutron Stars "
            "- [astro-ph.HE][nucl-th]2606.11959v1.pdf",
        )

    def test_filename_uses_compact_concepts_and_selected_cross_lists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-10.md").write_text(
                report_entry(
                    title="Renormalization Group Constraints on Dense Matter",
                    matched="neutron stars, hep-ph",
                    categories=(
                        "astro-ph.HE",
                        "gr-qc",
                        "nucl-th",
                        "hep-ph",
                    ),
                ).replace(
                    '"abstract":"Verified abstract."',
                    '"abstract":"We constrain the neutron-star equation of state '
                    'with a renormalization group calculation."',
                ),
                encoding="utf-8",
            )
            candidate = collect_effective_papers(root)["2606.11959"]

            filename = managed_filename(candidate)

        self.assertTrue(filename.startswith("[!!!][NS][EoS]"))
        self.assertIn("[EoS]", filename)
        self.assertTrue(
            filename.endswith(
                " - [astro-ph.HE][nucl-th][hep-ph]2606.11959v1.pdf"
            )
        )
        self.assertNotIn("[gr-qc]", filename)

    def test_primary_hep_ph_category_is_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-10.md").write_text(
                report_entry(
                    matched="field theory and RG methods",
                    categories=("hep-ph", "nucl-th", "gr-qc"),
                ),
                encoding="utf-8",
            )
            candidate = collect_effective_papers(root)["2606.11959"]

            filename = managed_filename(candidate)

        self.assertTrue(filename.startswith("[!!!][RG]"))
        self.assertTrue(
            filename.endswith(" - [hep-ph][nucl-th]2606.11959v1.pdf")
        )
        self.assertEqual(filename.count("[hep-ph]"), 1)

    def test_filename_rating_markers_are_symbolic_when_available(self) -> None:
        expected = {
            "high": "[!!!]",
            "medium": "[!!]",
            "low": "[!]",
            "skip": "[skip]",
            "unrated": "[unrated]",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rating, marker in expected.items():
                report = root / f"2026-06-{10 + len(list(root.glob('*.md'))):02d}.md"
                report.write_text(
                    report_entry(rating=rating),
                    encoding="utf-8",
                )
                candidate = collect_effective_papers(root)["2606.11959"]
                self.assertTrue(
                    managed_filename(candidate).startswith(marker),
                    rating,
                )

    def test_filename_sanitizes_tex_and_stays_within_180_utf8_bytes(self) -> None:
        title = (
            r"An $f(\mathbb{Q},\mathcal{L}_m)$ Study: "
            + "Neutron-Star Matter " * 20
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2026-06-10.md").write_text(
                report_entry(title=title, matched="field theory and RG methods"),
                encoding="utf-8",
            )
            candidate = collect_effective_papers(root)["2606.11959"]

            filename = managed_filename(candidate)

        self.assertLessEqual(len(filename.encode("utf-8")), 180)
        self.assertNotIn("\\", filename)
        self.assertNotIn("$", filename)
        self.assertNotIn(":", filename)
        self.assertTrue(
            filename.endswith(
                " - [astro-ph.HE][nucl-th]2606.11959v1.pdf"
            )
        )
        self.assertIn("[RG]", filename)


class ManifestTests(unittest.TestCase):
    def test_manifest_round_trips_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "downloads.json"
            manifest = DownloadManifest(path)
            manifest.set(
                arxiv_id="2606.11959",
                versioned_id="2606.11959v1",
                filename="[high][neutron-star] Paper - 2606.11959v1.pdf",
                rating="high",
                tags=("neutron-star",),
                report_date="2026-06-10",
            )

            manifest.save()
            loaded = DownloadManifest.load(path)

            self.assertEqual(
                loaded.entries["2606.11959"].filename,
                "[high][neutron-star] Paper - 2606.11959v1.pdf",
            )
            self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])


class ReconciliationTests(unittest.TestCase):
    def test_report_date_must_reference_a_regular_dated_markdown_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            outside = root / "outside.md"
            outside.write_text(report_entry(), encoding="utf-8")
            (records / "2026-06-09.md").symlink_to(outside)
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )

            for report_date in ["not-a-date", "2026-06-08", "2026-06-09"]:
                with self.subTest(report_date=report_date):
                    with self.assertRaises(DownloadError):
                        downloader.reconcile(report_date)

    def test_download_high_fetches_only_the_selected_report_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            (records / "2026-06-09.md").write_text(
                report_entry(
                    arxiv_id="2606.09001",
                    versioned_id="2606.09001v1",
                    title="Older High Paper",
                ),
                encoding="utf-8",
            )
            (records / "2026-06-10.md").write_text(
                report_entry(
                    arxiv_id="2606.10001",
                    versioned_id="2606.10001v1",
                    title="Selected High Paper",
                ),
                encoding="utf-8",
            )
            fetched: list[str] = []

            def fetch(candidate: object, destination: Path) -> None:
                fetched.append(candidate.arxiv_id)
                destination.write_bytes(b"%PDF-selected")

            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
                fetcher=fetch,
            )

            result = downloader.download_high("2026-06-10")

            self.assertEqual(fetched, ["2606.10001"])
            self.assertEqual(set(result), {"2606.10001"})
            self.assertEqual(result["2606.10001"].state, "downloaded")
            self.assertEqual(len(list(library.glob("*2606.09001v1.pdf"))), 0)

    def test_reconcile_preserves_manifest_entries_from_other_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            (records / "2026-06-09.md").write_text(
                report_entry(
                    arxiv_id="2606.09001",
                    versioned_id="2606.09001v1",
                    title="Other Date Paper",
                ),
                encoding="utf-8",
            )
            (records / "2026-06-10.md").write_text(
                report_entry(
                    arxiv_id="2606.10001",
                    versioned_id="2606.10001v1",
                    title="Selected Date Paper",
                    rating="low",
                ),
                encoding="utf-8",
            )
            other_name = "[high][nucl-th] Other Date Paper - 2606.09001v1.pdf"
            (library / other_name).write_bytes(b"%PDF-other")
            manifest = DownloadManifest(records / ".state" / "downloads.json")
            manifest.set(
                arxiv_id="2606.09001",
                versioned_id="2606.09001v1",
                filename=other_name,
                rating="high",
                tags=("nucl-th",),
                report_date="2026-06-09",
            )
            manifest.save()
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )

            result = downloader.reconcile("2026-06-10")
            reloaded = DownloadManifest.load(records / ".state" / "downloads.json")

            self.assertEqual(set(result), {"2606.10001"})
            self.assertEqual(reloaded.entries["2606.09001"].filename, other_name)
            self.assertTrue((library / other_name).is_file())

    def test_older_duplicate_is_superseded_by_the_newest_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            (records / "2026-06-09.md").write_text(
                report_entry(rating="high"),
                encoding="utf-8",
            )
            (records / "2026-06-10.md").write_text(
                report_entry(rating="skip"),
                encoding="utf-8",
            )
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
                fetcher=lambda candidate, destination: self.fail(
                    "a superseded paper must not download"
                ),
            )

            result = downloader.reconcile("2026-06-09")

            self.assertEqual(result["2606.11959"].state, "superseded")
            self.assertIn("2026-06-10", result["2606.11959"].message)
            self.assertFalse((records / ".state" / "downloads.json").exists())

    def test_reconciliation_removes_abandoned_temporary_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            (records / "2026-06-10.md").write_text("", encoding="utf-8")
            abandoned = library / ".arxiv-download-abandoned.tmp"
            abandoned.write_bytes(b"partial")
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )

            downloader.reconcile("2026-06-10")

            self.assertFalse(abandoned.exists())

    def test_invalid_existing_match_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            (records / "2026-06-10.md").write_text(
                report_entry(),
                encoding="utf-8",
            )
            existing = library / "2606.11959v1.pdf"
            existing.write_bytes(b"<html>not a pdf</html>")
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )

            result = downloader.reconcile("2026-06-10")

            self.assertEqual(result["2606.11959"].state, "failed")
            self.assertTrue(existing.is_file())
            self.assertFalse((records / ".state" / "downloads.json").exists())

    def test_unique_existing_pdf_is_adopted_and_renamed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            (records / "2026-06-10.md").write_text(
                report_entry(),
                encoding="utf-8",
            )
            existing = library / "2606.11959v1.pdf"
            existing.write_bytes(b"%PDF-1.7\nexisting")
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
                fetcher=lambda candidate, destination: self.fail(
                    "adoption must not download"
                ),
            )

            result = downloader.reconcile("2026-06-10")

            self.assertEqual(result["2606.11959"].state, "existing")
            expected = library / (
                "[!!!][NS] "
                "Identifiability of g-mode Resonances in Binary Neutron Stars "
                "- [astro-ph.HE][nucl-th]2606.11959v1.pdf"
            )
            self.assertTrue(expected.is_file())
            self.assertFalse(existing.exists())

    def test_multiple_existing_matches_are_reported_as_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            (records / "2026-06-10.md").write_text(
                report_entry(),
                encoding="utf-8",
            )
            (library / "first 2606.11959v1.pdf").write_bytes(b"%PDF-one")
            (library / "second 2606.11959v1.pdf").write_bytes(b"%PDF-two")
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )

            result = downloader.reconcile("2026-06-10")

            self.assertEqual(result["2606.11959"].state, "conflict")
            self.assertEqual(len(list(library.glob("*.pdf"))), 2)

    def test_rating_change_renames_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            report_path = records / "2026-06-10.md"
            report_path.write_text(report_entry(), encoding="utf-8")
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )
            old_name = managed_filename(
                collect_effective_papers(records)["2606.11959"]
            )
            (library / old_name).write_bytes(b"%PDF-managed")
            downloader.reconcile("2026-06-10")
            report_path.write_text(
                report_entry(rating="medium"),
                encoding="utf-8",
            )

            result = downloader.reconcile("2026-06-10")

            self.assertEqual(result["2606.11959"].state, "downloaded")
            self.assertFalse((library / old_name).exists())
            self.assertEqual(len(list(library.glob("[[]!![]]*.pdf"))), 1)

    def test_newer_version_replaces_old_only_after_successful_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            report_path = records / "2026-06-10.md"
            report_path.write_text(
                report_entry(versioned_id="2606.11959v1"),
                encoding="utf-8",
            )
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )
            old_name = managed_filename(
                collect_effective_papers(records)["2606.11959"]
            )
            (library / old_name).write_bytes(b"%PDF-old")
            downloader.reconcile("2026-06-10")
            report_path.write_text(
                report_entry(versioned_id="2606.11959v2"),
                encoding="utf-8",
            )

            def fail_fetch(candidate: object, destination: Path) -> None:
                raise DownloadError("temporary failure")

            downloader.fetcher = fail_fetch
            failed = downloader.download_high("2026-06-10")
            self.assertEqual(failed["2606.11959"].state, "failed")
            self.assertTrue((library / old_name).is_file())

            downloader.fetcher = (
                lambda candidate, destination: destination.write_bytes(
                    b"%PDF-new"
                )
            )
            succeeded = downloader.download_high("2026-06-10")

            self.assertEqual(succeeded["2606.11959"].state, "downloaded")
            self.assertFalse((library / old_name).exists())
            new_files = list(library.glob("*2606.11959v2.pdf"))
            self.assertEqual(len(new_files), 1)
            self.assertEqual(new_files[0].read_bytes(), b"%PDF-new")

    def test_newer_managed_version_is_not_downgraded_by_older_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = root / "records"
            library = root / "library"
            records.mkdir()
            library.mkdir()
            report_path = records / "2026-06-10.md"
            report_path.write_text(
                report_entry(versioned_id="2606.11959v2"),
                encoding="utf-8",
            )
            downloader = PaperDownloader(
                record_dir=records,
                library_dir=library,
                state_dir=records / ".state",
            )
            v2_name = managed_filename(
                collect_effective_papers(records)["2606.11959"]
            )
            (library / v2_name).write_bytes(b"%PDF-v2")
            downloader.reconcile("2026-06-10")
            report_path.write_text(
                report_entry(versioned_id="2606.11959v1"),
                encoding="utf-8",
            )
            downloader.fetcher = lambda candidate, destination: self.fail(
                "newer local version must not be downgraded"
            )

            result = downloader.download_high("2026-06-10")

            self.assertEqual(result["2606.11959"].state, "downloaded")
            self.assertEqual(len(list(library.glob("*2606.11959v2.pdf"))), 1)
            self.assertEqual(len(list(library.glob("*2606.11959v1.pdf"))), 0)
