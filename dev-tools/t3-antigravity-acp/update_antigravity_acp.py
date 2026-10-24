#!/usr/bin/env python3
"""Install and activate the Antigravity ACP release listed in the ACP registry.

Releases live in versioned, platform-specific directories under
``<install-root>/releases/``. A validated release is activated by atomically
repointing ``antigravity-acp-current``; the stable launcher
``<install-root>/antigravity-acp`` resolves that link at launch time. Nothing is
ever deleted unless ``--prune`` is given explicitly.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import http.client
import json
import os
import platform
import re
import selectors
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, cast

REGISTRY_URL = (
    "https://raw.githubusercontent.com/agentclientprotocol/registry/"
    + "refs/heads/main/antigravity-acp/agent.json"
)
DEFAULT_INSTALL_ROOT = Path.home() / ".t3" / "custom-tools"
DEFAULT_T3_MANAGED_ROOT = Path.home() / ".t3" / "tools" / "antigravity-acp"
LEGACY_DIR_PREFIX = "antigravity-acp-"
RELEASES_DIR = "releases"
CURRENT_LINK = "antigravity-acp-current"
PREVIOUS_LINK = "antigravity-acp-previous"
LAUNCHER_NAME = "antigravity-acp"
LOCK_NAME = ".antigravity-acp.lock"
MANIFEST_NAME = "install-manifest.json"
REGISTRY_COPY_NAME = "registry-agent.json"
RELEASE_LAUNCH_NAME = "launch"
STAGING_PREFIX = ".staging-"
MANIFEST_SCHEMA = 1
USER_AGENT = "t3-antigravity-acp-updater/2.0"
MAX_REDIRECTS = 5
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
MAX_EXTRACTED_BYTES = 8 * 1024**3
SMOKE_TEST_TIMEOUT_SECONDS = 180.0
EXIT_LOCK_BUSY = 75
INTEGRITY_NOTE = (
    "The archive matched the sha256 the ACP registry publishes. File hashes are computed "
    + "locally at install time as provenance records; they are not publisher signatures."
)

ELF_MACHINES = {62: "x86_64", 183: "aarch64"}
MACHO_CPU_TYPES = {0x01000007: "x86_64", 0x0100000C: "aarch64"}
PE_MACHINES = {0x8664: "x86_64", 0xAA64: "aarch64"}
EXPECTED_FORMAT = {"linux": "elf", "darwin": "macho", "windows": "pe"}


class UpdaterError(RuntimeError):
    """A failure the CLI reports without a traceback."""


class LockBusyError(UpdaterError):
    """Another updater holds the install-root lock."""


@dataclass(frozen=True)
class PlatformBinarySpec:
    platform_key: str
    archive_url: str
    cmd: str
    args: tuple[str, ...]
    sha256: str  # published by the registry; the download must match it


@dataclass(frozen=True)
class RegistryRelease:
    agent_id: str
    name: str
    version: str
    binaries: dict[str, PlatformBinarySpec]


@dataclass(frozen=True)
class RegistrySnapshot:
    release: RegistryRelease
    source: str
    sha256: str
    raw: bytes


@dataclass(frozen=True)
class NativePlatform:
    key: str
    translated: bool


@dataclass(frozen=True)
class BinaryFormat:
    kind: str
    archs: tuple[str, ...]


@dataclass(frozen=True)
class InstalledRelease:
    name: str
    directory: Path
    version: str
    platform_key: str
    manifest: dict[str, object]


@dataclass(frozen=True)
class LegacyRelease:
    version: str
    directory: Path


# ---------------------------------------------------------------- JSON helpers


def as_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return {}


def as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def as_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = cast("list[object]", value)
    return tuple(item for item in items if isinstance(item, str))


def parse_json_object(raw: str) -> dict[str, object]:
    loaded = cast("object", json.loads(raw))
    if not isinstance(loaded, dict):
        raise ValueError("Expected top-level JSON object")
    return cast("dict[str, object]", loaded)


# -------------------------------------------------------------------- registry


def parse_registry_release(raw: str) -> RegistryRelease:
    data = parse_json_object(raw)
    agent_id = as_str(data.get("id"))
    name = as_str(data.get("name"))
    version = as_str(data.get("version"))
    if not version:
        raise ValueError("Registry JSON is missing 'version'")

    distribution = as_dict(data.get("distribution"))
    binary_map = as_dict(distribution.get("binary"))
    if not binary_map:
        raise ValueError("Registry JSON is missing 'distribution.binary'")

    binaries: dict[str, PlatformBinarySpec] = {}
    for key, spec_obj in binary_map.items():
        spec = as_dict(spec_obj)
        archive_url = as_str(spec.get("archive"))
        cmd = as_str(spec.get("cmd"))
        args = as_str_tuple(spec.get("args"))
        digest = as_str(spec.get("sha256")).lower()
        if archive_url and cmd:
            binaries[key] = PlatformBinarySpec(
                platform_key=key,
                archive_url=archive_url,
                cmd=cmd,
                args=args,
                sha256=digest,
            )

    if not binaries:
        raise ValueError("No valid platform binary entries found in registry JSON")

    return RegistryRelease(agent_id=agent_id, name=name, version=version, binaries=binaries)


def is_remote_source(source: str) -> bool:
    return urllib.parse.urlsplit(source).scheme in ("http", "https")


def verify_archive_digest(archive_path: Path, spec: PlatformBinarySpec) -> None:
    """Fail unless the download matches the sha256 the registry publishes."""
    if not SHA256_HEX.fullmatch(spec.sha256):
        raise UpdaterError(f"Registry lists no valid sha256 for {spec.platform_key}; refusing to install")
    actual = sha256_file(archive_path)
    if actual != spec.sha256:
        raise UpdaterError(
            f"Archive sha256 mismatch for {spec.archive_url}: registry {spec.sha256}, downloaded {actual}"
        )


def load_registry(source: str, timeout: float = 15.0) -> RegistrySnapshot:
    """Load the registry from a URL or a local snapshot file, keeping its exact bytes."""
    if is_remote_source(source):
        with open_http_response(source, timeout=timeout) as response:
            raw = response.read()
        label = source
    else:
        path = Path(source.removeprefix("file://")).expanduser().resolve()
        raw = path.read_bytes()
        label = str(path)
    return RegistrySnapshot(
        release=parse_registry_release(raw.decode("utf-8")),
        source=label,
        sha256=hashlib.sha256(raw).hexdigest(),
        raw=raw,
    )


# -------------------------------------------------------------------- platform


def detect_platform_key(sys_name: str, machine: str) -> str:
    os_map = {"linux": "linux", "darwin": "darwin", "windows": "windows"}
    arch_map = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
    normalized_os = os_map.get(sys_name.lower())
    normalized_arch = arch_map.get(machine.lower())
    if normalized_os is None or normalized_arch is None:
        raise UpdaterError(f"Unsupported OS/architecture combination: {sys_name}-{machine}")
    return f"{normalized_os}-{normalized_arch}"


def read_sysctl(name: str) -> str | None:
    try:
        result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", name],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def detect_native_platform(
    sys_name: str | None = None,
    machine: str | None = None,
    sysctl: Callable[[str], str | None] = read_sysctl,
    libc: Callable[[], str] = lambda: platform.libc_ver()[0],
) -> NativePlatform:
    """Detect the hardware platform, seeing through Rosetta translation on macOS.

    ``platform.machine()`` reports ``x86_64`` for a translated process on Apple
    Silicon, so macOS asks the kernel whether the CPU supports arm64.
    """
    raw_sys = sys_name if sys_name is not None else platform.system()
    raw_machine = machine if machine is not None else os.uname().machine
    translated = False
    if raw_sys.lower() == "darwin":
        if sysctl("hw.optional.arm64") == "1":
            raw_machine = "arm64"
        translated = sysctl("sysctl.proc_translated") == "1"
    elif raw_sys.lower() == "linux":
        libc_name = libc()
        if libc_name != "glibc":
            raise UpdaterError(
                f"Unsupported Linux C library {libc_name or 'unknown'}; "
                + "the published Linux builds require glibc"
            )
    return NativePlatform(key=detect_platform_key(raw_sys, raw_machine), translated=translated)


def executable_names(platform_key: str) -> tuple[str, str]:
    if platform_key.startswith("windows-"):
        return ("agy_acp_server.exe", "localharness_external.exe")
    return ("agy_acp_server.par", "localharness_external")


def release_dir_name(version: str, platform_key: str) -> str:
    return f"{version}-{platform_key}"


# -------------------------------------------------------------- binary formats


def inspect_binary(path: Path) -> BinaryFormat:
    """Identify the executable container and CPU architectures from file headers."""
    with path.open("rb") as fh:
        header = fh.read(4096)
    if header[:4] == b"\x7fELF" and len(header) >= 20:
        machine = int.from_bytes(header[18:20], "little" if header[5] == 1 else "big")
        bits = "64" if header[4] == 2 else "32"
        arch = ELF_MACHINES.get(machine, f"elf-machine-{machine}") if bits == "64" else "elf32"
        return BinaryFormat(kind="elf", archs=(arch,))
    if header[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe") and len(header) >= 8:
        (cpu_type,) = struct.unpack_from("<I", header, 4)
        return BinaryFormat(kind="macho", archs=(MACHO_CPU_TYPES.get(cpu_type, f"cpu-{cpu_type:#x}"),))
    if header[:4] == b"\xca\xfe\xba\xbe" and len(header) >= 8:
        (count,) = struct.unpack_from(">I", header, 4)
        if 0 < count <= 16 and len(header) >= 8 + 20 * count:
            archs: list[str] = []
            for index in range(count):
                (cpu_type,) = struct.unpack_from(">I", header, 8 + 20 * index)
                archs.append(MACHO_CPU_TYPES.get(cpu_type, f"cpu-{cpu_type:#x}"))
            return BinaryFormat(kind="macho", archs=tuple(archs))
    if header[:2] == b"MZ" and len(header) >= 64:
        (pe_offset,) = struct.unpack_from("<I", header, 60)
        if header[pe_offset : pe_offset + 4] == b"PE\x00\x00":
            (machine,) = struct.unpack_from("<H", header, pe_offset + 4)
            return BinaryFormat(kind="pe", archs=(PE_MACHINES.get(machine, f"pe-{machine:#x}"),))
    return BinaryFormat(kind="unknown", archs=())


def verify_binary_platform(path: Path, platform_key: str) -> BinaryFormat:
    os_name, _, arch = platform_key.partition("-")
    found = inspect_binary(path)
    expected_kind = EXPECTED_FORMAT.get(os_name)
    if found.kind != expected_kind or arch not in found.archs:
        raise UpdaterError(
            f"{path.name} is {found.kind}/{','.join(found.archs) or '?'}, "
            + f"expected {expected_kind}/{arch} for {platform_key}"
        )
    return found


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


# -------------------------------------------------------------- launch scripts


def render_release_launch(version: str, platform_key: str, exe_name: str, args: tuple[str, ...]) -> str:
    """Release-local launcher: pin the sibling harness, add missing registry args, exec ACP.

    T3 already passes ``--uid=`` on Linux, so a registry argument is added only
    when the caller did not supply it. A ``--flag=`` argument counts as present
    whenever any argument starts with ``--flag=``.
    """
    lines = [
        "#!/bin/sh",
        f"# Antigravity ACP {version} ({platform_key}) release launcher.",
        "# Generated by update_antigravity_acp.py. Writes nothing to stdout.",
        'dir=$(CDPATH= cd -P -- "$(dirname -- "$0")" && pwd -P) || exit 127',
        'ANTIGRAVITY_HARNESS_PATH="$dir/localharness_external"',
        "export ANTIGRAVITY_HARNESS_PATH",
    ]
    for index, arg in reversed(list(enumerate(args))):
        prefix, sep, _ = arg.partition("=")
        pattern = f"{shlex.quote(prefix + sep)}*" if sep else shlex.quote(arg)
        flag = f"have_arg_{index}"
        lines += [
            f"{flag}=",
            'for arg in "$@"; do',
            f"  case $arg in {pattern}) {flag}=1 ;; esac",
            "done",
            f'[ -n "${flag}" ] || set -- {shlex.quote(arg)} "$@"',
        ]
    lines.append(f'exec "$dir/{exe_name}" "$@"')
    return "\n".join(lines) + "\n"


def render_root_launcher(install_root: Path) -> str:
    current = shlex.quote(str(install_root / CURRENT_LINK))
    return (
        "#!/bin/sh\n"
        + "# Stable Antigravity ACP launcher managed by update_antigravity_acp.py.\n"
        + "# Resolves the active release at launch time. Writes nothing to stdout.\n"
        + f"current=$(CDPATH= cd -P -- {current} 2>/dev/null && pwd -P) || {{\n"
        + f"  echo \"antigravity-acp: no active release at \"{current} >&2\n"
        + "  exit 127\n"
        + "}\n"
        + f'exec "$current/{RELEASE_LAUNCH_NAME}" "$@"\n'
    )


# ------------------------------------------------------------------- manifests


def read_manifest(directory: Path) -> dict[str, object] | None:
    try:
        return parse_json_object((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def validate_release(directory: Path, platform_key: str | None = None) -> InstalledRelease:
    """Re-verify a release directory against its manifest; raise UpdaterError if unusable."""
    manifest = read_manifest(directory)
    if manifest is None:
        raise UpdaterError(f"{directory.name}: no readable {MANIFEST_NAME}")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise UpdaterError(f"{directory.name}: unsupported manifest schema")
    recorded_platform = as_str(manifest.get("platform_key"))
    version = as_str(manifest.get("version"))
    if not recorded_platform or not version:
        raise UpdaterError(f"{directory.name}: manifest lacks platform or version")
    if platform_key is not None and recorded_platform != platform_key:
        raise UpdaterError(
            f"{directory.name}: built for {recorded_platform}, this host needs {platform_key}"
        )
    files = as_dict(manifest.get("files"))
    exe_name, harness_name = executable_names(recorded_platform)
    for name in (exe_name, harness_name, RELEASE_LAUNCH_NAME):
        record = as_dict(files.get(name))
        path = directory / name
        if not is_executable_file(path):
            raise UpdaterError(f"{directory.name}: {name} is missing or not executable")
        if path.stat().st_size != record.get("bytes") or sha256_file(path) != record.get("sha256"):
            raise UpdaterError(f"{directory.name}: {name} does not match its manifest hash")
    for name in (exe_name, harness_name):
        _ = verify_binary_platform(directory / name, recorded_platform)
    return InstalledRelease(
        name=directory.name,
        directory=directory,
        version=version,
        platform_key=recorded_platform,
        manifest=manifest,
    )


def list_release_dirs(install_root: Path) -> list[Path]:
    releases = install_root / RELEASES_DIR
    if not releases.is_dir():
        return []
    return sorted(
        entry
        for entry in releases.iterdir()
        if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".")
    )


def find_reusable_release(
    install_root: Path,
    version: str,
    platform_key: str,
) -> InstalledRelease | None:
    """Return a verified release for this version and platform, preferring the active one."""
    current = link_target(install_root, CURRENT_LINK)
    candidates = list_release_dirs(install_root)
    candidates.sort(key=lambda entry: entry != current)
    for entry in candidates:
        try:
            release = validate_release(entry, platform_key)
        except UpdaterError:
            continue
        if release.version == version:
            return release
    return None


def find_legacy_releases(install_root: Path) -> list[LegacyRelease]:
    """Pre-manifest ``antigravity-acp-<version>`` directories; reported, never reused."""
    if not install_root.is_dir():
        return []
    found: list[LegacyRelease] = []
    for entry in sorted(install_root.iterdir()):
        if entry.is_symlink() or not entry.is_dir() or not entry.name.startswith(LEGACY_DIR_PREFIX):
            continue
        version = entry.name.removeprefix(LEGACY_DIR_PREFIX)
        if version and version[0].isdigit():
            found.append(LegacyRelease(version=version, directory=entry))
    return found


def version_sort_key(version: str) -> tuple[tuple[int, int | str], ...]:
    parts: list[tuple[int, int | str]] = []
    for token in version.replace("-", ".").split("."):
        if token.isdigit():
            parts.append((0, int(token)))
        else:
            parts.append((1, token))
    return tuple(parts)


def detect_t3_managed_version(managed_root: Path) -> str | None:
    if not managed_root.is_dir():
        return None
    for arch_dir in sorted(managed_root.iterdir()):
        active_file = arch_dir / "active.json"
        if not active_file.is_file():
            continue
        try:
            active_data = parse_json_object(active_file.read_text(encoding="utf-8"))
            release_id = as_str(active_data.get("releaseId"))
            if not release_id:
                continue
            install_record = arch_dir / "versions" / release_id / ".install-complete.json"
            if not install_record.is_file():
                continue
            record_data = parse_json_object(install_record.read_text(encoding="utf-8"))
            version = as_str(record_data.get("version"))
            if version:
                return version
        except (OSError, ValueError):
            continue
    return None


# ------------------------------------------------------------------------ lock


@contextmanager
def install_lock(install_root: Path) -> Generator[None, None, None]:
    install_root.mkdir(parents=True, exist_ok=True)
    fd = os.open(install_root / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusyError(
                f"Another updater is running for {install_root} (lock {LOCK_NAME} is held)"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------- network


@contextmanager
def open_http_response(url: str, timeout: float) -> Generator[http.client.HTTPResponse, None, None]:
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlsplit(current_url)
        # https only, redirects included: a plaintext hop could swap the body.
        if parsed.scheme != "https" or not parsed.netloc:
            raise UpdaterError(f"Refusing non-https or invalid URL: {current_url}")
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn = http.client.HTTPSConnection(parsed.netloc, timeout=timeout)
        try:
            conn.request("GET", path, headers={"User-Agent": USER_AGENT})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                response.close()
                conn.close()
                if not location:
                    raise UpdaterError(f"Redirect ({response.status}) missing Location header")
                current_url = urllib.parse.urljoin(current_url, location)
                continue
            if response.status < 200 or response.status >= 300:
                status = response.status
                reason = response.reason
                response.close()
                conn.close()
                raise UpdaterError(f"HTTP {status} {reason} when fetching {current_url}")
            try:
                yield response
            finally:
                response.close()
                conn.close()
            return
        except BaseException:
            conn.close()
            raise
    raise UpdaterError(f"Too many redirects while fetching {url}")


def download_file(url: str, destination: Path, timeout: float = 120.0) -> None:
    show_progress = sys.stdout.isatty()
    with open_http_response(url, timeout=timeout) as response:
        total_raw = response.getheader("Content-Length")
        total_bytes = int(total_raw) if total_raw is not None and total_raw.isdigit() else 0
        free = shutil.disk_usage(destination.parent).free
        if total_bytes and free < total_bytes * 3:
            raise UpdaterError(
                f"Not enough free space in {destination.parent}: "
                + f"{free // 2**20} MiB free, about {total_bytes * 3 // 2**20} MiB needed"
            )
        downloaded = 0
        with destination.open("wb") as out_file:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                _ = out_file.write(chunk)
                downloaded += len(chunk)
                if show_progress and total_bytes > 0:
                    pct = (downloaded / total_bytes) * 100.0
                    _ = sys.stdout.write(
                        f"\r  Downloading: {downloaded / 2**20:6.1f} / "
                        + f"{total_bytes / 2**20:6.1f} MiB ({pct:5.1f}%)"
                    )
                    _ = sys.stdout.flush()
        if show_progress and total_bytes > 0:
            _ = sys.stdout.write("\n")
        if total_bytes and downloaded != total_bytes:
            raise UpdaterError(f"Download truncated: {downloaded} of {total_bytes} bytes")


# ------------------------------------------------------------------ extraction


def safe_extract(archive_path: Path, destination: Path, expected: tuple[str, ...]) -> dict[str, str]:
    """Extract exactly the expected top-level files, rejecting unsafe or unexpected members.

    Returns the SHA-256 of each extracted file.
    """
    hashes: dict[str, str] = {}
    with zipfile.ZipFile(archive_path, "r") as zf:
        members = zf.infolist()
        total = 0
        for info in members:
            name = info.filename
            mode = (info.external_attr >> 16) & 0o170000
            if info.is_dir():
                raise UpdaterError(f"Archive contains an unexpected directory: {name!r}")
            if name.startswith(("/", "\\")) or "\\" in name or ".." in name.split("/") or "/" in name:
                raise UpdaterError(f"Archive member has an unsafe path: {name!r}")
            if mode == stat.S_IFLNK:
                raise UpdaterError(f"Archive member is a symbolic link: {name!r}")
            if name not in expected:
                raise UpdaterError(f"Archive contains an unexpected file: {name!r}")
            if name in hashes:
                raise UpdaterError(f"Archive lists {name!r} more than once")
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise UpdaterError("Archive expands beyond the allowed size")
            hashes[name] = ""
        missing = [name for name in expected if name not in hashes]
        if missing:
            raise UpdaterError(f"Archive is missing {', '.join(missing)}")
        for info in members:
            target = destination / info.filename
            digest = hashlib.sha256()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
            with zf.open(info) as src, os.fdopen(fd, "wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
                    _ = dst.write(chunk)
            target.chmod(0o755)
            hashes[info.filename] = digest.hexdigest()
    return hashes


# ------------------------------------------------------------------ smoke test


def read_line_with_timeout(stream: IO[bytes], timeout: float) -> bytes:
    selector = selectors.DefaultSelector()
    _ = selector.register(stream, selectors.EVENT_READ)
    buffer = b""
    deadline = time.monotonic() + timeout
    try:
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise UpdaterError("ACP smoke test timed out waiting for a response")
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
    finally:
        selector.close()
    return buffer.split(b"\n", 1)[0]


def acp_initialize_smoke_test(release_dir: Path, scratch_dir: Path) -> dict[str, object]:
    """Start ACP through its release launcher and require a valid ``initialize`` reply.

    Runs with a scratch profile and no credentials, so it proves only that the
    native binaries execute on this host and speak ACP.
    """
    tmp_dir = scratch_dir / "tmp"
    gemini_home = scratch_dir / "gemini-home"
    tmp_dir.mkdir(mode=0o700)
    gemini_home.mkdir(mode=0o700)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(Path.home()),
        "TMPDIR": str(tmp_dir),
        "GEMINI_HOME": str(gemini_home),
        "AGY_ACP_FORCE_FILE_STORAGE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    request: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": 1, "clientCapabilities": {}},
    }
    proc = subprocess.Popen(
        [str(release_dir / RELEASE_LAUNCH_NAME)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=scratch_dir,
    )
    try:
        stdin = cast("IO[bytes]", proc.stdin)
        stdout = cast("IO[bytes]", proc.stdout)
        _ = stdin.write((json.dumps(request) + "\n").encode())
        stdin.flush()
        line = read_line_with_timeout(stdout, SMOKE_TEST_TIMEOUT_SECONDS)
    finally:
        proc.kill()
        stderr_tail = cast("IO[bytes]", proc.stderr).read()[-800:].decode("utf-8", "replace")
        _ = proc.wait()
    try:
        reply = parse_json_object(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdaterError(
            f"ACP smoke test got no JSON-RPC reply on stdout; stderr tail: {stderr_tail.strip()}"
        ) from None
    result = as_dict(reply.get("result"))
    if reply.get("id") != 1 or "protocolVersion" not in result:
        raise UpdaterError(f"ACP smoke test got an unexpected reply: {line[:300]!r}")
    return {"method": "acp-initialize", "protocol_version": result["protocolVersion"], "passed": True}


SmokeTest = Callable[[Path, Path], dict[str, object]]


# --------------------------------------------------------------------- install


def install_release(
    archive_path: Path,
    snapshot: RegistrySnapshot,
    spec: PlatformBinarySpec,
    install_root: Path,
    native: NativePlatform,
    smoke_test: SmokeTest,
) -> InstalledRelease:
    """Stage, verify, smoke-test, then atomically promote a new release directory.

    Any failure removes only the staging directory; existing releases and the
    active link are never touched here.
    """
    platform_key = spec.platform_key
    exe_name, harness_name = executable_names(platform_key)
    if spec.cmd.removeprefix("./") != exe_name:
        raise UpdaterError(f"Registry cmd {spec.cmd!r} does not name {exe_name}")
    releases = install_root / RELEASES_DIR
    releases.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{snapshot.release.version}-", dir=releases))
    try:
        payload = staging / "payload"
        scratch = staging / "scratch"
        payload.mkdir()
        scratch.mkdir(mode=0o700)
        verify_archive_digest(archive_path, spec)
        hashes = safe_extract(archive_path, payload, (exe_name, harness_name))
        formats = {name: verify_binary_platform(payload / name, platform_key) for name in hashes}
        launch_text = render_release_launch(snapshot.release.version, platform_key, exe_name, spec.args)
        launch_path = payload / RELEASE_LAUNCH_NAME
        _ = launch_path.write_text(launch_text, encoding="utf-8")
        launch_path.chmod(0o755)
        _ = (payload / REGISTRY_COPY_NAME).write_bytes(snapshot.raw)
        smoke = smoke_test(payload, scratch)
        files: dict[str, object] = {
            name: {
                "sha256": hashes[name],
                "bytes": (payload / name).stat().st_size,
                "format": formats[name].kind,
                "archs": list(formats[name].archs),
            }
            for name in (exe_name, harness_name)
        }
        files[RELEASE_LAUNCH_NAME] = {
            "sha256": hashlib.sha256(launch_text.encode()).hexdigest(),
            "bytes": launch_path.stat().st_size,
        }
        manifest: dict[str, object] = {
            "schema": MANIFEST_SCHEMA,
            "installer": USER_AGENT,
            "agent_id": snapshot.release.agent_id,
            "name": snapshot.release.name,
            "version": snapshot.release.version,
            "platform_key": platform_key,
            "native_detection": {"key": native.key, "rosetta_translated": native.translated},
            "archive_url": spec.archive_url,
            "archive_sha256": sha256_file(archive_path),
            "archive_bytes": archive_path.stat().st_size,
            "registry_source": snapshot.source,
            "registry_sha256": snapshot.sha256,
            "cmd": spec.cmd,
            "args": list(spec.args),
            "files": files,
            "smoke_test": smoke,
            "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "integrity_note": INTEGRITY_NOTE,
        }
        _ = (payload / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        final = releases / release_dir_name(snapshot.release.version, platform_key)
        if final.exists() or final.is_symlink():
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            final = final.with_name(f"{final.name}-{stamp}")
        _ = payload.rename(final)
        return validate_release(final, platform_key)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ------------------------------------------------------------------ activation


def link_target(install_root: Path, link_name: str) -> Path | None:
    link = install_root / link_name
    if not link.is_symlink():
        return None
    return (install_root / os.readlink(link)).resolve()


def atomic_symlink(link: Path, target: str) -> None:
    if link.is_symlink() and os.readlink(link) == target:
        return
    if link.exists() and not link.is_symlink():
        raise UpdaterError(f"{link} exists and is not a symlink; refusing to replace it")
    temp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
    if temp.is_symlink() or temp.exists():
        temp.unlink()
    temp.symlink_to(target)
    os.replace(temp, link)


def atomic_write_executable(path: Path, text: str) -> None:
    if path.is_file() and not path.is_symlink() and path.read_text(encoding="utf-8") == text:
        return
    if path.exists() and (path.is_dir() or path.is_symlink()):
        raise UpdaterError(f"{path} exists and is not a regular file; refusing to replace it")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _ = fh.write(text)
        os.chmod(temp_name, 0o755)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def activate_release(install_root: Path, release: InstalledRelease) -> bool:
    """Point the stable launcher at ``release``. Returns False when already active."""
    _, harness_name = executable_names(release.platform_key)
    atomic_write_executable(install_root / LAUNCHER_NAME, render_root_launcher(install_root))
    # T3 requires the harness beside the executable it resolves; route it through current.
    atomic_symlink(install_root / harness_name, f"{CURRENT_LINK}/{harness_name}")
    current = link_target(install_root, CURRENT_LINK)
    if current == release.directory.resolve():
        return False
    if current is not None:
        atomic_symlink(install_root / PREVIOUS_LINK, os.readlink(install_root / CURRENT_LINK))
    atomic_symlink(install_root / CURRENT_LINK, f"{RELEASES_DIR}/{release.directory.name}")
    return True


# --------------------------------------------------------------------- pruning


def prompt_yes_no(
    question: str,
    default: bool = False,
    input_fn: Callable[[str], str] = input,
) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        try:
            answer = input_fn(question + suffix).strip().lower()
        except EOFError:
            return default
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def prune_releases(
    install_root: Path,
    assume_yes: bool,
    input_fn: Callable[[str], str] = input,
) -> list[Path]:
    """Delete release directories other than the current and previous ones.

    Only directories under ``releases/`` are candidates; legacy directories and
    everything else in the install root are left alone.
    """
    keep = {link_target(install_root, CURRENT_LINK), link_target(install_root, PREVIOUS_LINK)}
    deleted: list[Path] = []
    for entry in list_release_dirs(install_root):
        if entry.resolve() in keep:
            continue
        if assume_yes or prompt_yes_no(f"Delete release {entry.name}?", input_fn=input_fn):
            shutil.rmtree(entry)
            deleted.append(entry)
            print(f"  Deleted {entry}")
        else:
            print(f"  Kept {entry}")
    return deleted


# ------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install and activate the Antigravity ACP release from the ACP registry. "
            + "Old releases are kept for rollback; deletion happens only with --prune."
        )
    )
    _ = parser.add_argument(
        "--install-root",
        type=Path,
        default=DEFAULT_INSTALL_ROOT,
        help=f"Directory for custom installations (default: {DEFAULT_INSTALL_ROOT})",
    )
    _ = parser.add_argument(
        "--registry",
        "--registry-url",
        dest="registry",
        default=REGISTRY_URL,
        help="Registry agent.json URL or local snapshot file",
    )
    _ = parser.add_argument(
        "--platform",
        dest="platform_key",
        default=None,
        help="Platform key to report with --check-only; installs must match the native platform",
    )
    action = parser.add_mutually_exclusive_group()
    _ = action.add_argument("--check-only", action="store_true", help="Report status only")
    _ = action.add_argument("--list", action="store_true", help="List releases and links")
    _ = action.add_argument("--activate", metavar="RELEASE", help="Activate releases/RELEASE")
    _ = action.add_argument("--rollback", action="store_true", help="Reactivate the previous release")
    _ = action.add_argument(
        "--prune",
        action="store_true",
        help="Delete releases other than current and previous (asks unless --yes)",
    )
    _ = parser.add_argument(
        "--force",
        action="store_true",
        help="Install a fresh copy into a new release directory even if a valid one exists",
    )
    _ = parser.add_argument(
        "--no-activate",
        action="store_true",
        help="Install and verify without switching the active release",
    )
    _ = parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm installation and activation (never implies --prune)",
    )
    return parser


def describe(install_root: Path) -> None:
    current = link_target(install_root, CURRENT_LINK)
    previous = link_target(install_root, PREVIOUS_LINK)
    print(f"  Launcher        : {install_root / LAUNCHER_NAME}")
    print(f"  Current release : {current.name if current else 'none'}")
    print(f"  Previous release: {previous.name if previous else 'none'}")
    for entry in list_release_dirs(install_root):
        try:
            release = validate_release(entry)
            state = f"verified {release.version} {release.platform_key}"
        except UpdaterError as error:
            state = f"INVALID: {error}"
        print(f"    releases/{entry.name}: {state}")
    for legacy in find_legacy_releases(install_root):
        print(f"    {legacy.directory.name}: legacy layout, no manifest (kept, never reused)")


def run_cli(
    argv: list[str] | None = None,
    input_fn: Callable[[str], str] = input,
    native: NativePlatform | None = None,
    downloader: Callable[[str, Path], None] = download_file,
    smoke_test: SmokeTest = acp_initialize_smoke_test,
) -> int:
    args = build_parser().parse_args(argv)
    install_root = cast("Path", args.install_root).expanduser().resolve()
    registry_source = cast("str", args.registry)
    platform_override = cast("str | None", args.platform_key)
    assume_yes = cast("bool", args.yes)

    if cast("bool", args.list):
        describe(install_root)
        return 0

    with install_lock(install_root):
        if cast("bool", args.prune):
            deleted = prune_releases(install_root, assume_yes=assume_yes, input_fn=input_fn)
            print(f"Pruned {len(deleted)} release(s).")
            return 0
        if cast("bool", args.rollback) or cast("str | None", args.activate):
            if cast("bool", args.rollback):
                target = link_target(install_root, PREVIOUS_LINK)
                if target is None:
                    raise UpdaterError("No previous release recorded")
            else:
                target = install_root / RELEASES_DIR / cast("str", args.activate)
            host = native or detect_native_platform()
            release = validate_release(target, host.key)
            changed = activate_release(install_root, release)
            print(f"{'Activated' if changed else 'Already active'}: {release.name}")
            print(f"Binary path: {install_root / LAUNCHER_NAME}")
            return 0

        host = native or detect_native_platform()
        platform_key = platform_override or host.key
        check_only = cast("bool", args.check_only)
        if platform_key != host.key and not check_only:
            raise UpdaterError(
                f"Refusing to install {platform_key} binaries on a {host.key} host"
            )

        print(f"Reading ACP registry ({registry_source}) ...")
        snapshot = load_registry(registry_source)
        spec = snapshot.release.binaries.get(platform_key)
        if spec is None:
            available = ", ".join(sorted(snapshot.release.binaries))
            raise UpdaterError(f"No binary listed for {platform_key}. Available: {available}")

        print(f"\nAntigravity ACP status ({platform_key}{', Rosetta shell' if host.translated else ''}):")
        managed = detect_t3_managed_version(DEFAULT_T3_MANAGED_ROOT)
        if managed is not None:
            print(f"  T3 managed version: {managed}")
        print(f"  Registry version  : {snapshot.release.version} (sha256 {snapshot.sha256})")
        describe(install_root)
        if check_only:
            return 0

        reusable = None if cast("bool", args.force) else find_reusable_release(
            install_root, snapshot.release.version, platform_key
        )
        if reusable is not None:
            print(f"\nVerified release already installed: {reusable.name}")
            release = reusable
        else:
            if not (
                assume_yes
                or prompt_yes_no(
                    f"\nInstall Antigravity ACP {snapshot.release.version} ({platform_key})?",
                    default=True,
                    input_fn=input_fn,
                )
            ):
                print("Aborted.")
                return 0
            releases = install_root / RELEASES_DIR
            releases.mkdir(parents=True, exist_ok=True)
            fd, archive_name = tempfile.mkstemp(prefix=f"{STAGING_PREFIX}archive-", suffix=".zip", dir=releases)
            os.close(fd)
            archive = Path(archive_name)
            try:
                print(f"Fetching {spec.archive_url} ...")
                downloader(spec.archive_url, archive)
                release = install_release(archive, snapshot, spec, install_root, host, smoke_test)
            finally:
                archive.unlink(missing_ok=True)
            print(f"Installed and smoke-tested: releases/{release.name}")

        if cast("bool", args.no_activate):
            print("Not activated (--no-activate).")
            return 0
        if link_target(install_root, CURRENT_LINK) != release.directory.resolve() and not (
            assume_yes
            or prompt_yes_no(f"Activate {release.name}?", default=True, input_fn=input_fn)
        ):
            print("Not activated.")
            return 0
        changed = activate_release(install_root, release)
        print(f"{'Activated' if changed else 'Already active'}: {release.name}")
        print(f"Binary path for T3 Code: {install_root / LAUNCHER_NAME}")
        return 0


def main() -> int:
    try:
        return run_cli()
    except LockBusyError as error:
        print(f"Error: {error}", file=sys.stderr)
        return EXIT_LOCK_BUSY
    except UpdaterError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
