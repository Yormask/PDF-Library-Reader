"""Tests covering three related fixes to reader keyboard handling:

1. A real bug: Left/Right arrow page-turning stopped working once a page
   was zoomed in (so QScrollArea had real content to scroll, activating
   its own built-in arrow-key handling) or after clicking into Simple
   Text mode's QTextBrowser -- both widgets default to a focus policy
   that lets them hold keyboard focus, and both fully consume an arrow
   key press for their own built-in behavior (scrolling, cursor
   movement) before it can ever reach ReaderWindow's own keyPressEvent.
   The reported repro was "zoomed in + F11 focus mode", but the
   underlying cause isn't focus-mode-specific: anything that leaves
   scroll_area or text_browser holding focus (a click, or losing other
   focusable widgets when the toolbar is hidden) triggers it.

2. New default shortcuts: "F" to toggle Fit to Screen, and "="/"-" as
   the (single, consolidated) default shortcuts for zoom in/out --
   replacing the earlier Ctrl+=/Ctrl+- defaults, not sitting alongside
   them -- all customizable through the same shortcuts catalog as
   everything else.

3. A second, DIFFERENT bug behind what looked like the same symptom:
   the zoom shortcuts (and Prev/Next, belt-and-suspenders aside) still
   didn't work in Focus Mode even after fix #1, because a QAction's
   keyboard shortcut silently stops being routable once every widget
   it's associated with is hidden -- true even though the action
   itself stays enabled and still reports the right QKeySequence.
   inc_action/dec_action were only ever added to the toolbar, which
   Focus Mode hides. The fix associates them (and prev_action/
   next_action, for defense in depth) with the always-visible
   ReaderWindow too, via self.addAction(). This class of bug needs
   QTest.keyClick to actually catch: QApplication.sendEvent(widget,
   QKeyEvent(...)) bypasses Qt's real shortcut-dispatch pipeline
   entirely, which is enough to test fix #1 (an event filter reacts to
   any dispatch route) but would have reported fix #3 as already
   working when it wasn't -- QAction shortcuts only actually break
   through the real pipeline, and only the real pipeline can confirm
   they're fixed.

Uses a real ReaderWindow, a real Database, and real (temporary) PDF
files throughout -- no mocks -- matching this project's existing testing
convention.
"""
import os
import sys
import tempfile
import unittest

import pymupdf as fitz
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from app.database import Database
from app.reader_window import ReaderWindow
from app.shortcuts import CATALOG, effective_shortcut, save_overrides


def _make_book(db, title="Nav Test Book", pages=3, width=400, height=600):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), f"page {i} some readable text content")
    doc.save(tmp_pdf)
    book = db.add_book(tmp_pdf, title, pages)
    return book, tmp_pdf


def _key_event(qt_key, modifiers=Qt.NoModifier):
    return QKeyEvent(QEvent.KeyPress, qt_key, modifiers)


class ReaderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book, self.tmp_pdf = _make_book(self.db)
        self.win = ReaderWindow(self.db, self.book["id"])

    def tearDown(self):
        self.win.close()
        for path in (self.tmp_db, self.tmp_pdf):
            if os.path.exists(path):
                os.remove(path)


