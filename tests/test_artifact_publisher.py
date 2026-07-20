from __future__ import annotations

from pathlib import Path

from docling_serve.artifacts.publish import publish_dir_to_s3
from docling_serve.schematic.extract import (
    publish_dir_to_s3 as compatibility_publish_dir_to_s3,
)


class FakeClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str, dict[str, str]]] = []

    def upload_file(
        self,
        path: str,
        bucket: str,
        key: str,
        ExtraArgs: dict[str, str],
    ) -> None:
        self.uploads.append((path, bucket, key, ExtraArgs))


def test_neutral_publisher_preserves_keys_and_content_types(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "artifact.json").write_text("{}")
    client = FakeClient()
    keys = publish_dir_to_s3(
        tmp_path,
        bucket="bundle-bucket",
        prefix="/tenant/bundle/",
        client_factory=lambda: client,
    )
    assert keys == ["tenant/bundle/nested/artifact.json"]
    assert client.uploads[0][3] == {"ContentType": "application/json"}


def test_schematic_publication_import_is_a_compatibility_facade() -> None:
    assert compatibility_publish_dir_to_s3 is publish_dir_to_s3
