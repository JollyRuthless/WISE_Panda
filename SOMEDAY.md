# App Name Y.O.D.A
Your Observational Data Agent

## Focus — AI words

The database is the benchmark library. Every imported log makes coaching
better.

The two priorities are:
1. The in-game overlay as the doorway.
2. Coaching that compares each fight to historical cohorts of the same
   class on the same boss. Same-fight peer comparison is a special case
   of the same engine.

## Focus — my own words

1. The overlay needs to be **hero-focused** — show the player the
   information they need to perform better. Beyond the main bars, other
   parts of the overlay should flash up in short bursts to help the
   player stay on track. This is different from post-fight analysis
   because it is **live data**.
2. I want to use the history of all the fights to track data on every
   time a given mob has been fought. The result is performance data for
   every class over time, so each class has something real to compare
   to. **Comparing to others not of the same class is not helpful for
   training.**

# App Ideas
- Let the user reorder tabs and save that layout so the app remembers it between sessions.
- Add long-term performance tracking so a player can see whether they are improving over time, and compare performance across specs or classes.

## Boss skill warnings (live sound cues)

Add sounds and a "skill watch" for bosses, so the overlay can warn the
player something is coming. Cast bar appears → sound plays → player
reacts in time instead of reacting to the damage.

**Open design question — how is this built?**

Two paths:
- (a) Build a list of every boss skill in the game, then let the user
  pick which ones get warnings.
- (b) Start from the operations themselves ("Dxun", "Nature of Progress"),
  list the bosses inside each op, then attach warnings per boss.

Path (b) is probably better because **most people don't know the bosses
without the context of the operation.** Saying "Master Mode Apex Vanguard"
out of nowhere means nothing; saying "the second boss in Dxun" lands.

This idea depends on having a curated list somewhere — either of skills
(path a) or of operations + bosses (path b). Either way it is real
content work, not just code.

## Live Combat Stream — new tab

A dedicated tab for **what's happening right now**, designed for a
second monitor while the game is on the first. Inspired by the existing
Overview tab but built around live data instead of post-fight data.

### Why a new tab and not a re-skinned Overview

Overview is the *post-fight* analysis tab. Mixing live and recorded data
into one tab gets ambiguous ("overview of what?") and clutters both
modes. A separate tab lets each one be designed for its actual purpose.

### What's on the tab

- **Stat cards across the top** — same shape as Overview today (Duration,
  DPS, Active DPS, HPS, Total damage, Crit rate, etc.) but updating in
  real time during the fight.
- **Combatant table below** — same layout as Overview, but live values.
- **Threat panel** — the predictive threat work (see commitments below).
  This is where it lives. Bigger and more detailed than the small
  floating Threat Board.
- **Fight picker** — last 5 live fights, most recent at the top. Click
  back to review any of them. Anything older needs a future search
  feature; not in scope here.
- **Copy-to-chat button** — see "Bragging rights" below.

### Visual states (very deliberate)

- **Main window border turns red** the moment Live mode is on — like a
  broadcast "on air" indicator. You can't miss that the app is in live
  mode.
- **Stat cards have a purple border** while a fight is in progress —
  numbers are still changing.
- **Stat cards have a gold border** once the fight ends — fight is "in
  the books," numbers are final.

The border colour alone tells the player what state every fight is in,
without reading any text.

### Bragging rights — copy to chat

One copy button at the tab level (not per row). Copies whatever fight is
currently selected in the picker. Format is **top 3 DPS + top healer**:

```
Apex Vanguard 2:47 · DPS: Jolly 14k / Cinder 12k / Doomside 10k · Heal: Marina 5.9k
```

Pastes straight into SWTOR group chat, ops chat, or wherever. Possible
future extension: integrate with a guild Discord (post automatically to
a webhook).

### Persistence — the "relic" table

A new dedicated table for live fight snapshots. **Separate from the
existing fights table** — this is a deliberate architectural choice:

- **Importing logs must never touch this table.** A log imported later
  cannot back-fill a live fight snapshot.
- **No back-filling, ever.** A live fight snapshot only exists because
  the app was running when it happened. That's the whole point — it's
  a relic of you actually being there.
- Schema sketch: timestamp, source log path, and the aggregated values
  needed for the cards/table/copy-button. Identity probably follows the
  existing `encounter_key` pattern (`log_path|line_start|line_end|start_time`).

