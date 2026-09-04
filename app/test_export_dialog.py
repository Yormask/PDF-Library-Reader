"""Tests for the redesigned export flow: a single "Export..." entry
point (replacing the previous four separate Full/Selected x Share/Full
menu items) that opens ExportOptionsDialog, where content is customized
via checkboxes (Bookmarks, Highlights & Drawings, Reading Status, Reading
Progress -- categories and the annotation field always travel along) and
scope (every book, or just a selection) is inferred automatically from
whether any books were selected before Export was clicked, rather than
being a separate choice in the dialog.

Uses a real Database, a real LibraryWindow, and real (temporary) PDF
files throughout -- no mocks except for the genuinely-blocking native
dialogs (QFileDialog's save picker, QMessageBox) that would otherwise
wait for a click that never comes in an unattended test run, matching
this project's existing testing convention.
"""
import os
import sys
import tempfile
import unittest

import pymupdf as fitz
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

app = QApplication.instance() or QApplication(sys.argv)
QMessageBox.information = staticmethod(lambda *a, **k: None)

from app.database import Database
from app.export_dialog import ExportOptionsDialog
from app.full_archive import apply_archive, build_manifest, read_manifest, write_archive
from app.library_window import LibraryWindow


def _make_book(db, title="Export Test Book", pages=1, width=400, height=600):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), f"page {i}")
    doc.save(tmp_pdf)
    book = db.add_book(tmp_pdf, title, pages)
    return book, tmp_pdf


class TestExportOptionsDialog(unittest.TestCase):
    def test_scope_text_for_full_library(self):
        dlg = ExportOptionsDialog(5, is_selection=False)
        self.assertIn("all 5", dlg.layout().itemAt(0).widget().text())

    def test_scope_text_for_selection(self):
        dlg = ExportOptionsDialog(3, is_selection=True)
        self.assertIn("3 selected", dlg.layout().itemAt(0).widget().text())

    def test_singular_book_wording(self):
        dlg_all = ExportOptionsDialog(1, is_selection=False)
        self.assertIn("all 1 book in your library.", dlg_all.layout().itemAt(0).widget().text())
        dlg_sel = ExportOptionsDialog(1, is_selection=True)
        self.assertIn("1 selected book.", dlg_sel.layout().itemAt(0).widget().text())

    def test_all_checkboxes_default_checked(self):
        dlg = ExportOptionsDialog(1, is_selection=False)
        self.assertTrue(dlg.pdf_check.isChecked())
        self.assertTrue(dlg.bookmarks_check.isChecked())
        self.assertTrue(dlg.highlights_check.isChecked())
        self.assertTrue(dlg.status_check.isChecked())
        self.assertTrue(dlg.progress_check.isChecked())

    def test_options_reflects_checkbox_state(self):
        dlg = ExportOptionsDialog(1, is_selection=False)
        dlg.highlights_check.setChecked(False)
        dlg.progress_check.setChecked(False)
        self.assertEqual(dlg.options(), (True, True, False, True, False))

    def test_unchecking_everything_yields_all_false(self):
        dlg = ExportOptionsDialog(1, is_selection=False)
        dlg.pdf_check.setChecked(False)
        dlg.bookmarks_check.setChecked(False)
        dlg.highlights_check.setChecked(False)
        dlg.status_check.setChecked(False)
        dlg.progress_check.setChecked(False)
        self.assertEqual(dlg.options(), (False, False, False, False, False))

    def test_pdf_files_can_be_unchecked_independently(self):
        """The actual point of this checkbox: a lightweight, PDF-less
        export while everything else stays included -- the same idea as
        the old separate Categories/Bookmarks Only actions."""
        dlg = ExportOptionsDialog(1, is_selection=False)
        dlg.pdf_check.setChecked(False)
        self.assertEqual(dlg.options(), (False, True, True, True, True))


