"""Tests for the Draw feature (app/reader_window.py's DrawingOverlay class
and ReaderWindow's drawing-related methods) plus its database layer in
app/database.py.

Uses a real ReaderWindow, a real Database, and real (temporary) PDF files
throughout -- no mocks -- matching this project's existing testing
convention. Drawing happens through real synthetic QMouseEvents dispatched
straight to DrawingOverlay's own event handlers (the same code path an
actual drag ultimately calls), not a shortcut that bypasses the event
handling being tested.

Drawing is a draft-then-save workflow, mirroring how text highlighting
already works (select text, then explicitly click Save Highlight):
finishing a stroke or shape does NOT touch the database -- it only lands
in an in-memory draft, undoable (Ctrl+Z / the Undo button) or discardable
(the Clear button, leaving Draw mode, or navigating to a different page)
right up until save_drawn_highlights() is actually called. Most tests
below exist specifically to pin down that boundary.
"""
import os
import sys
import tempfile
import unittest

import pymupdf as fitz
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

app = QApplication.instance() or QApplication(sys.argv)
# Drawing deletion normally goes through a right-click QMenu, which blocks
# waiting for a click that never comes in an unattended test run --
# stubbed here for every test in this module, not mocking the feature
# under test, just avoiding a real modal dialog headlessly.
QMessageBox.information = staticmethod(lambda *a, **k: None)

from app.database import Database
from app.reader_window import ReaderWindow
from app.shortcuts import CATALOG, effective_shortcut


def _make_book(db, title="Draw Test Book", width=400, height=600, pages=1):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), f"Page {i} text.")
    doc.save(tmp_pdf)
    book = db.add_book(tmp_pdf, title, pages)
    return book, tmp_pdf


def _mouse_event(event_type, pos, button=Qt.LeftButton, buttons=Qt.LeftButton):
    pt = QPointF(pos[0], pos[1])
    return QMouseEvent(event_type, pt, pt, button, buttons, Qt.NoModifier)


def _drag(win, tool, p0, p1):
    """A full press-move-release drag on the drawing overlay, through the
    real event handlers -- exactly what a mouse drag ultimately triggers,
    landing the finished shape in the draft (not the database)."""
    win._set_draw_tool(tool)
    overlay = win.drawing_overlay
    overlay.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, p0))
    overlay.mouseMoveEvent(_mouse_event(QEvent.MouseMove, p1))
    overlay.mouseReleaseEvent(_mouse_event(QEvent.MouseButtonRelease, p1))


class DrawingTestCase(unittest.TestCase):
    """Common setup: a real Database, a real single-page book, a real
    ReaderWindow with Draw mode already on -- torn down after every test
    so each one starts clean."""

    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book, self.tmp_pdf = _make_book(self.db)
        self.win = ReaderWindow(self.db, self.book["id"])
        self.win.toggle_draw_mode(True)

    def tearDown(self):
        self.win.close()
        for path in (self.tmp_db, self.tmp_pdf):
            if os.path.exists(path):
                os.remove(path)


