"""User-configurable keyboard shortcuts: a catalog of every action that can
have a shortcut, plus loading/saving/resolving a user's overrides.

Kept deliberately light on Qt -- QKeySequence is used only as a string
representation (its own native, portable text format, e.g. "Ctrl+Shift+A"),
never constructed or compared here, so this module's actual logic (catalog
lookup, override resolution, conflict detection) can be unit tested with
plain strings and dicts, no running GUI required.

Overrides are stored as a single JSON blob under one settings key, via the
database's existing get_setting/set_setting -- no schema change needed.
"""
import json

SETTINGS_KEY = "keyboard_shortcuts"

# Each entry: action_id -> (human label, default shortcut string, scope).
# scope is "library" or "reader", matching which window's actions dict it
# belongs to -- purely for grouping in the settings dialog and for knowing
# which open windows need to be told to re-apply after a change.
CATALOG = {
    # --- Library window ---
    "library.add_books": ("Add Book(s)", "", "library"),
    "library.add_folder": ("Add Folder", "", "library"),
    "library.import": ("Import...", "", "library"),
    "library.refresh": ("Refresh Library", "F5", "library"),
    "library.select_all": ("Select All Visible", "Ctrl+A", "library"),
    "library.toggle_select_mode": ("Toggle Select Mode", "", "library"),
    "library.focus_search": ("Focus Search Box", "Ctrl+F", "library"),
    "library.open_shortcuts": ("Open Keyboard Shortcuts", "Ctrl+,", "library"),
    # --- Reader window ---
    "reader.prev_page": ("Previous Page", "Left", "reader"),
    "reader.next_page": ("Next Page", "Right", "reader"),
    "reader.zoom_in": ("Increase Text Size / Zoom In", "=", "reader"),
    "reader.zoom_out": ("Decrease Text Size / Zoom Out", "-", "reader"),
    "reader.toggle_fit_to_screen": ("Toggle Fit to Screen", "F", "reader"),
    "reader.add_bookmark": ("Add Bookmark", "Ctrl+D", "reader"),
    "reader.toggle_select_text": ("Toggle Select Text Mode", "", "reader"),
    "reader.toggle_simple_text": ("Toggle Simple Text Mode", "", "reader"),
    "reader.toggle_two_page": ("Toggle Two-Page View", "", "reader"),
    "reader.close_window": ("Close Reader Window", "Ctrl+W", "reader"),
    "reader.toggle_focus_mode": ("Toggle Focus Mode (Hide All Menus)", "F11", "reader"),
    "reader.next_highlight": ("Jump to Next Highlight", "Ctrl+]", "reader"),
    "reader.prev_highlight": ("Jump to Previous Highlight", "Ctrl+[", "reader"),
    "reader.undo_drawing": ("Undo Last Drawing Stroke", "Ctrl+Z", "reader"),
}

# --- Mouse wheel gestures -----------------------------------------------
# A completely separate, parallel system from CATALOG above: each entry
# there maps ONE action to (at most) one key combination, and two actions
# fighting over the same key is a real conflict, since only one of them
# can actually fire. Wheel gestures work the other way around -- each
# gesture (a modifier key or a held mouse button, combined with scrolling)
# independently maps to AT MOST one action, but the same action can
# legitimately be reachable through more than one gesture at once (e.g.
# both Ctrl+Scroll and Right-Click+Scroll zooming simultaneously is the
# intended, expected setup, not a conflict to warn about) -- so this has
# its own small vocabulary, its own settings key, and no notion of
# "conflict" at all, rather than being shoehorned into the action->key
# model above.
WHEEL_ACTION_NONE = "None"
WHEEL_ACTION_ZOOM = "Zoom In/Out"
WHEEL_ACTION_PAGE_TURN = "Turn Page While Zoomed"
WHEEL_ACTIONS = (WHEEL_ACTION_NONE, WHEEL_ACTION_ZOOM, WHEEL_ACTION_PAGE_TURN)

# gesture_id -> human label, shown as a row in the settings dialog.
# Shift+Scroll and Alt+Scroll are deliberately not offered here: both
# already carry OS/desktop-environment-level meaning on most systems
# (Alt+Scroll commonly changes the scroll axis to horizontal, for
# instance), so claiming them for something else here would fight with
# behavior the user already has outside this app entirely, not just
# inside it.
WHEEL_GESTURES = {
    "ctrl_scroll": "Ctrl + Scroll",
    "middle_click_scroll": "Middle-Click (hold) + Scroll",
    "right_click_scroll": "Right-Click (hold) + Scroll",
}

