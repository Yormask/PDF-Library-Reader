"""Tests for the mouse-based shortcuts added in this round of changes:

1. Two new gestures can zoom simultaneously by default: Ctrl+Scroll (the
   existing default) and Right-Click held+Scroll (new). This is why
   mouse wheel gestures use a different data model from keyboard
   shortcuts (see shortcuts.py's WHEEL_GESTURES/WHEEL_ACTIONS) -- a
   keyboard shortcut can only ever belong to one action at a time (two
   actions sharing a key is a genuine conflict, since only one can fire),
   but more than one wheel gesture legitimately pointing at the same
   action is the whole point here, not an edge case to resolve away.

2. All three wheel gestures (Ctrl+Scroll, Middle/Right-Click held +
   Scroll -- Shift+Scroll and Alt+Scroll are deliberately not offered,
   since both already carry OS-level meaning on most systems) are
   independently configurable to any of three actions
   (None / Zoom In/Out / Turn Page While Zoomed) via the Keyboard
   Shortcuts dialog's new "Mouse Wheel Gestures" section.

3. A right-click on the page while just reading (neither Select Text nor
   Draw mode active) opens a menu with Select Text / Draw / Add Bookmark
   -- but only for an actual simple click. Holding the button through a
   scroll (the Right-Click+Scroll zoom gesture from #1) must not also
   pop the menu open right after, which would interrupt the gesture.

Uses a real ReaderWindow, a real Database, and real (temporary) PDF
files throughout. QMenu.exec() cannot be reliably monkeypatched in
PySide6 (it's a compiled/overloaded Qt method, unlike e.g.
QMessageBox.information) and would otherwise block waiting for a click
that never comes in an unattended test run -- so, matching this
project's existing tests for other context menus (see test_drawing.py),
menu-triggered actions are tested by calling the underlying method
directly (select_text_btn.click(), draw_btn.click(), add_bookmark())
rather than by driving a real QMenu, and the menu-vs-gesture
disambiguation is tested by monkeypatching _show_reading_context_menu
itself -- a plain Python method on ReaderWindow, not a Qt-native one.
"""
import os
import sys
import tempfile
import unittest

import pymupdf as fitz
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QInputDialog

app = QApplication.instance() or QApplication(sys.argv)
QInputDialog.getText = staticmethod(lambda *a, **k: ("", True))

from app.database import Database
from app.reader_window import ReaderWindow
from app.shortcuts_dialog import ShortcutsDialog
from app.shortcuts import (
    WHEEL_ACTION_NONE, WHEEL_ACTION_PAGE_TURN, WHEEL_ACTION_ZOOM, WHEEL_GESTURE_DEFAULTS,
    WHEEL_GESTURES, effective_wheel_action, load_wheel_overrides, save_wheel_overrides,
)


def _make_book(db, title="Mouse Shortcuts Test", pages=3, width=400, height=600):
    tmp_pdf = tempfile.mktemp(suffix=".pdf")
    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 72), f"page {i} text")
    doc.save(tmp_pdf)
    book = db.add_book(tmp_pdf, title, pages)
    return book, tmp_pdf


def _wheel_event(delta_y, buttons=Qt.NoButton, modifiers=Qt.NoModifier, pos=(50, 50)):
    pt = QPointF(*pos)
    return QWheelEvent(pt, pt, QPoint(0, 0), QPoint(0, delta_y), buttons, modifiers, Qt.NoScrollPhase, False)


def _mouse_event(event_type, buttons_held, button, modifiers=Qt.NoModifier, pos=(50, 50)):
    pt = QPointF(*pos)
    return QMouseEvent(event_type, pt, pt, button, buttons_held, modifiers)


class ReaderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)
        self.book, self.tmp_pdf = _make_book(self.db)
        self.win = ReaderWindow(self.db, self.book["id"])
        self.win.show()

    def tearDown(self):
        self.win.close()
        for path in (self.tmp_db, self.tmp_pdf):
            if os.path.exists(path):
                os.remove(path)


