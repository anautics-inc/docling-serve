"""Secure worker-side preconversion for legacy Microsoft Office documents."""

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import logging
import mimetypes
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from collections.abc import Coroutine, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePath
from typing import Any, NoReturn, Protocol, TypeVar
from urllib.parse import unquote, urljoin, urlparse

from pydantic import AnyUrl, TypeAdapter

from docling.datamodel.base_models import DocumentStream, FailureCategory
from docling.datamodel.document import ConversionResult
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.responses import FailurePhase, PublicFailureInfo
from docling_jobkit.convert.manager import (
    DoclingConverterManager as _BaseDoclingConverterManager,
    DoclingConverterManagerConfig,
)
from docling_jobkit.convert.materialization import (
    SourceFetchError,
    SourceLimitExceededError,
)
from docling_jobkit.public_errors import (
    build_public_task_error as _jobkit_build_public_task_error,
    classify_public_task_failure as _jobkit_classify_public_task_failure,
)

from docling_serve.legacy.source_identity import (
    SourceIdentityRestorer,
    restore_context_source_identity,
)

_log = logging.getLogger(__name__)
_T = TypeVar("_T")

LEGACY_OFFICE_TARGETS = {
    ".doc": ".docx",
    ".ppt": ".pptx",
    ".xls": ".xlsx",
}
LEGACY_OFFICE_MIME_TYPES = {
    ".doc": "application/msword",
    ".ppt": "application/vnd.ms-powerpoint",
    ".xls": "application/vnd.ms-excel",
}
APPROVED_SYSTEM_EXECUTABLE_ROOTS = (
    Path("/usr/bin"),
    Path("/usr/libexec"),
    Path("/usr/lib64/libreoffice"),
    Path("/usr/lib/libreoffice"),
    Path("/opt/libreoffice"),
)


class LegacyOfficeError(RuntimeError):
    """Base class for stable, client-visible preconversion failures."""

    code = "legacy_office_conversion_failed"
    public_message = "Legacy Office preconversion failed."
    category = FailureCategory.BACKEND_FAILURE
    retryable = False
    failure_phase = FailurePhase.EXECUTION


class LegacyOfficeCapabilityError(LegacyOfficeError):
    code = "legacy_office_capability_unavailable"
    public_message = "Legacy Office conversion capability is unavailable."


class LegacyOfficeConversionError(LegacyOfficeError):
    code = "legacy_office_conversion_failed"
    public_message = "Legacy Office document could not be converted."


class LegacyOfficeMissingOutputError(LegacyOfficeConversionError):
    code = "legacy_office_missing_output"
    public_message = "Legacy Office conversion produced no supported output."


class LegacyOfficeLimitError(LegacyOfficeError):
    code = "legacy_office_limit_exceeded"
    public_message = "Legacy Office conversion exceeded a configured size limit."
    category = FailureCategory.POLICY


class LegacyOfficeInputLimitError(LegacyOfficeLimitError, SourceLimitExceededError):
    code = "legacy_office_input_limit_exceeded"
    public_message = "Legacy Office input exceeds the configured size limit."


class LegacyOfficeOutputLimitError(LegacyOfficeLimitError):
    code = "legacy_office_output_limit_exceeded"
    public_message = "Converted Office output exceeds the configured size limit."


class LegacyOfficeScratchLimitError(LegacyOfficeLimitError):
    code = "legacy_office_scratch_limit_exceeded"
    public_message = "Legacy Office conversion exceeded its scratch-space limit."


class LegacyOfficeTimeoutError(LegacyOfficeError, TimeoutError):
    code = "legacy_office_timeout"
    public_message = "Legacy Office conversion exceeded the allowed execution time."
    category = FailureCategory.TIMEOUT
    retryable = True


class LegacyOfficeSourcePolicyError(LegacyOfficeLimitError):
    code = "legacy_office_source_policy"
    public_message = "Legacy Office source URL violates service network policy."
    failure_phase = FailurePhase.SOURCE_ENUMERATION


class LegacyOfficeSourceFetchError(SourceFetchError):
    """Transient fetch error known to originate in the legacy adapter."""


class LegacyOfficeProcessSurvivedError(LegacyOfficeError):
    code = "legacy_office_process_survived"
    public_message = "Legacy Office converter could not be terminated safely."


