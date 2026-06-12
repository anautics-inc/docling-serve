from __future__ import annotations

import json
from importlib import resources
from typing import Any

import jsonschema

SCHEMA_PACKAGE = "docling_serve.deep_document.schemas"


def load_schema(name: str) -> dict[str, Any]:
    with (
        resources.files(SCHEMA_PACKAGE)
        .joinpath(name)
        .open("r", encoding="utf-8") as handle
    ):
        return json.load(handle)


def validate_artifact(artifact: dict[str, Any], schema_name: str) -> None:
    jsonschema.Draft202012Validator(load_schema(schema_name)).validate(artifact)
