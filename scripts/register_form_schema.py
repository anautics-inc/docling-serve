"""Register an XFA form as an ontology schema (one row per form, upsert by slug).

This is the "form template → ontology" registration step: it runs the XFA
extractor on a form PDF, maps every fillable field to a SchemaProperty (typed,
labeled, ordered, carrying its XFA write-back path), uploads the blank template
to the platform artifacts bucket, and upserts ONE row in ``captify_core_schemas``
keyed by the form's slug — so each form exists exactly once per tenant and the
workflow builder's Start-node ``schemaRef`` can seed run properties from it.

Usage::

    .venv/bin/python scripts/register_form_schema.py <form.pdf> \
        [--name "AFMC MP5327.9001 Market Research Report"] \
        [--tenant anautics] [--category af-form] [--env /path/.env] [--force]

Idempotent: an existing schema (same tenant + slug) is updated in place with a
``version`` bump when ``--force`` is given, otherwise the script reports and
exits. AWS credentials load from ``--env`` (default: repo ``.env``).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from docling_serve.extractors.xfa_extractor import (  # noqa: E402
    parse_dataset_values,
    parse_template_fields,
    read_xfa_packets,
)

DEFAULT_BUCKET = "captify-core"
TEMPLATE_PREFIX = "forms/templates/"

# XFA ui type -> SchemaProperty type (schema-designer canonical enum).
_TYPE_MAP = {
    "textEdit": "text",
    "numericEdit": "number",
    "checkButton": "boolean",
    "dateTimeEdit": "date",
    "choiceList": "select",
    "signature": "text",
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _camel(leaf: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", leaf)
    parts = [p for p in parts if p]
    if not parts:
        return "field"
    head, *tail = parts
    return head.lower() + "".join(p.capitalize() for p in tail)


def _load_env(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_path.is_file():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values.setdefault(key.strip(), val.strip())
    return values


def build_properties(template_fields: list[dict[str, Any]], dataset_paths: set[str]) -> list[dict[str, Any]]:
    """Map fillable XFA fields to SchemaProperty entries (with xfaPath binding).

    Only fields that exist as dataset leaves are fillable (draws/labels and
    layout-only widgets are skipped). Property names are camelCase and
    de-duplicated by section prefix when a bare leaf name collides (the AFMC
    form reuses ``TextField1`` in every narrative section).
    """
    used_names: set[str] = set()
    properties: list[dict[str, Any]] = []
    sort_order = 10

    # Caption fallback: in table layouts the field's caption is a separate
    # `draw` label in the preceding cell, so pair each caption-less field with
    # the nearest preceding label text in template (walk) order.
    last_label: str | None = None
    annotated: list[dict[str, Any]] = []
    for record in template_fields:
        if record["kind"] == "label":
            text = str(record.get("text") or "").strip()
            # Section headers ("Section A: …") are not field captions.
            if text and not re.match(r"(?i)^section\s+[a-z]\b", text):
                last_label = text
            continue
        if not (record.get("caption") or "").strip() and last_label:
            record = {**record, "caption": last_label}
        last_label = None
        annotated.append(record)

    # Template order is form order.
    for field in annotated:
        path = field["path"]
        # Match the template path to a dataset leaf (suffix match — template
        # paths carry repeating-row indices the datasets omit and vice versa).
        leaf_path = next(
            (dp for dp in dataset_paths if dp == path or path.endswith(dp) or dp.endswith(path.split(".", 1)[-1])),
            None,
        )
        if leaf_path is None:
            continue
        if any(p.get("xfaPath") == leaf_path for p in properties):
            continue

        leaf = leaf_path.split(".")[-1]
        name = _camel(leaf)
        if name in used_names:
            section = field.get("section") or leaf_path.split(".")[-2:][0]
            name = _camel(f"{section}_{leaf}")
        suffix = 2
        base = name
        while name in used_names:
            name = f"{base}{suffix}"
            suffix += 1
        used_names.add(name)

        caption = (field.get("caption") or "").strip().rstrip(":")
        label = caption or (field.get("section") or "") + " " + leaf
        prop: dict[str, Any] = {
            "id": f"prop-{leaf_path}",
            "name": name,
            "label": label.strip(),
            "type": _TYPE_MAP.get(str(field.get("uiType")), "textarea"),
            "required": False,
            "dataSource": "manual-input",
            "sortOrder": sort_order,
            # Non-canonical, owned by the form pipeline: where fillForm writes
            # this property's value inside the XFA datasets packet.
            "xfaPath": leaf_path,
            "xfaSection": field.get("section"),
        }
        options = field.get("options") or []
        if options:
            prop["type"] = "select"
            prop["staticListValues"] = [{"key": _slug(o), "label": o} for o in options]
        properties.append(prop)
        sort_order += 10

    return properties


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--name", default=None, help="Schema name (default: PDF stem)")
    parser.add_argument("--tenant", default="anautics")
    parser.add_argument("--category", default="af-form")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument("--force", action="store_true", help="Update an existing schema row")
    args = parser.parse_args()

    import boto3

    env = _load_env(args.env)
    session = boto3.Session(
        region_name=env.get("AWS_REGION", "us-east-1"),
        aws_access_key_id=env.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=env.get("AWS_SECRET_ACCESS_KEY"),
    )

    name = args.name or args.pdf.stem
    slug = _slug(name)

    packets = read_xfa_packets(args.pdf)
    if "template" not in packets:
        print(f"ERROR: {args.pdf.name} has no XFA template packet — not a dynamic form.")
        return 1
    template_fields = parse_template_fields(packets["template"])
    dataset_paths = set(parse_dataset_values(packets["datasets"]).keys()) if packets.get("datasets") else set()
    # parse_dataset_values only returns non-empty leaves; index all leaves instead.
    import xml.etree.ElementTree as ET

    def all_leaf_paths(datasets_xml: bytes) -> set[str]:
        root = ET.fromstring(datasets_xml)
        data = next((c for c in root if c.tag.rsplit('}', 1)[-1] == "data"), root)
        xhtml = "{http://www.w3.org/1999/xhtml}"
        out: set[str] = set()

        def descend(el: ET.Element, path: list[str]) -> None:
            children = list(el)
            rich = any(c.tag.startswith(xhtml) for c in children)
            if not children or rich:
                out.add(".".join(path))
                return
            for c in children:
                descend(c, [*path, c.tag.rsplit('}', 1)[-1]])

        for c in data:
            descend(c, [c.tag.rsplit('}', 1)[-1]])
        return out

    if packets.get("datasets"):
        dataset_paths = all_leaf_paths(packets["datasets"])

    properties = build_properties(template_fields, dataset_paths)
    if not properties:
        print("ERROR: no fillable fields resolved — refusing to register an empty form schema.")
        return 1

    # Upload the blank template to the store the fill tool reads.
    template_key = f"{TEMPLATE_PREFIX}{slug}.pdf"
    s3 = session.client("s3")
    s3.put_object(
        Bucket=args.bucket,
        Key=template_key,
        Body=args.pdf.read_bytes(),
        ContentType="application/pdf",
    )

    ddb = session.resource("dynamodb")
    table = ddb.Table("captify_core_schemas")
    pk = f"TENANT#{args.tenant}"

    # One row per form: find an existing row by (tenant, slug or name).
    existing = None
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "PK = :pk AND (slug = :slug OR #n = :name)",
        "ExpressionAttributeValues": {":pk": pk, ":slug": slug, ":name": name},
        "ExpressionAttributeNames": {"#n": "name"},
    }
    while True:
        page = table.scan(**scan_kwargs)
        items = page.get("Items", [])
        if items:
            existing = items[0]
            break
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last

    now = Decimal(str(int(time.time() * 1000)))
    form_template = {
        "format": "xfa",
        "s3Uri": f"s3://{args.bucket}/{template_key}",
        "sourceFileName": args.pdf.name,
    }

    if existing is not None and not args.force:
        print(f"EXISTS: schema '{existing.get('name')}' (id={existing.get('id')}, version={existing.get('version')}).")
        print("Re-run with --force to update it in place.")
        return 0

    if existing is not None:
        schema_id = existing["id"]
        version = int(existing.get("version", 1)) + 1
        created_at = existing.get("createdAt", now)
        created_by = existing.get("createdById", "form-registrar")
    else:
        schema_id = str(uuid.uuid4())
        version = 1
        created_at = now
        created_by = "form-registrar"

    table.put_item(
        Item={
            "PK": pk,
            "SK": f"METADATA#SCHEMA#{schema_id}",
            "id": schema_id,
            "tenantId": args.tenant,
            "name": name,
            "slug": slug,
            "description": f"AF/DoD form (XFA) registered from {args.pdf.name}; "
            f"{len(properties)} fillable fields. Fill via the fillForm agent tool.",
            "category": args.category,
            "version": version,
            "implementsInterfaces": [],
            "parentSchemaId": None,
            "properties": properties,
            "propertyOverrides": {},
            "validationRules": [],
            "formTemplate": form_template,
            "createdAt": created_at,
            "updatedAt": now,
            "createdById": created_by,
        }
    )
    action = "UPDATED" if existing is not None else "CREATED"
    print(f"{action}: schema '{name}' slug={slug} id={schema_id} version={version}")
    print(f"  fields: {len(properties)} | template: {form_template['s3Uri']}")
    sections = sorted({str(p.get('xfaSection')) for p in properties if p.get('xfaSection')})
    print(f"  sections: {', '.join(sections)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
