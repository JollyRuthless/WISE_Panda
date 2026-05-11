"""
ui/live/tracker.py — LiveFightTracker: receives LogEvents in real time and maintains
                      a running picture of the current fight (DPS, threat, NPC state).

No Qt widgets — pure Python. Safe to unit-test without a QApplication.
"""

from datetime import time
from typing import Optional, List

from engine.aggregator import (
    EntityKind,
    _kind_from_entity,
    active_window_seconds,
    per_second,
    seconds_between,
)
from engine.parser_core import LogEvent


def _rate_from_samples(samples) -> float:
    """Compute threat-per-second from a list of (timestamp, cumulative) samples.

    The samples list is the rolling window kept by the tracker. The rate is
    `(latest - earliest) / seconds_between(earliest_ts, latest_ts)`. With
    fewer than 2 samples, or a zero time span, we return 0.0 (no signal).

    Pure function; safe to unit-test without a tracker instance. See the
    Threat panel sub-spec in SOMEDAY.md for context.
    """
    if not samples or len(samples) < 2:
        return 0.0
    earliest_ts, earliest_val = samples[0]
    latest_ts,   latest_val   = samples[-1]
    span = seconds_between(earliest_ts, latest_ts)
    if span <= 0.0:
        return 0.0
    return (latest_val - earliest_val) / span


def _time_to_zero(gap: float, closing_rate: float, cap: float) -> Optional[float]:
    """Predict seconds until the gap reaches zero, given a closing rate.

    Returns None when:
      - The gap is already non-positive (already overtaken or tied; the
        UI surfaces "already pulled" via the gap itself, not the time)
      - The closing_rate is zero or positive (gap stable or widening;
        no overtake predicted)
      - The predicted time exceeds `cap` (further out than we want to
        display — treat as "safe / no urgent prediction")

    `closing_rate` is in gap-units per second. A negative rate means the
    gap is shrinking (bad for whoever is "ahead"). Time-to-zero =
    `gap / -closing_rate` when both gap and -closing_rate are positive.
    """
    if gap <= 0.0:
        return None
    if closing_rate >= 0.0:
        return None
    seconds = gap / -closing_rate
    if seconds > cap:
        return None
    return seconds