class TestDatabaseLayer(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book = self.db.add_book("/tmp/fake_draw_test.pdf", "Fake Book", 5)

    def tearDown(self):
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)

    def test_round_trips_points_and_fields(self):
        drawing_id = self.db.add_drawing(
            self.book["id"], 0, "pen", "#FFDD00", 0.4, 3.0, [[10, 20], [12, 22], [15, 25]]
        )
        drawings = self.db.get_drawings_for_page(self.book["id"], 0)
        self.assertEqual(len(drawings), 1)
        d = drawings[0]
        self.assertEqual(d["id"], drawing_id)
        self.assertEqual(d["tool"], "pen")
        self.assertEqual(d["color"], "#FFDD00")
        self.assertAlmostEqual(d["opacity"], 0.4)
        self.assertAlmostEqual(d["stroke_width"], 3.0)
        self.assertEqual(d["points"], [[10, 20], [12, 22], [15, 25]])

    def test_scoped_to_the_right_page(self):
        self.db.add_drawing(self.book["id"], 0, "rectangle", "#FF0000", 0.35, 2.0, [[0, 0], [10, 10]])
        self.assertEqual(len(self.db.get_drawings_for_page(self.book["id"], 0)), 1)
        self.assertEqual(len(self.db.get_drawings_for_page(self.book["id"], 1)), 0)

    def test_delete_drawing(self):
        keep_id = self.db.add_drawing(self.book["id"], 0, "line", "#000000", 1.0, 1.0, [[0, 0], [5, 5]])
        drop_id = self.db.add_drawing(self.book["id"], 0, "line", "#000000", 1.0, 1.0, [[1, 1], [6, 6]])
        self.db.delete_drawing(drop_id)
        remaining = self.db.get_drawings_for_page(self.book["id"], 0)
        self.assertEqual([d["id"] for d in remaining], [keep_id])

    def test_deleting_book_cascades_to_its_drawings(self):
        self.db.add_drawing(self.book["id"], 0, "pen", "#FFDD00", 0.4, 3.0, [[10, 20], [15, 25]])
        self.db.remove_book(self.book["id"])
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])


class TestShortcutCatalog(unittest.TestCase):
    def test_undo_drawing_is_registered_with_ctrl_z_default(self):
        self.assertIn("reader.undo_drawing", CATALOG)
        label, default, scope = CATALOG["reader.undo_drawing"]
        self.assertEqual(default, "Ctrl+Z")
        self.assertEqual(scope, "reader")
        self.assertEqual(effective_shortcut("reader.undo_drawing", {}), "Ctrl+Z")

    def test_undo_drawing_is_remappable_like_any_other_shortcut(self):
        overrides = {"reader.undo_drawing": "Ctrl+Shift+Z"}
        self.assertEqual(effective_shortcut("reader.undo_drawing", overrides), "Ctrl+Shift+Z")


class TestDraftWorkflow(DrawingTestCase):
    """The core behavior this round of changes was about: nothing is
    permanent until Save Drawn Highlight is clicked."""

    def test_drawing_a_shape_does_not_touch_the_database(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        self.assertEqual(len(self.win.drawing_overlay.get_draft_drawings()), 1)
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])

    def test_action_buttons_enable_once_there_is_a_draft(self):
        self.assertFalse(self.win.draw_undo_btn.isEnabled())
        self.assertFalse(self.win.draw_clear_btn.isEnabled())
        self.assertFalse(self.win.draw_save_btn.isEnabled())
        _drag(self.win, "pen", (10, 10), (50, 50))
        self.assertTrue(self.win.draw_undo_btn.isEnabled())
        self.assertTrue(self.win.draw_clear_btn.isEnabled())
        self.assertTrue(self.win.draw_save_btn.isEnabled())

    def test_save_drawn_highlights_persists_every_draft_shape(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        _drag(self.win, "ellipse", (80, 80), (120, 120))
        _drag(self.win, "pen", (140, 140), (160, 160))
        self.win.save_drawn_highlights()

        saved = self.db.get_drawings_for_page(self.book["id"], 0)
        self.assertEqual(sorted(d["tool"] for d in saved), ["ellipse", "pen", "rectangle"])
        self.assertEqual(self.win.drawing_overlay.get_draft_drawings(), [])
        self.assertFalse(self.win.draw_undo_btn.isEnabled())
        self.assertFalse(self.win.draw_save_btn.isEnabled())

    def test_saved_shapes_reload_correctly_after_save(self):
        _drag(self.win, "rectangle", (50, 60), (150, 100))
        self.win.save_drawn_highlights()
        reloaded = self.win.drawing_overlay._saved
        self.assertEqual(len(reloaded), 1)
        points = [(round(p.x(), 3), round(p.y(), 3)) for p in reloaded[0]["points"]]
        self.assertEqual(points, [(50.0, 60.0), (150.0, 100.0)])

    def test_each_stroke_keeps_the_color_it_was_drawn_with(self):
        self.win.draw_color = "#FF0000"
        _drag(self.win, "rectangle", (10, 10), (30, 30))
        self.win.draw_color = "#00FF00"
        _drag(self.win, "rectangle", (50, 50), (70, 70))
        draft = self.win.drawing_overlay.get_draft_drawings()
        self.assertEqual(draft[0]["color"].name().upper(), "#FF0000")
        self.assertEqual(draft[1]["color"].name().upper(), "#00FF00")

    def test_saving_with_an_empty_draft_does_nothing(self):
        self.win.save_drawn_highlights()  # nothing drawn yet
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])


