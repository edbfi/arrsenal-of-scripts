"""Behavioural tests for update_antigravity_acp."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import struct
import subprocess
import tempfile
import unittest
import zipfile
from collections.abc import Callable, Generator
from pathlib import Path

from update_antigravity_acp import (
    CURRENT_LINK,
    LAUNCHER_NAME,
    PREVIOUS_LINK,
    RELEASES_DIR,
    LockBusyError,
    NativePlatform,
    UpdaterError,
    find_reusable_release,
    inspect_binary,
    install_lock,
    open_http_response,
    link_target,
    as_dict,
    parse_json_object,
    parse_registry_release,
    render_release_launch,
    render_root_launcher,
    run_cli,
    safe_extract,
)

LINUX_X64 = NativePlatform(key="linux-x86_64", translated=False)
LINUX_ARM = NativePlatform(key="linux-aarch64", translated=False)
DARWIN_ARM = NativePlatform(key="darwin-aarch64", translated=False)


def elf(arch: str) -> bytes:
    machine = {"x86_64": 62, "aarch64": 183}[arch]
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9) + struct.pack("<HH", 2, machine)
    return header + bytes(64)


def macho(arch: str) -> bytes:
    cpu = {"x86_64": 0x01000007, "aarch64": 0x0100000C}[arch]
    return b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu) + bytes(64)


ARCHIVE_URLS = {
    "linux-x86_64": "https://example.invalid/x64.zip",
    "linux-aarch64": "https://example.invalid/arm64.zip",
    "darwin-aarch64": "https://example.invalid/darwin-arm64.zip",
}


def native_header(platform_key: str) -> bytes:
    return {
        "linux-x86_64": elf("x86_64"),
        "linux-aarch64": elf("aarch64"),
        "darwin-aarch64": macho("aarch64"),
    }[platform_key]


def archive_bytes(header: bytes) -> bytes:
    """A release zip; fixed timestamps keep its digest stable."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name in ("agy_acp_server.par", "localharness_external"):
            zf.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), header)
    return buffer.getvalue()


def registry_json(
    version: str,
    platform_key: str = "linux-x86_64",
    header: bytes | None = None,
    digest: str | None = None,
) -> str:
    """The registry entry; it publishes the digest of the archive the fake download serves."""
    binary: dict[str, dict[str, object]] = {}
    for key, url in ARCHIVE_URLS.items():
        if key != platform_key and platform_key != "all":
            continue
        published = hashlib.sha256(archive_bytes(header or native_header(key))).hexdigest()
        entry: dict[str, object] = {
            "archive": url,
            "cmd": "./agy_acp_server.par",
            "sha256": published if digest is None else digest,
        }
        if key.startswith("linux-"):
            entry["args"] = ["--uid="]
        binary[key] = entry
    return json.dumps(
        {"id": "antigravity-acp", "name": "Google Antigravity", "version": version,
         "distribution": {"binary": binary}}
    )


def fake_archive(header: bytes | None) -> Callable[[str, Path], None]:
    def download(url: str, destination: Path) -> None:
        platform_key = next(k for k, v in ARCHIVE_URLS.items() if v == url)
        _ = destination.write_bytes(archive_bytes(header or native_header(platform_key)))

    return download


def passing_smoke(_release: Path, _scratch: Path) -> dict[str, object]:
    return {"method": "fake", "passed": True}


def failing_smoke(_release: Path, _scratch: Path) -> dict[str, object]:
    raise UpdaterError("smoke test failed")


def load_json(path: Path) -> dict[str, object]:
    return parse_json_object(path.read_text(encoding="utf-8"))


