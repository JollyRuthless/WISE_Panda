"""
ui/dialogs/great_hunt.py — GreatHuntDialog, GreatHuntEntryDelegate, GreatHuntEntriesDialog.
"""

from typing import Optional, List

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QDialogButtonBox,
    QLabel, QLineEdit, QComboBox, QGroupBox, QFormLayout,
    QTableWidget, QTableWidgetItem, QHeaderView, QPushButton, QMessageBox,
    QApplication, QStyledItemDelegate, QScrollArea, QWidget, QProgressDialog,
)
from PyQt6.QtCore import Qt, QTimer, QEvent, QSize
from PyQt6.QtGui import QKeySequence, QShortcut, QIcon, QPixmap, QPainter, QPen, QColor

from engine.aggregator import Fight
from engine.great_hunt import (
    get_choices as hunt_choices,
    get_contextual_choices as contextual_hunt_choices,
    get_recent_context_value as recent_hunt_context_value,
    infer_location_fields as infer_hunt_location_fields,
    known_mob_classifications as known_hunt_mob_classifications,
    clear_annotations as clear_hunt_annotations,
    count_annotation_entries as count_hunt_annotation_entries,
    import_missing_mobs_from_encounter_database,
    list_annotation_entry_page as list_hunt_annotation_entry_page,
    load_annotation as load_hunt_annotation,
    save_annotation as save_hunt_annotation,
    update_entry as update_hunt_entry,
)
from ui.theme import BG_PANEL, BORDER, TEXT_PRI, TEXT_SEC
from ui.window_state import restore_window_state, save_window_state

# ── Classification constants ──────────────────────────────────────────────────

GREAT_HUNT_CLASSIFICATION_CHOICES = [
    "Select",
    "Normal (Weak)",
    "Strong (Silver)",
    "Elite (Gold)",
    "Champion",
    "Boss",
]

GREAT_HUNT_CLASSIFICATION_ALIASES = {
    "normal": "Normal (Weak)", "weak": "Normal (Weak)", "normal (weak)": "Normal (Weak)",
    "strong": "Strong (Silver)", "silver": "Strong (Silver)", "strong (silver)": "Strong (Silver)",
    "elite": "Elite (Gold)", "gold": "Elite (Gold)", "elite (gold)": "Elite (Gold)",
    "champion": "Champion",
    "boss": "Boss",
}

GREAT_HUNT_LOCATION_TYPE_CHOICES = ["Select", "Open World", "Instanced"]
GREAT_HUNT_NONE_OF_THESE = "None of these"


def _draw_action_icon(kind: str, color: str) -> QIcon:
    pixmap = QPixmap(28, 28)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 3)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    if kind == "edit":
        painter.drawLine(7, 20, 19, 8)
        painter.drawLine(17, 6, 22, 11)
        painter.drawLine(6, 22, 15, 22)
    else:
        painter.drawRoundedRect(7, 6, 13, 16, 3, 3)
        painter.drawRoundedRect(11, 10, 13, 16, 3, 3)
    painter.end()
    return QIcon(pixmap)


def _style_hunt_action_button(button: QPushButton, accent: str) -> None:
    button.setFixedSize(96, 34)
    button.setIconSize(QSize(17, 17))
    button.setStyleSheet(
        f"""
        QPushButton {{
            background: {BG_PANEL};
            color: {TEXT_PRI};
            border: 1px solid {BORDER};
            border-radius: 4px;
            padding: 6px 8px;
            font-size: 12px;
            font-weight: 600;
            text-align: left;
        }}
        QPushButton:hover {{
            background: #1f2630;
            border-color: {accent};
        }}
        QPushButton:pressed {{
            background: #0d1117;
        }}
        """
    )


# ── GreatHuntDialog ───────────────────────────────────────────────────────────