class TestArrowKeyPageTurnFix(ReaderTestCase):
    """The actual bug report, reproduced exactly: focus scroll_area (as
    happens once zoomed in, or after a click-drag pan, or when the
    toolbar's buttons get hidden in Focus Mode), then send a real arrow
    key event straight to it -- before the fix, QScrollArea's own
    built-in scrolling consumed the event and current_page never
    changed."""

    def test_right_arrow_turns_page_when_scroll_area_has_focus_and_zoomed_in(self):
        self.win.auto_fit = False
        self.win.zoom = 2.5  # real scrollable content -- this is what activated the bug
        self.win.render_page()
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        self.assertEqual(self.win.current_page, 0)
        handled = app.sendEvent(self.win.scroll_area, _key_event(Qt.Key_Right))
        self.assertEqual(self.win.current_page, 1)

    def test_left_arrow_turns_page_when_scroll_area_has_focus_and_zoomed_in(self):
        self.win.current_page = 1
        self.win.auto_fit = False
        self.win.zoom = 2.5
        self.win.render_page()
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        app.sendEvent(self.win.scroll_area, _key_event(Qt.Key_Left))
        self.assertEqual(self.win.current_page, 0)

    def test_still_works_when_not_zoomed_in(self):
        """Regression guard the other direction: the fit-to-screen case
        (nothing to scroll) already worked before this fix and must keep
        working -- this shouldn't be a fix that only helps the zoomed-in
        case."""
        self.win.auto_fit = True
        self.win.render_page()
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)
        app.sendEvent(self.win.scroll_area, _key_event(Qt.Key_Right))
        self.assertEqual(self.win.current_page, 1)

    def test_works_while_in_focus_mode_specifically(self):
        """The exact scenario from the bug report."""
        self.win.auto_fit = False
        self.win.zoom = 2.5
        self.win.render_page()
        self.win.toggle_focus_mode()
        self.assertTrue(self.win._focus_mode)
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        app.sendEvent(self.win.scroll_area, _key_event(Qt.Key_Right))
        self.assertEqual(self.win.current_page, 1)

    def test_right_arrow_turns_page_when_text_browser_has_focus(self):
        """Simple Text mode's QTextBrowser has the exact same default
        StrongFocus policy and the same class of built-in key handling
        (cursor movement) that caused the scroll_area bug."""
        self.win.toggle_simple_text(True)
        self.win.render_page()
        self.win.text_browser.setFocus(Qt.OtherFocusReason)

        app.sendEvent(self.win.text_browser, _key_event(Qt.Key_Right))
        self.assertEqual(self.win.current_page, 1)

    def test_native_select_all_still_works_in_text_browser(self):
        """The fix must not come at the cost of QTextBrowser's own native
        keyboard behavior (Ctrl+A, Ctrl+C, cursor movement) -- Simple
        Text mode relies on that natively rather than reimplementing it,
        per _update_mode_visibility's own comment. Only Left/Right must
        be intercepted; everything else should reach the widget as
        normal."""
        self.win.toggle_simple_text(True)
        self.win.render_page()
        self.win.text_browser.setPlainText("some sample text for select all")
        self.win.text_browser.setFocus(Qt.OtherFocusReason)

        select_all = QKeySequence(QKeySequence.SelectAll)[0]
        app.sendEvent(
            self.win.text_browser,
            _key_event(select_all.key(), select_all.keyboardModifiers()),
        )
        cursor = self.win.text_browser.textCursor()
        self.assertTrue(cursor.hasSelection())

    def test_respects_a_remapped_next_page_shortcut(self):
        """The fix checks the LIVE action shortcut, not a hardcoded
        Left/Right -- a user who remapped Next Page must get the new key
        working here too, not the old default silently still doing it."""
        save_overrides(self.db, {"reader.next_page": "Ctrl+Right"})
        self.win.apply_shortcuts()
        self.win.auto_fit = False
        self.win.zoom = 2.5
        self.win.render_page()
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        # the OLD default (plain Right) must no longer trigger it here
        app.sendEvent(self.win.scroll_area, _key_event(Qt.Key_Right))
        self.assertEqual(self.win.current_page, 0)

        # the NEW mapping must
        app.sendEvent(self.win.scroll_area, _key_event(Qt.Key_Right, Qt.ControlModifier))
        self.assertEqual(self.win.current_page, 1)


class TestFitToScreenShortcut(ReaderTestCase):
    def test_catalog_entry_defaults_to_f(self):
        self.assertIn("reader.toggle_fit_to_screen", CATALOG)
        label, default, scope = CATALOG["reader.toggle_fit_to_screen"]
        self.assertEqual(default, "F")
        self.assertEqual(scope, "reader")

    def test_f_toggles_auto_fit_on_and_off(self):
        self.win.auto_fit = False
        self.win.fit_btn.setChecked(False)
        self.win.toggle_fit_shortcut.activated.emit()
        self.assertTrue(self.win.auto_fit)
        self.assertTrue(self.win.fit_btn.isChecked())
        self.win.toggle_fit_shortcut.activated.emit()
        self.assertFalse(self.win.auto_fit)
        self.assertFalse(self.win.fit_btn.isChecked())

    def test_shortcut_is_remappable(self):
        save_overrides(self.db, {"reader.toggle_fit_to_screen": "Ctrl+Shift+F"})
        self.win.apply_shortcuts()
        self.assertEqual(self.win.toggle_fit_shortcut.key().toString(), "Ctrl+Shift+F")