# gesture_id -> its default action, one of WHEEL_ACTIONS.
WHEEL_GESTURE_DEFAULTS = {
    "ctrl_scroll": WHEEL_ACTION_ZOOM,
    "middle_click_scroll": WHEEL_ACTION_PAGE_TURN,
    "right_click_scroll": WHEEL_ACTION_ZOOM,
}

WHEEL_SETTINGS_KEY = "mouse_wheel_gestures"


def load_overrides(db):
    """{action_id: shortcut_string} for every action the user has
    customized away from its default -- an action with no override at all
    is simply absent from this dict, not present with a copy of its
    default. Silently falls back to "no overrides" for a corrupted or
    missing settings value, rather than ever raising into the caller."""
    raw = db.get_setting(SETTINGS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k in CATALOG and isinstance(v, str)}


def save_overrides(db, overrides):
    """overrides: {action_id: shortcut_string}. An action mapped to "" is
    saved as "explicitly cleared" (no shortcut at all), distinct from an
    action simply absent from the dict (which just means "use the
    default"). Only known action_ids are persisted."""
    cleaned = {k: v for k, v in overrides.items() if k in CATALOG}
    db.set_setting(SETTINGS_KEY, json.dumps(cleaned))


def effective_shortcut(action_id, overrides):
    """The shortcut string actually in effect for `action_id` given a
    loaded overrides dict -- the user's override if they set one
    (including an explicit "" to mean "no shortcut"), otherwise the
    catalog default. Returns "" for an unknown action_id rather than
    raising, since a stale/renamed action_id should just quietly have no
    shortcut instead of crashing whatever's resolving it."""
    if action_id in overrides:
        return overrides[action_id]
    entry = CATALOG.get(action_id)
    return entry[1] if entry else ""


def find_conflicts(overrides):
    """{shortcut_string: [action_id, ...]} for every non-empty shortcut
    currently claimed by more than one action, given a full set of
    effective shortcuts (i.e. after applying `overrides` on top of the
    catalog's defaults) -- used by the settings dialog to warn about, and
    block saving, a duplicate assignment before it causes two actions to
    silently fight over the same key combination."""
    by_shortcut = {}
    for action_id in CATALOG:
        shortcut = effective_shortcut(action_id, overrides)
        if shortcut:
            by_shortcut.setdefault(shortcut, []).append(action_id)
    return {seq: ids for seq, ids in by_shortcut.items() if len(ids) > 1}


def load_wheel_overrides(db):
    """{gesture_id: action_string} for every wheel gesture the user has
    customized away from its default -- same shape and same "absent
    means use the default" convention as load_overrides above, just
    under its own settings key and validated against WHEEL_GESTURES/
    WHEEL_ACTIONS instead of CATALOG."""
    raw = db.get_setting(WHEEL_SETTINGS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if k in WHEEL_GESTURES and v in WHEEL_ACTIONS
    }


def save_wheel_overrides(db, overrides):
    """overrides: {gesture_id: action_string}. Unlike keyboard shortcut
    overrides, there's no "explicitly cleared" state to preserve here --
    every gesture always has a well-defined action (possibly "None"), so
    an override just directly replaces the default. Only known
    gesture_ids and action values are persisted."""
    cleaned = {
        k: v for k, v in overrides.items()
        if k in WHEEL_GESTURES and v in WHEEL_ACTIONS
    }
    db.set_setting(WHEEL_SETTINGS_KEY, json.dumps(cleaned))


def effective_wheel_action(gesture_id, overrides):
    """The action currently assigned to `gesture_id` given a loaded wheel
    overrides dict -- the user's override if they set one, otherwise the
    default from WHEEL_GESTURE_DEFAULTS. Returns WHEEL_ACTION_NONE for an
    unrecognized gesture_id rather than raising."""
    if gesture_id in overrides:
        return overrides[gesture_id]
    return WHEEL_GESTURE_DEFAULTS.get(gesture_id, WHEEL_ACTION_NONE)
