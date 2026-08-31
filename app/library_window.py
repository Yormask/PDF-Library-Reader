"""Main library window: browse, search, sort, filter, favorite, categorize,
and open PDF books."""
import json
import os
import shutil
import zipfile
from collections import OrderedDict

import pymupdf as fitz  # PyMuPDF (module renamed from "fitz")
from PySide6.QtCore import QEvent, QRegularExpression, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QRegularExpressionValidator, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .add_to_category_dialog import AddToCategoryDialog
from .badges import decorate_thumbnail
from .book_details_dialog import STATUS_OPTIONS, BookDetailsDialog
from .bookmark_export import apply_import_data as apply_bookmark_import
from .bookmark_export import build_export_data as build_bookmark_export
from .bookmark_export import read_export_file as read_bookmark_export_file
from .bookmark_export import write_export_file as write_bookmark_export_file
from .category_export import apply_import_data, build_export_data, read_export_file, write_export_file
from .database import Database
from .duplicates import compute_file_hash, find_duplicate_groups
from .file_naming import format_series_number, parse_filename, sync_filename
from .flow_layout import FlowLayout
from .full_archive import apply_archive as apply_full_archive
from .full_archive import build_manifest as build_archive_manifest
from .full_archive import read_manifest as read_archive_manifest
from .full_archive import write_archive
from .multi_select_combo import ClickToOpenComboBox, MultiSelectComboBox
from .pdf_password import PasswordUnlockDialog, strip_or_change_password
from .presets import GENRE_PRESETS, LANGUAGE_PRESETS, merge_with_used, normalize_custom_value
from .reader_window import ReaderWindow
from .search_dialog import TextSearchDialog
from .shortcuts import effective_shortcut, load_overrides, save_overrides, save_wheel_overrides
from .shortcuts_dialog import ShortcutsDialog
from .themes import DARK_THEME, LIGHT_THEME
from .thumbnails import delete_thumbnail, ensure_thumbnail
from .widgets import BookCard, CoverCell, human_size

# index in the sort combo -> (sort key, descending?)
SORT_OPTIONS = {
    0: ("title", False),   # Title A-Z
    1: ("title", True),    # Title Z-A
    2: ("recent", True),   # Recently read first
    3: ("recent", False),  # Least recently read first
    4: ("added", True),    # Recently added first
    5: ("size", True),     # Largest file first
    6: ("size", False),    # Smallest file first
    7: ("series_order", False),  # Series (Reading Order)
}

# Derived rather than hardcoded a second time, so _apply_series_suggestion()
# (clicking a Series in the search-suggestions preview) always lands on
# whichever combo index actually corresponds to "series_order" above, even
# if SORT_OPTIONS is ever reordered.
SERIES_ORDER_SORT_INDEX = next(idx for idx, (key, _) in SORT_OPTIONS.items() if key == "series_order")

# index in the status filter combo -> status value passed to the database
STATUS_FILTER_OPTIONS = [
    ("None", None),
    ("To Read", "to_read"),
    ("Currently Reading", "reading"),
    ("Finished", "finished"),
    ("Unread", "unread"),
]

ALPHABET_INDEX = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ") + ["#"]


class _ExportCancelledError(Exception):
    """Raised from a write_archive progress callback to abort a
    still-in-progress export when the user clicks Cancel on the progress
    dialog."""


def _group_letter(title):
    stripped = (title or "").strip()
    if not stripped:
        return "#"
    first = stripped[0].upper()
    return first if first.isalpha() else "#"


NO_SERIES_LABEL = "No Series"


def _group_series(series):
    stripped = (series or "").strip()
    return stripped or NO_SERIES_LABEL