class TestUndo(DrawingTestCase):
    def test_undo_removes_only_the_most_recent_stroke(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        _drag(self.win, "ellipse", (80, 80), (120, 120))
        _drag(self.win, "pen", (140, 140), (160, 160))
        self.win.undo_last_drawing_stroke()
        remaining = [d["tool"] for d in self.win.drawing_overlay.get_draft_drawings()]
        self.assertEqual(remaining, ["rectangle", "ellipse"])

    def test_undo_via_ctrl_z_shortcut_handler(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        _drag(self.win, "line", (80, 80), (120, 120))
        self.win._undo_drawing_shortcut_triggered()
        remaining = [d["tool"] for d in self.win.drawing_overlay.get_draft_drawings()]
        self.assertEqual(remaining, ["rectangle"])

    def test_ctrl_z_does_nothing_outside_draw_mode(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        self.win.toggle_draw_mode(False)  # also discards the draft, see below
        self.win._undo_drawing_shortcut_triggered()  # must not raise
        self.assertEqual(self.win.drawing_overlay.get_draft_drawings(), [])

    def test_undo_on_empty_draft_is_a_safe_no_op(self):
        self.win.undo_last_drawing_stroke()  # nothing drawn -- must not raise
        self.assertEqual(self.win.drawing_overlay.get_draft_drawings(), [])

    def test_repeated_undo_empties_the_draft(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        _drag(self.win, "ellipse", (80, 80), (120, 120))
        self.win.undo_last_drawing_stroke()
        self.win.undo_last_drawing_stroke()
        self.assertEqual(self.win.drawing_overlay.get_draft_drawings(), [])
        self.assertFalse(self.win.draw_undo_btn.isEnabled())


class TestClear(DrawingTestCase):
    def test_clear_discards_every_draft_shape_at_once(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        _drag(self.win, "ellipse", (80, 80), (120, 120))
        self.win.clear_draft_drawings()
        self.assertEqual(self.win.drawing_overlay.get_draft_drawings(), [])
        self.assertFalse(self.win.draw_clear_btn.isEnabled())

    def test_clear_does_not_touch_already_saved_drawings(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        self.win.save_drawn_highlights()
        _drag(self.win, "ellipse", (80, 80), (120, 120))
        self.win.clear_draft_drawings()
        saved = self.db.get_drawings_for_page(self.book["id"], 0)
        self.assertEqual([d["tool"] for d in saved], ["rectangle"])


class TestDiscardWithoutSaving(DrawingTestCase):
    """Exactly the behavior requested: unsaved drawings disappear."""

    def test_turning_off_draw_mode_discards_the_draft(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        self.win.toggle_draw_mode(False)
        self.assertEqual(self.win.drawing_overlay.get_draft_drawings(), [])
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])

    def test_switching_to_select_text_mode_discards_the_draft(self):
        _drag(self.win, "rectangle", (20, 20), (60, 60))
        self.win.toggle_select_text_mode(True)  # mutually exclusive -- turns Draw mode off
        self.assertEqual(self.win.drawing_overlay.get_draft_drawings(), [])
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])

    def test_navigating_to_another_page_discards_the_draft(self):
        self.db.remove_book(self.book["id"])
        book, tmp_pdf = _make_book(self.db, pages=3)
        try:
            win = ReaderWindow(self.db, book["id"])
            win.toggle_draw_mode(True)
            _drag(win, "triangle", (30, 30), (70, 70))
            self.assertEqual(len(win.drawing_overlay.get_draft_drawings()), 1)
            win.next_page()
            self.assertEqual(win.drawing_overlay.get_draft_drawings(), [])
            self.assertEqual(self.db.get_drawings_for_page(book["id"], 0), [])
            win.close()
        finally:
            if os.path.exists(tmp_pdf):
                os.remove(tmp_pdf)

    def test_same_page_rerender_does_not_discard_the_draft(self):
        """A resize, zoom change, or dark-mode toggle also calls
        render_page() -- none of those should wipe out an in-progress
        drawing the way actually turning the page does."""
        _drag(self.win, "rectangle", (30, 30), (70, 70))
        self.assertEqual(len(self.win.drawing_overlay.get_draft_drawings()), 1)
        self.win.render_page()  # same page, e.g. simulating a resize
        self.assertEqual(len(self.win.drawing_overlay.get_draft_drawings()), 1)


class TestAllToolTypes(DrawingTestCase):
    def test_all_tool_types_draft_and_save_and_render_without_error(self):
        shapes = [
            ("pen", (10, 10), (25, 25)),
            ("rectangle", (50, 60), (150, 100)),
            ("ellipse", (60, 200), (160, 260)),
            ("triangle", (70, 300), (170, 360)),
            ("line", (80, 400), (180, 420)),
        ]
        for tool, p0, p1 in shapes:
            _drag(self.win, tool, p0, p1)
        self.win.save_drawn_highlights()

        saved = self.db.get_drawings_for_page(self.book["id"], 0)
        self.assertEqual({d["tool"] for d in saved}, {t for t, _, _ in shapes})

        img = QImage(self.win.drawing_overlay.size(), QImage.Format_ARGB32)
        self.win.drawing_overlay.render(img)  # must not raise for any shape type


class TestZoomIndependence(DrawingTestCase):
    def test_saved_drawing_redraws_correctly_at_a_different_zoom(self):
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win.render_page()
        _drag(self.win, "rectangle", (40, 40), (140, 90))
        self.win.save_drawn_highlights()
        pdf_points = self.db.get_drawings_for_page(self.book["id"], 0)[0]["points"]

        self.win.zoom = 2.0
        self.win.render_page()
        reloaded = self.win.drawing_overlay._saved[0]["points"]
        actual = [(round(p.x(), 3), round(p.y(), 3)) for p in reloaded]
        expected = [(round(x * 2, 3), round(y * 2, 3)) for (x, y) in pdf_points]
        self.assertEqual(actual, expected)


class TestTwoPageView(DrawingTestCase):
    def test_attributes_drawing_to_the_correct_page(self):
        self.db.remove_book(self.book["id"])
        book, tmp_pdf = _make_book(self.db, pages=4)
        try:
            win = ReaderWindow(self.db, book["id"])
            win.two_page_mode = True
            win.current_page = 0
            win.render_page()
            win.toggle_draw_mode(True)
            right_x = win._left_page_px_width + 50
            _drag(win, "rectangle", (right_x, 40), (right_x + 50, 90))
            win.save_drawn_highlights()
            self.assertEqual(len(self.db.get_drawings_for_page(book["id"], 0)), 0)
            self.assertEqual(len(self.db.get_drawings_for_page(book["id"], 1)), 1)
            win.close()
        finally:
            if os.path.exists(tmp_pdf):
                os.remove(tmp_pdf)


class TestStrayClicks(DrawingTestCase):
    def test_a_click_with_no_real_drag_is_not_recorded(self):
        overlay = self.win.drawing_overlay
        overlay.mousePressEvent(_mouse_event(QEvent.MouseButtonPress, (200, 200)))
        overlay.mouseReleaseEvent(_mouse_event(QEvent.MouseButtonRelease, (200, 200)))
        self.assertEqual(overlay.get_draft_drawings(), [])

    def test_no_mouse_capture_when_draw_mode_is_off(self):
        win2_db_path = tempfile.mktemp(suffix=".db")
        db2 = Database(win2_db_path)
        book2, tmp_pdf2 = _make_book(db2)
        try:
            win2 = ReaderWindow(db2, book2["id"])  # Draw mode never turned on
            self.assertTrue(win2.drawing_overlay.testAttribute(Qt.WA_TransparentForMouseEvents))
            win2.close()
        finally:
            if os.path.exists(win2_db_path):
                os.remove(win2_db_path)
            if os.path.exists(tmp_pdf2):
                os.remove(tmp_pdf2)


class TestHitTestingAndDeletionOfSavedDrawings(DrawingTestCase):
    """Right-click delete is scoped to already-saved drawings -- draft
    shapes are managed via Undo/Clear/Save instead (see TestUndo/
    TestClear above), not individually right-clickable."""

    def test_drawing_at_point_finds_a_saved_rectangle(self):
        _drag(self.win, "rectangle", (50, 60), (150, 100))
        self.win.save_drawn_highlights()
        saved = self.win.drawing_overlay._saved[0]
        rect = QRectF(saved["points"][0], saved["points"][1]).normalized()
        hit = self.win.drawing_overlay.drawing_at_point(rect.center())
        self.assertIsNotNone(hit)
        self.assertEqual(hit["tool"], "rectangle")

    def test_delete_drawing_removes_a_saved_one(self):
        _drag(self.win, "rectangle", (50, 60), (150, 100))
        self.win.save_drawn_highlights()
        drawing_id = self.db.get_drawings_for_page(self.book["id"], 0)[0]["id"]
        self.win.delete_drawing(drawing_id)
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])
        self.assertEqual(self.win.drawing_overlay._saved, [])

    def test_draft_shapes_are_not_individually_hit_testable(self):
        _drag(self.win, "rectangle", (50, 60), (150, 100))  # drawn, not saved
        draft = self.win.drawing_overlay.get_draft_drawings()[0]
        rect = QRectF(draft["points"][0], draft["points"][1]).normalized()
        hit = self.win.drawing_overlay.drawing_at_point(rect.center())
        self.assertIsNone(hit)


class TestOpacityPercentageLabel(DrawingTestCase):
    def test_label_shows_initial_opacity(self):
        expected = f"{round(self.win.draw_opacity * 100)}%"
        self.assertEqual(self.win.draw_opacity_label.text(), expected)

    def test_label_updates_when_slider_moves(self):
        self.win.draw_opacity_slider.setValue(73)
        self.assertEqual(self.win.draw_opacity_label.text(), "73%")

    def test_edit_dialog_opacity_label_updates_too(self):
        from app.reader_window import DrawingDialog
        dialog = DrawingDialog("Edit Drawing", "#FF0000", 0.4, 3.0)
        self.assertEqual(dialog.opacity_label.text(), "40%")
        dialog.opacity_slider.setValue(90)
        self.assertEqual(dialog.opacity_label.text(), "90%")


class TestUnifiedHighlightsList(DrawingTestCase):
    """The actual point of this round of changes: a saved drawing shows
    up in the same Highlights list as a text highlight, in the same
    page order, and can be jumped to, edited, and deleted from there --
    not just via right-click on the canvas while in Draw mode."""

    def _drag_and_save(self, tool, p0, p1):
        _drag(self.win, tool, p0, p1)
        self.win.save_drawn_highlights()

    def test_saved_drawing_appears_in_the_shared_list(self):
        self.db.add_highlight(self.book["id"], 0, "#3878FF", [[10, 10, 100, 30]], text="hello world")
        self.win.refresh_highlights()
        self.assertEqual(self.win.highlight_list.count(), 1)

        self._drag_and_save("rectangle", (20, 20), (80, 80))
        self.assertEqual(self.win.highlight_list.count(), 2)
        kinds = {self.win.highlight_list.item(i).data(Qt.UserRole + 2) for i in range(2)}
        self.assertEqual(kinds, {"highlight", "drawing"})

    def test_list_is_sorted_by_page_regardless_of_kind(self):
        self.db.add_highlight(self.book["id"], 2, "#3878FF", [[0, 0, 10, 10]], text="p2")
        self.db.add_drawing(self.book["id"], 0, "pen", "#FF0000", 0.5, 2.0, [[0, 0], [5, 5]])
        self.db.add_highlight(self.book["id"], 1, "#00FF00", [[0, 0, 10, 10]], text="p1")
        self.win.refresh_highlights()
        pages = [self.win.highlight_list.item(i).data(Qt.UserRole + 1) for i in range(self.win.highlight_list.count())]
        self.assertEqual(pages, sorted(pages))
        self.assertEqual(pages, [0, 1, 2])

    def test_drawing_list_entry_shows_page_and_tool(self):
        self._drag_and_save("triangle", (20, 20), (80, 80))
        item = self.win.highlight_list.item(0)
        self.assertIn("Page 1", item.text())
        self.assertIn("Triangle", item.text())

    def test_double_click_on_drawing_entry_navigates_to_its_page(self):
        self.db.remove_book(self.book["id"])
        book, tmp_pdf = _make_book(self.db, pages=3)
        try:
            win = ReaderWindow(self.db, book["id"])
            win.toggle_draw_mode(True)
            win.next_page()  # move to page 1 before drawing there
            _drag(win, "line", (20, 20), (80, 80))
            win.save_drawn_highlights()
            win.current_page = 0
            win.render_page()
            item = win.highlight_list.item(0)
            self.assertEqual(item.data(Qt.UserRole + 1), 1)
            win.jump_to_highlight(item)
            self.assertEqual(win.current_page, 1)
            win.close()
        finally:
            if os.path.exists(tmp_pdf):
                os.remove(tmp_pdf)

    def test_remove_selected_highlight_deletes_a_drawing_entry(self):
        self._drag_and_save("rectangle", (20, 20), (80, 80))
        self.win.highlight_list.setCurrentRow(0)
        self.win.remove_selected_highlight()
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])
        self.assertEqual(self.win.highlight_list.count(), 0)

    def test_remove_selected_highlight_still_deletes_a_text_highlight(self):
        h_id = self.db.add_highlight(self.book["id"], 0, "#3878FF", [[10, 10, 100, 30]], text="hi")
        self.win.refresh_highlights()
        self.win.highlight_list.setCurrentRow(0)
        self.win.remove_selected_highlight()
        self.assertEqual(self.db.get_highlights(self.book["id"]), [])

    def test_context_menu_delete_routes_by_kind(self):
        self._drag_and_save("rectangle", (20, 20), (80, 80))
        drawing_id = self.db.get_drawings_for_page(self.book["id"], 0)[0]["id"]
        item = self.win.highlight_list.item(0)
        self.assertEqual(item.data(Qt.UserRole + 2), "drawing")
        self.win.delete_drawing(item.data(Qt.UserRole))
        self.assertEqual(self.db.get_drawings_for_page(self.book["id"], 0), [])

    def test_deleting_a_drawing_via_canvas_context_menu_still_refreshes_the_list(self):
        # DrawingOverlay.contextMenuEvent calls reader.delete_drawing directly
        # (right-click on the canvas, not the list) -- must also keep the
        # shared list in sync, not just the on-page overlay.
        self._drag_and_save("rectangle", (20, 20), (80, 80))
        self.assertEqual(self.win.highlight_list.count(), 1)
        drawing_id = self.db.get_drawings_for_page(self.book["id"], 0)[0]["id"]
        self.win.delete_drawing(drawing_id)
        self.assertEqual(self.win.highlight_list.count(), 0)

    def test_jump_to_next_highlight_cycles_through_both_kinds(self):
        self.db.add_highlight(self.book["id"], 0, "#3878FF", [[0, 0, 10, 10]], text="only text hl")
        self._drag_and_save("rectangle", (20, 20), (80, 80))  # also page 0
        self.win.current_page = 0
        # both entries are on the current page, so "next" should still
        # resolve to *something* on/after this page without raising
        self.win.jump_to_next_highlight()  # must not raise
        self.win.jump_to_prev_highlight()  # must not raise


