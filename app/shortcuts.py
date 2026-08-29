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
