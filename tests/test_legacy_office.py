from __future__ import annotations

import signal
import socket
import subprocess
from io import BytesIO
from pathlib import Path, PurePath
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from docling.datamodel.base_models import DocumentStream, FailureCategory
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.responses import FailurePhase
from docling_jobkit.convert.manager import (
    DoclingConverterManager as BaseDoclingConverterManager,
)

from docling_serve.legacy_office import (
    LEGACY_OFFICE_TARGETS,
    LegacyHttpFetchResult,
    LegacyOfficeCapabilityError,
    LegacyOfficeConversionError,
    LegacyOfficeDoclingConverterManager,
    LegacyOfficeInputLimitError,
    LegacyOfficeMissingOutputError,
    LegacyOfficeOutputLimitError,
    LegacyOfficeProcessSurvivedError,
    LegacyOfficeScratchLimitError,
    LegacyOfficeSourceFetchError,
    LegacyOfficeSourcePolicyError,
    LegacyOfficeTimeoutError,
    LibreOfficeHeadlessConverter,
    PinnedHttpResponse,
    ResolvedGlobalAddress,
    _chunk_source_metadata,
    _path_tree_size,
    _request_pinned,
    _resolve_global_addresses,
    _terminate_and_reap,
    _validate_shared_url_headers,
    build_legacy_office_public_task_error,
    classify_legacy_office_failure,
    fetch_legacy_http_source,
    preconvert_legacy_office_sources,
)
from docling_serve.settings import DoclingServeSettings


class _FakeLegacyOfficeConverter:
    def __init__(self, payload: bytes = b"converted:") -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def convert(
        self,
        source: Path,
        output_dir: Path,
        *,
        target_suffix: str,
        timeout_seconds: float,
        max_output_bytes: int,
        max_scratch_bytes: int,
        max_file_count: int,
    ) -> Path:
        target = output_dir / f"converted{target_suffix}"
        target.write_bytes(self.payload + source.read_bytes())
        self.calls.append(
            {
                "source": source,
                "output_dir": output_dir,
                "target_suffix": target_suffix,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
                "max_scratch_bytes": max_scratch_bytes,
                "max_file_count": max_file_count,
                "target": target,
                "source_bytes": source.read_bytes(),
            }
        )
        return target


def _prepare(sources, converter, tmp_path, **overrides):
    kwargs = {
        "scratch_dir": tmp_path,
        "timeout_seconds": 17,
        "max_input_bytes": 100,
        "max_output_bytes": 200,
        "max_scratch_bytes": 300,
        "max_file_count": 20,
        "fetch_timeout_seconds": 2,
        "max_redirects": 3,
    }
    kwargs.update(overrides)
    return preconvert_legacy_office_sources(
        sources,
        converter=converter,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("source_name", "target_name"),
    [
        ("report.doc", "report.docx"),
        ("deck.PPT", "deck.pptx"),
        ("workbook.xls", "workbook.xlsx"),
    ],
)
def test_all_legacy_formats_convert_with_clean_stems_and_cleanup(
    tmp_path, source_name, target_name
):
    converter = _FakeLegacyOfficeConverter()
    original = DocumentStream(name=source_name, stream=BytesIO(b"legacy-office"))

    with _prepare([original], converter, tmp_path) as prepared:
        converted = prepared.sources[0]
        assert isinstance(converted, DocumentStream)
        assert converted.name == target_name
        assert converted.stream.read() == b"converted:legacy-office"
        identity = prepared.identities[0]
        assert identity.original_name == source_name
        assert identity.original_mime_type
        call = converter.calls[0]
        assert call["source"].parent != call["output_dir"]
        assert call["max_scratch_bytes"] == 300
        assert call["target"].exists()

    assert not call["source"].exists()
    assert not call["output_dir"].exists()
    assert list(tmp_path.iterdir()) == []
    assert set(LEGACY_OFFICE_TARGETS) == {".doc", ".ppt", ".xls"}


def test_modern_source_is_unchanged(tmp_path):
    converter = _FakeLegacyOfficeConverter()
    source = DocumentStream(name="modern.docx", stream=BytesIO(b"modern"))
    with _prepare([source], converter, tmp_path) as prepared:
        assert prepared.sources == [source]
        assert prepared.identities == {}
    assert converter.calls == []


