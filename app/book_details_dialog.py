"""Dialog for previewing a book's cover and editing its metadata:
title, author, series, genre(s), language(s), annotation, favorite and status.

Genre and Language are both checkable multi-select dropdowns (a book can
have more than one of either), each with a "Custom" checkbox that adds one
more freely-typed value on top of whatever's picked from the list. Multiple
values are joined with '_' -- e.g. "Science Fiction_Fantasy" or
"English_Bulgarian".
"""
import os

from PySide6.QtCore import Qt, QRegularExpression, Signal
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from .file_naming import format_series_number, sync_filename
from .multi_select_combo import MultiSelectComboBox
from .presets import GENRE_PRESETS, LANGUAGE_PRESETS, merge_with_used, normalize_custom_value
from .thumbnails import ensure_thumbnail
from .widgets import human_size

STATUS_OPTIONS = [
    ("unread", "Unread"),
    ("to_read", "To Read"),
    ("reading", "Reading"),
    ("finished", "Finished"),
]


class BookDetailsDialog(QDialog):
    book_updated = Signal()       # emitted after Save, so the caller can refresh its view
    open_requested = Signal(int)  # emitted when "Open Book" is clicked

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.book_id = None
        # Populated with the full option list (presets + everything in
        # actual use across the library) each time load_book() runs -- see
        # _refresh_genre_language_options(). Defaulted here just so
        # _current_genre()/_current_language() have something sane if ever
        # called before the first load_book().
        self._all_genres = list(GENRE_PRESETS)
        self._all_languages = list(LANGUAGE_PRESETS)
        self.setWindowTitle("Book Details")
        self.resize(440, 660)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.cover_label = QLabel()
        self.cover_label.setAlignment(Qt.AlignCenter)
        self.cover_label.setFixedHeight(190)
        layout.addWidget(self.cover_label)

        self.meta_label = QLabel()
        self.meta_label.setAlignment(Qt.AlignCenter)
        self.meta_label.setStyleSheet("color: #888;")
        layout.addWidget(self.meta_label)

        self.filename_label = QLabel()
        self.filename_label.setAlignment(Qt.AlignCenter)
        self.filename_label.setWordWrap(True)
        self.filename_label.setStyleSheet("color: #999; font-size: 11px;")
        layout.addWidget(self.filename_label)

        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.author_edit = QLineEdit()
        self.series_edit = QLineEdit()

        # "Book #" -- a book's position within its Series (1, 2, 2.5 for a
        # novella between two entries, etc.), so a series can be browsed in
        # reading order rather than just grouped by name. Free-standing
        # (not tied to any preset list, and deliberately left out of the
        # filename convention -- see file_naming.py -- since it's a fifth
        # field on top of an already-fixed five-slot naming scheme).
        # Blank is a valid, common state: not every book belongs to a
        # numbered series. The validator blocks anything that isn't blank
        # or a plain non-negative number, so there's never an unparseable
        # value to reject later at save time.
        self.series_number_edit = QLineEdit()
        self.series_number_edit.setPlaceholderText("Book #")
        self.series_number_edit.setToolTip(
            "This book's position within its Series -- e.g. 1, 2, or 2.5 "
            "for a novella between two entries. Leave blank if it doesn't "
            "have one. Used by the \"Series (Reading Order)\" sort option."
        )
        self.series_number_edit.setFixedWidth(70)
        self.series_number_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"^\d{0,4}(\.\d{0,2})?$"))
        )
        series_row = QHBoxLayout()
        series_row.addWidget(self.series_edit, stretch=1)
        series_row.addWidget(QLabel("Book #"))
        series_row.addWidget(self.series_number_edit)

        # Genre: checkable multi-select dropdown (a book can be more than one
        # genre), plus an optional extra custom genre typed on top of that.
        self.genre_combo = MultiSelectComboBox()
        self.genre_combo.add_items(GENRE_PRESETS)
        self.genre_custom_check = QCheckBox("Custom")
        self.genre_custom_check.toggled.connect(self._on_genre_custom_toggled)
        self.genre_custom_edit = QLineEdit()
        self.genre_custom_edit.setPlaceholderText("Add another genre...")
        self.genre_custom_edit.hide()
        genre_row = QHBoxLayout()
        genre_row.addWidget(self.genre_combo, stretch=1)
        genre_row.addWidget(self.genre_custom_edit, stretch=1)
        genre_row.addWidget(self.genre_custom_check)

        # Language: same treatment -- a book can have more than one.
        self.language_combo = MultiSelectComboBox()
        self.language_combo.add_items(LANGUAGE_PRESETS)
        self.language_custom_check = QCheckBox("Custom")
        self.language_custom_check.toggled.connect(self._on_language_custom_toggled)
        self.language_custom_edit = QLineEdit()
        self.language_custom_edit.setPlaceholderText("Add another language...")
        self.language_custom_edit.hide()
        language_row = QHBoxLayout()
        language_row.addWidget(self.language_combo, stretch=1)
        language_row.addWidget(self.language_custom_edit, stretch=1)
        language_row.addWidget(self.language_custom_check)

        self.status_combo = QComboBox()
        for value, label in STATUS_OPTIONS:
            self.status_combo.addItem(label, value)
        self.annotation_edit = QTextEdit()
        self.annotation_edit.setPlaceholderText("Notes about this book...")
        self.annotation_edit.setFixedHeight(90)

        form.addRow("Title", self.title_edit)
        form.addRow("Author", self.author_edit)
        form.addRow("Series", series_row)
        form.addRow("Genre", genre_row)
        form.addRow("Language", language_row)
        form.addRow("Status", self.status_combo)
        form.addRow("Annotation", self.annotation_edit)
        layout.addLayout(form)

        hint = QLabel(
            "Saving renames the file to \u201cTitle - Author - Series - Genre - "
            "Language.pdf\u201d, so the info travels with it if you move or copy it "
            "to another device. A book with more than one genre or language "
            "shows as e.g. \u201cScience Fiction_Fantasy\u201d or "
            "\u201cEnglish_Bulgarian\u201d, and is found when searching for any one "
            "of them."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #999; font-size: 11px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        self.favorite_btn = QPushButton("\u2606 Favorite")
        self.favorite_btn.setCheckable(True)
        self.favorite_btn.clicked.connect(self._toggle_favorite)
        btn_row.addWidget(self.favorite_btn)

        btn_row.addStretch()

        open_btn = QPushButton("Open Book")
        open_btn.clicked.connect(self._open_book)
        btn_row.addWidget(open_btn)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

    def _on_genre_custom_toggled(self, checked):
        self.genre_custom_edit.setVisible(checked)

    def _on_language_custom_toggled(self, checked):
        self.language_custom_edit.setVisible(checked)

    def _refresh_genre_language_options(self):
        """Rebuild the Genre/Language dropdown items from the preset lists
        plus every value actually in use across the library right now --
        a custom genre/language typed on some other book, or one an
        imported filename already encoded -- so it shows up as a normal,
        checkable option here too instead of only ever living in the
        Custom field. Runs every time a book is loaded so a value added
        anywhere shows up immediately, without needing to reopen the app."""
        self._all_genres = merge_with_used(GENRE_PRESETS, self.db.get_distinct_genres())
        self.genre_combo.clear_items()
        self.genre_combo.add_items(self._all_genres)

        self._all_languages = merge_with_used(LANGUAGE_PRESETS, self.db.get_distinct_languages())
        self.language_combo.clear_items()
        self.language_combo.add_items(self._all_languages)

    def load_book(self, book_id):
        self.book_id = book_id
        book = self.db.get_book(book_id)
        if not book:
            return
        self.setWindowTitle(f"Book Details \u2014 {book['title']}")

        pixmap, _is_corrupted = ensure_thumbnail(book_id, book["filepath"])
        self.cover_label.setPixmap(
            pixmap.scaled(140, 190, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

        try:
            size = os.path.getsize(book["filepath"])
        except OSError:
            size = -1
        meta_bits = [human_size(size)]
        if book["page_count"]:
            meta_bits.append(f"{book['page_count']} pages")
        self.meta_label.setText(" \u00b7 ".join(meta_bits))
        self.filename_label.setText(f"File: {os.path.basename(book['filepath'])}")

        self.title_edit.setText(book["title"] or "")
        self.author_edit.setText(book["author"] or "")
        self.series_edit.setText(book["series"] or "")
        self.series_number_edit.setText(self._format_series_number(book["series_number"]))
        self.annotation_edit.setPlainText(book["annotation"] or "")

        self._refresh_genre_language_options()

        self._load_multi_value(
            book["genre"] or "", self._all_genres,
            self.genre_combo, self.genre_custom_check, self.genre_custom_edit,
        )
        self._on_genre_custom_toggled(self.genre_custom_check.isChecked())

        self._load_multi_value(
            book["language"] or "", self._all_languages,
            self.language_combo, self.language_custom_check, self.language_custom_edit,
        )
        self._on_language_custom_toggled(self.language_custom_check.isChecked())

        status = book["status"] or "unread"
        idx = self.status_combo.findData(status)
        self.status_combo.setCurrentIndex(idx if idx >= 0 else 0)

        self.favorite_btn.setChecked(bool(book["is_favorite"]))
        self.favorite_btn.setText("\u2605 Favorited" if book["is_favorite"] else "\u2606 Favorite")

    @staticmethod
    def _load_multi_value(raw_value, presets, combo, custom_check, custom_edit):
        """Split a '_'-joined value: a token matching a known value (case-
        insensitively, since older data may predate normalization) gets
        checked in the dropdown using that value's actual casing; anything
        else goes into the Custom field (joined back with '_' if there's
        more than one genuinely-unrecognized value)."""
        tokens = [t.strip() for t in raw_value.split("_") if t.strip()]
        known_lower = {p.lower(): p for p in presets}
        preset_tokens, custom_tokens = [], []
        for t in tokens:
            canonical = known_lower.get(t.lower())
            (preset_tokens if canonical else custom_tokens).append(canonical or t)
        combo.set_checked_items(preset_tokens)
        if custom_tokens:
            custom_check.setChecked(True)
            custom_edit.setText("_".join(custom_tokens))
        else:
            custom_check.setChecked(False)
            custom_edit.setText("")

    def _toggle_favorite(self):
        if self.book_id is None:
            return
        self.db.toggle_favorite(self.book_id)
        book = self.db.get_book(self.book_id)
        self.favorite_btn.setChecked(bool(book["is_favorite"]))
        self.favorite_btn.setText("\u2605 Favorited" if book["is_favorite"] else "\u2606 Favorite")
        self.book_updated.emit()

    @staticmethod
    def _combine_multi_value(combo, custom_check, custom_edit, known_values):
        parts = list(combo.checked_items())
        if custom_check.isChecked():
            custom = custom_edit.text().strip()
            if custom:
                parts.extend(
                    normalize_custom_value(p.strip(), known_values)
                    for p in custom.split("_") if p.strip()
                )
        # de-duplicate case-insensitively while preserving order
        seen = set()
        ordered = []
        for p in parts:
            key = p.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(p)
        return "_".join(ordered)

    def _current_genre(self):
        return self._combine_multi_value(
            self.genre_combo, self.genre_custom_check, self.genre_custom_edit, self._all_genres
        )

    def _current_language(self):
        return self._combine_multi_value(
            self.language_combo, self.language_custom_check, self.language_custom_edit,
            self._all_languages,
        )

    def _current_series(self):
        """Series has no preset list -- it's freeform -- so normalization
        here only ever reuses an existing series' own casing (typing "dune
        saga" when "Dune Saga" is already used elsewhere becomes "Dune
        Saga"); a genuinely new series still just gets light title-casing,
        same as a new custom Genre/Language."""
        existing = merge_with_used(
            [], (b["series"] for b in self.db.get_books() if b.get("series"))
        )
        return normalize_custom_value(self.series_edit.text().strip(), existing)

    @staticmethod
    def _format_series_number(value):
        return format_series_number(value)

    def _current_series_number(self):
        """Parse the Book # field back into a float, or None if left
        blank. The field's validator (see _build_ui) already guarantees
        the text is either empty or a plain non-negative number, so this
        never has anything genuinely invalid to reject -- the try/except
        is just a defensive backstop."""
        text = self.series_number_edit.text().strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _save(self):
        if self.book_id is None:
            return
        try:
            self.db.update_metadata(
                self.book_id,
                title=self.title_edit.text().strip() or "Untitled",
                author=self.author_edit.text().strip(),
                series=self._current_series(),
                genre=self._current_genre(),
                language=self._current_language(),
                annotation=self.annotation_edit.toPlainText().strip(),
            )
            self.db.set_series_number(self.book_id, self._current_series_number())
            self.db.set_status(self.book_id, self.status_combo.currentData())
            renamed, info = sync_filename(self.db, self.book_id)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Couldn't save changes",
                f"Something went wrong while saving:\n{exc}",
            )
            return  # leave the dialog open so nothing is lost

        if info and not renamed:
            # sync_filename returns (False, error_message) only when a rename
            # was needed but failed; (False, None) means nothing needed renaming.
            QMessageBox.warning(
                self,
                "Couldn't rename file",
                f"Your changes were saved, but the file on disk couldn't be renamed "
                f"to match:\n{info}",
            )

        self.book_updated.emit()
        self.close()

    def _open_book(self):
        if self.book_id is not None:
            self.open_requested.emit(self.book_id)
        self.close()