class TestBuildManifestGranularFlags(unittest.TestCase):
    """The four flags are independent -- each gates its own piece of
    data, and none of them affect categories or annotation, which
    always travel along."""

    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book, self.tmp_pdf = _make_book(self.db)
        self.db.add_bookmark(self.book["id"], 0, "a bookmark")
        self.db.add_highlight(self.book["id"], 0, "#3878FF", [[10, 10, 100, 30]], text="hi")
        self.db.add_drawing(self.book["id"], 0, "pen", "#FF0000", 0.5, 2.0, [[0, 0], [5, 5]])
        self.db.set_status(self.book["id"], "reading")
        self.db.toggle_favorite(self.book["id"])
        self.db.update_progress(self.book["id"], 1)

    def tearDown(self):
        for path in (self.tmp_db, self.tmp_pdf):
            if os.path.exists(path):
                os.remove(path)

    def _entry(self, **flags):
        manifest, _ = build_manifest(self.db, **flags)
        return manifest["books"][os.path.basename(self.tmp_pdf)]

    def test_categories_and_annotation_always_included(self):
        entry = self._entry(
            include_bookmarks=False, include_highlights=False,
            include_reading_status=False, include_reading_progress=False,
        )
        self.assertIn("categories", entry)
        self.assertIn("annotation", entry)

    def test_include_bookmarks_gates_only_bookmarks(self):
        with_it = self._entry(include_bookmarks=True, include_highlights=False,
                               include_reading_status=False, include_reading_progress=False)
        without_it = self._entry(include_bookmarks=False, include_highlights=False,
                                  include_reading_status=False, include_reading_progress=False)
        self.assertIn("bookmarks", with_it)
        self.assertNotIn("bookmarks", without_it)

    def test_include_highlights_gates_both_highlights_and_drawings(self):
        with_it = self._entry(include_bookmarks=False, include_highlights=True,
                               include_reading_status=False, include_reading_progress=False)
        without_it = self._entry(include_bookmarks=False, include_highlights=False,
                                  include_reading_status=False, include_reading_progress=False)
        self.assertIn("highlights", with_it)
        self.assertIn("drawings", with_it)
        self.assertNotIn("highlights", without_it)
        self.assertNotIn("drawings", without_it)

    def test_include_reading_status_gates_status_and_favorite(self):
        with_it = self._entry(include_bookmarks=False, include_highlights=False,
                               include_reading_status=True, include_reading_progress=False)
        without_it = self._entry(include_bookmarks=False, include_highlights=False,
                                  include_reading_status=False, include_reading_progress=False)
        self.assertIn("status", with_it)
        self.assertIn("is_favorite", with_it)
        self.assertNotIn("status", without_it)
        self.assertNotIn("is_favorite", without_it)

    def test_include_reading_progress_gates_only_last_page(self):
        with_it = self._entry(include_bookmarks=False, include_highlights=False,
                               include_reading_status=False, include_reading_progress=True)
        without_it = self._entry(include_bookmarks=False, include_highlights=False,
                                  include_reading_status=False, include_reading_progress=False)
        self.assertIn("last_page", with_it)
        self.assertNotIn("last_page", without_it)

    def test_all_flags_default_to_true(self):
        entry = self._entry()
        for key in ("bookmarks", "highlights", "drawings", "status", "is_favorite", "last_page"):
            self.assertIn(key, entry)

    def test_flags_are_independent_of_each_other(self):
        """Turning one off must not affect any of the others."""
        entry = self._entry(include_bookmarks=False)
        self.assertNotIn("bookmarks", entry)
        self.assertIn("highlights", entry)
        self.assertIn("status", entry)
        self.assertIn("last_page", entry)


class TestGranularExportImportRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book, self.tmp_pdf = _make_book(self.db)
        self.db.add_bookmark(self.book["id"], 0, "a bookmark")
        self.db.add_highlight(self.book["id"], 0, "#3878FF", [[10, 10, 100, 30]], text="hi")
        self.db.set_status(self.book["id"], "finished")
        self.db.update_progress(self.book["id"], 1)
        self.zip_path = tempfile.mktemp(suffix=".zip")

    def tearDown(self):
        for path in (self.tmp_db, self.tmp_pdf, self.zip_path):
            if os.path.exists(path):
                os.remove(path)

    def test_pdf_only_export_imports_with_no_extra_data(self):
        manifest, filepaths = build_manifest(
            self.db, include_bookmarks=False, include_highlights=False,
            include_reading_status=False, include_reading_progress=False,
        )
        write_archive(self.zip_path, manifest, filepaths)

        dst_db_path = tempfile.mktemp(suffix=".db")
        dst_db = Database(dst_db_path)
        dest_dir = tempfile.mkdtemp()
        try:
            summary = apply_archive(dst_db, self.zip_path, dest_dir)
            self.assertEqual(summary["bookmarks_added"], 0)
            self.assertEqual(summary["highlights_added"], 0)
            new_book = dst_db.get_book_by_filename(os.path.basename(self.tmp_pdf))
            self.assertIsNotNone(new_book)  # the PDF itself still imported
            self.assertEqual(dst_db.get_book(new_book["id"])["status"], "unread")
            self.assertEqual(dst_db.get_book(new_book["id"])["last_page"], 0)
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_bookmarks_only_export_imports_just_bookmarks(self):
        manifest, filepaths = build_manifest(
            self.db, include_bookmarks=True, include_highlights=False,
            include_reading_status=False, include_reading_progress=False,
        )
        write_archive(self.zip_path, manifest, filepaths)

        dst_db_path = tempfile.mktemp(suffix=".db")
        dst_db = Database(dst_db_path)
        dest_dir = tempfile.mkdtemp()
        try:
            summary = apply_archive(dst_db, self.zip_path, dest_dir)
            self.assertEqual(summary["bookmarks_added"], 1)
            self.assertEqual(summary["highlights_added"], 0)
            new_book = dst_db.get_book_by_filename(os.path.basename(self.tmp_pdf))
            self.assertEqual(dst_db.get_book(new_book["id"])["status"], "unread")
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)


class LibraryWindowTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.lib = LibraryWindow(self.db)
        self._tmp_pdfs = []
        self._orig_dialog_exec = ExportOptionsDialog.exec
        self._orig_get_save_filename = QFileDialog.getSaveFileName

    def tearDown(self):
        self.lib.close()
        ExportOptionsDialog.exec = self._orig_dialog_exec
        QFileDialog.getSaveFileName = self._orig_get_save_filename
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)
        for path in self._tmp_pdfs:
            if os.path.exists(path):
                os.remove(path)

    def _add_book(self, title="Book"):
        book, tmp_pdf = _make_book(self.db, title=title)
        self._tmp_pdfs.append(tmp_pdf)
        return book

    def _stub_dialog_reject_and_capture_scope(self, captured):
        original_init = ExportOptionsDialog.__init__

        def spy_init(dlg_self, book_count, is_selection, parent=None):
            captured.append((book_count, is_selection))
            original_init(dlg_self, book_count, is_selection, parent)

        ExportOptionsDialog.__init__ = spy_init
        ExportOptionsDialog.exec = lambda dlg_self: QDialog.Rejected
        return original_init