def test_input_is_read_with_bounded_overflow_check(tmp_path):
    converter = _FakeLegacyOfficeConverter()
    source = DocumentStream(name="large.xls", stream=BytesIO(b"12345"))
    with pytest.raises(LegacyOfficeInputLimitError):
        with _prepare(
            [source], converter, tmp_path, max_input_bytes=4
        ):  # enter performs work
            pass
    assert converter.calls == []
    assert list(tmp_path.iterdir()) == []


def test_http_legacy_source_uses_safe_bounded_materializer(tmp_path):
    converter = _FakeLegacyOfficeConverter()

    async def _fetch(
        source,
        *,
        headers,
        max_file_size,
        timeout_seconds,
        max_redirects,
    ):
        assert source == "https://example.test/files/report.doc?download=1"
        assert headers == {"Authorization": "opaque"}
        assert max_file_size == 100
        assert timeout_seconds == 2
        assert max_redirects == 3
        return LegacyHttpFetchResult(
            payload=b"downloaded",
            final_url=source,
            content_type="application/octet-stream",
        )

    with patch("docling_serve.legacy_office.fetch_legacy_http_source", _fetch):
        with _prepare(
            ["https://example.test/files/report.doc?download=1"],
            converter,
            tmp_path,
            headers={"Authorization": "opaque"},
        ) as prepared:
            assert prepared.sources[0].name == "report.docx"
            assert (
                prepared.identities[0].source_uri
                == "https://example.test/files/report.doc?download=1"
            )
            assert (
                prepared.identities[0].original_mime_type == "application/octet-stream"
            )
    assert converter.calls[0]["source_bytes"] == b"downloaded"


def test_every_non_global_address_is_rejected():
    for address in (
        "127.0.0.1",
        "10.0.0.4",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.2.1",
        "::1",
        "2001:db8::1",
    ):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (
            (address, 443, 0, 0)
            if family == socket.AF_INET6
            else (
                address,
                443,
            )
        )
        with (
            patch(
                "docling_serve.legacy_office.socket.getaddrinfo",
                return_value=[(family, socket.SOCK_STREAM, 6, "", sockaddr)],
            ),
            pytest.raises(LegacyOfficeSourcePolicyError),
        ):
            _resolve_global_addresses("unsafe.test", 443)


@pytest.mark.asyncio
async def test_fetch_connects_to_resolver_result_not_system_dns(monkeypatch):
    pinned = ResolvedGlobalAddress(
        family=socket.AF_INET,
        socktype=socket.SOCK_STREAM,
        proto=6,
        sockaddr=("93.184.216.34", 443),
        ip="93.184.216.34",
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        MagicMock(return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]),
    )
    seen = []

    def connector(url, *, address, headers, timeout_seconds, max_file_size):
        seen.append((url, address, headers, timeout_seconds, max_file_size))
        return PinnedHttpResponse(
            status=200,
            headers={"content-type": "application/octet-stream"},
            payload=b"legacy",
        )

    result = await fetch_legacy_http_source(
        "https://public.test/report.doc",
        headers=None,
        max_file_size=100,
        timeout_seconds=1,
        max_redirects=0,
        resolver=lambda host, port: (pinned,),
        connector=connector,
    )
    assert result.payload == b"legacy"
    assert seen[0][1] is pinned
    socket.getaddrinfo.assert_not_called()


@pytest.mark.asyncio
async def test_redirects_resolve_once_per_hop_and_strip_all_custom_headers():
    calls: list[tuple[str, tuple[str, ...]]] = []

    def resolver(host, port):
        family = socket.AF_INET6 if host == "v6.test" else socket.AF_INET
        ip = "2606:4700:4700::1111" if family == socket.AF_INET6 else "1.1.1.1"
        sockaddr = (ip, port, 0, 0) if family == socket.AF_INET6 else (ip, port)
        return (
            ResolvedGlobalAddress(
                family=family,
                socktype=socket.SOCK_STREAM,
                proto=6,
                sockaddr=sockaddr,
                ip=ip,
            ),
        )

    def connector(url, *, address, headers, **kwargs):
        del kwargs
        calls.append((address.ip, tuple(sorted(headers))))
        if "start.doc" in url:
            return PinnedHttpResponse(
                status=302,
                headers={"location": "https://v6.test/final.doc"},
                payload=b"",
            )
        return PinnedHttpResponse(
            status=200,
            headers={"content-type": "application/octet-stream"},
            payload=b"legacy",
        )

    await fetch_legacy_http_source(
        "https://public.test/start.doc",
        headers={"X-API-Key": "secret", "X-Arbitrary-Secret": "hidden"},
        max_file_size=100,
        timeout_seconds=1,
        max_redirects=2,
        resolver=resolver,
        connector=connector,
    )
    assert calls == [
        ("1.1.1.1", ("X-API-Key", "X-Arbitrary-Secret")),
        ("2606:4700:4700::1111", ()),
    ]


