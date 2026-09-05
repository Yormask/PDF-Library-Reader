"""Tests for import progress reporting: importing a Full Archive now
shows a QProgressDialog the same way exporting already does, since
matching filenames, extracting PDFs, and applying bookmarks/highlights/
etc. to potentially many books can take a while for a large archive.

Cancelling partway through works differently from export's cancellation,
on purpose: a cancelled export leaves nothing behind (the half-written
zip gets deleted), but a cancelled import may already have changed the
library -- books, categories, and bookmarks already added for whichever
entries were processed before Cancel was clicked don't get undone. So
apply_archive's ImportCancelled handling returns a summary reflecting
whatever actually completed, with "cancelled": True added, rather than
raising all the way out and losing that information the way write_archive
does for a cancelled export.

Uses a real Database, a real LibraryWindow, and real (temporary) PDF
files and zip archives throughout -- no mocks except for QFileDialog's
folder picker and QMessageBox, which would otherwise block waiting for a
click that never comes in an unattended test run.
"""
import os
import sys
import tempfile
import unittest

import pymupdf as fitz
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QProgressDialog

app = QApplication.instance() or QApplication(sys.argv)
QMessageBox.information = staticmethod(lambda *a, **k: None)

from app.database import Database
from app.full_archive import ImportCancelled, apply_archive, build_manifest, write_archive
from app.library_window import LibraryWindow


def _make_archive(num_books=3, pages=1):
    """A real zip archive built from a real source library of num_books
    temporary PDFs. Returns (zip_path, list_of_source_tmp_pdfs,
    src_db_path) -- caller is responsible for cleaning all three up."""
    src_db_path = tempfile.mktemp(suffix=".db")
    src_db = Database(src_db_path)
    tmp_pdfs = []
    for i in range(num_books):
        tmp_pdf = tempfile.mktemp(suffix=".pdf")
        doc = fitz.open()
        for p in range(pages):
            page = doc.new_page(width=400, height=600)
            page.insert_text((72, 72), f"book {i} page {p}")
        doc.save(tmp_pdf)
        tmp_pdfs.append(tmp_pdf)
        src_db.add_book(tmp_pdf, f"Book {i}", pages)
    manifest, filepaths = build_manifest(src_db)
    zip_path = tempfile.mktemp(suffix=".zip")
    write_archive(zip_path, manifest, filepaths)
    return zip_path, tmp_pdfs, src_db_path


class TestApplyArchiveProgressCallback(unittest.TestCase):
    """The underlying function, independent of any UI."""

    def setUp(self):
        self.zip_path, self.tmp_pdfs, self.src_db_path = _make_archive(num_books=3)
        self.dst_db_path = tempfile.mktemp(suffix=".db")
        self.dst_db = Database(self.dst_db_path)
        self.dest_dir = tempfile.mkdtemp()

    def tearDown(self):
        for path in [self.zip_path, self.src_db_path, self.dst_db_path] + self.tmp_pdfs:
            if os.path.exists(path):
                os.remove(path)

    def test_callback_is_called_once_per_book(self):
        calls = []
        apply_archive(self.dst_db, self.zip_path, self.dest_dir,
                       progress_callback=lambda i, t, f: calls.append((i, t, f)))
        self.assertEqual(len(calls), 3)

    def test_callback_receives_correct_index_and_total(self):
        calls = []
        apply_archive(self.dst_db, self.zip_path, self.dest_dir,
                       progress_callback=lambda i, t, f: calls.append((i, t)))
        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])

    def test_callback_receives_the_actual_filename(self):
        calls = []
        apply_archive(self.dst_db, self.zip_path, self.dest_dir,
                       progress_callback=lambda i, t, f: calls.append(f))
        expected = {os.path.basename(p) for p in self.tmp_pdfs}
        self.assertEqual(set(calls), expected)

    def test_no_callback_still_works_normally(self):
        summary = apply_archive(self.dst_db, self.zip_path, self.dest_dir)
        self.assertEqual(summary["added"], 3)
        self.assertFalse(summary["cancelled"])

    def test_normal_completion_reports_cancelled_false(self):
        summary = apply_archive(self.dst_db, self.zip_path, self.dest_dir,
                                 progress_callback=lambda i, t, f: None)
        self.assertFalse(summary["cancelled"])

    def test_raising_import_cancelled_stops_processing_further_books(self):
        def cancel_after_first(index, total, filename):
            if index > 1:
                raise ImportCancelled()
        apply_archive(self.dst_db, self.zip_path, self.dest_dir, progress_callback=cancel_after_first)
        self.assertEqual(len(self.dst_db.get_books()), 1)

    def test_cancelled_summary_reflects_partial_progress_not_zero(self):
        """The key difference from export's cancellation: this must NOT
        look like nothing happened, because something did."""
        def cancel_after_two(index, total, filename):
            if index > 2:
                raise ImportCancelled()
        summary = apply_archive(self.dst_db, self.zip_path, self.dest_dir,
                                 progress_callback=cancel_after_two)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["added"], 2)
        self.assertEqual(summary["matched"], 2)

    def test_cancelling_immediately_reports_zero_progress_and_cancelled_true(self):
        def cancel_immediately(index, total, filename):
            raise ImportCancelled()
        summary = apply_archive(self.dst_db, self.zip_path, self.dest_dir,
                                 progress_callback=cancel_immediately)
        self.assertTrue(summary["cancelled"])
        self.assertEqual(summary["added"], 0)
        self.assertEqual(len(self.dst_db.get_books()), 0)

    def test_a_different_exception_from_the_callback_is_not_swallowed(self):
        """Only ImportCancelled means "stop and report partial progress" --
        anything else is a real bug and must propagate normally."""
        def raise_something_else(index, total, filename):
            raise ValueError("not a cancellation")
        with self.assertRaises(ValueError):
            apply_archive(self.dst_db, self.zip_path, self.dest_dir,
                           progress_callback=raise_something_else)


class LibraryWindowImportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.lib = LibraryWindow(self.db)
        self.dest_dir = tempfile.mkdtemp()
        self._orig_get_existing_dir = QFileDialog.getExistingDirectory
        self._orig_progress_init = QProgressDialog.__init__
        self._orig_was_canceled = QProgressDialog.wasCanceled
        QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: self.dest_dir)

    def tearDown(self):
        self.lib.close()
        QFileDialog.getExistingDirectory = self._orig_get_existing_dir
        QProgressDialog.__init__ = self._orig_progress_init
        QProgressDialog.wasCanceled = self._orig_was_canceled
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)


class TestImportShowsProgressDialog(LibraryWindowImportTestCase):
    def test_a_progress_dialog_is_created_with_the_right_range(self):
        zip_path, tmp_pdfs, src_db_path = _make_archive(num_books=4)
        created = []
        original_init = QProgressDialog.__init__

        def spy_init(dlg_self, *args, **kwargs):
            created.append(args)
            original_init(dlg_self, *args, **kwargs)

        QProgressDialog.__init__ = spy_init
        try:
            self.lib._import_full_archive_file(zip_path)
            self.assertEqual(len(created), 1)
            label, cancel_text, minimum, maximum, _parent = created[0]
            self.assertEqual(cancel_text, "Cancel")
            self.assertEqual(minimum, 0)
            self.assertEqual(maximum, 4)
        finally:
            for path in [zip_path, src_db_path] + tmp_pdfs:
                if os.path.exists(path):
                    os.remove(path)

    def test_progress_dialog_range_matches_a_smaller_archive(self):
        zip_path, tmp_pdfs, src_db_path = _make_archive(num_books=1)
        created = []
        original_init = QProgressDialog.__init__

        def spy_init(dlg_self, *args, **kwargs):
            created.append(args)
            original_init(dlg_self, *args, **kwargs)

        QProgressDialog.__init__ = spy_init
        try:
            self.lib._import_full_archive_file(zip_path)
            self.assertEqual(created[0][3], 1)
        finally:
            for path in [zip_path, src_db_path] + tmp_pdfs:
                if os.path.exists(path):
                    os.remove(path)

    def test_import_actually_completes_and_adds_books(self):
        zip_path, tmp_pdfs, src_db_path = _make_archive(num_books=3)
        try:
            self.lib._import_full_archive_file(zip_path)
            self.assertEqual(len(self.db.get_books()), 3)
        finally:
            for path in [zip_path, src_db_path] + tmp_pdfs:
                if os.path.exists(path):
                    os.remove(path)


class TestImportCancellation(LibraryWindowImportTestCase):
    def _make_cancel_after(self, n):
        """Patches QProgressDialog.wasCanceled to start returning True
        after the nth check -- simulating the user clicking Cancel
        partway through a real progress dialog."""
        call_count = [0]

        def fake_was_canceled(dlg_self):
            call_count[0] += 1
            return call_count[0] > n

        QProgressDialog.wasCanceled = fake_was_canceled

    def test_cancelling_partway_leaves_the_books_processed_so_far(self):
        zip_path, tmp_pdfs, src_db_path = _make_archive(num_books=4)
        self._make_cancel_after(2)
        try:
            self.lib._import_full_archive_file(zip_path)
            self.assertEqual(len(self.db.get_books()), 2)
        finally:
            for path in [zip_path, src_db_path] + tmp_pdfs:
                if os.path.exists(path):
                    os.remove(path)

    def test_cancellation_message_says_cancelled_not_complete(self):
        zip_path, tmp_pdfs, src_db_path = _make_archive(num_books=4)
        self._make_cancel_after(1)
        messages = []
        QMessageBox.information = staticmethod(
            lambda parent, title, msg: messages.append((title, msg))
        )
        try:
            self.lib._import_full_archive_file(zip_path)
            self.assertEqual(len(messages), 1)
            title, msg = messages[0]
            self.assertEqual(title, "Import cancelled")
            self.assertIn("cancelled", msg.lower())
        finally:
            for path in [zip_path, src_db_path] + tmp_pdfs:
                if os.path.exists(path):
                    os.remove(path)

    def test_cancellation_message_reports_what_was_actually_added(self):
        """Must not imply nothing happened -- something did."""
        zip_path, tmp_pdfs, src_db_path = _make_archive(num_books=4)
        self._make_cancel_after(3)
        messages = []
        QMessageBox.information = staticmethod(
            lambda parent, title, msg: messages.append((title, msg))
        )
        try:
            self.lib._import_full_archive_file(zip_path)
            self.assertIn("Added 3 new book(s)", messages[0][1])
        finally:
            for path in [zip_path, src_db_path] + tmp_pdfs:
                if os.path.exists(path):
                    os.remove(path)

    def test_completing_without_cancelling_shows_the_normal_complete_message(self):
        zip_path, tmp_pdfs, src_db_path = _make_archive(num_books=2)
        messages = []
        QMessageBox.information = staticmethod(
            lambda parent, title, msg: messages.append((title, msg))
        )
        try:
            self.lib._import_full_archive_file(zip_path)
            self.assertEqual(messages[0][0], "Import complete")
            self.assertNotIn("cancelled", messages[0][1].lower())
        finally:
            for path in [zip_path, src_db_path] + tmp_pdfs:
                if os.path.exists(path):
                    os.remove(path)


if __name__ == "__main__":
    unittest.main()