class LibraryWindow(QMainWindow):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.setWindowTitle("PDF Library")
        self.resize(1080, 640)
        self.reader_windows = {}  # book_id -> ReaderWindow, kept alive while open
        self.show_favorites_only = False
        self.view_mode = db.get_setting("library_view_mode", "list")  # "list" or "grid"
        self._search_dialog = None
        self._details_dialog = None
        self._letter_headers = {}  # letter -> header QLabel, populated by _render_grid
        self._list_letter_headers = {}  # letter -> header QListWidgetItem, populated by _render_list
        self.selected_category_id = None  # None = "All Books" (no category filter)
        self._selected_book_ids = set()  # multi-selection for bulk actions
        self._last_clicked_book_id = None  # anchor for Shift+click range selection
        self.select_mode = False  # while off, clicking a book does nothing (prevents accidental selection)
        self.library_page = 1  # current page when paginating (applies to every sort mode)
        self._letter_page_map = {}  # letter -> page number, for cross-page A-Z jumps
        self.genre_lang_filter_mode = False  # when on, replaces the A-Z bar with genre/language filters
        self.selected_genres = set()
        self.selected_languages = set()
        self.selected_series = set()
        self._missing_book_ids = set()  # books flagged at last sync -- gone, or outside the library folder
        self._truly_missing_book_ids = set()  # subset of the above: file not found on disk at all
        self._relocatable_book_ids = set()  # subset of the above: file exists, just outside the library folder
        self._duplicate_groups = []  # populated by _sync_duplicate_books() -- see there

        self._build_ui()
        self._apply_theme(self.db.get_setting("theme", "light"))
        self.text_view_btn.setChecked(self.view_mode == "list")
        self.image_view_btn.setChecked(self.view_mode == "grid")
        self.refresh_library(show_feedback=False)  # initial startup check: also syncs missing files

    # ---------------- UI ----------------
    def _as_widget_action(self, widget):
        """Wraps an existing widget (typically a checkable QPushButton) so
        it can be embedded directly as a menu item -- keeping the exact
        same widget instance (and therefore every existing .setChecked()
        call site elsewhere in this file) working unchanged, just
        displayed inside a menu instead of the toolbar."""
        action = QWidgetAction(self)
        action.setDefaultWidget(widget)
        return action

    def _build_ui(self):
        menubar = self.menuBar()
        toolbar = QToolBar("Main")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # ---- Actions and toggle buttons (created once; placed into the
        # toolbar and/or menu bar below -- a QAction can live in both at
        # once and stays in sync automatically, so anything genuinely
        # frequent (adding books, refreshing, switching view) gets a
        # toolbar shortcut without losing menu-bar discoverability; a
        # QPushButton toggle can only be *shown* in one place, so those
        # sit wherever makes most sense and keep exactly the same
        # attribute name and setChecked() call sites as before. ----
        add_action = QAction("Add Book(s)...", self)
        add_action.triggered.connect(self.add_books)
        toolbar.addAction(add_action)
        self.add_books_action = add_action

        add_folder_action = QAction("Add Folder...", self)
        add_folder_action.triggered.connect(self.add_folder)
        toolbar.addAction(add_folder_action)
        self.add_folder_action = add_folder_action

        library_folder_action = QAction("Library Folder...", self)
        library_folder_action.setToolTip(
            "Choose a folder for \"Add Book(s)\" and \"Add Folder\" to move new "
            "books into, so your whole library lives in one place"
        )
        library_folder_action.triggered.connect(self.choose_library_folder)

        toolbar.addSeparator()

        self.select_mode_btn = QPushButton("Select")
        self.select_mode_btn.setCheckable(True)
        self.select_mode_btn.setToolTip(
            "Turn on to click books and select them for bulk actions "
            "(add to a category, remove several at once)"
        )
        self.select_mode_btn.clicked.connect(self.toggle_select_mode)
        toolbar.addWidget(self.select_mode_btn)

        toolbar.addSeparator()

        self.text_view_btn = QPushButton("Simple Text")
        self.text_view_btn.setCheckable(True)
        self.text_view_btn.setToolTip("Show the library as a detailed text list")
        self.text_view_btn.clicked.connect(lambda: self.set_view_mode("list"))
        toolbar.addWidget(self.text_view_btn)

        self.image_view_btn = QPushButton("Image Preview")
        self.image_view_btn.setCheckable(True)
        self.image_view_btn.setToolTip("Show the library as a grid of page-1 thumbnails")
        self.image_view_btn.clicked.connect(lambda: self.set_view_mode("grid"))
        toolbar.addWidget(self.image_view_btn)

        toolbar.addSeparator()

        self.refresh_action = QAction("Refresh Library", self)
        self.refresh_action.setToolTip(
            "Refresh the library -- re-checks for files that were renamed "
            "or deleted outside the app"
        )
        self.refresh_action.triggered.connect(lambda: self.refresh_library())
        toolbar.addAction(self.refresh_action)

        # Push the settings gear all the way to the toolbar's far right edge.
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)

        self.shortcuts_action = QAction("\u2699", self)  # gear glyph -- no icon assets elsewhere in the app to match
        self.shortcuts_action.setToolTip("Keyboard Shortcuts...")
        self.shortcuts_action.triggered.connect(self.open_shortcuts_dialog)
        toolbar.addAction(self.shortcuts_action)

        # ---- Toggle buttons that live in the View menu, not the toolbar ----
        self.all_btn = QPushButton("All Books")
        self.all_btn.setCheckable(True)
        self.all_btn.setChecked(True)
        self.all_btn.clicked.connect(lambda: self.set_favorites_filter(False))

        self.fav_btn = QPushButton("\u2605 Favorites")
        self.fav_btn.setCheckable(True)
        self.fav_btn.clicked.connect(lambda: self.set_favorites_filter(True))

        self.genre_lang_filter_btn = QPushButton("Genres, Languages && Series")
        self.genre_lang_filter_btn.setCheckable(True)
        self.genre_lang_filter_btn.setToolTip(
            "Show a Genre/Language/Series filter bar below the A-Z index"
        )
        self.genre_lang_filter_btn.clicked.connect(self.toggle_genre_lang_filter_mode)

        self.theme_btn = QPushButton("Dark Mode")
        self.theme_btn.setCheckable(True)
        self.theme_btn.clicked.connect(self.toggle_theme)

        # ---- Export submenu (also used verbatim inside the File menu) ----
        # Kept as a real attribute, not a local variable -- PySide6 can
        # garbage-collect a QMenu whose only reference is local even after
        # addMenu() reparents it at the C++ level, which would crash the
        # app the moment someone actually opened File > Export.
        export_menu = QMenu("Export", self)
        self._export_menu = export_menu
        export_menu.addAction(
            "Share Full Archive (PDFs + Categories + Bookmarks)..."
        ).triggered.connect(self.export_share_full_archive)
        export_menu.addAction(
            "Share Selected Books (PDFs + Categories + Bookmarks)..."
        ).triggered.connect(lambda: self.export_selected_books_share())
        export_menu.addSeparator()
        export_menu.addAction(
            "Full Export (PDFs + Categories + Bookmarks + Reading Data)..."
        ).triggered.connect(self.export_full_archive)
        export_menu.addAction(
            "Selected Books Export (PDFs + Categories + Bookmarks + Reading Data)..."
        ).triggered.connect(lambda: self.export_selected_books_archive())
        export_menu.addSeparator()
        export_menu.addAction("Categories Only...").triggered.connect(self.export_library)
        export_menu.addAction("Bookmarks Only...").triggered.connect(self.export_bookmarks_only)

        import_action = QAction("Import...", self)
        import_action.setToolTip(
            "Restore a previously exported file -- Full Archive (.zip), or a "
            "Categories or Bookmarks export (.json). The right kind of "
            "import is detected automatically from the file you pick."
        )
        import_action.triggered.connect(self.import_file)
        self.import_action = import_action

        search_text_action = QAction("Search Text...", self)
        search_text_action.setToolTip("Search for text inside all your books")
        search_text_action.triggered.connect(self.open_text_search)

        # ---- Menu bar: everything above, organized by purpose, so
        # nothing is more than two clicks away even after trimming the
        # toolbar down to just the handful of actions used constantly ----
        file_menu = menubar.addMenu("&File")
        self._file_menu = file_menu
        file_menu.addAction(add_action)
        file_menu.addAction(add_folder_action)
        file_menu.addAction(library_folder_action)
        file_menu.addSeparator()
        file_menu.addAction(import_action)
        file_menu.addMenu(export_menu)

        view_menu = menubar.addMenu("&View")
        self._view_menu = view_menu
        view_menu.addAction(self._as_widget_action(self.all_btn))
        view_menu.addAction(self._as_widget_action(self.fav_btn))
        view_menu.addSeparator()
        view_menu.addAction(self._as_widget_action(self.genre_lang_filter_btn))
        view_menu.addSeparator()
        view_menu.addAction(self._as_widget_action(self.theme_btn))

        tools_menu = menubar.addMenu("&Tools")
        self._tools_menu = tools_menu
        tools_menu.addAction(search_text_action)
        tools_menu.addAction(self.refresh_action)
        tools_menu.addSeparator()
        tools_menu.addAction(self.shortcuts_action)

        # A few actions are keyboard-only -- no toolbar button of their own,
        # but still customizable like everything else in the catalog.
        self.toggle_select_mode_shortcut = QShortcut(QKeySequence(), self)
        self.toggle_select_mode_shortcut.activated.connect(self.select_mode_btn.click)
        self.focus_search_shortcut = QShortcut(QKeySequence(), self)
        self.focus_search_shortcut.activated.connect(self._focus_search_box)
        self.open_shortcuts_shortcut = QShortcut(QKeySequence(), self)
        self.open_shortcuts_shortcut.activated.connect(self.open_shortcuts_dialog)

        # ---- Overall layout: category sidebar (left) + main content (right) ----
        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_category_sidebar())

        main_content = QWidget()
        layout = QVBoxLayout(main_content)

        controls = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            "Filter by title, author, series, or genre... (use \u201cSearch Text\u201d to search inside books)"
        )
        self.search_box.textChanged.connect(self._reset_page_and_refresh)
        self.search_box.textChanged.connect(self._update_search_suggestions)
        controls.addWidget(self.search_box, stretch=1)

        controls.addWidget(QLabel("Status:"))
        self.status_filter_combo = QComboBox()
        for label, value in STATUS_FILTER_OPTIONS:
            self.status_filter_combo.addItem(label, value)
        self.status_filter_combo.currentIndexChanged.connect(self._reset_page_and_refresh)
        controls.addWidget(self.status_filter_combo)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(
            [
                "Title (A-Z)",
                "Title (Z-A)",
                "Recently Read",
                "Oldest Read",
                "Recently Added",
                "File Size (Largest)",
                "File Size (Smallest)",
                "Series (Reading Order)",
            ]
        )
        self.sort_combo.setItemData(
            7,
            "Groups every book by Series, ordered within each by its Book # "
            "(set in Book Details) -- books with no series sort after every "
            "series; books in a series with no number sort after numbered "
            "ones. Combine with the search box or a category to browse just "
            "one series in reading order.",
            Qt.ToolTipRole,
        )
        self.sort_combo.currentIndexChanged.connect(self._reset_page_and_refresh)
        controls.addWidget(self.sort_combo)

        controls.addWidget(QLabel("Per page:"))
        self.per_page_combo = QComboBox()
        self.per_page_combo.addItems(["All", "10", "25", "50", "100"])
        self.per_page_combo.setToolTip(
            "Split the library into pages instead of showing every book at "
            "once -- helps with performance on large libraries, including "
            "when sorted alphabetically"
        )
        self.per_page_combo.currentIndexChanged.connect(self._reset_page_and_refresh)
        controls.addWidget(self.per_page_combo)
        layout.addLayout(controls)

        # Selection indicator: shown only while one or more books are selected.
        selection_row = QHBoxLayout()
        self.selection_label = QLabel("")
        self.selection_label.setStyleSheet("color: #888;")
        selection_row.addWidget(self.selection_label)
        selection_row.addStretch()
        self.clear_selection_btn = QPushButton("Clear Selection")
        self.clear_selection_btn.clicked.connect(self.clear_selection)
        selection_row.addWidget(self.clear_selection_btn)
        layout.addLayout(selection_row)
        self.selection_label.hide()
        self.clear_selection_btn.hide()

        # Missing-files indicator: shown after a sync (startup or Refresh/F5)
        # finds books whose file wasn't found where the library expects it.
        missing_row = QHBoxLayout()
        self.missing_files_btn = QPushButton("")
        self.missing_files_btn.setFlat(True)
        self.missing_files_btn.setCursor(Qt.PointingHandCursor)
        self.missing_files_btn.setStyleSheet(
            "color: #b45309; text-align: left; border: none; padding: 2px 0px;"
        )
        self.missing_files_btn.setToolTip("Click to see which books and choose what to do")
        self.missing_files_btn.clicked.connect(self._show_missing_files_dialog)
        missing_row.addWidget(self.missing_files_btn)
        missing_row.addStretch()
        layout.addLayout(missing_row)
        self.missing_files_btn.hide()

        # Duplicates indicator: shown after a sync (startup, Refresh/F5, or
        # right after Add Book(s)/Add Folder) finds books that look like
        # duplicates of each other -- see _sync_duplicate_books().
        duplicates_row = QHBoxLayout()
        self.duplicates_btn = QPushButton("")
        self.duplicates_btn.setFlat(True)
        self.duplicates_btn.setCursor(Qt.PointingHandCursor)
        self.duplicates_btn.setStyleSheet(
            "color: #b45309; text-align: left; border: none; padding: 2px 0px;"
        )
        self.duplicates_btn.setToolTip("Click to review and choose what to do")
        self.duplicates_btn.clicked.connect(self._show_duplicates_dialog)
        duplicates_row.addWidget(self.duplicates_btn)
        duplicates_row.addStretch()
        layout.addLayout(duplicates_row)
        self.duplicates_btn.hide()

        # Live categorized preview (Titles / Authors / Series / Genres) shown
        # while typing in the filter box; hidden whenever no text or no matches.
        self.suggestion_panel = QWidget()
        self.suggestion_layout = QVBoxLayout(self.suggestion_panel)
        self.suggestion_layout.setContentsMargins(6, 4, 6, 4)
        self.suggestion_layout.setSpacing(1)
        self.suggestion_panel.setStyleSheet(
            "background-color: rgba(127, 127, 127, 30); border: 1px solid #ccc; border-radius: 4px;"
        )
        layout.addWidget(self.suggestion_panel)
        self.suggestion_panel.hide()

        # A-Z index and the Genre/Language/Series filter bar sit above BOTH
        # the list and grid views (not nested inside either one), so they
        # stay available regardless of which view you're browsing in --
        # they used to live only inside the grid container, which meant
        # they silently vanished whenever Simple Text (list) view was
        # active, since that container was hidden outright.
        self.alpha_bar_top, self._alpha_buttons_top = self._build_alpha_bar()
        self.alpha_bar_top.hide()
        layout.addWidget(self.alpha_bar_top)

        self.genre_lang_bar = self._build_genre_lang_bar()
        self.genre_lang_bar.hide()
        layout.addWidget(self.genre_lang_bar)

        # "Simple Text" view: a detailed list of BookCard rows.
        self.list_widget = QListWidget()
        self.list_widget.setSpacing(4)
        layout.addWidget(self.list_widget)

        # "Image Preview" view: a scrollable, wrapping grid of cover thumbnails,
        # grouped under a letter header when sorted alphabetically.
        self.grid_container = QWidget()
        grid_col = QVBoxLayout(self.grid_container)
        grid_col.setContentsMargins(0, 0, 0, 0)
        grid_col.setSpacing(4)

        self.grid_scroll = QScrollArea()
        self.grid_scroll.setWidgetResizable(True)
        self.grid_scroll.setFrameShape(QScrollArea.NoFrame)
        grid_col.addWidget(self.grid_scroll, stretch=1)

        layout.addWidget(self.grid_container)

        self.empty_label = QLabel(
            'No books yet. Click "Add Book(s)" or "Add Folder" to build your library.'
        )
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #999; padding: 40px;")
        layout.addWidget(self.empty_label)
        self.empty_label.hide()

        # Pagination nav: only shown when "Per page" isn't "All" and the sort
        # isn't Title (which uses the A-Z index instead of pages).
        self.pagination_widget = QWidget()
        self.pagination_row = QHBoxLayout(self.pagination_widget)
        self.pagination_row.addStretch()
        self.prev_page_btn = QPushButton("\u25c0 Previous")
        self.prev_page_btn.clicked.connect(self._go_to_prev_page)
        self.pagination_row.addWidget(self.prev_page_btn)
        self.page_indicator_label = QLabel("")
        self.pagination_row.addWidget(self.page_indicator_label)
        self.next_page_btn = QPushButton("Next \u25b6")
        self.next_page_btn.clicked.connect(self._go_to_next_page)
        self.pagination_row.addWidget(self.next_page_btn)
        self.pagination_row.addStretch()
        layout.addWidget(self.pagination_widget)
        self.pagination_widget.hide()

        outer.addWidget(main_content, stretch=1)
        self.setCentralWidget(central)

        # Clicking empty space (no book under the cursor) in either view clears
        # the current multi-selection, so you don't have to click a selected
        # book again or hunt for the Clear Selection button.
        self.list_widget.viewport().installEventFilter(self)
        self.grid_scroll.viewport().installEventFilter(self)

        # Ctrl+A selects every book currently shown (respecting Select mode
        # and, when paginated, only the current page -- matching how "select
        # all" works in most apps: everything visible, not the whole library).
        self.select_all_shortcut = QShortcut(QKeySequence(), self)
        self.select_all_shortcut.activated.connect(self._select_all_visible)

        self._apply_shortcuts()

    def _focus_search_box(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _apply_shortcuts(self):
        """(Re-)applies every customizable action's current effective
        shortcut -- called once at startup, and again after the
        Keyboard Shortcuts dialog saves a change, so an already-running
        window picks up the new bindings immediately rather than needing
        a restart."""
        overrides = load_overrides(self.db)
        bindings = (
            (self.add_books_action, "library.add_books"),
            (self.add_folder_action, "library.add_folder"),
            (self.import_action, "library.import"),
            (self.refresh_action, "library.refresh"),
            (self.select_all_shortcut, "library.select_all"),
            (self.toggle_select_mode_shortcut, "library.toggle_select_mode"),
            (self.focus_search_shortcut, "library.focus_search"),
            (self.open_shortcuts_shortcut, "library.open_shortcuts"),
        )
        for target, action_id in bindings:
            seq = QKeySequence(effective_shortcut(action_id, overrides))
            if isinstance(target, QShortcut):
                target.setKey(seq)
            else:
                target.setShortcut(seq)

    def open_shortcuts_dialog(self):
        dialog = ShortcutsDialog(self.db, self)
        if dialog.exec() != QDialog.Accepted:
            return
        save_overrides(self.db, dialog.result_overrides())
        save_wheel_overrides(self.db, dialog.result_wheel_overrides())
        self._apply_shortcuts()
        for win in self.reader_windows.values():
            win.apply_shortcuts()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            if obj is self.list_widget.viewport():
                if self.list_widget.itemAt(event.position().toPoint()) is None:
                    self.clear_selection()
            elif obj is self.grid_scroll.viewport():
                content = self.grid_scroll.widget()
                if content is not None:
                    local_pos = content.mapFromParent(event.position().toPoint())
                    if content.childAt(local_pos) is None:
                        self.clear_selection()
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        # Belt-and-suspenders alongside the Select All QShortcut: some focused
        # child widgets (e.g. the list/grid viewport after a click) can
        # intercept the key before a window-level shortcut ever sees it, so
        # this guarantees Select All always works regardless of what has
        # focus -- checked against the CURRENT (possibly user-customized)
        # shortcut, not a hardcoded Ctrl+A, so a remapped key isn't silently
        # shadowed by the old default still working here underneath it.
        seq = QKeySequence(event.keyCombination())
        if not seq.isEmpty() and seq == self.select_all_shortcut.key():
            self._select_all_visible()
            event.accept()
            return
        super().keyPressEvent(event)

    def _build_alpha_bar(self):
        """A horizontal, wrapping row of A-Z (+#) buttons that jump to that
        letter's section in the grid. Returns (widget, {letter: button})."""
        bar = QWidget()
        bar_layout = FlowLayout(bar, margin=2, hspacing=2, vspacing=2)
        buttons = {}
        for letter in ALPHABET_INDEX:
            btn = QPushButton(letter)
            btn.setFlat(True)
            btn.setFixedSize(24, 22)
            btn.setToolTip(f"Jump to \u201c{letter}\u201d")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("font-size: 10px; padding: 0px;")
            btn.setEnabled(False)
            btn.clicked.connect(lambda checked=False, l=letter: self._jump_to_letter(l))
            bar_layout.addWidget(btn)
            buttons[letter] = btn
        return bar, buttons

    # ------------- Genre / Language / Series filter bar (replaces the A-Z bar) -------------
    def _build_genre_lang_bar(self):
        bar = QWidget()
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(2, 2, 2, 2)
        bar_layout.setSpacing(6)

        bar_layout.addWidget(QLabel("Genre:"))
        self.genre_filter_combo = MultiSelectComboBox()
        self.genre_filter_combo.setToolTip(
            "Select one or more genres -- matches are OR'd together"
        )
        self.genre_filter_combo.selection_changed.connect(self._on_genre_filter_changed)
        bar_layout.addWidget(self.genre_filter_combo, stretch=1)

        bar_layout.addWidget(QLabel("Language:"))
        self.language_filter_combo = MultiSelectComboBox()
        self.language_filter_combo.setToolTip(
            "Select one or more languages -- a multi-language book matches if "
            "it has ANY of the languages you pick"
        )
        self.language_filter_combo.selection_changed.connect(self._on_language_filter_changed)
        bar_layout.addWidget(self.language_filter_combo, stretch=1)

        bar_layout.addWidget(QLabel("Series:"))
        self.series_filter_combo = MultiSelectComboBox()
        self.series_filter_combo.setToolTip(
            "Select one or more series -- shows only books belonging to one "
            "of them. Combine with the \"Series (Reading Order)\" sort to "
            "browse a series in order."
        )
        self.series_filter_combo.selection_changed.connect(self._on_series_filter_changed)
        bar_layout.addWidget(self.series_filter_combo, stretch=1)

        clear_btn = QPushButton("Clear Filters")
        clear_btn.setToolTip("Deselect every genre, language, and series filter")
        clear_btn.clicked.connect(self._clear_genre_lang_filters)
        bar_layout.addWidget(clear_btn)

        return bar

    def _refresh_genre_lang_bar_contents(self):
        """Rebuild the genre/language/series dropdown items from the
        current preset lists (Series has none -- it's freeform) plus any
        custom values actually in use, so newly-added custom genres/
        languages/series show up as filter options too. Checked state
        (from self.selected_genres/languages/series) is preserved across
        rebuilds. Every item stays selectable regardless of current match
        count -- disabling an item you've already checked would leave you
        unable to uncheck it again once its filtered result count drops to
        zero."""
        all_genres = self._current_genre_options()
        self.genre_filter_combo.blockSignals(True)
        self.genre_filter_combo.clear_items()
        self.genre_filter_combo.add_items(all_genres)
        self.genre_filter_combo.set_checked_items(self.selected_genres)
        self.genre_filter_combo.blockSignals(False)

        all_languages = self._current_language_options()
        self.language_filter_combo.blockSignals(True)
        self.language_filter_combo.clear_items()
        self.language_filter_combo.add_items(all_languages)
        self.language_filter_combo.set_checked_items(self.selected_languages)
        self.language_filter_combo.blockSignals(False)

        all_series = self._current_series_options()
        self.series_filter_combo.blockSignals(True)
        self.series_filter_combo.clear_items()
        self.series_filter_combo.add_items(all_series)
        self.series_filter_combo.set_checked_items(self.selected_series)
        self.series_filter_combo.blockSignals(False)

    def _current_genre_options(self):
        """Every genre available to pick from anywhere in the app: the
        preset list plus any custom genre actually in use on a book right
        now (typed by hand, or backfilled from a compliant filename on
        import) -- shared by the filter bar and the Genre "Set..." dialogs
        so a custom value shows up as a normal option everywhere, not just
        wherever it was first typed."""
        return merge_with_used(GENRE_PRESETS, self.db.get_distinct_genres())

    def _current_language_options(self):
        """Language counterpart to _current_genre_options() above."""
        return merge_with_used(LANGUAGE_PRESETS, self.db.get_distinct_languages())

    def _current_series_options(self):
        """Every Series name currently in use, case-insensitively deduped
        -- Series has no preset list at all (it's freeform), unlike Genre/
        Language."""
        return merge_with_used([], self.db.get_distinct_series())


    def _on_genre_filter_changed(self):
        self.selected_genres = set(self.genre_filter_combo.checked_items())
        # Deferred: this fires synchronously from within the combo's own
        # item-press handler (while its popup is still open), and refreshing
        # rebuilds that same combo's model -- doing that immediately corrupts
        # the popup mid-interaction and can close it early. Let the click
        # finish first.
        QTimer.singleShot(0, self._reset_page_and_refresh)

    def _on_language_filter_changed(self):
        self.selected_languages = set(self.language_filter_combo.checked_items())
        QTimer.singleShot(0, self._reset_page_and_refresh)  # see _on_genre_filter_changed

    def _on_series_filter_changed(self):
        self.selected_series = set(self.series_filter_combo.checked_items())
        QTimer.singleShot(0, self._reset_page_and_refresh)  # see _on_genre_filter_changed

    def _clear_genre_lang_filters(self):
        self.selected_genres.clear()
        self.selected_languages.clear()
        self.selected_series.clear()
        self._reset_page_and_refresh()

    def toggle_genre_lang_filter_mode(self, checked):
        self.genre_lang_filter_btn.setChecked(checked)
        self.genre_lang_filter_mode = checked
        if not checked:
            # Leaving the mode clears the filters too, so there's no
            # invisible active filter left behind once the bar disappears.
            self.selected_genres.clear()
            self.selected_languages.clear()
            self.selected_series.clear()
        self.refresh_list()

    def _build_category_sidebar(self):
        sidebar = QWidget()
        sidebar.setFixedWidth(200)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)

        header = QLabel("Categories")
        header.setStyleSheet("font-weight: bold; font-size: 13px;")
        sidebar_layout.addWidget(header)

        new_cat_btn = QPushButton("+ New Category")
        new_cat_btn.clicked.connect(self.create_new_category)
        sidebar_layout.addWidget(new_cat_btn)

        self.category_list = QListWidget()
        self.category_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.category_list.itemSelectionChanged.connect(self._on_category_selection_changed)
        self.category_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.category_list.customContextMenuRequested.connect(self._show_category_context_menu)
        sidebar_layout.addWidget(self.category_list)

        hint = QLabel(
            "Right-click a category to add books, favorite, rename, export, "
            "or delete it. Ctrl+click/Shift+click/Ctrl+A to select several "
            "categories for bulk actions. Right-click any book (or a "
            "multi-selection) to add it to a category."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 11px;")
        sidebar_layout.addWidget(hint)

        return sidebar

    # ------------- Categories -------------
    def refresh_categories_sidebar(self):
        self.category_list.clear()
        none_item = QListWidgetItem("All Books (None)")
        none_item.setData(Qt.UserRole, None)
        self.category_list.addItem(none_item)

        selected_row = 0
        for i, cat in enumerate(self.db.get_categories(), start=1):
            star = "\u2605 " if cat["is_favorite"] else ""
            item = QListWidgetItem(f"{star}{cat['name']} ({cat['book_count']})")
            item.setData(Qt.UserRole, cat["id"])
            self.category_list.addItem(item)
            if cat["id"] == self.selected_category_id:
                selected_row = i
        self.category_list.setCurrentRow(selected_row)

    def _on_category_selection_changed(self):
        # Plain click (or Ctrl+click down to exactly one) sets the active
        # filter; a genuine multi-selection (2+, via Ctrl/Shift-click or
        # Ctrl+A) is for bulk actions instead and doesn't change what's
        # currently being filtered by.
        selected = self.category_list.selectedItems()
        if len(selected) == 1:
            self.selected_category_id = selected[0].data(Qt.UserRole)
            self.library_page = 1
            self.refresh_list()

    def create_new_category(self):
        name, ok = QInputDialog.getText(self, "New Category", "Category name:")
        if ok and name.strip():
            self.db.create_category(name.strip())
            self.refresh_categories_sidebar()

    def _show_category_context_menu(self, pos):
        item = self.category_list.itemAt(pos)
        if item is None or item.data(Qt.UserRole) is None:
            return  # empty area, or the "All Books (None)" pseudo-entry

        selected_items = [
            i for i in self.category_list.selectedItems() if i.data(Qt.UserRole) is not None
        ]
        if item in selected_items and len(selected_items) > 1:
            self._show_bulk_category_context_menu(selected_items, pos)
        else:
            self._show_single_category_context_menu(item, pos)

    def _show_single_category_context_menu(self, item, pos):
        category_id = item.data(Qt.UserRole)
        category = self.db.get_category(category_id)
        if category is None:
            return

        menu = QMenu(self)
        add_action = menu.addAction("Add Books...")
        fav_label = "Remove from Favorite Categories" if category["is_favorite"] else "Favorite Category"
        fav_action = menu.addAction(fav_label)
        rename_action = menu.addAction("Rename...")
        export_action = menu.addAction("Export...")
        delete_action = menu.addAction("Delete Category")
        chosen = menu.exec(self.category_list.viewport().mapToGlobal(pos))

        if chosen == add_action:
            self._open_add_to_category_dialog(category_id, category["name"])
        elif chosen == fav_action:
            self.db.toggle_category_favorite(category_id)
            self.refresh_categories_sidebar()
        elif chosen == rename_action:
            self._rename_category(category_id, category["name"])
        elif chosen == export_action:
            self._export_categories([category_id])
        elif chosen == delete_action:
            self._delete_category(category_id, category["name"])

    def _show_bulk_category_context_menu(self, items, pos):
        category_ids = [i.data(Qt.UserRole) for i in items]
        n = len(category_ids)
        menu = QMenu(self)
        fav_action = menu.addAction(f"Favorite {n} Categories")
        unfav_action = menu.addAction(f"Unfavorite {n} Categories")
        export_action = menu.addAction(f"Export {n} Categories...")
        delete_action = menu.addAction(f"Delete {n} Categories")
        chosen = menu.exec(self.category_list.viewport().mapToGlobal(pos))

        if chosen == fav_action:
            self._bulk_set_category_favorite(category_ids, True)
        elif chosen == unfav_action:
            self._bulk_set_category_favorite(category_ids, False)
        elif chosen == export_action:
            self._export_categories(category_ids)
        elif chosen == delete_action:
            self._bulk_delete_categories(category_ids)

    def _bulk_set_category_favorite(self, category_ids, favorite):
        for cid in category_ids:
            cat = self.db.get_category(cid)
            if cat and bool(cat["is_favorite"]) != favorite:
                self.db.toggle_category_favorite(cid)
        self.refresh_categories_sidebar()

    def _bulk_delete_categories(self, category_ids):
        reply = QMessageBox.question(
            self,
            "Delete categories",
            f"Delete these {len(category_ids)} categories? Your books stay in "
            f"the library \u2014 they're just removed from these categories.",
        )
        if reply == QMessageBox.Yes:
            for cid in category_ids:
                self.db.delete_category(cid)
                if self.selected_category_id == cid:
                    self.selected_category_id = None
            self.refresh_categories_sidebar()
            self.refresh_list()

    def _rename_category(self, category_id, current_name):
        name, ok = QInputDialog.getText(
            self, "Rename Category", "New name:", text=current_name
        )
        if ok and name.strip():
            if not self.db.rename_category(category_id, name.strip()):
                QMessageBox.warning(
                    self, "Couldn't rename", "A category with that name already exists."
                )
            self.refresh_categories_sidebar()

    def _delete_category(self, category_id, name):
        reply = QMessageBox.question(
            self,
            "Delete category",
            f"Delete the category \u201c{name}\u201d? Your books stay in the "
            f"library \u2014 they're just removed from this category.",
        )
        if reply == QMessageBox.Yes:
            self.db.delete_category(category_id)
            if self.selected_category_id == category_id:
                self.selected_category_id = None
            self.refresh_categories_sidebar()
            self.refresh_list()

    def _open_add_to_category_dialog(self, category_id, category_name):
        dialog = AddToCategoryDialog(self.db, category_id, category_name, self)
        dialog.books_added.connect(self.refresh_categories_sidebar)
        dialog.books_added.connect(self.refresh_list)
        dialog.exec()

    # ------------- Category export / import -------------
    def _export_categories(self, category_ids):
        """Export every book belonging to any of the given categories."""
        book_ids = set()
        for cid in category_ids:
            book_ids.update(b["id"] for b in self.db.get_books(category_id=cid))
        self._run_export(list(book_ids))

    def export_library(self):
        """Export every categorized book in the library. (For a narrower
        export, right-click a specific category instead -- or select several
        with Ctrl+click/Shift+click and use "Export N Categories..." from
        their right-click menu.)"""
        self._run_export(None)

    def _run_export(self, book_ids):
        """Returns True once the export actually completes, False if there
        was nothing to export, the save dialog was canceled, or writing
        failed -- used by callers that only want to react (e.g. clear a
        selection) on genuine success."""
        data = build_export_data(self.db, book_ids)
        if not data["books"]:
            QMessageBox.information(
                self, "Nothing to export",
                "None of those books belong to any category, so there's nothing to export.",
            )
            return False
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Categories", os.path.expanduser("~/library-categories.json"),
            "JSON files (*.json)",
        )
        if not path:
            return False
        try:
            write_export_file(path, data)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Couldn't write the export file:\n{exc}")
            return False
        n_books, n_cats = len(data["books"]), len(data["categories"])
        QMessageBox.information(
            self, "Export complete",
            f"Exported {n_books} book{'s' if n_books != 1 else ''} across {n_cats} "
            f"categor{'y' if n_cats == 1 else 'ies'} to:\n{path}",
        )
        return True

    def _import_categories_file(self, path):
        try:
            data = read_export_file(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Import failed", f"Couldn't read that file:\n{exc}")
            return
        summary = apply_import_data(self.db, data)
        self.refresh_categories_sidebar()
        self.refresh_list()
        n_created = summary["categories_created"]
        QMessageBox.information(
            self, "Import complete",
            f"Matched {summary['matched']} book(s) already in your library.\n"
            f"Skipped {summary['skipped']} book(s) not found here.\n"
            f"Created {n_created} new categor{'y' if n_created == 1 else 'ies'}.",
        )

    # ------------- Bookmarks-only export / import -------------
    def export_bookmarks_only(self):
        data = build_bookmark_export(self.db, None)
        if not data["books"]:
            QMessageBox.information(
                self, "Nothing to export", "No books have any bookmarks yet, so there's nothing to export.",
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Bookmarks", os.path.expanduser("~/library-bookmarks.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            write_bookmark_export_file(path, data)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Couldn't write the export file:\n{exc}")
            return
        n_books = len(data["books"])
        QMessageBox.information(
            self, "Export complete",
            f"Exported bookmarks for {n_books} book{'s' if n_books != 1 else ''} to:\n{path}",
        )

    def _import_bookmarks_file(self, path):
        try:
            data = read_bookmark_export_file(path)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Import failed", f"Couldn't read that file:\n{exc}")
            return
        summary = apply_bookmark_import(self.db, data)
        self.refresh_list()
        QMessageBox.information(
            self, "Import complete",
            f"Matched {summary['matched']} book(s) already in your library.\n"
            f"Skipped {summary['skipped']} book(s) not found here.\n"
            f"Added {summary['bookmarks_added']} new bookmark(s).",
        )

    # ------------- Full archive export / import -------------
    def export_full_archive(self):
        """Full Export: every book in your library, with categories,
        bookmarks, and your own reading progress (status, favorite,
        last page read). Best for backing up your whole library or moving
        it to another machine of your own."""
        self._run_full_archive_export(None, "Full Export", include_reading_state=True)

    def export_share_full_archive(self):
        """Share Full Archive: every book in your library, with categories
        and bookmarks, but not your own reading progress -- so whoever
        you're handing this to gets a clean copy instead of books that show
        up mysteriously pre-favorited or already marked Finished."""
        self._run_full_archive_export(None, "Share Full Archive", include_reading_state=False)

    def export_selected_books_archive(self, book_ids=None, clear_selection_after=False):
        """Selected Books Export: just the given books (or the current
        selection, if book_ids isn't passed) -- categories, bookmarks, and
        your own reading progress. Best for backing up a subset of your
        library or moving it to another machine of your own, since that
        reading state is exactly what you'd want restored. For sending
        books to someone else, use "Share Selected Books" instead."""
        book_ids = list(book_ids) if book_ids is not None else list(self._selected_book_ids)
        if not book_ids:
            QMessageBox.information(
                self, "No books selected",
                "Turn on Select, then click (or Ctrl+click/Shift+click for "
                "several) the books you want to export first.",
            )
            return
        success = self._run_full_archive_export(
            book_ids, "Selected Books Export", include_reading_state=True
        )
        if success and clear_selection_after:
            self.clear_selection()

    def export_selected_books_share(self, book_ids=None, clear_selection_after=False):
        """Share Selected Books: just the given books (or the current
        selection, if book_ids isn't passed) -- categories and bookmarks
        travel over, but not your own status/favorite/last-page -- so their
        copy starts clean."""
        book_ids = list(book_ids) if book_ids is not None else list(self._selected_book_ids)
        if not book_ids:
            QMessageBox.information(
                self, "No books selected",
                "Turn on Select, then click (or Ctrl+click/Shift+click for "
                "several) the books you want to share first.",
            )
            return
        success = self._run_full_archive_export(
            book_ids, "Share Selected Books", include_reading_state=False
        )
        if success and clear_selection_after:
            self.clear_selection()

    def _run_full_archive_export(self, book_ids, dialog_title, include_reading_state=True):
        """Returns True once the export actually completes, False if there
        was nothing to export, the save dialog was canceled, the write
        failed, or the user cancelled partway through -- used by callers
        that only want to react (e.g. clear a selection) on genuine
        success."""
        manifest, filepaths = build_archive_manifest(
            self.db, book_ids, include_reading_state=include_reading_state
        )
        if not manifest["books"]:
            QMessageBox.information(
                self, "Nothing to export", "There's nothing to export.",
            )
            return False
        default_name = "library-backup.zip" if book_ids is None else "shared-books.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, dialog_title, os.path.expanduser(f"~/{default_name}"),
            "ZIP archives (*.zip)",
        )
        if not path:
            return False

        # Copying potentially many/large PDFs into a zip can take a while --
        # show real progress instead of leaving the window looking frozen.
        total = len(manifest["books"])
        progress = QProgressDialog("Preparing export...", "Cancel", 0, total, self)
        progress.setWindowTitle(dialog_title)
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)  # show immediately, even for quick exports
        progress.setValue(0)
        QApplication.processEvents()

        def on_progress(index, total_count, filename):
            if progress.wasCanceled():
                raise _ExportCancelledError()
            progress.setLabelText(f"Exporting {index} of {total_count}: {filename}")
            progress.setValue(index)
            QApplication.processEvents()

        try:
            skipped = write_archive(path, manifest, filepaths, progress_callback=on_progress)
        except _ExportCancelledError:
            progress.close()
            try:
                os.remove(path)  # don't leave a half-written archive behind
            except OSError:
                pass
            return False
        except OSError as exc:
            progress.close()
            QMessageBox.critical(self, "Export failed", f"Couldn't write the archive:\n{exc}")
            return False
        progress.close()

        n_books = len(manifest["books"]) - len(skipped)
        detail = "categories, bookmarks, and reading progress" if include_reading_state \
            else "categories and bookmarks"
        msg = f"Exported {n_books} book(s) (with {detail}) to:\n{path}"
        if skipped:
            msg += f"\n\n{len(skipped)} book(s) were skipped because their file couldn't be found on disk."
        QMessageBox.information(self, "Export complete", msg)
        return True

    def import_file(self):
        """Universal import: pick one file, and the right kind of import
        (Full Archive, Categories, or Bookmarks) is detected automatically
        from its content, instead of needing to pick the right menu item
        first."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import",
            os.path.expanduser("~"),
            "Supported files (*.zip *.json);;ZIP archives (*.zip);;"
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return

        kind = self._detect_import_kind(path)
        if kind == "full_archive":
            self._import_full_archive_file(path)
        elif kind == "categories":
            self._import_categories_file(path)
        elif kind == "bookmarks":
            self._import_bookmarks_file(path)
        else:
            QMessageBox.critical(
                self, "Import failed",
                "That doesn't look like a file this app can import \u2014 "
                "expected a Full Archive (.zip), or a Categories or "
                "Bookmarks export (.json).",
            )

    @staticmethod
    def _detect_import_kind(path):
        """Returns 'full_archive', 'categories', 'bookmarks', or None if the
        file doesn't look like any export this app produces."""
        if zipfile.is_zipfile(path):
            return "full_archive"

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        kind = data.get("kind")
        if kind in ("categories", "bookmarks"):
            return kind

        # No "kind" marker -- this predates that field, so fall back to
        # structural detection: the categories format has a top-level
        # "categories" key, while the bookmarks format's "books" entries
        # are each a plain list of bookmarks rather than a dict.
        if "categories" in data:
            return "categories"
        books = data.get("books")
        if isinstance(books, dict) and books and all(isinstance(v, list) for v in books.values()):
            return "bookmarks"
        return None

    def _import_full_archive_file(self, path):
        library_folder = self.db.get_setting("library_folder")
        if library_folder and os.path.isdir(library_folder):
            # A library folder is configured -- use it automatically instead
            # of asking each time, same as Add Book(s)/Add Folder already do.
            destination = library_folder
        else:
            destination = QFileDialog.getExistingDirectory(
                self, "Choose a folder to save any new books into", os.path.expanduser("~"),
            )
            if not destination:
                return
        try:
            summary = apply_full_archive(self.db, path, destination)
        except (OSError, KeyError, ValueError) as exc:
            QMessageBox.critical(self, "Import failed", f"Couldn't read that archive:\n{exc}")
            return
        self.refresh_categories_sidebar()
        self.refresh_list()
        n_created = summary["categories_created"]
        n_highlights = summary.get("highlights_added", 0)
        n_drawings = summary.get("drawings_added", 0)
        msg = (
            f"Added {summary['added']} new book(s) to your library.\n"
            f"Matched {summary['matched']} book(s) total (new + already present).\n"
            f"Skipped {summary['skipped']} book(s) missing from the archive.\n"
            f"Added {summary['bookmarks_added']} new bookmark(s), "
            f"created {n_created} new categor{'y' if n_created == 1 else 'ies'}."
        )
        if n_highlights:
            msg += f"\nAdded {n_highlights} new highlight(s)."
        if n_drawings:
            msg += f"\nAdded {n_drawings} new drawing(s)."
        QMessageBox.information(self, "Import complete", msg)

    # ------------- Multi-select & bulk actions -------------
    def toggle_book_selection(self, book_id):
        # Deferred: this is called synchronously from within the clicked
        # card/cell's own mousePressEvent, and refresh_list() destroys that
        # same widget tree -- rebuilding immediately would delete the widget
        # while Qt is still mid-dispatch on its event, causing a crash.
        if book_id in self._selected_book_ids:
            self._selected_book_ids.discard(book_id)
        else:
            self._selected_book_ids.add(book_id)
        self._last_clicked_book_id = book_id  # anchor for a future Shift+click
        self._update_selection_indicator()
        QTimer.singleShot(0, self.refresh_list)

    def handle_range_select(self, target_book_id):
        """Shift+click: select every book between the last-clicked book (the
        anchor) and this one, inclusive, in their current on-screen order."""
        if not self.select_mode:
            return
        visible_ids = self._get_visible_book_ids_in_order()
        anchor_id = self._last_clicked_book_id
        if anchor_id is None or anchor_id not in visible_ids or target_book_id not in visible_ids:
            # No usable anchor (e.g. it scrolled off to a different page) --
            # fall back to treating this like a normal single click.
            self.toggle_book_selection(target_book_id)
            return
        start = visible_ids.index(anchor_id)
        end = visible_ids.index(target_book_id)
        if start > end:
            start, end = end, start
        self._selected_book_ids = set(visible_ids[start:end + 1])
        self._last_clicked_book_id = target_book_id
        self._update_selection_indicator()
        QTimer.singleShot(0, self.refresh_list)

    def _select_all_visible(self):
        """Ctrl+A: select every book currently shown (the current page, if
        paginated) -- same "select what's visible" convention most apps use."""
        if not self.select_mode:
            return
        ids = self._get_visible_book_ids_in_order()
        if not ids:
            return
        self._selected_book_ids.update(ids)
        self._last_clicked_book_id = ids[-1]
        self._update_selection_indicator()
        self.refresh_list()

    def _get_visible_book_ids_in_order(self):
        if self.view_mode == "list":
            return [
                self.list_widget.itemWidget(self.list_widget.item(i)).book_id
                for i in range(self.list_widget.count())
            ]
        content = self.grid_scroll.widget()
        if content is None:
            return []
        return [cell.book_id for cell in content.findChildren(CoverCell)]

    def clear_selection(self):
        # Deferred for the same reason -- this can also be triggered from a
        # book's own right-click context menu ("Clear Selection").
        if not self._selected_book_ids:
            return  # nothing to do -- avoid an unnecessary re-render
        self._selected_book_ids.clear()
        self._last_clicked_book_id = None
        self._update_selection_indicator()
        QTimer.singleShot(0, self.refresh_list)

    def _update_selection_indicator(self):
        n = len(self._selected_book_ids)
        self.selection_label.setVisible(n > 0 or self.select_mode)
        self.clear_selection_btn.setVisible(n > 0)
        if n > 0:
            self.selection_label.setText(
                f"{n} book{'s' if n != 1 else ''} selected \u2014 right-click any "
                f"selected book to add them all to a category"
            )
        elif self.select_mode:
            self.selection_label.setText("Select mode is on \u2014 click books to select them")

    def show_book_context_menu(self, book_id, global_pos):
        if book_id in self._selected_book_ids and len(self._selected_book_ids) > 1:
            self._show_bulk_context_menu(set(self._selected_book_ids), global_pos)
        else:
            self._show_single_context_menu(book_id, global_pos)

    def _show_single_context_menu(self, book_id, global_pos):
        menu = QMenu(self)
        menu.addAction("Open").triggered.connect(lambda: self.open_book(book_id))
        menu.addAction("Details").triggered.connect(lambda: self.open_book_details(book_id))
        menu.addAction("Toggle Favorite").triggered.connect(lambda: self.toggle_favorite(book_id))
        add_menu = menu.addMenu("Add to Category")
        # Single-book action: don't touch any unrelated active multi-selection.
        self._populate_category_menu(add_menu, [book_id], clear_selection_after=False)
        status_menu = menu.addMenu("Mark as")
        self._populate_status_menu(status_menu, [book_id], clear_selection_after=False)
        menu.addAction("Set Series...").triggered.connect(lambda: self._set_series_for_books([book_id]))
        menu.addAction("Set Genre...").triggered.connect(lambda: self._set_genre_for_books([book_id]))
        menu.addAction("Set Language...").triggered.connect(lambda: self._set_language_for_books([book_id]))
        menu.addAction("Remove from Library").triggered.connect(lambda: self.remove_book(book_id))
        menu.addAction("Delete from Disk...").triggered.connect(lambda: self.delete_book_from_disk(book_id))
        menu.exec(global_pos)

    def _show_bulk_context_menu(self, book_ids, global_pos):
        n = len(book_ids)
        menu = QMenu(self)
        add_menu = menu.addMenu(f"Add {n} Selected to Category")
        # Bulk action: the selection has now been "used", so clear it once done.
        self._populate_category_menu(add_menu, list(book_ids), clear_selection_after=True)
        menu.addAction(f"Set Series for {n} Selected...").triggered.connect(
            lambda: self._set_series_for_books(list(book_ids), clear_selection_after=True)
        )
        menu.addAction(f"Set Genre for {n} Selected...").triggered.connect(
            lambda: self._set_genre_for_books(list(book_ids), clear_selection_after=True)
        )
        menu.addAction(f"Set Language for {n} Selected...").triggered.connect(
            lambda: self._set_language_for_books(list(book_ids), clear_selection_after=True)
        )
        status_menu = menu.addMenu(f"Mark {n} Selected as")
        self._populate_status_menu(status_menu, list(book_ids), clear_selection_after=True)
        export_menu = menu.addMenu("Exports")
        export_menu.addAction("Share Selected Books (PDFs + Categories + Bookmarks)...").triggered.connect(
            lambda: self.export_selected_books_share(list(book_ids), clear_selection_after=True)
        )
        export_menu.addAction(
            "Selected Books Export (PDFs + Categories + Bookmarks + Reading Data)..."
        ).triggered.connect(
            lambda: self.export_selected_books_archive(list(book_ids), clear_selection_after=True)
        )
        menu.addAction(f"Remove {n} Selected from Library").triggered.connect(
            lambda: self._bulk_remove_books(list(book_ids))
        )
        menu.addAction(f"Delete {n} Selected from Disk...").triggered.connect(
            lambda: self._bulk_delete_books_from_disk(list(book_ids))
        )
        menu.addAction("Clear Selection").triggered.connect(self.clear_selection)
        menu.exec(global_pos)

    def _populate_category_menu(self, menu, book_ids, clear_selection_after=False):
        categories = self.db.get_categories()
        if not categories:
            empty_action = menu.addAction("(No categories yet)")
            empty_action.setEnabled(False)
        for cat in categories:
            action = menu.addAction(cat["name"])
            action.triggered.connect(
                lambda checked=False, cid=cat["id"]: self._add_books_to_category(
                    cid, book_ids, clear_selection_after
                )
            )
        menu.addSeparator()
        menu.addAction("New Category...").triggered.connect(
            lambda: self._create_category_and_add(book_ids, clear_selection_after)
        )

    def _populate_status_menu(self, menu, book_ids, clear_selection_after=False):
        for value, label in STATUS_OPTIONS:
            menu.addAction(label).triggered.connect(
                lambda checked=False, s=value: self._set_status_for_books(
                    book_ids, s, clear_selection_after
                )
            )

    def _set_status_for_books(self, book_ids, status, clear_selection_after=False):
        self.db.bulk_set_status(book_ids, status)
        if clear_selection_after:
            self.clear_selection()
        self.refresh_list()

    def _add_books_to_category(self, category_id, book_ids, clear_selection_after=False):
        self.db.add_books_to_category(category_id, book_ids)
        self.refresh_categories_sidebar()
        if clear_selection_after:
            self.clear_selection()

    def _create_category_and_add(self, book_ids, clear_selection_after=False):
        name, ok = QInputDialog.getText(self, "New Category", "Category name:")
        if not ok or not name.strip():
            return
        category = self.db.create_category(name.strip())
        if category:
            self.db.add_books_to_category(category["id"], book_ids)
            self.refresh_categories_sidebar()
            if clear_selection_after:
                self.clear_selection()

    # ------------- Quick-set Series / Genre / Language for selected books -------------
    def _set_series_for_books(self, book_ids, clear_selection_after=False):
        existing = merge_with_used(
            [], (b["series"] for b in self.db.get_books() if b.get("series"))
        )
        current_series = ""
        current_number = ""
        if len(book_ids) == 1:
            book = self.db.get_book(book_ids[0])
            if book:
                current_series = book["series"] or ""
                current_number = format_series_number(book["series_number"])

        dialog = QDialog(self)
        dialog.setWindowTitle("Set Series")
        layout = QVBoxLayout(dialog)

        row = QHBoxLayout()
        row.addWidget(QLabel("Series:"))
        combo = ClickToOpenComboBox()
        combo.addItems(existing)
        combo.setCurrentText(current_series)
        row.addWidget(combo, stretch=1)

        row.addWidget(QLabel("Book #"))
        number_edit = QLineEdit()
        number_edit.setText(current_number)
        number_edit.setPlaceholderText("Book #")
        number_edit.setFixedWidth(70)
        number_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"^\d{0,4}(\.\d{0,2})?$"))
        )
        if len(book_ids) > 1:
            number_edit.setToolTip(
                "Sets the same Book # for every selected book -- leave blank "
                "to clear it for all of them instead. Usually only useful "
                "one book at a time; use Book Details for that."
            )
        else:
            number_edit.setToolTip(
                "This book's position within its Series -- e.g. 1, 2, or "
                "2.5 for a novella between two entries. Leave blank if it "
                "doesn't have one. Used by the \"Series (Reading Order)\" "
                "sort option."
            )
        row.addWidget(number_edit)
        layout.addLayout(row)
        layout.addLayout(self._ok_cancel_row(dialog))

        if dialog.exec() != QDialog.Accepted:
            return

        series_value = combo.currentText().strip()
        number_text = number_edit.text().strip()
        series_number = float(number_text) if number_text else None

        self.db.bulk_set_series(book_ids, series_value)
        self.db.bulk_set_series_number(book_ids, series_number)
        for book_id in book_ids:
            sync_filename(self.db, book_id)
        if clear_selection_after:
            self.clear_selection()
        self.refresh_list()

    def _set_genre_for_books(self, book_ids, clear_selection_after=False):
        current = ""
        if len(book_ids) == 1:
            current = (self.db.get_book(book_ids[0]) or {"genre": ""})["genre"] or ""
        value = self._prompt_multi_value("Set Genre", self._current_genre_options(), current)
        if value is None:
            return
        self._apply_bulk_field(book_ids, self.db.bulk_set_genre, value, clear_selection_after)

    def _set_language_for_books(self, book_ids, clear_selection_after=False):
        current = ""
        if len(book_ids) == 1:
            current = (self.db.get_book(book_ids[0]) or {"language": ""})["language"] or ""
        value = self._prompt_multi_value("Set Language", self._current_language_options(), current)
        if value is None:
            return
        self._apply_bulk_field(book_ids, self.db.bulk_set_language, value, clear_selection_after)

    def _prompt_multi_value(self, title, presets, current_value):
        """Shared dialog for bulk-setting Genre/Language: a checkable
        multi-select dropdown (check any number of presets, or previously-
        used custom values -- see _current_genre_options/_current_language_
        options) plus a Custom field for one more, freely-typed value --
        mirrors the same fields in Book Details. Returns the new
        '_'-joined value, or None if canceled."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)

        combo = MultiSelectComboBox()
        combo.add_items(presets)
        custom_check = QCheckBox("Custom")
        custom_edit = QLineEdit()
        custom_edit.setPlaceholderText("Add another...")
        custom_edit.hide()
        custom_check.toggled.connect(custom_edit.setVisible)

        tokens = [t.strip() for t in (current_value or "").split("_") if t.strip()]
        # Case-insensitive lookup so a token that only differs from a known
        # value by capitalization (e.g. leftover dirty data from before
        # values were normalized) still lands as a checked box using that
        # value's actual on-screen casing, instead of silently falling
        # through to the Custom field.
        known_lower = {p.lower(): p for p in presets}
        preset_tokens, custom_tokens = [], []
        for t in tokens:
            canonical = known_lower.get(t.lower())
            (preset_tokens if canonical else custom_tokens).append(canonical or t)
        combo.set_checked_items(preset_tokens)
        if custom_tokens:
            custom_check.setChecked(True)
            custom_edit.setText("_".join(custom_tokens))

        row = QHBoxLayout()
        row.addWidget(combo, stretch=1)
        row.addWidget(custom_edit, stretch=1)
        row.addWidget(custom_check)
        layout.addLayout(row)
        layout.addLayout(self._ok_cancel_row(dialog))

        if dialog.exec() != QDialog.Accepted:
            return None

        parts = list(combo.checked_items())
        if custom_check.isChecked():
            custom = custom_edit.text().strip()
            if custom:
                parts.extend(
                    normalize_custom_value(p.strip(), presets)
                    for p in custom.split("_") if p.strip()
                )
        seen, ordered = set(), []
        for p in parts:
            key = p.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(p)
        return "_".join(ordered)

    @staticmethod
    def _ok_cancel_row(dialog):
        row = QHBoxLayout()
        row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dialog.reject)
        row.addWidget(cancel_btn)
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(dialog.accept)
        row.addWidget(ok_btn)
        return row

    def _apply_bulk_field(self, book_ids, db_setter, value, clear_selection_after):
        db_setter(book_ids, value)
        for book_id in book_ids:
            sync_filename(self.db, book_id)
        if clear_selection_after:
            self.clear_selection()
        self.refresh_list()

    def _delete_books_from_disk(self, book_ids):
        """Permanently deletes the given books' PDF files from disk, then
        removes their library entries -- but only once a file is confirmed
        gone (deleted just now, or already missing); if deletion fails
        (permissions, a locked file, etc.) that book's entry is left
        completely untouched rather than orphaning it for a file that's
        still sitting on disk unprotected. No confirmation dialog here --
        every caller is expected to confirm with the user itself, since
        the right wording differs by context.

        Returns (deleted_ids, failed) where failed is a list of
        (title, error_message) tuples for anything that couldn't be
        removed."""
        deleted, failed = [], []
        for book_id in book_ids:
            book = self.db.get_book(book_id)
            if not book:
                continue  # already gone somehow -- nothing left to do
            try:
                if os.path.exists(book["filepath"]):
                    os.remove(book["filepath"])
            except OSError as exc:
                failed.append((book["title"], str(exc)))
                continue
            self.db.remove_book(book_id)
            delete_thumbnail(book_id)
            self._selected_book_ids.discard(book_id)
            deleted.append(book_id)
        return deleted, failed

    def _confirm_delete_dialog(self, books):
        """A confirmation dialog listing exactly which book(s) are about to
        be permanently deleted -- not just a count -- so there's no doubt
        about what you're confirming before it happens. Returns True only
        if "Yes" was actually clicked; "No" is the keyboard-default (Enter
        triggers it, not the destructive action), since this can't be
        undone and a stray Enter shouldn't be the thing that deletes
        something."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Delete from Disk")
        dialog.resize(420, 320)
        layout = QVBoxLayout(dialog)

        n = len(books)
        label = QLabel(
            f"Permanently delete the following {n} book{'s' if n != 1 else ''} "
            f"from disk?\n\nThis cannot be undone."
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        list_widget = QListWidget()
        list_widget.setSelectionMode(QAbstractItemView.NoSelection)
        for book in books:
            list_widget.addItem(book["title"])
        layout.addWidget(list_widget)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        no_btn = QPushButton("No")
        no_btn.setDefault(True)
        no_btn.clicked.connect(dialog.reject)
        btn_row.addWidget(no_btn)
        yes_btn = QPushButton("Yes")
        yes_btn.clicked.connect(dialog.accept)
        btn_row.addWidget(yes_btn)
        layout.addLayout(btn_row)

        return dialog.exec() == QDialog.Accepted

    def delete_book_from_disk(self, book_id):
        book = self.db.get_book(book_id)
        if not book:
            return
        if not self._confirm_delete_dialog([book]):
            return
        deleted, failed = self._delete_books_from_disk([book_id])
        self._update_selection_indicator()
        # Deferred: reachable from the book's own right-click menu, whose
        # event Qt may still be dispatching on this widget.
        QTimer.singleShot(0, self.refresh_list)
        QTimer.singleShot(0, self.refresh_categories_sidebar)
        if failed:
            title, err = failed[0]
            QMessageBox.warning(
                self, "Couldn't delete file",
                f"\u201c{title}\u201d was left untouched in your library since "
                f"its file couldn't be deleted:\n\n{err}",
            )

    def _bulk_delete_books_from_disk(self, book_ids):
        book_ids = list(book_ids)
        books = [b for b in (self.db.get_book(bid) for bid in book_ids) if b]
        if not books:
            return
        if not self._confirm_delete_dialog(books):
            return
        deleted, failed = self._delete_books_from_disk(book_ids)
        self._update_selection_indicator()
        # Deferred: this can be triggered from a right-click on one of the
        # very books being deleted, whose event Qt is still dispatching.
        QTimer.singleShot(0, self.refresh_list)
        QTimer.singleShot(0, self.refresh_categories_sidebar)
        if failed:
            details = "\n".join(f"\u2022 {title}: {err}" for title, err in failed)
            QMessageBox.warning(
                self, "Some files couldn't be deleted",
                f"Deleted {len(deleted)} of {len(book_ids)} book(s). The rest "
                f"were left untouched in your library since their files "
                f"couldn't be deleted:\n\n{details}",
            )

    def _bulk_remove_books(self, book_ids):
        reply = QMessageBox.question(
            self,
            "Remove books",
            f"Remove {len(book_ids)} book(s) from your library? "
            f"The files themselves won't be deleted.",
        )
        if reply == QMessageBox.Yes:
            for book_id in book_ids:
                self.db.remove_book(book_id)
                delete_thumbnail(book_id)
                self._selected_book_ids.discard(book_id)
            self._update_selection_indicator()
            # Deferred: this can be triggered from a right-click on one of the
            # very books being removed, whose event Qt is still dispatching.
            QTimer.singleShot(0, self.refresh_list)
            QTimer.singleShot(0, self.refresh_categories_sidebar)

    # ------------- Actions -------------
    def choose_library_folder(self):
        current = self.db.get_setting("library_folder") or ""
        msg = QMessageBox(self)
        msg.setWindowTitle("Library Folder")
        if current:
            msg.setText(
                "New books added via \u201cAdd Book(s)\u201d or \u201cAdd Folder\u201d "
                f"are moved into:\n\n{current}"
            )
        else:
            msg.setText(
                "No library folder is set, so new books stay wherever you "
                "originally select them from.\n\nSet one to have \u201cAdd "
                "Book(s)\u201d and \u201cAdd Folder\u201d automatically move new "
                "books into a single folder."
            )
        change_btn = msg.addButton("Change...", QMessageBox.ActionRole)
        clear_btn = msg.addButton("Clear", QMessageBox.DestructiveRole) if current else None
        msg.addButton(QMessageBox.Close)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked is change_btn:
            folder = QFileDialog.getExistingDirectory(
                self, "Select library folder", current or os.path.expanduser("~")
            )
            if folder:
                self.db.set_setting("library_folder", os.path.abspath(folder))
                added = self.refresh_library()
                if added:
                    QMessageBox.information(
                        self, "Library folder updated",
                        f"Found and added {added} book(s) already in this folder.",
                    )
        elif clear_btn is not None and clicked is clear_btn:
            self.db.set_setting("library_folder", "")
            self.refresh_library()

    def add_books(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select PDF files", os.path.expanduser("~"), "PDF files (*.pdf)"
        )
        for path in paths:
            self._import_pdf(self._move_into_library_folder(path))
        self._sync_duplicate_books()
        self.refresh_list()

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if not folder:
            return
        count = 0
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith(".pdf"):
                    path = self._move_into_library_folder(os.path.join(root, f))
                    self._import_pdf(path)
                    count += 1
        self._sync_duplicate_books()
        self.refresh_list()
        QMessageBox.information(self, "Import complete", f"Added {count} PDF file(s).")

    def _move_into_library_folder(self, path):
        """If a library folder is configured, move `path` into it (flat --
        no subfolders preserved) and return the new location. Returns the
        original path unchanged if no library folder is set, the file is
        already directly inside it, or the move can't be completed for any
        reason (so a book is never lost, just left where it was)."""
        folder = self.db.get_setting("library_folder")
        if not folder or not os.path.isdir(folder):
            return path
        abs_path = os.path.abspath(path)
        abs_folder = os.path.abspath(folder)
        if os.path.dirname(abs_path) == abs_folder:
            return abs_path  # already there

        dest = os.path.join(abs_folder, os.path.basename(abs_path))
        dest = self._unique_destination(dest)
        try:
            shutil.move(abs_path, dest)
            return dest
        except OSError:
            return abs_path

    @staticmethod
    def _unique_destination(dest):
        """Appends ' (1)', ' (2)', etc. before the extension if something
        already occupies `dest`, so an unrelated same-named file already in
        the library folder is never silently overwritten."""
        if not os.path.exists(dest):
            return dest
        base, ext = os.path.splitext(dest)
        n = 1
        while True:
            candidate = f"{base} ({n}){ext}"
            if not os.path.exists(candidate):
                return candidate
            n += 1

    def _import_pdf(self, path):
        abs_path = os.path.abspath(path)
        is_new_book = self.db.get_book_by_path(abs_path) is None

        # Title (and Author/Series/Genre, when present) come from the filename
        # itself -- e.g. "Dune * Frank Herbert * Dune Saga * Sci-Fi.pdf" --
        # rather than the PDF's own internal metadata, which often reflects
        # whatever a document's first heading happened to be, not the actual
        # book title.
        parsed = parse_filename(os.path.basename(path))

        page_count = 0
        try:
            doc = fitz.open(path)
            page_count = doc.page_count
            doc.close()
        except Exception:
            pass  # page_count stays 0; title still comes from the filename

        # A content hash lets duplicate detection catch the exact same file
        # re-imported under a different name or from a different source --
        # only worth computing for a genuinely new import (see
        # _sync_duplicate_books()); an already-tracked path's hash, if any,
        # is left exactly as it was.
        file_hash = compute_file_hash(abs_path) if is_new_book else ""
        book = self.db.add_book(abs_path, parsed["title"], page_count, file_hash)
        if book and is_new_book and (
            parsed["author"] or parsed["series"] or parsed["genre"] or parsed["language"]
            or parsed["series_number"] is not None
        ):
            # Only backfill these for a genuinely new import -- never overwrite
            # metadata someone already edited by hand on a book already in the library.
            self.db.update_metadata(
                book["id"],
                author=parsed["author"],
                series=parsed["series"],
                genre=parsed["genre"],
                language=parsed["language"],
            )
            if parsed["series_number"] is not None:
                # Its own call, not folded into update_metadata() above --
                # see set_series_number()'s docstring for why.
                self.db.set_series_number(book["id"], parsed["series_number"])

    def set_favorites_filter(self, favorites_only):
        self.show_favorites_only = favorites_only
        self.all_btn.setChecked(not favorites_only)
        self.fav_btn.setChecked(favorites_only)
        self.library_page = 1
        self.refresh_list()

    def toggle_theme(self, checked):
        self.theme_btn.setChecked(checked)
        theme = "dark" if checked else "light"
        self.db.set_setting("theme", theme)
        self._apply_theme(theme)

    def _apply_theme(self, theme):
        from PySide6.QtWidgets import QApplication

        QApplication.instance().setStyleSheet(DARK_THEME if theme == "dark" else LIGHT_THEME)
        self.theme_btn.setChecked(theme == "dark")

    # ------------- Library sync (missing/renamed files) -------------
    def refresh_library(self, show_feedback=True):
        """Re-check the library against what's on disk -- run automatically
        on startup, and on demand via the Refresh button or F5. Picks up any
        PDFs sitting in the configured library folder that aren't tracked
        yet, catches files that were renamed or deleted outside the app, and
        refreshes category counts and the book list to match. Returns the
        number of newly-discovered books added during this refresh."""
        added = self._scan_library_folder()
        self._sync_missing_files()
        self._sync_duplicate_books()
        self.refresh_categories_sidebar()
        self.refresh_list()
        if show_feedback:
            self._flash_refresh_feedback()
        return added

    def _scan_library_folder(self):
        """If a library folder is configured, pick up any PDFs sitting
        inside it (directly, or in a subfolder) that the library isn't
        already tracking -- e.g. files the user dropped in there outside
        the app, or that were already present when the folder was first
        set as the library folder. Already-tracked files are left alone
        entirely (no move, no re-import) so this is a cheap no-op on a
        folder where nothing's changed, since it runs on every refresh.
        Files found in a subfolder get flattened to the top level, same as
        "Add Folder" does. Returns the count of newly-added books."""
        folder = self.db.get_setting("library_folder")
        if not folder or not os.path.isdir(folder):
            return 0
        count = 0
        for root, _, files in os.walk(folder):
            for f in files:
                if not f.lower().endswith(".pdf"):
                    continue
                path = os.path.join(root, f)
                if self.db.get_book_by_path(os.path.abspath(path)) is not None:
                    continue  # already tracked -- nothing to do
                path = self._move_into_library_folder(path)
                self._import_pdf(path)
                count += 1
        return count

    def _flash_refresh_feedback(self):
        """A simple, temporary visual cue on the Refresh button itself so a
        click that finds nothing new still visibly confirms it worked."""
        self.refresh_action.setText("\u2713 Refreshed")
        QTimer.singleShot(1200, lambda: self.refresh_action.setText("Refresh"))

    def _sync_missing_files(self):
        """A book is flagged -- hidden from the main list, surfaced via the
        missing-files indicator -- if its file is outright gone, OR if it
        still exists but doesn't belong to the currently configured library
        folder. That "doesn't belong" check only applies once the library
        folder feature has actually been engaged with -- either a real
        folder is set (flag anything outside it), or it was explicitly
        cleared via Library Folder > Clear (flag everything, the same way
        switching to a different folder would flag whatever isn't in it).
        A library folder that's simply never been touched at all flags
        nothing, so the feature has zero effect until someone opts in.
        get_setting returns None only in that last, untouched case -- once
        Clear has been used it returns "" instead, which is what tells the
        two apart. The two flagged cases get different resolutions (move
        vs. remove), so they're tracked separately even though both feed
        into the same _missing_book_ids the rest of the UI checks."""
        library_folder = self.db.get_setting("library_folder")
        feature_engaged = library_folder is not None
        abs_library_folder = (
            os.path.abspath(library_folder)
            if library_folder and os.path.isdir(library_folder) else None
        )

        gone = set()
        outside_folder = set()
        for book in self.db.get_books():
            filepath = book["filepath"]
            if not os.path.exists(filepath):
                gone.add(book["id"])
            elif feature_engaged and os.path.dirname(os.path.abspath(filepath)) != abs_library_folder:
                outside_folder.add(book["id"])

        self._truly_missing_book_ids = gone
        self._relocatable_book_ids = outside_folder
        self._missing_book_ids = gone | outside_folder
        self._update_missing_files_indicator()

    def _update_missing_files_indicator(self):
        n = len(self._missing_book_ids)
        self.missing_files_btn.setVisible(n > 0)
        if n > 0:
            self.missing_files_btn.setText(
                f"\u26a0 {n} book{'s' if n != 1 else ''} need attention "
                f"(missing, or outside your library folder) \u2014 click for details"
            )

    def _show_missing_files_dialog(self):
        books = [b for b in (self.db.get_book(bid) for bid in self._missing_book_ids) if b]
        if not books:
            return
        books.sort(key=lambda b: b["title"].lower())

        dialog = QDialog(self)
        dialog.setWindowTitle("Books Needing Attention")
        dialog.resize(520, 380)
        layout = QVBoxLayout(dialog)

        hint = QLabel(
            "These books are hidden from your library until resolved. Some "
            "weren't found on disk at all \u2014 likely renamed or moved "
            "outside the app, or on a drive that isn't connected right now. "
            "Others still exist but sit outside your configured library "
            "folder.\n\nSelect entries below and click \u201cClear "
            "Selected\u201d to remove just those, or \u201cClear All\u201d for "
            "everything in this list."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        list_widget = QListWidget()
        list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        for book in books:
            tag = "missing" if book["id"] in self._truly_missing_book_ids else "outside library folder"
            item = QListWidgetItem(f"{book['title']} \u2014 [{tag}] {book['filepath']}")
            item.setData(Qt.UserRole, book["id"])
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        btn_row.addWidget(close_btn)

        clear_selected_btn = QPushButton("Clear Selected")
        clear_selected_btn.setToolTip("Remove just the selected book(s) from your library")
        clear_selected_btn.clicked.connect(
            lambda: self._clear_selected_missing_books(dialog, list_widget)
        )
        btn_row.addWidget(clear_selected_btn)

        all_ids = [b["id"] for b in books]
        clear_all_btn = QPushButton(f"Clear All {len(all_ids)}")
        clear_all_btn.setToolTip("Remove every book in this list from your library")
        clear_all_btn.clicked.connect(lambda: self._clear_all_missing_books(dialog, all_ids))
        btn_row.addWidget(clear_all_btn)

        relocatable_ids = [b["id"] for b in books if b["id"] in self._relocatable_book_ids]
        library_folder = self.db.get_setting("library_folder")
        if relocatable_ids and library_folder and os.path.isdir(library_folder):
            # Only offer this when there's an actual folder to move into --
            # after Library Folder > Clear, every book counts as
            # "relocatable" (nothing to compare it to), but there's nowhere
            # to move it, so the button would just silently do nothing.
            move_btn = QPushButton(f"Move {len(relocatable_ids)} Into Library Folder")
            move_btn.clicked.connect(lambda: self._move_missing_books(dialog, relocatable_ids))
            btn_row.addWidget(move_btn)

        missing_ids = [b["id"] for b in books if b["id"] in self._truly_missing_book_ids]
        if missing_ids:
            remove_btn = QPushButton(f"Remove All {len(missing_ids)} Missing")
            remove_btn.clicked.connect(lambda: self._remove_missing_books(dialog, missing_ids))
            btn_row.addWidget(remove_btn)

        layout.addLayout(btn_row)
        dialog.exec()

    def _clear_selected_missing_books(self, dialog, list_widget):
        selected_items = list_widget.selectedItems()
        if not selected_items:
            QMessageBox.information(
                self, "Nothing selected", "Select one or more books in the list first."
            )
            return
        book_ids = [item.data(Qt.UserRole) for item in selected_items]
        self._clear_missing_books(
            dialog, book_ids,
            title="Clear selected books",
            prompt=(
                f"Remove {len(book_ids)} selected book(s) from your library? "
                f"This only removes their library entries (bookmarks, categories, "
                f"metadata) \u2014 if a file still exists on disk it's left alone, "
                f"just no longer tracked here."
            ),
        )

    def _clear_all_missing_books(self, dialog, book_ids):
        self._clear_missing_books(
            dialog, book_ids,
            title="Clear all books",
            prompt=(
                f"Remove all {len(book_ids)} book(s) in this list from your library "
                f"\u2014 both missing ones and ones outside your library folder? "
                f"This only removes their library entries (bookmarks, categories, "
                f"metadata) \u2014 any files that still exist on disk are left "
                f"alone, just no longer tracked here."
            ),
        )

    def _clear_missing_books(self, dialog, book_ids, title, prompt):
        if not book_ids:
            return
        reply = QMessageBox.question(self, title, prompt)
        if reply != QMessageBox.Yes:
            return

        for book_id in book_ids:
            self.db.remove_book(book_id)
            delete_thumbnail(book_id)
        dialog.close()
        self.refresh_library(show_feedback=False)

    def _move_missing_books(self, dialog, book_ids):
        moved = 0
        for book_id in book_ids:
            book = self.db.get_book(book_id)
            if not book or not os.path.exists(book["filepath"]):
                continue  # disappeared between opening the dialog and clicking Move
            new_path = self._move_into_library_folder(book["filepath"])
            if os.path.abspath(new_path) != os.path.abspath(book["filepath"]):
                self.db.update_filepath(book_id, new_path)
                moved += 1
        dialog.close()
        self.refresh_library(show_feedback=False)
        QMessageBox.information(
            self, "Move complete", f"Moved {moved} book(s) into your library folder."
        )

    def _remove_missing_books(self, dialog, book_ids):
        reply = QMessageBox.question(
            self,
            "Remove missing books",
            f"Permanently remove {len(book_ids)} book(s) from your library? "
            f"This only removes their library entries (bookmarks, categories, "
            f"metadata) \u2014 there's no file to delete, since they're already gone.",
        )
        if reply == QMessageBox.Yes:
            for book_id in book_ids:
                self.db.remove_book(book_id)
                delete_thumbnail(book_id)
            dialog.close()
            self.refresh_library(show_feedback=False)

    # ------------- Duplicate detection -------------
    def _sync_duplicate_books(self):
        """Flag books that look like duplicates of each other. An identical
        file (matching content hash) is the strongest signal; a matching
        Title+Author is a weaker, free-to-check fallback that also catches
        the same book re-imported under a different filename or from a
        different source, before either copy has ever been hashed. Runs on
        every refresh and right after Add Book(s)/Add Folder, same as the
        missing-files check, but -- unlike that hash computation itself --
        never touches the filesystem here: hashing a whole library on every
        refresh would be far too expensive for a large one, so an existing
        book's hash is only ever computed at import time (_import_pdf) or
        during an explicit "Scan Entire Library..." pass from the dialog
        below."""
        self._duplicate_groups = find_duplicate_groups(self.db.get_books())
        self._update_duplicates_indicator()

    def _update_duplicates_indicator(self):
        n = len(self._duplicate_groups)
        self.duplicates_btn.setVisible(n > 0)
        if n > 0:
            total_books = sum(len(g["book_ids"]) for g in self._duplicate_groups)
            self.duplicates_btn.setText(
                f"\u26a0 {n} possible duplicate group{'s' if n != 1 else ''} "
                f"({total_books} books) \u2014 click for details"
            )

    def _show_duplicates_dialog(self):
        if not self._duplicate_groups:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Possible Duplicate Books")
        dialog.resize(560, 420)
        layout = QVBoxLayout(dialog)

        hint = QLabel(
            "These look like duplicates \u2014 either byte-identical files "
            "(the strongest signal) or a matching Title & Author (a weaker "
            "one, since two different editions or scans can share both). "
            "Nothing happens automatically: review each group and select "
            "whichever copy(ies) you don't want to keep.\n\n"
            "\u201cRemove Selected From Library\u201d only detaches the "
            "library entry -- the file stays on disk untouched, so if it "
            "lives inside your watched Library Folder, the next Refresh "
            "will just re-import it. \u201cDelete Selected From Disk\u201d "
            "actually deletes the file too, which is what you want in "
            "that case -- but it's permanent."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        list_widget = QListWidget()
        list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._populate_duplicates_list(list_widget)
        layout.addWidget(list_widget)

        btn_row = QHBoxLayout()
        scan_btn = QPushButton("Scan Entire Library for Exact Duplicates...")
        scan_btn.setToolTip(
            "Compute a content hash for every book that doesn't have one yet "
            "-- can take a while on a large library -- to catch byte-identical "
            "files that a Title/Author match alone would miss, including "
            "books added before this feature existed"
        )
        scan_btn.clicked.connect(lambda: self._run_duplicate_hash_scan(dialog, list_widget))
        btn_row.addWidget(scan_btn)
        btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.close)
        btn_row.addWidget(close_btn)

        remove_btn = QPushButton("Remove Selected From Library")
        remove_btn.setToolTip(
            "Detach just the selected book(s) from your library -- the file "
            "itself is left on disk. If it's inside your watched Library "
            "Folder, the next Refresh will re-import it -- use \u201cDelete "
            "Selected From Disk\u201d instead if that's not what you want."
        )
        remove_btn.clicked.connect(lambda: self._remove_selected_duplicates(dialog, list_widget))
        btn_row.addWidget(remove_btn)

        delete_btn = QPushButton("Delete Selected From Disk")
        delete_btn.setStyleSheet("color: #b91c1c;")
        delete_btn.setToolTip(
            "Permanently delete the selected book(s)' PDF file(s) from disk, "
            "and remove them from your library -- this is what actually "
            "stops a duplicate living in your Library Folder from coming "
            "back on the next Refresh. Cannot be undone."
        )
        delete_btn.clicked.connect(lambda: self._delete_selected_duplicates(dialog, list_widget))
        btn_row.addWidget(delete_btn)

        layout.addLayout(btn_row)
        dialog.exec()

    def _populate_duplicates_list(self, list_widget):
        """(Re)build the group/book listing from self._duplicate_groups.
        A group header is its own, non-selectable list item -- only the
        book rows underneath it carry a book_id and can be selected."""
        list_widget.clear()
        for group in self._duplicate_groups:
            books = [b for b in (self.db.get_book(bid) for bid in group["book_ids"]) if b]
            if len(books) < 2:
                continue  # one of them was removed from under us since the last sync
            label = "Exact file match" if group["match_type"] == "hash" else "Same Title & Author"

            header = QListWidgetItem(f"\u2014 {label} \u2014")
            header.setFlags(Qt.ItemIsEnabled)  # visible, but not selectable
            header.setForeground(Qt.gray)
            list_widget.addItem(header)

            for book in sorted(books, key=lambda b: b["title"].lower()):
                try:
                    size = human_size(os.path.getsize(book["filepath"]))
                except OSError:
                    size = "file missing"
                item = QListWidgetItem(f"    {book['title']} \u2014 {size} \u2014 {book['filepath']}")
                item.setData(Qt.UserRole, book["id"])
                list_widget.addItem(item)

    def _remove_selected_duplicates(self, dialog, list_widget):
        book_ids = [
            item.data(Qt.UserRole)
            for item in list_widget.selectedItems()
            if item.data(Qt.UserRole) is not None
        ]
        if not book_ids:
            QMessageBox.information(
                self, "Nothing selected", "Select one or more books in the list first."
            )
            return
        reply = QMessageBox.question(
            self,
            "Remove selected duplicates",
            f"Remove {len(book_ids)} selected book(s) from your library? "
            f"This only removes their library entries (bookmarks, categories, "
            f"highlights, drawings, reading status) \u2014 the files themselves are left "
            f"on disk, untouched.",
        )
        if reply != QMessageBox.Yes:
            return
        for book_id in book_ids:
            self.db.remove_book(book_id)
            delete_thumbnail(book_id)
        dialog.close()
        self.refresh_library(show_feedback=False)

    def _delete_selected_duplicates(self, dialog, list_widget):
        """Permanently delete the selected book(s)' files from disk, then
        remove their library entries -- unlike "Remove Selected From
        Library", this is what actually stops a duplicate from coming
        right back: a book living inside the watched Library Folder gets
        silently re-imported as "new" on the very next Refresh if only its
        library entry (and not the file itself) is removed.

        The library entry is only ever removed once the file is
        confirmed gone (deleted just now, or already missing) -- if
        deletion fails (permissions, a locked file, etc.), that book is
        left completely untouched rather than orphaning a library entry
        for a file that's still sitting on disk unprotected."""
        book_ids = [
            item.data(Qt.UserRole)
            for item in list_widget.selectedItems()
            if item.data(Qt.UserRole) is not None
        ]
        if not book_ids:
            QMessageBox.information(
                self, "Nothing selected", "Select one or more books in the list first."
            )
            return

        reply = self._confirm_delete_dialog(
            [b for b in (self.db.get_book(bid) for bid in book_ids) if b]
        )
        if not reply:
            return

        deleted, failed = self._delete_books_from_disk(book_ids)

        dialog.close()
        self.refresh_library(show_feedback=False)

        if failed:
            details = "\n".join(f"\u2022 {title}: {err}" for title, err in failed)
            QMessageBox.warning(
                self,
                "Some files couldn't be deleted",
                f"Deleted {len(deleted)} of {len(book_ids)} book(s). The rest "
                f"were left untouched in your library since their files "
                f"couldn't be deleted:\n\n{details}",
            )

    def _run_duplicate_hash_scan(self, dialog, list_widget):
        """Compute a content hash for every book that doesn't already have
        one, then re-check for duplicates with that fuller picture. This is
        the only place a whole library's worth of files ever gets hashed at
        once -- an explicit, on-demand action (with its own progress and
        Cancel), rather than something that happens silently on every
        refresh."""
        books = [b for b in self.db.get_books() if not b.get("file_hash")]
        if not books:
            QMessageBox.information(
                self, "Nothing to scan",
                "Every book already has a content hash on file \u2014 nothing left to compute.",
            )
            return

        progress = QProgressDialog("Scanning for exact duplicates...", "Cancel", 0, len(books), self)
        progress.setWindowTitle("Scanning Library")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        QApplication.processEvents()

        for i, book in enumerate(books):
            if progress.wasCanceled():
                break
            progress.setLabelText(f"Hashing {i + 1} of {len(books)}: {book['title']}")
            progress.setValue(i)
            QApplication.processEvents()
            file_hash = compute_file_hash(book["filepath"])
            if file_hash:
                self.db.update_file_hash(book["id"], file_hash)
        progress.close()

        self._sync_duplicate_books()
        if self._duplicate_groups:
            self._populate_duplicates_list(list_widget)
        else:
            dialog.close()
            QMessageBox.information(self, "No duplicates found", "No duplicate books were found.")

    def refresh_list(self):
        # Preserve scroll position across the rebuild below -- otherwise
        # selecting a book, toggling a favorite, or any other small action
        # would silently jump you back to the top of a long list.
        list_scroll = self.list_widget.verticalScrollBar().value()
        grid_scroll = self.grid_scroll.verticalScrollBar().value()

        sort_by, descending = SORT_OPTIONS[self.sort_combo.currentIndex()]
        search = self.search_box.text().strip() or None
        status_filter = self.status_filter_combo.currentData()

        # The A-Z index (only meaningful under Title sort) and the
        # Genre/Language filter bar are shared across both view modes, so
        # they behave identically whichever one you're browsing in.
        self.alpha_bar_top.setVisible(sort_by == "title")
        self.genre_lang_bar.setVisible(self.genre_lang_filter_mode)

        all_books = self.db.get_books(
            favorites_only=self.show_favorites_only,
            search=search,
            sort_by=sort_by,
            descending=descending,
            status=status_filter,
            category_id=self.selected_category_id,
            genres=list(self.selected_genres) if self.selected_genres else None,
            languages=list(self.selected_languages) if self.selected_languages else None,
            series=list(self.selected_series) if self.selected_series else None,
        )

        # Books whose file wasn't found at the last sync (startup or a
        # Refresh/F5) are kept in the database -- in case the file was just
        # temporarily unavailable (e.g. an external drive) -- but hidden from
        # the normal view so a stale, broken entry doesn't show up as if
        # nothing's wrong.
        if self._missing_book_ids:
            all_books = [b for b in all_books if b["id"] not in self._missing_book_ids]

        if self.genre_lang_filter_mode:
            self._refresh_genre_lang_bar_contents()

        # Pagination now applies to every sort mode, including Title -- large
        # libraries could otherwise render everything at once and hurt
        # performance/memory, even when sorted alphabetically.
        per_page = self._get_per_page()
        self._letter_page_map = (
            self._compute_letter_page_map(all_books, per_page) if sort_by == "title" else {}
        )

        if per_page:
            total_books = len(all_books)
            total_pages = max(1, (total_books + per_page - 1) // per_page)
            self.library_page = min(max(self.library_page, 1), total_pages)
            start = (self.library_page - 1) * per_page
            books = all_books[start:start + per_page]
            self._update_pagination_controls(total_pages, total_books)
            self.pagination_widget.setVisible(total_pages > 1)
        else:
            books = all_books
            self.pagination_widget.hide()

        # Visible whenever we're in that view mode, regardless of whether the
        # current filters happen to match zero books -- the grid container
        # also holds the A-Z / Genre-Language filter bars, and hiding it on
        # zero results would hide those controls too, making a "no matches"
        # filter impossible to see or adjust.
        if len(books) == 0:
            if self.db.get_books():
                self.empty_label.setText("No books match the current filters.")
            else:
                self.empty_label.setText(
                    'No books yet. Click "Add Book(s)" or "Add Folder" to build your library.'
                )
            self.empty_label.show()
        else:
            self.empty_label.hide()
        self.list_widget.setVisible(self.view_mode == "list")
        self.grid_container.setVisible(self.view_mode == "grid")

        if self.view_mode == "grid":
            self._render_grid(books, sort_by)
        else:
            self._render_list(books, sort_by)

        # Synchronous, not deferred: both QListWidget and the grid's
        # scrollbar range are already correct immediately after rebuilding,
        # and restoring right away (same paint cycle) avoids a visible
        # flicker where the list would otherwise flash at the top for a
        # frame before jumping back down to the restored position.
        self._restore_scroll_position(list_scroll, grid_scroll)

    def _restore_scroll_position(self, list_scroll, grid_scroll):
        self.list_widget.verticalScrollBar().setValue(list_scroll)
        self.grid_scroll.verticalScrollBar().setValue(grid_scroll)

    def _compute_letter_page_map(self, books, per_page):
        """For alphabetical sort: which page (1-indexed) each letter's first
        matching book falls on, given the current page size -- lets the A-Z
        bar jump across pages. With no pagination ("All"), everything is
        effectively page 1."""
        mapping = {}
        for i, book in enumerate(books):
            letter = _group_letter(book["title"])
            if letter not in mapping:
                mapping[letter] = (i // per_page) + 1 if per_page else 1
        return mapping

    def _reset_page_and_refresh(self):
        self.library_page = 1
        self.refresh_list()

    def _get_per_page(self):
        text = self.per_page_combo.currentText()
        return None if text == "All" else int(text)

    def _update_pagination_controls(self, total_pages, total_books):
        self.prev_page_btn.setEnabled(self.library_page > 1)
        self.next_page_btn.setEnabled(self.library_page < total_pages)
        self.page_indicator_label.setText(
            f"Page {self.library_page} of {total_pages} ({total_books} books)"
        )

    def _go_to_prev_page(self):
        if self.library_page > 1:
            self.library_page -= 1
            self.refresh_list()

    def _go_to_next_page(self):
        self.library_page += 1  # refresh_list() clamps this to the valid range
        self.refresh_list()

    # ------------- Search suggestions preview -------------
    def _update_search_suggestions(self, text):
        text = text.strip()
        self._clear_suggestion_layout()
        if not text:
            self.suggestion_panel.hide()
            return

        results = self.db.search_suggestions(text, limit=5)
        if not (results["titles"] or results["authors"] or results["series"] or results["genres"]):
            self.suggestion_panel.hide()
            return

        if results["titles"]:
            self._add_suggestion_header("Titles")
            for row in results["titles"]:
                self._add_suggestion_row(row["title"], row["title"])
        if results["authors"]:
            self._add_suggestion_header("Authors")
            for row in results["authors"]:
                label = f"{row['name']} ({row['count']} book{'s' if row['count'] != 1 else ''})"
                self._add_suggestion_row(label, row["name"])
        if results["series"]:
            self._add_suggestion_header("Series")
            for row in results["series"]:
                label = f"{row['name']} ({row['count']} book{'s' if row['count'] != 1 else ''})"
                self._add_suggestion_row(label, row["name"], on_click=self._apply_series_suggestion)
        if results["genres"]:
            self._add_suggestion_header("Genres")
            for row in results["genres"]:
                label = f"{row['name']} ({row['count']} book{'s' if row['count'] != 1 else ''})"
                self._add_suggestion_row(label, row["name"])

        self.suggestion_panel.show()

    def _clear_suggestion_layout(self):
        while self.suggestion_layout.count():
            item = self.suggestion_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()  # deleteLater() is deferred; hide it now so it can't linger visually
                w.deleteLater()

    def _add_suggestion_header(self, text):
        label = QLabel(text)
        label.setStyleSheet("font-weight: bold; color: #888; font-size: 11px; padding-top: 4px;")
        self.suggestion_layout.addWidget(label)

    def _add_suggestion_row(self, label_text, filter_value, on_click=None):
        on_click = on_click or self._apply_suggestion
        btn = QPushButton(label_text)
        btn.setFlat(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet("text-align: left; padding: 2px 8px; border: none;")
        btn.clicked.connect(lambda: on_click(filter_value))
        self.suggestion_layout.addWidget(btn)

    def _apply_suggestion(self, value):
        self.search_box.setText(value)  # triggers refresh_list + _update_search_suggestions
        self.suggestion_panel.hide()    # then collapse the preview -- selection made

    def _apply_series_suggestion(self, value):
        """Same as _apply_suggestion(), but also switches the sort mode to
        "Series (Reading Order)" -- clicking a Series in the search
        preview means "show me this series", and reading order is
        virtually always what you want once you're looking at just one
        series. Both widgets' change signals are blocked and a single
        manual refresh is triggered instead, so this doesn't cause two
        redundant refreshes back to back."""
        self.search_box.blockSignals(True)
        self.search_box.setText(value)
        self.search_box.blockSignals(False)
        self.sort_combo.blockSignals(True)
        self.sort_combo.setCurrentIndex(SERIES_ORDER_SORT_INDEX)
        self.sort_combo.blockSignals(False)
        self.suggestion_panel.hide()
        self._reset_page_and_refresh()

    # ------------- Simple Text (list) view -------------
    def _render_list(self, books, sort_by):
        self.list_widget.clear()
        self._list_letter_headers = {}
        is_alpha_sort = sort_by == "title"

        def add_book_item(book):
            item = QListWidgetItem()
            card = BookCard(
                book,
                selected=book["id"] in self._selected_book_ids,
                select_mode=self.select_mode,
            )
            card.open_requested.connect(self.open_book)
            card.favorite_toggled.connect(self.toggle_favorite)
            card.remove_requested.connect(self.remove_book)
            card.details_requested.connect(self.open_book_details)
            card.selection_toggled.connect(self.toggle_book_selection)
            card.range_select_requested.connect(self.handle_range_select)
            card.context_menu_requested.connect(self.show_book_context_menu)
            item.setSizeHint(card.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, card)

        if is_alpha_sort and books:
            # Same grouped-header idea as Image Preview's A-Z groups, so the
            # alpha bar's jump-to-letter behaves identically in both views.
            self._update_alpha_bars(set(self._letter_page_map.keys()))
            groups = OrderedDict()
            for book in books:
                groups.setdefault(_group_letter(book["title"]), []).append(book)
            for letter, group_books in groups.items():
                header_item = QListWidgetItem(letter)
                header_item.setFlags(Qt.NoItemFlags)  # a label, not a selectable/clickable row
                header_font = header_item.font()
                header_font.setBold(True)
                header_font.setPointSize(header_font.pointSize() + 1)
                header_item.setFont(header_font)
                self.list_widget.addItem(header_item)
                self._list_letter_headers[letter] = header_item
                for book in group_books:
                    add_book_item(book)
        else:
            for book in books:
                add_book_item(book)

    # ------------- Image Preview (grid) view -------------
    def _render_grid(self, books, sort_by):
        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(6)

        self._letter_headers = {}
        is_alpha_sort = sort_by == "title"
        is_series_sort = sort_by == "series_order"

        if is_alpha_sort:
            # Enabled letters reflect the FULL filtered set (every page), not
            # just what's on the current page, so the bar can jump across pages.
            self._update_alpha_bars(set(self._letter_page_map.keys()))
            if books:
                groups = OrderedDict()
                for book in books:
                    groups.setdefault(_group_letter(book["title"]), []).append(book)
                for letter, group_books in groups.items():
                    header = QLabel(letter)
                    header.setStyleSheet(
                        "font-weight: bold; font-size: 15px; color: #666;"
                        "padding: 4px 2px; border-bottom: 2px solid #cfcfcf; margin-top: 6px;"
                    )
                    outer.addWidget(header)
                    outer.addWidget(self._build_cover_group(group_books))
                    self._letter_headers[letter] = header
        elif is_series_sort and books:
            # Same grouped-header idea as the A-Z index above, but the
            # header is a Series name instead of a letter, and there's no
            # index bar to jump with -- books already arrive in Series
            # order (grouped by _series_order_key, with no-Series books
            # last), each Series internally in Book # order, so grouping
            # consecutive same-Series books here and laying each group out
            # left-to-right (FlowLayout, via _build_cover_group) reproduces
            # reading order visually, the same way the alphabet groups do.
            groups = OrderedDict()
            for book in books:
                groups.setdefault(_group_series(book.get("series")), []).append(book)
            for series_name, group_books in groups.items():
                header = QLabel(series_name)
                header.setStyleSheet(
                    "font-weight: bold; font-size: 15px; color: #666;"
                    "padding: 4px 2px; border-bottom: 2px solid #cfcfcf; margin-top: 6px;"
                )
                outer.addWidget(header)
                outer.addWidget(self._build_cover_group(group_books))
        elif books:
            outer.addWidget(self._build_cover_group(books))

        outer.addStretch()
        self.grid_scroll.setWidget(content)

    def _update_alpha_bars(self, active_letters):
        for letter in ALPHABET_INDEX:
            self._alpha_buttons_top[letter].setEnabled(letter in active_letters)

    def _jump_to_letter(self, letter):
        headers = self._list_letter_headers if self.view_mode == "list" else self._letter_headers
        if letter in headers:
            # Already visible on the current page (this also correctly
            # handles a letter group that spans multiple pages: if we're on
            # a later page that still shows this letter, no need to jump
            # backward to wherever it first started).
            self._scroll_to_letter_header(letter)
            return
        target_page = self._letter_page_map.get(letter)
        if target_page is not None and target_page != self.library_page:
            # The letter is on a different page -- switch there first, then
            # scroll, deferred so the new page's layout has settled first.
            self.library_page = target_page
            self.refresh_list()
            QTimer.singleShot(0, lambda l=letter: self._scroll_to_letter_header(l))
        else:
            self._scroll_to_letter_header(letter)

    def _scroll_to_letter_header(self, letter):
        if self.view_mode == "list":
            item = self._list_letter_headers.get(letter)
            if item is not None:
                self.list_widget.scrollToItem(item, QAbstractItemView.PositionAtTop)
        else:
            header = self._letter_headers.get(letter)
            if header is not None:
                self.grid_scroll.verticalScrollBar().setValue(header.y())

    def _build_cover_group(self, books):
        group_widget = QWidget()
        flow = FlowLayout(group_widget, margin=0, hspacing=14, vspacing=14)
        for book in books:
            pixmap, is_corrupted = ensure_thumbnail(book["id"], book["filepath"])
            series_number = book.get("series_number")
            # Bottom-left Book # badge: only for a book that's actually IN a
            # series AND has a number set -- a series with no number given
            # yet, or no series at all, shows no badge.
            series_number_text = (
                format_series_number(series_number)
                if book.get("series") and series_number is not None
                else None
            )
            pixmap = decorate_thumbnail(
                pixmap, book.get("status") or "unread", bool(book.get("is_favorite")), is_corrupted,
                series_number_text=series_number_text,
            )
            cell = CoverCell(
                book,
                pixmap,
                selected=book["id"] in self._selected_book_ids,
                select_mode=self.select_mode,
            )
            cell.open_requested.connect(self.open_book)
            cell.details_requested.connect(self.open_book_details)
            cell.favorite_toggled.connect(self.toggle_favorite)
            cell.remove_requested.connect(self.remove_book)
            cell.selection_toggled.connect(self.toggle_book_selection)
            cell.range_select_requested.connect(self.handle_range_select)
            cell.context_menu_requested.connect(self.show_book_context_menu)
            flow.addWidget(cell)
        return group_widget

    def set_view_mode(self, mode):
        self.view_mode = mode
        self.db.set_setting("library_view_mode", mode)
        self.text_view_btn.setChecked(mode == "list")
        self.image_view_btn.setChecked(mode == "grid")
        self.refresh_list()

    def toggle_select_mode(self, checked):
        self.select_mode_btn.setChecked(checked)
        self.select_mode = checked
        if not checked:
            # Turning Select off should also drop whatever was selected --
            # otherwise it silently reappears (still selected) the next
            # time Select is turned back on, which is confusing.
            self._selected_book_ids.clear()
            self._last_clicked_book_id = None
        self._update_selection_indicator()
        self.refresh_list()

    def open_text_search(self):
        if self._search_dialog is None:
            self._search_dialog = TextSearchDialog(self.db, self.open_book_at_page, self)
        self._search_dialog.show()
        self._search_dialog.raise_()
        self._search_dialog.activateWindow()

    def open_book_details(self, book_id):
        if self._details_dialog is None:
            self._details_dialog = BookDetailsDialog(self.db, self)
            self._details_dialog.book_updated.connect(self.refresh_list)
            self._details_dialog.open_requested.connect(self.open_book)
        self._details_dialog.load_book(book_id)
        self._details_dialog.show()
        self._details_dialog.raise_()
        self._details_dialog.activateWindow()

    def open_book_at_page(self, book_id, page_number):
        self.open_book(book_id)
        win = self.reader_windows.get(book_id)
        if win is not None:
            win.jump_to_page(page_number + 1)
            win.raise_()
            win.activateWindow()

    def toggle_favorite(self, book_id):
        # Deferred: this is reachable from the card/cell's own fav button or
        # right-click menu, both nested within that widget's own event chain.
        self.db.toggle_favorite(book_id)
        QTimer.singleShot(0, self.refresh_list)

    def remove_book(self, book_id):
        reply = QMessageBox.question(
            self,
            "Remove book",
            "Remove this book from your library? The file itself will not be deleted.",
        )
        if reply == QMessageBox.Yes:
            self.db.remove_book(book_id)
            delete_thumbnail(book_id)
            self._selected_book_ids.discard(book_id)
            self._update_selection_indicator()
            # Deferred: reachable from the book's own right-click menu / remove
            # button, whose event Qt may still be dispatching on this widget.
            QTimer.singleShot(0, self.refresh_list)
            QTimer.singleShot(0, self.refresh_categories_sidebar)

    def open_book(self, book_id, password=None):
        book = self.db.get_book(book_id)
        if not book:
            return
        if not os.path.exists(book["filepath"]):
            QMessageBox.warning(self, "File missing", "This file could not be found on disk.")
            return

        existing = self.reader_windows.get(book_id)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return

        # Pre-check before actually opening the reader: distinguish a
        # genuinely corrupted file (can't be opened at all) from one that's
        # simply password-protected (opens fine once unlocked), and give a
        # clear message for the former instead of a cryptic failure.
        try:
            probe = fitz.open(book["filepath"])
        except Exception:
            QMessageBox.critical(
                self, "File is corrupted",
                f"\u201c{book['title']}\u201d is corrupted and cannot be opened.",
            )
            self.refresh_list()  # so the corrupted-file badge appears on its cover
            return

        needs_pass = probe.needs_pass
        if needs_pass and password:
            unlocked = probe.authenticate(password) != 0
        else:
            unlocked = not needs_pass
        probe.close()

        if needs_pass and not unlocked:
            dialog = PasswordUnlockDialog(os.path.basename(book["filepath"]), self)
            if dialog.exec() != QDialog.Accepted:
                return
            entered = dialog.password()

            verify = fitz.open(book["filepath"])
            correct = verify.authenticate(entered) != 0
            verify.close()
            if not correct:
                QMessageBox.warning(self, "Incorrect password", "That password didn't work.")
                return

            if dialog.wants_remove() or dialog.wants_change():
                new_pw = dialog.new_password() if dialog.wants_change() else None
                ok, err = strip_or_change_password(book["filepath"], entered, new_pw)
                if not ok:
                    QMessageBox.critical(self, "Couldn't update password", err)
                    # Still proceed to open it with the password that DID work.
                elif new_pw:
                    entered = new_pw  # the file now needs the NEW password, not the old one

            password = entered

        win = ReaderWindow(
            self.db, book_id, on_close=self.refresh_list, password=password,
            open_book_at_page=self.open_book_at_page,
        )
        self.reader_windows[book_id] = win
        win.show()