@pytest.mark.asyncio
async def test_same_origin_redirect_keeps_headers_and_private_redirect_is_blocked():
    address = ResolvedGlobalAddress(
        family=socket.AF_INET,
        socktype=socket.SOCK_STREAM,
        proto=6,
        sockaddr=("1.1.1.1", 443),
        ip="1.1.1.1",
    )
    seen_headers = []

    def connector(url, *, headers, **kwargs):
        del kwargs
        seen_headers.append(headers)
        if url.endswith("/start.doc"):
            return PinnedHttpResponse(
                status=302,
                headers={"location": "/final.doc"},
                payload=b"",
            )
        return PinnedHttpResponse(status=200, headers={}, payload=b"legacy")

    await fetch_legacy_http_source(
        "https://public.test/start.doc",
        headers={"Authorization": "secret"},
        max_file_size=100,
        timeout_seconds=1,
        max_redirects=2,
        resolver=lambda host, port: (address,),
        connector=connector,
    )
    assert seen_headers == [
        {"Authorization": "secret"},
        {"Authorization": "secret"},
    ]

    def private_redirect_connector(url, **kwargs):
        del kwargs
        return PinnedHttpResponse(
            status=302,
            headers={"location": "http://127.0.0.1/private.doc"},
            payload=b"",
        )

    with pytest.raises(LegacyOfficeSourcePolicyError):
        await fetch_legacy_http_source(
            "https://public.test/start.doc",
            headers=None,
            max_file_size=100,
            timeout_seconds=1,
            max_redirects=2,
            resolver=(
                lambda host, port: (
                    _resolve_global_addresses(host, port)
                    if host == "127.0.0.1"
                    else (address,)
                )
            ),
            connector=private_redirect_connector,
        )


def test_mixed_origin_any_shared_headers_fail_closed():
    with pytest.raises(LegacyOfficeSourcePolicyError, match="exact same origin"):
        _validate_shared_url_headers(
            [
                "https://one.test/report.doc",
                "https://two.test/book.xls",
            ],
            {"X-Arbitrary-Secret": "secret"},
        )
    _validate_shared_url_headers(
        [
            "https://one.test/report.doc",
            "https://one.test/book.xls",
        ],
        {"X-API-Key": "secret"},
    )


def test_pinned_https_preserves_tls_hostname_and_exact_ip():
    raw_socket = MagicMock()
    tls_socket = MagicMock()
    ssl_context = MagicMock()
    ssl_context.wrap_socket.return_value = tls_socket
    response = MagicMock()
    response.status = 200
    response.getheaders.return_value = [
        ("Content-Type", "application/octet-stream"),
        ("Content-Length", "6"),
    ]
    response.read.side_effect = [b"legacy", b""]
    address = ResolvedGlobalAddress(
        family=socket.AF_INET6,
        socktype=socket.SOCK_STREAM,
        proto=6,
        sockaddr=("2606:4700:4700::1111", 8443, 0, 0),
        ip="2606:4700:4700::1111",
    )
    with patch(
        "docling_serve.legacy_office.http.client.HTTPResponse", return_value=response
    ):
        result = _request_pinned(
            "https://public.test:8443/report.doc",
            address=address,
            headers={},
            timeout_seconds=2,
            max_file_size=100,
            ssl_context_factory=lambda: ssl_context,
            socket_factory=MagicMock(return_value=raw_socket),
        )
    raw_socket.connect.assert_called_once_with(address.sockaddr)
    ssl_context.wrap_socket.assert_called_once_with(
        raw_socket, server_hostname="public.test"
    )
    sent = tls_socket.sendall.call_args.args[0]
    assert b"Host: public.test:8443\r\n" in sent
    assert result.payload == b"legacy"