class TestWheelGestureDefaults(unittest.TestCase):
    def test_ctrl_and_right_click_both_default_to_zoom(self):
        self.assertEqual(effective_wheel_action("ctrl_scroll", {}), WHEEL_ACTION_ZOOM)
        self.assertEqual(effective_wheel_action("right_click_scroll", {}), WHEEL_ACTION_ZOOM)

    def test_middle_click_defaults_to_page_turn(self):
        self.assertEqual(effective_wheel_action("middle_click_scroll", {}), WHEEL_ACTION_PAGE_TURN)

    def test_shift_and_alt_scroll_are_not_offered_as_gestures(self):
        """Both already carry OS-level meaning on most systems (Alt+Scroll
        commonly changes the scroll axis, for instance) -- claiming them
        for something else here would fight with behavior the user
        already has outside this app entirely."""
        self.assertNotIn("shift_scroll", WHEEL_GESTURES)
        self.assertNotIn("alt_scroll", WHEEL_GESTURES)

    def test_exactly_three_gestures_are_offered(self):
        self.assertEqual(set(WHEEL_GESTURES), {"ctrl_scroll", "middle_click_scroll", "right_click_scroll"})

    def test_all_gestures_have_defaults(self):
        for gesture_id in WHEEL_GESTURES:
            self.assertIn(gesture_id, WHEEL_GESTURE_DEFAULTS)

    def test_unrecognized_gesture_id_defaults_to_none(self):
        self.assertEqual(effective_wheel_action("nonexistent_gesture", {}), WHEEL_ACTION_NONE)


class TestWheelOverridesPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)

    def tearDown(self):
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)

    def test_save_and_reload_round_trips(self):
        save_wheel_overrides(self.db, {"middle_click_scroll": WHEEL_ACTION_ZOOM})
        reloaded = load_wheel_overrides(self.db)
        self.assertEqual(reloaded.get("middle_click_scroll"), WHEEL_ACTION_ZOOM)

    def test_unknown_gesture_id_is_not_persisted(self):
        save_wheel_overrides(self.db, {"not_a_real_gesture": WHEEL_ACTION_ZOOM})
        reloaded = load_wheel_overrides(self.db)
        self.assertNotIn("not_a_real_gesture", reloaded)

    def test_shift_and_alt_gesture_ids_are_not_persisted(self):
        """Even if a stale value from before this change is still sitting
        in the settings JSON (or someone hand-edits it), a removed
        gesture_id must not resurrect itself."""
        save_wheel_overrides(self.db, {"shift_scroll": WHEEL_ACTION_ZOOM, "alt_scroll": WHEEL_ACTION_ZOOM})
        reloaded = load_wheel_overrides(self.db)
        self.assertNotIn("shift_scroll", reloaded)
        self.assertNotIn("alt_scroll", reloaded)

    def test_invalid_action_value_is_not_persisted(self):
        save_wheel_overrides(self.db, {"middle_click_scroll": "Not A Real Action"})
        reloaded = load_wheel_overrides(self.db)
        self.assertNotIn("middle_click_scroll", reloaded)

    def test_missing_settings_value_yields_empty_overrides(self):
        self.assertEqual(load_wheel_overrides(self.db), {})

    def test_overriding_one_gesture_does_not_affect_others(self):
        save_wheel_overrides(self.db, {"ctrl_scroll": WHEEL_ACTION_NONE})
        self.assertEqual(effective_wheel_action("right_click_scroll", load_wheel_overrides(self.db)), WHEEL_ACTION_ZOOM)


