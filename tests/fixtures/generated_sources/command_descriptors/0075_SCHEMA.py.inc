SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "$defs": {"choice": {"type": "string", "enum": ["yes", "no"]}},
    "properties": {
        "count": {"type": "integer", "minimum": 1, "default": 2},
        "choice": {"$ref": "#/$defs/choice"},
        "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        "nested": {"type": "object", "properties": {"flag": {"type": "boolean"}},
                   "required": ["flag"], "additionalProperties": False},
    },
    "required": ["count", "choice"],
}


