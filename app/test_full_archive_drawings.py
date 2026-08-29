"""Tests for app/full_archive.py's handling of drawn annotations
(app/database.py's `drawings` table), covering the bug where drawn
highlights disappeared after an export/import round trip: they were
never written into the archive manifest in the first place, so importing
one produced a book with no drawings at all, and nothing showed up in
the Highlights list to find or delete.

Uses a real Database, a real (temporary) PDF, and a real zip archive on
disk throughout -- no mocks -- matching this project's existing testing
convention.
"""
import os
import sys
import tempfile
import unittest

import pymupdf as fitz
from PySide6.QtWidgets import QApplication, QMessageBox

app = QApplication.instance() or QApplication(sys.argv)
QMessageBox.information = staticmethod(lambda *a, **k: None)

from app.database import Database
from app.full_archive import apply_archive, build_manifest, write_archive
from app.reader_window import ReaderWindow


def _make_real_pdf(pages=3, width=400, height=600):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), f"page {i}")
    doc.save(tmp_pdf)
    return tmp_pdf


class ArchiveTestCase(unittest.TestCase):
    """Common setup: a source library with one book carrying both a
    drawing and a text highlight, ready to export."""

    def setUp(self):
        self.src_db_path = tempfile.mktemp(suffix=".db")
        self.src_db = Database(self.src_db_path)
        self.tmp_pdf = _make_real_pdf()
        self.book = self.src_db.add_book(self.tmp_pdf, "Archive Test Book", 3)
        self.drawing_id = self.src_db.add_drawing(
            self.book["id"], 1, "triangle", "#FF00AA", 0.55, 4.0, [[20, 30], [80, 90]]
        )
        self.highlight_id = self.src_db.add_highlight(
            self.book["id"], 0, "#3878FF", [[10, 10, 100, 30]], text="hello world", label="My highlight"
        )
        self.zip_path = tempfile.mktemp(suffix=".zip")

    def tearDown(self):
        for path in (self.src_db_path, self.tmp_pdf, self.zip_path):
            if os.path.exists(path):
                os.remove(path)

    def _export(self, include_reading_state=True):
        manifest, filepaths = build_manifest(self.src_db, include_reading_state=include_reading_state)
        write_archive(self.zip_path, manifest, filepaths)
        return manifest

    def _fresh_destination(self):
        dst_db_path = tempfile.mktemp(suffix=".db")
        dst_db = Database(dst_db_path)
        dest_dir = tempfile.mkdtemp()
        return dst_db, dst_db_path, dest_dir


class TestManifestIncludesDrawings(ArchiveTestCase):
    def test_drawing_appears_in_the_manifest(self):
        manifest = self._export()
        entry = manifest["books"][os.path.basename(self.tmp_pdf)]
        self.assertIn("drawings", entry)
        self.assertEqual(len(entry["drawings"]), 1)
        d = entry["drawings"][0]
        self.assertEqual(d["page_number"], 1)
        self.assertEqual(d["tool"], "triangle")
        self.assertEqual(d["color"], "#FF00AA")
        self.assertAlmostEqual(d["opacity"], 0.55)
        self.assertEqual(d["stroke_width"], 4.0)
        self.assertEqual(d["points"], [[20, 30], [80, 90]])

    def test_share_mode_excludes_drawings_same_as_highlights(self):
        """A share-oriented export (include_reading_state=False) already
        omits highlights, since covering someone else's book in your own
        markup would be surprising -- drawings are exactly the same kind
        of personal reading data and must be excluded the same way."""
        manifest = self._export(include_reading_state=False)
        entry = manifest["books"][os.path.basename(self.tmp_pdf)]
        self.assertNotIn("drawings", entry)
        self.assertNotIn("highlights", entry)