class Sandbox:
    """A throwaway install root plus a scripted updater run."""

    def __init__(self, base: Path) -> None:
        self.base: Path = base
        self.root: Path = base / "custom-tools"
        self.downloads: list[str] = []

    def registry(
        self,
        version: str,
        platform_key: str = "linux-x86_64",
        header: bytes | None = None,
        digest: str | None = None,
    ) -> Path:
        path = self.base / f"registry-{version}-{platform_key}.json"
        _ = path.write_text(registry_json(version, platform_key, header, digest), encoding="utf-8")
        return path

    def run_updater(
        self,
        *args: str,
        native: NativePlatform = LINUX_X64,
        header: bytes | None = None,
        smoke: Callable[[Path, Path], dict[str, object]] = passing_smoke,
        answers: list[str] | None = None,
    ) -> tuple[int, str]:
        archive = fake_archive(header)

        def download(url: str, destination: Path) -> None:
            self.downloads.append(url)
            archive(url, destination)

        replies = iter(answers or [])

        def answer(_prompt: str) -> str:
            return next(replies)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_cli(
                ["--install-root", str(self.root), *args],
                input_fn=answer,
                native=native,
                downloader=download,
                smoke_test=smoke,
            )
        return code, out.getvalue()

    def current(self) -> str | None:
        target = link_target(self.root, CURRENT_LINK)
        return target.name if target else None

    def release_names(self) -> list[str]:
        releases = self.root / RELEASES_DIR
        return sorted(p.name for p in releases.iterdir()) if releases.is_dir() else []


@contextlib.contextmanager
def sandbox() -> Generator[Sandbox, None, None]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Sandbox(Path(tmp).resolve())


class PlatformDetectionTests(unittest.TestCase):
    def detect(self, sys_name: str, machine: str, sysctl: dict[str, str] | None = None) -> NativePlatform:
        from update_antigravity_acp import detect_native_platform

        values = sysctl or {}
        return detect_native_platform(sys_name, machine, sysctl=values.get, libc=lambda: "glibc")

    def test_linux_x64_and_arm64(self) -> None:
        self.assertEqual(self.detect("Linux", "x86_64").key, "linux-x86_64")
        self.assertEqual(self.detect("Linux", "aarch64").key, "linux-aarch64")

    def test_macos_apple_silicon_native(self) -> None:
        native = self.detect("Darwin", "arm64", {"hw.optional.arm64": "1", "sysctl.proc_translated": "0"})
        self.assertEqual(native, NativePlatform("darwin-aarch64", translated=False))

    def test_rosetta_shell_still_selects_arm64(self) -> None:
        native = self.detect("Darwin", "x86_64", {"hw.optional.arm64": "1", "sysctl.proc_translated": "1"})
        self.assertEqual(native, NativePlatform("darwin-aarch64", translated=True))

    def test_intel_mac_stays_x86_64(self) -> None:
        self.assertEqual(self.detect("Darwin", "x86_64", {}).key, "darwin-x86_64")

    def test_non_glibc_linux_is_unsupported(self) -> None:
        from update_antigravity_acp import detect_native_platform

        with self.assertRaises(UpdaterError):
            _ = detect_native_platform("Linux", "x86_64", libc=lambda: "")

    def test_binary_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bin"
            for data, kind, arch in (
                (elf("x86_64"), "elf", "x86_64"),
                (elf("aarch64"), "elf", "aarch64"),
                (macho("aarch64"), "macho", "aarch64"),
            ):
                _ = path.write_bytes(data)
                found = inspect_binary(path)
                self.assertEqual((found.kind, found.archs), (kind, (arch,)))


