from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from arxiv_daily.models import Paper
from arxiv_daily.seed_library import (
    SeedScanError,
    scan_seed_library,
    suggest_profile,
    verify_seed_candidates,
)


def paper(
    arxiv_id: str,
    *,
    title: str,
    abstract: str,
    categories: tuple[str, ...],
) -> Paper:
    return Paper(
        arxiv_id=arxiv_id,
        versioned_id=f"{arxiv_id}v1",
        title=title,
        abstract=abstract,
        authors=("Researcher",),
        categories=categories,
        primary_category=categories[0],
        published=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated=datetime(2026, 1, 1, tzinfo=timezone.utc),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v1",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v1",
    )


class FakePage:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text


class FakeReader:
    def __init__(
        self,
        *,
        metadata: dict[str, str] | None = None,
        pages: tuple[str, ...] = (),
        encrypted: bool = False,
    ) -> None:
        self.metadata = metadata or {}
        self.pages = [FakePage(text) for text in pages]
        self.is_encrypted = encrypted


class SeedLibraryTests(unittest.TestCase):
    def test_detects_ids_from_filename_metadata_and_first_two_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            named = root / "2501.01234v2.pdf"
            metadata = root / "renamed-paper.pdf"
            text = root / "another-paper.pdf"
            for path in (named, metadata, text):
                path.write_bytes(b"%PDF-test")
            readers = {
                metadata: FakeReader(metadata={"/Subject": "arXiv:2408.06789"}),
                text: FakeReader(
                    pages=(
                        "Accepted manuscript",
                        "Preprint arXiv:2312.12345v3",
                        "This third page must not be needed",
                    )
                ),
            }
            calls: list[Path] = []

            result = scan_seed_library(
                root,
                cache_path=root / ".cache.json",
                limit=100,
                reader_factory=lambda path: (
                    calls.append(path) or readers[path]
                ),
            )

            self.assertEqual(
                {item.arxiv_id for item in result.candidates},
                {"2501.01234", "2408.06789", "2312.12345"},
            )
            self.assertEqual(calls, [text, metadata])
            self.assertEqual(result.counts["identified"], 3)

    def test_reports_encrypted_corrupt_duplicate_and_unrecognized_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {
                name: root / name
                for name in (
                    "one-2501.00001.pdf",
                    "duplicate-2501.00001.pdf",
                    "encrypted.pdf",
                    "corrupt.pdf",
                    "notes.pdf",
                )
            }
            for path in paths.values():
                path.write_bytes(b"%PDF-test")

            def reader(path: Path) -> FakeReader:
                if path == paths["encrypted.pdf"]:
                    return FakeReader(encrypted=True)
                if path == paths["corrupt.pdf"]:
                    raise ValueError("broken xref")
                return FakeReader()

            result = scan_seed_library(
                root,
                cache_path=root / ".cache.json",
                limit=100,
                reader_factory=reader,
            )

            self.assertEqual(result.counts["identified"], 1)
            self.assertEqual(result.counts["duplicate"], 1)
            self.assertEqual(result.counts["encrypted"], 1)
            self.assertEqual(result.counts["corrupt"], 1)
            self.assertEqual(result.counts["unrecognized"], 1)

    def test_reuses_cache_and_never_changes_source_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "renamed.pdf"
            original = b"%PDF-immutable"
            source.write_bytes(original)
            cache = root / ".state" / "seed-library-index.json"
            calls = 0

            def reader(path: Path) -> FakeReader:
                nonlocal calls
                calls += 1
                return FakeReader(metadata={"/Title": "arXiv:2502.00002"})

            first = scan_seed_library(
                root,
                cache_path=cache,
                limit=100,
                reader_factory=reader,
            )
            second = scan_seed_library(
                root,
                cache_path=cache,
                limit=100,
                reader_factory=reader,
            )

            self.assertEqual(first.candidates, second.candidates)
            self.assertEqual(calls, 1)
            self.assertEqual(source.read_bytes(), original)

    def test_rejects_symlinked_root_and_skips_symlinked_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            root.mkdir()
            real = root / "2501.00001.pdf"
            real.write_bytes(b"%PDF-test")
            linked = root / "2501.00002.pdf"
            linked.symlink_to(real)
            root_link = Path(tmp) / "library-link"
            root_link.symlink_to(root, target_is_directory=True)

            result = scan_seed_library(
                root,
                cache_path=Path(tmp) / "cache.json",
                limit=100,
                reader_factory=lambda path: FakeReader(),
            )

            self.assertEqual(
                [item.arxiv_id for item in result.candidates],
                ["2501.00001"],
            )
            self.assertEqual(result.counts["symlink"], 1)
            with self.assertRaises(SeedScanError):
                scan_seed_library(
                    root_link,
                    cache_path=Path(tmp) / "cache-2.json",
                    limit=100,
                    reader_factory=lambda path: FakeReader(),
                )

    def test_verification_excludes_ids_missing_from_official_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "2501.00001.pdf").write_bytes(b"%PDF-test")
            (root / "2501.00002.pdf").write_bytes(b"%PDF-test")
            scan = scan_seed_library(
                root,
                cache_path=root / ".cache.json",
                limit=100,
                reader_factory=lambda path: FakeReader(),
            )

            verified = verify_seed_candidates(
                scan,
                fetcher=lambda ids: [
                    paper(
                        "2501.00001",
                        title="Dense matter",
                        abstract="Neutron star equation of state",
                        categories=("nucl-th",),
                    )
                ],
            )

            self.assertEqual(
                [item.arxiv_id for item in verified.papers],
                ["2501.00001"],
            )
            self.assertEqual(verified.counts["unverified"], 1)

    def test_suggestions_are_deterministic_and_category_weighted(self) -> None:
        papers = [
            paper(
                "2501.00001",
                title="Neutron star dense matter",
                abstract="Equation of state and tidal deformability",
                categories=("nucl-th", "astro-ph.HE"),
            ),
            paper(
                "2501.00002",
                title="Dense nuclear matter",
                abstract="Neutron star equation of state",
                categories=("nucl-th",),
            ),
        ]
        embeddings = {
            "2501.00001": [1.0, 0.0],
            "2501.00002": [0.98, 0.02],
        }

        first = suggest_profile(papers, embeddings)
        second = suggest_profile(papers, embeddings)

        self.assertEqual(first, second)
        self.assertEqual(first.categories[0].name, "nucl-th")
        self.assertEqual(first.categories[0].weight, 1.0)
        self.assertIn("neutron star", first.topics[0].phrases)


if __name__ == "__main__":
    unittest.main()