def test_pinned_connector_enforces_declared_size_and_transport_timeout():
    address = ResolvedGlobalAddress(
        family=socket.AF_INET,
        socktype=socket.SOCK_STREAM,
        proto=6,
        sockaddr=("1.1.1.1", 80),
        ip="1.1.1.1",
    )
    response = MagicMock()
    response.status = 200
    response.getheaders.return_value = [("Content-Length", "5")]
    raw_socket = MagicMock()
    with (
        patch(
            "docling_serve.legacy_office.http.client.HTTPResponse",
            return_value=response,
        ),
        pytest.raises(LegacyOfficeInputLimitError),
    ):
        _request_pinned(
            "http://public.test/report.doc",
            address=address,
            headers={},
            timeout_seconds=1,
            max_file_size=4,
            socket_factory=MagicMock(return_value=raw_socket),
        )

    timed_out_socket = MagicMock()
    timed_out_socket.connect.side_effect = TimeoutError("timed out")
    with pytest.raises(LegacyOfficeSourceFetchError):
        _request_pinned(
            "http://public.test/report.doc",
            address=address,
            headers={},
            timeout_seconds=1,
            max_file_size=100,
            socket_factory=MagicMock(return_value=timed_out_socket),
        )


def test_path_symlinks_are_rejected(tmp_path):
    target = tmp_path / "target.doc"
    target.write_bytes(b"legacy")
    symlink = tmp_path / "link.doc"
    symlink.symlink_to(target)
    with pytest.raises(LegacyOfficeConversionError, match="non-symlink"):
        with _prepare([symlink], _FakeLegacyOfficeConverter(), tmp_path / "scratch"):
            pass


def test_converter_output_symlink_and_traversal_are_rejected(tmp_path):
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside")

    class UnsafeConverter(_FakeLegacyOfficeConverter):
        def convert(self, source, output_dir, **kwargs):
            link = output_dir / "converted.docx"
            link.symlink_to(outside)
            return link

    source = DocumentStream(name="report.doc", stream=BytesIO(b"legacy"))
    with pytest.raises(LegacyOfficeConversionError, match="symlink"):
        with _prepare([source], UnsafeConverter(), tmp_path / "scratch"):
            pass

    class TraversalConverter(_FakeLegacyOfficeConverter):
        def convert(self, source, output_dir, **kwargs):
            return outside

    with pytest.raises(LegacyOfficeConversionError, match="contained"):
        with _prepare([source], TraversalConverter(), tmp_path / "scratch2"):
            pass


def test_converter_output_read_is_bounded(tmp_path):
    source = DocumentStream(name="report.doc", stream=BytesIO(b"x"))
    converter = _FakeLegacyOfficeConverter(payload=b"12345")
    with pytest.raises(LegacyOfficeOutputLimitError):
        with _prepare([source], converter, tmp_path, max_output_bytes=4):
            pass


def test_unavailable_and_explicit_executable_validation(tmp_path):
    with patch("docling_serve.legacy_office.shutil.which", return_value=None):
        with pytest.raises(LegacyOfficeCapabilityError):
            LibreOfficeHeadlessConverter().check_capability()

    with pytest.raises(ValidationError, match="absolute path"):
        DoclingServeSettings(legacy_office_executable=Path("relative/soffice"))

    configured = tmp_path / "soffice"
    configured.write_text("#!/bin/sh\n")
    configured.chmod(0o600)
    with (
        patch(
            "docling_serve.legacy_office.APPROVED_SYSTEM_EXECUTABLE_ROOTS",
            (tmp_path,),
        ),
        pytest.raises(LegacyOfficeCapabilityError, match="regular executable"),
    ):
        LibreOfficeHeadlessConverter(configured).check_capability()


def test_discovered_and_configured_launcher_symlinks_resolve_safely(tmp_path):
    target = tmp_path / "program" / "soffice.bin"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\n")
    target.chmod(0o700)
    launcher = tmp_path / "soffice"
    launcher.symlink_to(target)

    with (
        patch(
            "docling_serve.legacy_office.APPROVED_SYSTEM_EXECUTABLE_ROOTS",
            (tmp_path,),
        ),
        patch("docling_serve.legacy_office.shutil.which", return_value=str(launcher)),
    ):
        assert LibreOfficeHeadlessConverter().resolve_executable() == target
        assert LibreOfficeHeadlessConverter(launcher).resolve_executable() == target