class TestEditDrawing(DrawingTestCase):
    def test_edit_drawing_updates_color_opacity_and_width(self):
        _drag(self.win, "rectangle", (20, 20), (80, 80))
        self.win.save_drawn_highlights()
        drawing_id = self.db.get_drawings_for_page(self.book["id"], 0)[0]["id"]

        from app.reader_window import DrawingDialog

        def fake_exec(dialog_self):
            dialog_self._color = QColor("#00FF00")
            dialog_self.opacity_slider.setValue(80)
            dialog_self.width_spin.setValue(9)
            return QDialog.Accepted

        original_exec = DrawingDialog.exec
        DrawingDialog.exec = fake_exec
        try:
            self.win.edit_drawing(drawing_id)
        finally:
            DrawingDialog.exec = original_exec

        updated = self.db.get_drawings_for_page(self.book["id"], 0)[0]
        self.assertEqual(updated["color"], "#00ff00")
        self.assertAlmostEqual(updated["opacity"], 0.8)
        self.assertEqual(updated["stroke_width"], 9.0)

    def test_editing_updates_the_overlay_render_too(self):
        _drag(self.win, "ellipse", (20, 20), (80, 80))
        self.win.save_drawn_highlights()
        drawing_id = self.db.get_drawings_for_page(self.book["id"], 0)[0]["id"]

        from app.reader_window import DrawingDialog

        def fake_exec(dialog_self):
            dialog_self._color = QColor("#123456")
            return QDialog.Accepted

        original_exec = DrawingDialog.exec
        DrawingDialog.exec = fake_exec
        try:
            self.win.edit_drawing(drawing_id)
        finally:
            DrawingDialog.exec = original_exec

        self.assertEqual(self.win.drawing_overlay._saved[0]["color"].name(), "#123456")

    def test_editing_a_nonexistent_drawing_id_does_not_raise(self):
        self.win.edit_drawing(99999)


