"""Thumbnail generation and disk caching for the library's image-preview view,
and for the reader's per-page navigation thumbnails (see PAGE_THUMB_SIZE and
ensure_page_thumbnail below).
"""
import shutil

import pymupdf as fitz
from PySide6.QtGui import QColor, QPixmap

from .database import get_data_dir

THUMB_SIZE = (140, 190)  # roughly paperback-cover proportions, in pixels


def _thumb_dir():
    d = get_data_dir() / "thumbnails"
    d.mkdir(parents=True, exist_ok=True)
    return d


def thumbnail_path(book_id):
    return _thumb_dir() / f"{book_id}.png"


def ensure_thumbnail(book_id, filepath):
    """Return (QPixmap, is_corrupted).

    Generated once and cached to disk; later calls just load the cached file.
    A password-protected file gets a plain placeholder (it isn't broken, just
    locked) with is_corrupted=False. A file that genuinely can't be opened or
    rendered at all gets is_corrupted=True, so the caller can show a warning
    badge on it.
    """
    path = thumbnail_path(book_id)
    if path.exists():
        pix = QPixmap(str(path))
        if not pix.isNull():
            return pix, False

    try:
        doc = fitz.open(filepath)
        if doc.needs_pass:
            doc.close()
            return _placeholder(), False
        page = doc[0]
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            raise ValueError("degenerate page size")
        scale = min(THUMB_SIZE[0] / rect.width, THUMB_SIZE[1] / rect.height)
        matrix = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=matrix)
        pix.save(str(path))
        doc.close()
        qpix = QPixmap(str(path))
        return (qpix, False) if not qpix.isNull() else (_placeholder(), False)
    except Exception:
        return _placeholder(), True


def _placeholder():
    pix = QPixmap(*THUMB_SIZE)
    pix.fill(QColor(225, 225, 225))
    return pix


def delete_thumbnail(book_id):
    """Removes the cached cover thumbnail AND every cached page thumbnail
    for this book -- one call covers all thumbnail-related disk state for
    a book, so every existing call site (there are several, scattered
    across book removal, duplicate handling, and re-import) automatically
    stays correct without each one needing to separately remember the
    per-page cache too."""
    path = thumbnail_path(book_id)
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
    delete_page_thumbnails(book_id)


# --- Reader page-navigation thumbnails ---------------------------------
# A separate cache from the cover thumbnails above: one small image per
# page rather than one per book, so it lives in its own per-book
# subfolder to keep a book with hundreds of pages from dumping hundreds
# of loose files into a single shared directory alongside everything
# else. PNG, not JPEG: for typical rendered PDF content (text and vector
# graphics, which is nearly all of it, even a "busy" page) PNG comes out
# both smaller and noticeably faster to decode than JPEG -- measured
# directly rather than assumed, since photographic-content intuitions
# about JPEG being the "compressed" choice don't hold for this kind of
# image. The real win either way is caching at all: loading a small
# cached image is roughly an order of magnitude faster than asking
# PyMuPDF to rasterize the full page again, and that gap only grows with
# how complex the source page actually is, while a cached load stays
# roughly constant regardless.
PAGE_THUMB_SIZE = (90, 120)


def _page_thumb_dir(book_id):
    d = get_data_dir() / "thumbnails" / "pages" / str(book_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def page_thumbnail_path(book_id, page_number):
    return _page_thumb_dir(book_id) / f"{page_number}.png"


def ensure_page_thumbnail(book_id, page_number, page, size=PAGE_THUMB_SIZE):
    """Returns a QPixmap for the given page (a real PyMuPDF Page object,
    already open -- the reader already has the document open, so this
    never opens the PDF itself). Generated once and cached to disk;
    reopening the same book's thumbnail panel in a later session loads
    straight from these small cached files instead of re-rasterizing
    every page from the PDF again. Returns None if the page can't be
    rendered at all (e.g. a degenerate/zero-size page) or the pixmap
    that came back is somehow unusable -- a caller should treat that the
    same as "no thumbnail available", not retry."""
    path = page_thumbnail_path(book_id, page_number)
    if path.exists():
        pix = QPixmap(str(path))
        if not pix.isNull():
            return pix

    try:
        rect = page.rect
        if rect.width <= 0 or rect.height <= 0:
            return None
        scale = min(size[0] / rect.width, size[1] / rect.height)
        matrix = fitz.Matrix(scale, scale)
        rendered = page.get_pixmap(matrix=matrix)
        rendered.save(str(path))
        qpix = QPixmap(str(path))
        return qpix if not qpix.isNull() else None
    except Exception:
        return None


def delete_page_thumbnails(book_id):
    """Removes every cached page thumbnail for a book. Called automatically
    by delete_thumbnail() above -- exposed separately too in case a caller
    ever needs to clear just the per-page cache (e.g. forcing a re-render
    after a book's file was replaced) without touching the cover."""
    d = get_data_dir() / "thumbnails" / "pages" / str(book_id)
    if d.exists():
        try:
            shutil.rmtree(d)
        except OSError:
            pass