### Open questions / things to design when we build it

- What exact fields go in the live snapshot table? (Probably mirror
  what Overview shows + top-3 + threat summary.)
- Where does the fight picker live on the tab? (Side panel? Top strip?
  Chips below the stat cards?)
- The threat panel needs the predictive math (gap, closing rate,
  time-to-overtake) — that work is now on the critical path for this
  tab, not optional.
- Should the red "on air" border be only on Live Combat Stream, or on
  the whole main window? Probably whole window — it's a mode indicator,
  not a tab indicator.

### Scope honesty

This is a real feature, not a small thing. It involves:
- A new tab (UI work)
- Live data plumbing into the existing stat cards/table
- A new DB table + persistence on combat exit
- The predictive threat math (previously a separate idea, now part of this)
- A copy-to-chat formatter

Probably a couple of careful evenings to do well. Worth doing — it is
arguably the single most useful thing the live side of the app could
gain — but worth knowing the scope before starting.

## Threat panel — sub-spec for Live Combat Stream

The threat panel inside the Live Combat Stream tab. Predictive, not
reactive. The pitch: a DPS knows they're about to pull aggro *before*
they pull it; a tank knows their grip is slipping. Other parsers show
current threat numbers; this one shows what's about to happen.

### Design decisions (locked in)

- **Both perspectives at once**: each NPC row shows both your-vs-tank
  gap (DPS perspective: "am I about to pull?") and tank's-vs-second
  gap (tank perspective: "am I about to lose it?"). More info, more
  screen space — fine for a second-monitor live tab.
- **Per-NPC ranked list**: one row per active NPC, sorted by danger
  level (smallest gap first). Scales from a single boss to a 20-mob
  trash pull.
- **5-second rolling window** for closing-rate calculation. Smooths
  the natural burstiness of damage events into a usable prediction.
  May tune up or down after seeing it in practice.
- **"Approximate" label** somewhere on the panel — the math is a
  *model* built from per-event threat increments, not the game's true
  threat state. Decay, taunts, threat-drops, and threat-modifier
  buffs we can't see all mean our model drifts from ground truth.

### Math (the spine of the feature)

Per (source_player, target_npc) pair, accumulate threat by summing
`event.result.threat` from damage events. ModifyThreat events also
adjust the accumulator. This gives an absolute threat number per
player per NPC.

For each NPC where the player is engaged:

    your_threat       = accumulated threat by local player
    tank_threat       = accumulated threat by group's tank
    second_threat     = next-highest non-tank player's threat
    third_threat      = third-highest player's threat

DPS perspective (am I about to pull?):
    dps_gap           = tank_threat - your_threat   (positive = safe)
    dps_closing_rate  = d(dps_gap)/dt over 5s window
    dps_time_left     = dps_gap / -dps_closing_rate when closing_rate < 0

Tank perspective (am I about to lose it?):
    tank_gap          = tank_threat - second_threat   (positive = safe)
    tank_closing_rate = d(tank_gap)/dt over 5s window
    tank_time_left    = tank_gap / -tank_closing_rate when closing_rate < 0

"Tank" identity: heuristic — highest cumulative threat over the fight,
or the player generating threat at the highest sustained rate. Not
relying on class detection because that's brittle and live mode might
not know everyone's class yet.

### Refresh and stability

- Recompute on every event tick (same hook as the rest of the tab).
- Closing rate uses a rolling 5-second window: store (timestamp,
  threat) samples per (player, npc) pair, prune anything older than
  the window. Rate = (latest_threat - oldest_threat) / window_seconds.
- Prediction time is in seconds, capped at 60 (anything further out is
  "safe" and just shows the gap, not the time).

### UI columns

Per-NPC row:

    NPC name | HP% | Your threat | Tank threat | DPS gap | DPS time |
                                                Tank gap | Tank time

Status icon (green/yellow/red) sits at the left edge based on the
worst signal across both perspectives.

Sorting: by danger first — smallest absolute gap (your-vs-tank OR
tank-vs-second, whichever is smaller) at the top. NPCs not currently
engaged with the player drop to the bottom or are filtered out.

### Colour treatment (4c — last build phase)

- Green: gap is healthy, closing rate is zero or positive (you're
  gaining ground in the right direction)
- Yellow: gap is small (under some threshold) OR closing rate is
  negative (drifting toward overtake)
- Red: time-to-overtake is under 5 seconds AND closing rate is
  meaningfully negative