class TestBothZoomGesturesWorkSimultaneously(ReaderTestCase):
    """The actual point of the redesign: Ctrl+Scroll and Right-Click
    held+Scroll are two independent, simultaneously-active gestures both
    pointing at the same action -- not one overriding the other."""

    def test_ctrl_scroll_zooms_by_default(self):
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win._handle_wheel(_wheel_event(120, modifiers=Qt.ControlModifier))
        self.assertGreater(self.win.zoom, 1.0)

    def test_right_click_held_scroll_zooms_by_default(self):
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win._handle_wheel(_wheel_event(120, buttons=Qt.RightButton))
        self.assertGreater(self.win.zoom, 1.0)

    def test_both_gestures_zoom_out_too(self):
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win._handle_wheel(_wheel_event(-120, modifiers=Qt.ControlModifier))
        self.assertLess(self.win.zoom, 1.0)
        self.win.zoom = 1.0
        self.win._handle_wheel(_wheel_event(-120, buttons=Qt.RightButton))
        self.assertLess(self.win.zoom, 1.0)

    def test_middle_click_scroll_turns_page_while_zoomed(self):
        self.win.auto_fit = False
        self.win.zoom = 2.0
        self.win.current_page = 0
        self.win._handle_wheel(_wheel_event(-120, buttons=Qt.MiddleButton))
        self.assertEqual(self.win.current_page, 1)

    def test_plain_scroll_while_zoomed_does_not_turn_page(self):
        self.win.auto_fit = False
        self.win.zoom = 2.0
        self.win.current_page = 0
        handled = self.win._handle_wheel(_wheel_event(-120))
        self.assertFalse(handled)
        self.assertEqual(self.win.current_page, 0)

    def test_plain_scroll_turns_page_when_fit_to_screen(self):
        self.win.auto_fit = True
        self.win.current_page = 0
        self.win._handle_wheel(_wheel_event(-120))
        self.assertEqual(self.win.current_page, 1)


class TestWheelGestureReconfiguration(ReaderTestCase):
    def test_reassigning_ctrl_scroll_to_none_stops_it_zooming(self):
        save_wheel_overrides(self.db, {"ctrl_scroll": WHEEL_ACTION_NONE})
        self.win.apply_shortcuts()
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win._handle_wheel(_wheel_event(120, modifiers=Qt.ControlModifier))
        self.assertEqual(self.win.zoom, 1.0)

    def test_right_click_scroll_unaffected_by_reassigning_ctrl_scroll(self):
        save_wheel_overrides(self.db, {"ctrl_scroll": WHEEL_ACTION_NONE})
        self.win.apply_shortcuts()
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win._handle_wheel(_wheel_event(120, buttons=Qt.RightButton))
        self.assertGreater(self.win.zoom, 1.0)

    def test_middle_click_scroll_can_be_reassigned_to_zoom(self):
        save_wheel_overrides(self.db, {"middle_click_scroll": WHEEL_ACTION_ZOOM})
        self.win.apply_shortcuts()
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self.win._handle_wheel(_wheel_event(120, buttons=Qt.MiddleButton))
        self.assertGreater(self.win.zoom, 1.0)

    def test_apply_shortcuts_refreshes_the_cached_gesture_actions(self):
        self.assertEqual(self.win.wheel_gesture_actions["middle_click_scroll"], WHEEL_ACTION_PAGE_TURN)
        save_wheel_overrides(self.db, {"middle_click_scroll": WHEEL_ACTION_ZOOM})
        self.win.apply_shortcuts()
        self.assertEqual(self.win.wheel_gesture_actions["middle_click_scroll"], WHEEL_ACTION_ZOOM)