class TestExportScopeDetection(LibraryWindowTestCase):
    def test_no_selection_scopes_to_the_whole_library(self):
        self._add_book("A")
        self._add_book("B")
        captured = []
        original_init = self._stub_dialog_reject_and_capture_scope(captured)
        try:
            self.lib.export_books()
        finally:
            ExportOptionsDialog.__init__ = original_init
        self.assertEqual(captured[-1], (2, False))

    def test_active_selection_scopes_to_just_that(self):
        book_a = self._add_book("A")
        self._add_book("B")
        self.lib._selected_book_ids = {book_a["id"]}
        captured = []
        original_init = self._stub_dialog_reject_and_capture_scope(captured)
        try:
            self.lib.export_books()
        finally:
            ExportOptionsDialog.__init__ = original_init
        self.assertEqual(captured[-1], (1, True))

    def test_explicit_book_ids_overrides_current_selection(self):
        """The right-click "Export N Selected..." path passes book_ids
        explicitly -- it must win even if the window's own broader
        selection state says something else."""
        book_a = self._add_book("A")
        book_b = self._add_book("B")
        self.lib._selected_book_ids = {book_a["id"], book_b["id"]}
        captured = []
        original_init = self._stub_dialog_reject_and_capture_scope(captured)
        try:
            self.lib.export_books([book_a["id"]])
        finally:
            ExportOptionsDialog.__init__ = original_init
        self.assertEqual(captured[-1], (1, True))

    def test_empty_library_and_no_selection_shows_nothing_to_export(self):
        # no books added at all
        called = []
        ExportOptionsDialog.exec = lambda self: called.append(1) or QDialog.Rejected
        self.lib.export_books()
        self.assertEqual(called, [])  # dialog never even opened


class TestExportBooksEndToEnd(LibraryWindowTestCase):
    def test_cancelling_the_dialog_writes_nothing(self):
        self._add_book()
        ExportOptionsDialog.exec = lambda self: QDialog.Rejected
        save_calls = []
        QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (save_calls.append(1), ("", ""))[1]
        )
        self.lib.export_books()
        self.assertEqual(save_calls, [])  # never even got to the save dialog

    def test_accepting_with_custom_options_writes_the_right_manifest(self):
        book = self._add_book()
        self.db.add_bookmark(book["id"], 0, "note")
        self.db.add_highlight(book["id"], 0, "#3878FF", [[10, 10, 100, 30]], text="hi")

        def fake_exec(dlg_self):
            dlg_self.pdf_check.setChecked(True)
            dlg_self.bookmarks_check.setChecked(True)
            dlg_self.highlights_check.setChecked(False)
            dlg_self.status_check.setChecked(False)
            dlg_self.progress_check.setChecked(False)
            return QDialog.Accepted

        ExportOptionsDialog.exec = fake_exec
        zip_dest = tempfile.mktemp(suffix=".zip")
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (zip_dest, ""))
        try:
            self.lib.export_books()
            self.assertTrue(os.path.exists(zip_dest))
            manifest = read_manifest(zip_dest)
            filename = os.path.basename(self._tmp_pdfs[0])
            entry = manifest["books"][filename]
            self.assertIn("bookmarks", entry)
            self.assertNotIn("highlights", entry)
            self.assertNotIn("status", entry)
            import zipfile
            with zipfile.ZipFile(zip_dest) as zf:
                self.assertIn(f"books/{filename}", zf.namelist())  # PDF Files was checked
        finally:
            if os.path.exists(zip_dest):
                os.remove(zip_dest)

    def test_context_menu_path_clears_selection_on_success(self):
        book = self._add_book()
        self.lib._selected_book_ids = {book["id"]}
        ExportOptionsDialog.exec = lambda self: QDialog.Accepted
        zip_dest = tempfile.mktemp(suffix=".zip")
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (zip_dest, ""))
        try:
            self.lib.export_books([book["id"]], clear_selection_after=True)
            self.assertEqual(self.lib._selected_book_ids, set())
        finally:
            if os.path.exists(zip_dest):
                os.remove(zip_dest)

    def test_cancelling_save_dialog_does_not_clear_selection(self):
        book = self._add_book()
        self.lib._selected_book_ids = {book["id"]}
        ExportOptionsDialog.exec = lambda self: QDialog.Accepted
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: ("", ""))
        self.lib.export_books([book["id"]], clear_selection_after=True)
        self.assertEqual(self.lib._selected_book_ids, {book["id"]})

    def test_unchecking_pdf_files_writes_a_zip_with_no_books_folder(self):
        """The merged replacement for the old separate Categories Only /
        Bookmarks Only actions: unchecking PDF Files produces a zip with
        just manifest.json, no PDF bytes at all."""
        book = self._add_book()
        self.db.add_bookmark(book["id"], 0, "note")

        def fake_exec(dlg_self):
            dlg_self.pdf_check.setChecked(False)
            return QDialog.Accepted

        ExportOptionsDialog.exec = fake_exec
        zip_dest = tempfile.mktemp(suffix=".zip")
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (zip_dest, ""))
        try:
            self.lib.export_books()
            import zipfile
            with zipfile.ZipFile(zip_dest) as zf:
                names = zf.namelist()
            self.assertIn("manifest.json", names)
            self.assertFalse(any(n.startswith("books/") for n in names))
        finally:
            if os.path.exists(zip_dest):
                os.remove(zip_dest)

    def test_pdf_less_archive_still_applies_metadata_to_a_book_already_present(self):
        """The whole reason this works as a replacement for Categories/
        Bookmarks Only: apply_archive already applies metadata directly
        to a book it finds by filename, without needing to extract a PDF
        for it -- so a metadata-only zip imports correctly into a library
        that already has the matching PDF."""
        book = self._add_book()
        self.db.add_bookmark(book["id"], 0, "a note")

        def fake_exec(dlg_self):
            dlg_self.pdf_check.setChecked(False)
            return QDialog.Accepted

        ExportOptionsDialog.exec = fake_exec
        zip_dest = tempfile.mktemp(suffix=".zip")
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (zip_dest, ""))
        dst_db_path = tempfile.mktemp(suffix=".db")
        try:
            self.lib.export_books()
            dst_db = Database(dst_db_path)
            dst_db.add_book(self._tmp_pdfs[0], "Already Here", 1)  # same filename already present
            dest_dir = tempfile.mkdtemp()
            summary = apply_archive(dst_db, zip_dest, dest_dir)
            self.assertEqual(summary["added"], 0)  # no new PDF extraction needed
            self.assertEqual(summary["bookmarks_added"], 1)
        finally:
            if os.path.exists(zip_dest):
                os.remove(zip_dest)
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)


