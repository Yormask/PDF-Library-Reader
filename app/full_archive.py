"""Full backup archive: a ZIP containing the actual PDF files plus a
manifest of everything that isn't already encoded in their filenames --
categories, bookmarks, reading status, favorite, annotation, reading
progress, saved highlights, and drawn annotations. The natural way to
move (or back up) an entire library, not just its categorization.
"""
import json
import os
import zipfile
from datetime import datetime

import pymupdf as fitz

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
BOOKS_DIR = "books"


def build_manifest(db, book_ids=None, include_reading_state=True):
    """Returns (manifest_dict, {filename: source_filepath}).

    include_reading_state controls whether each book's personal reading
    data -- status, favorite flag, last-read page, saved highlights, and
    drawn annotations -- is included. That data makes sense for a backup
    of your own library (restoring it to yourself elsewhere should put
    you back where you left off), but imposing it on someone you're
    sharing books with doesn't: they'd end up with books mysteriously
    pre-marked "Finished" or already favorited, resuming mid-book at a
    page they never reached, or covered in someone else's highlights and
    drawings. Pass False for a share-oriented export -- the importing
    side already treats a missing status/is_favorite/last_page/
    highlights/drawings as "leave it alone", so simply omitting them here
    is enough; no changes needed on the import path.
    """
    if book_ids is None:
        book_ids = [b["id"] for b in db.get_books()]

    books_out = {}
    categories_seen = {}
    filepaths = {}
    for book_id in book_ids:
        book = db.get_book(book_id)
        if not book:
            continue
        filename = os.path.basename(book["filepath"])
        cats = db.get_categories_for_book(book_id)
        bookmarks = db.get_bookmarks(book_id)
        entry = {
            "categories": [c["name"] for c in cats],
            "bookmarks": [
                {"page_number": bm["page_number"], "label": bm["label"] or ""} for bm in bookmarks
            ],
            "annotation": book["annotation"] or "",
        }
        if include_reading_state:
            entry["status"] = book["status"] or "unread"
            entry["is_favorite"] = bool(book["is_favorite"])
            entry["last_page"] = book["last_page"] or 0
            entry["highlights"] = [
                {
                    "page_number": h["page_number"], "color": h["color"],
                    "label": h["label"] or "", "text": h["text"] or "", "rects": h["rects"],
                    "style": h.get("style") or "fill",
                }
                for h in db.get_highlights(book_id)
            ]
            entry["drawings"] = [
                {
                    "page_number": d["page_number"], "tool": d["tool"], "color": d["color"],
                    "opacity": d["opacity"], "stroke_width": d["stroke_width"], "points": d["points"],
                }
                for d in db.get_drawings(book_id)
            ]
        books_out[filename] = entry
        for c in cats:
            categories_seen[c["name"]] = bool(c["is_favorite"])
        filepaths[filename] = book["filepath"]

    manifest = {
        "kind": "full_archive",
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(),
        "books": books_out,
        "categories": {name: {"favorite": fav} for name, fav in categories_seen.items()},
    }
    return manifest, filepaths


def write_archive(zip_path, manifest, filepaths, progress_callback=None):
    """Writes manifest.json plus every PDF (under books/) into the zip.
    Returns a list of filenames that were skipped because their source file
    no longer existed on disk at export time.

    If given, progress_callback(index, total, filename) is called after each
    file is handled (index starting at 1) -- copying many/large PDFs into a
    zip can take a while, so a caller can use this to drive a progress
    dialog instead of leaving the UI looking frozen. Raising an exception
    from the callback (e.g. to signal the user cancelled) aborts the write;
    the zip file will exist but be incomplete, so callers doing that should
    delete it afterward."""
    skipped = []
    total = len(filepaths)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, ensure_ascii=False))
        for i, (filename, source_path) in enumerate(filepaths.items(), start=1):
            if not os.path.exists(source_path):
                skipped.append(filename)
            else:
                zf.write(source_path, arcname=f"{BOOKS_DIR}/{filename}")
            if progress_callback:
                progress_callback(i, total, filename)
    return skipped


def read_manifest(zip_path):
    with zipfile.ZipFile(zip_path, "r") as zf:
        return json.loads(zf.read(MANIFEST_NAME))