class TestReadingContextMenu(ReaderTestCase):
    """See the module docstring for why _show_reading_context_menu is
    monkeypatched rather than driving a real QMenu."""

    def setUp(self):
        super().setUp()
        self.menu_calls = []
        self.win._show_reading_context_menu = lambda event: self.menu_calls.append(event)

    def _press(self, pos=(50, 50)):
        return self.win.eventFilter(
            self.win.page_label,
            _mouse_event(QEvent.MouseButtonPress, Qt.RightButton, Qt.RightButton, pos=pos),
        )

    def _release(self, pos=(50, 50)):
        return self.win.eventFilter(
            self.win.page_label,
            _mouse_event(QEvent.MouseButtonRelease, Qt.NoButton, Qt.RightButton, pos=pos),
        )

    def test_simple_right_click_opens_the_menu(self):
        self._press()
        self._release()
        self.assertEqual(len(self.menu_calls), 1)

    def test_press_resets_the_scroll_flag(self):
        self.win._right_click_held_through_scroll = True
        self._press()
        self.assertFalse(self.win._right_click_held_through_scroll)

    def test_right_click_held_through_a_scroll_does_not_open_the_menu(self):
        self.win.auto_fit = False
        self.win.zoom = 1.0
        self._press()
        self.win._handle_wheel(_wheel_event(120, buttons=Qt.RightButton))
        self._release()
        self.assertEqual(len(self.menu_calls), 0)

    def test_scroll_flag_is_set_regardless_of_which_action_it_triggers(self):
        """Even if right-click isn't currently configured for anything
        (e.g. reassigned to None), scrolling while it's held still reads
        as a deliberate hold, not a simple click -- the menu should still
        be suppressed."""
        save_wheel_overrides(self.db, {"right_click_scroll": WHEEL_ACTION_NONE})
        self.win.apply_shortcuts()
        self._press()
        self.win._handle_wheel(_wheel_event(120, buttons=Qt.RightButton))
        self._release()
        self.assertEqual(len(self.menu_calls), 0)

    def test_press_and_release_without_scroll_in_between_reopens_menu_each_time(self):
        self._press()
        self._release()
        self._press()
        self._release()
        self.assertEqual(len(self.menu_calls), 2)

    def test_menu_not_reachable_via_left_click(self):
        self.win.eventFilter(
            self.win.page_label,
            _mouse_event(QEvent.MouseButtonPress, Qt.LeftButton, Qt.LeftButton),
        )
        self.win.eventFilter(
            self.win.page_label,
            _mouse_event(QEvent.MouseButtonRelease, Qt.NoButton, Qt.LeftButton),
        )
        self.assertEqual(len(self.menu_calls), 0)


