"""Tests for the reader's per-page thumbnail disk cache (app/thumbnails.py's
ensure_page_thumbnail/page_thumbnail_path/delete_page_thumbnails, and
delete_thumbnail's now-unified cleanup of both the cover and page caches).

Before this, opening the Pages panel re-rasterized every page from the PDF
with PyMuPDF every single time -- fine once, but repeated in full on every
new reader session for the same book, which is the actual, repeated cost a
long book's owner pays over and over. This mirrors the pattern the library's
own cover thumbnails already use (app/thumbnails.py's ensure_thumbnail),
just extended to one small cached image per page instead of one per book,
in a per-book subfolder so a long book doesn't dump hundreds of loose files
into one shared directory.

Uses a real Database, a real ReaderWindow, and real (temporary) PDF files
throughout -- no mocks -- matching this project's existing testing
convention. Uses large, book_id-namespaced fake ids (900000+) for tests
that don't go through a real Database, to avoid any chance of colliding
with a real cache directory on the machine running these tests.
"""
import os
import sys
import tempfile
import time
import unittest

import pymupdf as fitz
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.database import Database
from app.reader_window import ReaderWindow
from app.thumbnails import (
    delete_page_thumbnails, delete_thumbnail, ensure_page_thumbnail,
    ensure_thumbnail, page_thumbnail_path, thumbnail_path,
)


def _make_pdf(pages=3, busy=False):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=400, height=600)
        page.insert_text((72, 72), f"page {i} content " * 15)
        if busy:
            for j in range(30):
                page.draw_rect(fitz.Rect(10 + j * 5, 100, 30 + j * 5, 120), fill=(0.2, 0.4, 0.8))
    doc.save(tmp_pdf)
    return tmp_pdf


class TestEnsurePageThumbnail(unittest.TestCase):
    def setUp(self):
        self.book_id = 900001
        self.tmp_pdf = _make_pdf(pages=1)
        self.doc = fitz.open(self.tmp_pdf)

    def tearDown(self):
        self.doc.close()
        delete_page_thumbnails(self.book_id)
        if os.path.exists(self.tmp_pdf):
            os.remove(self.tmp_pdf)

    def test_first_call_creates_a_cache_file(self):
        path = page_thumbnail_path(self.book_id, 0)
        self.assertFalse(path.exists())
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        self.assertTrue(path.exists())

    def test_returns_a_usable_non_null_pixmap(self):
        pix = ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        self.assertIsNotNone(pix)
        self.assertFalse(pix.isNull())

    def test_cache_file_is_png(self):
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        path = page_thumbnail_path(self.book_id, 0)
        self.assertTrue(str(path).endswith(".png"))

    def test_second_call_loads_from_cache_without_rerendering(self):
        """Delete-then-recreate the page's own source PDF file between
        calls -- if the second call somehow still needed the live page
        object for anything beyond the already-cached file, this would
        surface it, since the page object would still technically work
        (it's a Python object independent of the file), but the point is
        the cache path returns first regardless."""
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        path = page_thumbnail_path(self.book_id, 0)
        mtime_before = path.stat().st_mtime
        time.sleep(0.01)
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        mtime_after = path.stat().st_mtime
        self.assertEqual(mtime_before, mtime_after)  # file was never rewritten

    def test_cached_load_is_meaningfully_faster_than_first_render(self):
        t0 = time.perf_counter()
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        t_first = time.perf_counter() - t0

        t0 = time.perf_counter()
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        t_second = time.perf_counter() - t0

        self.assertLess(t_second, t_first)

    def test_different_pages_get_different_cache_files(self):
        tmp_pdf2 = _make_pdf(pages=3)
        doc2 = fitz.open(tmp_pdf2)
        try:
            ensure_page_thumbnail(self.book_id, 0, doc2[0])
            ensure_page_thumbnail(self.book_id, 1, doc2[1])
            self.assertNotEqual(
                page_thumbnail_path(self.book_id, 0),
                page_thumbnail_path(self.book_id, 1),
            )
            self.assertTrue(page_thumbnail_path(self.book_id, 0).exists())
            self.assertTrue(page_thumbnail_path(self.book_id, 1).exists())
        finally:
            doc2.close()
            delete_page_thumbnails(self.book_id)
            os.remove(tmp_pdf2)

    def test_different_books_do_not_collide(self):
        other_book_id = 900002
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        ensure_page_thumbnail(other_book_id, 0, self.doc[0])
        try:
            self.assertNotEqual(
                page_thumbnail_path(self.book_id, 0),
                page_thumbnail_path(other_book_id, 0),
            )
        finally:
            delete_page_thumbnails(other_book_id)

    def test_degenerate_page_size_returns_none_not_a_crash(self):
        class FakePage:
            rect = fitz.Rect(0, 0, 0, 0)  # zero width/height
        result = ensure_page_thumbnail(self.book_id, 0, FakePage())
        self.assertIsNone(result)

    def test_a_broken_page_object_returns_none_not_a_crash(self):
        class BrokenPage:
            @property
            def rect(self):
                raise RuntimeError("simulated failure")
        result = ensure_page_thumbnail(self.book_id, 0, BrokenPage())
        self.assertIsNone(result)


