"""
W.I.S.E. Panda — Parser Validation Suite
Audits raw SWTOR combat log lines against parser and aggregator output.

Usage:
    py -3 validate_parser_upgraded.py <combat_log.txt> [fight_number]
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from engine.aggregator import Fight, build_fights, load_raw_lines, resolve_fight_names, scan_fights
from engine.parser_core import LogEvent, parse_file, parse_line


@dataclass
class ValidationResult:
    name: str
    status: str
    message: str
    detail: str = ""


STATUS_SYMBOLS = {
    "PASS": "PASS",
    "WARN": "WARN",
    "FAIL": "FAIL",
    "INFO": "INFO",
}


def seconds_fmt(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _raw_effect_lines(lines: Iterable[str], marker: str) -> List[str]:
    inline_needle = f": {marker} {{"
    prefix_needle = f"[{marker} {{"
    return [line for line in lines if inline_needle in line or prefix_needle in line]


def _count_events(events: Iterable[LogEvent], predicate) -> int:
    return sum(1 for ev in events if predicate(ev))


def _top_damage_entities(fight: Fight, merged: bool = False) -> list[tuple[str, int]]:
    totals: dict[str, int] = {}
    for name, stats in fight.entity_stats.items():
        if merged and "/" in name:
            owner = name.split("/", 1)[0].strip()
            totals[owner] = totals.get(owner, 0) + stats.damage_dealt
            continue
        totals[name] = totals.get(name, 0) + stats.damage_dealt
    return sorted(totals.items(), key=lambda item: (-item[1], item[0]))


def _compare_fights(file_fight: Fight, eager_fight: Optional[Fight]) -> list[ValidationResult]:
    if eager_fight is None:
        return [ValidationResult(
            "Aggregator path parity",
            "FAIL",
            "No matching eager fight found for this scanned fight.",
        )]

    checks: list[ValidationResult] = []
    if len(file_fight.events) != len(eager_fight.events):
        checks.append(ValidationResult(
            "Aggregator event count parity",
            "FAIL",
            f"File-backed fight has {len(file_fight.events)} events; eager fight has {len(eager_fight.events)}.",
        ))
    else:
        checks.append(ValidationResult(
            "Aggregator event count parity",
            "PASS",
            f"Both aggregator paths see {len(file_fight.events)} fight events.",
        ))

    if abs(file_fight.duration_seconds - eager_fight.duration_seconds) > 0.001:
        checks.append(ValidationResult(
            "Aggregator duration parity",
            "WARN",
            f"File-backed duration {file_fight.duration_seconds:.3f}s vs eager {eager_fight.duration_seconds:.3f}s.",
        ))
    else:
        checks.append(ValidationResult(
            "Aggregator duration parity",
            "PASS",
            f"Both aggregator paths report {file_fight.duration_seconds:.3f}s.",
        ))

    mismatches: list[str] = []
    names = set(file_fight.entity_stats) | set(eager_fight.entity_stats)
    for name in sorted(names):
        left = file_fight.entity_stats.get(name)
        right = eager_fight.entity_stats.get(name)
        left_dmg = left.damage_dealt if left else 0
        right_dmg = right.damage_dealt if right else 0
        left_heal = left.healing_done if left else 0
        right_heal = right.healing_done if right else 0
        if left_dmg != right_dmg or left_heal != right_heal:
            mismatches.append(
                f"{name}: damage {left_dmg:,}/{right_dmg:,}, healing {left_heal:,}/{right_heal:,}"
            )
            if len(mismatches) >= 5:
                break

    if mismatches:
        checks.append(ValidationResult(
            "Aggregator stat parity",
            "FAIL",
            "File-backed and eager aggregation disagree on per-entity totals.",
            " | ".join(mismatches),
        ))
    else:
        checks.append(ValidationResult(
            "Aggregator stat parity",
            "PASS",
            "File-backed and eager aggregation agree on per-entity damage/healing totals.",
        ))

    return checks


def validate_fight(
    fight: Fight,
    eager_fight: Optional[Fight],
    log_path: str,
    global_parse_errors: Optional[int],
) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    raw_lines = load_raw_lines(log_path, fight._line_start, fight._line_end)
    parsed_raw = [(line, parse_line(line)) for line in raw_lines]
    raw_failures = [line for line, ev in parsed_raw if ev is None]

    raw_damage = _raw_effect_lines(raw_lines, "Damage")
    raw_heal = _raw_effect_lines(raw_lines, "Heal")
    raw_spend = _raw_effect_lines(raw_lines, "Spend")
    raw_restore = _raw_effect_lines(raw_lines, "Restore")

    parsed_damage = [ev for ev in fight.events if ev.is_damage]
    parsed_heal = [ev for ev in fight.events if ev.is_heal]
    parsed_spend = [ev for ev in fight.events if ev.spend_amount is not None]
    parsed_restore = [ev for ev in fight.events if ev.restore_amount is not None]
    parsed_charges = [ev for ev in fight.events if ev.charges is not None]

    suspicious_damage = [
        line for line, ev in parsed_raw
        if ": Damage {" in line and (ev is None or not ev.is_damage)
    ]
    suspicious_heal = [
        line for line, ev in parsed_raw
        if ": Heal {" in line and (ev is None or not ev.is_heal)
    ]
    suspicious_spend = [
        line for line, ev in parsed_raw
        if ": Spend {" in line and (ev is None or ev.spend_amount is None)
    ]
    suspicious_restore = [
        line for line, ev in parsed_raw
        if ": Restore {" in line and (ev is None or ev.restore_amount is None)
    ]

    results.append(ValidationResult(
        "Parse completeness",
        "PASS" if not raw_failures else "FAIL",
        f"{len(raw_lines) - len(raw_failures)} of {len(raw_lines)} raw lines parsed in this fight.",
        "" if not raw_failures else " | ".join(raw_failures[:3]),
    ))
    if global_parse_errors is not None:
        results.append(ValidationResult(
            "Global parse errors",
            "PASS" if global_parse_errors == 0 else "WARN",
            f"{global_parse_errors} total line parse failures in the whole log.",
        ))
    else:
        results.append(ValidationResult(
            "Global parse errors",
            "INFO",
            "Whole-log parse errors were not computed for this in-app validation run.",
        ))

    results.append(ValidationResult(
        "Raw line coverage",
        "INFO",
        f"Damage={len(raw_damage)} Heal={len(raw_heal)} Spend={len(raw_spend)} Restore={len(raw_restore)} Charges={len(parsed_charges)}",
    ))

    for label, suspicious in (
        ("Damage line classification", suspicious_damage),
        ("Heal line classification", suspicious_heal),
        ("Spend line classification", suspicious_spend),
        ("Restore line classification", suspicious_restore),
    ):
        results.append(ValidationResult(
            label,
            "PASS" if not suspicious else "FAIL",
            "All matching raw lines were classified correctly." if not suspicious else f"{len(suspicious)} suspicious raw lines found.",
            "" if not suspicious else " | ".join(suspicious[:3]),
        ))

    results.append(ValidationResult(
        "Parsed event coverage",
        "INFO",
        f"Parsed damage={len(parsed_damage)} heal={len(parsed_heal)} spend={len(parsed_spend)} restore={len(parsed_restore)}",
    ))

    damage_without_result = [ev for ev in parsed_damage if ev.result is None]
    if damage_without_result:
        samples = [ev.raw_result_text or "(empty result)" for ev in damage_without_result[:5]]
        results.append(ValidationResult(
            "Damage result parsing",
            "FAIL",
            f"{len(damage_without_result)} parsed damage events are missing a result payload.",
            " | ".join(samples),
        ))
    else:
        results.append(ValidationResult(
            "Damage result parsing",
            "PASS",
            "All parsed damage events include a parsed result payload.",
        ))

    boss_name = fight.boss_name or "Unknown Encounter"
    boss_hp_values: list[int] = []
    dmg_to_boss = 0
    for ev in fight.events:
        if ev.target.display_name == boss_name and ev.target.hp is not None:
            boss_hp_values.append(ev.target.hp)
        if ev.source.display_name == boss_name and ev.source.hp is not None:
            boss_hp_values.append(ev.source.hp)
        if ev.is_damage and ev.result and not ev.result.is_miss and ev.target.display_name == boss_name:
            dmg_to_boss += ev.result.amount

    if boss_hp_values:
        hp_lost = max(boss_hp_values) - min(boss_hp_values)
        ratio = (dmg_to_boss / hp_lost) if hp_lost else 0.0
        results.append(ValidationResult(
            "Boss HP sanity",
            "PASS" if hp_lost > 0 else "WARN",
            f"{boss_name}: observed HP range {min(boss_hp_values):,}-{max(boss_hp_values):,}; logged damage to boss {dmg_to_boss:,}.",
            "" if hp_lost == 0 else f"Damage/HP-lost ratio={ratio:.3f}. Ratios above 1 can still happen if there are adds, heals, or snapshot lag.",
        ))

    threat_ratios = []
    for name, stats in fight.entity_stats.items():
        if stats.damage_dealt <= 0:
            continue
        threat = sum(
            ev.result.threat or 0.0
            for ev in fight.events
            if ev.is_damage and ev.result and not ev.result.is_miss and ev.source.display_name == name
        )
        if threat > 0:
            threat_ratios.append(f"{name}={threat / stats.damage_dealt:.2f}x")
    if threat_ratios:
        results.append(ValidationResult(
            "Threat sanity",
            "INFO",
            "Observed threat-to-damage ratios for entities with threat values.",
            " | ".join(threat_ratios[:8]),
        ))

    top_players = _top_damage_entities(fight, merged=False)[:8]
    if top_players:
        results.append(ValidationResult(
            "Damage ranking",
            "INFO",
            "Top damage entities in app aggregation.",
            " | ".join(f"{name}={amount:,}" for name, amount in top_players),
        ))

    top_merged = _top_damage_entities(fight, merged=True)[:8]
    if top_merged:
        results.append(ValidationResult(
            "Damage ranking (companions merged)",
            "INFO",
            "Merged-owner totals for StarParse-style comparisons.",
            " | ".join(f"{name}={amount:,}" for name, amount in top_merged),
        ))

    effect_counts = Counter(f"{ev.effect_type}:{ev.effect_name}" for ev in fight.events)
    results.append(ValidationResult(
        "Top event kinds",
        "INFO",
        ", ".join(f"{name}={count}" for name, count in effect_counts.most_common(8)),
    ))

    results.extend(_compare_fights(fight, eager_fight))
    return results


def print_report(fight: Fight, results: list[ValidationResult]) -> None:
    print(format_report_text(fight, results))


def format_report_text(fight: Fight, results: list[ValidationResult]) -> str:
    lines = [
        "",
        "=" * 88,
        f"VALIDATION REPORT: {fight.boss_name or 'Unknown Encounter'}",
        (
            f"Fight #{fight.index} | Duration {fight.duration_str} ({fight.duration_seconds:.1f}s) "
            f"| Lines {fight._line_start}-{fight._line_end}"
        ),
        "=" * 88,
    ]
    for result in results:
        symbol = STATUS_SYMBOLS.get(result.status, result.status)
        lines.append(f"[{symbol}] {result.name}")
        lines.append(f"  {result.message}")
        if result.detail:
            lines.append(f"  {result.detail}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: py -3 validate_parser_upgraded.py <combat_log.txt> [fight_number]")
        raise SystemExit(1)

    log_path = sys.argv[1]
    fight_num = int(sys.argv[2]) if len(sys.argv) > 2 else None
    if not Path(log_path).exists():
        print(f"File not found: {log_path}")
        raise SystemExit(1)

    print(f"Parsing {Path(log_path).name}...")
    events, errors = parse_file(log_path)
    print(f"  Parsed {len(events)} events with {errors} line parse failures.")

    eager_fights = build_fights(events)
    scanned_fights = scan_fights(log_path)
    resolve_fight_names(log_path, scanned_fights)
    for fight in scanned_fights:
        fight.ensure_loaded()

    print(f"  Eager fights: {len(eager_fights)}")
    print(f"  File-backed fights: {len(scanned_fights)}")

    if len(eager_fights) != len(scanned_fights):
        print("  WARNING: eager and file-backed fight detection disagree on fight count.")

    targets = scanned_fights
    if fight_num is not None:
        targets = [fight for fight in scanned_fights if fight.index == fight_num]
        if not targets:
            print(f"Fight #{fight_num} not found. Available: {[fight.index for fight in scanned_fights]}")
            raise SystemExit(1)

    eager_by_index = {fight.index: fight for fight in eager_fights}
    for fight in targets:
        results = validate_fight(
            fight=fight,
            eager_fight=eager_by_index.get(fight.index),
            log_path=log_path,
            global_parse_errors=errors,
        )
        print_report(fight, results)


if __name__ == "__main__":
    main()