def test_broken_launcher_symlink_is_rejected(tmp_path):
    launcher = tmp_path / "soffice"
    launcher.symlink_to(tmp_path / "missing")
    with (
        patch(
            "docling_serve.legacy_office.APPROVED_SYSTEM_EXECUTABLE_ROOTS",
            (tmp_path,),
        ),
        pytest.raises(LegacyOfficeCapabilityError, match="broken link"),
    ):
        LibreOfficeHeadlessConverter(launcher).resolve_executable()


class _FakeProcess:
    def __init__(self, polls: list[int | None], *, returncode: int = 0):
        self.pid = 4321
        self._polls = polls
        self.returncode: int | None = None
        self.final_returncode = returncode
        self.waited = False
        self.killed = False

    def poll(self):
        value = self._polls.pop(0) if self._polls else self.returncode
        if value is not None:
            self.returncode = self.final_returncode if value == 0 else value
        return self.returncode

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _run_real_converter_boundary(tmp_path, process, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.doc"
    source.write_bytes(b"legacy")
    output = tmp_path / "output"
    output.mkdir()
    converter = LibreOfficeHeadlessConverter(Path("/usr/bin/true"), poll_interval=0)
    defaults = {
        "target_suffix": ".docx",
        "timeout_seconds": 10,
        "max_output_bytes": 100,
        "max_scratch_bytes": 100,
        "max_file_count": 20,
    }
    defaults.update(kwargs)
    return converter, source, output, defaults


def test_timeout_terminates_and_reaps_process_group(tmp_path):
    process = _FakeProcess([None, None])
    converter, source, output, kwargs = _run_real_converter_boundary(
        tmp_path, process, timeout_seconds=1
    )
    with (
        patch(
            "docling_serve.legacy_office.subprocess.Popen", return_value=process
        ) as popen,
        patch("docling_serve.legacy_office.time.monotonic", side_effect=[0, 2]),
        patch("docling_serve.legacy_office.os.killpg") as killpg,
        pytest.raises(LegacyOfficeTimeoutError),
    ):
        converter.convert(source, output, **kwargs)
    killpg.assert_called_once_with(process.pid, signal.SIGTERM)
    assert process.waited
    command = popen.call_args.args[0]
    assert Path(command[0]).name == "prlimit"
    assert "--fsize=100:100" in command
    assert "--" in command
    assert command[command.index("--") + 1] == "/usr/bin/true"


def test_surviving_process_is_reported_after_escalation():
    process = MagicMock()
    process.pid = 9876
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("soffice", 1)
    with (
        patch("docling_serve.legacy_office.os.killpg") as killpg,
        pytest.raises(LegacyOfficeProcessSurvivedError),
    ):
        _terminate_and_reap(process)
    assert [call.args[1] for call in killpg.call_args_list] == [
        signal.SIGTERM,
        signal.SIGKILL,
    ]


def test_surviving_process_fatally_stops_worker_and_wins_over_original(tmp_path):
    process = _FakeProcess([None, None])
    converter, source, output, kwargs = _run_real_converter_boundary(tmp_path, process)
    fatal = MagicMock()
    converter._fatal_worker_terminator = fatal
    survivor = LegacyOfficeProcessSurvivedError("alive")
    with (
        patch("docling_serve.legacy_office.subprocess.Popen", return_value=process),
        patch(
            "docling_serve.legacy_office._check_converter_growth",
            side_effect=KeyboardInterrupt("cancelled"),
        ),
        patch(
            "docling_serve.legacy_office._terminate_and_reap",
            side_effect=survivor,
        ),
        pytest.raises(LegacyOfficeProcessSurvivedError),
    ):
        converter.convert(source, output, **kwargs)
    fatal.assert_called_once_with(survivor)


def test_cancellation_terminates_and_reaps_without_masking(tmp_path):
    process = _FakeProcess([None, None])
    converter, source, output, kwargs = _run_real_converter_boundary(tmp_path, process)
    with (
        patch("docling_serve.legacy_office.subprocess.Popen", return_value=process),
        patch(
            "docling_serve.legacy_office._path_tree_size",
            side_effect=KeyboardInterrupt,
        ),
        patch("docling_serve.legacy_office.os.killpg") as killpg,
        pytest.raises(KeyboardInterrupt),
    ):
        converter.convert(source, output, **kwargs)
    killpg.assert_called_once()
    assert process.waited


def test_scratch_growth_terminates_process(tmp_path):
    process = _FakeProcess([None, None])
    converter, source, output, kwargs = _run_real_converter_boundary(
        tmp_path, process, max_scratch_bytes=4
    )
    (output / "growth").write_bytes(b"12345")
    with (
        patch("docling_serve.legacy_office.subprocess.Popen", return_value=process),
        patch("docling_serve.legacy_office.os.killpg"),
        pytest.raises(LegacyOfficeScratchLimitError),
    ):
        converter.convert(source, output, **kwargs)
    assert process.waited


def test_scratch_size_tolerates_libreoffice_temp_directory_disappearing(
    tmp_path, monkeypatch
):
    transient = tmp_path / "lu-temp"
    transient.mkdir()
    (transient / "content").write_bytes(b"temporary")
    stable = tmp_path / "stable"
    stable.write_bytes(b"1234")
    real_scandir = __import__("os").scandir

    def disappearing_scandir(path):
        if Path(path) == transient and transient.exists():
            (transient / "content").unlink()
            transient.rmdir()
        return real_scandir(path)

    monkeypatch.setattr("docling_serve.legacy_office.os.scandir", disappearing_scandir)

    assert _path_tree_size(tmp_path) == stable.stat().st_size


def test_output_growth_terminates_process_before_completion(tmp_path):
    process = _FakeProcess([None, None])
    converter, source, output, kwargs = _run_real_converter_boundary(
        tmp_path, process, max_output_bytes=4
    )
    (output / "source.docx").write_bytes(b"12345")
    with (
        patch("docling_serve.legacy_office.subprocess.Popen", return_value=process),
        patch("docling_serve.legacy_office.os.killpg"),
        pytest.raises(LegacyOfficeOutputLimitError),
    ):
        converter.convert(source, output, **kwargs)
    assert process.waited


def test_converter_file_count_limit_terminates_process(tmp_path):
    process = _FakeProcess([None, None])
    converter, source, output, kwargs = _run_real_converter_boundary(
        tmp_path, process, max_file_count=1
    )
    (output / "one.tmp").write_bytes(b"1")
    (output / "two.tmp").write_bytes(b"2")
    with (
        patch("docling_serve.legacy_office.subprocess.Popen", return_value=process),
        patch("docling_serve.legacy_office.os.killpg"),
        pytest.raises(LegacyOfficeScratchLimitError, match="files"),
    ):
        converter.convert(source, output, **kwargs)
    assert process.waited


def test_nonzero_and_missing_output_are_distinct_typed_failures(tmp_path):
    nonzero = _FakeProcess([7], returncode=7)
    converter, source, output, kwargs = _run_real_converter_boundary(tmp_path, nonzero)
    with (
        patch("docling_serve.legacy_office.subprocess.Popen", return_value=nonzero),
        pytest.raises(LegacyOfficeConversionError, match="exit code 7"),
    ):
        converter.convert(source, output, **kwargs)

    missing = _FakeProcess([0])
    converter, source, output, kwargs = _run_real_converter_boundary(
        tmp_path / "missing", missing
    )
    with (
        patch("docling_serve.legacy_office.subprocess.Popen", return_value=missing),
        pytest.raises(LegacyOfficeMissingOutputError),
    ):
        converter.convert(source, output, **kwargs)


@pytest.mark.parametrize(
    ("error", "category", "retryable", "code"),
    [
        (
            LegacyOfficeCapabilityError(),
            FailureCategory.BACKEND_FAILURE,
            False,
            "legacy_office_capability_unavailable",
        ),
        (
            LegacyOfficeConversionError(),
            FailureCategory.BACKEND_FAILURE,
            False,
            "legacy_office_conversion_failed",
        ),
        (
            LegacyOfficeMissingOutputError(),
            FailureCategory.BACKEND_FAILURE,
            False,
            "legacy_office_missing_output",
        ),
        (
            LegacyOfficeInputLimitError(),
            FailureCategory.POLICY,
            False,
            "legacy_office_input_limit_exceeded",
        ),
        (
            LegacyOfficeOutputLimitError(),
            FailureCategory.POLICY,
            False,
            "legacy_office_output_limit_exceeded",
        ),
        (
            LegacyOfficeTimeoutError(),
            FailureCategory.TIMEOUT,
            True,
            "legacy_office_timeout",
        ),
    ],
)
def test_public_failure_mapping_is_stable(error, category, retryable, code):
    failure = classify_legacy_office_failure(error, task_id="task")
    assert failure.category == category
    assert failure.retryable is retryable
    assert failure.phase == FailurePhase.EXECUTION
    assert failure.details["code"] == code
    assert build_legacy_office_public_task_error(error) == error.public_message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        LegacyOfficeCapabilityError(),
        LegacyOfficeConversionError(),
        LegacyOfficeInputLimitError(),
        LegacyOfficeScratchLimitError(),
        LegacyOfficeSourcePolicyError(),
    ],
)
async def test_ray_does_not_retry_nonretryable_failures(error):
    from docling_serve.ray_legacy import LegacyOfficeRayConverterDeployment

    replica_class = LegacyOfficeRayConverterDeployment.func_or_class
    attempts = 0

    def fail():
        nonlocal attempts
        attempts += 1
        raise error

    replica = replica_class.__new__(replica_class)
    replica.config = SimpleNamespace(max_task_retries=3, retry_delay=0)
    task = SimpleNamespace(
        task_id="ray-task",
        sources=[object()],
        target=SimpleNamespace(kind="inbody"),
    )
    result = await replica_class._run_with_retry(
        replica,
        "ray-task",
        fail,
        task=task,
    )
    assert attempts == 1
    assert result.failure.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        LegacyOfficeTimeoutError(),
        LegacyOfficeSourceFetchError("transient"),
    ],
)
async def test_ray_retries_retryable_timeout_and_fetch_failures(error):
    from docling_serve.ray_legacy import LegacyOfficeRayConverterDeployment

    replica_class = LegacyOfficeRayConverterDeployment.func_or_class
    attempts = 0

    def fail():
        nonlocal attempts
        attempts += 1
        raise error

    replica = replica_class.__new__(replica_class)
    replica.config = SimpleNamespace(max_task_retries=2, retry_delay=0)
    task = SimpleNamespace(
        task_id="ray-task",
        sources=[object()],
        target=SimpleNamespace(kind="inbody"),
    )
    result = await replica_class._run_with_retry(
        replica,
        "ray-task",
        fail,
        task=task,
    )
    assert attempts == 3
    assert result.failure.retryable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("ordinary conversion failure"),
        MemoryError("capacity"),
    ],
)
async def test_ray_nonlegacy_failures_delegate_to_untouched_upstream(error):
    from docling_serve.ray_legacy import (
        LegacyOfficeRayConverterDeployment,
        _BaseConverterReplica,
    )

    replica_class = LegacyOfficeRayConverterDeployment.func_or_class
    replica = replica_class.__new__(replica_class)
    replica.config = SimpleNamespace(max_task_retries=2, retry_delay=0)

    def fail():
        raise error

    with patch.object(
        _BaseConverterReplica,
        "_run_with_retry",
        return_value="upstream-result",
    ) as upstream_retry:
        result = await replica_class._run_with_retry(
            replica,
            "nonlegacy",
            fail,
            task=None,
        )
    assert result == "upstream-result"
    upstream_retry.assert_called_once()


