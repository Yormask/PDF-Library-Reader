"""Tests for toggle_favorite mirroring book-level favorite status into a
"Favorites" category, kept automatically in sync -- so a book's favorite
status now travels through category-based exports (checking Categories
in the Export dialog, independent of Reading Status) and shows up in the
category sidebar, without changing anything about how favoriting itself
works day to day (the star button, right-click "Toggle Favorite", and
the reader's own favorite button all still just call toggle_favorite,
unchanged).

Deliberately one-directional and import-safe by design, not a full
bidirectional sync:
- toggle_favorite (the star button, right-click menu, reader's favorite
  button, and import's conditional "mark as favorite if the archive said
  so") keeps is_favorite and Favorites-category membership in sync with
  each other.
- Manually editing the Favorites category through the ordinary category
  UI (adding/removing books via category management, not via favoriting)
  is NOT synced back to is_favorite -- this is an accepted, minor edge
  case rather than something actively guarded against, in exchange for a
  much simpler implementation.
- Importing a "Favorites" category membership (because Categories was
  checked at export time) does NOT retroactively set is_favorite on the
  receiving side unless Reading Status was also checked at export time.
  This matters specifically for the "share books without your own
  reading history" use case the granular export checkboxes exist for:
  apply_archive adds category memberships directly, not through
  toggle_favorite, so it never has this side effect.

Uses a real Database and real (temporary) PDF files throughout -- no
mocks -- matching this project's existing testing convention.
"""
import os
import sys
import tempfile
import unittest

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.database import Database
from app.full_archive import apply_archive, build_manifest, write_archive


def _make_book(db, title="Favorites Test Book"):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    with open(tmp_pdf, "wb") as f:
        f.write(b"%PDF-1.4\nfake\n")
    book = db.add_book(tmp_pdf, title, 1)
    return book, tmp_pdf


class FavoritesCategoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self._tmp_pdfs = []

    def tearDown(self):
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)
        for path in self._tmp_pdfs:
            if os.path.exists(path):
                os.remove(path)

    def _add_book(self, title="Favorites Test Book"):
        book, tmp_pdf = _make_book(self.db, title)
        self._tmp_pdfs.append(tmp_pdf)
        return book


