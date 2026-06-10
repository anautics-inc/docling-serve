from __future__ import annotations

import json
from importlib import resources
from typing import Any

import jsonschema

SCHEMA_PACKAGE = "docling_serve.powerpoint_courseware.schemas"


def load_schema(name: str) -> dict[str, Any]:
    return json.loads(resources.files(SCHEMA_PACKAGE).joinpath(name).read_text())


def validate_artifact(artifact: dict[str, Any], schema_name: str) -> None:
    jsonschema.validate(instance=artifact, schema=load_schema(schema_name))