def test_scoped_adapters_leave_jobkit_modules_untouched():
    import docling_jobkit.convert.manager as manager_module
    import docling_jobkit.orchestrators.local.worker as local_worker
    import docling_jobkit.orchestrators.ray.failure_classification as ray_failures
    import docling_jobkit.orchestrators.ray.serve_deployment as ray_deployment
    import docling_jobkit.orchestrators.rq.worker as rq_worker

    assert manager_module.DoclingConverterManager is BaseDoclingConverterManager
    assert local_worker.DoclingConverterManager is BaseDoclingConverterManager
    assert rq_worker.DoclingConverterManager is BaseDoclingConverterManager
    assert ray_deployment.DoclingConverterManager is BaseDoclingConverterManager
    assert ray_failures.classify_public_task_failure is not (
        classify_legacy_office_failure
    )


def test_rq_worker_fails_before_queue_consumption_when_capability_missing(
    tmp_path, monkeypatch
):
    from docling_jobkit.orchestrators.rq.worker import CustomRQWorker

    from docling_serve import rq_worker_instrumented as rq_module

    monkeypatch.setattr(
        rq_module.docling_serve_settings,
        "legacy_office_enabled",
        True,
    )
    with (
        patch.object(CustomRQWorker, "__init__") as base_init,
        patch.object(
            rq_module,
            "check_legacy_office_capability",
            side_effect=LegacyOfficeCapabilityError(),
        ),
        pytest.raises(LegacyOfficeCapabilityError),
    ):
        rq_module.InstrumentedRQWorker(
            [],
            orchestrator_config=MagicMock(),
            cm_config=MagicMock(),
            scratch_dir=tmp_path,
        )
    base_init.assert_not_called()


