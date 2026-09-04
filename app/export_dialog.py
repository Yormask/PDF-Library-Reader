"""ExportOptionsDialog -- the single, unified "Export..." entry point.
Customizable checkboxes for exactly what an export contains: the PDF
files themselves, plus bookmarks, highlights & drawings, reading status,
and reading progress, independently. Category memberships always travel
along regardless -- that's what makes even the lightest export (PDF
Files unchecked, everything else unchecked too) still a coherent piece
of a library rather than a bare, uncategorized file list.

Unchecking PDF Files produces the same kind of lightweight, metadata-only
export the old separate "Categories Only" and "Bookmarks Only" actions
used to (now chosen right here instead of as separate menu items) -- for
syncing categories/bookmarks/etc. between installs that already share
the same PDF files, matched by filename on import exactly like a full
archive already does.

Scope -- every book in the library, or just the ones currently selected
-- is decided automatically from whether any books were selected before
Export was clicked, passed in here rather than chosen in this dialog, so
there's nothing to pick wrong.
"""
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class ExportOptionsDialog(QDialog):
    def __init__(self, book_count, is_selection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export")
        layout = QVBoxLayout(self)

        if is_selection:
            scope_text = f"Exporting {book_count} selected book{'s' if book_count != 1 else ''}."
        else:
            scope_text = f"Exporting all {book_count} book{'s' if book_count != 1 else ''} in your library."
        scope_label = QLabel(scope_text)
        scope_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(scope_label)

        note = QLabel("Category memberships always travel along. Choose what else to include:")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.pdf_check = QCheckBox("PDF Files")
        self.pdf_check.setChecked(True)
        self.pdf_check.setToolTip(
            "Uncheck for a lightweight, metadata-only export -- for syncing "
            "categories/bookmarks/etc. between installs that already share the "
            "same PDF files, matched by filename"
        )
        layout.addWidget(self.pdf_check)

        self.bookmarks_check = QCheckBox("Bookmarks")
        self.bookmarks_check.setChecked(True)
        layout.addWidget(self.bookmarks_check)

        self.highlights_check = QCheckBox("Highlights && Drawings")
        self.highlights_check.setChecked(True)
        layout.addWidget(self.highlights_check)

        self.status_check = QCheckBox("Reading Status (unread / reading / finished, favorites)")
        self.status_check.setChecked(True)
        layout.addWidget(self.status_check)

        self.progress_check = QCheckBox("Reading Progress (last page read)")
        self.progress_check.setChecked(True)
        layout.addWidget(self.progress_check)

        hint = QLabel(
            "Tip: uncheck PDF Files for a lightweight export of just categories "
            "plus whatever else is checked above -- the same idea as the old "
            "\"Categories Only\"/\"Bookmarks Only\" exports, now chosen right "
            "here. Uncheck everything except PDF Files to share books without "
            "handing over your own notes or reading history."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        export_btn = QPushButton("Export...")
        export_btn.setDefault(True)
        export_btn.clicked.connect(self.accept)
        btn_row.addWidget(export_btn)
        layout.addLayout(btn_row)

    def options(self):
        """(include_pdf_files, include_bookmarks, include_highlights,
        include_reading_status, include_reading_progress) -- call only
        after exec() returns QDialog.Accepted."""
        return (
            self.pdf_check.isChecked(),
            self.bookmarks_check.isChecked(),
            self.highlights_check.isChecked(),
            self.status_check.isChecked(),
            self.progress_check.isChecked(),
        )
