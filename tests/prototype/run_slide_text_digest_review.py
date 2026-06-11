from __future__ import annotations

import json
import re
import time
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from docling_serve.powerpoint_courseware.pedagogy_provider import anthropic_text, extract_json


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "prototype" / "out"
GEOMETRY_PATH = OUT / "pptx-ooxml-geometry.json"
DIGEST_PATH = OUT / "slide-text-digest.json"
REVIEW_PATH = OUT / "haiku-slide-text-digest-review.json"
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION = "us-east-1"


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\t", " ")
    text = re.sub(r"[ \u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


def split_body_title(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[0], lines[1]
    if len(lines) == 1:
        return "", lines[0]
    return "", ""


def slide_title_lines(slide: dict[str, Any]) -> list[str]:
    lines = (((slide.get("slideFormat") or {}).get("titleStructure") or {}).get("lines")) or []
    return [clean_text(line) for line in lines if clean_text(line)]


def visible_text_elements(slide: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for element in slide.get("elements") or []:
        kind = str(element.get("kind") or "")
        element_type = str(element.get("type") or "")
        if kind.startswith("master_") or element_type == "image" or kind == "image":
            continue
        text = clean_text((element.get("text") or {}).get("plain"))
        if text:
            values.append(text)
    return values


def remove_title_prefix(text: str, title_values: list[str]) -> str:
    remaining = text.strip()
    ordered = sorted((title for title in title_values if title), key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for title in ordered:
            if remaining == title:
                return ""
            if remaining.startswith(title + "\n"):
                remaining = remaining[len(title) + 1 :].strip()
                changed = True
                break
    lines = [line for line in remaining.splitlines() if line.strip()]
    while lines and any(lines[0].strip() == title for title in ordered):
        lines.pop(0)
    return "\n".join(lines).strip()


def slide_digest(slide: dict[str, Any]) -> dict[str, Any]:
    title_lines = slide_title_lines(slide)
    text_values = visible_text_elements(slide)
    header = title_lines[0] if title_lines else ""
    sub_header = " - ".join(title_lines[1:]) if len(title_lines) > 1 else ""

    slide_title = clean_text(slide.get("title"))
    if not sub_header and slide_title and slide_title.upper() != header.upper():
        sub_header = slide_title

    if not sub_header:
        for text in text_values:
            body_header, body_sub_header = split_body_title(text)
            if body_header and clean_text(body_header).upper() == header.upper() and body_sub_header != header:
                sub_header = body_sub_header
                break
            if not header and body_sub_header and body_sub_header != header:
                sub_header = body_sub_header
                break

    title_values = ["\n".join(line for line in [header, sub_header] if line), header, sub_header]
    content_parts: list[str] = []
    for text in text_values:
        content = remove_title_prefix(text, title_values)
        if content and content not in {header, sub_header}:
            content_parts.append(content)

    return {
        "slide": int(slide.get("slideNumber") or slide.get("index") or 0) + 1
        if int(slide.get("slideNumber") or 0) == 0
        else int(slide.get("slideNumber")),
        "header": header,
        "subHeader": sub_header,
        "content": "\n\n".join(content_parts),
    }


def build_digest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [slide_digest(slide) for slide in manifest.get("slides") or []]


def expected_structure() -> list[dict[str, Any]]:
    return [
        {
            "module": 1,
            "title": "Course purpose, form purpose, and responsibilities",
            "slides": [1, 2, 3],
            "bloomLevel": "understand",
        },
        {
            "module": 2,
            "title": "AFTO Form 874 header, identification, and Part A common entries",
            "slides": [4, 5, 6, 7, 8, 9],
            "bloomLevel": "apply",
        },
        {
            "module": 3,
            "title": "Part B and Part C spare modification and kit requirements",
            "slides": [10, 11, 12, 13, 14],
            "bloomLevel": "apply",
        },
        {
            "module": 4,
            "title": "Part D through Part H disposition, spares, support, and supply records",
            "slides": [15, 16, 17, 18, 19, 20, 21],
            "bloomLevel": "analyze",
        },
        {
            "module": 5,
            "title": "Part I through Part K tools, remarks, certification, and validation practice",
            "slides": [22, 23, 24, 25, 26, 27],
            "bloomLevel": "apply",
        },
    ]


def prompt_for(digest: list[dict[str, Any]]) -> str:
    return (
        "You are reviewing a PowerPoint-derived training text digest. The only source "
        "data you may use is this JSON array of slides with header, subHeader, and "
        "content. Do not assume image content. Build a concise Bloom taxonomy and Air "
        "Force task/condition/standard instructional design review. Return ONLY valid "
        "JSON in this exact shape:\n"
        '{"recommendedModules":[{"id":"M1","title":str,"slideNumbers":[int],'
        '"bloomLevel":"remember|understand|apply|analyze|evaluate|create",'
        '"objective":str,"taskConditionStandard":{"task":str,"condition":str,'
        '"standard":str},"rationale":str}],"bloomProgression":[str],'
        '"courseGaps":[{"severity":"low|medium|high","gap":str,'
        '"recommendation":str,"slideNumbers":[int]}],"slideFeedback":['
        '{"slide":int,"bloomLevel":"remember|understand|apply|analyze|evaluate|create",'
        '"gaps":[str],"authoringNotes":[str]}],"confidence":number}\n'
        "Constraints: recommend 5 to 7 modules, keep module rationale under 20 words, "
        "limit each slideFeedback item to at most one gap and one authoring note, and "
        "prefer performance-based verbs over awareness verbs.\n\n"
        f"Slide text digest:\n{json.dumps(digest, ensure_ascii=True)}"
    )


def safe_model_slug(model_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model_id).strip("-")


def invoke_model(prompt: str, *, model_id: str, region: str) -> tuple[float, dict[str, Any], dict[str, Any]]:
    client = boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=120,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 5000,
        "temperature": 0,
        "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
    }
    started = time.time()
    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body).encode("utf-8"),
    )
    payload = json.loads(response["body"].read())
    return round(time.time() - started, 2), payload, extract_json(anthropic_text(payload))


def main() -> None:
    parser = ArgumentParser()
    parser.add_argument("--digest-only", action="store_true")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--review-path", type=Path)
    args = parser.parse_args()

    manifest = json.loads(GEOMETRY_PATH.read_text())
    digest = build_digest(manifest)
    DIGEST_PATH.write_text(json.dumps(digest, indent=2) + "\n")
    if args.digest_only:
        print(
            json.dumps(
                {
                    "digestStats": {
                        "slides": len(digest),
                        "characters": len(json.dumps(digest, ensure_ascii=True)),
                        "nonEmptyContentSlides": sum(1 for slide in digest if slide.get("content")),
                    },
                    "digestPath": str(DIGEST_PATH.relative_to(ROOT)),
                },
                indent=2,
            )
        )
        return

    prompt = prompt_for(digest)
    seconds, raw_payload, parsed = invoke_model(prompt, model_id=args.model_id, region=args.region)
    usage = raw_payload.get("usage") or {}
    review_path = args.review_path or (OUT / f"{safe_model_slug(args.model_id)}-slide-text-digest-review.json")
    if not review_path.is_absolute():
        review_path = ROOT / review_path
    result = {
        "modelId": args.model_id,
        "region": args.region,
        "seconds": seconds,
        "usage": usage,
        "digestStats": {
            "slides": len(digest),
            "characters": len(json.dumps(digest, ensure_ascii=True)),
            "nonEmptyContentSlides": sum(1 for slide in digest if slide.get("content")),
        },
        "expectedStructureForComparison": expected_structure(),
        "llmReview": parsed,
    }
    review_path.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "seconds": seconds,
                "usage": usage,
                "digestStats": result["digestStats"],
                "modules": len(parsed.get("recommendedModules") or []),
                "courseGaps": len(parsed.get("courseGaps") or []),
                "slideFeedback": len(parsed.get("slideFeedback") or []),
                "digestPath": str(DIGEST_PATH.relative_to(ROOT)),
                "reviewPath": str(review_path.relative_to(ROOT)),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