def test_ray_converter_replica_constructor_has_capability_gate(monkeypatch):
    from docling_serve import ray_legacy
    from docling_serve.settings import docling_serve_settings

    monkeypatch.setattr(docling_serve_settings, "legacy_office_enabled", True)
    replica_class = ray_legacy.LegacyOfficeRayConverterDeployment.func_or_class
    monkeypatch.setattr(
        ray_legacy,
        "check_legacy_office_capability",
        MagicMock(side_effect=LegacyOfficeCapabilityError()),
    )
    instance = replica_class.__new__(replica_class)
    with pytest.raises(LegacyOfficeCapabilityError):
        replica_class.__init__(instance, MagicMock(), MagicMock())


def test_manager_constructs_normally_and_restores_public_metadata(
    tmp_path, monkeypatch
):
    from docling_serve.settings import docling_serve_settings

    monkeypatch.setattr(docling_serve_settings, "legacy_office_enabled", True)
    config = SimpleNamespace(max_file_size=100)

    def _base_init(self, received_config):
        self.config = received_config

    converted_result = MagicMock()
    converted_result.input.file = PurePath("report.docx")
    converted_result.document.origin.filename = "report.docx"
    converted_result.document.origin.mimetype = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    captured = {}

    def _base_convert(self, sources, options, headers=None):
        captured["sources"] = sources
        return iter([converted_result])

    monkeypatch.setattr(BaseDoclingConverterManager, "__init__", _base_init)
    monkeypatch.setattr(BaseDoclingConverterManager, "convert_documents", _base_convert)
    manager = LegacyOfficeDoclingConverterManager(
        config,
        converter=_FakeLegacyOfficeConverter(),
        scratch_dir=tmp_path,
        timeout_seconds=9,
        max_input_bytes=100,
        max_output_bytes=100,
        max_scratch_bytes=100,
    )
    original = DocumentStream(name="report.doc", stream=BytesIO(b"binary"))
    results = list(manager.convert_documents([original], ConvertDocumentsOptions()))

    assert captured["sources"][0].name == "report.docx"
    assert results[0].input.file == PurePath("report.doc")
    assert results[0].document.origin.filename == "report.doc"
    assert results[0].document.origin.mimetype == "application/msword"
    assert original.name == "report.doc"
    assert list(tmp_path.iterdir()) == []

    from docling_serve.upload_staging import (
        StagedPublicIdentity,
        bind_staged_identities,
    )

    converted_result.input.file = PurePath("report.docx")
    converted_result.document.origin.filename = "report.docx"
    converted_result.document.origin.mimetype = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    with bind_staged_identities(
        (
            StagedPublicIdentity(
                original_name="report.doc",
                content_type="application/octet-stream",
                original_uri=None,
            ),
        )
    ):
        staged_results = list(
            manager.convert_documents([original], ConvertDocumentsOptions())
        )
    assert staged_results[0].input.file == PurePath("report.doc")
    assert staged_results[0].document.origin.mimetype == "application/octet-stream"


def test_chunk_public_metadata_preserves_original_name_mime_and_uri():
    document = SimpleNamespace(
        origin=SimpleNamespace(
            filename="report.doc",
            mimetype="application/msword",
            uri="https://example.test/report.doc",
        )
    )
    assert _chunk_source_metadata(document, "report.doc") == {
        "originalFilename": "report.doc",
        "originalContentType": "application/msword",
        "sourceUri": "https://example.test/report.doc",
    }


@pytest.mark.skipif(
    not (Path("/usr/bin/soffice").exists() or Path("/usr/bin/libreoffice").exists()),
    reason="Real LibreOffice round-trip runs in production image CI.",
)
def test_real_runtime_smoke():
    from scripts.smoke_legacy_office_runtime import main

    main()