class TestImportRestoresDrawings(ArchiveTestCase):
    def test_drawing_is_imported_into_a_fresh_library(self):
        self._export()
        dst_db, dst_db_path, dest_dir = self._fresh_destination()
        try:
            summary = apply_archive(dst_db, self.zip_path, dest_dir)
            self.assertEqual(summary["drawings_added"], 1)
            self.assertEqual(summary["highlights_added"], 1)

            new_book = dst_db.get_book_by_filename(os.path.basename(self.tmp_pdf))
            drawings = dst_db.get_drawings_for_page(new_book["id"], 1)
            self.assertEqual(len(drawings), 1)
            d = drawings[0]
            self.assertEqual(d["tool"], "triangle")
            self.assertEqual(d["color"], "#FF00AA")
            self.assertAlmostEqual(d["opacity"], 0.55)
            self.assertEqual(d["stroke_width"], 4.0)
            self.assertEqual(d["points"], [[20, 30], [80, 90]])
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_imported_drawing_appears_in_the_highlights_list(self):
        """The actual bug report: not only must the drawing exist in the
        database after import, it must show up in the unified Highlights
        list a real ReaderWindow builds, exactly like a real drawing made
        and saved directly would."""
        self._export()
        dst_db, dst_db_path, dest_dir = self._fresh_destination()
        try:
            apply_archive(dst_db, self.zip_path, dest_dir)
            new_book = dst_db.get_book_by_filename(os.path.basename(self.tmp_pdf))
            win = ReaderWindow(dst_db, new_book["id"])
            try:
                win.refresh_highlights()
                self.assertEqual(win.highlight_list.count(), 2)
                kinds = {win.highlight_list.item(i).data(1002) for i in range(2)}
                # Qt.UserRole is 0x0100; UserRole+2 == 0x0102 == 258
                from PySide6.QtCore import Qt
                kinds = {win.highlight_list.item(i).data(Qt.UserRole + 2) for i in range(2)}
                self.assertEqual(kinds, {"highlight", "drawing"})
                texts = [win.highlight_list.item(i).text() for i in range(2)]
                self.assertTrue(any("Triangle" in t for t in texts))
            finally:
                win.close()
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_reimporting_the_same_archive_does_not_duplicate_drawings(self):
        self._export()
        dst_db, dst_db_path, dest_dir = self._fresh_destination()
        try:
            apply_archive(dst_db, self.zip_path, dest_dir)
            summary2 = apply_archive(dst_db, self.zip_path, dest_dir)
            self.assertEqual(summary2["drawings_added"], 0)
            new_book = dst_db.get_book_by_filename(os.path.basename(self.tmp_pdf))
            self.assertEqual(len(dst_db.get_drawings_for_page(new_book["id"], 1)), 1)
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_importing_into_a_library_that_already_has_the_book_still_adds_the_drawing(self):
        """Matched-by-filename existing book (not newly added from the
        archive) must still pick up drawings it doesn't already have --
        the dedup check is per-drawing, not "skip everything if the book
        already exists"."""
        self._export()
        dst_db, dst_db_path, dest_dir = self._fresh_destination()
        try:
            # Pre-populate the destination with the SAME file/book, no drawings yet
            existing_book = dst_db.add_book(self.tmp_pdf, "Archive Test Book", 3)
            summary = apply_archive(dst_db, self.zip_path, dest_dir)
            self.assertEqual(summary["added"], 0)  # book already existed
            self.assertEqual(summary["drawings_added"], 1)
            self.assertEqual(len(dst_db.get_drawings_for_page(existing_book["id"], 1)), 1)
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_share_mode_export_then_import_brings_no_drawings(self):
        self._export(include_reading_state=False)
        dst_db, dst_db_path, dest_dir = self._fresh_destination()
        try:
            summary = apply_archive(dst_db, self.zip_path, dest_dir)
            self.assertEqual(summary.get("drawings_added", 0), 0)
            new_book = dst_db.get_book_by_filename(os.path.basename(self.tmp_pdf))
            self.assertEqual(dst_db.get_drawings_for_page(new_book["id"], 1), [])
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)


class TestDrawingSummaryDefaultsGracefully(unittest.TestCase):
    """A drawing entry missing opacity/stroke_width (e.g. from an older
    archive written before this fix existed) shouldn't crash the
    import -- it should fall back to the same defaults new_drawing()
    itself uses."""

    def test_missing_opacity_and_width_use_defaults(self):
        src_db_path = tempfile.mktemp(suffix=".db")
        dst_db_path = tempfile.mktemp(suffix=".db")
        tmp_pdf = _make_real_pdf(pages=1)
        zip_path = tempfile.mktemp(suffix=".zip")
        dest_dir = tempfile.mkdtemp()
        try:
            src_db = Database(src_db_path)
            book = src_db.add_book(tmp_pdf, "Old Archive Format", 1)
            manifest, filepaths = build_manifest(src_db, include_reading_state=True)
            # simulate an older manifest shape missing the newer fields
            filename = os.path.basename(tmp_pdf)
            manifest["books"][filename]["drawings"] = [
                {"page_number": 0, "tool": "line", "color": "#123456", "points": [[1, 1], [2, 2]]}
            ]
            write_archive(zip_path, manifest, filepaths)

            dst_db = Database(dst_db_path)
            summary = apply_archive(dst_db, zip_path, dest_dir)
            self.assertEqual(summary["drawings_added"], 1)
            new_book = dst_db.get_book_by_filename(filename)
            d = dst_db.get_drawings_for_page(new_book["id"], 0)[0]
            self.assertEqual(d["tool"], "line")
            self.assertAlmostEqual(d["opacity"], 0.4)  # database default
            self.assertAlmostEqual(d["stroke_width"], 3.0)  # database default
        finally:
            for path in (src_db_path, dst_db_path, tmp_pdf, zip_path):
                if os.path.exists(path):
                    os.remove(path)


if __name__ == "__main__":
    unittest.main()
