# Re-export everything from parser.py under the name parser_core
# This lets main.py and aggregator.py both import from parser_core
from engine.parser import *
from engine.parser import (
    Entity, NamedThing, DamageResult, LogEvent,
    parse_entity, parse_named_thing, parse_effect_block,
    parse_result, parse_line, parse_file, _open_log,
    ENTITY_RE, NAMED_THING_RE, DAMAGE_RE,
    RESTORE_SPEND_RE, CHARGES_RE, LINE_RE,
)