def _unique_dest_path(directory, filename):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({counter}){ext}")
        counter += 1
    return candidate


def apply_archive(db, zip_path, destination_dir):
    """Extracts any PDF not already in the library (matched by filename)
    into destination_dir and adds it, then applies categories, bookmarks,
    status, favorite, and annotation to every matched book -- whether newly
    added or already present. Returns a summary dict."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        manifest = json.loads(zf.read(MANIFEST_NAME))
        books = manifest.get("books", {})
        categories = manifest.get("categories", {})

        name_to_cat_id = {}
        categories_created = 0
        for name, meta in categories.items():
            existing = db.get_category_by_name(name)
            cat = db.create_category(name)
            if cat is None:
                continue
            if existing is None:
                categories_created += 1
            if meta.get("favorite") and not cat["is_favorite"]:
                db.toggle_category_favorite(cat["id"])
            name_to_cat_id[name] = cat["id"]

        matched, added, skipped, bookmarks_added = 0, 0, 0, 0
        highlights_added = 0
        drawings_added = 0
        for filename, meta in books.items():
            book = db.get_book_by_filename(filename)
            if book is None:
                archive_entry = f"{BOOKS_DIR}/{filename}"
                if archive_entry not in zf.namelist():
                    skipped += 1
                    continue
                target_path = _unique_dest_path(destination_dir, filename)
                with zf.open(archive_entry) as src, open(target_path, "wb") as dst:
                    dst.write(src.read())
                page_count = 0
                try:
                    doc = fitz.open(target_path)
                    page_count = doc.page_count
                    doc.close()
                except Exception:
                    pass
                title = os.path.splitext(filename)[0].split(" - ")[0].strip() or "Untitled"
                book = db.add_book(target_path, title, page_count)
                added += 1

            matched += 1

            for cat_name in meta.get("categories", []):
                cat_id = name_to_cat_id.get(cat_name)
                if cat_id is None:
                    cat = db.create_category(cat_name)
                    if cat is None:
                        continue
                    cat_id = cat["id"]
                    name_to_cat_id[cat_name] = cat_id
                db.add_books_to_category(cat_id, [book["id"]])

            existing_bookmarks = {
                (bm["page_number"], bm["label"] or "") for bm in db.get_bookmarks(book["id"])
            }
            for bm in meta.get("bookmarks", []):
                key = (bm["page_number"], bm.get("label") or "")
                if key in existing_bookmarks:
                    continue
                db.add_bookmark(book["id"], bm["page_number"], bm.get("label") or "")
                bookmarks_added += 1

            if meta.get("status"):
                db.set_status(book["id"], meta["status"])
            if meta.get("is_favorite") and not book["is_favorite"]:
                db.toggle_favorite(book["id"])
            if meta.get("annotation"):
                db.update_metadata(book["id"], annotation=meta["annotation"])
            if meta.get("last_page"):
                db.update_progress(book["id"], meta["last_page"])

            existing_highlights = {
                (h["page_number"], h["color"], json.dumps(h["rects"]))
                for h in db.get_highlights(book["id"])
            }
            for h in meta.get("highlights", []):
                key = (h["page_number"], h["color"], json.dumps(h["rects"]))
                if key in existing_highlights:
                    continue
                db.add_highlight(
                    book["id"], h["page_number"], h["color"], h["rects"],
                    text=h.get("text") or "", label=h.get("label") or "",
                    style=h.get("style") or "fill",
                )
                highlights_added += 1

            existing_drawings = {
                (d["page_number"], d["tool"], d["color"], json.dumps(d["points"]))
                for d in db.get_drawings(book["id"])
            }
            for d in meta.get("drawings", []):
                key = (d["page_number"], d["tool"], d["color"], json.dumps(d["points"]))
                if key in existing_drawings:
                    continue
                db.add_drawing(
                    book["id"], d["page_number"], d["tool"], d["color"],
                    d.get("opacity", 0.4), d.get("stroke_width", 3.0), d["points"],
                )
                drawings_added += 1

    return {
        "matched": matched, "added": added, "skipped": skipped,
        "categories_created": categories_created, "bookmarks_added": bookmarks_added,
        "highlights_added": highlights_added, "drawings_added": drawings_added,
    }
