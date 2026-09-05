"""Tests for the category sidebar's book count actually updating when a
book's favorite status changes, without needing a manual refresh.

The bug: Database.toggle_favorite (see app/test_favorites_category.py)
already keeps a "Favorites" category correctly in sync at the data layer
-- but none of the three places in the UI that call it told the sidebar
to redraw itself afterward:
- LibraryWindow.toggle_favorite (the book card/cell's own star button,
  and the right-click "Toggle Favorite" action) refreshed the book list
  but never the sidebar, so the Favorites category's count next to its
  name stayed stale until something else happened to rebuild it.
- BookDetailsDialog's favorite button didn't notify the library window
  of anything at all -- it only updated its own local button state.
- Closing a reader window after favoriting a book from inside it
  refreshed the book list (via the existing on_close callback) but,
  again, never the sidebar.

All three now go through LibraryWindow._refresh_list_and_categories,
which refreshes both together.

Uses a real Database, a real LibraryWindow, a real BookDetailsDialog,
and a real ReaderWindow throughout -- no mocks -- matching this
project's existing testing convention.
"""
import os
import sys
import tempfile
import unittest

import pymupdf as fitz
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.book_details_dialog import BookDetailsDialog
from app.database import Database
from app.library_window import LibraryWindow
from app.reader_window import ReaderWindow


def _make_book(db, title="Sidebar Refresh Test"):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    doc = fitz.open()
    doc.new_page(width=400, height=600)
    doc.save(tmp_pdf)
    book = db.add_book(tmp_pdf, title, 1)
    return book, tmp_pdf


def _favorites_sidebar_text(lib):
    for i in range(lib.category_list.count()):
        item = lib.category_list.item(i)
        if "Favorites" in item.text():
            return item.text()
    return None


class SidebarRefreshTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.lib = LibraryWindow(self.db)
        self._tmp_pdfs = []

    def tearDown(self):
        self.lib.close()
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)
        for path in self._tmp_pdfs:
            if os.path.exists(path):
                os.remove(path)

    def _add_book(self, title="Sidebar Refresh Test"):
        book, tmp_pdf = _make_book(self.db, title)
        self._tmp_pdfs.append(tmp_pdf)
        return book


class TestLibraryToggleRefreshesSidebar(SidebarRefreshTestCase):
    def test_favoriting_updates_the_sidebar_without_a_manual_refresh(self):
        book = self._add_book()
        self.assertIsNone(_favorites_sidebar_text(self.lib))  # no category yet

        self.lib.toggle_favorite(book["id"])
        app.processEvents()  # the refresh is deferred via QTimer.singleShot(0, ...)

        text = _favorites_sidebar_text(self.lib)
        self.assertIsNotNone(text)
        self.assertIn("(1)", text)

    def test_unfavoriting_updates_the_count_back_down(self):
        book = self._add_book()
        self.lib.toggle_favorite(book["id"])
        app.processEvents()
        self.lib.toggle_favorite(book["id"])
        app.processEvents()

        text = _favorites_sidebar_text(self.lib)
        self.assertIn("(0)", text)

    def test_count_reflects_multiple_favorited_books(self):
        book1 = self._add_book("A")
        book2 = self._add_book("B")
        self.lib.toggle_favorite(book1["id"])
        app.processEvents()
        self.lib.toggle_favorite(book2["id"])
        app.processEvents()

        text = _favorites_sidebar_text(self.lib)
        self.assertIn("(2)", text)

    def test_refresh_list_and_categories_helper_updates_both(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])  # data layer only, bypassing the UI wrapper
        self.lib._refresh_list_and_categories()
        text = _favorites_sidebar_text(self.lib)
        self.assertIn("(1)", text)


class TestBookDetailsDialogRefreshesSidebar(SidebarRefreshTestCase):
    def setUp(self):
        super().setUp()
        self.dialog = BookDetailsDialog(self.db, self.lib)
        self.dialog.book_updated.connect(self.lib._refresh_list_and_categories)

    def tearDown(self):
        self.dialog.close()
        super().tearDown()

    def test_toggling_favorite_in_the_dialog_emits_book_updated(self):
        book = self._add_book()
        self.dialog.load_book(book["id"])
        received = []
        self.dialog.book_updated.connect(lambda: received.append(1))
        self.dialog._toggle_favorite()
        self.assertEqual(len(received), 1)

    def test_toggling_favorite_in_the_dialog_updates_the_sidebar(self):
        book = self._add_book()
        self.dialog.load_book(book["id"])
        self.dialog._toggle_favorite()
        text = _favorites_sidebar_text(self.lib)
        self.assertIsNotNone(text)
        self.assertIn("(1)", text)

    def test_toggling_off_again_updates_the_sidebar_back_down(self):
        book = self._add_book()
        self.dialog.load_book(book["id"])
        self.dialog._toggle_favorite()  # on
        self.dialog._toggle_favorite()  # off
        text = _favorites_sidebar_text(self.lib)
        self.assertIn("(0)", text)

    def test_the_dialogs_own_button_state_still_updates_too(self):
        """The fix must not regress the dialog's own visible state --
        just add the missing notification on top of it."""
        book = self._add_book()
        self.dialog.load_book(book["id"])
        self.dialog._toggle_favorite()
        self.assertTrue(self.dialog.favorite_btn.isChecked())


class TestReaderWindowCloseRefreshesSidebar(SidebarRefreshTestCase):
    def test_favoriting_in_the_reader_then_closing_updates_the_sidebar(self):
        book = self._add_book()
        win = ReaderWindow(self.db, book["id"], on_close=self.lib._refresh_list_and_categories)
        win.toggle_favorite()
        self.assertIsNone(_favorites_sidebar_text(self.lib))  # not yet -- reader still open
        win.close()
        text = _favorites_sidebar_text(self.lib)
        self.assertIsNotNone(text)
        self.assertIn("(1)", text)

    def test_unfavoriting_in_the_reader_then_closing_updates_the_count(self):
        book = self._add_book()
        self.lib.toggle_favorite(book["id"])
        app.processEvents()
        self.assertIn("(1)", _favorites_sidebar_text(self.lib))

        win = ReaderWindow(self.db, book["id"], on_close=self.lib._refresh_list_and_categories)
        win.toggle_favorite()  # un-favorite from inside the reader
        win.close()
        self.assertIn("(0)", _favorites_sidebar_text(self.lib))

    def test_closing_without_favoriting_anything_does_not_create_the_category(self):
        book = self._add_book()
        win = ReaderWindow(self.db, book["id"], on_close=self.lib._refresh_list_and_categories)
        win.close()
        self.assertIsNone(_favorites_sidebar_text(self.lib))


if __name__ == "__main__":
    unittest.main()