def terminate_worker_fatally(error: BaseException) -> NoReturn:
    """Production fail-stop: never let a worker consume after an escaped child."""

    _log.critical(
        "Terminating worker after unreaped LibreOffice process", exc_info=error
    )
    os._exit(70)


@dataclass(frozen=True)
class LegacyHttpFetchResult:
    payload: bytes
    final_url: str
    content_type: str


_UNSAFE_FORWARD_HEADERS = {
    "connection",
    "content-length",
    "host",
    "proxy-connection",
    "transfer-encoding",
}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


@dataclass(frozen=True)
class ResolvedGlobalAddress:
    family: int
    socktype: int
    proto: int
    sockaddr: tuple[Any, ...]
    ip: str


@dataclass(frozen=True)
class PinnedHttpResponse:
    status: int
    headers: dict[str, str]
    payload: bytes


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise LegacyOfficeSourcePolicyError(
            "Legacy Office URLs must use http or https."
        )
    if parsed.username is not None or parsed.password is not None:
        raise LegacyOfficeSourcePolicyError(
            "Legacy Office URLs must not contain embedded credentials."
        )
    return (
        scheme,
        parsed.hostname.lower().rstrip("."),
        parsed.port or (443 if scheme == "https" else 80),
    )


def _resolve_global_addresses(
    host: str, port: int
) -> tuple[ResolvedGlobalAddress, ...]:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LegacyOfficeSourceFetchError(
            f"Source host '{host}' could not be resolved."
        ) from exc
    addresses: list[ResolvedGlobalAddress] = []
    seen: set[tuple[int, str]] = set()
    for family, socktype, proto, _canonical_name, sockaddr in records:
        address = str(sockaddr[0])
        identity = (family, address)
        if identity in seen:
            continue
        seen.add(identity)
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise LegacyOfficeSourcePolicyError(
                f"Source host '{host}' resolves to a non-global address."
            )
        addresses.append(
            ResolvedGlobalAddress(
                family=family,
                socktype=socktype,
                proto=proto,
                sockaddr=tuple(sockaddr),
                ip=address,
            )
        )
    if not addresses:
        raise LegacyOfficeSourceFetchError(
            f"Source host '{host}' returned no addresses."
        )
    return tuple(addresses)


def _sanitized_source_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    for key, value in (headers or {}).items():
        name = str(key)
        text = str(value)
        if name.lower() in _UNSAFE_FORWARD_HEADERS:
            continue
        if _HEADER_NAME.fullmatch(name) is None or "\r" in text or "\n" in text:
            raise LegacyOfficeSourcePolicyError("Source headers contain invalid bytes.")
        sanitized[name] = text
    return sanitized


def _host_header(scheme: str, host: str, port: int) -> str:
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 443 if scheme == "https" else 80
    return rendered_host if port == default_port else f"{rendered_host}:{port}"


