"""
ui/main_window.py — MainWindow: top-level application window.

Imports all ui submodules. Contains only wiring logic — no widget code.
"""

import sys
import os
import shutil
import time as time_mod
from pathlib import Path
from typing import Callable, List, Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QTabWidget, QLabel,
    QPushButton, QFileDialog, QStatusBar, QComboBox, QInputDialog,
    QDialog, QDialogButtonBox, QPlainTextEdit, QListView, QAbstractItemView,
    QProgressDialog, QMenu, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QTextCursor, QPixmap

from engine.parser_core import parse_file, LogEvent
from engine.aggregator import (
    build_fights, scan_fights, resolve_fight_names,
    Fight, EntityKind,
)
from storage.encounter_db import (
    init_db, upsert_fight, import_combat_log, sync_import_ledger,
    DuplicateCombatLogImportError, is_combat_log_imported,
    seed_player_character_from_log, seed_player_characters_from_logs,
    sync_seen_player_cache, get_database_dashboard_snapshot,
)
from engine.great_hunt import (
    infer_location_fields as infer_hunt_location_fields,
    has_complete_annotation as has_complete_hunt_annotation,
    list_annotation_entries as list_hunt_annotation_entries,
    load_annotation as load_hunt_annotation,
    save_annotation as save_hunt_annotation,
    save_automatic_fight_data as save_automatic_hunt_fight_data,
    import_missing_mobs_from_encounter_database,
    known_mob_classifications as known_hunt_mob_classifications,
)
from ui.theme import (
    STYLESHEET, BG_PANEL, BORDER, TEXT_SEC,
    ENCOUNTER_BOSS_BG, ENCOUNTER_BOSS_FG,
    ENCOUNTER_TRASH_BG, ENCOUNTER_TRASH_FG,
    GREAT_HUNT_ICON_PATH,
)
from ui.settings import settings as _settings
from ui.app_icon import apply_app_icon, configured_icon_path, set_custom_app_icon
from ui.features import TabFeature, build_tab_features
from ui.tabs.dashboard import DashboardSnapshot
from ui.widgets import DraggableToolbar, EncounterListDelegate, style_encounter_toggle
from ui.window_state import restore_window_state, save_window_state
from ui.watcher import LogWatcherThread
from ui.live.tracker import LiveFightTracker
from ui.live.battle import LiveBattleWindow
from ui.live.threat import LiveThreatWindow
from ui.dialogs.great_hunt import GreatHuntDialog
from ui.dialogs.encounter import EncounterDataDialog
from ui.dialogs.characters import CharacterListDialog, CharacterAbilitiesDialog
from ui.dialogs.character_database import ImportedCharacterListDialog, ImportedCharacterAbilitiesDialog
from ui.dialogs.import_history import ImportHistoryDialog
from ui.dialogs.player_roster import PlayerRosterDialog, SeenPlayerAbilitiesDialog
from ui.dialogs.player_notes import SeenPlayerNoteDialog
from ui.dialogs.settings import SettingsDialog


class EncounterListWidget(QListWidget):
    """
    QListWidget for the encounter list with two Phase G adjustments:

    1. Right-click does NOT change the current selection. By default, Qt
       treats a right-click on a list item as a selection event, which would
       fire currentRowChanged and re-load the fight in all the analysis
       tabs. That makes the right-click context menu feel laggy and changes
       what the user is looking at unexpectedly.

    2. Right-click directly emits customContextMenuRequested with the
       click position, which the parent connects to its handler. This is
       the same as the default CustomContextMenu policy, but happens AFTER
       we've already swallowed the selection-change side effect.

    Everything else (left-click, keyboard navigation, scrolling) continues
    to work normally.
    """

    def mousePressEvent(self, event):
        # Right-click: do not let the default handler change the selection.
        # We still want the context menu request to fire, which Qt does
        # automatically for widgets with CustomContextMenu policy as long
        # as we don't accept the event ourselves. Calling event.accept()
        # would prevent the contextMenuEvent.
        if event.button() == Qt.MouseButton.RightButton:
            # Don't call super(). Don't call event.accept(). Just let Qt
            # bubble up to its context-menu plumbing.
            return
        super().mousePressEvent(event)