class TestRealMenuActionTriggers(LibraryWindowTestCase):
    """Regression coverage for the exact bug this whole feature shipped
    with: QAction.triggered emits a bool (the checked state), and
    connecting it directly to a slot with an optional leading parameter
    -- triggered.connect(self.export_books), no lambda -- silently passes
    that bool as book_ids instead of using the default. Calling
    export_books() directly in a test (as every other test in this file
    does deliberately, to isolate what's being tested) can never catch
    this class of bug, because it bypasses exactly the mechanism that's
    broken: Qt's real argument dispatch from a signal to a connected
    slot. Only actually firing the QAction, as done here, exercises it."""

    def test_export_action_opens_the_dialog_when_really_triggered(self):
        self._add_book()
        called = []
        ExportOptionsDialog.exec = lambda self: called.append(1) or QDialog.Rejected
        self.lib.export_action.trigger()
        self.assertEqual(len(called), 1)

    def test_export_action_does_not_crash_with_an_empty_library(self):
        # no books added -- must show "nothing to export" and return
        # cleanly, not raise, when fired as a real QAction
        called = []
        ExportOptionsDialog.exec = lambda self: called.append(1) or QDialog.Rejected
        self.lib.export_action.trigger()
        self.assertEqual(called, [])  # dialog never even opened -- nothing to export

    def test_export_action_is_on_the_file_menu(self):
        self.assertIn(self.lib.export_action, self.lib._file_menu.actions())

    def test_no_leftover_categories_only_or_bookmarks_only_menu_items(self):
        labels = {a.text() for a in self.lib._file_menu.actions()}
        self.assertNotIn("Categories Only...", labels)
        self.assertNotIn("Bookmarks Only...", labels)


if __name__ == "__main__":
    unittest.main()