def _request_pinned(
    url: str,
    *,
    address: ResolvedGlobalAddress,
    headers: dict[str, str],
    timeout_seconds: float,
    max_file_size: int,
    ssl_context_factory: Any = None,
    socket_factory: Any = socket.socket,
) -> PinnedHttpResponse:
    """Connect directly to one validated address while retaining hostname TLS."""

    scheme, host, port = _url_origin(url)
    deadline = time.monotonic() + timeout_seconds
    parsed = urlparse(url)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    raw_socket = socket_factory(address.family, address.socktype, address.proto)
    connection_socket: Any = raw_socket
    try:
        raw_socket.settimeout(timeout_seconds)
        raw_socket.connect(address.sockaddr)
        if scheme == "https":
            context_factory = ssl_context_factory
            if context_factory is None:
                import ssl

                context_factory = ssl.create_default_context
            context = context_factory()
            connection_socket = context.wrap_socket(raw_socket, server_hostname=host)

        request_headers = {
            **headers,
            "Host": _host_header(scheme, host, port),
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        request_lines = [f"GET {target} HTTP/1.1"]
        request_lines.extend(
            f"{key}: {value}" for key, value in request_headers.items()
        )
        connection_socket.sendall(("\r\n".join(request_lines) + "\r\n\r\n").encode())
        response = http.client.HTTPResponse(connection_socket)
        response.begin()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        declared = response_headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > max_file_size:
                    raise LegacyOfficeInputLimitError(
                        f"Source exceeds {max_file_size} bytes."
                    )
            except ValueError:
                pass
        buffer = BytesIO()
        bytes_seen = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LegacyOfficeSourceFetchError(
                    "Legacy Office source retrieval timed out."
                )
            connection_socket.settimeout(remaining)
            chunk = response.read(min(64 * 1024, max_file_size + 1 - bytes_seen))
            if not chunk:
                break
            bytes_seen += len(chunk)
            if bytes_seen > max_file_size:
                raise LegacyOfficeInputLimitError(
                    f"Source exceeds {max_file_size} bytes."
                )
            buffer.write(chunk)
        return PinnedHttpResponse(
            status=response.status,
            headers=response_headers,
            payload=buffer.getvalue(),
        )
    except (OSError, http.client.HTTPException) as exc:
        raise LegacyOfficeSourceFetchError(
            "Legacy Office source could not be downloaded."
        ) from exc
    finally:
        try:
            connection_socket.close()
        finally:
            if connection_socket is not raw_socket:
                raw_socket.close()


def _fetch_legacy_http_source_sync(
    url: str,
    *,
    headers: dict[str, Any] | None,
    max_file_size: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Any,
    connector: Any,
) -> LegacyHttpFetchResult:
    current_url = url
    deadline = time.monotonic() + timeout_seconds
    current_headers = _sanitized_source_headers(headers)
    original_origin = _url_origin(url)
    for redirect_count in range(max_redirects + 1):
        origin = _url_origin(current_url)
        addresses = tuple(resolver(origin[1], origin[2]))
        if not addresses:
            raise LegacyOfficeSourceFetchError(
                f"Source host '{origin[1]}' returned no addresses."
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LegacyOfficeSourceFetchError(
                "Legacy Office source retrieval timed out."
            )
        response = connector(
            current_url,
            address=addresses[0],
            headers=current_headers,
            timeout_seconds=remaining,
            max_file_size=max_file_size,
        )
        if response.status in _REDIRECT_STATUSES:
            if redirect_count >= max_redirects:
                raise LegacyOfficeSourcePolicyError(
                    "Legacy Office source exceeded the redirect limit."
                )
            location = response.headers.get("location")
            if not location:
                raise LegacyOfficeSourcePolicyError(
                    "Legacy Office redirect omitted its destination."
                )
            destination = urljoin(current_url, location)
            destination_origin = _url_origin(destination)
            if destination_origin != original_origin:
                current_headers = {}
            current_url = destination
            continue
        if response.status < 200 or response.status >= 300:
            raise LegacyOfficeSourceFetchError(
                f"Legacy Office source returned HTTP status {response.status}."
            )
        content_type = (
            response.headers.get("content-type", "application/octet-stream")
            .split(";", 1)[0]
            .strip()
        )
        return LegacyHttpFetchResult(
            payload=response.payload,
            final_url=current_url,
            content_type=content_type or "application/octet-stream",
        )
    raise LegacyOfficeSourcePolicyError(
        "Legacy Office source exceeded the redirect limit."
    )


async def fetch_legacy_http_source(
    url: str,
    *,
    headers: dict[str, Any] | None,
    max_file_size: int,
    timeout_seconds: float,
    max_redirects: int,
    resolver: Any = _resolve_global_addresses,
    connector: Any = _request_pinned,
) -> LegacyHttpFetchResult:
    """Fetch with DNS-to-socket pinning and no environment proxy resolution."""

    return await asyncio.to_thread(
        _fetch_legacy_http_source_sync,
        url,
        headers=headers,
        max_file_size=max_file_size,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
        resolver=resolver,
        connector=connector,
    )


def _validate_shared_url_headers(
    sources: list[Path | str | DocumentStream],
    headers: dict[str, Any] | None,
) -> None:
    if not headers:
        return
    origins = {
        _url_origin(source)
        for source in sources
        if isinstance(source, str)
        and urlparse(source).scheme.lower() in {"http", "https"}
    }
    if len(origins) != 1:
        raise LegacyOfficeSourcePolicyError(
            "Mixed URL sources with custom headers must all use the exact same origin."
        )


class LegacyOfficeConverter(Protocol):
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
    ) -> Path: ...


def _path_tree_size(root: Path) -> int:
    total = 0
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            raise LegacyOfficeConversionError(
                                "LibreOffice created an unsafe symbolic link in scratch space."
                            )
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        # LibreOffice creates and removes profile temp files while
                        # conversion is active. A vanished entry contributes no size.
                        continue
        except FileNotFoundError:
            # A queued temporary directory may disappear before it is scanned.
            continue
    return total


def _check_converter_growth(
    output_dir: Path,
    *,
    target_suffix: str,
    max_output_bytes: int,
    max_scratch_bytes: int,
    max_file_count: int,
) -> None:
    regular_file_count = 0
    pending = [output_dir]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            raise LegacyOfficeConversionError(
                                "LibreOffice scratch contains an unsafe symbolic link."
                            )
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                            continue
                        mode = entry.stat(follow_symlinks=False).st_mode
                        if not stat.S_ISREG(mode):
                            raise LegacyOfficeConversionError(
                                "LibreOffice scratch contains a non-regular file."
                            )
                        regular_file_count += 1
                        if regular_file_count > max_file_count:
                            raise LegacyOfficeScratchLimitError(
                                f"LibreOffice scratch exceeds {max_file_count} files."
                            )
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            continue
    if _path_tree_size(output_dir) > max_scratch_bytes:
        raise LegacyOfficeScratchLimitError(
            f"LibreOffice scratch exceeds {max_scratch_bytes} bytes."
        )
    for candidate in output_dir.glob(f"*{target_suffix}"):
        if (
            not candidate.is_symlink()
            and candidate.is_file()
            and candidate.stat().st_size > max_output_bytes
        ):
            raise LegacyOfficeOutputLimitError(
                f"Converted output exceeds {max_output_bytes} bytes."
            )


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    """Terminate the process group and prove the child has been reaped."""

    if process.poll() is not None:
        process.wait(timeout=1)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise LegacyOfficeProcessSurvivedError(
                "LibreOffice remained alive after SIGTERM and SIGKILL."
            ) from exc
    if process.poll() is None:
        raise LegacyOfficeProcessSurvivedError(
            "LibreOffice remained alive after process-group cleanup."
        )


def _resolve_approved_system_executable(
    candidate: Path,
    *,
    label: str,
    approved_roots: tuple[Path, ...] | None = None,
) -> Path:
    if not candidate.is_absolute():
        raise LegacyOfficeCapabilityError(f"{label} must be an absolute path.")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise LegacyOfficeCapabilityError(
            f"{label} is missing or has a broken link."
        ) from exc
    if (
        not resolved.is_file()
        or not stat.S_ISREG(resolved.stat().st_mode)
        or not os.access(resolved, os.X_OK)
    ):
        raise LegacyOfficeCapabilityError(
            f"{label} target is not a regular executable."
        )
    roots = approved_roots or APPROVED_SYSTEM_EXECUTABLE_ROOTS
    if not any(resolved.is_relative_to(root) for root in roots):
        raise LegacyOfficeCapabilityError(
            f"{label} target is outside approved system executable paths."
        )
    return resolved


class LibreOfficeHeadlessConverter:
    """Run an installed LibreOffice binary with isolated state and hard limits."""

    def __init__(
        self,
        executable: Path | None = None,
        *,
        approved_roots: tuple[Path, ...] | None = None,
        poll_interval: float = 0.05,
        fatal_worker_terminator: Any = terminate_worker_fatally,
    ):
        self._configured_executable = executable
        self._approved_roots = approved_roots
        self._poll_interval = poll_interval
        self._fatal_worker_terminator = fatal_worker_terminator

    def resolve_executable(self) -> Path:
        if self._configured_executable is not None:
            return _resolve_approved_system_executable(
                self._configured_executable,
                label="Configured LibreOffice executable",
                approved_roots=self._approved_roots,
            )

        discovered = shutil.which("soffice") or shutil.which("libreoffice")
        if discovered:
            return _resolve_approved_system_executable(
                Path(discovered),
                label="Discovered LibreOffice executable",
                approved_roots=self._approved_roots,
            )
        raise LegacyOfficeCapabilityError(
            "No sanctioned LibreOffice headless executable was found."
        )

    def check_capability(self) -> Path:
        return self.resolve_executable()

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
        executable = self.resolve_executable()
        prlimit_name = shutil.which("prlimit")
        if prlimit_name is None:
            raise LegacyOfficeCapabilityError(
                "The required prlimit resource-control launcher is unavailable."
            )
        prlimit = _resolve_approved_system_executable(
            Path(prlimit_name),
            label="prlimit executable",
            approved_roots=self._approved_roots,
        )
        profile_dir = output_dir / "profile"
        profile_dir.mkdir(mode=0o700)
        command = [
            str(prlimit),
            f"--fsize={max_output_bytes}:{max_output_bytes}",
            "--",
            str(executable),
            "--headless",
            "--nologo",
            "--nodefault",
            "--nolockcheck",
            "--nofirststartwizard",
            f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
            "--convert-to",
            target_suffix.removeprefix("."),
            "--outdir",
            str(output_dir),
            str(source),
        ]
        env = {
            "HOME": str(output_dir),
            "TMPDIR": str(output_dir),
            "XDG_CACHE_HOME": str(output_dir / "cache"),
            "XDG_CONFIG_HOME": str(output_dir / "config"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise LegacyOfficeCapabilityError(
                "LibreOffice executable could not be started."
            ) from exc

        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise LegacyOfficeTimeoutError(
                        f"LibreOffice exceeded {timeout_seconds:g} seconds."
                    )
                _check_converter_growth(
                    output_dir,
                    target_suffix=target_suffix,
                    max_output_bytes=max_output_bytes,
                    max_scratch_bytes=max_scratch_bytes,
                    max_file_count=max_file_count,
                )
                time.sleep(self._poll_interval)
        except BaseException as original_error:
            try:
                _terminate_and_reap(process)
            except LegacyOfficeProcessSurvivedError as survivor:
                _log.critical(
                    "LibreOffice survived cleanup; original failure follows",
                    exc_info=True,
                )
                _log.error(
                    "Original converter failure before fatal cleanup",
                    exc_info=original_error,
                )
                self._fatal_worker_terminator(survivor)
                raise survivor
            raise
        _terminate_and_reap(process)
        _check_converter_growth(
            output_dir,
            target_suffix=target_suffix,
            max_output_bytes=max_output_bytes,
            max_scratch_bytes=max_scratch_bytes,
            max_file_count=max_file_count,
        )

        if process.returncode != 0:
            raise LegacyOfficeConversionError(
                f"LibreOffice failed to convert {source.name} "
                f"(exit code {process.returncode})."
            )

        output_root = output_dir.resolve()
        candidates: list[Path] = []
        for candidate in output_dir.glob(f"*{target_suffix}"):
            if candidate.is_symlink():
                raise LegacyOfficeConversionError(
                    "LibreOffice output must not be a symbolic link."
                )
            candidate_stat = candidate.stat(follow_symlinks=False)
            resolved = candidate.resolve()
            if (
                not stat.S_ISREG(candidate_stat.st_mode)
                or resolved.parent != output_root
            ):
                raise LegacyOfficeConversionError(
                    "LibreOffice output is not a contained regular file."
                )
            candidates.append(candidate)
        if len(candidates) != 1:
            raise LegacyOfficeMissingOutputError(
                "LibreOffice did not produce exactly one supported output."
            )
        converted = candidates[0]
        if converted.stat().st_size > max_output_bytes:
            raise LegacyOfficeOutputLimitError(
                f"Converted output exceeds {max_output_bytes} bytes."
            )
        return converted


def _source_filename(name: str | Path) -> str:
    text = str(name)
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"}:
        return unquote(PurePath(parsed.path).name)
    return Path(text).name


def is_legacy_office_name(name: str | Path) -> bool:
    return Path(_source_filename(name)).suffix.lower() in LEGACY_OFFICE_TARGETS


def _safe_source_name(name: str, fallback: str) -> str:
    basename = _source_filename(name)
    return basename if basename not in {"", ".", ".."} else fallback


def _bounded_stream_read(
    stream: Any,
    limit: int,
    error_cls: type[LegacyOfficeLimitError] = LegacyOfficeInputLimitError,
) -> bytes:
    payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise error_cls(f"Payload exceeds {limit} bytes.")
    return payload


def _bounded_file_read(
    path: Path,
    limit: int,
    error_cls: type[LegacyOfficeLimitError] = LegacyOfficeInputLimitError,
) -> bytes:
    with path.open("rb") as handle:
        return _bounded_stream_read(handle, limit, error_cls)


def _validate_converter_output(path: Path, output_dir: Path) -> None:
    if not path.exists():
        raise LegacyOfficeMissingOutputError("Converted output does not exist.")
    if path.is_symlink():
        raise LegacyOfficeConversionError("Converted output must not be a symlink.")
    path_stat = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path.resolve().parent != output_dir.resolve()
    ):
        raise LegacyOfficeConversionError(
            "Converted output must be a contained regular file."
        )


def _run_coroutine(coroutine: Coroutine[Any, Any, _T]) -> _T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coroutine).result()


@dataclass(frozen=True)
class LegacySourceIdentity:
    original_name: str
    original_mime_type: str
    source_uri: str | None = None


@dataclass
class PreparedLegacySources:
    sources: list[Path | str | DocumentStream]
    identities: dict[int, LegacySourceIdentity]


@contextmanager
def preconvert_legacy_office_sources(
    sources: Iterable[Path | str | DocumentStream],
    *,
    converter: LegacyOfficeConverter,
    scratch_dir: Path | None,
    timeout_seconds: float,
    max_input_bytes: int,
    max_output_bytes: int,
    max_scratch_bytes: int,
    max_file_count: int,
    fetch_timeout_seconds: float,
    max_redirects: int,
    headers: dict[str, Any] | None = None,
) -> Iterator[PreparedLegacySources]:
    temp_parent = scratch_dir
    if temp_parent is not None:
        temp_parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="legacy-office-",
        dir=str(temp_parent) if temp_parent is not None else None,
    ) as raw_work_dir:
        work_dir = Path(raw_work_dir)
        source_list = list(sources)
        _validate_shared_url_headers(source_list, headers)
        prepared: list[Path | str | DocumentStream] = []
        identities: dict[int, LegacySourceIdentity] = {}
        for index, source in enumerate(source_list):
            if not isinstance(source, (DocumentStream, Path, str)):
                prepared.append(source)
                continue
            source_name = (
                source.name if isinstance(source, DocumentStream) else str(source)
            )
            if not is_legacy_office_name(source_name):
                prepared.append(source)
                continue

            safe_name = _safe_source_name(source_name, f"source-{index}.bin")
            suffix = Path(safe_name).suffix.lower()
            is_http_source = isinstance(source, str) and urlparse(
                source_name
            ).scheme in {"http", "https"}
            source_uri = source_name if is_http_source else None
            if isinstance(source, DocumentStream):
                payload = _bounded_stream_read(source.stream, max_input_bytes)
                original_mime_type = LEGACY_OFFICE_MIME_TYPES[suffix]
            elif isinstance(source, Path):
                source_stat = source.stat(follow_symlinks=False)
                if source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
                    raise LegacyOfficeConversionError(
                        "Legacy Office path input must be a regular non-symlink file."
                    )
                payload = _bounded_file_read(source, max_input_bytes)
            elif is_http_source:
                fetched = _run_coroutine(
                    fetch_legacy_http_source(
                        source_name,
                        headers=headers,
                        max_file_size=max_input_bytes,
                        timeout_seconds=fetch_timeout_seconds,
                        max_redirects=max_redirects,
                    )
                )
                payload = fetched.payload
                original_mime_type = fetched.content_type
            else:
                path_source = Path(source)
                source_stat = path_source.stat(follow_symlinks=False)
                if path_source.is_symlink() or not stat.S_ISREG(source_stat.st_mode):
                    raise LegacyOfficeConversionError(
                        "Legacy Office path input must be a regular non-symlink file."
                    )
                payload = _bounded_file_read(path_source, max_input_bytes)
            if not isinstance(source, (DocumentStream, str)) or source_uri is None:
                original_mime_type = (
                    original_mime_type
                    if isinstance(source, DocumentStream)
                    else LEGACY_OFFICE_MIME_TYPES[suffix]
                )

            source_path = work_dir / f"source-{index}" / safe_name
            source_path.parent.mkdir(mode=0o700)
            source_path.write_bytes(payload)
            output_dir = work_dir / f"output-{index}"
            output_dir.mkdir(mode=0o700)
            target_suffix = LEGACY_OFFICE_TARGETS[suffix]
            converted_path = converter.convert(
                source_path,
                output_dir,
                target_suffix=target_suffix,
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                max_scratch_bytes=max_scratch_bytes,
                max_file_count=max_file_count,
            )
            _validate_converter_output(converted_path, output_dir)
            converted_bytes = _bounded_file_read(
                converted_path,
                max_output_bytes,
                LegacyOfficeOutputLimitError,
            )
            converted_name = str(Path(safe_name).with_suffix(target_suffix))
            prepared.append(
                DocumentStream(name=converted_name, stream=BytesIO(converted_bytes))
            )
            identities[index] = LegacySourceIdentity(
                original_name=safe_name,
                original_mime_type=original_mime_type,
                source_uri=source_uri,
            )
            _log.info(
                "Preconverted legacy Office source %s to %s (%d bytes)",
                safe_name,
                converted_name,
                len(converted_bytes),
            )
        yield PreparedLegacySources(sources=prepared, identities=identities)