class TestToggleFavoriteSync(FavoritesCategoryTestCase):
    def test_no_favorites_category_exists_before_anything_is_favorited(self):
        self._add_book()
        self.assertIsNone(self.db.get_category_by_name("Favorites"))

    def test_favoriting_a_book_creates_the_favorites_category(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        self.assertIsNotNone(self.db.get_category_by_name("Favorites"))

    def test_favoriting_adds_the_book_to_the_category(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        names = [c["name"] for c in self.db.get_categories_for_book(book["id"])]
        self.assertIn("Favorites", names)

    def test_unfavoriting_removes_the_book_from_the_category(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])  # on
        self.db.toggle_favorite(book["id"])  # off
        names = [c["name"] for c in self.db.get_categories_for_book(book["id"])]
        self.assertNotIn("Favorites", names)

    def test_unfavoriting_does_not_delete_the_category_itself(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        self.db.toggle_favorite(book["id"])
        self.assertIsNotNone(self.db.get_category_by_name("Favorites"))

    def test_the_favorites_category_is_pinned_as_a_favorite_category(self):
        """So it naturally sorts to the top of the sidebar, matching
        get_categories()'s ORDER BY is_favorite DESC, name ASC."""
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        cat = self.db.get_category_by_name("Favorites")
        self.assertTrue(cat["is_favorite"])

    def test_the_categorys_book_count_reflects_favorited_books(self):
        book1 = self._add_book("A")
        book2 = self._add_book("B")
        self.db.toggle_favorite(book1["id"])
        self.db.toggle_favorite(book2["id"])
        cats = {c["name"]: c for c in self.db.get_categories()}
        self.assertEqual(cats["Favorites"]["book_count"], 2)

    def test_unfavoriting_one_book_does_not_affect_another(self):
        book1 = self._add_book("A")
        book2 = self._add_book("B")
        self.db.toggle_favorite(book1["id"])
        self.db.toggle_favorite(book2["id"])
        self.db.toggle_favorite(book1["id"])  # un-favorite book1 only
        names2 = [c["name"] for c in self.db.get_categories_for_book(book2["id"])]
        self.assertIn("Favorites", names2)
        cats = {c["name"]: c for c in self.db.get_categories()}
        self.assertEqual(cats["Favorites"]["book_count"], 1)

    def test_is_favorite_flag_itself_still_works_as_before(self):
        """The existing flag and its filtering must keep working
        unchanged -- this is additive, not a replacement."""
        book = self._add_book()
        self.assertEqual(self.db.get_book(book["id"])["is_favorite"], 0)
        self.db.toggle_favorite(book["id"])
        self.assertEqual(self.db.get_book(book["id"])["is_favorite"], 1)
        favorites = self.db.get_books(favorites_only=True)
        self.assertEqual([b["id"] for b in favorites], [book["id"]])

    def test_self_healing_after_the_category_is_manually_deleted(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        cat = self.db.get_category_by_name("Favorites")
        self.db.delete_category(cat["id"])
        self.assertIsNone(self.db.get_category_by_name("Favorites"))

        # Un-favoriting after the category is gone must not raise
        self.db.toggle_favorite(book["id"])
        self.assertEqual(self.db.get_book(book["id"])["is_favorite"], 0)

        # Favoriting again recreates the category and re-adds the book
        self.db.toggle_favorite(book["id"])
        cat2 = self.db.get_category_by_name("Favorites")
        self.assertIsNotNone(cat2)
        names = [c["name"] for c in self.db.get_categories_for_book(book["id"])]
        self.assertIn("Favorites", names)


class TestFavoritesMigration(FavoritesCategoryTestCase):
    """Simulates a database created before this feature existed: books
    with is_favorite=1 set directly, bypassing toggle_favorite, the way
    an already-existing database would have that state on disk."""

    def _mark_favorite_directly(self, book_id):
        self.db.conn.execute("UPDATE books SET is_favorite = 1 WHERE id = ?", (book_id,))
        self.db.conn.commit()

    def _clear_migration_flag(self):
        self.db.conn.execute("DELETE FROM settings WHERE key = 'favorites_category_migrated'")
        self.db.conn.commit()

    def test_reopening_the_database_migrates_pre_existing_favorites(self):
        book = self._add_book()
        self._mark_favorite_directly(book["id"])
        self._clear_migration_flag()

        db2 = Database(self.tmp_db)
        names = [c["name"] for c in db2.get_categories_for_book(book["id"])]
        self.assertIn("Favorites", names)

    def test_migrates_every_favorited_book_not_just_one(self):
        book1 = self._add_book("A")
        book2 = self._add_book("B")
        book3 = self._add_book("C")  # never favorited
        self._mark_favorite_directly(book1["id"])
        self._mark_favorite_directly(book2["id"])
        self._clear_migration_flag()

        db2 = Database(self.tmp_db)
        cats = {c["name"]: c for c in db2.get_categories()}
        self.assertEqual(cats["Favorites"]["book_count"], 2)
        self.assertNotIn("Favorites", [c["name"] for c in db2.get_categories_for_book(book3["id"])])

    def test_migration_only_runs_once(self):
        book = self._add_book()
        self._mark_favorite_directly(book["id"])
        self._clear_migration_flag()

        db2 = Database(self.tmp_db)  # runs the migration
        self.db.toggle_favorite(book["id"])  # un-favorite via the normal path
        db3 = Database(self.tmp_db)  # must NOT re-run and re-add it
        names = [c["name"] for c in db3.get_categories_for_book(book["id"])]
        self.assertNotIn("Favorites", names)

    def test_a_library_with_no_favorites_at_all_does_not_create_the_category(self):
        self._add_book()
        self._clear_migration_flag()
        db2 = Database(self.tmp_db)
        self.assertIsNone(db2.get_category_by_name("Favorites"))

    def test_reopening_a_normal_already_migrated_database_is_a_no_op(self):
        """A database created fresh (after this feature already existed)
        never needed migration in the first place -- toggle_favorite
        alone already created the category. Simulate that settled state
        and confirm reopening doesn't recreate or duplicate it."""
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        self.db.set_setting("favorites_category_migrated", "1")
        cat_before = self.db.get_category_by_name("Favorites")["id"]

        db2 = Database(self.tmp_db)
        cat_after = db2.get_category_by_name("Favorites")["id"]
        self.assertEqual(cat_before, cat_after)  # same category, not recreated


class TestFavoritesExportImport(FavoritesCategoryTestCase):
    def setUp(self):
        super().setUp()
        self.zip_path = tempfile.mktemp(suffix=".zip")

    def tearDown(self):
        if os.path.exists(self.zip_path):
            os.remove(self.zip_path)
        super().tearDown()

    def test_favorites_category_travels_via_categories_checkbox_alone(self):
        """The actual point of this whole feature: Categories checked,
        Reading Status unchecked -- favorite status must still travel,
        via category membership rather than the is_favorite field."""
        book = self._add_book()
        self.db.toggle_favorite(book["id"])

        manifest, filepaths = build_manifest(
            self.db, include_categories=True, include_bookmarks=False,
            include_highlights=False, include_reading_status=False, include_reading_progress=False,
        )
        entry = manifest["books"][os.path.basename(self._tmp_pdfs[0])]
        self.assertNotIn("is_favorite", entry)  # Reading Status was off
        self.assertIn("Favorites", entry["categories"])  # but Categories still carries it

    def test_importing_that_archive_adds_the_favorites_category(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        manifest, filepaths = build_manifest(
            self.db, include_categories=True, include_bookmarks=False,
            include_highlights=False, include_reading_status=False, include_reading_progress=False,
        )
        write_archive(self.zip_path, manifest, filepaths)

        dst_db_path = tempfile.mktemp(suffix=".db")
        dst_db = Database(dst_db_path)
        dest_dir = tempfile.mkdtemp()
        try:
            apply_archive(dst_db, self.zip_path, dest_dir)
            new_book = dst_db.get_book_by_filename(os.path.basename(self._tmp_pdfs[0]))
            names = [c["name"] for c in dst_db.get_categories_for_book(new_book["id"])]
            self.assertIn("Favorites", names)
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_importing_a_favorites_category_does_not_retroactively_set_is_favorite(self):
        """The specific behavior that keeps the "share without my reading
        history" export scenario honest: getting the category doesn't
        mean the receiving book silently becomes favorited too."""
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        manifest, filepaths = build_manifest(
            self.db, include_categories=True, include_bookmarks=False,
            include_highlights=False, include_reading_status=False, include_reading_progress=False,
        )
        write_archive(self.zip_path, manifest, filepaths)

        dst_db_path = tempfile.mktemp(suffix=".db")
        dst_db = Database(dst_db_path)
        dest_dir = tempfile.mkdtemp()
        try:
            apply_archive(dst_db, self.zip_path, dest_dir)
            new_book = dst_db.get_book_by_filename(os.path.basename(self._tmp_pdfs[0]))
            self.assertEqual(dst_db.get_book(new_book["id"])["is_favorite"], 0)
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_including_reading_status_still_sets_is_favorite_and_the_category(self):
        """With Reading Status also checked, is_favorite itself travels
        too (via the existing mechanism) and toggle_favorite's own sync
        then adds the category on the importing side as well."""
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        manifest, filepaths = build_manifest(
            self.db, include_categories=True, include_bookmarks=False,
            include_highlights=False, include_reading_status=True, include_reading_progress=False,
        )
        write_archive(self.zip_path, manifest, filepaths)

        dst_db_path = tempfile.mktemp(suffix=".db")
        dst_db = Database(dst_db_path)
        dest_dir = tempfile.mkdtemp()
        try:
            apply_archive(dst_db, self.zip_path, dest_dir)
            new_book = dst_db.get_book_by_filename(os.path.basename(self._tmp_pdfs[0]))
            self.assertEqual(dst_db.get_book(new_book["id"])["is_favorite"], 1)
            names = [c["name"] for c in dst_db.get_categories_for_book(new_book["id"])]
            self.assertIn("Favorites", names)
        finally:
            if os.path.exists(dst_db_path):
                os.remove(dst_db_path)

    def test_excluding_categories_entirely_means_no_favorites_category_travels(self):
        book = self._add_book()
        self.db.toggle_favorite(book["id"])
        manifest, filepaths = build_manifest(
            self.db, include_categories=False, include_bookmarks=False,
            include_highlights=False, include_reading_status=False, include_reading_progress=False,
        )
        entry = manifest["books"][os.path.basename(self._tmp_pdfs[0])]
        self.assertNotIn("categories", entry)

    def test_a_non_favorited_book_does_not_get_a_favorites_category_on_export(self):
        self._add_book()  # never favorited
        manifest, _ = build_manifest(self.db, include_categories=True)
        entry = manifest["books"][os.path.basename(self._tmp_pdfs[0])]
        self.assertNotIn("Favorites", entry.get("categories", []))
        self.assertNotIn("Favorites", manifest["categories"])


if __name__ == "__main__":
    unittest.main()