class LaunchScriptTests(unittest.TestCase):
    """Run the generated launchers against a stand-in ACP that reports its argv and env."""

    def make_release(self, root: Path, args: tuple[str, ...]) -> Path:
        release = root / RELEASES_DIR / "1.0.0-test"
        release.mkdir(parents=True)
        fake = release / "agy_acp_server.par"
        _ = fake.write_text(
            '#!/bin/sh\nfor a in "$@"; do printf "arg:%s\\n" "$a"; done\n'
            + 'printf "harness:%s\\n" "$ANTIGRAVITY_HARNESS_PATH"\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
        launch = release / "launch"
        _ = launch.write_text(render_release_launch("1.0.0", "linux-x86_64", "agy_acp_server.par", args))
        launch.chmod(0o755)
        (root / CURRENT_LINK).symlink_to(f"{RELEASES_DIR}/{release.name}")
        launcher = root / LAUNCHER_NAME
        _ = launcher.write_text(render_root_launcher(root), encoding="utf-8")
        launcher.chmod(0o755)
        return release

    def launch(self, root: Path, *argv: str, env: dict[str, str] | None = None) -> list[str]:
        result = subprocess.run(
            [str(root / LAUNCHER_NAME), *argv],
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", **(env or {})},
        )
        self.assertEqual(result.stderr, "")
        return result.stdout.splitlines()

    def test_registry_arg_added_once_and_arguments_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            release = self.make_release(root, ("--uid=",))
            harness = f"harness:{release}/localharness_external"
            self.assertEqual(self.launch(root), ["arg:--uid=", harness])
            # T3 already passes --uid= on Linux: it must not be duplicated.
            self.assertEqual(self.launch(root, "--uid=", "a b"), ["arg:--uid=", "arg:a b", harness])
            self.assertEqual(self.launch(root, "--uid=42"), ["arg:--uid=42", harness])

    def test_no_registry_args_on_macos_and_harness_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            release = self.make_release(root, ())
            lines = self.launch(root, "--debug", env={"ANTIGRAVITY_HARNESS_PATH": "/elsewhere"})
            self.assertEqual(lines, ["arg:--debug", f"harness:{release}/localharness_external"])

    def test_missing_current_fails_without_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            launcher = root / LAUNCHER_NAME
            _ = launcher.write_text(render_root_launcher(root), encoding="utf-8")
            launcher.chmod(0o755)
            result = subprocess.run([str(launcher)], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 127)
            self.assertEqual(result.stdout, "")
            self.assertIn("no active release", result.stderr)


class InstallTests(unittest.TestCase):
    def test_install_activates_and_records_provenance(self) -> None:
        with sandbox() as sb:
            code, out = sb.run_updater("--registry", str(sb.registry("1.4.1")), "--yes")
            self.assertEqual(code, 0)
            self.assertEqual(sb.current(), "1.4.1-linux-x86_64")
            self.assertIn(str(sb.root / LAUNCHER_NAME), out)
            manifest = load_json(sb.root / CURRENT_LINK / "install-manifest.json")
            self.assertEqual(manifest["platform_key"], "linux-x86_64")
            self.assertEqual(manifest["args"], ["--uid="])
            self.assertRegex(str(manifest["registry_sha256"]), r"^[0-9a-f]{64}$")
            self.assertEqual(as_dict(as_dict(manifest["files"])["agy_acp_server.par"])["archs"], ["x86_64"])
            harness = sb.root / "localharness_external"
            self.assertTrue(harness.is_symlink() and harness.resolve().is_file())

    def test_rerun_is_idempotent(self) -> None:
        with sandbox() as sb:
            registry = str(sb.registry("1.4.1"))
            _ = sb.run_updater("--registry", registry, "--yes")
            launcher_before = (sb.root / LAUNCHER_NAME).stat()
            code, out = sb.run_updater("--registry", registry, "--yes")
            self.assertEqual(code, 0)
            self.assertEqual(len(sb.downloads), 1)
            self.assertIn("Already active", out)
            self.assertEqual(sb.release_names(), ["1.4.1-linux-x86_64"])
            self.assertEqual((sb.root / LAUNCHER_NAME).stat().st_ino, launcher_before.st_ino)

    def test_wrong_architecture_same_version_is_not_reused(self) -> None:
        with sandbox() as sb:
            # A same-version release whose binaries are aarch64 but recorded for x86_64.
            _ = sb.run_updater("--registry", str(sb.registry("1.4.1", "linux-aarch64")), "--yes",
                                 native=LINUX_ARM, header=elf("aarch64"))
            arm_dir = sb.root / RELEASES_DIR / "1.4.1-linux-aarch64"
            manifest_path = arm_dir / "install-manifest.json"
            manifest = load_json(manifest_path)
            manifest["platform_key"] = "linux-x86_64"
            _ = manifest_path.write_text(json.dumps(manifest))
            self.assertIsNone(find_reusable_release(sb.root, "1.4.1", "linux-x86_64"))
            code, _ = sb.run_updater("--registry", str(sb.registry("1.4.1")), "--yes")
            self.assertEqual(code, 0)
            self.assertEqual(len(sb.downloads), 2)
            self.assertEqual(sb.current(), "1.4.1-linux-x86_64")

    def test_legacy_directory_without_manifest_is_not_reused(self) -> None:
        with sandbox() as sb:
            legacy = sb.root / "antigravity-acp-1.4.1"
            legacy.mkdir(parents=True)
            for name in ("agy_acp_server.par", "localharness_external"):
                _ = (legacy / name).write_bytes(elf("x86_64"))
                (legacy / name).chmod(0o755)
            code, _ = sb.run_updater("--registry", str(sb.registry("1.4.1")), "--yes")
            self.assertEqual(code, 0)
            self.assertEqual(len(sb.downloads), 1)
            self.assertTrue(legacy.is_dir())

    def test_archive_for_another_architecture_is_rejected(self) -> None:
        with sandbox() as sb:
            with self.assertRaises(UpdaterError):
                registry = sb.registry("1.4.1", header=elf("aarch64"))
                _ = sb.run_updater("--registry", str(registry), "--yes", header=elf("aarch64"))
            self.assertEqual(sb.release_names(), [])
            self.assertIsNone(sb.current())

    def test_refuses_foreign_platform_install(self) -> None:
        with sandbox() as sb:
            with self.assertRaises(UpdaterError):
                _ = sb.run_updater("--registry", str(sb.registry("1.4.1", "all")), "--yes",
                                     "--platform", "darwin-aarch64")

    def test_failed_install_preserves_current(self) -> None:
        with sandbox() as sb:
            _ = sb.run_updater("--registry", str(sb.registry("1.4.1")), "--yes")
            with self.assertRaises(UpdaterError):
                _ = sb.run_updater("--registry", str(sb.registry("1.5.0")), "--yes", smoke=failing_smoke)
            self.assertEqual(sb.current(), "1.4.1-linux-x86_64")
            self.assertEqual(sb.release_names(), ["1.4.1-linux-x86_64"])
            launched = subprocess.run(
                ["/bin/sh", "-c", f'cd -P "{sb.root / CURRENT_LINK}" && ls'],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("agy_acp_server.par", launched.stdout)

    def test_force_never_mutates_the_active_directory(self) -> None:
        with sandbox() as sb:
            registry = str(sb.registry("1.4.1"))
            _ = sb.run_updater("--registry", registry, "--yes")
            active = sb.root / RELEASES_DIR / "1.4.1-linux-x86_64"
            inode = (active / "agy_acp_server.par").stat().st_ino
            code, _ = sb.run_updater("--registry", registry, "--yes", "--force")
            self.assertEqual(code, 0)
            self.assertEqual((active / "agy_acp_server.par").stat().st_ino, inode)
            self.assertEqual(len(sb.release_names()), 2)
            self.assertEqual(link_target(sb.root, PREVIOUS_LINK), active.resolve())

    def test_macos_install_uses_macho_and_no_args(self) -> None:
        with sandbox() as sb:
            code, _ = sb.run_updater("--registry", str(sb.registry("1.4.1", "darwin-aarch64")), "--yes",
                                       native=DARWIN_ARM, header=macho("aarch64"))
            self.assertEqual(code, 0)
            launch = (sb.root / CURRENT_LINK / "launch").read_text()
            self.assertNotIn("--uid", launch)


    def test_archive_must_match_the_registry_digest(self) -> None:
        with sandbox() as sb:
            for digest, message in (("0" * 64, "sha256 mismatch"), ("", "no valid sha256")):
                with self.assertRaisesRegex(UpdaterError, message):
                    _ = sb.run_updater("--registry", str(sb.registry("1.4.1", digest=digest)), "--yes")
            self.assertEqual(sb.release_names(), [])
            self.assertIsNone(sb.current())


class UnattendedAndPruneTests(unittest.TestCase):
    def test_yes_upgrade_keeps_old_releases_and_rollback_works(self) -> None:
        with sandbox() as sb:
            _ = sb.run_updater("--registry", str(sb.registry("1.4.1")), "--yes")
            _ = sb.run_updater("--registry", str(sb.registry("1.5.0")), "--yes")
            self.assertEqual(sb.release_names(), ["1.4.1-linux-x86_64", "1.5.0-linux-x86_64"])
            self.assertEqual(sb.current(), "1.5.0-linux-x86_64")
            code, _ = sb.run_updater("--rollback")
            self.assertEqual(code, 0)
            self.assertEqual(sb.current(), "1.4.1-linux-x86_64")

    def test_prune_is_explicit_and_keeps_current_and_previous(self) -> None:
        with sandbox() as sb:
            for version in ("1.3.0", "1.4.1", "1.5.0"):
                _ = sb.run_updater("--registry", str(sb.registry(version)), "--yes")
            legacy = sb.root / "antigravity-acp-1.2.0"
            legacy.mkdir()
            self.assertEqual(len(sb.release_names()), 3)
            code, _ = sb.run_updater("--prune", answers=["n"])
            self.assertEqual(code, 0)
            self.assertEqual(len(sb.release_names()), 3)
            _ = sb.run_updater("--prune", "--yes")
            self.assertEqual(sb.release_names(), ["1.4.1-linux-x86_64", "1.5.0-linux-x86_64"])
            self.assertTrue(legacy.is_dir())

    def test_lock_contention_fails_fast(self) -> None:
        with sandbox() as sb:
            with install_lock(sb.root):
                with self.assertRaises(LockBusyError):
                    _ = sb.run_updater("--registry", str(sb.registry("1.4.1")), "--yes")
            self.assertIsNone(sb.current())
            code, _ = sb.run_updater("--registry", str(sb.registry("1.4.1")), "--yes")
            self.assertEqual(code, 0)


class ArchiveAndRegistryTests(unittest.TestCase):
    def test_parse_registry_release(self) -> None:
        release = parse_registry_release(registry_json("1.4.1", "all"))
        self.assertEqual(release.version, "1.4.1")
        self.assertEqual(release.binaries["linux-aarch64"].args, ("--uid=",))
        self.assertEqual(release.binaries["darwin-aarch64"].args, ())

    def test_plain_http_is_refused(self) -> None:
        with self.assertRaisesRegex(UpdaterError, "non-https"):
            with open_http_response("http://example.invalid/agent.json", timeout=1):
                pass

    def test_registry_digest_is_parsed(self) -> None:
        release = parse_registry_release(registry_json("1.4.1", digest="AB" * 32))
        self.assertEqual(release.binaries["linux-x86_64"].sha256, "ab" * 32)

    def test_unsafe_archive_members_are_rejected(self) -> None:
        expected = ("agy_acp_server.par", "localharness_external")
        cases: list[list[tuple[str, bytes]]] = [
            [("../agy_acp_server.par", b"x"), ("localharness_external", b"x")],
            [("agy_acp_server.par", b"x"), ("localharness_external", b"x"), ("extra", b"x")],
            [("agy_acp_server.par", b"x")],
        ]
        for members in cases:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "a.zip"
                with zipfile.ZipFile(archive, "w") as zf:
                    for name, data in members:
                        zf.writestr(name, data)
                out = Path(tmp) / "out"
                out.mkdir()
                with self.assertRaises(UpdaterError):
                    _ = safe_extract(archive, out, expected)
                self.assertEqual(os.listdir(out), [])


if __name__ == "__main__":
    _ = unittest.main()