class MainWindow(QMainWindow):
    def __init__(
        self,
        initial_path: Optional[str] = None,
        startup_status: Optional[Callable[[str], None]] = None,
        startup_finished: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self._startup_status = startup_status
        self._startup_finished = startup_finished
        self._show_startup_status("Restoring window...")
        _folder = Path(__file__).parent.parent.name
        self.setWindowTitle(f"W.I.S.E. Panda [{_folder}] — SWTOR Combat Parser")
        apply_app_icon(self)
        restore_window_state(self, "main_window", (1280, 800))
        self._initial_path        = initial_path
        self._show_npcs           = bool(_settings.get("show_npcs", False))
        self._show_companions     = bool(_settings.get("show_companions", False))
        self._show_boss_fights    = bool(_settings.get("encounter_show_boss", True))
        self._show_trash_fights   = bool(_settings.get("encounter_show_trash", True))
        self._current_fight: Optional[Fight] = None
        self._current_hide_comp   = False
        self._current_hide_npcs   = False
        self._loaded_tab_indexes  = set()
        self._all_fights: List[Fight] = []
        self._fights: List[Fight] = []
        self._hunt_encounter_cache: dict[str, dict] = {}
        self._great_hunt_enabled  = True
        self._great_hunt_pending_keys: List[str] = []
        self._great_hunt_pending_fights: dict[str, Fight] = {}
        self._pending_db_ingest: List[Fight] = []
        self._db_ingest_active    = False
        self._auto_encounter_ingest_enabled = False
        self._watcher: Optional[LogWatcherThread] = None
        self._live_events: List[LogEvent] = []
        self._live_completed_fights: List[Fight] = []
        self._live_current_fight: Optional[Fight] = None
        self._live_event_count    = 0
        self._live_parse_errors   = 0
        self._tracker             = LiveFightTracker()
        self._live_window: Optional[LiveBattleWindow] = None
        self._live_threat_window: Optional[LiveThreatWindow] = None
        self._tab_features: list[TabFeature] = []
        self._tab_features_by_widget: dict[QWidget, TabFeature] = {}

        # Dialog references (singleton non-modal dialogs)
        self._encounter_data_dialog: Optional[EncounterDataDialog] = None
        self._character_list_dialog: Optional[CharacterListDialog] = None
        self._character_abilities_dialogs: dict[str, CharacterAbilitiesDialog] = {}
        self._database_character_list_dialog: Optional[ImportedCharacterListDialog] = None
        self._database_character_abilities_dialogs: dict[str, ImportedCharacterAbilitiesDialog] = {}
        self._player_roster_dialog: Optional[PlayerRosterDialog] = None
        self._player_roster_abilities_dialogs: dict[str, SeenPlayerAbilitiesDialog] = {}
        self._player_roster_note_dialogs: dict[str, SeenPlayerNoteDialog] = {}
        self._import_history_dialog: Optional[ImportHistoryDialog] = None
        self._great_hunt_entries_dialog = None

        self._show_startup_status("Opening encounter database...")
        init_db()
        try:
            self._show_startup_status("Syncing import history...")
            sync_import_ledger()
        except Exception:
            pass
        self._show_startup_status("Building interface...")
        self._build_ui()
        self.setStyleSheet(STYLESHEET)
        QTimer.singleShot(0, self._startup_load)

    # ── Startup ───────────────────────────────────────────────────────────────

    def _show_startup_status(self, message: str):
        if self._startup_status is not None:
            self._startup_status(message)
            QApplication.processEvents()

    def _startup_load(self):
        try:
            self._show_startup_status("Preparing dashboard...")
            self._select_feature_tab("dashboard")
            self._refresh_dashboard(force=True)
            target = self._initial_path
            if target and Path(target).exists():
                self._show_startup_status("Reading character data...")
                try:
                    seed_player_characters_from_logs(Path(target).parent)
                except Exception:
                    pass
                self._load_file(target, startup=True)
            else:
                self._refresh_dashboard(force=True)
        finally:
            self._show_startup_status("Ready.")
            if self._startup_finished is not None:
                self._startup_finished()
            self._startup_status = None
            self._startup_finished = None

    # ── Settings helpers ──────────────────────────────────────────────────────

    def _fight_name_settings_key(self, path: str) -> str:
        return f"fight_names_{Path(path).name}"

    def _fight_storage_key(self, fight: Fight) -> str:
        parts = [
            str(fight.index),
            fight.start_time.strftime("%Y%m%d_%H%M%S"),
            fight.boss_name or "unknown",
        ]
        return "_".join(parts)

    def _apply_saved_fight_names(self, fights: List[Fight], path: str):
        saved = _settings.get(self._fight_name_settings_key(path), {})
        if not isinstance(saved, dict):
            return
        for fight in fights:
            key = self._fight_storage_key(fight)
            if key in saved:
                fight.custom_name = saved[key]

    def _save_fight_name(self, fight: Fight):
        path = getattr(self, "_current_path", None)
        if not path:
            return
        key = self._fight_name_settings_key(path)
        saved = _settings.get(key, {})
        if not isinstance(saved, dict):
            saved = {}
        storage_key = self._fight_storage_key(fight)
        if fight.custom_name:
            saved[storage_key] = fight.custom_name
        else:
            saved.pop(storage_key, None)
        _settings.set(key, saved)

    # ── Great Hunt helpers ────────────────────────────────────────────────────

    def _toggle_great_hunt_mode(self, checked: bool):
        self._great_hunt_enabled = True
        self._update_status_indicators()
        self._view_great_hunt_entries()

    def _import_great_hunt_data(self):
        from engine.great_hunt import import_reference_file as import_hunt_reference_file
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Great Hunt Reference File", "", "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        try:
            import_hunt_reference_file(path)
            self.status.showMessage(f"Great Hunt reference data imported from {Path(path).name}.")
        except Exception as e:
            self.status.showMessage(f"Great Hunt import failed: {e}")

    def _view_great_hunt_entries(self):
        from ui.dialogs.great_hunt import GreatHuntEntriesDialog
        if self._great_hunt_entries_dialog is None:
            self._great_hunt_entries_dialog = GreatHuntEntriesDialog(self)
        self._great_hunt_entries_dialog.show()
        self._great_hunt_entries_dialog.raise_()

    def _view_encounter_data(self):
        if self._encounter_data_dialog is None:
            self._encounter_data_dialog = EncounterDataDialog(self)
        self._encounter_data_dialog.show()
        self._encounter_data_dialog.raise_()

    def _view_characters(self):
        if self._character_list_dialog is None:
            self._character_list_dialog = CharacterListDialog(self)
        self._character_list_dialog.show()
        self._character_list_dialog.raise_()

    def _open_settings(self):
        """Open the Settings dialog (modal, short-lived, no caching)."""
        dlg = SettingsDialog(self)
        dlg.exec()

    def _view_character_abilities(self, character_name: str):
        if character_name in self._character_abilities_dialogs:
            dlg = self._character_abilities_dialogs[character_name]
            dlg.show()
            dlg.raise_()
        else:
            dlg = CharacterAbilitiesDialog(self, character_name)
            self._character_abilities_dialogs[character_name] = dlg
            dlg.show()

    def _view_database_characters(self):
        if self._database_character_list_dialog is None:
            self._database_character_list_dialog = ImportedCharacterListDialog(self)
        else:
            self._database_character_list_dialog.refresh()
        self._database_character_list_dialog.show()
        self._database_character_list_dialog.raise_()

    def _view_imported_character_abilities(self, character_name: str):
        if character_name in self._database_character_abilities_dialogs:
            dlg = self._database_character_abilities_dialogs[character_name]
            dlg.show()
            dlg.raise_()
        else:
            dlg = ImportedCharacterAbilitiesDialog(self, character_name)
            self._database_character_abilities_dialogs[character_name] = dlg
            dlg.show()

    def _view_player_roster(self):
        progress = QProgressDialog("Building seen-player roster...", None, 0, 0, self)
        progress.setWindowTitle("Seen Players")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        progress.setCancelButton(None)
        progress.setValue(0)

        def update_progress(current: int, total: int):
            if total <= 0:
                progress.setRange(0, 0)
                progress.setLabelText("Seen-player roster is already up to date.")
            else:
                progress.setRange(0, total)
                progress.setLabelText(f"Updating seen-player roster... {current:,}/{total:,} log(s)")
                progress.setValue(current)
            QApplication.processEvents()

        progress.show()
        try:
            sync_seen_player_cache(progress_callback=update_progress)
        finally:
            progress.close()

        if self._player_roster_dialog is None:
            self._player_roster_dialog = PlayerRosterDialog(self)
        else:
            self._player_roster_dialog.refresh()
        self._player_roster_dialog.show()
        self._player_roster_dialog.raise_()

    def _refresh_player_roster_dialog(self):
        self._view_player_roster()

    def _view_seen_player_abilities(self, player_name: str):
        if player_name in self._player_roster_abilities_dialogs:
            dlg = self._player_roster_abilities_dialogs[player_name]
            dlg.show()
            dlg.raise_()
        else:
            dlg = SeenPlayerAbilitiesDialog(self, player_name)
            self._player_roster_abilities_dialogs[player_name] = dlg
            dlg.show()

    def _view_seen_player_note(self, player_name: str):
        if player_name in self._player_roster_note_dialogs:
            dlg = self._player_roster_note_dialogs[player_name]
            dlg.show()
            dlg.raise_()
        else:
            dlg = SeenPlayerNoteDialog(self, player_name)
            self._player_roster_note_dialogs[player_name] = dlg
            dlg.show()

    def _view_import_history(self):
        if self._import_history_dialog is None:
            self._import_history_dialog = ImportHistoryDialog(self)
        else:
            self._import_history_dialog.refresh()
        self._import_history_dialog.show()
        self._import_history_dialog.raise_()

    def choose_app_icon(self):
        start_path = configured_icon_path()
        start_dir = str(start_path.parent) if start_path is not None else str(Path.home() / "Pictures")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose App Icon",
            start_dir,
            "Icon Files (*.ico *.png *.jpg *.jpeg *.bmp);;All Files (*)",
        )
        if not path:
            return
        if set_custom_app_icon(path, self):
            self.status.showMessage(f"App icon updated: {Path(path).name}")
        else:
            self.status.showMessage("Could not use that file as an app icon.")

    def _encounter_data_rows(self) -> List[dict]:
        rows = []
        for fight in self._all_fights:
            fight_key = self._fight_catalog_key(fight)
            ctx = self._hunt_context_for_fight(fight)
            if has_complete_hunt_annotation(fight_key):
                continue
            mob_rows = self._build_great_hunt_rows(fight)
            if not mob_rows:
                continue
            needs_parts = []
            if not ctx.get("location_name"):
                needs_parts.append("Location")
            unclassified = sum(
                1 for mob in mob_rows
                if not ctx.get("mobs", {}).get(mob["mob_key"], {}).get("classification")
            )
            if unclassified:
                needs_parts.append(f"{unclassified} mob type(s)")
            if not needs_parts:
                continue
            rows.append({
                "fight_key": fight_key,
                "label":     fight.label,
                "location":  ctx.get("location_name", ""),
                "zone":      ctx.get("zone_name", ""),
                "mob_count": len(mob_rows),
                "needs":     ", ".join(needs_parts),
            })
        return rows

    def _review_encounter_data_fight(self, fight_key: str) -> bool:
        fight = self._find_fight_by_key(fight_key)
        if fight is None:
            return False
        self._open_great_hunt_dialog(fight)
        return True

    def _fight_time_seconds(self, timestamp) -> float:
        try:
            return timestamp.timestamp()
        except Exception:
            return 0.0

    def _hunt_context_for_fight(self, fight: Fight, *, refresh: bool = False) -> dict:
        fight_key = self._fight_catalog_key(fight)
        if not refresh and fight_key in self._hunt_encounter_cache:
            return self._hunt_encounter_cache[fight_key]
        annotation = load_hunt_annotation(fight_key)
        ctx = annotation if isinstance(annotation, dict) else {}
        fight_info = ctx.get("fight", {})
        result = {
            "location_name": fight_info.get("location_name", ""),
            "zone_name":     fight_info.get("zone_name", ""),
            "location_type": fight_info.get("location_type", ""),
            "instance_name": fight_info.get("instance_name", ""),
            "quest_name":    fight_info.get("quest_name", ""),
            "character_name":fight_info.get("character_name", ""),
            "mobs":          ctx.get("mobs", {}),
        }
        self._hunt_encounter_cache[fight_key] = result
        return result

    def _suggest_hunt_prefill(self, fight: Fight) -> dict:
        """Suggest location/zone/instance from most recent nearby annotation."""
        for past_fight in reversed(self._all_fights):
            if past_fight is fight:
                continue
            past_key = self._fight_catalog_key(past_fight)
            ctx = self._hunt_context_for_fight(past_fight)
            if ctx.get("location_name"):
                return ctx
        return {}

    def _fight_catalog_key(self, fight: Fight) -> str:
        return self._fight_storage_key(fight)

    def _find_fight_by_key(self, fight_key: str) -> Optional[Fight]:
        for fight in self._all_fights:
            if self._fight_catalog_key(fight) == fight_key:
                return fight
        return None

    def _great_hunt_needs_review(self, fight: Fight) -> bool:
        fight_key = self._fight_catalog_key(fight)
        return not has_complete_hunt_annotation(fight_key)

    def _update_great_hunt_queue_button(self):
        pending = len(self._great_hunt_pending_keys)
        if pending:
            self.status.showMessage(f"Great Hunt: {pending} fight(s) pending review.")

    def _prune_great_hunt_queue(self):
        self._great_hunt_pending_keys = [
            k for k in self._great_hunt_pending_keys
            if not has_complete_hunt_annotation(k)
        ]
        self._great_hunt_pending_fights = {
            k: v for k, v in self._great_hunt_pending_fights.items()
            if k in self._great_hunt_pending_keys
        }

    def _queue_great_hunt_review(self, fight: Fight) -> bool:
        fight_key = self._fight_catalog_key(fight)
        if fight_key in self._great_hunt_pending_keys:
            return False
        self._great_hunt_pending_keys.append(fight_key)
        self._great_hunt_pending_fights[fight_key] = fight
        self._update_great_hunt_queue_button()
        return True

    def _review_pending_great_hunt_queue(self):
        self._prune_great_hunt_queue()
        if not self._great_hunt_pending_keys:
            return
        fight_key = self._great_hunt_pending_keys[0]
        fight = self._great_hunt_pending_fights.get(fight_key)
        if fight is None:
            return
        self._open_great_hunt_dialog(fight)

    def _build_great_hunt_rows(self, fight: Fight) -> List[dict]:
        fight.ensure_loaded()

        def is_player_side(entity) -> bool:
            if entity.player:
                return True
            if entity.companion:
                return True
            return False

        mobs: dict[str, dict] = {}
        for ev in fight.events:
            for ent in (ev.source, ev.target):
                if not getattr(ent, "npc", None):
                    continue
                if is_player_side(ent):
                    continue
                npc_id  = ent.npc_entity_id or ""
                mob_key = f"{npc_id}|{ent.npc_instance or ''}"
                if mob_key not in mobs:
                    mobs[mob_key] = {
                        "mob_key":      mob_key,
                        "mob_name":     ent.display_name or ent.npc or "Unknown",
                        "npc_entity_id":npc_id,
                        "max_hp_seen":  0,
                        "instances":    set(),
                    }
                mobs[mob_key]["max_hp_seen"] = max(mobs[mob_key]["max_hp_seen"], ent.maxhp or 0)
                if ent.npc_instance:
                    mobs[mob_key]["instances"].add(ent.npc_instance)

        output = []
        for row in mobs.values():
            output.append({
                "mob_key":       row["mob_key"],
                "mob_name":      row["mob_name"],
                "npc_entity_id": row["npc_entity_id"],
                "max_hp_seen":   row["max_hp_seen"],
                "instances_seen":max(len(row["instances"]), 1),
            })
        return sorted(output, key=lambda item: (-item["max_hp_seen"], item["mob_name"].lower()))

    def _open_great_hunt_dialog(self, fight: Fight):
        if fight is None:
            return
        fight.ensure_loaded()
        mob_rows = self._build_great_hunt_rows(fight)
        dlg = GreatHuntDialog(self, fight, mob_rows, self._fight_catalog_key(fight))
        if dlg.exec():
            fight_key = self._fight_catalog_key(fight)
            if fight_key in self._great_hunt_pending_keys:
                self._great_hunt_pending_keys = [k for k in self._great_hunt_pending_keys if k != fight_key]
                self._great_hunt_pending_fights.pop(fight_key, None)
                self._update_great_hunt_queue_button()
            self.status.showMessage(
                f"Saved Great Hunt data for {fight.custom_name or fight.boss_name or 'selected fight'}."
            )

    def _maybe_prompt_great_hunt(self, fight: Fight, *, allow_live_prompt: bool = False):
        if fight is None:
            return
        if not fight._log_path:
            return
        if self._live_fight_still_changing(fight):
            return
        fight.ensure_loaded()
        mob_rows = self._build_great_hunt_rows(fight)
        if not mob_rows:
            return
        fight_key        = self._fight_catalog_key(fight)
        detected_location = infer_hunt_location_fields(
            fight._log_path or "",
            line_start=max(fight._line_start, 0),
            line_end=fight._line_end if fight._line_end else None,
        )
        try:
            save_automatic_hunt_fight_data(fight_key, fight, detected_location)
        except Exception:
            pass
        existing      = load_hunt_annotation(fight_key)
        existing_fight = existing.get("fight", {}) if isinstance(existing, dict) else {}
        existing_mobs  = existing.get("mobs", {})  if isinstance(existing, dict) else {}
        known_classifications = known_hunt_mob_classifications(
            [mob["mob_key"] for mob in mob_rows], detected_location, fight_key
        )
        payload = {
            "fight": {
                "location_name": existing_fight.get("location_name", detected_location.get("location_name", "")),
                "zone_name":     existing_fight.get("zone_name",     detected_location.get("zone_name", "")),
                "location_type": existing_fight.get("location_type", ""),
                "instance_name": existing_fight.get("instance_name", detected_location.get("instance_name", "")),
                "quest_name":    existing_fight.get("quest_name",    ""),
                "character_name":existing_fight.get("character_name", fight.player_name or ""),
                "fight_label":   fight.label,
                "log_path":      fight._log_path or "",
            },
            "mobs": {},
        }
        for mob in mob_rows:
            existing_mob = existing_mobs.get(mob["mob_key"], {}) if isinstance(existing_mobs, dict) else {}
            if not isinstance(existing_mob, dict):
                existing_mob = {}
            saved_mob = {
                "mob_name":       mob["mob_name"],
                "npc_entity_id":  mob["npc_entity_id"],
                "classification": existing_mob.get("classification", "") or known_classifications.get(mob["mob_key"], ""),
                "max_hp_seen":    max(int(existing_mob.get("max_hp_seen") or 0), mob["max_hp_seen"]),
                "instances_seen": max(int(existing_mob.get("instances_seen") or 0), mob["instances_seen"]),
            }
            for field in (
                "abilities_used", "kill_count", "total_damage_taken", "total_damage_done",
                "largest_hit_taken_amount", "largest_hit_taken_by", "largest_hit_taken_ability",
                "largest_hit_done_amount", "largest_hit_done_target", "largest_hit_done_ability",
                "first_seen_date", "first_killed_date", "last_kill_date",
            ):
                if field in existing_mob:
                    saved_mob[field] = existing_mob[field]
            payload["mobs"][mob["mob_key"]] = saved_mob
        save_hunt_annotation(fight_key, payload)

    # ── Live mode helpers ─────────────────────────────────────────────────────

    def _live_mode_active(self) -> bool:
        return bool(getattr(self, "btn_live", None) and self.btn_live.isChecked())

    def _live_fight_still_changing(self, fight: Fight) -> bool:
        if not self._live_mode_active() or not self._all_fights:
            return False
        return bool(self._tracker.in_combat and fight is self._all_fights[-1])

    # ── Encounter list ────────────────────────────────────────────────────────

    def _refresh_fight_list_labels(self, keep_row: Optional[int] = None):
        current_row = self.fight_list.currentRow() if keep_row is None else keep_row
        self.fight_list.blockSignals(True)
        self.fight_list.setUpdatesEnabled(False)
        # Track which row we end up on so we can fire the selection handler
        # once at the end. Setting current row while signals are blocked is
        # a deliberate optimization (avoids paint/select churn during the
        # bulk add loop), but it has the side effect that
        # _on_fight_selected never runs — leaving the analysis tabs empty.
        # Especially noticeable on single-fight logs where the user has
        # nothing else to click. We fire it manually after unblocking.
        selected_row: Optional[int] = None
        try:
            self.fight_list.clear()
            for fight in self._fights:
                item = QListWidgetItem(fight.label)
                encounter_type = "Likely boss fight" if fight.is_boss_like else "Trash / small encounter"
                hp_ratio_text  = "Only major NPC seen" if fight.boss_hp_ratio == float("inf") else f"{fight.boss_hp_ratio:.2f}x over next NPC"
                item.setToolTip(
                    f"Started at {fight.start_time.strftime('%H:%M:%S')}\n"
                    f"{encounter_type}\n"
                    f"Top NPC max HP: {fight.boss_max_hp:,}\n"
                    f"Top target damage share: {fight.boss_damage_share:.0%}\n"
                    f"HP lead: {hp_ratio_text}"
                )
                item.setData(Qt.ItemDataRole.UserRole, fight.is_boss_like)
                self.fight_list.addItem(item)
            if self._fights:
                row = current_row if current_row is not None and 0 <= current_row < len(self._fights) else 0
                self.fight_list.setCurrentRow(row)
                selected_row = row
            else:
                self.rename_fight_btn.setEnabled(False)
        finally:
            self.fight_list.setUpdatesEnabled(True)
            self.fight_list.blockSignals(False)

        # Fire the selection handler for the auto-selected row. We do this
        # outside the try/finally so it runs after signals are unblocked,
        # and only if a row was actually selected. This ensures the analysis
        # tabs populate even when the user hasn't manually clicked a fight
        # — which is the common case for single-fight logs, and the case
        # this fix exists for.
        if selected_row is not None:
            self._on_fight_selected(selected_row)

    def _min_duration_filter_seconds(self) -> float:
        return {"All Fights": 0.0, "10s+": 10.0, "20s+": 20.0, "30s+": 30.0, "40s+": 40.0}.get(
            self.duration_filter_combo.currentText().strip(), 0.0
        )

    def _encounter_type_visible(self, fight: Fight) -> bool:
        if fight.is_boss_like and not self._show_boss_fights:
            return False
        if (not fight.is_boss_like) and not self._show_trash_fights:
            return False
        return True

    def _apply_encounter_filter(self, keep_fight: Optional[Fight] = None, fallback_row: int = 0):
        min_seconds = self._min_duration_filter_seconds()
        self._fights = [f for f in self._all_fights
                        if f.duration_seconds >= min_seconds and self._encounter_type_visible(f)]
        keep_row = fallback_row
        if keep_fight is not None:
            try:
                keep_row = self._fights.index(keep_fight)
            except ValueError:
                keep_row = 0
        self._refresh_fight_list_labels(keep_row=keep_row)

    def _on_encounter_filter_changed(self, value: str):
        _settings.set("encounter_duration_filter", value)
        self._apply_encounter_filter(keep_fight=self._current_fight, fallback_row=0)

    def _style_encounter_type_filters(self):
        style_encounter_toggle(self.boss_filter_btn,  ENCOUNTER_BOSS_BG,  ENCOUNTER_BOSS_FG,  self._show_boss_fights)
        style_encounter_toggle(self.trash_filter_btn, ENCOUNTER_TRASH_BG, ENCOUNTER_TRASH_FG, self._show_trash_fights)

    def _toggle_boss_filter(self):
        self._show_boss_fights = not self._show_boss_fights
        _settings.set("encounter_show_boss", self._show_boss_fights)
        self._style_encounter_type_filters()
        self._apply_encounter_filter(keep_fight=self._current_fight, fallback_row=0)

    def _toggle_trash_filter(self):
        self._show_trash_fights = not self._show_trash_fights
        _settings.set("encounter_show_trash", self._show_trash_fights)
        self._style_encounter_type_filters()
        self._apply_encounter_filter(keep_fight=self._current_fight, fallback_row=0)

    # ── DB ingest ─────────────────────────────────────────────────────────────

    def _queue_encounter_ingest(self):
        if not getattr(self, "_auto_encounter_ingest_enabled", False):
            self._pending_db_ingest = []
            self._db_ingest_active = False
            return
        self._pending_db_ingest = list(self._all_fights)
        if not self._db_ingest_active:
            self._db_ingest_active = True
            QTimer.singleShot(0, self._ingest_next_encounter)

    def _ingest_next_encounter(self):
        if not self._pending_db_ingest:
            self._db_ingest_active = False
            return
        fight = self._pending_db_ingest.pop(0)
        try:
            fight.ensure_loaded()
        except Exception:
            pass
        try:
            upsert_fight(fight)
        except Exception:
            pass
        try:
            self._hunt_context_for_fight(fight, refresh=True)
        except Exception:
            pass
        if self._pending_db_ingest:
            QTimer.singleShot(0, self._ingest_next_encounter)
        else:
            self._db_ingest_active = False
            self._refresh_dashboard(force=True)

    # ── Status bar ────────────────────────────────────────────────────────────

    def _record_status_message(self, message: str):
        message = (message or "").strip()
        if not message:
            return
        if not hasattr(self, "_status_history"):
            self._status_history = []
        timestamp = time_mod.strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        if self._status_history and self._status_history[-1].endswith(message):
            self._status_history[-1] = entry
        else:
            self._status_history.append(entry)
            self._status_history = self._status_history[-200:]
        self._refresh_dashboard()

    def _show_status_history(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Status History")
        dlg.resize(720, 420)
        layout = QVBoxLayout(dlg)
        viewer = QPlainTextEdit()
        viewer.setReadOnly(True)
        viewer.setPlainText("\n".join(getattr(self, "_status_history", [])) or "No status messages yet.")
        viewer.moveCursor(QTextCursor.MoveOperation.End)
        layout.addWidget(viewer, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()

    def _build_status_indicators(self):
        self.watch_status_icon = QLabel("● Watch")
        self.watch_status_icon.setToolTip("Watch Mode is active")
        self.watch_status_icon.setStyleSheet(
            "color: #f85149; font-size: 11px; font-weight: 700; padding: 0 6px;"
        )
        self.great_hunt_status_icon = QLabel()
        self.great_hunt_status_icon.setToolTip("The Great Hunt is always watching for new mobs")
        hunt_icon = QPixmap(str(GREAT_HUNT_ICON_PATH))
        if hunt_icon.isNull():
            self.great_hunt_status_icon.setText("Hunt")
            self.great_hunt_status_icon.setStyleSheet(
                "color: #ffa657; font-size: 11px; font-weight: 700; padding: 0 6px;"
            )
        else:
            self.great_hunt_status_icon.setPixmap(
                hunt_icon.scaled(28, 18, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            )
            self.great_hunt_status_icon.setStyleSheet("padding: 0 6px;")
        self.status.addPermanentWidget(self.watch_status_icon)
        self.status.addPermanentWidget(self.great_hunt_status_icon)
        self._update_status_indicators()

    def _update_status_indicators(self):
        if hasattr(self, "watch_status_icon"):
            self.watch_status_icon.setVisible(self._live_mode_active())
        if hasattr(self, "great_hunt_status_icon"):
            self.great_hunt_status_icon.setVisible(True)

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Status bar
        self._status_history = []
        self.status = QStatusBar()
        self.status.messageChanged.connect(self._record_status_message)
        self._build_status_indicators()
        self.status_history_btn = QPushButton("History")
        self.status_history_btn.setToolTip("Show recent app status messages")
        self.status_history_btn.clicked.connect(self._show_status_history)
        self.status.addPermanentWidget(self.status_history_btn)
        self.setStatusBar(self.status)
        self.status.showMessage("No log loaded.")

        # Toolbar
        toolbar = QWidget()
        toolbar.setStyleSheet(f"background: {BG_PANEL}; border-bottom: 1px solid {BORDER};")
        toolbar.setFixedHeight(52)
        tb_lay = QHBoxLayout(toolbar)
        tb_lay.setContentsMargins(12, 8, 12, 8)
        tb_lay.setSpacing(10)

        _folder = Path(__file__).parent.parent.name
        title = QLabel(f"🐼  W.I.S.E. Panda  [{_folder}]")
        title.setObjectName("title")
        tb_lay.addWidget(title)
        tagline = QLabel("Workflow Insight & Skill Engine")
        tagline.setObjectName("subtitle")
        tagline.setStyleSheet(f"color:{TEXT_SEC}; font-size:10px; font-style:italic;")
        tb_lay.addWidget(tagline)
        tb_lay.addSpacing(16)

        # Drag-reorderable action button strip. Each button gets a stable id;
        # the user's preferred order is saved to settings.json under "toolbar_order".
        self._toolbar_strip = DraggableToolbar(spacing=10)

        # ── 1. Live Tracker ────────────────────────────────────────────────
        # Toggle. When on, auto-opens Battle Window and Threat Board, and
        # swaps the toolbar to live-mode buttons only (the overlay toggles
        # appear; everything else is hidden so the user can't try to import
        # mid-fight).
        self.btn_live = QPushButton("🔴  Live Tracker")
        self.btn_live.setCheckable(True)
        self.btn_live.clicked.connect(self.toggle_live)
        self._toolbar_strip.add_button("live_tracker", self.btn_live)

        # ── 1a/1b. Overlay toggles (only visible while Live Tracker is on) ──
        # These replace the old top-level Battle Window / Threat Board buttons
        # and the short-lived sub-menu under Live Tracker. They're inserted
        # right after Live Tracker in the layout, hidden by default, shown
        # by toggle_live() when live mode turns on.
        self.btn_live_window = QPushButton("📡  Battle Window")
        self.btn_live_window.setCheckable(True)
        self.btn_live_window.setEnabled(False)
        self.btn_live_window.setVisible(False)
        self.btn_live_window.clicked.connect(self._toggle_live_window)
        self._toolbar_strip.add_button("battle_window", self.btn_live_window)

        self.btn_live_threat = QPushButton("🎯  Threat Board")
        self.btn_live_threat.setCheckable(True)
        self.btn_live_threat.setEnabled(False)
        self.btn_live_threat.setVisible(False)
        self.btn_live_threat.clicked.connect(self._toggle_live_threat_window)
        self._toolbar_strip.add_button("threat_board", self.btn_live_threat)

        # ── 2. Open Log (single action) ────────────────────────────────────
        self.btn_open = QPushButton("📂  Open Log File")
        self.btn_open.setObjectName("primary")
        self.btn_open.clicked.connect(self.open_file)
        self._toolbar_strip.add_button("open_log", self.btn_open)

        # ── 3. Import ▾ ────────────────────────────────────────────────────
        self.btn_import = QPushButton("🧠  Import ▾")
        import_menu = QMenu(self.btn_import)
        act_import_log = import_menu.addAction("Import Log to DB")
        act_import_log.triggered.connect(self.import_logs_to_database)
        act_import_all = import_menu.addAction("Import All Logs")
        act_import_all.triggered.connect(self.import_all_logs_to_database)
        act_import_history = import_menu.addAction("Import History")
        act_import_history.triggered.connect(self._view_import_history)
        self.btn_import.setMenu(import_menu)
        self._toolbar_strip.add_button("import_menu", self.btn_import)

        # ── 4. Library ▾ ──────────────────────────────────────────────────
        self.btn_library = QPushButton("📚  Library ▾")
        library_menu = QMenu(self.btn_library)
        act_characters = library_menu.addAction("Characters")
        act_characters.triggered.connect(self._view_characters)
        act_great_hunt = library_menu.addAction("The Great Hunt")
        act_great_hunt.triggered.connect(self._view_great_hunt_entries)
        act_encounter_data = library_menu.addAction("Encounter Data")
        act_encounter_data.triggered.connect(self._view_encounter_data)
        library_menu.addSeparator()
        act_app_icon = library_menu.addAction("App Icon…")
        act_app_icon.triggered.connect(self.choose_app_icon)
        self.btn_library.setMenu(library_menu)
        self._toolbar_strip.add_button("library_menu", self.btn_library)

        # ── 5. ⚙ Settings ──────────────────────────────────────────────────
        # User preferences: log folder paths, history folder, retention,
        # server. Opens a modal tabbed dialog.
        self.btn_settings = QPushButton("⚙  Settings")
        self.btn_settings.clicked.connect(self._open_settings)
        self._toolbar_strip.add_button("settings", self.btn_settings)

        # ── 6. Dev ▾ ───────────────────────────────────────────────────────
        # Developer / debug actions. Currently just Save Log As — only
        # relevant when actively looking at a log file.
        self.btn_dev = QPushButton("🛠  Dev ▾")
        dev_menu = QMenu(self.btn_dev)
        self.act_save_as = dev_menu.addAction("Save Log As…")
        self.act_save_as.setEnabled(False)
        self.act_save_as.triggered.connect(self.save_log_as)
        # Kept around as btn_save_as for backward compatibility with code
        # that toggles the old QPushButton's enabled state.
        self.btn_save_as = self.act_save_as
        self.btn_dev.setMenu(dev_menu)
        self._toolbar_strip.add_button("dev_menu", self.btn_dev)

        # Apply the user's saved order, then wire up persistence for future drags.
        saved_toolbar_order = _settings.get("toolbar_order", [])
        if isinstance(saved_toolbar_order, list):
            self._toolbar_strip.apply_order([str(b) for b in saved_toolbar_order])
        self._toolbar_strip.order_changed.connect(self._save_toolbar_order)
        tb_lay.addWidget(self._toolbar_strip)
        tb_lay.addStretch()

        tb_lay.addSpacing(8)
        self.file_label = QLabel("No file loaded")
        self.file_label.setObjectName("subtitle")
        tb_lay.addWidget(self.file_label)

        # Central widget + splitter
        central = QWidget()
        central.setObjectName("app_shell")
        self._app_shell = central
        main_lay = QVBoxLayout(central)
        self._app_shell_layout = main_lay
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)
        main_lay.addWidget(toolbar, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        main_lay.addWidget(splitter, 1)

        # Left: fight list
        left = QWidget()
        left.setMinimumWidth(180)
        left.setMaximumWidth(360)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(8, 8, 4, 8)
        left_lay.setSpacing(4)

        fight_header = QLabel("ENCOUNTERS")
        fight_header.setObjectName("stat_label")
        left_lay.addWidget(fight_header)

        fight_actions = QHBoxLayout()
        self.duration_filter_combo = QComboBox()
        self.duration_filter_combo.addItems(["All Fights", "10s+", "20s+", "30s+", "40s+"])
        saved_filter = str(_settings.get("encounter_duration_filter", "All Fights"))
        idx = self.duration_filter_combo.findText(saved_filter)
        if idx >= 0:
            self.duration_filter_combo.setCurrentIndex(idx)
        self.duration_filter_combo.setMinimumWidth(84)
        self.duration_filter_combo.setMaximumWidth(96)
        self.duration_filter_combo.currentTextChanged.connect(self._on_encounter_filter_changed)
        fight_actions.addWidget(self.duration_filter_combo)

        self.boss_filter_btn = QPushButton("Boss")
        self.boss_filter_btn.setCheckable(True)
        self.boss_filter_btn.setChecked(self._show_boss_fights)
        self.boss_filter_btn.clicked.connect(self._toggle_boss_filter)
        fight_actions.addWidget(self.boss_filter_btn)

        self.trash_filter_btn = QPushButton("Trash")
        self.trash_filter_btn.setCheckable(True)
        self.trash_filter_btn.setChecked(self._show_trash_fights)
        self.trash_filter_btn.clicked.connect(self._toggle_trash_filter)
        fight_actions.addWidget(self.trash_filter_btn)
        self._style_encounter_type_filters()
        fight_actions.addStretch()

        self.rename_fight_btn = QPushButton("Rename Encounter")
        self.rename_fight_btn.clicked.connect(self._rename_current_fight)
        self.rename_fight_btn.setEnabled(False)
        left_lay.addLayout(fight_actions)

        self.fight_list = EncounterListWidget()
        self.fight_list.setItemDelegate(EncounterListDelegate(self.fight_list))
        self.fight_list.setUniformItemSizes(True)
        self.fight_list.setLayoutMode(QListView.LayoutMode.Batched)
        self.fight_list.setBatchSize(256)
        self.fight_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.fight_list.currentRowChanged.connect(self._on_fight_selected)
        # Phase G: right-click on a fight to save it to the DB. Lets the user
        # capture a single fight on demand without importing the whole log.
        self.fight_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.fight_list.customContextMenuRequested.connect(self._on_fight_list_context_menu)
        left_lay.addWidget(self.fight_list)
        splitter.addWidget(left)

        # Right: tab panel
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(4, 8, 8, 8)

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self._tab_features = build_tab_features(self)
        self._tab_features_by_widget = {
            feature.widget: feature for feature in self._tab_features
        }
        self._restore_tab_order()
        self.tabs.tabBar().tabMoved.connect(self._save_tab_order)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        right_lay.addWidget(self.tabs)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 1040])

        self.setCentralWidget(central)
        self._set_watch_mode_frame(False)

    def _set_watch_mode_frame(self, active: bool):
        if not hasattr(self, "_app_shell"):
            return
        self._update_status_indicators()
        if active:
            self._app_shell.setStyleSheet("QWidget#app_shell { border: 3px solid #f85149; }")
            self._app_shell_layout.setContentsMargins(3, 3, 3, 3)
        else:
            self._app_shell.setStyleSheet("")
            self._app_shell_layout.setContentsMargins(0, 0, 0, 0)

    def _restore_tab_order(self):
        saved_order = _settings.get("tab_order", [])
        known_ids   = [feature.feature_id for feature in self._tab_features]
        desired_ids = [tab_id for tab_id in saved_order if tab_id in known_ids]
        remaining   = [tab_id for tab_id in known_ids if tab_id not in desired_ids]
        tab_map     = {
            feature.feature_id: feature for feature in self._tab_features
        }
        for tab_id in desired_ids + remaining:
            feature = tab_map[tab_id]
            self.tabs.addTab(feature.widget, feature.label)

    def _select_feature_tab(self, feature_id: str):
        for idx in range(self.tabs.count()):
            widget = self.tabs.widget(idx)
            feature = self._tab_features_by_widget.get(widget)
            if feature is not None and feature.feature_id == feature_id:
                self.tabs.setCurrentIndex(idx)
                return

    def _build_dashboard_snapshot(self) -> DashboardSnapshot:
        db_snapshot = get_database_dashboard_snapshot()
        current_log_name = Path(getattr(self, "_current_path", "")).name if getattr(self, "_current_path", None) else ""
        latest_status = getattr(self, "_status_history", [])[-1] if getattr(self, "_status_history", []) else ""
        return DashboardSnapshot(
            current_log_name=current_log_name,
            loaded_fight_count=len(getattr(self, "_all_fights", [])),
            encounter_count=db_snapshot.encounter_count,
            imported_log_count=db_snapshot.imported_log_count,
            imported_event_count=db_snapshot.imported_event_count,
            imported_character_count=db_snapshot.imported_character_count,
            seen_player_count=db_snapshot.seen_player_count,
            latest_status=latest_status,
        )

    def _refresh_dashboard(self, force: bool = False):
        dashboard = getattr(self, "dashboard_tab", None)
        if dashboard is None or not hasattr(dashboard, "refresh_snapshot"):
            return
        if not force:
            current_widget = self.tabs.currentWidget() if hasattr(self, "tabs") else None
            if current_widget is not dashboard:
                return
        try:
            dashboard.refresh_snapshot(self._build_dashboard_snapshot())
        except Exception:
            pass

    def _save_tab_order(self, *_args):
        current_order: list[str] = []
        for idx in range(self.tabs.count()):
            widget = self.tabs.widget(idx)
            feature = self._tab_features_by_widget.get(widget)
            if feature is not None:
                current_order.append(feature.feature_id)
        _settings.set("tab_order", current_order)

    def _save_toolbar_order(self, ordered_ids: list[str]):
        """Persist the toolbar button order after a drag-reorder."""
        _settings.set("toolbar_order", list(ordered_ids))

    # Buttons hidden while Live Tracker is active. The principle: while live,
    # don't tempt the user to fire off an import or wander into management
    # screens. The two overlay toggles take their place in the strip.
    _LIVE_HIDDEN_BUTTONS = ("open_log", "import_menu", "library_menu", "settings", "dev_menu")
    _LIVE_ONLY_BUTTONS   = ("battle_window", "threat_board")

    def _set_toolbar_live_mode(self, live: bool):
        """Swap the toolbar between idle and live presentation.

        Idle: Live Tracker + Open Log + Import + Library + Dev
        Live: Live Tracker + Battle Window + Threat Board
        """
        strip = getattr(self, "_toolbar_strip", None)
        if strip is None:
            return
        for bid in self._LIVE_HIDDEN_BUTTONS:
            strip.set_button_visible(bid, not live)
        for bid in self._LIVE_ONLY_BUTTONS:
            strip.set_button_visible(bid, live)

    # ── File loading ──────────────────────────────────────────────────────────

    def open_file(self):
        start_dir = str(Path(_settings.get("last_log_path", "") or "").parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open SWTOR Combat Log", start_dir, "Text Files (*.txt);;All Files (*)"
        )
        if path:
            self._load_file(path)

    def open_fight_by_encounter_key(self, encounter_key: str) -> None:
        """
        Phase F: open a specific fight given its encounter_key.

        The Find-a-Fight tab emits this when the user clicks "Open this
        fight" on a search result. We:
          1. Decode the log path + line range from the encounter_key
          2. Load the log file (skip the load if it's already current)
          3. Find the fight whose line range matches and select it in
             the fight list — the existing fight-selection signal then
             populates all the analysis tabs.

        Falls back gracefully if the log file no longer exists at the
        path stored in the DB (user moved it, etc.) — we report the
        problem on the status bar instead of crashing.
        """
        from storage.cohort import parse_encounter_key

        log_path, line_start, line_end, _ = parse_encounter_key(encounter_key)
        if not log_path:
            self.status.showMessage("Could not decode encounter key.")
            return
        if not Path(log_path).exists():
            self.status.showMessage(
                f"Log file not found: {Path(log_path).name}. "
                "It may have been moved or deleted since import."
            )
            return

        # Load the log if it isn't the current one. _load_file is the
        # canonical entry point — it does scanning, name resolution,
        # encounter list building, and dashboard refresh.
        current = getattr(self, "_current_path", None)
        if current != log_path:
            self._load_file(log_path)

        # Find the fight in the (possibly filtered) list whose line range
        # matches. If the user has filters on (e.g. "boss only"), the
        # target fight may be filtered out — for now we just report that
        # we couldn't find it and leave the list as-is. A future polish
        # could temporarily clear filters; tracking issue for later.
        target_row: Optional[int] = None
        for idx, fight in enumerate(self._fights):
            f_start = getattr(fight, "_line_start", None)
            f_end = getattr(fight, "_line_end", None)
            if f_start == line_start and f_end == line_end:
                target_row = idx
                break

        if target_row is None:
            self.status.showMessage(
                "Fight located but it's filtered out of the current encounter "
                "list. Adjust the encounter filters and try again."
            )
            return

        # Select the row. The existing currentRowChanged → _on_fight_selected
        # path does the rest — populates analysis tabs, triggers any
        # auto-load behavior, etc.
        self.fight_list.setCurrentRow(target_row)
        # Switch focus to the Overview tab so the user lands on real
        # analysis instead of staring at the search results behind it.
        overview_widget = getattr(self, "overview_tab", None)
        if overview_widget is not None:
            tab_idx = self.tabs.indexOf(overview_widget)
            if tab_idx >= 0:
                self.tabs.setCurrentIndex(tab_idx)

    def _load_file(self, path: str, startup: bool = False):
        if startup:
            self._show_startup_status(f"Scanning {Path(path).name}...")
        try:
            seed_player_character_from_log(path)
        except Exception:
            pass
        self.file_label.setText(f"Scanning {Path(path).name}…")
        QApplication.processEvents()
        self._hunt_encounter_cache = {}
        self._all_fights = scan_fights(path)
        if startup:
            self._show_startup_status("Resolving encounter names...")
        resolve_fight_names(path, self._all_fights)
        self._apply_saved_fight_names(self._all_fights, path)
        if startup:
            self._show_startup_status("Preparing encounter list...")
        self._apply_encounter_filter(fallback_row=0)
        self._queue_encounter_ingest()
        self.file_label.setText(Path(path).name)
        self.status.showMessage(f"Scanned {len(self._all_fights)} fights from {Path(path).name}")
        self._current_path = path
        _settings.set("last_log_path", path)
        self.btn_save_as.setEnabled(True)
        self._refresh_dashboard(force=True)

    def save_log_as(self):
        src = getattr(self, "_current_path", None)
        if not src or not Path(src).exists():
            self.status.showMessage("No log file loaded to save.")
            return
        original      = Path(src)
        suggested_name = original.stem
        suffix        = original.suffix or ".txt"
        last_save_dir = _settings.get("last_save_dir", "")
        if not last_save_dir or not Path(last_save_dir).is_dir():
            last_save_dir = str(Path.home() / "Documents")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Save Combat Log As",
            str(Path(last_save_dir) / (suggested_name + suffix)),
            f"Text Files (*{suffix});;All Files (*)",
        )
        if not dest:
            return
        try:
            shutil.copy2(src, dest)
            _settings.set("last_save_dir", str(Path(dest).parent))
            self.status.showMessage(f"Log saved to: {dest}")
        except Exception as e:
            self.status.showMessage(f"Save failed: {e}")

    def import_logs_to_database(self):
        start_dir = str(Path(_settings.get("last_log_path", "") or "").parent)
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import Combat Logs Into Database",
            start_dir,
            "Text Files (*.txt);;All Files (*)",
        )
        if not paths:
            return
        self._run_log_import(list(paths))

    def import_all_logs_to_database(self):
        start_dir = str(Path(_settings.get("last_log_path", "") or "").parent)
        folder = QFileDialog.getExistingDirectory(
            self,
            "Import All Combat Logs In Folder",
            start_dir,
        )
        if not folder:
            return
        paths = [
            str(path)
            for path in sorted(Path(folder).glob("combat_*.txt"))
            if path.is_file()
        ]
        if not paths:
            self.status.showMessage("No combat_*.txt files were found in that folder.")
            return
        self._run_log_import(paths)

    def _run_log_import(self, paths: list[str]):
        if not paths:
            return

        # Phase G: import_combat_log is now idempotent. Re-running it on a
        # log that's already in the DB refreshes fight data instead of
        # raising. We no longer need a pre-check or try/except for
        # DuplicateCombatLogImportError — that error is no longer raised.
        #
        # Progress dialog: bulk imports of historical logs can take minutes.
        # The user needs to know it's working and have the option to bail
        # out. We use cancel-after-current-log semantics — if the user
        # cancels mid-batch, we finish the current log (it's atomic) then
        # stop before starting the next one. Already-imported logs stay
        # in the DB (Phase G makes each one idempotent and standalone).
        progress = QProgressDialog(
            f"Preparing to import {len(paths)} log(s)...",
            "Cancel",
            0,
            len(paths),
            self,
        )
        progress.setWindowTitle("Importing Combat Logs")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        progress.setValue(0)
        progress.show()

        imported = 0
        total_lines = 0
        total_errors = 0
        # Phase G aggregate fight counts across all logs in this batch.
        total_fights_new = 0
        total_fights_refreshed = 0
        total_fights_failed = 0
        imported_ids: list[int] = []
        failures: list[str] = []
        cancelled = False

        try:
            for idx, path in enumerate(paths):
                # Per-log progress label. Dialog gets two lines of context:
                # which log out of how many, and what we're doing right
                # now. processEvents() between updates keeps the dialog
                # responsive enough to register cancel clicks.
                file_name = Path(path).name
                progress.setLabelText(
                    f"Importing {idx + 1} of {len(paths)} — parsing {file_name}..."
                )
                QApplication.processEvents()

                # Cancel check BEFORE starting the next log. If the user
                # clicked Cancel during the previous log's processing, we
                # honor it here and stop the loop. Already-imported logs
                # stay in the DB.
                if progress.wasCanceled():
                    cancelled = True
                    break

                try:
                    # Sub-status while the work is happening. The import
                    # itself runs synchronously and won't yield until done,
                    # so this label is what the user sees during the
                    # heavy lifting.
                    progress.setLabelText(
                        f"Importing {idx + 1} of {len(paths)} — aggregating fights in {file_name}..."
                    )
                    QApplication.processEvents()

                    summary = import_combat_log(path)
                    imported += 1
                    imported_ids.append(summary.import_id)
                    total_lines += summary.line_count
                    total_errors += summary.parse_error_count
                    total_fights_new += summary.fights_new
                    total_fights_refreshed += summary.fights_refreshed
                    total_fights_failed += summary.fights_failed
                except Exception as exc:
                    failures.append(f"{file_name}: {exc}")

                progress.setValue(idx + 1)
                QApplication.processEvents()
        finally:
            progress.close()

        if self._import_history_dialog is not None:
            self._import_history_dialog.refresh()

        hunt_added = 0
        hunt_updated = 0
        if imported_ids:
            try:
                hunt_result = import_missing_mobs_from_encounter_database(import_ids=imported_ids)
                hunt_added = int(hunt_result.get("added", 0) or 0)
                hunt_updated = int(hunt_result.get("updated", 0) or 0)
            except Exception:
                hunt_added = 0
                hunt_updated = 0
        hunt_suffix = ""
        if hunt_added or hunt_updated:
            hunt_suffix = f" Great Hunt added {hunt_added:,}, updated {hunt_updated:,}."

        # Phase G: build a status message that reports new/refreshed fights
        # instead of file-level duplicates. This is more honest about what
        # the import actually did — re-importing a log that was already
        # processed produces "0 new, N refreshed" rather than being silently
        # skipped.
        fight_suffix = ""
        if total_fights_new or total_fights_refreshed or total_fights_failed:
            parts = []
            if total_fights_new:
                parts.append(f"{total_fights_new} new")
            if total_fights_refreshed:
                parts.append(f"{total_fights_refreshed} refreshed")
            if total_fights_failed:
                parts.append(f"{total_fights_failed} failed")
            if parts:
                fight_suffix = " Fights: " + ", ".join(parts) + "."

        # If the user cancelled, lead with that — they want to know their
        # cancel was honored. Otherwise, normal success/failure messages.
        if cancelled:
            cancel_suffix = f" (Cancelled — {imported} of {len(paths)} log(s) completed.)"
            self.status.showMessage(
                f"Imported {imported} log(s) before cancel "
                f"({total_lines:,} lines, {total_errors:,} parse errors kept)."
                f"{fight_suffix}{hunt_suffix}{cancel_suffix}"
            )
            self._refresh_dashboard(force=True)
            return

        if imported and not failures:
            self.status.showMessage(
                f"Imported {imported} log(s) into the database "
                f"({total_lines:,} lines, {total_errors:,} parse errors kept)."
                f"{fight_suffix}{hunt_suffix}"
            )
            self._refresh_dashboard(force=True)
            return
        if imported and failures:
            self.status.showMessage(
                f"Imported {imported} log(s), {len(failures)} failure(s)."
                f"{fight_suffix}{hunt_suffix}"
            )
            self._refresh_dashboard(force=True)
            return
        self.status.showMessage(f"Combat log import failed. {failures[-1] if failures else ''}".strip())
        self._refresh_dashboard(force=True)

    # ── Filter properties ─────────────────────────────────────────────────────

    @property
    def show_companions(self) -> bool:
        return self._show_companions

    @property
    def show_npcs(self) -> bool:
        return self._show_npcs

    def _on_overview_filters_changed(self, show_npcs: bool, show_companions: bool):
        self._show_npcs       = show_npcs
        self._show_companions = show_companions
        _settings.set("show_npcs", show_npcs)
        _settings.set("show_companions", show_companions)
        row = self.fight_list.currentRow()
        if row >= 0:
            self._on_fight_selected(row)

    # ── Fight selection ───────────────────────────────────────────────────────

    def _on_fight_selected(self, row: int):
        if row < 0 or row >= len(self._fights):
            self.rename_fight_btn.setEnabled(False)
            return
        fight = self._fights[row]
        self.rename_fight_btn.setEnabled(True)
        old_label = fight.label
        fight.ensure_loaded()
        # Ability icon resolving is disabled while we verify app performance.
        # self._resolve_encounter_ability_icons(fight)
        if fight.label != old_label:
            item = self.fight_list.item(row)
            if item is not None:
                item.setText(fight.label)
        self._current_fight      = fight
        self._current_hide_comp  = not self.show_companions
        self._current_hide_npcs  = not self.show_npcs
        self._loaded_tab_indexes = set()
        self._load_visible_tab(force=True)
        # Great Hunt data is updated during explicit database imports, not while browsing encounters.
        # self._maybe_prompt_great_hunt(fight)

    # def _resolve_encounter_ability_icons(self, fight: Fight):
    #     try:
    #         pairs = encounter_ability_pairs(fight)
    #         if not pairs:
    #             return
    #         result = get_ability_icon_library().rename_noid_icons_for_abilities(pairs)
    #         if result.renamed:
    #             self._clear_ability_icon_caches()
    #     except Exception:
    #         pass
    #
    # def _clear_ability_icon_caches(self):
    #     for module_name in ("training_tabs", "ui.tabs.ability"):
    #         module = sys.modules.get(module_name)
    #         cache = getattr(module, "_ABILITY_QICONS", None) if module is not None else None
    #         if isinstance(cache, dict):
    #             cache.clear()

    def _load_visible_tab(self, force: bool = False):
        fight = getattr(self, "_current_fight", None)
        if fight is None:
            return
        tab_index = self.tabs.currentIndex()
        if not force and tab_index in getattr(self, "_loaded_tab_indexes", set()):
            return
        tab = self.tabs.widget(tab_index)
        if hasattr(tab, "load_fight"):
            tab.load_fight(
                fight,
                hide_companions=getattr(self, "_current_hide_comp", False),
                hide_npcs=getattr(self, "_current_hide_npcs", False),
            )
        loaded = getattr(self, "_loaded_tab_indexes", set())
        loaded.add(tab_index)
        self._loaded_tab_indexes = loaded

    def _on_tab_changed(self, _index: int):
        current_widget = self.tabs.currentWidget()
        feature = self._tab_features_by_widget.get(current_widget)
        if feature is not None and feature.feature_id == "dashboard":
            self._refresh_dashboard(force=True)
        self._load_visible_tab()

    def _on_fight_list_context_menu(self, position):
        """
        Phase G: Build a right-click context menu for the encounter list.

        The menu offers "Save to DB" for the right-clicked fight. Other
        actions can be added here later (e.g. "Open from DB", "Compare
        with...").

        position is the click position relative to the fight_list widget.
        We use itemAt to figure out which row was clicked, then look up
        the underlying Fight in self._fights.

        Note: this requires EncounterListWidget (the QListWidget subclass)
        to suppress right-click selection. Without that, the right-click
        triggers a full tab reload that interferes with the menu drawing.
        """
        item = self.fight_list.itemAt(position)
        if item is None:
            return  # right-clicked on empty space; no menu
        row = self.fight_list.row(item)
        if row < 0 or row >= len(self._fights):
            return
        fight = self._fights[row]
        label = fight.custom_name or fight.boss_name or f"Fight #{fight.index}"

        menu = QMenu(self.fight_list)
        save_action = menu.addAction(f"Save \"{label}\" to DB")
        # Use exec() at the global click position so the menu pops up under
        # the cursor, not inside the widget's coordinate system.
        chosen = menu.exec(self.fight_list.mapToGlobal(position))
        if chosen is save_action:
            self._save_fight_to_db(fight, row)

    def _save_fight_to_db(self, fight, row: int):
        """
        Phase G: Run upsert_fight against a single fight, with feedback.

        ensure_loaded() is called before upsert because the fight list shows
        scanned-but-not-yet-loaded fights (we lazy-parse to keep the list
        snappy). Loading first makes sure entity_stats is populated so the
        per-player rows actually have data.

        Errors are surfaced in a message box rather than a status bar message
        because saving to DB is a deliberate user action and they need to
        know whether it succeeded.
        """
        # Disable the row's interaction briefly so a double-click doesn't
        # fire the same save twice during the load. Best-effort.
        self.fight_list.setEnabled(False)
        try:
            try:
                fight.ensure_loaded()
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Save failed",
                    f"Could not load fight events:\n\n{exc}",
                )
                return

            try:
                upsert_fight(fight)
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Save failed",
                    f"Could not save fight to database:\n\n{exc}",
                )
                return

            # Status message instead of a popup — saving is a quick op and
            # blocking the UI for an OK click would feel heavy.
            label = (fight.custom_name or fight.boss_name or f"Fight #{fight.index}")
            self.status.showMessage(f"Saved \"{label}\" to database.")
            self._refresh_dashboard(force=True)
        finally:
            self.fight_list.setEnabled(True)

    def _rename_current_fight(self):
        row = self.fight_list.currentRow()
        if row < 0 or row >= len(self._fights):
            return
        fight = self._fights[row]
        current_name = fight.custom_name or fight.boss_name or ""
        new_name, ok = QInputDialog.getText(
            self, "Rename Encounter", "Custom encounter name:", text=current_name
        )
        if not ok:
            return
        cleaned   = new_name.strip()
        auto_name = (fight.boss_name or "").strip()
        fight.custom_name = cleaned if cleaned and cleaned != auto_name else None
        self._save_fight_name(fight)
        self._refresh_fight_list_labels(keep_row=row)
        try:
            upsert_fight(fight)
        except Exception:
            pass

    # ── Live mode ─────────────────────────────────────────────────────────────

    def _sync_live_fight_indexes(self):
        combined = list(self._live_completed_fights)
        if self._live_current_fight is not None:
            combined.append(self._live_current_fight)
        for idx, fight in enumerate(combined, start=1):
            fight.index = idx
        self._all_fights = combined

    def _attach_live_ranges(self, fights: List[Fight], path: Optional[str], start_index: int):
        if not fights or not path:
            return
        scanned = scan_fights(path)
        for fight, scanned_fight in zip(fights, scanned[start_index:start_index + len(fights)]):
            fight._log_path   = scanned_fight._log_path or path
            fight._line_start = scanned_fight._line_start
            fight._line_end   = scanned_fight._line_end

    def _split_live_state(self, events: List[LogEvent], path: Optional[str]):
        fights      = build_fights(events)
        in_combat   = False
        open_start  = None
        for idx, ev in enumerate(events):
            if ev.is_enter_combat:
                in_combat  = True
                open_start = idx
            elif ev.is_exit_combat:
                in_combat  = False
                open_start = None
        current_fight  = fights.pop() if in_combat and fights else None
        current_events = list(events[open_start:]) if in_combat and open_start is not None else []
        if current_fight is not None and path:
            current_fight._log_path = path
        return fights, current_fight, current_events

    def _prime_live_state(self, events: List[LogEvent], path: Optional[str]):
        completed, current, current_events = self._split_live_state(events, path)
        self._attach_live_ranges(completed, path, start_index=0)
        self._live_completed_fights = completed
        self._live_current_fight    = current
        self._live_events           = current_events
        self._live_event_count      = len(events)
        self._sync_live_fight_indexes()

    def _ingest_live_events(self, events: List[LogEvent], path: Optional[str]):
        if not events:
            return
        self._live_events.extend(events)
        self._live_event_count += len(events)
        completed, current, current_events = self._split_live_state(self._live_events, path)
        if completed:
            self._attach_live_ranges(completed, path, start_index=len(self._live_completed_fights))
            self._live_completed_fights.extend(completed)
        self._live_current_fight = current
        self._live_events        = current_events
        self._sync_live_fight_indexes()

    def _stop_live_mode_ui(self, message: str):
        if self._watcher:
            self._watcher.stop()
            self._watcher.wait(1000)
            self._watcher = None
        if self._live_window:
            self._live_window.hide()
        if self._live_threat_window:
            self._live_threat_window.hide()
        self.btn_live.setChecked(False)
        self.btn_live.setText("🔴  Live Tracker")
        self.btn_live_window.setChecked(False)
        self.btn_live_window.setEnabled(False)
        self.btn_live_threat.setChecked(False)
        self.btn_live_threat.setEnabled(False)
        self._set_watch_mode_frame(False)
        self._set_toolbar_live_mode(False)
        # Reset the Live Combat Stream tab cards to their empty state.
        stream_tab = getattr(self, "live_combat_stream_tab", None)
        if stream_tab is not None:
            stream_tab.clear()
        self.status.showMessage(message)

    def _maybe_prompt_completed_live_great_hunt(self, was_in_combat: bool):
        return

    def _on_watcher_error(self, message: str, failures: int):
        self.status.showMessage(f"⚠ Watch issue ({failures} failures) — {message}")

    def _on_watcher_stopped(self, message: str):
        self._stop_live_mode_ui(message)

    def toggle_live(self, checked: bool):
        if checked:
            path = getattr(self, "_current_path", None)
            folder_mode = False

            if not path:
                # No specific file loaded. Prefer the live folder configured
                # in Settings → Log Files. The watcher will attach to the
                # newest file in that folder, or wait for SWTOR to create
                # one if the user enabled Live Tracker before launching the
                # game.
                live_dir = _settings.user_get("live_log_dir", "") or ""
                if live_dir and Path(live_dir).is_dir():
                    folder_mode = True
                else:
                    # No configured folder either — fall back to a file
                    # picker, and nudge the user toward Settings so this
                    # doesn't happen next time.
                    QMessageBox.information(
                        self, "Live folder not set",
                        "Tip: set your live combat logs folder under ⚙ Settings → "
                        "Log Files and Live Tracker will auto-attach to the newest "
                        "log next time.\n\n"
                        "For now, pick the log file you want to watch."
                    )
                    path, _ = QFileDialog.getOpenFileName(
                        self, "Select Active Log File", "",
                        "Text Files (*.txt);;All Files (*)"
                    )
                    if not path:
                        self.btn_live.setChecked(False)
                        return
                    self._load_file(path)

            self._tracker.reset()
            self._live_events           = []
            self._live_completed_fights = []
            self._live_current_fight    = None
            self._live_event_count      = 0
            self._live_parse_errors     = 0

            # File mode only: seed historical fights from the existing file.
            # In folder mode there's nothing to seed from yet — we just wait
            # for the watcher to attach and stream events.
            if not folder_mode:
                try:
                    seed_events, seed_errors = parse_file(path)
                    self._live_parse_errors  = seed_errors
                    self._prime_live_state(seed_events, path)
                    if self._all_fights and self._all_fights[-1].player_name:
                        self._tracker.player_name = self._all_fights[-1].player_name
                    self._apply_encounter_filter(
                        keep_fight=self._all_fights[-1] if self._all_fights else None,
                        fallback_row=len(self._all_fights) - 1,
                    )
                    self._queue_encounter_ingest()
                except Exception:
                    self._live_events           = []
                    self._live_completed_fights = []
                    self._live_current_fight    = None
                    self._live_event_count      = 0

            # Construct the watcher in the right mode.
            if folder_mode:
                self._watcher = LogWatcherThread.from_folder(live_dir)
            else:
                self._watcher = LogWatcherThread(path)
            self._watcher.new_events.connect(self._on_live_events)
            self._watcher.log_switched.connect(self._on_live_log_switched)
            self._watcher.watch_error.connect(self._on_watcher_error)
            self._watcher.watch_stopped.connect(self._on_watcher_stopped)
            self._watcher.start()

            self.btn_live.setText("⏹  Stop Live Tracker")
            self.btn_live_window.setEnabled(True)
            self.btn_live_threat.setEnabled(True)
            self._set_watch_mode_frame(True)
            self._set_toolbar_live_mode(True)
            if folder_mode:
                self.status.showMessage(
                    f"🔴 Live Tracker active — watching folder {Path(live_dir).name} "
                    "for the next combat log…"
                )
            else:
                self.status.showMessage("🔴 Live Tracker active — watching for new events…")

            # Auto-open the Battle Window and Threat Board overlays. The user
            # can still dismiss either one via its ✕ button, then bring it
            # back via the Battle Window / Threat Board toolbar buttons that
            # are now visible.
            self.btn_live_window.setChecked(True)
            self._toggle_live_window(True)
            self.btn_live_threat.setChecked(True)
            self._toggle_live_threat_window(True)
        else:
            self._stop_live_mode_ui("Live Tracker stopped.")

    def _on_live_events(self, events: List[LogEvent]):
        was_in_combat    = self._tracker.in_combat
        completed_combat = was_in_combat or any(ev.is_exit_combat for ev in events)
        self._tracker.push(events)
        self._ingest_live_events(events, getattr(self, "_current_path", None))
        if not self._tracker.player_name and self._all_fights and self._all_fights[-1].player_name:
            self._tracker.player_name = self._all_fights[-1].player_name
        new_count = len(self._all_fights)
        self._apply_encounter_filter(
            keep_fight=self._all_fights[-1] if self._all_fights else None,
            fallback_row=new_count - 1,
        )
        self._queue_encounter_ingest()
        self.status.showMessage(f"🔴 Watch · {self._live_event_count:,} events · {new_count} fights")
        self._maybe_prompt_completed_live_great_hunt(completed_combat)
        # Push live data into the Live Combat Stream tab's stat cards.
        # Safe if the tab attr isn't set yet — getattr returns None and
        # update_from_tracker handles tracker=None.
        stream_tab = getattr(self, "live_combat_stream_tab", None)
        if stream_tab is not None:
            stream_tab.update_from_tracker(self._tracker)

    def _on_live_log_switched(self, path: str, seed_events: List[LogEvent]):
        self._current_path = path
        _settings.set("last_log_path", path)
        self.file_label.setText(Path(path).name)
        self._tracker.reset()
        self._hunt_encounter_cache = {}
        try:
            seed_player_character_from_log(path)
        except Exception:
            pass
        self._prime_live_state(seed_events, path)
        if self._all_fights and self._all_fights[-1].player_name:
            self._tracker.player_name = self._all_fights[-1].player_name
        self._apply_encounter_filter(
            keep_fight=self._all_fights[-1] if self._all_fights else None,
            fallback_row=len(self._all_fights) - 1,
        )
        self._queue_encounter_ingest()
        self.status.showMessage(
            f"🔴 Watch switched to {Path(path).name} · {self._live_event_count:,} seeded events"
        )

    def _toggle_live_window(self, checked: bool):
        if checked:
            if self._live_window is None:
                self._live_window = LiveBattleWindow(self._tracker)
            self._live_window.show()
            if self.btn_live_threat.isChecked():
                self._toggle_live_threat_window(True)
        else:
            if self._live_window:
                self._live_window.hide()
            if self._live_threat_window:
                self._live_threat_window.hide()
            self.btn_live_threat.setChecked(False)

    def _toggle_live_threat_window(self, checked: bool):
        if checked:
            if self._live_window is None or not self._live_window.isVisible():
                self.btn_live_window.setChecked(True)
                self._toggle_live_window(True)
            QTimer.singleShot(0, self._show_live_threat_window)
        else:
            if self._live_threat_window:
                self._live_threat_window.hide()

    def _show_live_threat_window(self):
        if self._live_window is None or not self._live_window.isVisible():
            return
        if self._live_threat_window is None:
            self._live_threat_window = LiveThreatWindow(self._tracker, self._live_window)
        elif self._live_threat_window._snapped_to_anchor:
            self._live_threat_window._sync_to_anchor()
        self._live_threat_window.show()
        self._live_threat_window._refresh()
        self._live_threat_window.raise_()
        self._live_threat_window.activateWindow()

    def closeEvent(self, event):
        self._pending_db_ingest = []
        self._db_ingest_active = False
        save_window_state(self, "main_window")
        if self._watcher:
            self._watcher.stop()
        if self._live_window:
            self._live_window.close()
        if self._live_threat_window:
            self._live_threat_window.close()
        super().closeEvent(event)