Exact thresholds need tuning after we see real numbers — flag for the
4c build.

### Build phases (commitment for this session, in order)

- **4a** — Tracker math. `threat_panel_snapshot()` returns per-NPC
  rows with gaps, closing rates, predictions. Unit tests for the
  math itself (synthetic events through the tracker, check the
  derived numbers). NO UI in this phase.
- **4b** — UI widget. Reads from the snapshot, renders the table
  inside Live Combat Stream tab. Numbers only, no color coding yet.
- **4c** — Color coding + status icon + "approximate" label +
  threshold tuning if needed.

If 4a takes longer than expected, stop at 4a and ship clean. Math
correctness matters more than visible UI.

### Open questions (decide before or during 4b)

- Does the panel go above or below the combatant table? Above probably
  — threat is more time-critical than combatant comparisons.
- Should the panel collapse when there's no active threat-modeled NPC,
  to give the table more space? Probably yes, with a small "Threat:
  no NPCs engaged" placeholder.
- Tank identity heuristic — does "highest cumulative threat" actually
  pick the tank, or sometimes pick a heavy-damage DPS? May need to
  factor in role detection from the abilities used.

## Toolbar restructure — groups, menus, and Live Tracker

The current toolbar is 12 flat buttons in one row. Most of them aren't
peers — they're parents and children mixed together. The fix: regroup
them into 5 or 6 top-level items, with dropdown menus for the children.

### New top-level structure

1. **Live Tracker** (toggle button) — renamed from "Watch Mode" for
   clarity. When ON, automatically opens both the Battle Window and the
   Threat Board. When OFF, both close. Has a small dropdown sub-menu to
   reopen either one individually if the user has dismissed it via the
   ✕ on the overlay.
2. **Open Log** (action) — unchanged, stays as its own button.
3. **Import** ▾ (dropdown) — contains "Import Log to DB", "Import All
   Logs", "Import History".
4. **Library** ▾ (dropdown) — contains "Characters", "The Great Hunt",
   "Encounter Data", "App Icon".
5. **Dev** ▾ (dropdown) — contains "Save Log As". For developer/debug
   features that aren't part of normal player workflow.
6. **Settings** (action, opens dialog) — see Settings dialog block below.

The strip stays drag-reorderable. Menus inside don't need reordering —
they're rarely-used child actions.

### What goes away

- "Battle Window" and "Threat Board" as top-level buttons. They're now
  managed by Live Tracker — auto-open with it, individually toggleable
  via the sub-menu.
- "App Icon" as a top-level button. Feature stays, moves into Library.

### Why this matters

The 12-button flat row treats everything as equally important. It isn't.
The user reaches for Open Log and Live Tracker all the time. They reach
for App Icon basically never. Grouping by frequency-of-use is the whole
point — the eye should find common actions instantly and rare ones
through menus.

### Scope honesty

This is medium-sized — bigger than the drag-reorder, smaller than Live
Combat Stream. Roughly:
- Rename "Watch Mode" to "Live Tracker" (one-line)
- Convert three flat buttons (Import, Library, Dev) to QPushButton-with-
  QMenu dropdowns (existing Qt pattern, ~30 lines per button)
- Wire Live Tracker to auto-open Battle Window and Threat Board
- Add the Live Tracker sub-menu with two toggleable items
- Remove the now-redundant top-level Battle Window and Threat Board
  buttons
- Build a Settings dialog (see below)

The toolbar restructure alone is probably 90 minutes. The Settings
dialog is a separate hour or two depending on scope.

## Settings dialog — first version

A real Settings feature, opened via the Settings button on the toolbar.
First-version content (from earlier in the conversation):

- **Live log folder** — where the game writes the active combat log
- **Old logs folder** — where past combat logs are archived
- **Server** — which SWTOR server the player is on (could be useful
  for filtering in Find-a-Fight and Cohort later)
- **Live mode on by default** — checkbox. If on, the app starts Live
  Tracker automatically at launch.

These all need persistence (settings.json already has the mechanism),
sensible defaults, and at minimum a "Browse..." button for the folder
paths.

### Open questions

- Server list — hard-coded list of current SWTOR servers, or free text?
  Hard-coded is safer (no typos break Cohort lookups later) but goes
  stale if Bioware renames or merges servers.
- "Live mode on by default" — does it auto-open Battle Window and
  Threat Board too, or just start Live Tracker silently?