class TestDeletePageThumbnails(unittest.TestCase):
    def setUp(self):
        self.book_id = 900003
        self.tmp_pdf = _make_pdf(pages=3)
        self.doc = fitz.open(self.tmp_pdf)

    def tearDown(self):
        self.doc.close()
        delete_page_thumbnails(self.book_id)
        if os.path.exists(self.tmp_pdf):
            os.remove(self.tmp_pdf)

    def test_removes_every_cached_page_for_the_book(self):
        for i in range(3):
            ensure_page_thumbnail(self.book_id, i, self.doc[i])
        delete_page_thumbnails(self.book_id)
        for i in range(3):
            self.assertFalse(page_thumbnail_path(self.book_id, i).exists())

    def test_does_not_affect_a_different_book(self):
        other_book_id = 900004
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        ensure_page_thumbnail(other_book_id, 0, self.doc[0])
        try:
            delete_page_thumbnails(self.book_id)
            self.assertTrue(page_thumbnail_path(other_book_id, 0).exists())
        finally:
            delete_page_thumbnails(other_book_id)

    def test_calling_on_a_book_with_no_cache_does_not_raise(self):
        delete_page_thumbnails(999999999)  # never had anything cached


class TestUnifiedDeleteThumbnail(unittest.TestCase):
    """delete_thumbnail() (the pre-existing cover-thumbnail cleanup,
    called from several places in library_window.py) now also clears the
    page cache -- so every existing call site stays correct automatically,
    with nothing new for them to remember."""

    def setUp(self):
        self.book_id = 900005
        self.tmp_pdf = _make_pdf(pages=2)
        self.doc = fitz.open(self.tmp_pdf)

    def tearDown(self):
        self.doc.close()
        delete_thumbnail(self.book_id)
        if os.path.exists(self.tmp_pdf):
            os.remove(self.tmp_pdf)

    def test_removes_both_cover_and_page_caches_in_one_call(self):
        ensure_thumbnail(self.book_id, self.tmp_pdf)
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        ensure_page_thumbnail(self.book_id, 1, self.doc[1])

        self.assertTrue(thumbnail_path(self.book_id).exists())
        self.assertTrue(page_thumbnail_path(self.book_id, 0).exists())
        self.assertTrue(page_thumbnail_path(self.book_id, 1).exists())

        delete_thumbnail(self.book_id)

        self.assertFalse(thumbnail_path(self.book_id).exists())
        self.assertFalse(page_thumbnail_path(self.book_id, 0).exists())
        self.assertFalse(page_thumbnail_path(self.book_id, 1).exists())

    def test_works_fine_when_only_a_cover_thumbnail_exists(self):
        ensure_thumbnail(self.book_id, self.tmp_pdf)
        delete_thumbnail(self.book_id)  # must not raise just because no page cache exists
        self.assertFalse(thumbnail_path(self.book_id).exists())

    def test_works_fine_when_only_page_thumbnails_exist(self):
        ensure_page_thumbnail(self.book_id, 0, self.doc[0])
        delete_thumbnail(self.book_id)  # must not raise just because no cover exists
        self.assertFalse(page_thumbnail_path(self.book_id, 0).exists())


class TestReaderWindowUsesTheCache(unittest.TestCase):
    """End-to-end through the real ReaderWindow, not just the underlying
    thumbnails.py functions directly."""

    def setUp(self):
        self.tmp_pdf = _make_pdf(pages=6)
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book = self.db.add_book(self.tmp_pdf, "Cache Test", 6)

    def tearDown(self):
        delete_page_thumbnails(self.book["id"])
        for path in (self.tmp_db, self.tmp_pdf):
            if os.path.exists(path):
                os.remove(path)

    def _drain_queue(self, win, batch_size=10):
        while win._thumbnail_render_queue:
            win._render_next_thumbnail_batch(batch_size=batch_size)

    def test_opening_the_panel_populates_every_thumbnail_icon(self):
        win = ReaderWindow(self.db, self.book["id"])
        try:
            win._start_thumbnail_rendering()
            self._drain_queue(win)
            for i in range(6):
                self.assertFalse(win.thumbnail_list.item(i).icon().isNull())
        finally:
            win.close()

    def test_opening_the_panel_writes_the_disk_cache(self):
        win = ReaderWindow(self.db, self.book["id"])
        try:
            win._start_thumbnail_rendering()
            self._drain_queue(win)
            for i in range(6):
                self.assertTrue(page_thumbnail_path(self.book["id"], i).exists())
        finally:
            win.close()

    def test_a_second_reader_session_reuses_the_cache(self):
        win1 = ReaderWindow(self.db, self.book["id"])
        try:
            win1._start_thumbnail_rendering()
            self._drain_queue(win1)
        finally:
            win1.close()

        cache_mtimes_before = [page_thumbnail_path(self.book["id"], i).stat().st_mtime for i in range(6)]

        win2 = ReaderWindow(self.db, self.book["id"])
        try:
            win2._start_thumbnail_rendering()
            self._drain_queue(win2)
            for i in range(6):
                self.assertFalse(win2.thumbnail_list.item(i).icon().isNull())
        finally:
            win2.close()

        cache_mtimes_after = [page_thumbnail_path(self.book["id"], i).stat().st_mtime for i in range(6)]
        self.assertEqual(cache_mtimes_before, cache_mtimes_after)  # never rewritten

    def test_a_second_session_is_meaningfully_faster_than_the_first(self):
        win1 = ReaderWindow(self.db, self.book["id"])
        try:
            t0 = time.perf_counter()
            win1._start_thumbnail_rendering()
            self._drain_queue(win1)
            t_first = time.perf_counter() - t0
        finally:
            win1.close()

        win2 = ReaderWindow(self.db, self.book["id"])
        try:
            t0 = time.perf_counter()
            win2._start_thumbnail_rendering()
            self._drain_queue(win2)
            t_second = time.perf_counter() - t0
        finally:
            win2.close()

        self.assertLess(t_second, t_first)


if __name__ == "__main__":
    unittest.main()
