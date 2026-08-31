"""Reader window: renders PDF pages, and supports simple-text mode, bookmarks,
text size / zoom, dark mode, text selection/copy, two-page view, and
favoriting a book while reading it."""
import os

import pymupdf as fitz  # PyMuPDF (module renamed from "fitz")
from PySide6.QtCore import QElapsedTimer, QEvent, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDialog,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from .database import Database
from .highlights_notes import build_highlights_notes
from .search_dialog import TextSearchDialog
from .shortcuts import (
    effective_shortcut, effective_wheel_action, load_overrides, load_wheel_overrides,
    WHEEL_ACTION_PAGE_TURN, WHEEL_ACTION_ZOOM, WHEEL_GESTURES,
)
from .text_selection import (
    char_index_at_point,
    chars_from_rawdict,
    combined_selected_text,
    paragraph_bounds_at_index,
    resolve_multi_page_selection,
    selected_text as selected_text_for_range,
    selection_rects,
    word_bounds_at_index,
)
from .themes import DARK_THEME, LIGHT_THEME

MIN_ZOOM = 0.2
MAX_ZOOM = 6.0
VIEWPORT_MARGIN = 24  # px of breathing room so a fitted page never touches the edges
PAGE_GAP = 12  # px between the two pages in Two-Page View
DEFAULT_HIGHLIGHT_COLOR = "#3878FF"
DEFAULT_DRAWING_COLOR = "#FFD400"
DRAWING_TOOLS = ("pen", "rectangle", "ellipse", "triangle", "line")
FAR_POINT = (10 ** 9, 10 ** 9)     # a page-space point past any real content -- see char_index_at_point
NEAR_POINT = (-10 ** 9, -10 ** 9)  # ditto, before any real content


class TextSelectionOverlay(QWidget):
    """A transparent overlay sitting on top of the rendered page pixmap.
    Only shown while "Select Text" mode is on -- lets you drag over the
    page and highlights exactly the text a real PDF viewer would, in
    reading order (not just whatever falls inside the drag rectangle:
    see app/text_selection.py for why that distinction matters). Layered
    over the pixel-perfect rendered image rather than replacing it, so
    visual fidelity (fonts, layout, embedded images) is unaffected.

    The overlay only tracks raw mouse positions; all the actual text
    logic (which words are selected, what rectangles to highlight) lives
    in ReaderWindow, which knows about pages, zoom, and two-page offsets
    that this widget doesn't need to care about."""

    def __init__(self, reader, parent=None):
        super().__init__(parent)
        self.reader = reader
        self.setCursor(Qt.IBeamCursor)
        self._drag_start = None
        self._drag_current = None
        self._highlight_rects = []
        self._saved_highlights = []
        self._live_color = QColor(60, 120, 255, 90)
        self._click_count = 0
        self._last_click_pos = None
        self._click_timer = QElapsedTimer()
        self._autoscroll_direction = 0
        self._autoscroll_timer = QTimer(self)
        self._autoscroll_timer.setInterval(30)
        self._autoscroll_timer.timeout.connect(self._autoscroll_step)
        self.hide()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        if self._is_rapid_repeat_click(pos):
            self._click_count += 1
        else:
            self._click_count = 1
        self._register_click(pos)

        if self._click_count >= 3:
            # Qt has no native triple-click event -- the 2nd click of any
            # rapid sequence already arrives as mouseDoubleClickEvent
            # below, so a 3rd press this close in time and position to it
            # is the triple-click.
            self.reader.select_word_or_paragraph_at(pos, paragraph=True)
            self.reader.show_selection_popup()
            self._click_count = 0  # a 4th click starts a fresh count, not "quadruple"
            return

        self.reader.selection_popup.hide()  # a fresh drag replaces whatever was selected before
        self._drag_start = pos
        self._drag_current = pos
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag_start is None:
            return
        self._drag_current = event.position()
        self.reader.update_text_selection(self._drag_start, self._drag_current, finished=False)
        self._update_autoscroll(self._drag_current)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag_start is None or event.button() != Qt.LeftButton:
            return
        start, end = self._drag_start, self._drag_current
        self._drag_start = None
        self._drag_current = None
        self._autoscroll_timer.stop()
        self._autoscroll_direction = 0
        self.reader.update_text_selection(start, end, finished=True)
        self.reader.show_selection_popup()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pos = event.position()
        self._click_count = 2
        self._register_click(pos)
        self.reader.select_word_or_paragraph_at(pos, paragraph=False)
        self.reader.show_selection_popup()

    def _is_rapid_repeat_click(self, pos):
        if self._last_click_pos is None or not self._click_timer.isValid():
            return False
        close_enough = (pos - self._last_click_pos).manhattanLength() < 6
        fast_enough = self._click_timer.elapsed() < QApplication.doubleClickInterval()
        return close_enough and fast_enough

    def _register_click(self, pos):
        self._last_click_pos = pos
        self._click_timer.restart()

    def _update_autoscroll(self, drag_pos):
        """While dragging a selection, if the mouse is near the top/bottom
        edge of the visible scroll area, keep scrolling in that direction
        for as long as it stays there -- otherwise a selection that needs
        to extend past what's currently on screen is simply impossible to
        make, since the mouse can't drag past the viewport's own edge."""
        viewport_pos = self.mapTo(self.reader.scroll_area.viewport(), drag_pos.toPoint())
        viewport_h = self.reader.scroll_area.viewport().height()
        margin = 40
        if viewport_pos.y() < margin:
            self._autoscroll_direction = -1
        elif viewport_pos.y() > viewport_h - margin:
            self._autoscroll_direction = 1
        else:
            self._autoscroll_direction = 0

        if self._autoscroll_direction != 0 and not self._autoscroll_timer.isActive():
            self._autoscroll_timer.start()
        elif self._autoscroll_direction == 0:
            self._autoscroll_timer.stop()

    def _autoscroll_step(self):
        if self._autoscroll_direction == 0 or self._drag_start is None:
            self._autoscroll_timer.stop()
            return
        vbar = self.reader.scroll_area.verticalScrollBar()
        vbar.setValue(vbar.value() + self._autoscroll_direction * 18)
        # The page hasn't moved, only the viewport's scroll position, so
        # the drag's live highlight needs to be recomputed against the
        # same (unchanged) overlay-space coordinates -- but since the
        # mouse itself hasn't moved, re-driving the same current drag
        # point is enough to keep the highlight extending correctly.
        self.reader.update_text_selection(self._drag_start, self._drag_current, finished=False)

    def contextMenuEvent(self, event):
        pos = event.position()
        menu = QMenu(self)
        if self.reader.selected_text:
            copy_action = menu.addAction("Copy")
            search_action = menu.addAction("Search in Book")
            save_action = menu.addAction("Save Highlight...")
            select_all_action = menu.addAction("Select All")
            chosen = menu.exec(event.globalPos())
            if chosen is copy_action:
                self.reader.copy_selection()
            elif chosen is search_action:
                self.reader.search_selection_in_book()
            elif chosen is save_action:
                self.reader.save_selection_as_highlight()
            elif chosen is select_all_action:
                self.reader.select_all_text()
            return

        existing = self.highlight_at_point(pos)
        if existing is not None:
            edit_action = menu.addAction("Edit Highlight...")
            delete_action = menu.addAction("Delete Highlight")
            chosen = menu.exec(event.globalPos())
            if chosen is edit_action:
                self.reader.edit_highlight(existing["id"])
            elif chosen is delete_action:
                self.reader.delete_highlight(existing["id"])
            return

        select_all_action = menu.addAction("Select All")
        chosen = menu.exec(event.globalPos())
        if chosen is select_all_action:
            self.reader.select_all_text()

    def set_highlight_rects(self, rects):
        self._highlight_rects = rects
        self.update()

    def set_live_color(self, color):
        self._live_color = QColor(color)
        self.update()

    def set_saved_highlights(self, highlights):
        """highlights: a list of {"id": int, "color": QColor, "rects":
        [QRectF, ...]} -- already converted to this overlay's own pixel
        space by the caller (ReaderWindow knows about zoom and two-page
        offsets that this widget doesn't need to)."""
        self._saved_highlights = highlights
        self.update()

    def highlight_at_point(self, pos):
        """The saved highlight (as passed to set_saved_highlights) whose
        rects contain `pos`, or None. Used to route a right-click either
        to the "made a new selection" menu or the "clicked an existing
        highlight" menu."""
        for h in self._saved_highlights:
            for r in h["rects"]:
                if r.contains(pos):
                    return h
        return None

    def clear(self):
        self._drag_start = None
        self._drag_current = None
        self._highlight_rects = []
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        # Saved highlights draw first (underneath), each in its own
        # stored color and style, so the just-finished selection (drawn
        # last, below) is never visually lost underneath one it overlaps.
        for h in self._saved_highlights:
            self._paint_highlight(painter, h["rects"], h["color"], h.get("accent_color") or h["color"], h.get("style", "fill"))
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._live_color)
        for r in self._highlight_rects:
            painter.drawRect(r)
        painter.end()

    @staticmethod
    def _paint_highlight(painter, rects, fill_color, accent_color, style):
        if style in ("fill", "fill_underline", "fill_strikethrough"):
            fill = QColor(fill_color)
            fill.setAlpha(110)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            for r in rects:
                painter.drawRect(r)
        if style in ("underline", "fill_underline"):
            pen_color = QColor(accent_color)
            pen_color.setAlpha(220)
            painter.setPen(QPen(pen_color, 2))
            painter.setBrush(Qt.NoBrush)
            for r in rects:
                y = r.bottom() - 1
                painter.drawLine(r.left(), y, r.right(), y)
        if style in ("strikethrough", "fill_strikethrough"):
            pen_color = QColor(accent_color)
            pen_color.setAlpha(220)
            painter.setPen(QPen(pen_color, 2))
            painter.setBrush(Qt.NoBrush)
            for r in rects:
                y = r.top() + r.height() / 2
                painter.drawLine(r.left(), y, r.right(), y)