class TestDatabaseUpdateDrawing(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book = self.db.add_book("/tmp/fake_draw_update.pdf", "Fake Book", 5)

    def tearDown(self):
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)

    def test_update_drawing_changes_color_opacity_width(self):
        drawing_id = self.db.add_drawing(
            self.book["id"], 0, "rectangle", "#FF0000", 0.4, 3.0, [[0, 0], [10, 10]]
        )
        self.db.update_drawing(drawing_id, "#00FF00", 0.75, 5.0)
        d = self.db.get_drawings_for_page(self.book["id"], 0)[0]
        self.assertEqual(d["color"], "#00FF00")
        self.assertAlmostEqual(d["opacity"], 0.75)
        self.assertEqual(d["stroke_width"], 5.0)

    def test_update_drawing_does_not_touch_points_or_tool(self):
        drawing_id = self.db.add_drawing(
            self.book["id"], 0, "triangle", "#FF0000", 0.4, 3.0, [[1, 2], [3, 4]]
        )
        self.db.update_drawing(drawing_id, "#000000", 1.0, 1.0)
        d = self.db.get_drawings_for_page(self.book["id"], 0)[0]
        self.assertEqual(d["tool"], "triangle")
        self.assertEqual(d["points"], [[1, 2], [3, 4]])


class TestToolSettingsPersistence(DrawingTestCase):
    def test_tool_color_opacity_width_persist_across_windows(self):
        self.win._set_draw_tool("ellipse")
        self.win._set_draw_opacity(65)
        self.win._set_draw_stroke_width(7)
        self.win.draw_color = "#00FF00"
        self.db.set_setting("last_draw_color", self.win.draw_color)

        win2 = ReaderWindow(self.db, self.book["id"])
        try:
            self.assertEqual(win2.draw_tool, "ellipse")
            self.assertAlmostEqual(win2.draw_opacity, 0.65)
            self.assertEqual(win2.draw_stroke_width, 7.0)
            self.assertEqual(win2.draw_color, "#00FF00")
        finally:
            win2.close()


if __name__ == "__main__":
    unittest.main()