- Where does the Settings dialog live in the codebase? Probably
  `ui/dialogs/settings.py` to match the existing pattern.

## Encounter Panel

### Current Purpose
- Shows the detected fights/encounters from the loaded log.
- Lets the user select which fight all right-side tabs should display.
- Includes rename support for encounters.

### Ideas
- When a file is loaded, scroll the encounter list to the top automatically.
- Simplify the encounter list by removing the visible timestamp, but keep the fight length shown. Show the timestamp in a hover tooltip instead.
- Color-code encounters so boss fights are visually different from trash fights.
- Add encounter filters to hide short fights, with options like under 10, 20, 30, or 40 seconds.
- Add a way to hide or separate very small log files, such as logs smaller than 500 KB.
- Add support for parsing and viewing non-combat logs, especially smaller logs that do not contain normal encounter data.


## Overview Tab

### Current Purpose
- High-level summary of the selected fight.
- Shows combatants, core totals, DPS/HPS, crits, and highlights.

### Ideas
- 


## Abilities Tab

### Current Purpose
- Breaks down damage/healing by ability for the selected entity.
- Compares shared abilities across players.

### Ideas
- 


## Mob Contributions Tab

### Current Purpose
- Lists mobs seen in the selected fight.
- Shows who contributed damage to each mob and by how much.

### Ideas
- 


## Raw Fight Log Tab

### Current Purpose
- Shows the raw lines for the selected encounter.
- Supports validation and Great Hunt access.

### Ideas
- 


## Rotation Tab

### Current Purpose
- Shows ability timeline/rotation for up to four entities.
- Highlights gaps and ability usage flow.

### Ideas
- 


## Compare Tab

### Current Purpose
- Compares two entities in the same encounter.
- Surfaces deltas, strengths, and weaknesses.

### Ideas
- 


## DPS Training Tab

### Current Purpose
- Gives damage-focused feedback and metrics.

### Ideas
- Make the Damage Type Breakdown section smaller or collapsible because it currently takes up too much space.
- Make the Coaching Tips section larger and easier to read so it feels more useful and prominent.


## Tank Training Tab

### Current Purpose
- Gives tank-focused feedback and metrics.

### Ideas
- Clarify the purpose of the Tank Training tab so the user understands what it is measuring and why it matters.
- Show the amount of threat the player had on each mob during the fight.
- Show which abilities generated threat and how much threat each one contributed.
- Show which interrupts were used and which enemy abilities or casts they interrupted.
- Show tank resource usage, such as Rage spent and gained for a Juggernaut or Warrior-style class.


## Healer Training Tab

### Current Purpose
- Gives healer-focused feedback and metrics.

### Ideas
- 


## Live Mode

### Current Purpose
- Watches the active combat log in real time.
- Builds encounters as they happen.

### Ideas
- See the **Live Combat Stream** design block earlier in this file —
  that's the main planned home for the in-app live experience. This
  section is for ideas specific to the watcher/log-tailing layer itself.


## Battle Window

### Current Purpose
- Always-on-top live combat overlay.
- Shows real-time DPS bars during combat.

### Ideas
- The Battle Window stays as a small glanceable overlay. The bigger,
  second-monitor experience belongs on the **Live Combat Stream** tab
  (designed earlier in this file). Don't try to make the Battle Window
  do both.


## Great Hunt

### Current Purpose
- Lets the user classify mobs and annotate fight/location details.

### Ideas
- Auto-fill the Great Hunt `Location`, `Zone`, and `Instance` fields from the combat log.
- Use log events such as `AreaEntered` to detect location changes as they happen.
- Update the detected location fields automatically when the player moves to a new area.
- Allow the user to override the detected values manually if the log is incomplete or incorrect.

### Bigger vision

Great Hunt is not a critical part of the app — honestly it is a separate
app that uses the same data. But the vision for it is much grander than
"classify mobs and annotate fights."

I want it to be a **diary** the player keeps and looks back on, and finds
a way to share with others. Roleplayers should be able to go crazy adding
detail to the fights.

The dream feature: the log gets auto-added to the fight, but not as raw
log lines — as a **book of what happened**. Narrative, not numbers.
Something readable. That needs an engine that looks at log events and
generates text from them.

This is a long-term idea. The current Great Hunt classification work is
the foundation; the diary/narrative layer sits on top of it later.