class DrawingOverlay(QWidget):
    """A transparent overlay sitting on top of the rendered page pixmap,
    a sibling of TextSelectionOverlay (see its docstring for why
    mouse-transparency, not an internal mode flag, is what routes clicks
    to the right handler) -- same idea here: only non-transparent (and
    thus only receiving mouse events) while Draw mode is on, but always
    visible and always painting whatever's been saved, so drawings stay
    on the page regardless of which mode is currently active, the same
    way saved highlights do.

    Drawing works as a draft, not an immediate save -- the same
    "select/draw first, explicitly save second" shape as text
    highlighting (drag to select text, then click Save Highlight): a
    finished stroke or shape lands in _draft, not the database, so it
    can be undone (Ctrl+Z / the Undo button) or discarded entirely (the
    Clear button, or just leaving Draw mode or the page without saving)
    before it ever becomes permanent. Only ReaderWindow.save_drawn_
    highlights() actually writes anything to the database. Freehand pen
    strokes record every point the mouse passes through; the shape tools
    (rectangle/ellipse/triangle/line) only need the drag start and
    current position -- a "corner to corner" box, the same interaction
    for all four, with the specific shape rendered differently within
    that box."""

    def __init__(self, reader, parent=None):
        super().__init__(parent)
        self.reader = reader
        self.setCursor(Qt.CrossCursor)
        self._drawing = False
        self._live_points = []
        self._saved = []  # [{"id", "tool", "color": QColor, "opacity", "stroke_width", "points": [QPointF,...]}]
        self._draft = []  # same shape as _saved, minus "id" -- finished but not yet persisted to the database
        self.hide()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._drawing = True
        self._live_points = [event.position()]
        self.update()

    def mouseMoveEvent(self, event):
        if not self._drawing:
            return
        pos = event.position()
        if self.reader.draw_tool == "pen":
            self._live_points.append(pos)
        else:
            self._live_points = [self._live_points[0], pos]
        self.update()

    def mouseReleaseEvent(self, event):
        if not self._drawing or event.button() != Qt.LeftButton:
            return
        self._drawing = False
        pos = event.position()
        if self.reader.draw_tool == "pen":
            self._live_points.append(pos)
        else:
            self._live_points = [self._live_points[0], pos]
        points = self._live_points
        self._live_points = []
        self.update()
        # a click with no real drag (a stray click while switching tools,
        # or a shape with a zero-size box) isn't a real shape -- require
        # actual movement before it's worth keeping
        if len(points) >= 2 and (points[0] - points[-1]).manhattanLength() >= 3:
            # captured with THIS stroke's own color/opacity/width, not
            # read again later at save time -- so changing the color
            # partway through a drawing session doesn't retroactively
            # repaint strokes already finished, the same way a real
            # drawing tool's already-drawn strokes don't change color
            # when you pick a new one for the next stroke
            self._draft.append({
                "tool": self.reader.draw_tool,
                "points": points,
                "color": QColor(self.reader.draw_color),
                "opacity": self.reader.draw_opacity,
                "stroke_width": self.reader.draw_stroke_width,
            })
            self.reader.notify_draft_drawing_added()

    def contextMenuEvent(self, event):
        pos = event.position()
        hit = self.drawing_at_point(pos)
        if hit is None:
            return
        menu = QMenu(self)
        delete_action = menu.addAction("Delete Drawing")
        chosen = menu.exec(event.globalPos())
        if chosen is delete_action:
            self.reader.delete_drawing(hit["id"])

    def drawing_at_point(self, pos, tolerance=6):
        """The saved drawing (as passed to set_saved_drawings) that `pos`
        falls on or near, or None -- checked last-drawn-first, so an
        overlapping shape's most-recently-added (visually topmost) match
        wins, matching how the paintEvent below layers them."""
        for d in reversed(self._saved):
            if self._point_hits(pos, d, tolerance):
                return d
        return None

    @staticmethod
    def _point_hits(pos, drawing, tolerance):
        tool, points = drawing["tool"], drawing["points"]
        if tool == "pen":
            for a, b in zip(points, points[1:]):
                if DrawingOverlay._distance_to_segment(pos, a, b) <= tolerance:
                    return True
            return False
        if tool == "line":
            return DrawingOverlay._distance_to_segment(pos, points[0], points[-1]) <= tolerance
        rect = QRectF(points[0], points[-1]).normalized()
        margin = QPointF(tolerance, tolerance)
        return QRectF(rect.topLeft() - margin, rect.bottomRight() + margin).contains(pos)

    @staticmethod
    def _distance_to_segment(p, a, b):
        seg = b - a
        length_sq = seg.x() ** 2 + seg.y() ** 2
        if length_sq <= 1e-9:
            return (p - a).manhattanLength()
        t = max(0.0, min(1.0, QPointF.dotProduct(p - a, seg) / length_sq))
        proj = a + seg * t
        d = p - proj
        return (d.x() ** 2 + d.y() ** 2) ** 0.5

    def set_saved_drawings(self, drawings):
        """drawings: a list of {"id", "tool", "color": QColor, "opacity",
        "stroke_width", "points": [QPointF, ...]} -- already converted to
        this overlay's own pixel space by the caller, same convention as
        TextSelectionOverlay.set_saved_highlights."""
        self._saved = drawings
        self.update()

    def clear_live_stroke(self):
        self._drawing = False
        self._live_points = []
        self.update()

    def get_draft_drawings(self):
        return list(self._draft)

    def has_draft(self):
        return bool(self._draft)

    def undo_last_draft(self):
        if self._draft:
            self._draft.pop()
            self.update()

    def clear_draft(self):
        if self._draft:
            self._draft = []
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        for d in self._saved:
            self._paint_shape(painter, d["tool"], d["points"], d["color"], d["opacity"], d["stroke_width"])
        for d in self._draft:
            self._paint_shape(painter, d["tool"], d["points"], d["color"], d["opacity"], d["stroke_width"])
        if self._live_points:
            self._paint_shape(
                painter, self.reader.draw_tool, self._live_points,
                self.reader.draw_color, self.reader.draw_opacity, self.reader.draw_stroke_width,
            )
        painter.end()

    @staticmethod
    def _paint_shape(painter, tool, points, color, opacity, stroke_width):
        if len(points) < 2:
            return
        c = QColor(color)
        c.setAlphaF(max(0.0, min(1.0, opacity)))
        if tool == "pen":
            painter.setPen(QPen(c, stroke_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            path = QPainterPath()
            path.moveTo(points[0])
            for p in points[1:]:
                path.lineTo(p)
            painter.drawPath(path)
        elif tool == "line":
            painter.setPen(QPen(c, stroke_width, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(points[0], points[-1])
        else:
            rect = QRectF(points[0], points[-1]).normalized()
            painter.setPen(Qt.NoPen)
            painter.setBrush(c)
            if tool == "rectangle":
                painter.drawRect(rect)
            elif tool == "ellipse":
                painter.drawEllipse(rect)
            elif tool == "triangle":
                path = QPainterPath()
                path.moveTo(rect.center().x(), rect.top())
                path.lineTo(rect.left(), rect.bottom())
                path.lineTo(rect.right(), rect.bottom())
                path.closeSubpath()
                painter.drawPath(path)


class SelectionPopup(QWidget):
    """A small floating toolbar that appears next to a just-finished text
    selection -- offering Copy and Search in Book right there, instead of
    only via the right-click menu or Ctrl+C. A plain child widget of the
    ReaderWindow itself (not a separate top-level popup), positioned with
    an explicit move() and shown/hidden explicitly, rather than relying
    on a native popup window's own focus/dismiss behavior -- simpler and
    more predictable than fighting Qt.Popup's auto-grab quirks."""

    def __init__(self, reader, parent=None):
        super().__init__(parent)
        self.reader = reader
        self.setAutoFillBackground(True)
        self.setStyleSheet(
            "SelectionPopup { background: palette(window); border: 1px solid palette(mid); "
            "border-radius: 4px; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(4)
        copy_btn = QPushButton("Copy")
        copy_btn.setFlat(True)
        copy_btn.clicked.connect(self._copy)
        layout.addWidget(copy_btn)
        search_btn = QPushButton("Search in Book")
        search_btn.setFlat(True)
        search_btn.clicked.connect(self._search)
        layout.addWidget(search_btn)
        save_btn = QPushButton("Save Highlight")
        save_btn.setFlat(True)
        save_btn.clicked.connect(self._save_highlight)
        layout.addWidget(save_btn)
        self.hide()

    def _copy(self):
        self.reader.copy_selection()
        self.hide()

    def _search(self):
        self.reader.search_selection_in_book()
        self.hide()

    def _save_highlight(self):
        self.reader.save_selection_as_highlight()
        self.hide()

    def show_near(self, local_pos):
        self.adjustSize()
        self.move(int(local_pos.x()), int(local_pos.y()) + 14)
        self.show()
        self.raise_()


class HighlightDialog(QDialog):
    """Prompts for a highlight's name, color, style, and (when the style
    involves a line -- underline, strikethrough, or one of the combined
    styles) a separate accent color for that line, independent of the
    fill color. The fill color swatch starts on whatever color was
    already chosen (the most recently used color when saving new, or the
    highlight's own color when editing), and "Choose Color..." opens the
    full picker (basic swatches, a spectrum/wheel, and exact RGB/HSV/hex
    entry) to change it -- letting someone highlight different passages
    in different colors of their own choosing, one save at a time. The
    accent color row only appears for styles that actually have a line to
    color, and defaults to matching the fill color until changed. When
    editing an existing highlight, text_preview shows what was actually
    highlighted, read-only, so it's easy to tell highlights apart without
    having to jump to the page."""

    STYLES = [
        ("fill", "Highlight (fill)"),
        ("underline", "Underline"),
        ("strikethrough", "Strikethrough"),
        ("fill_underline", "Highlight + Underline"),
        ("fill_strikethrough", "Highlight + Strikethrough"),
    ]

    def __init__(
        self, title, initial_name, initial_color, initial_accent_color=None,
        initial_style="fill", text_preview=None, parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._color = QColor(initial_color)
        self._accent_color = QColor(initial_accent_color or initial_color)

        layout = QVBoxLayout(self)
        self.form = QFormLayout()
        self.name_edit = QLineEdit(initial_name or "")
        self.name_edit.setPlaceholderText("Leave blank to use the page number")
        self.form.addRow("Name", self.name_edit)

        color_row = QHBoxLayout()
        self.color_swatch = QLabel()
        self.color_swatch.setFixedSize(22, 22)
        self._update_swatch(self.color_swatch, self._color)
        color_row.addWidget(self.color_swatch)
        choose_btn = QPushButton("Choose Color...")
        choose_btn.clicked.connect(self._choose_color)
        color_row.addWidget(choose_btn)
        color_row.addStretch()
        self.form.addRow("Color", color_row)

        accent_row = QHBoxLayout()
        self.accent_swatch = QLabel()
        self.accent_swatch.setFixedSize(22, 22)
        self._update_swatch(self.accent_swatch, self._accent_color)
        accent_row.addWidget(self.accent_swatch)
        accent_choose_btn = QPushButton("Choose Color...")
        accent_choose_btn.clicked.connect(self._choose_accent_color)
        accent_row.addWidget(accent_choose_btn)
        accent_row.addStretch()
        self.form.addRow("Underline/Strikethrough Color", accent_row)

        self.style_combo = QComboBox()
        for value, label in self.STYLES:
            self.style_combo.addItem(label, value)
        idx = self.style_combo.findData(initial_style)
        self.style_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.style_combo.currentIndexChanged.connect(self._update_accent_row_visibility)
        self.form.addRow("Style", self.style_combo)
        layout.addLayout(self.form)
        self._update_accent_row_visibility()

        if text_preview is not None:
            preview_label = QLabel("Highlighted text:")
            layout.addWidget(preview_label)
            preview = QTextBrowser()
            preview.setPlainText(text_preview or "(no text captured)")
            preview.setReadOnly(True)
            preview.setMaximumHeight(110)
            layout.addWidget(preview)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _update_accent_row_visibility(self):
        needs_accent = self.style_combo.currentData() != "fill"
        self.form.setRowVisible(2, needs_accent)  # row 2 = "Underline/Strikethrough Color"

    @staticmethod
    def _update_swatch(swatch_label, color):
        swatch_label.setStyleSheet(
            f"background-color: {color.name()}; border: 1px solid palette(mid);"
        )

    def _choose_color(self):
        color = QColorDialog.getColor(self._color, self, "Choose Highlight Color")
        if color.isValid():
            self._color = color
            self._update_swatch(self.color_swatch, self._color)

    def _choose_accent_color(self):
        color = QColorDialog.getColor(self._accent_color, self, "Choose Underline/Strikethrough Color")
        if color.isValid():
            self._accent_color = color
            self._update_swatch(self.accent_swatch, self._accent_color)

    def result_values(self):
        """(name, fill_color, accent_color, style) -- call only after
        exec() returns QDialog.Accepted."""
        return self.name_edit.text().strip(), self._color, self._accent_color, self.style_combo.currentData()


class DrawingDialog(QDialog):
    """Edits a saved drawing's color, opacity, and stroke/outline width --
    the same appearance properties chosen up front in the draw toolbar
    before drawing it, now editable afterward too. Its shape and position
    aren't editable this way; that would need interactive resize handles
    on the page itself rather than a simple form, so for now correcting
    those means deleting the drawing and redrawing it."""

    def __init__(self, title, initial_color, initial_opacity, initial_stroke_width, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._color = QColor(initial_color)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        color_row = QHBoxLayout()
        self.color_swatch = QLabel()
        self.color_swatch.setFixedSize(22, 22)
        self._update_swatch()
        color_row.addWidget(self.color_swatch)
        choose_btn = QPushButton("Choose Color...")
        choose_btn.clicked.connect(self._choose_color)
        color_row.addWidget(choose_btn)
        color_row.addStretch()
        form.addRow("Color", color_row)

        opacity_row = QHBoxLayout()
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(round(initial_opacity * 100))
        self.opacity_label = QLabel(f"{self.opacity_slider.value()}%")
        self.opacity_label.setFixedWidth(36)
        self.opacity_slider.valueChanged.connect(lambda v: self.opacity_label.setText(f"{v}%"))
        opacity_row.addWidget(self.opacity_slider)
        opacity_row.addWidget(self.opacity_label)
        form.addRow("Opacity", opacity_row)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 20)
        self.width_spin.setValue(round(initial_stroke_width))
        form.addRow("Width", self.width_spin)

        layout.addLayout(form)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self.accept)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

    def _update_swatch(self):
        self.color_swatch.setStyleSheet(
            f"background-color: {self._color.name()}; border: 1px solid palette(mid);"
        )

    def _choose_color(self):
        color = QColorDialog.getColor(self._color, self, "Choose Drawing Color")
        if color.isValid():
            self._color = color
            self._update_swatch()

    def result_values(self):
        """(color, opacity, stroke_width) -- call only after exec()
        returns QDialog.Accepted."""
        return self._color, self.opacity_slider.value() / 100.0, float(self.width_spin.value())


class ReaderWindow(QMainWindow):
    def __init__(self, db: Database, book_id: int, on_close=None, password=None, open_book_at_page=None):
        super().__init__()
        self.db = db
        self.book_id = book_id
        self.on_close = on_close
        self.open_book_at_page = open_book_at_page  # callback(book_id, page_number) -- lets a
        # search result for a DIFFERENT book (searching is library-wide) actually open it;
        # this window has no way to do that itself, since it only ever owns one book

        db.mark_as_reading_if_new(book_id)  # first open promotes 'unread' -> 'reading'
        self.book = db.get_book(book_id)

        try:
            self.doc = fitz.open(self.book["filepath"])
            if self.doc.needs_pass:
                # The caller (library_window.open_book) already verified this
                # password is correct before ever constructing this window.
                self.doc.authenticate(password or "")
        except Exception as exc:
            QMessageBox.critical(self, "Could not open file", str(exc))
            self.doc = None
            self.page_count = 0
            self.current_page = 0
            return

        self.page_count = max(self.doc.page_count, 1)
        self.current_page = min(max(self.book["last_page"] or 0, 0), self.page_count - 1)
        self.zoom = float(db.get_setting("reader_zoom", 1.3))
        self.auto_fit = db.get_setting("reader_auto_fit", "1") == "1"
        self.font_size = int(db.get_setting("reader_font_size", 15))
        self.simple_text_mode = db.get_setting("reader_text_mode", "normal") == "simple"
        self.dark_mode = db.get_setting("theme", "light") == "dark"
        self.dark_pages = db.get_setting("reader_dark_pages", "0") == "1"
        self.two_page_mode = db.get_setting("reader_two_page", "0") == "1"
        if self.two_page_mode:
            self.current_page = self._pair_start(self.current_page)

        self.setWindowTitle(self.book["title"])
        self.resize(920, 800)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self.render_page)

        self._panning = False
        self._pan_start_pos = None
        self._pan_start_h = 0
        self._pan_start_v = 0
        # Set on a right-button press on the page, cleared on release --
        # tracks whether the button was held through a scroll (the
        # right-click-hold-and-scroll gesture, if that's how zoom or
        # page-turn-while-zoomed is currently configured), so the reading
        # context menu only appears for an actual simple right-click, not
        # as an unwelcome interruption right after using the button for a
        # scroll gesture instead.
        self._right_click_held_through_scroll = False

        self._current_render_zoom = 1.0

        self.select_text_mode = False
        self.selected_text = ""
        self._chars_cache = {}  # page_index -> chars_from_rawdict() result, built lazily
        self._left_page_px_width = 0  # set on every two-page render; used to map clicks to the right page
        self._pending_overlay_size = (0, 0)
        self._last_selection_pos = None  # overlay-local point to anchor the selection popup near
        self._search_dialog = None
        self._last_selection_page_ranges = []
        self._last_selection_chars_by_page = {}
        self.highlight_color = db.get_setting("highlight_color", DEFAULT_HIGHLIGHT_COLOR)
        # The color/accent a NEW highlight's dialog starts from -- updated
        # every time a highlight is actually saved or edited with a
        # particular color, so highlighting several passages in one color
        # doesn't mean re-picking it every single time. Distinct from
        # highlight_color above, which only changes via the toolbar's
        # explicit "Highlight Color" default-setting button.
        self.last_highlight_color = db.get_setting("last_highlight_color", self.highlight_color)
        self.last_highlight_accent_color = db.get_setting("last_highlight_accent_color", self.last_highlight_color)

        self.draw_mode = False
        self.draw_tool = db.get_setting("last_draw_tool", "pen")
        if self.draw_tool not in DRAWING_TOOLS:
            self.draw_tool = "pen"
        self.draw_color = db.get_setting("last_draw_color", DEFAULT_DRAWING_COLOR)
        self.draw_opacity = float(db.get_setting("last_draw_opacity", 0.4))
        self.draw_stroke_width = float(db.get_setting("last_draw_stroke_width", 3.0))
        self._draft_drawing_page = None  # which page (if any) has unsaved drawn strokes right now

        self._focus_mode = False
        self._pre_focus_bookmarks_visible = True
        self._pre_focus_thumbnails_visible = False

        self._build_ui()
        self._build_bookmarks_dock()
        self._build_thumbnail_dock()
        self.render_page()
        self.refresh_bookmarks()
        self.refresh_highlights()

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
        toolbar = QToolBar("Reader")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.toolbar = toolbar

        prev_action = QAction("\u25c0 Prev", self)
        prev_action.triggered.connect(self.prev_page)
        toolbar.addAction(prev_action)
        # Also associated directly with the window itself, not just the
        # toolbar -- a QAction's keyboard shortcut stops being routable
        # once every widget it's associated with is hidden, even though
        # the action stays enabled and reports the correct shortcut, and
        # Focus Mode hides the toolbar. The window itself is never
        # hidden, so this keeps the shortcut alive there too. (Belt and
        # suspenders here specifically: Prev/Next also has an independent
        # fix in _maybe_handle_page_turn_key for the OTHER way this class
        # of bug shows up -- a focused child widget like QScrollArea
        # consuming the arrow key itself before any shortcut mechanism
        # sees it. Two different mechanisms, two different failure modes,
        # both needed for page-turning to be genuinely unbreakable.)
        self.addAction(prev_action)
        self.prev_action = prev_action

        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setMaximum(self.page_count)
        self.page_spin.setValue(self.current_page + 1)
        self.page_spin.setFocusPolicy(Qt.ClickFocus)  # don't let it grab arrow keys by default
        self.page_spin.valueChanged.connect(self.jump_to_page)
        toolbar.addWidget(self.page_spin)

        toolbar.addWidget(QLabel(f" / {self.page_count}   "))

        next_action = QAction("Next \u25b6", self)
        next_action.triggered.connect(self.next_page)
        toolbar.addAction(next_action)
        self.addAction(next_action)  # see prev_action's comment above
        self.next_action = next_action

        toolbar.addSeparator()

        dec_action = QAction("A-", self)
        dec_action.setToolTip("Decrease text size / zoom out")
        dec_action.triggered.connect(self.decrease_text_size)
        toolbar.addAction(dec_action)
        self.addAction(dec_action)  # see prev_action's comment above -- this one has no
        self.dec_action = dec_action  # independent fallback, so this is the ONLY thing keeping it working in Focus Mode

        inc_action = QAction("A+", self)
        inc_action.setToolTip("Increase text size / zoom in")
        inc_action.triggered.connect(self.increase_text_size)
        toolbar.addAction(inc_action)
        self.addAction(inc_action)  # see prev_action's comment above -- same as dec_action
        self.inc_action = inc_action

        toolbar.addSeparator()

        self.select_text_btn = QPushButton("Select Text")
        self.select_text_btn.setCheckable(True)
        self.select_text_btn.clicked.connect(self.toggle_select_text_mode)
        toolbar.addWidget(self.select_text_btn)

        self.copy_feedback_label = QLabel("")
        self.copy_feedback_label.setStyleSheet("color: #888; padding-left: 6px;")
        toolbar.addWidget(self.copy_feedback_label)

        # ---- Everything below lives in the menu bar, not the toolbar --
        # kept as the exact same widgets/actions (same attribute names,
        # same setChecked()/setText() call sites elsewhere in this file),
        # just displayed in View/Book instead of crowding the toolbar. ----
        self.fit_btn = QPushButton("Fit to Screen")
        self.fit_btn.setToolTip(
            "Automatically scale each page to fit the window (pages can vary in size)"
        )
        self.fit_btn.setCheckable(True)
        self.fit_btn.setChecked(self.auto_fit)
        self.fit_btn.clicked.connect(self.toggle_auto_fit)

        self.two_page_btn = QPushButton("Two-Page View")
        self.two_page_btn.setToolTip(
            "Show two pages side by side, like a book spread -- handy on a wide screen"
        )
        self.two_page_btn.setCheckable(True)
        self.two_page_btn.setChecked(self.two_page_mode)
        self.two_page_btn.clicked.connect(self.toggle_two_page_mode)

        highlight_color_btn = QPushButton("Highlight Color...")
        highlight_color_btn.setToolTip(
            "Set the default color used for the live selection highlight and for new saved highlights"
        )
        highlight_color_btn.clicked.connect(self.choose_default_highlight_color)

        self.simple_btn = QPushButton("Simple Text")
        self.simple_btn.setToolTip("Show only the extracted text of this page")
        self.simple_btn.setCheckable(True)
        self.simple_btn.setChecked(self.simple_text_mode)
        self.simple_btn.clicked.connect(self.toggle_simple_text)

        self.dark_btn = QPushButton("Dark Mode")
        self.dark_btn.setToolTip("Light/dark app theme (toolbars, menus, text mode)")
        self.dark_btn.setCheckable(True)
        self.dark_btn.setChecked(self.dark_mode)
        self.dark_btn.clicked.connect(self.toggle_dark_mode)

        self.dark_pages_btn = QPushButton("Dark Pages")
        self.dark_pages_btn.setToolTip(
            "Invert rendered page colors (dark file), independent of the app theme"
        )
        self.dark_pages_btn.setCheckable(True)
        self.dark_pages_btn.setChecked(self.dark_pages)
        self.dark_pages_btn.clicked.connect(self.toggle_dark_pages)

        # Thumbnail panel layout choice -- an exclusive pair, so picking
        # one always un-picks the other, same as any other radio-style menu.
        initial_orientation = self.db.get_setting("thumbnail_orientation", "vertical")
        self.vertical_orientation_action = QAction("Vertical (Left)", self)
        self.vertical_orientation_action.setCheckable(True)
        self.vertical_orientation_action.setChecked(initial_orientation != "horizontal")
        self.vertical_orientation_action.triggered.connect(lambda: self.set_thumbnail_orientation("vertical"))
        self.horizontal_orientation_action = QAction("Horizontal (Bottom)", self)
        self.horizontal_orientation_action.setCheckable(True)
        self.horizontal_orientation_action.setChecked(initial_orientation == "horizontal")
        self.horizontal_orientation_action.triggered.connect(lambda: self.set_thumbnail_orientation("horizontal"))
        # Kept as a real attribute, not a local variable -- like the menus
        # below, an exclusive QActionGroup with no surviving Python
        # reference can be garbage-collected out from under its actions.
        self._thumbnail_orientation_group = QActionGroup(self)
        self._thumbnail_orientation_group.setExclusive(True)
        self._thumbnail_orientation_group.addAction(self.vertical_orientation_action)
        self._thumbnail_orientation_group.addAction(self.horizontal_orientation_action)

        self.fav_btn = QPushButton(self._fav_label())
        self.fav_btn.setCheckable(True)
        self.fav_btn.setChecked(bool(self.book["is_favorite"]))
        self.fav_btn.clicked.connect(self.toggle_favorite)

        self.finished_btn = QPushButton(self._finished_label())
        self.finished_btn.setToolTip("Mark this book as finished / not finished")
        self.finished_btn.setCheckable(True)
        self.finished_btn.setChecked(self.book["status"] == "finished")
        self.finished_btn.clicked.connect(self.toggle_finished)

        bookmark_action = QAction("+ Bookmark", self)
        bookmark_action.triggered.connect(self.add_bookmark)
        self.bookmark_action = bookmark_action

        self.next_highlight_action = QAction("Jump to Next Highlight", self)
        self.next_highlight_action.triggered.connect(self.jump_to_next_highlight)
        self.prev_highlight_action = QAction("Jump to Previous Highlight", self)
        self.prev_highlight_action.triggered.connect(self.jump_to_prev_highlight)

        self.toggle_focus_mode_action = QAction("Focus Mode (Hide All Menus)", self)
        self.toggle_focus_mode_action.triggered.connect(self.toggle_focus_mode)

        # ---- Menu bar ----
        view_menu = menubar.addMenu("&View")
        self._view_menu = view_menu
        view_menu.addAction(self._as_widget_action(self.fit_btn))
        view_menu.addAction(self._as_widget_action(self.two_page_btn))
        view_menu.addSeparator()
        view_menu.addAction(self._as_widget_action(self.simple_btn))
        view_menu.addSeparator()
        view_menu.addAction(self._as_widget_action(self.dark_btn))
        view_menu.addAction(self._as_widget_action(self.dark_pages_btn))
        view_menu.addSeparator()
        view_menu.addAction(self._as_widget_action(highlight_color_btn))
        view_menu.addSeparator()
        thumbnail_menu = view_menu.addMenu("Thumbnail Panel Layout")
        self._thumbnail_menu = thumbnail_menu
        thumbnail_menu.addAction(self.vertical_orientation_action)
        thumbnail_menu.addAction(self.horizontal_orientation_action)
        view_menu.addSeparator()
        view_menu.addAction(self.toggle_focus_mode_action)

        book_menu = menubar.addMenu("&Book")
        self._book_menu = book_menu
        book_menu.addAction(self._as_widget_action(self.fav_btn))
        book_menu.addAction(self._as_widget_action(self.finished_btn))
        book_menu.addSeparator()
        book_menu.addAction(self.bookmark_action)
        book_menu.addSeparator()
        book_menu.addAction(self.next_highlight_action)
        book_menu.addAction(self.prev_highlight_action)

        self.bookmarks_btn = QPushButton("Bookmarks/Highlights")
        self.bookmarks_btn.setToolTip("Show or hide the bookmarks and highlights panel")
        self.bookmarks_btn.setCheckable(True)
        self.bookmarks_btn.setChecked(True)  # the panel starts open
        self.bookmarks_btn.clicked.connect(self.toggle_bookmarks_dock)
        toolbar.addWidget(self.bookmarks_btn)

        self.thumbnails_btn = QPushButton("Pages")
        self.thumbnails_btn.setToolTip("Show or hide a page thumbnail panel for visual navigation")
        self.thumbnails_btn.setCheckable(True)
        self.thumbnails_btn.setChecked(False)  # starts hidden -- opt in, no rendering cost until you want it
        self.thumbnails_btn.clicked.connect(self.toggle_thumbnail_dock)
        toolbar.addWidget(self.thumbnails_btn)

        self.draw_btn = QPushButton("Draw")
        self.draw_btn.setToolTip(
            "Draw freehand or with simple shapes -- a manual way to mark up a "
            "page, like highlighting or annotating a normal textbook"
        )
        self.draw_btn.setCheckable(True)
        self.draw_btn.clicked.connect(self.toggle_draw_mode)
        toolbar.addWidget(self.draw_btn)

        # Central viewing area holds both the page-image view and the plain
        # text view; only one is visible at a time depending on the mode.
        container = QWidget()
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)

        self.page_label = QLabel()
        self.page_label.setObjectName("pageLabel")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.page_label)

        self.text_overlay = TextSelectionOverlay(self, self.page_label)
        live_color = QColor(self.highlight_color)
        live_color.setAlpha(90)
        self.text_overlay.set_live_color(live_color)
        self.selection_popup = SelectionPopup(self, self)

        self.drawing_overlay = DrawingOverlay(self, self.page_label)

        self.text_browser = QTextBrowser()
        self.text_browser.setReadOnly(True)

        v.addWidget(self.scroll_area)
        v.addWidget(self.text_browser)
        self.setCentralWidget(container)
        self._build_draw_toolbar()
        self._update_mode_visibility()

        # Ctrl+scroll to zoom, plain scroll to turn pages (see eventFilter / _handle_wheel).
        # Left-click-drag pans a zoomed-in page (see _handle_pan_*).
        self.scroll_area.viewport().installEventFilter(self)
        self.text_browser.viewport().installEventFilter(self)
        self.page_label.installEventFilter(self)
        # QScrollArea and QTextBrowser both default to a focus policy that
        # lets them hold keyboard focus and both have their own built-in
        # arrow-key handling -- installed here (on the widgets themselves,
        # where focus and keyPressEvent actually land, not just their
        # viewports) so _maybe_handle_page_turn_key gets first look at
        # Left/Right before either widget's own handling can consume it.
        self.scroll_area.installEventFilter(self)
        self.text_browser.installEventFilter(self)

        # A few actions are keyboard-only -- no toolbar button of their own,
        # but still customizable like everything else in the catalog.
        self.toggle_select_text_shortcut = QShortcut(QKeySequence(), self)
        self.toggle_select_text_shortcut.activated.connect(self.select_text_btn.click)
        self.toggle_simple_text_shortcut = QShortcut(QKeySequence(), self)
        self.toggle_simple_text_shortcut.activated.connect(self.simple_btn.click)
        self.toggle_two_page_shortcut = QShortcut(QKeySequence(), self)
        self.toggle_two_page_shortcut.activated.connect(self.two_page_btn.click)
        self.close_window_shortcut = QShortcut(QKeySequence(), self)
        self.close_window_shortcut.activated.connect(self.close)
        self.undo_drawing_shortcut = QShortcut(QKeySequence(), self)
        self.undo_drawing_shortcut.activated.connect(self._undo_drawing_shortcut_triggered)
        self.toggle_fit_shortcut = QShortcut(QKeySequence(), self)
        self.toggle_fit_shortcut.activated.connect(self.fit_btn.click)

        self.apply_shortcuts()

    def apply_shortcuts(self):
        """(Re-)applies every customizable action's current effective
        shortcut -- called once at startup, and again whenever the
        library window's Keyboard Shortcuts dialog saves a change, so
        this window (if already open) picks up the new bindings
        immediately rather than needing to be closed and reopened."""
        overrides = load_overrides(self.db)
        bindings = (
            (self.prev_action, "reader.prev_page"),
            (self.next_action, "reader.next_page"),
            (self.inc_action, "reader.zoom_in"),
            (self.dec_action, "reader.zoom_out"),
            (self.bookmark_action, "reader.add_bookmark"),
            (self.toggle_select_text_shortcut, "reader.toggle_select_text"),
            (self.toggle_simple_text_shortcut, "reader.toggle_simple_text"),
            (self.toggle_two_page_shortcut, "reader.toggle_two_page"),
            (self.close_window_shortcut, "reader.close_window"),
            (self.toggle_focus_mode_action, "reader.toggle_focus_mode"),
            (self.next_highlight_action, "reader.next_highlight"),
            (self.prev_highlight_action, "reader.prev_highlight"),
            (self.undo_drawing_shortcut, "reader.undo_drawing"),
            (self.toggle_fit_shortcut, "reader.toggle_fit_to_screen"),
        )
        for target, action_id in bindings:
            seq = QKeySequence(effective_shortcut(action_id, overrides))
            if isinstance(target, QShortcut):
                target.setKey(seq)
            else:
                target.setShortcut(seq)

        # The two mouse-wheel actions aren't QAction/QShortcut shortcuts at
        # all (no QKeySequence can represent "hold the right mouse button"),
        # so they're not part of the bindings tuple above -- just cached
        # here as a plain {gesture_id: action} dict for _resolve_wheel_
        # action to compare against on every wheel event, rather than
        # re-reading and re-parsing settings on every single scroll tick.
        wheel_overrides = load_wheel_overrides(self.db)
        self.wheel_gesture_actions = {
            gesture_id: effective_wheel_action(gesture_id, wheel_overrides)
            for gesture_id in WHEEL_GESTURES
        }

    def _build_bookmarks_dock(self):
        dock = QDockWidget("Bookmarks/Highlights", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        holder = QWidget()
        layout = QVBoxLayout(holder)

        # Bookmarks stay on top...
        self.bookmark_list = QListWidget()
        self.bookmark_list.itemDoubleClicked.connect(self.jump_to_bookmark)
        layout.addWidget(self.bookmark_list)
        remove_btn = QPushButton("Remove selected bookmark")
        remove_btn.clicked.connect(self.remove_selected_bookmark)
        layout.addWidget(remove_btn)

        # ...with Highlights right below, in the same scrollable panel, so
        # both are easy to browse without needing a whole separate dock.
        highlights_label = QLabel("Highlights")
        highlights_label.setStyleSheet("font-weight: bold; margin-top: 10px;")
        layout.addWidget(highlights_label)
        self.highlight_list = QListWidget()
        self.highlight_list.itemDoubleClicked.connect(self.jump_to_highlight)
        self.highlight_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.highlight_list.customContextMenuRequested.connect(self._show_highlight_list_menu)
        layout.addWidget(self.highlight_list)
        remove_highlight_btn = QPushButton("Remove selected highlight")
        remove_highlight_btn.clicked.connect(self.remove_selected_highlight)
        layout.addWidget(remove_highlight_btn)

        export_highlights_btn = QPushButton("Export Highlights...")
        export_highlights_btn.setToolTip(
            "Save every highlight in this book as a plain-text notes file (Markdown)"
        )
        export_highlights_btn.clicked.connect(self.export_highlights_notes)
        layout.addWidget(export_highlights_btn)

        dock.setWidget(holder)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

        self.bookmarks_dock = dock
        # Keep the toolbar button in sync if the panel is closed via its own
        # [x] button (or reopened some other way), not just via our toggle.
        dock.visibilityChanged.connect(self._on_bookmarks_dock_visibility_changed)

    def toggle_bookmarks_dock(self, checked):
        self.bookmarks_btn.setChecked(checked)
        self.bookmarks_dock.setVisible(checked)
        if checked:
            self.bookmarks_dock.raise_()

    def _on_bookmarks_dock_visibility_changed(self, visible):
        self.bookmarks_btn.setChecked(visible)

    # ------------- Page thumbnail / filmstrip panel -------------
    THUMB_PANEL_SIZE = (90, 120)  # smaller than the library's cover thumbnails -- a navigation strip, not a browsing grid

    def _build_thumbnail_dock(self):
        dock = QDockWidget("Pages", self)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)

        self.thumbnail_list = QListWidget()
        self.thumbnail_list.setIconSize(QSize(*self.THUMB_PANEL_SIZE))
        self.thumbnail_list.setViewMode(QListWidget.IconMode)
        self.thumbnail_list.setWrapping(False)
        self.thumbnail_list.setMovement(QListWidget.Static)
        self.thumbnail_list.setSpacing(4)
        self.thumbnail_list.itemClicked.connect(self._on_thumbnail_clicked)
        for i in range(self.page_count):
            item = QListWidgetItem(str(i + 1))
            item.setTextAlignment(Qt.AlignHCenter)
            item.setSizeHint(QSize(self.THUMB_PANEL_SIZE[0] + 24, self.THUMB_PANEL_SIZE[1] + 28))
            self.thumbnail_list.addItem(item)

        dock.setWidget(self.thumbnail_list)
        self.thumbnail_dock = dock
        self.thumbnail_orientation = self.db.get_setting("thumbnail_orientation", "vertical")
        self._apply_thumbnail_orientation(self.thumbnail_orientation, initial=True)
        dock.hide()  # matches thumbnails_btn's unchecked starting state -- opt in, no cost until shown

        dock.visibilityChanged.connect(self._on_thumbnail_dock_visibility_changed)

        self._thumbnails_rendered = False
        self._thumbnail_render_queue = []
        self._thumbnail_render_timer = QTimer(self)
        self._thumbnail_render_timer.setInterval(15)
        self._thumbnail_render_timer.timeout.connect(self._render_next_thumbnail_batch)

    def set_thumbnail_orientation(self, orientation):
        """Switches the page thumbnail panel between a vertical strip on
        the left and a horizontal filmstrip under the book -- live, with
        the panel's open/closed state preserved across the move, and
        remembered for next time this book (or any book) is opened."""
        if orientation == self.thumbnail_orientation:
            return
        self.thumbnail_orientation = orientation
        self.db.set_setting("thumbnail_orientation", orientation)
        self._apply_thumbnail_orientation(orientation)

    def _apply_thumbnail_orientation(self, orientation, initial=False):
        was_visible = None if initial else self.thumbnail_dock.isVisible()
        # QWIDGETSIZE_MAX -- Qt's own "no constraint" value, used below to
        # clear whichever max-size limit applied to the PREVIOUS orientation.
        no_limit = 16777215
        if orientation == "horizontal":
            self.thumbnail_list.setFlow(QListWidget.LeftToRight)
            # Locking vertical scrolling off and the dock's own height to
            # roughly one row's worth is what actually keeps this a single-
            # row filmstrip -- without it, Qt has no reason not to leave
            # room (and a redundant, wrong-direction scrollbar) for more
            # rows than the LeftToRight flow will ever actually use.
            self.thumbnail_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.thumbnail_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            thickness = self.THUMB_PANEL_SIZE[1] + 50
            self.thumbnail_dock.setMaximumHeight(thickness)
            self.thumbnail_dock.setMaximumWidth(no_limit)
            self.thumbnail_list.setMaximumHeight(thickness)
            self.thumbnail_list.setMaximumWidth(no_limit)
            area = Qt.BottomDockWidgetArea
            resize_orientation, resize_size = Qt.Vertical, thickness
        else:
            self.thumbnail_list.setFlow(QListWidget.TopToBottom)
            self.thumbnail_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.thumbnail_list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            thickness = self.THUMB_PANEL_SIZE[0] + 50
            self.thumbnail_dock.setMaximumWidth(thickness)
            self.thumbnail_dock.setMaximumHeight(no_limit)
            self.thumbnail_list.setMaximumWidth(thickness)
            self.thumbnail_list.setMaximumHeight(no_limit)
            area = Qt.LeftDockWidgetArea
            resize_orientation, resize_size = Qt.Horizontal, thickness
        if not initial:
            self.removeDockWidget(self.thumbnail_dock)
        self.addDockWidget(area, self.thumbnail_dock)
        self.resizeDocks([self.thumbnail_dock], [resize_size], resize_orientation)
        if was_visible is not None:
            self.thumbnail_dock.setVisible(was_visible)
        if hasattr(self, "vertical_orientation_action"):
            self.vertical_orientation_action.setChecked(orientation == "vertical")
            self.horizontal_orientation_action.setChecked(orientation == "horizontal")

    def toggle_thumbnail_dock(self, checked):
        self.thumbnails_btn.setChecked(checked)
        self.thumbnail_dock.setVisible(checked)
        if checked:
            self.thumbnail_dock.raise_()
            self._start_thumbnail_rendering()
            self._sync_thumbnail_selection()

    def _on_thumbnail_dock_visibility_changed(self, visible):
        self.thumbnails_btn.setChecked(visible)
        if visible:
            self._start_thumbnail_rendering()
            self._sync_thumbnail_selection()

    def _start_thumbnail_rendering(self):
        """Renders every page's thumbnail once, the first time the panel is
        actually shown -- not at reader startup, so opening a book never
        pays this cost unless the panel is actually opened. Runs a few
        pages at a time on a short timer rather than all at once, so even
        a very long book doesn't freeze the UI while thumbnails are
        generated; results stay in memory for the rest of this reader
        session, so toggling the panel off and back on doesn't re-render
        anything."""
        if self._thumbnails_rendered or self.doc is None:
            return
        self._thumbnails_rendered = True
        self._thumbnail_render_queue = list(range(self.page_count))
        self._thumbnail_render_timer.start()

    def _render_next_thumbnail_batch(self, batch_size=3):
        if not self._thumbnail_render_queue:
            self._thumbnail_render_timer.stop()
            return
        for _ in range(batch_size):
            if not self._thumbnail_render_queue:
                break
            page_idx = self._thumbnail_render_queue.pop(0)
            icon = self._render_thumbnail_icon(page_idx)
            item = self.thumbnail_list.item(page_idx)
            if icon is not None and item is not None:
                item.setIcon(icon)

    def _render_thumbnail_icon(self, page_idx):
        try:
            page = self.doc[page_idx]
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0:
                return None
            scale = min(self.THUMB_PANEL_SIZE[0] / rect.width, self.THUMB_PANEL_SIZE[1] / rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
            fmt = QImage.Format_RGB888 if pix.n < 4 else QImage.Format_RGBA8888
            image = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
            return QIcon(QPixmap.fromImage(image.copy()))
        except Exception:
            return None

    def _on_thumbnail_clicked(self, item):
        self.jump_to_page(self.thumbnail_list.row(item) + 1)

    def _sync_thumbnail_selection(self):
        """Highlights and scrolls to whichever page is now current --
        called every time render_page() runs, but a cheap no-op if the
        panel doesn't exist yet or has never been shown."""
        if not hasattr(self, "thumbnail_list") or not self.thumbnail_dock.isVisible():
            return
        target = self._pair_start(self.current_page) if self.two_page_mode else self.current_page
        item = self.thumbnail_list.item(target)
        if item is None:
            return
        self.thumbnail_list.blockSignals(True)
        self.thumbnail_list.setCurrentItem(item)
        self.thumbnail_list.blockSignals(False)
        self.thumbnail_list.scrollToItem(item)

    # ------------- Focus mode (hide all menus, like F11 in a browser) -------------
    def toggle_focus_mode(self):
        if self._focus_mode:
            self._exit_focus_mode()
        else:
            self._enter_focus_mode()

    def _enter_focus_mode(self):
        if self._focus_mode:
            return
        self._focus_mode = True
        # Remember exactly what was open so exiting restores it precisely,
        # rather than always reopening both panels (or leaving them however
        # they happened to end up) regardless of what you actually had.
        self._pre_focus_bookmarks_visible = self.bookmarks_dock.isVisible()
        self._pre_focus_thumbnails_visible = self.thumbnail_dock.isVisible()
        self.toolbar.hide()
        self.bookmarks_dock.hide()
        self.thumbnail_dock.hide()
        self.showFullScreen()

    def _exit_focus_mode(self):
        if not self._focus_mode:
            return
        self._focus_mode = False
        self.showNormal()
        self.toolbar.show()
        self.bookmarks_dock.setVisible(self._pre_focus_bookmarks_visible)
        self.thumbnail_dock.setVisible(self._pre_focus_thumbnails_visible)

    def _fav_label(self):
        return "\u2605 Favorited" if self.book["is_favorite"] else "\u2606 Favorite"

    def _finished_label(self):
        return "\u2713 Finished" if self.book["status"] == "finished" else "Mark Finished"

    # ------------- Rendering -------------
    def _update_mode_visibility(self):
        self.scroll_area.setVisible(not self.simple_text_mode)
        self.text_browser.setVisible(self.simple_text_mode)
        # Simple Text mode already supports selecting/copying its text
        # natively (it's a plain QTextBrowser) -- our custom drag-select
        # overlay only applies to the rendered page image.
        self.select_text_btn.setEnabled(not self.simple_text_mode)
        self.select_text_btn.setToolTip(
            "Text in Simple Text mode can already be selected and copied directly"
            if self.simple_text_mode else
            "Drag over text on the page to select it, then Ctrl+C or right-click to copy"
        )
        if self.simple_text_mode:
            self.text_overlay.hide()
        else:
            # The overlay stays visible whenever there's a rendered page to
            # show it over -- even outside Select Text mode -- so saved
            # highlights are always visible while reading normally, not
            # just while actively selecting. It only INTERCEPTS mouse
            # input (for making a new selection) while Select Text mode
            # is on; otherwise clicks pass through to the page underneath
            # so panning keeps working exactly as before.
            self.text_overlay.show()
            self.text_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, not self.select_text_mode)
        # Draw mode is exactly the same story as Select Text mode above --
        # meaningless in Simple Text mode, since there's no rendered page
        # image to draw on top of there, only plain extracted text.
        if self.simple_text_mode:
            self.drawing_overlay.hide()
        else:
            self.drawing_overlay.show()
            self.drawing_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, not self.draw_mode)
        self.draw_btn.setEnabled(not self.simple_text_mode)
        self.draw_btn.setToolTip(
            "Not available in Simple Text mode"
            if self.simple_text_mode else
            "Draw freehand or with simple shapes -- a manual way to mark up a "
            "page, like highlighting or annotating a normal textbook"
        )
        # Two-Page View is a rendered-image concept, so it doesn't apply to
        # Simple Text mode's plain extracted text either.
        self.two_page_btn.setEnabled(not self.simple_text_mode)
        self.two_page_btn.setToolTip(
            "Not available in Simple Text mode"
            if self.simple_text_mode else
            "Show two pages side by side, like a book spread -- handy on a wide screen"
        )

    def _compute_fit_zoom(self, page):
        """Zoom level that scales this page to fit the current viewport,
        preserving aspect ratio. Pages within a book can differ in size, so
        this is recalculated for every page rather than assumed constant."""
        rect = page.rect
        return self._fit_zoom_for_size(rect.width, rect.height)

    def _compute_fit_zoom_two_page(self, left_page, right_page):
        """Like _compute_fit_zoom, but fits BOTH pages side by side (their
        combined width, and the taller of the two heights) into the viewport."""
        left_rect = left_page.rect
        right_rect = right_page.rect if right_page is not None else left_rect
        combined_w = left_rect.width + PAGE_GAP + (right_rect.width if right_page is not None else 0)
        combined_h = max(left_rect.height, right_rect.height)
        return self._fit_zoom_for_size(combined_w, combined_h)

    def _fit_zoom_for_size(self, content_w, content_h):
        if content_w <= 0 or content_h <= 0:
            return 1.0
        viewport = self.scroll_area.viewport()
        avail_w = viewport.width() - VIEWPORT_MARGIN
        avail_h = viewport.height() - VIEWPORT_MARGIN
        if avail_w <= 0 or avail_h <= 0:
            return 1.0  # window not laid out yet; corrected on the next showEvent/resize
        zoom = min(avail_w / content_w, avail_h / content_h)
        return max(MIN_ZOOM, min(zoom, MAX_ZOOM))

    @staticmethod
    def _pair_start(page_index):
        """The left-hand page index of the two-page spread containing this
        page (spreads pair 0&1, 2&3, 4&5, ... -- the common convention most
        readers use without a separate "cover page alone" exception)."""
        return page_index - (page_index % 2)

    def render_page(self):
        if self.doc is None:
            return
        if self._draft_drawing_page is not None and self._draft_drawing_page != self.current_page:
            # Only a genuine page change (not a resize, zoom change, or
            # dark-mode toggle -- all of which also call render_page())
            # should discard an in-progress, not-yet-saved drawing.
            # current_page only changes for real navigation, so checking
            # it here -- rather than hooking every individual navigation
            # method -- catches every way of moving to a different page,
            # present or future, in one place.
            self.clear_draft_drawings()
        if self.simple_text_mode:
            page = self.doc[self.current_page]
            text = page.get_text("text").strip() or "(This page has no extractable text.)"
            self.text_browser.setStyleSheet(f"font-size: {self.font_size}pt; padding: 24px;")
            self.text_browser.setPlainText(text)
        elif self.two_page_mode:
            self._render_two_page_spread()
        else:
            self._render_single_page()

        self.page_spin.blockSignals(True)
        self.page_spin.setValue(self.current_page + 1)
        self.page_spin.blockSignals(False)
        self.db.update_progress(self.book_id, self.current_page)
        self._sync_thumbnail_selection()

    def _render_single_page(self):
        page = self.doc[self.current_page]
        zoom = self._compute_fit_zoom(page) if self.auto_fit else self.zoom
        self._current_render_zoom = zoom
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix)
        if self.dark_pages:
            pix.invert_irect(pix.irect)
        fmt = QImage.Format_RGB888 if pix.n < 4 else QImage.Format_RGBA8888
        image = QImage(pix.samples, pix.width, pix.height, pix.stride, fmt)
        self.page_label.setPixmap(QPixmap.fromImage(image.copy()))
        self._sync_overlay_geometry(pix.width, pix.height)

    def _render_two_page_spread(self):
        left_idx = self._pair_start(self.current_page)
        right_idx = left_idx + 1
        left_page = self.doc[left_idx]
        right_page = self.doc[right_idx] if right_idx < self.page_count else None

        zoom = self._compute_fit_zoom_two_page(left_page, right_page) if self.auto_fit else self.zoom
        self._current_render_zoom = zoom
        matrix = fitz.Matrix(zoom, zoom)

        left_pix = left_page.get_pixmap(matrix=matrix)
        right_pix = right_page.get_pixmap(matrix=matrix) if right_page is not None else None
        if self.dark_pages:
            left_pix.invert_irect(left_pix.irect)
            if right_pix is not None:
                right_pix.invert_irect(right_pix.irect)

        total_w = left_pix.width + PAGE_GAP + (right_pix.width if right_pix is not None else 0)
        total_h = max(left_pix.height, right_pix.height if right_pix is not None else 0)

        combined = QImage(total_w, total_h, QImage.Format_RGB888)
        combined.fill(QColor(60, 60, 60) if self.dark_pages else QColor(235, 235, 235))
        painter = QPainter(combined)
        left_fmt = QImage.Format_RGB888 if left_pix.n < 4 else QImage.Format_RGBA8888
        painter.drawImage(0, 0, QImage(left_pix.samples, left_pix.width, left_pix.height, left_pix.stride, left_fmt))
        if right_pix is not None:
            right_fmt = QImage.Format_RGB888 if right_pix.n < 4 else QImage.Format_RGBA8888
            painter.drawImage(
                left_pix.width + PAGE_GAP, 0,
                QImage(right_pix.samples, right_pix.width, right_pix.height, right_pix.stride, right_fmt),
            )
        painter.end()

        # Text selection needs this later (mouse events arrive well after
        # this render call returns) to know where the right page starts
        # in the combined image's pixel space.
        self._left_page_px_width = left_pix.width

        self.page_label.setPixmap(QPixmap.fromImage(combined))
        self._sync_overlay_geometry(total_w, total_h)

    def _sync_overlay_geometry(self, width, height):
        # A new page/zoom/spread invalidates any old highlight immediately.
        self.text_overlay.clear()
        self.selected_text = ""
        self.selection_popup.hide()
        self._pending_overlay_size = (width, height)
        self._load_saved_highlights_for_current_view()
        self._load_saved_drawings_for_current_view()
        self._check_no_selectable_text()
        # Deferred: page_label is resized to fill the scroll area's viewport
        # (setWidgetResizable(True)) and centers the pixmap within itself
        # (AlignCenter) whenever the page is smaller than the window --  so
        # the overlay has to be positioned at the pixmap's actual centered
        # offset within the label, not just the label's own (0, 0) origin,
        # or it ends up sitting over empty label space instead of the page
        # itself. But page_label's post-resize size isn't guaranteed to be
        # up to date synchronously right after setPixmap() -- same reason
        # _update_pan_cursor below already has to wait a beat -- so this
        # has to be computed after the pending layout pass actually finishes.
        QTimer.singleShot(0, self._apply_pending_overlay_geometry)
        QTimer.singleShot(0, self._update_pan_cursor)

    def _apply_pending_overlay_geometry(self):
        width, height = self._pending_overlay_size
        offset_x = max(0, (self.page_label.width() - width) // 2)
        offset_y = max(0, (self.page_label.height() - height) // 2)
        self.text_overlay.setGeometry(offset_x, offset_y, width, height)
        self.text_overlay.raise_()
        self.drawing_overlay.setGeometry(offset_x, offset_y, width, height)
        self.drawing_overlay.raise_()  # always on top -- Draw mode should win over text selection visually too

    # ------------- Navigation -------------
    def keyPressEvent(self, event):
        # Belt-and-suspenders alongside the Prev/Next QAction shortcuts:
        # guarantees page-turning always works even if some focused child
        # widget would otherwise swallow the key first -- checked against
        # the CURRENT (possibly user-customized) shortcut, not a hardcoded
        # Left/Right, so a remapped key isn't silently shadowed by the old
        # default still working here underneath it.
        if self._maybe_handle_page_turn_key(event):
            return
        if event.matches(QKeySequence.Copy) and self.selected_text:
            self.copy_selection()
            event.accept()
            return
        if event.matches(QKeySequence.SelectAll) and self.select_text_mode and not self.simple_text_mode:
            self.select_all_text()
            event.accept()
            return
        if self._focus_mode and event.key() == Qt.Key_Escape:
            # Standard convention (browsers do the same for fullscreen) --
            # works alongside F11/whatever toggle_focus_mode is bound to,
            # not instead of it.
            self._exit_focus_mode()
            event.accept()
            return
        super().keyPressEvent(event)

    def _maybe_handle_page_turn_key(self, event):
        """True (and turns the page) if `event` matches the current
        Prev/Next Page shortcut, checked against the live QAction shortcut
        rather than a hardcoded Left/Right so a user-remapped key is
        honored here too. Shared by keyPressEvent (the top-level fallback)
        and eventFilter (the actual fix): QScrollArea and QTextBrowser
        both default to a focus policy that lets them grab keyboard focus
        and both have their own built-in arrow-key handling (scrolling
        the view, moving a text cursor) that ACCEPTS the key itself,
        which stops it from ever bubbling up to this window's own
        keyPressEvent at all -- that's exactly the bug where page-turning
        stopped working once zoomed in (real scrollable content makes
        QScrollArea's own arrow-key scrolling kick in) or in Simple Text
        mode after clicking into the text. keyPressEvent's own check
        alone was never reachable in those cases; catching it earlier, in
        an event filter installed directly on those two widgets, is what
        actually closes the gap."""
        seq = QKeySequence(event.keyCombination())
        if seq.isEmpty():
            return False
        if seq == self.prev_action.shortcut():
            self.prev_page()
            event.accept()
            return True
        if seq == self.next_action.shortcut():
            self.next_page()
            event.accept()
            return True
        return False

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress and obj in (self.scroll_area, self.text_browser):
            if self._maybe_handle_page_turn_key(event):
                return True
        if event.type() == QEvent.Wheel and obj in (
            self.scroll_area.viewport(),
            self.text_browser.viewport(),
        ):
            return self._handle_wheel(event)
        if obj is self.page_label:
            if event.type() == QEvent.MouseButtonPress:
                if event.button() == Qt.RightButton:
                    return self._handle_reading_menu_press(event)
                return self._handle_pan_press(event)
            if event.type() == QEvent.MouseMove:
                return self._handle_pan_move(event)
            if event.type() == QEvent.MouseButtonRelease:
                if event.button() == Qt.RightButton:
                    return self._handle_reading_menu_release(event)
                return self._handle_pan_release(event)
        return super().eventFilter(obj, event)

    def _wheel_action_active(self, action, modifiers, buttons):
        """True if any currently-held gesture (a modifier key or a held
        mouse button) is configured -- see self.wheel_gesture_actions,
        cached by apply_shortcuts -- to perform `action` for this wheel
        event. Checking "is any gesture pointing at this action active"
        rather than "which single gesture is active" is what lets more
        than one gesture (by default, both Ctrl+Scroll and Right-Click
        held+Scroll) trigger the same action simultaneously, which is the
        intended behavior, not an edge case to resolve away."""
        held = {
            "ctrl_scroll": bool(modifiers & Qt.ControlModifier),
            "shift_scroll": bool(modifiers & Qt.ShiftModifier),
            "alt_scroll": bool(modifiers & Qt.AltModifier),
            "middle_click_scroll": bool(buttons & Qt.MiddleButton),
            "right_click_scroll": bool(buttons & Qt.RightButton),
        }
        return any(
            is_held and self.wheel_gesture_actions.get(gesture_id) == action
            for gesture_id, is_held in held.items()
        )

    def _handle_wheel(self, event):
        """Zoom and page-turn-while-zoomed are both configurable gestures
        (see WHEEL_GESTURES in the shortcuts module -- self.wheel_gesture_
        actions, cached by apply_shortcuts, is the live {gesture_id:
        action} mapping). By default, Ctrl+Scroll and Right-Click
        held+Scroll both zoom, and Middle-Click held+Scroll turns the page
        while zoomed in, but any of the five available gestures (Ctrl/
        Shift/Alt modifier, or Middle/Right mouse button held) can be
        reassigned to either action or turned off entirely. Plain scroll
        (satisfying no gesture currently assigned to either action) turns
        pages only when the page is fit to the screen (nothing to
        accidentally scroll past) or in Simple Text mode (scroll past the
        top/bottom edge); once zoomed in manually, plain scroll only pans,
        and an active page-turn gesture is the explicit "turn the page
        anyway" override."""
        if self.doc is None:
            return False

        modifiers = event.modifiers()
        buttons = event.buttons()
        if buttons & Qt.RightButton:
            # Whatever else this wheel event does, the right button was
            # just used for a scroll gesture, not a plain click -- the
            # eventual release must not also pop open the reading context
            # menu on top of it. Set unconditionally (not just when this
            # scroll ends up triggering an action): the user scrolling at
            # all while holding right-click reads as "this was a
            # deliberate hold", regardless of what it's currently
            # configured to do.
            self._right_click_held_through_scroll = True

        if self._wheel_action_active(WHEEL_ACTION_ZOOM, modifiers, buttons):
            delta = event.angleDelta().y()
            if delta > 0:
                self.increase_text_size()
            elif delta < 0:
                self.decrease_text_size()
            return True

        delta_y = event.angleDelta().y()
        if delta_y == 0:
            return False

        if self.simple_text_mode:
            vbar = self.text_browser.verticalScrollBar()
            at_top = vbar.value() <= vbar.minimum()
            at_bottom = vbar.value() >= vbar.maximum()
            if delta_y > 0 and not at_top:
                return False  # room to scroll up within the text -- let it scroll normally
            if delta_y < 0 and not at_bottom:
                return False  # room to scroll down within the text -- let it scroll normally
            if delta_y > 0:
                self.prev_page()
            else:
                self.next_page()
            return True

        if self.auto_fit:
            # Page fits the screen entirely -- nothing to accidentally scroll
            # past, so plain scroll always turns the page.
            if delta_y > 0:
                self.prev_page()
            else:
                self.next_page()
            return True

        # Zoomed in manually: plain scroll only pans, never changes pages by
        # accident. An active page-turn gesture is the explicit "turn the
        # page anyway" override.
        if self._wheel_action_active(WHEEL_ACTION_PAGE_TURN, modifiers, buttons):
            if delta_y > 0:
                self.prev_page()
            else:
                self.next_page()
            return True

        return False  # let the scroll area pan normally

    # ------------- Right-click reading menu (page_label only -- Select
    # Text mode and Draw mode's own overlays handle right-click themselves,
    # for editing/deleting whatever was clicked on) -------------
    def _handle_reading_menu_press(self, event):
        self._right_click_held_through_scroll = False
        return True  # consumed -- nothing else on this widget cares about a right-press

    def _handle_reading_menu_release(self, event):
        if not self._right_click_held_through_scroll:
            self._show_reading_context_menu(event)
        return True

    def _show_reading_context_menu(self, event):
        """A right-click on the page while just reading (neither Select
        Text nor Draw mode active) -- exists so the handful of actions
        someone's most likely to reach for mid-book are reachable with the
        mouse alone, without needing to put the book down for a keyboard."""
        menu = QMenu(self)
        select_text_action = menu.addAction("Select Text")
        draw_action = menu.addAction("Draw")
        bookmark_action = menu.addAction("Add Bookmark")
        chosen = menu.exec(event.globalPosition().toPoint())
        if chosen is select_text_action:
            self.select_text_btn.click()
        elif chosen is draw_action:
            self.draw_btn.click()
        elif chosen is bookmark_action:
            self.add_bookmark()

    # ------------- Click-and-drag panning (zoomed-in pages) -------------
    def _handle_pan_press(self, event):
        if event.button() != Qt.LeftButton or self.simple_text_mode:
            return False
        hbar = self.scroll_area.horizontalScrollBar()
        vbar = self.scroll_area.verticalScrollBar()
        if hbar.maximum() <= hbar.minimum() and vbar.maximum() <= vbar.minimum():
            return False  # page fits entirely -- nothing to pan
        self._panning = True
        self._pan_start_pos = event.globalPosition().toPoint()
        self._pan_start_h = hbar.value()
        self._pan_start_v = vbar.value()
        self.page_label.setCursor(Qt.ClosedHandCursor)
        return True

    def _handle_pan_move(self, event):
        if not self._panning:
            return False
        current = event.globalPosition().toPoint()
        delta = current - self._pan_start_pos
        self.scroll_area.horizontalScrollBar().setValue(self._pan_start_h - delta.x())
        self.scroll_area.verticalScrollBar().setValue(self._pan_start_v - delta.y())
        return True

    def _handle_pan_release(self, event):
        if event.button() != Qt.LeftButton or not self._panning:
            return False
        self._panning = False
        self._update_pan_cursor()
        return True

    def _update_pan_cursor(self):
        hbar = self.scroll_area.horizontalScrollBar()
        vbar = self.scroll_area.verticalScrollBar()
        scrollable = hbar.maximum() > hbar.minimum() or vbar.maximum() > vbar.minimum()
        self.page_label.setCursor(Qt.OpenHandCursor if scrollable else Qt.ArrowCursor)

    # ------------- Text selection / copy -------------
    def toggle_select_text_mode(self, checked):
        self.select_text_btn.setChecked(checked)
        self.select_text_mode = checked
        if checked and self.draw_mode:
            # mutually exclusive with Draw mode -- see toggle_draw_mode
            self.draw_btn.setChecked(False)
            self.toggle_draw_mode(False)
        if not self.simple_text_mode:
            self.text_overlay.show()  # stays visible either way, to show saved highlights
            self.text_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, not checked)
        if not checked:
            self.text_overlay.clear()
            self.selected_text = ""
            self.selection_popup.hide()
            self._update_pan_cursor()
        else:
            self._check_no_selectable_text()

    def _get_page_chars(self, page_index):
        """get_text("rawdict") is a real (if fast) PDF-parsing call, and
        this can be invoked many times per second while dragging -- cache
        each page's character list the first time it's needed rather
        than re-extracting it on every mouse-move event."""
        if page_index not in self._chars_cache:
            self._chars_cache[page_index] = chars_from_rawdict(self.doc[page_index].get_text("rawdict"))
        return self._chars_cache[page_index]

    def _page_and_point_at(self, overlay_pos):
        """Maps a point in the overlay's pixel coordinates (same space as
        the rendered pixmap) to (page_index, (x, y)) in that page's own
        PDF coordinate space -- accounting for the current zoom, and in
        Two-Page View, for which of the two pages the point actually
        falls on and that page's x-offset within the combined image.
        This is exactly the mapping the old implementation got wrong: it
        always resolved to self.current_page (the left page), regardless
        of where the point actually was."""
        zoom = self._current_render_zoom or 1.0
        x, y = overlay_pos.x(), overlay_pos.y()
        if not self.two_page_mode:
            return self.current_page, (x / zoom, y / zoom)

        left_idx = self._pair_start(self.current_page)
        right_idx = left_idx + 1
        boundary = self._left_page_px_width + PAGE_GAP / 2
        if x < boundary or right_idx >= self.page_count:
            return left_idx, (x / zoom, y / zoom)
        x_offset = self._left_page_px_width + PAGE_GAP
        return right_idx, ((x - x_offset) / zoom, y / zoom)

    def _page_x_offset_px(self, page_index):
        """Inverse of the offset applied in _page_and_point_at -- how far
        (in overlay pixels) this page's own (0, 0) sits from the combined
        image's left edge. 0 in single-page mode or for the left page of
        a spread; the left page's rendered width plus the gap for the
        right page."""
        if not self.two_page_mode or page_index == self._pair_start(self.current_page):
            return 0
        return self._left_page_px_width + PAGE_GAP

    def update_text_selection(self, start_pos, end_pos, finished):
        """Recomputes the highlighted rectangles and pending selected text
        for a drag from start_pos to end_pos (both in overlay pixel
        coordinates, in either order). Called continuously while dragging
        (finished=False, for a live-updating highlight -- not just
        computed once when the mouse button comes up) and once more on
        release (finished=True)."""
        if self.doc is None:
            return
        rect = QRectF(start_pos, end_pos).normalized()
        if finished and rect.width() < 3 and rect.height() < 3:
            # A near-zero-size drag is a click, not a real selection --
            # clear any existing highlight rather than "select" a sliver.
            self.text_overlay.set_highlight_rects([])
            self.selected_text = ""
            self.selection_popup.hide()
            return

        start_page, start_pt = self._page_and_point_at(start_pos)
        end_page, end_pt = self._page_and_point_at(end_pos)
        chars_by_page = {start_page: self._get_page_chars(start_page)}
        if end_page != start_page:
            chars_by_page[end_page] = self._get_page_chars(end_page)

        page_ranges = resolve_multi_page_selection(chars_by_page, start_page, start_pt, end_page, end_pt)
        self._apply_page_ranges(page_ranges, chars_by_page)
        self._last_selection_pos = end_pos

    def select_word_or_paragraph_at(self, pos, paragraph):
        """Double-click (paragraph=False) selects the whole word under
        pos; triple-click (paragraph=True) selects its whole paragraph,
        however many lines it wraps to. pos is in overlay pixel
        coordinates, same as update_text_selection."""
        if self.doc is None:
            return
        page_idx, (x, y) = self._page_and_point_at(pos)
        chars = self._get_page_chars(page_idx)
        if not chars:
            return
        idx = char_index_at_point(chars, x, y)
        if idx is None:
            return
        bounds = paragraph_bounds_at_index(chars, idx) if paragraph else word_bounds_at_index(chars, idx)
        if bounds is None:
            return
        self._apply_page_ranges([(page_idx, *bounds)], {page_idx: chars})
        self._last_selection_pos = pos

    def select_all_text(self):
        """Selects everything on the current page (or both pages of the
        current spread, in Two-Page View)."""
        if self.doc is None:
            return
        if self.two_page_mode:
            left_idx = self._pair_start(self.current_page)
            right_idx = left_idx + 1
            start_page, end_page = left_idx, (right_idx if right_idx < self.page_count else left_idx)
        else:
            start_page = end_page = self.current_page

        chars_by_page = {start_page: self._get_page_chars(start_page)}
        if end_page != start_page:
            chars_by_page[end_page] = self._get_page_chars(end_page)
        page_ranges = resolve_multi_page_selection(chars_by_page, start_page, NEAR_POINT, end_page, FAR_POINT)
        self._apply_page_ranges(page_ranges, chars_by_page)
        if self.selected_text:
            self.copy_feedback_label.setText(f"{len(self.selected_text)} characters selected")
            QTimer.singleShot(2500, lambda: self.copy_feedback_label.setText(""))

    def _apply_page_ranges(self, page_ranges, chars_by_page):
        """Shared by update_text_selection, select_word_or_paragraph_at,
        and select_all_text: turns a list of (page_index, start_char,
        end_char) ranges into the actual selected text and the overlay's
        highlight rectangles (each scaled by zoom and shifted by that
        page's x-offset in the combined image, so a right-page rect in
        Two-Page View lands in the right place)."""
        self.selected_text = combined_selected_text(chars_by_page, page_ranges)
        self._last_selection_page_ranges = page_ranges  # used by save_selection_as_highlight
        self._last_selection_chars_by_page = chars_by_page
        highlight_rects = []
        zoom = self._current_render_zoom or 1.0
        for (page_idx, s, e) in page_ranges:
            x_offset = self._page_x_offset_px(page_idx)
            for (rx0, ry0, rx1, ry1) in selection_rects(chars_by_page[page_idx], s, e):
                highlight_rects.append(QRectF(
                    rx0 * zoom + x_offset, ry0 * zoom, (rx1 - rx0) * zoom, (ry1 - ry0) * zoom,
                ))
        self.text_overlay.set_highlight_rects(highlight_rects)

    # ------------- Saved (persistent) highlights -------------
    def _visible_page_indices(self):
        """The page index(es) currently on screen -- just current_page in
        single-page mode, or the current spread's pages in Two-Page View."""
        if not self.two_page_mode:
            return [self.current_page]
        left_idx = self._pair_start(self.current_page)
        right_idx = left_idx + 1
        return [left_idx] + ([right_idx] if right_idx < self.page_count else [])

    def _load_saved_highlights_for_current_view(self):
        """Loads every saved highlight for whichever page(s) are currently
        visible, converts each one's stored PDF-point rects to this
        render's pixel space, and hands them to the overlay to draw.
        Called on every render (new page, zoom change, or spread), so a
        saved highlight always shows up correctly regardless of zoom
        level or which page it happens to fall on in Two-Page View."""
        if self.doc is None:
            return
        zoom = self._current_render_zoom or 1.0

        overlay_highlights = []
        for page_idx in self._visible_page_indices():
            x_offset = self._page_x_offset_px(page_idx)
            for h in self.db.get_highlights_for_page(self.book_id, page_idx):
                rects = [
                    QRectF(x0 * zoom + x_offset, y0 * zoom, (x1 - x0) * zoom, (y1 - y0) * zoom)
                    for (x0, y0, x1, y1) in h["rects"]
                ]
                overlay_highlights.append({
                    "id": h["id"], "color": QColor(h["color"]), "rects": rects,
                    "accent_color": QColor(h.get("accent_color") or h["color"]),
                    "style": h.get("style") or "fill",
                })
        self.text_overlay.set_saved_highlights(overlay_highlights)

    # ------------- Freehand / shape drawing -------------

    def _build_draw_toolbar(self):
        """A second toolbar row, hidden until Draw mode is turned on --
        tool choice, color, opacity, and pen/stroke width. Kept as its
        own toolbar rather than folded into the main one so it doesn't
        take up permanent space for a mode most viewing sessions never
        use."""
        self.addToolBarBreak()
        self.draw_toolbar = QToolBar("Drawing Tools", self)
        self.draw_toolbar.setMovable(False)
        self.addToolBar(self.draw_toolbar)

        self._draw_tool_buttons = {}
        tool_group = QButtonGroup(self)
        tool_group.setExclusive(True)
        tool_labels = [
            ("pen", "Pen"), ("rectangle", "Box"), ("ellipse", "Circle"),
            ("triangle", "Triangle"), ("line", "Line"),
        ]
        for tool_id, label in tool_labels:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(tool_id == self.draw_tool)
            btn.clicked.connect(lambda _checked, t=tool_id: self._set_draw_tool(t))
            tool_group.addButton(btn)
            self._draw_tool_buttons[tool_id] = btn
            self.draw_toolbar.addWidget(btn)

        self.draw_toolbar.addSeparator()

        self.draw_toolbar.addWidget(QLabel(" Color: "))
        self.draw_color_swatch = QLabel()
        self.draw_color_swatch.setFixedSize(22, 22)
        self.draw_color_swatch.setCursor(Qt.PointingHandCursor)
        self.draw_color_swatch.setToolTip("Choose drawing color")
        self._update_draw_color_swatch()
        self.draw_color_swatch.mousePressEvent = lambda _event: self._choose_draw_color()
        self.draw_toolbar.addWidget(self.draw_color_swatch)

        self.draw_toolbar.addWidget(QLabel(" Opacity: "))
        self.draw_opacity_slider = QSlider(Qt.Horizontal)
        self.draw_opacity_slider.setRange(5, 100)
        self.draw_opacity_slider.setValue(round(self.draw_opacity * 100))
        self.draw_opacity_slider.setFixedWidth(100)
        self.draw_opacity_slider.setToolTip("Drawing opacity")
        self.draw_opacity_slider.valueChanged.connect(self._set_draw_opacity)
        self.draw_toolbar.addWidget(self.draw_opacity_slider)
        self.draw_opacity_label = QLabel(f"{self.draw_opacity_slider.value()}%")
        self.draw_opacity_label.setFixedWidth(36)
        self.draw_toolbar.addWidget(self.draw_opacity_label)

        self.draw_toolbar.addWidget(QLabel(" Width: "))
        self.draw_width_spin = QSpinBox()
        self.draw_width_spin.setRange(1, 20)
        self.draw_width_spin.setValue(round(self.draw_stroke_width))
        self.draw_width_spin.setToolTip("Pen/outline width")
        self.draw_width_spin.valueChanged.connect(self._set_draw_stroke_width)
        self.draw_toolbar.addWidget(self.draw_width_spin)

        self.draw_toolbar.addSeparator()

        self.draw_undo_btn = QPushButton("\u21b6 Undo")
        self.draw_undo_btn.setToolTip("Undo the last stroke or shape (Ctrl+Z)")
        self.draw_undo_btn.setEnabled(False)
        self.draw_undo_btn.clicked.connect(self.undo_last_drawing_stroke)
        self.draw_toolbar.addWidget(self.draw_undo_btn)

        self.draw_clear_btn = QPushButton("Clear")
        self.draw_clear_btn.setToolTip("Discard everything drawn since the last save on this page")
        self.draw_clear_btn.setEnabled(False)
        self.draw_clear_btn.clicked.connect(self.clear_draft_drawings)
        self.draw_toolbar.addWidget(self.draw_clear_btn)

        self.draw_toolbar.addSeparator()

        self.draw_save_btn = QPushButton("Save Drawn Highlight")
        self.draw_save_btn.setToolTip(
            "Save what you've drawn on this page permanently -- until then, it's "
            "just a draft: leaving Draw mode or the page discards it"
        )
        self.draw_save_btn.setEnabled(False)
        self.draw_save_btn.clicked.connect(self.save_drawn_highlights)
        self.draw_toolbar.addWidget(self.draw_save_btn)

        self.draw_toolbar.addSeparator()
        hint = QLabel(" Right-click a saved drawing to delete it ")
        hint.setEnabled(False)  # renders in the palette's disabled/dim text color, a subtle hint not a warning
        self.draw_toolbar.addWidget(hint)

        self.draw_toolbar.setVisible(False)

    def _update_draw_color_swatch(self):
        self.draw_color_swatch.setStyleSheet(
            f"background-color: {self.draw_color}; border: 1px solid palette(mid);"
        )

    def _choose_draw_color(self):
        color = QColorDialog.getColor(QColor(self.draw_color), self, "Choose Drawing Color")
        if color.isValid():
            self.draw_color = color.name()
            self.db.set_setting("last_draw_color", self.draw_color)
            self._update_draw_color_swatch()

    def _set_draw_tool(self, tool_id):
        self.draw_tool = tool_id
        self.db.set_setting("last_draw_tool", tool_id)
        self.drawing_overlay.clear_live_stroke()

    def _set_draw_opacity(self, value):
        self.draw_opacity = value / 100.0
        self.db.set_setting("last_draw_opacity", self.draw_opacity)
        self.draw_opacity_label.setText(f"{value}%")

    def _set_draw_stroke_width(self, value):
        self.draw_stroke_width = float(value)
        self.db.set_setting("last_draw_stroke_width", self.draw_stroke_width)

    def _update_draw_action_buttons(self):
        has_draft = self.drawing_overlay.has_draft()
        self.draw_undo_btn.setEnabled(has_draft)
        self.draw_clear_btn.setEnabled(has_draft)
        self.draw_save_btn.setEnabled(has_draft)

    def notify_draft_drawing_added(self):
        """Called by DrawingOverlay right after a finished stroke/shape
        lands in its draft list -- the overlay owns the draft data itself
        (see its docstring), but button enabled-state lives here on
        ReaderWindow, so it needs telling when that data changes."""
        self._draft_drawing_page = self.current_page
        self._update_draw_action_buttons()

    def undo_last_drawing_stroke(self):
        self.drawing_overlay.undo_last_draft()
        self._update_draw_action_buttons()

    def _undo_drawing_shortcut_triggered(self):
        # scoped to Draw mode explicitly, rather than relying only on
        # undo_last_draft()'s own "nothing to undo" no-op -- Ctrl+Z
        # should read as "undo my drawing", not fire (harmlessly, but
        # confusingly) any time it's pressed regardless of context
        if self.draw_mode:
            self.undo_last_drawing_stroke()

    def clear_draft_drawings(self):
        self.drawing_overlay.clear_draft()
        self._draft_drawing_page = None
        self._update_draw_action_buttons()

    def save_drawn_highlights(self):
        """Persists every drawing currently in the draft (drawn since
        the last save on this page, not yet in the database) -- the
        drawing equivalent of clicking Save Highlight after selecting
        text: nothing before this point was ever permanent."""
        if self.doc is None:
            return
        draft = self.drawing_overlay.get_draft_drawings()
        if not draft:
            return
        zoom = self._current_render_zoom or 1.0
        for item in draft:
            screen_points = item["points"]
            page_idx, _first_pdf_point = self._page_and_point_at(screen_points[0])
            x_offset = self._page_x_offset_px(page_idx)
            pdf_points = [
                [(p.x() - x_offset) / zoom, p.y() / zoom]
                for p in screen_points
            ]
            self.db.add_drawing(
                self.book_id, page_idx, item["tool"], item["color"].name(),
                item["opacity"], item["stroke_width"], pdf_points,
            )
        self.drawing_overlay.clear_draft()
        self._draft_drawing_page = None
        self._load_saved_drawings_for_current_view()
        self._update_draw_action_buttons()
        self.refresh_highlights()

    def toggle_draw_mode(self, checked):
        self.draw_btn.setChecked(checked)
        self.draw_mode = checked
        self.draw_toolbar.setVisible(checked)
        if checked and self.select_text_mode:
            # mutually exclusive with Select Text mode -- both bind plain
            # left-click-drag on the page to different things, so only
            # one can own the mouse at a time
            self.select_text_btn.setChecked(False)
            self.toggle_select_text_mode(False)
        if not self.simple_text_mode:
            self.drawing_overlay.show()  # stays visible either way, to show saved drawings
            self.drawing_overlay.setAttribute(Qt.WA_TransparentForMouseEvents, not checked)
            self.drawing_overlay.raise_()
        if not checked:
            self.drawing_overlay.clear_live_stroke()
            # anything drawn but not explicitly saved is gone the moment
            # you leave Draw mode -- the same "never became permanent"
            # fate as a text selection you never clicked Save Highlight on
            self.clear_draft_drawings()
        self._update_pan_cursor()

    def delete_drawing(self, drawing_id):
        self.db.delete_drawing(drawing_id)
        self._load_saved_drawings_for_current_view()
        self.refresh_highlights()

    def edit_drawing(self, drawing_id):
        drawing = next((d for d in self.db.get_drawings(self.book_id) if d["id"] == drawing_id), None)
        if drawing is None:
            return
        dialog = DrawingDialog(
            "Edit Drawing", drawing["color"], drawing["opacity"], drawing["stroke_width"], parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        color, opacity, stroke_width = dialog.result_values()
        self.db.update_drawing(drawing_id, color.name(), opacity, stroke_width)
        self._load_saved_drawings_for_current_view()
        self.refresh_highlights()

    def _load_saved_drawings_for_current_view(self):
        """Loads every saved drawing for whichever page(s) are currently
        visible and hands them to the overlay in this render's pixel
        space -- called on every render alongside
        _load_saved_highlights_for_current_view, for the same reason."""
        if self.doc is None:
            return
        zoom = self._current_render_zoom or 1.0

        overlay_drawings = []
        for page_idx in self._visible_page_indices():
            x_offset = self._page_x_offset_px(page_idx)
            for d in self.db.get_drawings_for_page(self.book_id, page_idx):
                points = [
                    QPointF(x * zoom + x_offset, y * zoom)
                    for (x, y) in d["points"]
                ]
                overlay_drawings.append({
                    "id": d["id"], "tool": d["tool"], "color": QColor(d["color"]),
                    "opacity": d["opacity"], "stroke_width": d["stroke_width"] * zoom,
                    "points": points,
                })
        self.drawing_overlay.set_saved_drawings(overlay_drawings)

    def _check_no_selectable_text(self):
        """While Select Text mode is on, if the current page (or both
        pages of a spread) has no extractable text at all -- most likely
        a scanned image with no OCR text layer -- let the user know via
        the same feedback label used for copy/select-all messages,
        instead of leaving them to wonder why dragging over the page
        silently does nothing."""
        if self.doc is None or not self.select_text_mode:
            return
        has_text = any(self._get_page_chars(p) for p in self._visible_page_indices())
        if not has_text:
            self.copy_feedback_label.setText(
                "No selectable text on this page \u2014 it may be a scanned image"
            )
            QTimer.singleShot(4000, lambda: self.copy_feedback_label.setText(""))

    def save_selection_as_highlight(self):
        if not self.selected_text or not self._last_selection_page_ranges:
            return
        dialog = HighlightDialog(
            "Save Highlight", "", self.last_highlight_color,
            initial_accent_color=self.last_highlight_accent_color, parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        custom_name, color, accent_color, style = dialog.result_values()
        chars_by_page = self._last_selection_chars_by_page
        for (page_idx, s, e) in self._last_selection_page_ranges:
            chars = chars_by_page.get(page_idx)
            if not chars:
                continue
            rects = selection_rects(chars, s, e)
            text = selected_text_for_range(chars, s, e)
            label = custom_name or self._default_highlight_label(page_idx)
            self.db.add_highlight(
                self.book_id, page_idx, color.name(), rects, text=text, label=label,
                style=style, accent_color=accent_color.name(),
            )
        self._remember_last_highlight_colors(color, accent_color)
        self.selected_text = ""
        self.text_overlay.set_highlight_rects([])
        self.selection_popup.hide()
        self._load_saved_highlights_for_current_view()
        self._load_saved_drawings_for_current_view()
        self.refresh_highlights()

    def _remember_last_highlight_colors(self, color, accent_color):
        self.last_highlight_color = color.name()
        self.last_highlight_accent_color = accent_color.name()
        self.db.set_setting("last_highlight_color", self.last_highlight_color)
        self.db.set_setting("last_highlight_accent_color", self.last_highlight_accent_color)

    def _default_highlight_label(self, page_idx):
        """"Page N" for the first highlight on a page when the user
        doesn't type a name of their own; "Page N - 1", "Page N - 2", etc.
        for subsequent ones on that same page, so they stay distinguishable
        in the sidebar list."""
        existing_count = len(self.db.get_highlights_for_page(self.book_id, page_idx))
        if existing_count == 0:
            return f"Page {page_idx + 1}"
        return f"Page {page_idx + 1} - {existing_count}"

    def delete_highlight(self, highlight_id):
        self.db.delete_highlight(highlight_id)
        self._load_saved_highlights_for_current_view()
        self._load_saved_drawings_for_current_view()
        self.refresh_highlights()

    def edit_highlight(self, highlight_id):
        highlight = next((h for h in self.db.get_highlights(self.book_id) if h["id"] == highlight_id), None)
        if highlight is None:
            return
        dialog = HighlightDialog(
            "Edit Highlight", highlight["label"], highlight["color"],
            initial_accent_color=highlight.get("accent_color") or highlight["color"],
            initial_style=highlight.get("style") or "fill", text_preview=highlight["text"], parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        name, color, accent_color, style = dialog.result_values()
        label = name or f"Page {highlight['page_number'] + 1}"
        self.db.update_highlight_label(highlight_id, label)
        self.db.update_highlight_color(highlight_id, color.name())
        self.db.update_highlight_accent_color(highlight_id, accent_color.name())
        self.db.update_highlight_style(highlight_id, style)
        self._remember_last_highlight_colors(color, accent_color)
        self._load_saved_highlights_for_current_view()
        self._load_saved_drawings_for_current_view()
        self.refresh_highlights()

    def choose_default_highlight_color(self):
        color = QColorDialog.getColor(QColor(self.highlight_color), self, "Default Highlight Color")
        if not color.isValid():
            return
        self.highlight_color = color.name()
        self.db.set_setting("highlight_color", self.highlight_color)
        live_color = QColor(self.highlight_color)
        live_color.setAlpha(90)
        self.text_overlay.set_live_color(live_color)



    def copy_selection(self):
        if not self.selected_text:
            return
        QApplication.clipboard().setText(self.selected_text)
        self._flash_copy_feedback(len(self.selected_text))

    def _flash_copy_feedback(self, n_chars):
        self.copy_feedback_label.setText(f"Copied {n_chars} character{'s' if n_chars != 1 else ''}")
        QTimer.singleShot(2500, lambda: self.copy_feedback_label.setText(""))

    def show_selection_popup(self):
        if not self.selected_text or self._last_selection_pos is None:
            self.selection_popup.hide()
            return
        global_pt = self.text_overlay.mapToGlobal(self._last_selection_pos.toPoint())
        local_pt = self.mapFromGlobal(global_pt)
        self.selection_popup.show_near(local_pt)

    def search_selection_in_book(self):
        if not self.selected_text:
            return
        query = " ".join(self.selected_text.split())  # collapse newlines/extra whitespace to one line
        if self._search_dialog is None:
            self._search_dialog = TextSearchDialog(self.db, self._handle_search_result, self)
        self._search_dialog.query_edit.setText(query)
        self._search_dialog.start_search()
        self._search_dialog.show()
        self._search_dialog.raise_()
        self._search_dialog.activateWindow()

    def _handle_search_result(self, book_id, page_number):
        if book_id == self.book_id:
            self.jump_to_page(page_number + 1)
            self.raise_()
            self.activateWindow()
        elif self.open_book_at_page:
            # A result in a DIFFERENT book -- this window only ever owns
            # the one book, so hand off to the library window's own
            # open-any-book logic (threaded through at construction time).
            self.open_book_at_page(book_id, page_number)

    def prev_page(self):
        if self.two_page_mode:
            prev_left = self._pair_start(self.current_page) - 2
            if prev_left >= 0:
                self.current_page = prev_left
                self.render_page()
        elif self.current_page > 0:
            self.current_page -= 1
            self.render_page()

    def next_page(self):
        if self.two_page_mode:
            next_left = self._pair_start(self.current_page) + 2
            if next_left < self.page_count:
                self.current_page = next_left
                self.render_page()
        elif self.current_page < self.page_count - 1:
            self.current_page += 1
            self.render_page()

    def jump_to_page(self, value):
        page = value - 1
        if not (0 <= page < self.page_count):
            return
        target = self._pair_start(page) if self.two_page_mode else page
        if target != self.current_page:
            self.current_page = target
            self.render_page()

    # ------------- View options -------------
    def increase_text_size(self):
        if self.simple_text_mode:
            self.font_size = min(self.font_size + 1, 48)
            self.db.set_setting("reader_font_size", self.font_size)
        else:
            self._leave_auto_fit_if_needed()
            self.zoom = min(round(self.zoom + 0.1, 2), MAX_ZOOM)
            self.db.set_setting("reader_zoom", self.zoom)
        self.render_page()

    def decrease_text_size(self):
        if self.simple_text_mode:
            self.font_size = max(self.font_size - 1, 8)
            self.db.set_setting("reader_font_size", self.font_size)
        else:
            self._leave_auto_fit_if_needed()
            self.zoom = max(round(self.zoom - 0.1, 2), MIN_ZOOM)
            self.db.set_setting("reader_zoom", self.zoom)
        self.render_page()

    def _leave_auto_fit_if_needed(self):
        """Manually adjusting zoom overrides auto-fit; start from the size the
        page is currently showing at so the change feels continuous."""
        if not self.auto_fit or self.doc is None:
            return
        if self.two_page_mode:
            left_idx = self._pair_start(self.current_page)
            right_idx = left_idx + 1
            left_page = self.doc[left_idx]
            right_page = self.doc[right_idx] if right_idx < self.page_count else None
            self.zoom = self._compute_fit_zoom_two_page(left_page, right_page)
        else:
            self.zoom = self._compute_fit_zoom(self.doc[self.current_page])
        self.auto_fit = False
        self.fit_btn.setChecked(False)
        self.db.set_setting("reader_auto_fit", "0")

    def toggle_auto_fit(self, checked):
        self.fit_btn.setChecked(checked)
        self.auto_fit = checked
        self.db.set_setting("reader_auto_fit", "1" if checked else "0")
        self.render_page()

    def toggle_simple_text(self, checked):
        self.simple_btn.setChecked(checked)
        self.simple_text_mode = checked
        self.db.set_setting("reader_text_mode", "simple" if checked else "normal")
        self._update_mode_visibility()
        self.render_page()

    def toggle_dark_mode(self, checked):
        from PySide6.QtWidgets import QApplication

        self.dark_btn.setChecked(checked)
        self.dark_mode = checked
        theme = "dark" if checked else "light"
        self.db.set_setting("theme", theme)
        QApplication.instance().setStyleSheet(DARK_THEME if checked else LIGHT_THEME)

    def toggle_dark_pages(self, checked):
        self.dark_pages_btn.setChecked(checked)
        self.dark_pages = checked
        self.db.set_setting("reader_dark_pages", "1" if checked else "0")
        self.render_page()

    def toggle_two_page_mode(self, checked):
        self.two_page_btn.setChecked(checked)
        self.two_page_mode = checked
        self.db.set_setting("reader_two_page", "1" if checked else "0")
        if checked:
            # Snap to a pairing boundary so the spread shows sensible pages
            # immediately, rather than the single page you happened to be on.
            self.current_page = self._pair_start(self.current_page)
        self.render_page()

    def toggle_favorite(self):
        self.db.toggle_favorite(self.book_id)
        self.book = self.db.get_book(self.book_id)
        self.fav_btn.setText(self._fav_label())

    def toggle_finished(self, checked):
        self.finished_btn.setChecked(checked)
        status = "finished" if checked else "reading"
        self.db.set_status(self.book_id, status)
        self.book = self.db.get_book(self.book_id)
        self.finished_btn.setText(self._finished_label())

    # ------------- Bookmarks -------------
    def add_bookmark(self):
        label, ok = QInputDialog.getText(self, "Add bookmark", "Label (optional):")
        if not ok:
            return
        self.db.add_bookmark(self.book_id, self.current_page, label.strip())
        self.refresh_bookmarks()

    def refresh_bookmarks(self):
        self.bookmark_list.clear()
        for bm in self.db.get_bookmarks(self.book_id):
            text = f"Page {bm['page_number'] + 1}"
            if bm["label"]:
                text += f" \u2014 {bm['label']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, bm["id"])
            item.setData(Qt.UserRole + 1, bm["page_number"])
            self.bookmark_list.addItem(item)

    def jump_to_bookmark(self, item):
        self.current_page = item.data(Qt.UserRole + 1)
        self.render_page()

    def remove_selected_bookmark(self):
        item = self.bookmark_list.currentItem()
        if not item:
            return
        self.db.delete_bookmark(item.data(Qt.UserRole))
        self.refresh_bookmarks()

    def _get_all_annotations(self):
        """Every highlight and drawing in this book, as a single
        page-ordered list of dicts -- each tagged with "kind"
        ("highlight" or "drawing") so callers can branch on how to
        display/edit/delete an entry, without needing to care that the
        two live in separate database tables under the hood. (They do,
        deliberately: a drawn ellipse or triangle forced through a
        highlight's rectangle-list storage would lose its actual shape,
        rendering as a plain box instead -- see DrawingOverlay's
        docstring. Keeping the tables separate costs nothing here, since
        this is the one place that needs to treat them as one list, and
        it's cheap to merge them on the way out.)"""
        highlights = [dict(h, kind="highlight") for h in self.db.get_highlights(self.book_id)]
        drawings = [dict(d, kind="drawing") for d in self.db.get_drawings(self.book_id)]
        combined = highlights + drawings
        combined.sort(key=lambda entry: (entry["page_number"], entry["id"]))
        return combined

    def refresh_highlights(self):
        self.highlight_list.clear()
        for entry in self._get_all_annotations():
            if entry["kind"] == "drawing":
                text = f"Page {entry['page_number'] + 1} \u2014 {entry['tool'].capitalize()} drawing"
            else:
                text = entry["label"] or f"Page {entry['page_number'] + 1}"
                snippet = (entry["text"] or "").strip().replace("\n", " ")
                if snippet:
                    text += f" \u2014 {snippet[:40]}{'...' if len(snippet) > 40 else ''}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, entry["id"])
            item.setData(Qt.UserRole + 1, entry["page_number"])
            item.setData(Qt.UserRole + 2, entry["kind"])
            item.setForeground(QColor(entry["color"]))
            self.highlight_list.addItem(item)

    def jump_to_highlight(self, item):
        self.current_page = item.data(Qt.UserRole + 1)
        self.render_page()

    def jump_to_next_highlight(self):
        """Jumps to the first highlight or drawing on a page after the
        current one, wrapping around to the very first one in the book
        if the current page is at or past the last one that has any."""
        entries = self._get_all_annotations()  # already ordered by page, then id
        if not entries:
            return
        for entry in entries:
            if entry["page_number"] > self.current_page:
                self._jump_to_highlight_entry(entry)
                return
        self._jump_to_highlight_entry(entries[0])

    def jump_to_prev_highlight(self):
        """Same idea in reverse -- the last highlight or drawing on a
        page before the current one, wrapping around to the very last
        one if already at or before the first page that has any."""
        entries = self._get_all_annotations()
        if not entries:
            return
        for entry in reversed(entries):
            if entry["page_number"] < self.current_page:
                self._jump_to_highlight_entry(entry)
                return
        self._jump_to_highlight_entry(entries[-1])

    def _jump_to_highlight_entry(self, entry):
        self.jump_to_page(entry["page_number"] + 1)
        if entry["kind"] == "drawing":
            label = f"{entry['tool'].capitalize()} drawing"
        else:
            label = entry["label"] or f"Page {entry['page_number'] + 1}"
        self.copy_feedback_label.setText(f"Highlight: {label}")
        QTimer.singleShot(2500, lambda: self.copy_feedback_label.setText(""))

    def _show_highlight_list_menu(self, pos):
        item = self.highlight_list.itemAt(pos)
        if item is None:
            return
        entry_id = item.data(Qt.UserRole)
        is_drawing = item.data(Qt.UserRole + 2) == "drawing"
        menu = QMenu(self)
        jump_action = menu.addAction("Jump to Page")
        edit_action = menu.addAction("Edit Drawing..." if is_drawing else "Edit Highlight...")
        delete_action = menu.addAction("Delete Drawing" if is_drawing else "Delete Highlight")
        chosen = menu.exec(self.highlight_list.mapToGlobal(pos))
        if chosen is jump_action:
            self.jump_to_highlight(item)
        elif chosen is edit_action:
            self.edit_drawing(entry_id) if is_drawing else self.edit_highlight(entry_id)
        elif chosen is delete_action:
            self.delete_drawing(entry_id) if is_drawing else self.delete_highlight(entry_id)

    def remove_selected_highlight(self):
        item = self.highlight_list.currentItem()
        if not item:
            return
        entry_id = item.data(Qt.UserRole)
        if item.data(Qt.UserRole + 2) == "drawing":
            self.delete_drawing(entry_id)
        else:
            self.delete_highlight(entry_id)

    def export_highlights_notes(self):
        highlights = self.db.get_highlights(self.book_id)
        if not highlights:
            QMessageBox.information(self, "No highlights", "This book doesn't have any saved highlights yet.")
            return
        book_title = self.book["title"] or "Untitled"
        default_name = f"{book_title} - Highlights.md"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Highlights", os.path.expanduser(f"~/{default_name}"),
            "Markdown files (*.md);;Text files (*.txt);;All files (*)",
        )
        if not path:
            return
        content = build_highlights_notes(book_title, highlights)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", f"Couldn't write that file:\n{exc}")
            return
        QMessageBox.information(
            self, "Export complete", f"Exported {len(highlights)} highlight(s) to:\n{path}"
        )

    # ------------- Lifecycle -------------
    def showEvent(self, event):
        super().showEvent(event)
        # The viewport has no real size until the window is actually shown/laid
        # out, so the very first render_page() (called from __init__) may have
        # used a placeholder size. Recompute now that geometry is final.
        if self.auto_fit and not self.simple_text_mode and self.doc is not None:
            self.render_page()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.auto_fit and not self.simple_text_mode and self.doc is not None:
            self._resize_timer.start(120)  # debounce so dragging the edge doesn't re-render every pixel

    def closeEvent(self, event):
        self._resize_timer.stop()  # cancel any pending debounced re-fit
        if self.doc is not None:
            self.db.update_progress(self.book_id, self.current_page)
            self.doc.close()
            self.doc = None
        if self.on_close:
            self.on_close()
        super().closeEvent(event)