class TestReadingContextMenuActions(ReaderTestCase):
    """The menu's three actions route to the exact same methods the
    toolbar buttons and Ctrl+D already use -- tested directly per this
    project's existing convention for context-menu-triggered actions."""

    def test_select_text_action_enables_select_text_mode(self):
        self.assertFalse(self.win.select_text_mode)
        self.win.select_text_btn.click()
        self.assertTrue(self.win.select_text_mode)

    def test_draw_action_enables_draw_mode(self):
        self.assertFalse(self.win.draw_mode)
        self.win.draw_btn.click()
        self.assertTrue(self.win.draw_mode)

    def test_add_bookmark_action_adds_a_bookmark(self):
        before = len(self.db.get_bookmarks(self.book["id"]))
        self.win.add_bookmark()
        after = len(self.db.get_bookmarks(self.book["id"]))
        self.assertEqual(after, before + 1)

    def test_menu_only_reachable_in_plain_reading_state(self):
        """Right-click while Select Text or Draw mode is active lands on
        that mode's own overlay (its own existing context menu, for
        editing/deleting whatever was clicked on) rather than page_label
        -- confirmed here by checking which widget is actually
        mouse-transparent (and therefore NOT receiving the click) in
        each state."""
        self.win.toggle_select_text_mode(True)
        self.assertFalse(self.win.text_overlay.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.win.toggle_select_text_mode(False)

        self.win.toggle_draw_mode(True)
        self.assertFalse(self.win.drawing_overlay.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.win.toggle_draw_mode(False)

        # back to plain reading: page_label is what's left to receive it
        self.assertTrue(self.win.text_overlay.testAttribute(Qt.WA_TransparentForMouseEvents))
        self.assertTrue(self.win.drawing_overlay.testAttribute(Qt.WA_TransparentForMouseEvents))


class TestShortcutsDialogWheelSection(unittest.TestCase):
    def setUp(self):
        self.tmp_db = tempfile.mktemp(suffix=".db")
        self.db = Database(self.tmp_db)

    def tearDown(self):
        if os.path.exists(self.tmp_db):
            os.remove(self.tmp_db)

    def test_all_gestures_appear_as_rows(self):
        dlg = ShortcutsDialog(self.db)
        for gesture_id in WHEEL_GESTURES:
            self.assertIn(gesture_id, dlg._wheel_edits)

    def test_shift_and_alt_scroll_do_not_appear_as_rows(self):
        dlg = ShortcutsDialog(self.db)
        self.assertNotIn("shift_scroll", dlg._wheel_edits)
        self.assertNotIn("alt_scroll", dlg._wheel_edits)

    def test_rows_default_to_the_correct_action(self):
        dlg = ShortcutsDialog(self.db)
        self.assertEqual(dlg._wheel_edits["ctrl_scroll"].currentText(), WHEEL_ACTION_ZOOM)
        self.assertEqual(dlg._wheel_edits["right_click_scroll"].currentText(), WHEEL_ACTION_ZOOM)
        self.assertEqual(dlg._wheel_edits["middle_click_scroll"].currentText(), WHEEL_ACTION_PAGE_TURN)

    def test_changing_a_dropdown_updates_result_wheel_overrides(self):
        dlg = ShortcutsDialog(self.db)
        dlg._wheel_edits["middle_click_scroll"].setCurrentText(WHEEL_ACTION_NONE)
        self.assertEqual(dlg.result_wheel_overrides().get("middle_click_scroll"), WHEEL_ACTION_NONE)

    def test_two_gestures_sharing_an_action_is_not_flagged_as_a_conflict(self):
        """The keyboard-shortcut conflict warning must never fire because
        of wheel gestures -- they're not even in the same catalog."""
        dlg = ShortcutsDialog(self.db)
        dlg._wheel_edits["middle_click_scroll"].setCurrentText(WHEEL_ACTION_ZOOM)  # now shares Zoom with ctrl_scroll and right_click_scroll
        self.assertTrue(dlg._save_btn.isEnabled())
        self.assertEqual(dlg._conflict_label.text(), "")

    def test_reset_one_restores_the_default(self):
        dlg = ShortcutsDialog(self.db)
        dlg._wheel_edits["ctrl_scroll"].setCurrentText(WHEEL_ACTION_NONE)
        dlg._reset_one_wheel("ctrl_scroll")
        self.assertEqual(dlg._wheel_edits["ctrl_scroll"].currentText(), WHEEL_ACTION_ZOOM)

    def test_reset_all_restores_wheel_gestures_too(self):
        dlg = ShortcutsDialog(self.db)
        dlg._wheel_edits["ctrl_scroll"].setCurrentText(WHEEL_ACTION_NONE)
        dlg._wheel_edits["middle_click_scroll"].setCurrentText(WHEEL_ACTION_ZOOM)
        dlg._reset_all()
        self.assertEqual(dlg._wheel_edits["ctrl_scroll"].currentText(), WHEEL_ACTION_ZOOM)
        self.assertEqual(dlg._wheel_edits["middle_click_scroll"].currentText(), WHEEL_ACTION_PAGE_TURN)

    def test_save_and_reload_through_the_dialog_round_trips(self):
        dlg = ShortcutsDialog(self.db)
        dlg._wheel_edits["middle_click_scroll"].setCurrentText(WHEEL_ACTION_NONE)
        save_wheel_overrides(self.db, dlg.result_wheel_overrides())

        dlg2 = ShortcutsDialog(self.db)
        self.assertEqual(dlg2._wheel_edits["middle_click_scroll"].currentText(), WHEEL_ACTION_NONE)


if __name__ == "__main__":
    unittest.main()