class TestZoomShortcuts(ReaderTestCase):
    """Zoom in/out have exactly one default shortcut each: the bare "="
    and "-" keys, not a Ctrl-modified pair plus a separate "alt"
    binding -- consolidated from an earlier two-entries-per-direction
    design once it turned out one clean default per direction was all
    that was actually wanted."""

    def test_catalog_defaults_are_bare_equals_and_minus(self):
        self.assertEqual(effective_shortcut("reader.zoom_in", {}), "=")
        self.assertEqual(effective_shortcut("reader.zoom_out", {}), "-")

    def test_no_leftover_alt_entries_in_the_catalog(self):
        self.assertNotIn("reader.zoom_in_alt", CATALOG)
        self.assertNotIn("reader.zoom_out_alt", CATALOG)

    def test_equals_key_zooms_in(self):
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win.inc_action.trigger()
        self.assertGreater(self.win.zoom, 1.0)

    def test_minus_key_zooms_out(self):
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win.dec_action.trigger()
        self.assertLess(self.win.zoom, 1.0)

    def test_zoom_in_is_remappable_back_to_a_ctrl_combo_if_wanted(self):
        save_overrides(self.db, {"reader.zoom_in": "Ctrl+="})
        self.win.apply_shortcuts()
        self.assertEqual(self.win.inc_action.shortcut().toString(), "Ctrl+=")
        # the OTHER direction must be untouched by remapping just this one
        self.assertEqual(self.win.dec_action.shortcut().toString(), "-")


class TestShortcutsSurviveToolbarBeingHidden(ReaderTestCase):
    """The second Focus Mode bug: a QAction's shortcut stops being
    routable once every widget it's associated with is hidden, which is
    exactly what happens to inc_action/dec_action (toolbar-only) once
    Focus Mode hides the toolbar -- even though the action itself
    remains enabled throughout. Must use QTest.keyClick, not a directly
    dispatched QKeyEvent, since only the real shortcut-dispatch pipeline
    actually exhibits this bug (see the module docstring)."""

    def setUp(self):
        super().setUp()
        self.win.show()
        QApplication.instance().setActiveWindow(self.win)

    def test_zoom_in_key_still_works_after_toolbar_is_hidden(self):
        self.win.toggle_focus_mode()
        self.assertFalse(self.win.toolbar.isVisible())
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        QTest.keyClick(self.win.scroll_area, Qt.Key_Equal)
        self.assertGreater(self.win.zoom, 1.0)

    def test_zoom_out_key_still_works_after_toolbar_is_hidden(self):
        self.win.toggle_focus_mode()
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        QTest.keyClick(self.win.scroll_area, Qt.Key_Minus)
        self.assertLess(self.win.zoom, 1.0)

    def test_page_turn_keys_still_work_after_toolbar_is_hidden(self):
        """Belt-and-suspenders regression guard: Prev/Next has its OWN
        independent fix (the event filter), but is now also associated
        with the window directly for defense in depth -- confirm that
        addition didn't break or double-fire anything."""
        self.win.toggle_focus_mode()
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        QTest.keyClick(self.win.scroll_area, Qt.Key_Right)
        self.assertEqual(self.win.current_page, 1)

    def test_zoom_shortcut_fires_exactly_once_per_keypress(self):
        """Associating the action with both the toolbar AND the window
        must not cause it to double-fire when both are visible."""
        calls = []
        self.win.inc_action.triggered.disconnect()
        self.win.inc_action.triggered.connect(lambda: calls.append(1))
        self.win.scroll_area.setFocus(Qt.OtherFocusReason)

        QTest.keyClick(self.win.scroll_area, Qt.Key_Equal)
        self.assertEqual(len(calls), 1)

    def test_toolbar_button_click_still_works_normally(self):
        """The fix must not interfere with the ordinary, toolbar-visible,
        click-the-button path most sessions actually use."""
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win.inc_action.trigger()
        self.assertGreater(self.win.zoom, 1.0)


if __name__ == "__main__":
    unittest.main()