def _restore_public_identity(
    result: ConversionResult, identity: LegacySourceIdentity
) -> ConversionResult:
    result.input.file = PurePath(identity.original_name)
    if result.document is not None and result.document.origin is not None:
        result.document.origin.filename = identity.original_name
        result.document.origin.mimetype = identity.original_mime_type
        if identity.source_uri is not None:
            result.document.origin.uri = TypeAdapter(AnyUrl).validate_python(
                identity.source_uri
            )
    return result


class LegacyOfficeDoclingConverterManager(_BaseDoclingConverterManager):
    """One converter-manager implementation shared by Local, RQ, and Ray."""

    def __init__(
        self,
        config: DoclingConverterManagerConfig,
        *,
        converter: LegacyOfficeConverter | None = None,
        scratch_dir: Path | None = None,
        timeout_seconds: float | None = None,
        max_input_bytes: int | None = None,
        max_output_bytes: int | None = None,
        max_scratch_bytes: int | None = None,
        max_file_count: int | None = None,
        fetch_timeout_seconds: float | None = None,
        max_redirects: int | None = None,
        executable: Path | None = None,
        source_identity_restorer: SourceIdentityRestorer = (
            restore_context_source_identity
        ),
    ):
        from docling_serve.settings_views import current_legacy_office_settings
        from docling_serve.storage import get_scratch

        super().__init__(config)
        self.source_identity_restorer = source_identity_restorer
        settings = current_legacy_office_settings()
        self.legacy_office_enabled = settings.enabled
        self.legacy_office_converter = converter or LibreOfficeHeadlessConverter(
            executable if executable is not None else settings.executable,
            approved_roots=settings.approved_executable_roots,
        )
        self.legacy_office_scratch_dir = scratch_dir or get_scratch()
        self.legacy_office_timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.timeout_seconds
        )
        self.legacy_office_max_input_bytes = min(
            max_input_bytes or settings.max_input_bytes,
            self.config.max_file_size,
        )
        self.legacy_office_max_output_bytes = min(
            max_output_bytes or settings.max_output_bytes,
            self.config.max_file_size,
        )
        self.legacy_office_max_scratch_bytes = (
            max_scratch_bytes or settings.max_scratch_bytes
        )
        self.legacy_office_max_file_count = max_file_count or settings.max_file_count
        self.legacy_office_fetch_timeout_seconds = (
            fetch_timeout_seconds or settings.fetch_timeout_seconds
        )
        self.legacy_office_max_redirects = (
            max_redirects if max_redirects is not None else settings.max_redirects
        )

    def convert_documents(
        self,
        sources: Iterable[Path | str | DocumentStream],
        options: ConvertDocumentsOptions,
        headers: dict[str, Any] | None = None,
    ) -> Iterable[ConversionResult]:
        if not self.legacy_office_enabled:
            results = super().convert_documents(
                sources=sources, options=options, headers=headers
            )

            def restore_staged() -> Iterator[ConversionResult]:
                for index, result in enumerate(results):
                    yield self.source_identity_restorer(result, index)

            return restore_staged()

        def convert() -> Iterator[ConversionResult]:
            with preconvert_legacy_office_sources(
                sources,
                converter=self.legacy_office_converter,
                scratch_dir=self.legacy_office_scratch_dir,
                timeout_seconds=self.legacy_office_timeout_seconds,
                max_input_bytes=self.legacy_office_max_input_bytes,
                max_output_bytes=self.legacy_office_max_output_bytes,
                max_scratch_bytes=self.legacy_office_max_scratch_bytes,
                max_file_count=self.legacy_office_max_file_count,
                fetch_timeout_seconds=self.legacy_office_fetch_timeout_seconds,
                max_redirects=self.legacy_office_max_redirects,
                headers=headers,
            ) as prepared:
                results = super(
                    LegacyOfficeDoclingConverterManager, self
                ).convert_documents(
                    sources=prepared.sources,
                    options=options,
                    headers=headers,
                )
                for index, result in enumerate(results):
                    identity = prepared.identities.get(index)
                    restored = (
                        _restore_public_identity(result, identity)
                        if identity is not None
                        else result
                    )
                    yield self.source_identity_restorer(restored, index)

        return convert()