class GreatHuntDialog(QDialog):
    def __init__(self, parent, fight: Fight, mob_rows: List[dict], fight_key: str):
        super().__init__(parent)
        self._fight     = fight
        self._mob_rows  = mob_rows
        self._fight_key = fight_key
        self._mob_combos: dict[str, QComboBox] = {}
        self._saved_payload: Optional[dict] = None
        self._detected_location = self._detect_location()
        self._build_ui()
        self._load_existing()

    def _build_ui(self):
        self.setWindowTitle("The Great Hunt")
        self.resize(980, 720)
        root = QVBoxLayout(self)

        title = QLabel("The Great Hunt")
        title.setObjectName("title")
        root.addWidget(title)

        subtitle = QLabel(
            "Your personal guide to everything you have taken on in combat. "
            "Set the fight location once, then classify the mobs below."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(subtitle)

        fight_box = QGroupBox("Fight Details")
        form = QFormLayout(fight_box)

        self.location_combo = QLineEdit()
        self.location_combo.setReadOnly(True)
        self.location_combo.setPlaceholderText("Detected from combat log")
        self.location_combo.setToolTip("Location is detected from the combat log and cannot be edited here.")
        self.zone_combo          = self._make_combo("zone")
        self.location_type_combo = QComboBox()
        self.location_type_combo.addItems(GREAT_HUNT_LOCATION_TYPE_CHOICES)
        self.instance_combo      = self._make_combo("instance")
        self.quest_combo         = self._make_combo("quest")
        self.character_input     = QLineEdit()
        self.character_input.setPlaceholderText("Character that added this entry")
        self.character_input.setText(self._fight.player_name or "")

        self.location_combo.textChanged.connect(self._refresh_zone_choices)
        self.zone_combo.currentTextChanged.connect(self._refresh_instance_choices)
        self.location_type_combo.currentTextChanged.connect(lambda _: self._refresh_instance_choices())

        form.addRow("Location",      self.location_combo)
        form.addRow("Zone",          self.zone_combo)
        form.addRow("Location Type", self.location_type_combo)
        form.addRow("Instance Name", self.instance_combo)
        form.addRow("Quest Name",    self.quest_combo)
        form.addRow("Character Name",self.character_input)
        self.detected_label = QLabel(self._detected_location_text())
        self.detected_label.setWordWrap(True)
        self.detected_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        form.addRow("Detected", self.detected_label)
        root.addWidget(fight_box)

        mob_box = QGroupBox("Mobs In This Fight")
        mob_layout = QVBoxLayout(mob_box)
        hint = QLabel(
            "Known mob types are filled from saved Great Hunt data. "
            "For new mobs, choose a SWTOR mob type from the dropdown."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        mob_layout.addWidget(hint)

        self.mob_table = QTableWidget()
        self.mob_table.setColumnCount(5)
        self.mob_table.setHorizontalHeaderLabels(["Mob", "NPC ID", "Max HP", "Instances", "Classification"])
        self.mob_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.mob_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.mob_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.mob_table.verticalHeader().setVisible(False)
        self.mob_table.setRowCount(len(self._mob_rows))
        mob_layout.addWidget(self.mob_table, 1)
        root.addWidget(mob_box, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save_and_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._populate_rows()
        self._refresh_zone_choices(self.location_combo.text())

    def _make_combo(self, kind: str, parent_value: Optional[str] = None) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.lineEdit().installEventFilter(self)
        combo.lineEdit().setProperty("great_hunt_combo", combo)
        combo.activated.connect(lambda _idx, box=combo: self._handle_combo_choice(box))
        combo.addItem("")
        for value in self._choice_values(kind, parent_value=parent_value):
            combo.addItem(value)
        combo.addItem(GREAT_HUNT_NONE_OF_THESE)
        return combo

    def eventFilter(self, obj, event):
        combo = obj.property("great_hunt_combo") if hasattr(obj, "property") else None
        if isinstance(combo, QComboBox) and event.type() == QEvent.Type.MouseButtonPress:
            QTimer.singleShot(0, combo.showPopup)
        return super().eventFilter(obj, event)

    def _handle_combo_choice(self, combo: QComboBox):
        if combo.currentText().strip() == GREAT_HUNT_NONE_OF_THESE:
            combo.setEditText("")
            combo.setFocus()
            if combo is self.zone_combo:
                self._refresh_instance_choices("")
            elif combo is self.instance_combo:
                self._refresh_quest_choices()

    def _combo_text(self, combo) -> str:
        text = combo.text().strip() if isinstance(combo, QLineEdit) else combo.currentText().strip()
        return "" if text == GREAT_HUNT_NONE_OF_THESE else text

    def _choice_values(self, kind: str, parent_value: Optional[str] = None) -> List[str]:
        location      = self._combo_text(self.location_combo) if hasattr(self, "location_combo") else ""
        zone          = self._combo_text(self.zone_combo)     if hasattr(self, "zone_combo")     else ""
        location_type = self._saved_location_type_text()      if hasattr(self, "location_type_combo") else ""
        if kind == "location":   return contextual_hunt_choices("location")
        if kind == "zone":       return contextual_hunt_choices("zone", location=parent_value or location)
        if kind == "instance":   return contextual_hunt_choices("instance", location=location, zone=parent_value or zone, location_type=location_type)
        if kind == "quest":      return contextual_hunt_choices("quest", location=location, location_type=location_type)
        return hunt_choices(kind, parent_value)

    def _refresh_combo_items(self, combo: QComboBox, values: List[str], keep_text: str, auto_select_single: bool = False):
        keep_text = "" if keep_text == GREAT_HUNT_NONE_OF_THESE else keep_text
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("")
        for value in values:
            combo.addItem(value)
        combo.addItem(GREAT_HUNT_NONE_OF_THESE)
        if keep_text:
            combo.setCurrentText(keep_text)
        elif auto_select_single and len(values) == 1:
            combo.setCurrentText(values[0])
        else:
            combo.setCurrentText("")
        combo.blockSignals(False)

    def _refresh_zone_choices(self, location_name: str):
        location_name = "" if (location_name or "").strip() == GREAT_HUNT_NONE_OF_THESE else location_name
        keep   = self._combo_text(self.zone_combo)
        values = self._choice_values("zone", location_name or None)
        self._refresh_combo_items(self.zone_combo, values, keep, auto_select_single=bool((location_name or "").strip()))
        if location_name and not keep and values and not self._combo_text(self.zone_combo):
            recent_zone = recent_hunt_context_value("zone", location=location_name)
            if recent_zone in values:
                self.zone_combo.blockSignals(True)
                self.zone_combo.setCurrentText(recent_zone)
                self.zone_combo.blockSignals(False)
        self._refresh_location_type_choice()
        self._refresh_instance_choices(self.zone_combo.currentText())
        self._refresh_quest_choices()

    def _refresh_location_type_choice(self):
        keep   = self._saved_location_type_text()
        values = contextual_hunt_choices("location_type", location=self._combo_text(self.location_combo), zone=self._combo_text(self.zone_combo))
        self.location_type_combo.blockSignals(True)
        if keep and (not values or keep in values):
            self.location_type_combo.setCurrentText(keep)
        elif len(values) == 1:
            self.location_type_combo.setCurrentText(values[0])
        elif keep and values and keep not in values:
            self.location_type_combo.setCurrentText("Select")
        self.location_type_combo.blockSignals(False)

    def _refresh_instance_choices(self, zone_name: str = ""):
        zone_name = "" if (zone_name or "").strip() == GREAT_HUNT_NONE_OF_THESE else zone_name
        keep = self._combo_text(self.instance_combo)
        self._refresh_combo_items(self.instance_combo, self._choice_values("instance", zone_name or None), keep,
                                  auto_select_single=bool((zone_name or self._combo_text(self.location_combo)).strip()))
        self._refresh_quest_choices()

    def _refresh_quest_choices(self):
        keep = self._combo_text(self.quest_combo)
        self._refresh_combo_items(self.quest_combo, self._choice_values("quest"), keep,
                                  auto_select_single=bool(self.zone_combo.currentText().strip()))

    def _populate_rows(self):
        def cell(text: str) -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        for row, mob in enumerate(self._mob_rows):
            self.mob_table.setItem(row, 0, cell(mob["mob_name"]))
            self.mob_table.setItem(row, 1, cell(mob["npc_entity_id"]))
            self.mob_table.setItem(row, 2, cell(f"{mob['max_hp_seen']:,}" if mob["max_hp_seen"] else "—"))
            self.mob_table.setItem(row, 3, cell(str(mob["instances_seen"])))
            combo = QComboBox()
            combo.setEditable(False)
            for value in GREAT_HUNT_CLASSIFICATION_CHOICES:
                combo.addItem(value)
            self.mob_table.setCellWidget(row, 4, combo)
            self._mob_combos[mob["mob_key"]] = combo
        self.mob_table.resizeRowsToContents()

    def _classification_display_text(self, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return "Select"
        return GREAT_HUNT_CLASSIFICATION_ALIASES.get(cleaned.lower(), cleaned)

    def _saved_classification_text(self, combo: Optional[QComboBox]) -> str:
        if combo is None:
            return ""
        text = combo.currentText().strip()
        return "" if text == "Select" else text

    def _set_location_type(self, value: str):
        cleaned = (value or "").strip()
        self.location_type_combo.setCurrentText(
            cleaned if cleaned in GREAT_HUNT_LOCATION_TYPE_CHOICES else "Select"
        )

    def _saved_location_type_text(self) -> str:
        text = self.location_type_combo.currentText().strip()
        return "" if text == "Select" else text

    def _detect_location(self) -> dict:
        return infer_hunt_location_fields(
            self._fight._log_path or "",
            line_start=max(self._fight._line_start, 0),
            line_end=self._fight._line_end if self._fight._line_end else None,
        )

    def _detected_location_text(self) -> str:
        if not self._detected_location:
            return "No location could be detected from the log."
        parts = [
            self._detected_location.get("location_name", "").strip(),
            self._detected_location.get("zone_name", "").strip(),
            self._detected_location.get("instance_name", "").strip(),
        ]
        visible = [p for p in parts if p]
        area_name = self._detected_location.get("detected_area_name", "").strip()
        if visible:
            text = " / ".join(visible)
            if area_name and area_name not in visible:
                text += f"  (from {area_name})"
            return text
        return f"{area_name} (log-detected area)" if area_name else "No location could be detected from the log."

    def _apply_detected_location_if_needed(self):
        if not self._combo_text(self.location_combo):
            self.location_combo.setText(self._detected_location.get("location_name", ""))
        if self._combo_text(self.zone_combo) or self._combo_text(self.instance_combo):
            return
        self._refresh_zone_choices(self.location_combo.text())
        detected_zone = self._detected_location.get("zone_name", "")
        if detected_zone:
            self.zone_combo.setCurrentText(detected_zone)
        self._refresh_instance_choices(self.zone_combo.currentText())
        detected_instance = self._detected_location.get("instance_name", "")
        if detected_instance:
            self.instance_combo.setCurrentText(detected_instance)

    def _apply_prefill_fight_info(self, fight_info: dict):
        if not isinstance(fight_info, dict):
            return
        if not self.location_combo.text().strip():
            self.location_combo.setText(fight_info.get("location_name", fight_info.get("planet_name", "")))
        self._refresh_zone_choices(self.location_combo.text())
        if not self._combo_text(self.zone_combo):
            self.zone_combo.setCurrentText(fight_info.get("zone_name", ""))
        self._refresh_instance_choices(self.zone_combo.currentText())
        if not self._saved_location_type_text():
            saved_type     = fight_info.get("location_type", "")
            saved_instance = fight_info.get("instance_name", "")
            if not saved_type and saved_instance in ("Open World", "Instanced"):
                saved_type = saved_instance
            self._set_location_type(saved_type)
        if not self._combo_text(self.instance_combo):
            saved_instance = fight_info.get("instance_name", "")
            if saved_instance not in ("Open World", "Instanced"):
                self.instance_combo.setCurrentText(saved_instance)
        if not self._combo_text(self.quest_combo):
            self.quest_combo.setCurrentText(fight_info.get("quest_name", ""))
        if not self.character_input.text().strip():
            self.character_input.setText(fight_info.get("character_name", self._fight.player_name or ""))

    def _load_existing(self):
        existing = load_hunt_annotation(self._fight_key)
        if not existing:
            self._apply_detected_location_if_needed()
            parent = self.parent()
            if parent is not None and hasattr(parent, "_suggest_hunt_prefill"):
                self._apply_prefill_fight_info(parent._suggest_hunt_prefill(self._fight))
            mobs = {}
        else:
            fight_info     = existing.get("fight", {})
            mobs           = existing.get("mobs", {})
            self.location_combo.setText(fight_info.get("location_name", fight_info.get("planet_name", "")))
            self._refresh_zone_choices(self.location_combo.text())
            self.zone_combo.setCurrentText(fight_info.get("zone_name", ""))
            self._refresh_instance_choices(self.zone_combo.currentText())
            saved_instance = fight_info.get("instance_name", "")
            saved_type     = fight_info.get("location_type", "")
            if not saved_type and saved_instance in ("Open World", "Instanced"):
                saved_type     = saved_instance
                saved_instance = ""
            self._set_location_type(saved_type)
            self.instance_combo.setCurrentText(saved_instance)
            self.quest_combo.setCurrentText(fight_info.get("quest_name", ""))
            self.character_input.setText(fight_info.get("character_name", self._fight.player_name or ""))
            if not (self.location_combo.text().strip() or self._combo_text(self.zone_combo)
                    or self._saved_location_type_text() or self._combo_text(self.instance_combo)):
                self._apply_detected_location_if_needed()

        known_classifications = known_hunt_mob_classifications(
            [mob["mob_key"] for mob in self._mob_rows],
            self._detected_location,
            self._fight_key,
        )
        for mob in self._mob_rows:
            mob_data = mobs.get(mob["mob_key"], {})
            combo = self._mob_combos.get(mob["mob_key"])
            if combo is not None:
                combo.setCurrentText(self._classification_display_text(
                    mob_data.get("classification", "") or known_classifications.get(mob["mob_key"], "")
                ))

    def _save_and_accept(self):
        existing = load_hunt_annotation(self._fight_key)
        existing_fight = existing.get("fight", {}) if isinstance(existing, dict) else {}
        existing_mobs = existing.get("mobs", {}) if isinstance(existing, dict) else {}
        payload = {
            "fight": {
                "location_name": self._combo_text(self.location_combo),
                "zone_name":     self._combo_text(self.zone_combo),
                "location_type": self._saved_location_type_text(),
                "instance_name": self._combo_text(self.instance_combo),
                "quest_name":    self._combo_text(self.quest_combo),
                "character_name":self.character_input.text().strip(),
                "fight_label":   self._fight.label,
                "log_path":      self._fight._log_path or "",
                "fight_date":    existing_fight.get("fight_date", ""),
            },
            "mobs": {},
        }
        automatic_fields = (
            "abilities_used", "kill_count", "total_damage_taken", "total_damage_done",
            "largest_hit_taken_amount", "largest_hit_taken_by", "largest_hit_taken_ability",
            "largest_hit_done_amount", "largest_hit_done_target", "largest_hit_done_ability",
            "first_seen_date", "first_killed_date", "last_kill_date",
        )
        for mob in self._mob_rows:
            combo = self._mob_combos.get(mob["mob_key"])
            existing_mob = existing_mobs.get(mob["mob_key"], {}) if isinstance(existing_mobs, dict) else {}
            saved_mob = {
                "mob_name":        mob["mob_name"],
                "npc_entity_id":   mob["npc_entity_id"],
                "classification":  self._saved_classification_text(combo),
                "max_hp_seen":     mob["max_hp_seen"],
                "instances_seen":  mob["instances_seen"],
            }
            for field in automatic_fields:
                if isinstance(existing_mob, dict) and field in existing_mob:
                    saved_mob[field] = existing_mob[field]
            payload["mobs"][mob["mob_key"]] = saved_mob
        save_hunt_annotation(self._fight_key, payload)
        self._saved_payload = payload
        self.accept()

    @property
    def saved_payload(self) -> Optional[dict]:
        return self._saved_payload


# ── GreatHuntEntryDelegate ────────────────────────────────────────────────────

class GreatHuntEntryDelegate(QStyledItemDelegate):
    def __init__(self, dialog, field_name: str):
        super().__init__(dialog.table)
        self._dialog     = dialog
        self._field_name = field_name

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(True)
        current_text = index.data(Qt.ItemDataRole.EditRole) or index.data() or ""
        combo.addItem("")
        for value in self._dialog._choices_for_cell(index.row(), self._field_name):
            combo.addItem(value)
        combo.setCurrentText(str(current_text).strip())
        return combo

    def setEditorData(self, editor, index):
        editor.setCurrentText(str(index.data() or "").strip())

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText().strip(), Qt.ItemDataRole.EditRole)


class GreatHuntEntryEditDialog(QDialog):
    FIELD_LABELS = [
        ("npc_entity_id", "Mob ID"),
        ("mob_name", "Mob Name"),
        ("classification", "Type"),
        ("location", "Location"),
        ("zone", "Zone"),
        ("location_type", "Location Type"),
        ("instance_name", "Instance Name"),
        ("quest_name", "Quest Name"),
        ("character_name", "First Recorded By"),
        ("max_hp_seen", "Max HP"),
        ("mob_count", "Seen"),
        ("kill_count", "Kills"),
        ("total_damage_taken", "Damage To Mob"),
        ("total_damage_done", "Damage From Mob"),
        ("largest_hit_taken_amount", "Best Hit On Mob"),
        ("largest_hit_taken_by", "Hit By"),
        ("largest_hit_taken_ability", "Hit Ability"),
        ("largest_hit_done_amount", "Biggest Mob Hit"),
        ("largest_hit_done_target", "Hit Target"),
        ("largest_hit_done_ability", "Mob Hit Ability"),
        ("first_seen_date", "First Seen"),
        ("first_killed_date", "First Killed"),
        ("last_kill_date", "Last Kill"),
        ("first_seen_by", "First Seen By"),
        ("last_seen_by", "Last Seen By"),
        ("last_zone_loaded", "Last Zone Loaded"),
        ("abilities_used", "Abilities"),
        ("conflict", "Conflict"),
        ("toughness", "Toughness"),
        ("journal_entry", "Journal Entry"),
        ("fight_notes", "Fight Notes"),
        ("picture_path", "Picture Path"),
    ]

    def __init__(self, parent, row: dict):
        super().__init__(parent)
        self._row = dict(row)
        self._inputs: dict[str, QLineEdit] = {}
        self.setWindowTitle(f"Edit Great Hunt Entry - {row.get('mob_name') or row.get('npc_entity_id')}")
        restore_window_state(self, "great_hunt_entry_editor", (640, 720))
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel(self._row.get("mob_name", "Mob Entry"))
        title.setObjectName("title")
        title.setToolTip(f"Mob ID: {self._row.get('npc_entity_id', '')}")
        root.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        form_host = QWidget()
        form = QFormLayout(form_host)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        for field, label in self.FIELD_LABELS:
            editor = QLineEdit(str(self._row.get(field, "")))
            if field == "npc_entity_id":
                editor.setReadOnly(True)
            editor.setToolTip(str(self._row.get(field, "")))
            self._inputs[field] = editor
            form.addRow(label, editor)
        scroll.setWidget(form_host)
        root.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self):
        npc_id = self._row.get("npc_entity_id", "")
        updates = {
            field: editor.text().strip()
            for field, editor in self._inputs.items()
            if field != "npc_entity_id"
        }
        update_hunt_entry(npc_id, updates)
        self.accept()

    def closeEvent(self, event):
        save_window_state(self, "great_hunt_entry_editor")
        super().closeEvent(event)


# ── GreatHuntEntriesDialog ────────────────────────────────────────────────────

class GreatHuntEntriesDialog(QDialog):
    PAGE_SIZE = 100
    COLUMN_FIELDS = [
        "mob_name", "classification", "location", "zone", "max_hp_seen",
        "kill_count", "first_seen_date", "last_seen_by", "abilities_used",
    ]
    ACTION_COLUMN = len(COLUMN_FIELDS)

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Great Hunt Entries")
        restore_window_state(self, "great_hunt_entries", (1120, 720))
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._total_count = 0
        self._filtered_count = 0
        self._conflict_count = 0
        self._rows = []
        self._page_index = 0
        self._updating_table = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(60_000)
        self._refresh_timer.timeout.connect(self._refresh_rows)
        self._refresh_timer.start()
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        title = QLabel("Great Hunt Entries")
        title.setObjectName("title")
        root.addWidget(title)

        conflict_count = sum(1 for row in self._rows if row["conflict"])
        self.summary = QLabel(
            f"{len(self._rows):,} unique NPC IDs"
            + (f" - {conflict_count:,} conflicts need review" if conflict_count else "")
        )
        self.summary.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        root.addWidget(self.summary)

        filter_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search mob")
        self.search_input.textChanged.connect(self._apply_search_filter)
        filter_row.addWidget(self.search_input, 1)
        self.import_missing_btn = QPushButton("Pull Missing Mobs From DB")
        self.import_missing_btn.clicked.connect(self._import_missing_database_mobs)
        filter_row.addWidget(self.import_missing_btn)
        root.addLayout(filter_row)

        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMN_FIELDS) + 1)
        self.table.setHorizontalHeaderLabels([
            "Mob Name", "Type", "Location", "Zone", "Max HP", "Kills",
            "First Seen", "Last Seen By", "Abilities", "",
        ])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setRowCount(len(self._rows))
        self.table.cellDoubleClicked.connect(self._edit_entry_at_row)
        root.addWidget(self.table, 1)
        self.table.itemChanged.connect(self._on_item_changed)

        QShortcut(QKeySequence.StandardKey.Copy,  self.table, activated=self._copy_selection)

        action_row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self._refresh_rows)
        action_row.addWidget(self.refresh_btn)
        self.prev_page_btn = QPushButton("Previous 100")
        self.prev_page_btn.clicked.connect(self._previous_page)
        action_row.addWidget(self.prev_page_btn)
        self.page_label = QLabel("")
        self.page_label.setStyleSheet(f"color:{TEXT_SEC}; font-size:11px;")
        action_row.addWidget(self.page_label)
        self.next_page_btn = QPushButton("Next 100")
        self.next_page_btn.clicked.connect(self._next_page)
        action_row.addWidget(self.next_page_btn)
        self.delete_all_btn = QPushButton("Delete All")
        self.delete_all_btn.clicked.connect(self._delete_all_entries)
        action_row.addWidget(self.delete_all_btn)
        action_row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        action_row.addWidget(buttons)
        root.addLayout(action_row)
        self._configure_table_columns()
        self._apply_search_filter()

    def _refresh_rows(self):
        self._apply_search_filter()

    def _apply_search_filter(self):
        self._page_index = 0
        self._apply_page()

    def _apply_page(self):
        needle = self.search_input.text().strip().lower() if hasattr(self, "search_input") else ""
        counts = count_hunt_annotation_entries(needle)
        self._total_count = counts["total"]
        self._filtered_count = counts["filtered"]
        self._conflict_count = counts["conflicts"]
        page_count = self._page_count()
        if self._page_index >= page_count:
            self._page_index = max(page_count - 1, 0)
        self._rows = list_hunt_annotation_entry_page(
            needle,
            limit=self.PAGE_SIZE,
            offset=self._page_index * self.PAGE_SIZE,
        )
        self._update_summary()
        self.table.setRowCount(len(self._rows))
        self._populate_rows()
        self._update_page_controls()

    def _page_count(self) -> int:
        if not self._filtered_count:
            return 1
        return (self._filtered_count + self.PAGE_SIZE - 1) // self.PAGE_SIZE

    def _previous_page(self):
        if self._page_index <= 0:
            return
        self._page_index -= 1
        self._apply_page()

    def _next_page(self):
        if self._page_index >= self._page_count() - 1:
            return
        self._page_index += 1
        self._apply_page()

    def _update_page_controls(self):
        page_count = self._page_count()
        if hasattr(self, "prev_page_btn"):
            self.prev_page_btn.setEnabled(self._page_index > 0)
        if hasattr(self, "next_page_btn"):
            self.next_page_btn.setEnabled(self._page_index < page_count - 1)
        if hasattr(self, "page_label"):
            if self._filtered_count:
                start = self._page_index * self.PAGE_SIZE + 1
                end = min(start + len(self._rows) - 1, self._filtered_count)
                self.page_label.setText(f"{start:,}-{end:,} of {self._filtered_count:,}")
            else:
                self.page_label.setText("0 of 0")

    def _update_summary(self):
        if self._filtered_count == self._total_count:
            text = f"{self._total_count:,} unique NPC IDs"
        else:
            text = f"{self._filtered_count:,} of {self._total_count:,} unique NPC IDs"
        if self._conflict_count:
            text += f" - {self._conflict_count:,} conflicts need review"
        self.summary.setText(text)

    def _import_missing_database_mobs(self):
        self.import_missing_btn.setEnabled(False)
        progress = QProgressDialog("Preparing encounter database scan...", "Cancel", 0, 0, self)
        progress.setWindowTitle("Pull Missing Mobs From DB")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()

        def update_progress(current: int, total: int, message: str) -> bool:
            if total > 0:
                progress.setRange(0, total)
                progress.setValue(min(current, total))
                progress.setLabelText(f"{message}\n{current:,}/{total:,} log rows")
            else:
                progress.setRange(0, 0)
                progress.setLabelText(message)
            QApplication.processEvents()
            return not progress.wasCanceled()

        try:
            result = import_missing_mobs_from_encounter_database(progress_callback=update_progress)
        except Exception as exc:
            QMessageBox.warning(self, "Pull Missing Mobs From DB", f"Could not pull missing mobs: {exc}")
            return
        finally:
            progress.close()
            self.import_missing_btn.setEnabled(True)
        self._refresh_rows()
        QMessageBox.information(
            self,
            "Pull Missing Mobs From DB",
            "Great Hunt table updated.\n"
            f"Added: {result.get('added', 0):,}\n"
            f"Updated: {result.get('updated', 0):,}\n"
            f"Scanned: {result.get('processed', 0):,} log rows",
        )

    def _delete_all_entries(self):
        if not self._rows:
            return
        reply = QMessageBox.question(
            self, "Delete Great Hunt Entries",
            "Delete all saved Great Hunt entries? This is intended for testing and cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        clear_hunt_annotations()
        self._refresh_rows()

    def closeEvent(self, event):
        save_window_state(self, "great_hunt_entries")
        self._refresh_timer.stop()
        window = self.parent()
        if window is not None and hasattr(window, "_great_hunt_entries_dialog"):
            window._great_hunt_entries_dialog = None
        super().closeEvent(event)

    def _populate_rows(self):
        self._updating_table = True
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        was_sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.clearContents()

        def cell(text: str, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter) -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            item.setTextAlignment(align)
            item.setToolTip(text)
            return item

        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for row_idx, row in enumerate(self._rows):
            for col_idx, field in enumerate(self.COLUMN_FIELDS):
                value = row.get(field, "")
                if field == "classification":
                    value = value or "Select"
                align = right if field in {"max_hp_seen", "kill_count"} else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                item = cell(value, align)
                if field == "mob_name":
                    item.setToolTip(f"{value}\nMob ID: {row.get('npc_entity_id', '')}")
                self.table.setItem(row_idx, col_idx, item)
            self.table.setCellWidget(row_idx, self.ACTION_COLUMN, self._action_widget(row))

        self.table.setSortingEnabled(was_sorting)
        self.table.blockSignals(False)
        self.table.setUpdatesEnabled(True)
        self._updating_table = False

    def _configure_table_columns(self):
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(self.ACTION_COLUMN, QHeaderView.ResizeMode.Fixed)
        widths = {
            1: 125, 2: 135, 3: 135, 4: 85, 5: 70, 6: 95, 7: 130, 8: 300, 9: 112,
        }
        for col, width in widths.items():
            if col < self.table.columnCount():
                self.table.setColumnWidth(col, width)

    def _action_widget(self, row: dict) -> QWidget:
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        copy_btn = QPushButton("Copy")
        copy_btn.setIcon(_draw_action_icon("copy", "#8b6ff5"))
        copy_btn.setToolTip("Copy")
        _style_hunt_action_button(copy_btn, "#8b6ff5")
        copy_btn.clicked.connect(lambda _checked=False, npc_id=row.get("npc_entity_id", ""): self._copy_mob_info(npc_id))
        layout.addWidget(copy_btn)
        return host

    def _edit_entry_at_row(self, row: int, _column: int):
        if 0 <= row < len(self._rows):
            self._edit_entry(self._rows[row].get("npc_entity_id", ""))

    def _row_for_npc_id(self, npc_id: str) -> dict:
        npc_id = str(npc_id or "").strip()
        return next((row for row in self._rows if str(row.get("npc_entity_id") or "").strip() == npc_id), {})

    def _edit_entry(self, npc_id: str):
        row = self._row_for_npc_id(npc_id)
        if not row:
            return
        dlg = GreatHuntEntryEditDialog(self, row)
        if dlg.exec():
            self._apply_page()

    def _copy_mob_info(self, npc_id: str):
        row = self._row_for_npc_id(npc_id)
        if not row:
            return
        lines = [
            f"Mob Name: {row.get('mob_name', '')}",
            f"Mob ID: {row.get('npc_entity_id', '')}",
            f"Type: {row.get('classification', '')}",
            f"Location: {row.get('location', '')}",
            f"Zone: {row.get('zone', '')}",
            f"Max HP: {row.get('max_hp_seen', '')}",
            f"Kills: {row.get('kill_count', '')}",
            f"First Seen: {row.get('first_seen_date', '')}",
            f"Last Seen By: {row.get('last_seen_by', '')}",
            f"Abilities: {row.get('abilities_used', '')}",
        ]
        QApplication.clipboard().setText("\n".join(lines))

    def _field_for_column(self, column: int) -> str:
        return self.COLUMN_FIELDS[column]

    def _row_context(self, row: int) -> dict:
        def text(col: int) -> str:
            item = self.table.item(row, col)
            return item.text().strip() if item else ""
        return {
            "location": text(self.COLUMN_FIELDS.index("location")),
            "zone": text(self.COLUMN_FIELDS.index("zone")),
            "location_type": text(self.COLUMN_FIELDS.index("location_type")),
        }

    def _choices_for_cell(self, row: int, field_name: str) -> List[str]:
        context = self._row_context(row)
        if field_name == "classification":
            return [v for v in GREAT_HUNT_CLASSIFICATION_CHOICES if v != "Select"]
        if field_name == "location":       return contextual_hunt_choices("location")
        if field_name == "zone":           return contextual_hunt_choices("zone", location=context["location"])
        if field_name == "location_type":
            return contextual_hunt_choices("location_type", location=context["location"], zone=context["zone"]) \
                or [v for v in GREAT_HUNT_LOCATION_TYPE_CHOICES if v != "Select"]
        if field_name == "instance_name":
            return contextual_hunt_choices("instance", location=context["location"], zone=context["zone"], location_type=context["location_type"])
        if field_name == "quest_name":
            return contextual_hunt_choices("quest", location=context["location"], location_type=context["location_type"])
        return []

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._updating_table or item is None:
            return
        row = item.row()
        npc_item = self.table.item(row, 0)
        if npc_item is None:
            return
        field_name = self._field_for_column(item.column())
        if field_name in (
            "npc_entity_id", "mob_count", "kill_count", "total_damage_taken",
            "total_damage_done", "largest_hit_taken_amount", "largest_hit_taken_by",
            "largest_hit_done_amount", "largest_hit_done_target", "first_seen_date",
            "first_killed_date", "last_kill_date", "last_seen_by", "abilities_used",
            "conflict",
        ):
            return
        update_hunt_entry(npc_item.text().strip(), {field_name: item.text().strip()})
        self._apply_page()

    def _selection_bounds(self):
        indexes = self.table.selectedIndexes()
        if not indexes:
            return None
        rows = [i.row() for i in indexes]
        cols = [i.column() for i in indexes]
        return min(rows), max(rows), min(cols), max(cols)

    def _copy_selection(self):
        bounds = self._selection_bounds()
        if not bounds:
            return
        top, bottom, left, right = bounds
        lines = []
        for row in range(top, bottom + 1):
            item_row = [self.table.item(row, col) for col in range(left, right + 1)]
            lines.append("\t".join(item.text() if item else "" for item in item_row))
        QApplication.clipboard().setText("\n".join(lines))

    def _paste_selection(self):
        start = self.table.currentIndex()
        if not start.isValid():
            return
        text = QApplication.clipboard().text()
        if not text.strip():
            return
        self.table.blockSignals(True)
        self._updating_table = True
        try:
            for row_offset, line in enumerate(text.splitlines()):
                for col_offset, value in enumerate(line.split("\t")):
                    row = start.row() + row_offset
                    col = start.column() + col_offset
                    if row >= self.table.rowCount() or col >= self.table.columnCount() or self._field_for_column(col) in (
                        "npc_entity_id", "mob_count", "kill_count", "total_damage_taken",
                        "total_damage_done", "largest_hit_taken_amount", "largest_hit_taken_by",
                        "largest_hit_done_amount", "largest_hit_done_target", "first_seen_date",
                        "first_killed_date", "last_kill_date", "last_seen_by", "abilities_used",
                        "conflict",
                    ):
                        continue
                    item = self.table.item(row, col)
                    if item is None:
                        item = QTableWidgetItem("")
                        self.table.setItem(row, col, item)
                    item.setText(value.strip())
        finally:
            self._updating_table = False
            self.table.blockSignals(False)
        self._persist_changed_cells_from_selection(start.row(), start.column(), text)

    def _persist_changed_cells_from_selection(self, start_row: int, start_col: int, pasted_text: str):
        for row_offset, line in enumerate(pasted_text.splitlines()):
            row = start_row + row_offset
            npc_item = self.table.item(row, 0)
            if npc_item is None:
                continue
            updates = {}
            for col_offset, value in enumerate(line.split("\t")):
                col = start_col + col_offset
                if col >= self.table.columnCount() or self._field_for_column(col) in (
                    "npc_entity_id", "mob_count", "kill_count", "total_damage_taken",
                    "total_damage_done", "largest_hit_taken_amount", "largest_hit_taken_by",
                    "largest_hit_done_amount", "largest_hit_done_target", "first_seen_date",
                    "first_killed_date", "last_kill_date", "last_seen_by", "abilities_used",
                    "conflict",
                ):
                    continue
                updates[self._field_for_column(col)] = value.strip()
            if updates:
                update_hunt_entry(npc_item.text().strip(), updates)
        self._refresh_rows()

    def _cut_selection(self):
        self._copy_selection()
        bounds = self._selection_bounds()
        if not bounds:
            return
        top, bottom, left, right = bounds
        self.table.blockSignals(True)
        self._updating_table = True
        try:
            for row in range(top, bottom + 1):
                npc_item = self.table.item(row, 0)
                if npc_item is None:
                    continue
                updates = {}
                for col in range(left, right + 1):
                    if self._field_for_column(col) in (
                        "npc_entity_id", "mob_count", "kill_count", "total_damage_taken",
                        "total_damage_done", "largest_hit_taken_amount", "largest_hit_taken_by",
                        "largest_hit_done_amount", "largest_hit_done_target", "first_seen_date",
                        "first_killed_date", "last_kill_date", "last_seen_by", "abilities_used",
                        "conflict",
                    ):
                        continue
                    item = self.table.item(row, col)
                    if item is None:
                        continue
                    item.setText("")
                    updates[self._field_for_column(col)] = ""
                if updates:
                    update_hunt_entry(npc_item.text().strip(), updates)
        finally:
            self._updating_table = False
            self.table.blockSignals(False)
        self._refresh_rows()
