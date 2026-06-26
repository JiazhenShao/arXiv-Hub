from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from arxiv_daily.history import (
    RATING_WEIGHTS,
    annotation_weight,
    extract_arxiv_id,
    parse_report_history,
    scan_library,
)


class HistoryTests(unittest.TestCase):
    def test_extracts_modern_and_legacy_arxiv_ids(self) -> None:
        self.assertEqual(extract_arxiv_id("[!!!] 2604.26715v1.pdf"), "2604.26715")
        self.assertEqual(extract_arxiv_id("hep-ph_0110026v2.pdf"), "hep-ph/0110026")
        self.assertIsNone(extract_arxiv_id("Qualifier_prep.pdf"))
        self.assertIsNone(
            extract_arxiv_id("ouraring_label_798021930129.pdf")
        )

    def test_filename_annotations_map_to_approved_weights(self) -> None:
        self.assertEqual(annotation_weight("[!!!] 2604.26715v1.pdf"), 4.0)
        self.assertEqual(annotation_weight("[!!] 2604.26715v1.pdf"), 3.0)
        self.assertEqual(annotation_weight("[!] 2604.26715v1.pdf"), 2.0)
        self.assertEqual(annotation_weight("[high] 2604.26715v1.pdf"), 4.0)
        self.assertEqual(annotation_weight("[medium] 2604.26715v1.pdf"), 2.0)
        self.assertEqual(annotation_weight("[low] 2604.26715v1.pdf"), 0.5)
        self.assertEqual(annotation_weight("[skip] 2604.26715v1.pdf"), -3.0)
        self.assertEqual(annotation_weight("[unrated] 2604.26715v1.pdf"), 0.25)
        self.assertEqual(annotation_weight("2604.26715v1.pdf"), 1.0)

    def test_scan_library_ignores_non_arxiv_files_and_sorts_recent_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            older = root / "1 Jan 2025"
            newer = root / "2 Jan 2025 [!]"
            older.mkdir()
            newer.mkdir()
            (older / "2501.00001v1.pdf").touch()
            (newer / "[!!] 2501.00002v1.pdf").touch()
            (newer / "notes.pdf").touch()

            items = scan_library([root], limit=10)

        self.assertEqual([item.arxiv_id for item in items], [
            "2501.00002",
            "2501.00001",
        ])
        self.assertEqual(items[0].weight, 3.0)
        self.assertEqual(items[0].observed_date, date(2025, 1, 2))

    def test_library_scan_exposes_manual_filename_marker_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "2606.00001v1.pdf"
            original.write_bytes(b"%PDF-test")
            first = scan_library([root], limit=10)[0]
            renamed = root / "[!!!] 2606.00001v1.pdf"
            original.rename(renamed)
            second = scan_library([root], limit=10)[0]

        self.assertFalse(first.explicit)
        self.assertEqual(first.fingerprint, "unmarked")
        self.assertTrue(second.explicit)
        self.assertEqual(second.fingerprint, "!!!")

    def test_report_scan_exposes_direct_markdown_rating_changes(self) -> None:
        report_fixture = (
            '<!-- arxiv-record:{"arxiv_id":"2606.00001",'
            '"versioned_id":"2606.00001v1","title":"One","abstract":"A",'
            '"authors":["A"],"categories":["nucl-th"],'
            '"primary_category":"nucl-th",'
            '"published":"2026-06-08T00:00:00+00:00",'
            '"updated":"2026-06-08T00:00:00+00:00",'
            '"abs_url":"https://arxiv.org/abs/2606.00001v1",'
            '"pdf_url":"https://arxiv.org/pdf/2606.00001v1"} -->\n'
            '**Interest:** high\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-06-08.md"
            path.write_text(report_fixture, encoding="utf-8")
            first = parse_report_history(Path(tmp), limit=100)[0]
            path.write_text(
                report_fixture.replace("Interest:** high", "Interest:** skip"),
                encoding="utf-8",
            )
            second = parse_report_history(Path(tmp), limit=100)[0]

        self.assertEqual(first.fingerprint, "2026-06-08.md:high")
        self.assertEqual(second.fingerprint, "2026-06-08.md:skip")

    def test_report_ratings_override_unrated_exposure_weight(self) -> None:
        report = """# Daily arXiv Recommendations

<!-- arxiv-record:{"arxiv_id":"2606.00001","versioned_id":"2606.00001v1","title":"One","abstract":"A","authors":["A"],"categories":["nucl-th"],"primary_category":"nucl-th","published":"2026-06-08T00:00:00+00:00","updated":"2026-06-08T00:00:00+00:00","abs_url":"https://arxiv.org/abs/2606.00001v1","pdf_url":"https://arxiv.org/pdf/2606.00001v1"} -->
**Interest:** strong

<!-- arxiv-record:{"arxiv_id":"2606.00002","versioned_id":"2606.00002v1","title":"Two","abstract":"B","authors":["B"],"categories":["hep-ph"],"primary_category":"hep-ph","published":"2026-06-08T00:00:00+00:00","updated":"2026-06-08T00:00:00+00:00","abs_url":"https://arxiv.org/abs/2606.00002v1","pdf_url":"https://arxiv.org/pdf/2606.00002v1"} -->
**Interest:** skip
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "2026-06-08.md"
            path.write_text(report, encoding="utf-8")
            items = parse_report_history(Path(tmp), limit=100)

        self.assertEqual([(item.paper.arxiv_id, item.weight) for item in items], [
            ("2606.00001", 4.0),
            ("2606.00002", -3.0),
        ])

    def test_fine_grained_ratings_and_legacy_aliases_have_expected_weights(self) -> None:
        self.assertEqual(RATING_WEIGHTS["high"], 4.0)
        self.assertEqual(RATING_WEIGHTS["medium"], 2.0)
        self.assertEqual(RATING_WEIGHTS["low"], 0.5)
        self.assertEqual(RATING_WEIGHTS["unrated"], 0.25)
        self.assertEqual(RATING_WEIGHTS["skip"], -3.0)
        self.assertEqual(RATING_WEIGHTS["strong"], RATING_WEIGHTS["high"])
        self.assertEqual(RATING_WEIGHTS["maybe"], RATING_WEIGHTS["medium"])

    def test_report_history_accepts_fine_grained_ratings(self) -> None:
        records = []
        for index, rating in enumerate(
            ("high", "medium", "low", "unrated", "skip"),
            start=1,
        ):
            records.append(
                '<!-- arxiv-record:'
                f'{{"arxiv_id":"2606.0000{index}",'
                f'"versioned_id":"2606.0000{index}v1",'
                f'"title":"Paper {index}","abstract":"A","authors":["A"],'
                '"categories":["nucl-th"],"primary_category":"nucl-th",'
                '"published":"2026-06-08T00:00:00+00:00",'
                '"updated":"2026-06-08T00:00:00+00:00",'
                f'"abs_url":"https://arxiv.org/abs/2606.0000{index}v1",'
                f'"pdf_url":"https://arxiv.org/pdf/2606.0000{index}v1"}} -->\n'
                f"**Interest:** {rating}\n"
            )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "2026-06-08.md").write_text(
                "\n".join(records),
                encoding="utf-8",
            )
            items = parse_report_history(Path(tmp), limit=100)

        self.assertEqual(
            [item.weight for item in items],
            [4.0, 2.0, 0.5, 0.25, -3.0],
        )


if __name__ == "__main__":
    unittest.main()