def _chunk_source_metadata(document: Any, filename: str) -> dict[str, str]:
    origin = getattr(document, "origin", None)
    return {
        "originalFilename": getattr(origin, "filename", None) or filename,
        "originalContentType": getattr(origin, "mimetype", None)
        or original_content_type(filename),
        **(
            {"sourceUri": str(origin.uri)}
            if origin is not None and origin.uri is not None
            else {}
        ),
    }


def _build_source_metadata_chunker_class():
    from docling_jobkit.convert.chunking import DocumentChunkerManager

    class SourceMetadataChunkerManager(DocumentChunkerManager):
        def chunk_document(self, document, filename, options):
            source_metadata = _chunk_source_metadata(document, filename)
            for chunk in super().chunk_document(document, filename, options):
                chunk.metadata = {
                    **(chunk.metadata or {}),
                    **source_metadata,
                }
                yield chunk

    return SourceMetadataChunkerManager


def classify_legacy_office_failure(
    exc: BaseException,
    *,
    task_id: str,
    phase: FailurePhase = FailurePhase.EXECUTION,
    details: dict[str, str] | None = None,
) -> PublicFailureInfo:
    if isinstance(exc, LegacyOfficeError):
        return PublicFailureInfo(
            category=exc.category,
            message=exc.public_message,
            retryable=exc.retryable,
            phase=exc.failure_phase,
            details={
                **(details or {}),
                "code": exc.code,
            },
        )
    if isinstance(exc, LegacyOfficeSourceFetchError):
        return PublicFailureInfo(
            category=FailureCategory.SOURCE_UNAVAILABLE,
            message="Legacy Office source could not be reached.",
            retryable=True,
            phase=FailurePhase.SOURCE_ENUMERATION,
            details={**(details or {}), "code": "legacy_office_source_fetch"},
        )
    return _jobkit_classify_public_task_failure(
        exc, task_id=task_id, phase=phase, details=details
    )


