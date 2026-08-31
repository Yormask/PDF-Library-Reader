"""The Keyboard Shortcuts settings dialog -- lists every customizable
action grouped by window, lets the user record a new key combination for
any of them via QKeySequenceEdit, flags conflicts in real time, and only
allows saving once none remain.

A second, separate section further down lists the three mouse wheel
gestures (Ctrl+Scroll, Middle/Right-Click held + Scroll -- Shift+Scroll
and Alt+Scroll are deliberately not offered, since both already carry
OS-level meaning on most systems that this shouldn't compete with),
each assignable to an action via a dropdown -- there's no keyboard-style
"type a combo" way to capture a mouse gesture, and unlike the action->key
shortcuts above, more than one gesture can legitimately point at the same
action at once (both Ctrl+Scroll and Right-Click+Scroll zooming
simultaneously by default, for instance), so this section has its own
data model entirely (see WHEEL_GESTURES/WHEEL_ACTIONS in shortcuts.py)
and no conflict checking -- there's nothing to conflict.
"""
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .shortcuts import (
    CATALOG, WHEEL_ACTIONS, WHEEL_GESTURE_DEFAULTS, WHEEL_GESTURES, effective_shortcut,
    effective_wheel_action, find_conflicts, load_overrides, load_wheel_overrides,
)


class ShortcutsDialog(QDialog):
    """Changes are staged in this dialog's own working copy and only
    written back to the database if the user clicks Save; Cancel (or
    closing the dialog) discards them entirely. Call result_overrides()
    and result_wheel_overrides() after exec() returns QDialog.Accepted to
    get the final mappings to persist."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(480, 640)
        self._overrides = dict(load_overrides(db))
        self._edits = {}
        self._wheel_overrides = dict(load_wheel_overrides(db))
        self._wheel_edits = {}

        outer = QVBoxLayout(self)

        intro = QLabel(
            "Click a shortcut field and press a new key combination to change it. "
            "Backspace clears a field entirely."
        )
        intro.setWordWrap(True)
        outer.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        form = QVBoxLayout(holder)

        for scope, heading in (("library", "Library"), ("reader", "Reader")):
            section_label = QLabel(heading)
            section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
            form.addWidget(section_label)
            for action_id, (label, _default, action_scope) in CATALOG.items():
                if action_scope != scope:
                    continue
                row = QHBoxLayout()
                row.addWidget(QLabel(label), 1)
                edit = QKeySequenceEdit(QKeySequence(effective_shortcut(action_id, self._overrides)))
                if hasattr(edit, "setMaximumSequenceLength"):
                    edit.setMaximumSequenceLength(1)  # a single combo, matching every shortcut elsewhere in the app
                edit.keySequenceChanged.connect(lambda _seq, aid=action_id: self._on_edit_changed(aid))
                self._edits[action_id] = edit
                row.addWidget(edit)
                reset_btn = QPushButton("Reset")
                reset_btn.setFixedWidth(60)
                reset_btn.clicked.connect(lambda _checked=False, aid=action_id: self._reset_one(aid))
                row.addWidget(reset_btn)
                form.addLayout(row)

        wheel_heading = QLabel("Mouse Wheel Gestures")
        wheel_heading.setStyleSheet("font-weight: bold; margin-top: 8px;")
        form.addWidget(wheel_heading)
        wheel_note = QLabel(
            "More than one gesture can be set to the same action -- both Ctrl+Scroll "
            "and Right-Click held+Scroll zoom by default, for instance."
        )
        wheel_note.setWordWrap(True)
        wheel_note.setStyleSheet("color: palette(mid);")
        form.addWidget(wheel_note)
        for gesture_id, label in WHEEL_GESTURES.items():
            row = QHBoxLayout()
            row.addWidget(QLabel(label), 1)
            combo = QComboBox()
            combo.addItems(WHEEL_ACTIONS)
            combo.setCurrentText(effective_wheel_action(gesture_id, self._wheel_overrides))
            combo.currentTextChanged.connect(lambda text, gid=gesture_id: self._on_wheel_changed(gid, text))
            self._wheel_edits[gesture_id] = combo
            row.addWidget(combo)
            reset_btn = QPushButton("Reset")
            reset_btn.setFixedWidth(60)
            reset_btn.clicked.connect(lambda _checked=False, gid=gesture_id: self._reset_one_wheel(gid))
            row.addWidget(reset_btn)
            form.addLayout(row)

        scroll.setWidget(holder)
        outer.addWidget(scroll)

        self._conflict_label = QLabel("")
        self._conflict_label.setStyleSheet("color: #c0392b;")
        self._conflict_label.setWordWrap(True)
        outer.addWidget(self._conflict_label)

        btn_row = QHBoxLayout()
        reset_all_btn = QPushButton("Reset All to Defaults")
        reset_all_btn.clicked.connect(self._reset_all)
        btn_row.addWidget(reset_all_btn)
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        self._save_btn = QPushButton("Save")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._save_btn)
        outer.addLayout(btn_row)

        self._update_conflicts()

    def _on_edit_changed(self, action_id):
        self._overrides[action_id] = self._edits[action_id].keySequence().toString()
        self._update_conflicts()

    def _on_wheel_changed(self, gesture_id, text):
        self._wheel_overrides[gesture_id] = text
        # No conflict check here by design -- see the class/module
        # docstring: two gestures sharing an action is intentional, not
        # an error, so there's nothing for _update_conflicts to flag.

    def _reset_one(self, action_id):
        default = CATALOG[action_id][1]
        self._edits[action_id].setKeySequence(QKeySequence(default))
        # setKeySequence() above fires keySequenceChanged, which routes
        # through _on_edit_changed and updates self._overrides itself --
        # nothing further to do here.

    def _reset_one_wheel(self, gesture_id):
        self._wheel_edits[gesture_id].setCurrentText(WHEEL_GESTURE_DEFAULTS[gesture_id])
        # setCurrentText() above fires currentTextChanged, which routes
        # through _on_wheel_changed -- nothing further to do here.

    def _reset_all(self):
        for action_id, edit in self._edits.items():
            edit.setKeySequence(QKeySequence(CATALOG[action_id][1]))
        for gesture_id, combo in self._wheel_edits.items():
            combo.setCurrentText(WHEEL_GESTURE_DEFAULTS[gesture_id])

    def _update_conflicts(self):
        conflicts = find_conflicts(self._overrides)
        if conflicts:
            lines = [
                f'"{seq}" is used by: ' + ", ".join(CATALOG[a][0] for a in ids)
                for seq, ids in conflicts.items()
            ]
            self._conflict_label.setText("\n".join(lines))
            self._save_btn.setEnabled(False)
        else:
            self._conflict_label.setText("")
            self._save_btn.setEnabled(True)

    def result_overrides(self):
        """{action_id: shortcut_string} to persist -- call only after
        exec() returns QDialog.Accepted."""
        return dict(self._overrides)

    def result_wheel_overrides(self):
        """{gesture_id: action_string} to persist -- call only after
        exec() returns QDialog.Accepted."""
        return dict(self._wheel_overrides)
