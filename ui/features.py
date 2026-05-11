"""
ui/features.py — Tab feature registry for the main application shell.

Keeps tab registration declarative so MainWindow can host features without
hard-coding each tab's construction and wiring inline.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from PyQt6.QtWidgets import QWidget

from training_tabs import (
    ComparisonTab,
    DpsTrainingTab,
    HealerTrainingTab,
    RotationTab,
    TankTrainingTab,
)
from ui.tabs.dashboard import DashboardTab
from ui.tabs.ability import AbilityTab
from ui.tabs.cohort_compare import CohortCompareTab
from ui.tabs.find_fight import FindFightTab
from ui.tabs.inspector import InspectorTab
from ui.tabs.live_combat_stream import LiveCombatStreamTab
from ui.tabs.mob import MobContributionTab
from ui.tabs.overview import OverviewTab
from ui.tabs.raw_log import RawFightLogTab

if TYPE_CHECKING:
    from ui.main_window import MainWindow


FeatureFactory = Callable[[], QWidget]
FeatureSetup = Callable[["MainWindow", QWidget], None]


@dataclass(frozen=True)
class TabFeatureDefinition:
    feature_id: str
    label: str
    attr_name: str
    factory: FeatureFactory
    setup: Optional[FeatureSetup] = None


@dataclass
class TabFeature:
    definition: TabFeatureDefinition
    widget: QWidget

    @property
    def feature_id(self) -> str:
        return self.definition.feature_id

    @property
    def label(self) -> str:
        return self.definition.label

    @property
    def attr_name(self) -> str:
        return self.definition.attr_name


def _connect_overview_filters(window: "MainWindow", widget: QWidget) -> None:
    if isinstance(widget, OverviewTab):
        widget.filters_changed.connect(window._on_overview_filters_changed)


def _connect_dashboard_actions(window: "MainWindow", widget: QWidget) -> None:
    if isinstance(widget, DashboardTab):
        widget.open_log_requested.connect(window.open_file)
        widget.import_logs_requested.connect(window.import_logs_to_database)
        widget.import_all_logs_requested.connect(window.import_all_logs_to_database)
        widget.characters_requested.connect(window._view_characters)
        widget.seen_players_requested.connect(window._view_player_roster)
        widget.import_history_requested.connect(window._view_import_history)


def _connect_inspector_actions(window: "MainWindow", widget: QWidget) -> None:
    """
    Refresh the Inspector once on first build so it shows current data
    without the user having to click Refresh. After this, refreshes are
    user-driven via the button.
    """
    if isinstance(widget, InspectorTab):
        # Defer the first refresh until after MainWindow finishes wiring.
        # If we call it during build_tab_features the database may not be
        # initialized yet (init_db hasn't run). MainWindow's startup will
        # call refresh once everything is ready.
        widget.refresh()


def _connect_find_fight_actions(window: "MainWindow", widget: QWidget) -> None:
    """
    Wire Phase F's Find-a-Fight tab. Two pieces:
      - Refresh the dropdowns on first build so they show real data
      - Connect the "Open this fight" signal to main_window's loader
    """
    if isinstance(widget, FindFightTab):
        widget.refresh()
        widget.fight_open_requested.connect(window.open_fight_by_encounter_key)


def default_tab_feature_definitions() -> list[TabFeatureDefinition]:
    return [
        TabFeatureDefinition("dashboard", "Home", "dashboard_tab", DashboardTab, _connect_dashboard_actions),
        TabFeatureDefinition("live_combat_stream", "📡  Live Combat Stream", "live_combat_stream_tab", LiveCombatStreamTab),
        TabFeatureDefinition("inspector", "🔍  Inspector", "inspector_tab", InspectorTab, _connect_inspector_actions),
        TabFeatureDefinition("find_fight", "🔎  Find a Fight", "find_fight_tab", FindFightTab, _connect_find_fight_actions),
        TabFeatureDefinition("overview", "📊  Overview", "overview_tab", OverviewTab, _connect_overview_filters),
        TabFeatureDefinition("abilities", "⚡  Abilities", "ability_tab", AbilityTab),
        TabFeatureDefinition("mob_contributions", "🎯  Mob Contributions", "mob_tab", MobContributionTab),
        TabFeatureDefinition("raw_log", "🧾  Raw Fight Log", "raw_log_tab", RawFightLogTab),
        TabFeatureDefinition("rotation", "🔄  Rotation", "rotation_tab", RotationTab),
        TabFeatureDefinition("compare", "⚔  Compare", "comparison_tab", ComparisonTab),
        TabFeatureDefinition("cohort_compare", "🎓  Cohort", "cohort_compare_tab", CohortCompareTab),
        TabFeatureDefinition("dps_training", "🎯  DPS Training", "dps_tab", DpsTrainingTab),
        TabFeatureDefinition("tank_training", "🛡  Tank Training", "tank_tab", TankTrainingTab),
        TabFeatureDefinition("healer_training", "💊  Healer Training", "healer_tab", HealerTrainingTab),
    ]


def build_tab_features(window: "MainWindow") -> list[TabFeature]:
    features: list[TabFeature] = []
    for definition in default_tab_feature_definitions():
        widget = definition.factory()
        setattr(window, definition.attr_name, widget)
        if definition.setup is not None:
            definition.setup(window, widget)
        features.append(TabFeature(definition=definition, widget=widget))
    return features