def build_legacy_office_public_task_error(exc: BaseException) -> str:
    if isinstance(exc, LegacyOfficeError):
        return exc.public_message
    return _jobkit_build_public_task_error(exc)


async def ray_converter_run_with_retry(
    replica: Any,
    task_label: str,
    func: Any,
    *,
    task: Any = None,
) -> Any:
    """Ray converter retry policy that honors every classified retryable flag."""

    from docling_jobkit.orchestrators.ray.models import ConverterFailureResult

    max_retries = replica.config.max_task_retries
    retry_delay = replica.config.retry_delay
    last_exception: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(func)
        except Exception as exc:
            last_exception = exc
            failure = classify_legacy_office_failure(
                exc,
                task_id=task.task_id if task is not None else str(task_label),
                phase=FailurePhase.EXECUTION,
                details=(
                    {
                        "task_size": str(len(task.sources)),
                        "target_kind": task.target.kind,
                    }
                    if task is not None
                    else None
                ),
            )
            if not failure.retryable:
                if task is not None:
                    return ConverterFailureResult(failure=failure)
                raise
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
            elif task is not None:
                return ConverterFailureResult(failure=failure)
    raise last_exception or RuntimeError("Converter request failed")


def build_converter_manager(
    config: DoclingConverterManagerConfig,
) -> LegacyOfficeDoclingConverterManager:
    return LegacyOfficeDoclingConverterManager(config=config)


def check_legacy_office_capability() -> Path:
    from docling_serve.settings_views import current_legacy_office_settings

    settings = current_legacy_office_settings()
    return LibreOfficeHeadlessConverter(
        settings.executable,
        approved_roots=settings.approved_executable_roots,
    ).check_capability()


def original_content_type(name: str) -> str:
    return LEGACY_OFFICE_MIME_TYPES.get(
        Path(name).suffix.lower(),
        mimetypes.guess_type(name)[0] or "application/octet-stream",
    )
