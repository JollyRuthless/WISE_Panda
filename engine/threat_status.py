"""
engine/threat_status.py — Threat panel status grading.

Pure functions for grading a threat-panel row as green / yellow / red.
Kept out of the UI module so tests can run without PyQt6 and so the
logic can be reused (e.g. if we ever expose threat status from another
surface like the floating Threat Board overlay).

Phase 4c of the threat panel build. See SOMEDAY.md's Threat panel
sub-spec for the design context.
"""

from typing import Optional


# Thresholds used by _perspective_status. Tunable — these are educated
# guesses; expect to revise after seeing real fights.
#
# "Small gap" is the smaller of:
#   - absolute threat units (covers low-threat early-fight scenarios)
#   - percentage of the tank's threat (covers high-threat late-fight)
# Whichever fires first triggers a yellow signal.
THREAT_SMALL_GAP_ABS    = 500.0
THREAT_SMALL_GAP_PCT    = 0.05    # 5% of reference threat
# "Meaningfully negative" closing rate — distinguishes real catch-up
# from small fluctuations. Negative because the gap is shrinking.
THREAT_NEGATIVE_RATE    = -10.0   # threat-per-second
# "Imminent overtake" — when this fires AND closing rate is negative,
# the row turns red.
THREAT_IMMINENT_SECONDS = 5.0


def _perspective_status(gap: float, closing_rate: float,
                        time_left: Optional[float],
                        reference_threat: float) -> str:
    """Return 'red' / 'yellow' / 'green' for one threat perspective.

    Each row has two perspectives (DPS and tank); the row's overall
    color is the worst of the two — that's threat_row_status().

    The math:
      - red    = overtake imminent (time_left < THREAT_IMMINENT_SECONDS
                                    AND closing_rate < THREAT_NEGATIVE_RATE)
      - yellow = warning (gap is "small" OR closing_rate is
                          meaningfully negative)
      - green  = safe (everything else)

    `reference_threat` is the threat of the player being compared
    against. Used to compute the percentage gap threshold. When zero
    (no one else has threat yet), the percentage check is skipped.
    """
    if (time_left is not None
            and time_left < THREAT_IMMINENT_SECONDS
            and closing_rate < THREAT_NEGATIVE_RATE):
        return "red"

    gap_is_small = abs(gap) < THREAT_SMALL_GAP_ABS
    if (reference_threat > 0
            and abs(gap) < THREAT_SMALL_GAP_PCT * reference_threat):
        gap_is_small = True
    if gap_is_small or closing_rate < THREAT_NEGATIVE_RATE:
        return "yellow"

    return "green"


def threat_row_status(row: dict) -> str:
    """Return 'red' / 'yellow' / 'green' for a single threat-panel row.

    The row's status is the worst of two perspectives:
      - DPS perspective:  your_gap vs tank
      - tank perspective: tank's gap vs second-place player

    Edge case: if a perspective has zero reference threat (no second-
    place player yet, or no tank yet), that perspective is forced to
    green because there's nothing to compare against.
    """
    # DPS perspective (am I about to pull?)
    if row.get("tank_threat", 0.0) > 0:
        dps_status = _perspective_status(
            gap=row.get("dps_gap", 0.0),
            closing_rate=row.get("dps_closing_rate", 0.0),
            time_left=row.get("dps_time_left"),
            reference_threat=row.get("tank_threat", 0.0),
        )
    else:
        dps_status = "green"

    # Tank perspective (am I about to lose it?)
    if row.get("second_threat", 0.0) > 0:
        tank_status = _perspective_status(
            gap=row.get("tank_gap", 0.0),
            closing_rate=row.get("tank_closing_rate", 0.0),
            time_left=row.get("tank_time_left"),
            reference_threat=row.get("tank_threat", 0.0),
        )
    else:
        tank_status = "green"

    severity = {"green": 0, "yellow": 1, "red": 2}
    if severity[dps_status] >= severity[tank_status]:
        return dps_status
    return tank_status