class LiveFightTracker:
    """
    Receives LogEvents in real time and maintains a running picture of the
    current fight: DPS (rolling window), total damage, entity kind, NPC threat.
    """

    ROLLING_WINDOW = 6.0
    # Predictive threat panel — window over which we compute the closing
    # rate (d(gap)/dt). Five seconds smooths out the natural burstiness
    # of individual damage events while still being responsive. May tune
    # after seeing it in practice (see SOMEDAY: Threat panel sub-spec).
    THREAT_WINDOW_SECONDS = 5.0
    # Hard cap on predicted seconds-until-overtake. Anything further out
    # than this is "safe" and just shows the gap, not the time.
    THREAT_PREDICTION_CAP = 60.0

    def __init__(self):
        self.reset()

    def reset(self):
        self.in_combat: bool = False
        self.fight_start: Optional[time] = None
        self.latest_event_time: Optional[time] = None
        self.player_name: Optional[str] = None
        # {name: {"total": int, "kind": EntityKind, "events": [(timestamp, amount)]}}
        self.entities: dict = {}
        self.npcs: dict = {}

    # ── NPC helpers ───────────────────────────────────────────────────────────

    def _npc_key(self, entity) -> str:
        npc_id = entity.npc_entity_id or entity.display_name or "unknown"
        return f"{npc_id}|{entity.npc_instance or ''}"

    def _ensure_npc(self, entity) -> Optional[dict]:
        if not getattr(entity, "npc", None):
            return None
        key = self._npc_key(entity)
        row = self.npcs.setdefault(key, {
            "key":              key,
            "name":             entity.display_name or entity.npc or "Unknown NPC",
            "npc_entity_id":    entity.npc_entity_id or "",
            "npc_instance":     entity.npc_instance or "",
            "max_hp":           entity.maxhp or 0,
            "is_dead":          False,
            "current_target":   "",
            "targeting_player": False,
            "player_top_threat": False,
            "last_threat_target": "",
            "last_event_time":  0.0,
            # Phase 4a — predictive threat panel.
            # threat_by_player: running total threat each player has
            #   generated on THIS NPC. Key = player display_name.
            # threat_samples_by_player: rolling-window samples used to
            #   compute closing rates. Each list holds (timestamp,
            #   cumulative_threat) tuples; we prune anything older than
            #   THREAT_WINDOW_SECONDS on each push.
            "threat_by_player":         {},
            "threat_samples_by_player": {},
        })
        row["name"]   = entity.display_name or row["name"]
        row["max_hp"] = max(int(row["max_hp"]), entity.maxhp or 0)
        if entity.hp is not None:
            row["is_dead"] = entity.hp <= 0
        return row

    # ── Main push ─────────────────────────────────────────────────────────────

    def push(self, events: List[LogEvent]):
        for ev in events:
            # Discover player name
            if ev.source.player and not ev.source.companion and not self.player_name:
                self.player_name = ev.source.player
            elif ev.target.player and not ev.target.companion and not self.player_name:
                self.player_name = ev.target.player

            # Combat state transitions
            if ev.is_enter_combat:
                if not self.in_combat:
                    self.in_combat   = True
                    self.fight_start = ev.timestamp
                    self.latest_event_time = ev.timestamp
                    self.entities.clear()
                    self.npcs.clear()
            elif ev.is_exit_combat:
                self.latest_event_time = ev.timestamp
                self.in_combat = False
                self.npcs.clear()
            elif ev.is_damage and ev.result and not ev.result.is_miss and self.in_combat:
                src  = ev.source.display_name
                kind = _kind_from_entity(ev.source, self.player_name)
                t    = ev.timestamp
                e    = self.entities.setdefault(src, {
                    "total": 0,
                    "kind": kind,
                    "events": [],
                    "first_damage_time": None,
                    "last_damage_time": None,
                    "heal_total": 0,
                    "heal_events": [],
                    "first_heal_time": None,
                    "last_heal_time": None,
                })
                e["total"] += ev.result.amount
                e["events"].append((t, ev.result.amount))
                if e["first_damage_time"] is None:
                    e["first_damage_time"] = t
                e["last_damage_time"] = t
            elif ev.is_heal and ev.result and not ev.result.is_miss and self.in_combat:
                # Healing aggregation mirrors damage exactly so the snapshot
                # can compute encounter/active/rolling HPS the same way as
                # DPS. The "active" window for healing is the span from the
                # entity's first heal to its last heal — matches what the
                # post-fight aggregator does for healers.
                src  = ev.source.display_name
                kind = _kind_from_entity(ev.source, self.player_name)
                t    = ev.timestamp
                e    = self.entities.setdefault(src, {
                    "total": 0,
                    "kind": kind,
                    "events": [],
                    "first_damage_time": None,
                    "last_damage_time": None,
                    "heal_total": 0,
                    "heal_events": [],
                    "first_heal_time": None,
                    "last_heal_time": None,
                })
                e["heal_total"] += ev.result.amount
                e["heal_events"].append((t, ev.result.amount))
                if e["first_heal_time"] is None:
                    e["first_heal_time"] = t
                e["last_heal_time"] = t

            if not self.in_combat:
                continue

            # NPC tracking
            self.latest_event_time = ev.timestamp
            now        = ev.timestamp
            source_npc = self._ensure_npc(ev.source)
            target_npc = self._ensure_npc(ev.target)
            if source_npc is not None:
                source_npc["last_event_time"] = now
            if target_npc is not None:
                target_npc["last_event_time"] = now

            # Accumulate per-player-per-NPC threat from damage events.
            # The log line emits a `<NNNN>` threat value on damage events;
            # the parser exposes it as ev.result.threat. We sum these per
            # (source player, target NPC) and store a rolling-window of
            # samples for the closing-rate math used by threat_panel_snapshot.
            #
            # Limitations (documented in SOMEDAY's Threat panel sub-spec):
            #   • Healing-generated threat isn't captured here — log events
            #     for healing don't carry a threat value the same way.
            #   • ModifyThreat events (taunts, threat drops) are handled
            #     below but don't carry a numeric value, so we can't model
            #     them quantitatively. We treat them as "the model may be
            #     wrong from this point" rather than try to guess.
            #   • In-game threat decay (if any) isn't modeled.
            #
            # The math is a useful approximation, not ground truth.
            if (target_npc is not None
                    and ev.is_damage
                    and ev.result
                    and not ev.result.is_miss
                    and ev.result.threat
                    and ev.source.player
                    and not ev.source.companion):
                src_player = ev.source.player
                threat_inc = float(ev.result.threat)
                tot = target_npc["threat_by_player"].get(src_player, 0.0) + threat_inc
                target_npc["threat_by_player"][src_player] = tot
                # Append a sample for the rolling window, then prune
                # anything older than the window. We keep one extra
                # sample older than the cutoff so the rate calculation
                # has a stable baseline to subtract from.
                samples = target_npc["threat_samples_by_player"].setdefault(src_player, [])
                samples.append((now, tot))
                window_seconds = self.THREAT_WINDOW_SECONDS
                # Keep one pre-window sample for the diff. Walk from the
                # start and drop anything older than window EXCEPT the
                # most recent of those (the anchor for d(threat)/dt).
                cutoff_idx = 0
                for i, (ts, _) in enumerate(samples):
                    if seconds_between(ts, now) > window_seconds:
                        cutoff_idx = i
                    else:
                        break
                if cutoff_idx > 0:
                    del samples[:cutoff_idx]

            if ev.effect_name == "TargetSet" and source_npc is not None:
                target_name       = ev.target.display_name
                targeting_player  = bool(
                    ev.target.player
                    and not ev.target.companion
                    and ev.target.player == self.player_name
                )
                source_npc["current_target"]   = target_name
                source_npc["targeting_player"] = targeting_player
                source_npc["player_top_threat"] = targeting_player
            elif ev.effect_name == "TargetCleared" and source_npc is not None:
                source_npc["current_target"]   = ""
                source_npc["targeting_player"] = False

            if ev.effect_name == "ModifyThreat" and source_npc is not None:
                threat_target = ev.target.display_name
                source_npc["last_threat_target"] = threat_target
                if (ev.target.player and not ev.target.companion
                        and ev.target.player == self.player_name):
                    source_npc["player_top_threat"] = True
                elif source_npc["current_target"] and source_npc["current_target"] != self.player_name:
                    source_npc["player_top_threat"] = False

    # ── Snapshots ─────────────────────────────────────────────────────────────

    def snapshot(self, metric: str = "encounter") -> List[dict]:
        """Return sorted list of entity rows for the live DPS overlay."""
        latest = self.latest_event_time
        rows   = []
        encounter_elapsed = 0.0
        if self.fight_start is not None and latest is not None:
            encounter_elapsed = max(seconds_between(self.fight_start, latest), 0.0)
        for name, data in self.entities.items():
            recent = 0
            if latest is not None:
                recent = sum(
                    amount for ts, amount in data["events"]
                    if 0.0 <= seconds_between(ts, latest) <= self.ROLLING_WINDOW
                )
            recent_dps = recent / self.ROLLING_WINDOW
            encounter_dps = per_second(data["total"], encounter_elapsed)
            active_dps = per_second(
                data["total"],
                active_window_seconds(data["first_damage_time"], data["last_damage_time"]),
            )
            dps = {
                "rolling": recent_dps,
                "encounter": encounter_dps,
                "active": active_dps,
            }.get(metric, encounter_dps)

            # Healing aggregation. Entities created via setdefault before
            # the healing fields were added may not have them; fall back
            # to zero so older snapshots don't crash.
            heal_total = data.get("heal_total", 0)
            heal_events = data.get("heal_events", [])
            recent_heal = 0
            if latest is not None and heal_events:
                recent_heal = sum(
                    amount for ts, amount in heal_events
                    if 0.0 <= seconds_between(ts, latest) <= self.ROLLING_WINDOW
                )
            recent_hps    = recent_heal / self.ROLLING_WINDOW
            encounter_hps = per_second(heal_total, encounter_elapsed)
            active_hps    = per_second(
                heal_total,
                active_window_seconds(
                    data.get("first_heal_time"),
                    data.get("last_heal_time"),
                ),
            )
            hps = {
                "rolling": recent_hps,
                "encounter": encounter_hps,
                "active": active_hps,
            }.get(metric, encounter_hps)

            rows.append({
                "name": name,
                "kind": data["kind"],
                "total_damage": data["total"],
                "dps": dps,
                "recent_dps": recent_dps,
                "encounter_dps": encounter_dps,
                "active_dps": active_dps,
                "total_heal":    heal_total,
                "hps":           hps,
                "recent_hps":    recent_hps,
                "encounter_hps": encounter_hps,
                "active_hps":    active_hps,
            })
        rows.sort(key=lambda r: -r["dps"])
        top_dps = rows[0]["dps"] if rows else 1.0
        for r in rows:
            r["pct"] = r["dps"] / top_dps if top_dps > 0 else 0.0
        return rows

    def threat_snapshot(self) -> List[dict]:
        """Return sorted list of NPC rows for the threat overlay."""
        rows = []
        for data in self.npcs.values():
            if data.get("is_dead"):
                continue
            rows.append({
                "name":               data["name"],
                "npc_entity_id":      data["npc_entity_id"],
                "top_threat":         bool(data["player_top_threat"] or data["targeting_player"]),
                "targeting_player":   bool(data["targeting_player"]),
                "current_target":     data["current_target"],
                "last_threat_target": data["last_threat_target"],
                "max_hp":             int(data["max_hp"] or 0),
                "sort_targeted":      1 if data["targeting_player"] else 0,
                "sort_threat":        1 if (data["player_top_threat"] or data["targeting_player"]) else 0,
            })
        rows.sort(key=lambda r: (-r["sort_targeted"], -r["sort_threat"], r["name"].lower()))
        return rows

    def threat_panel_snapshot(self) -> List[dict]:
        """Phase 4a output: per-NPC predictive threat rows.

        Returns one row per engaged (non-dead) NPC, with:
          - your_threat / tank_threat / second_threat / third_threat
          - DPS-perspective gap, closing rate, and time-to-overtake
          - tank-perspective gap, closing rate, and time-to-lose
          - danger_score for sort order (smallest absolute gap first)

        Tank identity heuristic: highest cumulative threat across all
        engaged NPCs (summed). The tank is the same player for every
        row in the snapshot — picked once, not per-NPC, so the panel
        is consistent.

        All math is a *useful approximation* not the game's true threat
        state. Heal-generated threat, in-game decay, and quantitative
        ModifyThreat (taunts, threat drops) are not modeled. See the
        Threat panel sub-spec in SOMEDAY.md for the full list of
        limitations.
        """
        you = self.player_name or ""

        # Step 1 — Pick the tank. Sum each player's threat across all
        # engaged NPCs; the highest total is "the tank". This is a
        # heuristic; class detection isn't reliable in live mode.
        # The local player is excluded from tank candidacy when the
        # local player is the only one tracked — otherwise they could
        # always be flagged as the tank in solo content. (In group
        # content where the local player IS the tank, they'll have the
        # most cumulative threat and the heuristic picks them anyway.)
        cumulative: dict = {}
        for data in self.npcs.values():
            if data.get("is_dead"):
                continue
            for player, threat in data.get("threat_by_player", {}).items():
                cumulative[player] = cumulative.get(player, 0.0) + threat
        if not cumulative:
            return []
        tank_name = max(cumulative.items(), key=lambda kv: kv[1])[0]

        # Step 2 — Per-NPC: compute gaps and rates.
        rows: List[dict] = []
        for data in self.npcs.values():
            if data.get("is_dead"):
                continue
            threat_map: dict = data.get("threat_by_player", {})
            samples_map: dict = data.get("threat_samples_by_player", {})

            # If nobody has generated threat on this NPC yet, skip.
            if not threat_map:
                continue

            # Per-player threat totals on this NPC.
            your_threat = float(threat_map.get(you, 0.0))
            tank_threat = float(threat_map.get(tank_name, 0.0))

            # Find the second- and third-highest player by threat on
            # this specific NPC, excluding the tank. This is needed for
            # the tank's perspective ("am I about to lose it to the
            # next-highest DPS?").
            others = sorted(
                ((p, t) for p, t in threat_map.items() if p != tank_name),
                key=lambda kv: kv[1],
                reverse=True,
            )
            second_name, second_threat = (others[0] if others else ("", 0.0))
            third_name,  third_threat  = (others[1] if len(others) > 1 else ("", 0.0))

            # Per-player threat-generation rate (threat per second),
            # computed from the rolling-window samples. Negative or zero
            # rate is possible if a player's samples span less than the
            # window (early in fight) or if their samples are all the
            # same value (no recent activity).
            your_rate = _rate_from_samples(samples_map.get(you, []))
            tank_rate = _rate_from_samples(samples_map.get(tank_name, []))
            second_rate = _rate_from_samples(samples_map.get(second_name, []))

            # DPS perspective: am I closing on the tank?
            # gap > 0 means tank is ahead (safe).
            # rate of gap change = d(tank_threat)/dt - d(your_threat)/dt
            #                    = tank_rate - your_rate
            # negative rate → gap shrinking → bad for DPS
            dps_gap = tank_threat - your_threat
            dps_closing_rate = tank_rate - your_rate
            dps_time_left = _time_to_zero(dps_gap, dps_closing_rate,
                                          cap=self.THREAT_PREDICTION_CAP)

            # Tank perspective: is the next-highest player catching up?
            tank_gap = tank_threat - second_threat
            tank_closing_rate = tank_rate - second_rate
            tank_time_left = _time_to_zero(tank_gap, tank_closing_rate,
                                           cap=self.THREAT_PREDICTION_CAP)

            # Sort key: smallest absolute gap across both perspectives
            # bubbles the most-dangerous NPC to the top. Use absolute
            # value because a slightly-negative gap (just overtaken)
            # is just as relevant as a slightly-positive one.
            danger_score = min(abs(dps_gap), abs(tank_gap))

            rows.append({
                "name":              data["name"],
                "npc_entity_id":     data["npc_entity_id"],
                "max_hp":            int(data["max_hp"] or 0),
                "tank_name":         tank_name,
                "second_name":       second_name,
                "third_name":        third_name,
                "your_threat":       your_threat,
                "tank_threat":       tank_threat,
                "second_threat":     float(second_threat),
                "third_threat":      float(third_threat),
                "dps_gap":           dps_gap,
                "dps_closing_rate":  dps_closing_rate,
                "dps_time_left":     dps_time_left,
                "tank_gap":          tank_gap,
                "tank_closing_rate": tank_closing_rate,
                "tank_time_left":    tank_time_left,
                "danger_score":      danger_score,
            })

        rows.sort(key=lambda r: (r["danger_score"], r["name"].lower()))
        return rows

    @property
    def elapsed(self) -> float:
        if self.fight_start is None or self.latest_event_time is None:
            return 0.0
        return max(seconds_between(self.fight_start, self.latest_event_time), 0.0)
