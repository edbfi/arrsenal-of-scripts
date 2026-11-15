"""Behavioural tests for antigravity_acp_gateway."""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from typing import cast

from antigravity_acp_gateway import (
    DEFAULT_MODEL_PATTERN,
    INSTALL_NAME,
    LAUNCHER_NAME,
    MAX_GATEWAY_MODELS,
    Catalog,
    Family,
    Gateway,
    GatewayError,
    PlainModel,
    Translator,
    as_dict,
    as_list,
    build_catalog,
    install,
    load_catalog,
    main,
    parse_json,
    nearest_level,
    parse_valid_levels,
    rewrite_line,
    split_variant,
    write_cache,
)

SCRIPT = Path(__file__).resolve().parent.parent / "antigravity_acp_gateway.py"
CARBON = Family("gemini-4-carbon", "Gemini 4 Carbon", ("low", "medium", "high"))
HYDROGEN = Family("gemini-4-hydrogen", "Gemini 4 Hydrogen", ("low", "medium", "high", "xhigh"))
ARGON = PlainModel("gemini-4-argon-high", "Gemini 4 Argon")
CATALOG = Catalog((CARBON, HYDROGEN), (ARGON,))
PATTERN = re.compile(DEFAULT_MODEL_PATTERN)


def model_option(current: str, values: list[str]) -> dict[str, object]:
    return {
        "id": "model",
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": current,
        "options": [{"value": v, "name": v} for v in values],
    }


MODE_OPTION: dict[str, object] = {"id": "mode", "type": "select", "currentValue": "default", "options": []}
AGENT_IDS = CATALOG.gateway_models("gemini-4-carbon(high)")


def options_by_id(container: dict[str, object]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for raw in as_list(container.get("configOptions")):
        option = as_dict(raw) or {}
        result[str(option.get("id"))] = option
    return result


def values(option: dict[str, object]) -> list[object]:
    return [(as_dict(o) or {}).get("value") for o in as_list(option.get("options"))]


class ParsingTest(unittest.TestCase):
    def test_split_variant_accepts_both_spellings(self) -> None:
        self.assertEqual(split_variant("gemini-4-carbon(low)"), ("gemini-4-carbon", "low"))
        self.assertEqual(split_variant("gemini-4-hydrogen-xhigh"), ("gemini-4-hydrogen", "xhigh"))
        self.assertEqual(split_variant("gemini-3.8-flash-tiered"), ("gemini-3.8-flash-tiered", None))

    def test_nearest_level_prefers_exact_then_closest_higher(self) -> None:
        self.assertEqual(nearest_level(CARBON.levels, "medium"), "medium")
        self.assertEqual(nearest_level(CARBON.levels, "xhigh"), "high")
        self.assertEqual(nearest_level(CARBON.levels, None), "high")
        self.assertEqual(nearest_level(("low", "high"), "medium"), "high")

    def test_parse_valid_levels_reads_cliproxy_rejection(self) -> None:
        body = b'{"error":{"message":"level \\"__probe__\\" not supported, valid levels: xhigh, low, high"}}'
        self.assertEqual(parse_valid_levels(body), ("low", "high", "xhigh"))
        self.assertIsNone(parse_valid_levels(b'{"error":{"message":"unknown model"}}'))

    def test_default_pattern_selects_gemini_4_and_later(self) -> None:
        candidates = ("gemini-4-carbon", "gemini-12-x", "gemini-40", "gemini-3.8-flash", "gemini-2.5-pro", "gemini-4o")
        matches = [m for m in candidates if PATTERN.search(m)]
        self.assertEqual(matches, ["gemini-4-carbon", "gemini-12-x", "gemini-40"])


class CatalogTest(unittest.TestCase):
    def test_build_catalog_groups_variants_and_keeps_unprobed_models(self) -> None:
        listed: list[tuple[str, str | None]] = [
            ("gemini-3.8-flash", "Gemini 3.8 Flash"),
            ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
            ("gemini-4-carbon", "Gemini 4 Carbon"),
            ("gemini-4-carbon-high", None),
            ("gemini-4-hydrogen", None),
        ]
        levels = {"gemini-4-carbon": CARBON.levels, "gemini-4-hydrogen": HYDROGEN.levels}
        catalog = build_catalog(listed, PATTERN, levels.get)
        self.assertEqual(catalog.families, (CARBON, Family("gemini-4-hydrogen", "Gemini 4 Hydrogen", HYDROGEN.levels)))
        self.assertEqual(catalog.plain, (ARGON,))

    def test_resolve_default_accepts_every_spelling(self) -> None:
        self.assertEqual(CATALOG.resolve_default(None), "gemini-4-carbon(high)")
        self.assertEqual(CATALOG.resolve_default("gemini-4-hydrogen-xhigh"), "gemini-4-hydrogen(xhigh)")
        self.assertEqual(CATALOG.resolve_default("gemini-4-hydrogen(low)"), "gemini-4-hydrogen(low)")
        self.assertEqual(CATALOG.resolve_default("gemini-4-hydrogen"), "gemini-4-hydrogen(high)")
        self.assertEqual(CATALOG.resolve_default("gemini-4-argon-high"), "gemini-4-argon-high")
        self.assertEqual(CATALOG.resolve_default("gemini-9-unknown"), "gemini-4-carbon(high)")

    def test_gateway_models_lists_default_first_without_duplicates(self) -> None:
        models = CATALOG.gateway_models("gemini-4-hydrogen(xhigh)")
        self.assertEqual(models[0], "gemini-4-hydrogen(xhigh)")
        self.assertEqual(len(models), 8)
        self.assertEqual(len(set(models)), len(models))
        self.assertIn("gemini-4-carbon(low)", models)
        self.assertIn("gemini-4-argon-high", models)

    def test_catalog_json_round_trips(self) -> None:
        self.assertEqual(Catalog.from_json(CATALOG.to_json()), CATALOG)
        self.assertIsNone(Catalog.from_json({"families": [{"family_id": "x"}]}))


class Discovery:
    """Counts rediscovery calls; ``fail`` makes them raise like an offline gateway."""

    def __init__(self, *, fail: bool) -> None:
        self.fail: bool = fail
        self.calls: int = 0

    def __call__(self) -> Catalog:
        self.calls += 1
        if self.fail:
            raise GatewayError("offline")
        return CATALOG


class LoadCatalogTest(unittest.TestCase):
    def test_fresh_cache_skips_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, discovery = Path(tmp) / "catalog.json", Discovery(fail=True)
            write_cache(cache, "k", CATALOG, now=1000.0)
            self.assertEqual(load_catalog("k", discovery, cache, now=1001.0), CATALOG)
            self.assertEqual(discovery.calls, 0)

    def test_stale_cache_is_used_when_discovery_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, discovery = Path(tmp) / "catalog.json", Discovery(fail=True)
            write_cache(cache, "k", CATALOG, now=0.0)
            self.assertEqual(load_catalog("k", discovery, cache, now=10**9), CATALOG)
            self.assertEqual(discovery.calls, 1)

    def test_other_gateway_key_ignores_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "catalog.json"
            write_cache(cache, "other", CATALOG, now=1000.0)
            self.assertIsNone(load_catalog("k", Discovery(fail=True), cache, now=1001.0))

    def test_failed_cache_write_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "catalog.json"
            cache.mkdir()  # os.replace cannot put a file over a directory.
            with contextlib.redirect_stderr(io.StringIO()):
                write_cache(cache, "k", CATALOG, now=1.0)
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["catalog.json"])

    def test_discovery_result_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache, ok, offline = Path(tmp) / "catalog.json", Discovery(fail=False), Discovery(fail=True)
            self.assertEqual(load_catalog("k", ok, cache, now=5.0), CATALOG)
            self.assertEqual(load_catalog("k", offline, cache, now=6.0), CATALOG)
            self.assertEqual((ok.calls, offline.calls), (1, 0))


class Session:
    """Drives a Translator through one ACP session, as T3 and the agent would."""

    def __init__(self) -> None:
        self.t: Translator = Translator(CATALOG)

    def new_session(self, current: str = "gemini-4-carbon(high)") -> dict[str, object]:
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {
                "sessionId": "s1",
                "configOptions": [model_option(current, AGENT_IDS), MODE_OPTION],
                "models": {
                    "currentModelId": current,
                    "availableModels": [{"modelId": m, "name": m} for m in AGENT_IDS],
                },
            },
        }
        return as_dict(self.t.from_agent(message).get("result")) or {}

    def set_option(self, config_id: str, value: str) -> dict[str, object]:
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/set_config_option",
            "params": {"sessionId": "s1", "configId": config_id, "value": value},
        }
        return as_dict(self.t.from_client(message).get("params")) or {}


class TranslatorTest(unittest.TestCase):
    def test_authenticate_is_switched_to_gateway(self) -> None:
        s = Session()
        message: dict[str, object] = {"id": 1, "method": "authenticate", "params": {"methodId": "gemini-api-key"}}
        self.assertEqual(s.t.from_client(message)["params"], {"methodId": "gateway"})
        other: dict[str, object] = {"id": 1, "method": "authenticate", "params": {"methodId": "oauth-personal"}}
        self.assertIs(s.t.from_client(other), other)

    def test_session_shows_families_and_thinking_level(self) -> None:
        s = Session()
        result = s.new_session()
        options = options_by_id(result)
        self.assertEqual(list(options), ["model", "thinking_level", "mode"])
        self.assertEqual(options["model"]["currentValue"], "gemini-4-carbon")
        self.assertEqual(values(options["model"]), ["gemini-4-carbon", "gemini-4-hydrogen", "gemini-4-argon-high"])
        self.assertEqual(options["thinking_level"]["category"], "thought_level")
        self.assertEqual(options["thinking_level"]["currentValue"], "high")
        self.assertEqual(values(options["thinking_level"]), ["low", "medium", "high"])
        models = as_dict(result.get("models")) or {}
        self.assertEqual(models.get("currentModelId"), "gemini-4-carbon")

    def test_plain_model_has_no_thinking_picker(self) -> None:
        s = Session()
        options = options_by_id(s.new_session("gemini-4-argon-high"))
        self.assertEqual(list(options), ["model", "mode"])
        self.assertEqual(options["model"]["currentValue"], "gemini-4-argon-high")

    def test_switching_family_keeps_the_nearest_level(self) -> None:
        s = Session()
        _ = s.new_session("gemini-4-hydrogen(xhigh)")
        self.assertEqual(s.set_option("model", "gemini-4-carbon")["value"], "gemini-4-carbon(high)")
        self.assertEqual(s.set_option("model", "gemini-4-hydrogen")["value"], "gemini-4-hydrogen(high)")

    def test_thinking_level_selects_the_variant(self) -> None:
        s = Session()
        _ = s.new_session("gemini-4-hydrogen(high)")
        params = s.set_option("thinking_level", "xhigh")
        self.assertEqual((params["configId"], params["value"]), ("model", "gemini-4-hydrogen(xhigh)"))

    def test_legacy_and_unknown_model_ids(self) -> None:
        s = Session()
        _ = s.new_session()
        self.assertEqual(s.set_option("model", "gemini-4-hydrogen-xhigh")["value"], "gemini-4-hydrogen(xhigh)")
        self.assertEqual(s.set_option("model", "gemini-9-unknown")["value"], "gemini-9-unknown")
        self.assertEqual(s.set_option("mode", "yolo"), {"sessionId": "s1", "configId": "mode", "value": "yolo"})

    def test_set_option_response_is_rewritten_for_the_pending_session(self) -> None:
        s = Session()
        _ = s.new_session()
        _ = s.set_option("model", "gemini-4-hydrogen")
        response: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {"configOptions": [model_option("gemini-4-hydrogen(high)", AGENT_IDS), MODE_OPTION]},
        }
        options = options_by_id(as_dict(s.t.from_agent(response).get("result")) or {})
        self.assertEqual(options["model"]["currentValue"], "gemini-4-hydrogen")
        self.assertEqual(values(options["thinking_level"]), ["low", "medium", "high", "xhigh"])
        self.assertEqual(s.set_option("thinking_level", "low")["value"], "gemini-4-hydrogen(low)")

    def test_config_option_update_notification_is_rewritten(self) -> None:
        s = Session()
        notification: dict[str, object] = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s1",
                "update": {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [model_option("gemini-4-carbon(low)", AGENT_IDS)],
                },
            },
        }
        params = as_dict(s.t.from_agent(notification).get("params")) or {}
        options = options_by_id(as_dict(params.get("update")) or {})
        self.assertEqual(options["thinking_level"]["currentValue"], "low")
        self.assertEqual(s.set_option("model", "gemini-4-hydrogen")["value"], "gemini-4-hydrogen(low)")

    def test_agent_grouped_options_pass_through(self) -> None:
        s = Session()
        thinking: dict[str, object] = {"id": "thinking_level", "category": "thought_level", "type": "select"}
        message: dict[str, object] = {
            "id": 4,
            "result": {"configOptions": [model_option("gemini-3.8-flash", ["gemini-3.8-flash"]), thinking]},
        }
        self.assertIs(s.t.from_agent(message), message)

    def test_rewrite_line_survives_a_lone_surrogate(self) -> None:
        line = b'{"id": 1, "method": "authenticate", "params": {"methodId": "gemini-api-key", "x": "\\ud83d"}}\n'
        out = rewrite_line(line, Session().t.from_client)
        self.assertEqual((as_dict(parse_json(out)) or {}).get("params"), {"methodId": "gateway"})
        notification = b'{"method": "session/update", "params": {"sessionId": "s1", "update": {"configOptions": [], "t": "\\ud83d"}}}\n'
        self.assertIs(rewrite_line(notification, Session().t.from_agent), notification)
        update = json.dumps({
            "id": 2,
            "result": {"note": "\ud83d", "configOptions": [model_option("gemini-4-carbon(low)", AGENT_IDS)]},
        }).encode()
        rewritten = rewrite_line(update + b"\n", Session().t.from_agent)
        _ = rewritten.decode("ascii")
        self.assertEqual(rewritten.count(b"\n"), 1)

    def test_rewrite_line_keeps_untouched_bytes(self) -> None:
        s = Session()
        line = b'{"jsonrpc": "2.0", "method": "session/update", "params": {"x": 1}}\n'
        self.assertIs(rewrite_line(line, s.t.from_agent), line)
        self.assertEqual(rewrite_line(b"not json\n", s.t.from_agent), b"not json\n")


ARGON_FAMILY = Family("gemini-4-argon", "Gemini 4 Argon", ("low", "medium", "high"), "gemini-4-argon-high")


class WireIdTest(unittest.TestCase):
    """A gateway may list only a level-suffixed id, such as ``x-high``, and not the bare ``x``."""

    def test_listed_variant_is_probed_when_the_bare_name_is_unknown(self) -> None:
        listed: list[tuple[str, str | None]] = [("gemini-4-argon-high", "Gemini 4 Argon (High)")]
        probes: list[str] = []

        def probe(model_id: str) -> tuple[str, ...] | None:
            probes.append(model_id)
            return ARGON_FAMILY.levels if model_id == "gemini-4-argon-high" else None

        catalog = build_catalog(listed, PATTERN, probe)
        self.assertEqual(catalog.families, (ARGON_FAMILY,))
        self.assertEqual(catalog.plain, ())
        self.assertEqual(probes, ["gemini-4-argon", "gemini-4-argon-high"])

    def test_wire_family_ids_and_legacy_spelling(self) -> None:
        catalog = Catalog((ARGON_FAMILY,), ())
        self.assertEqual(ARGON_FAMILY.variant("low"), "gemini-4-argon-high(low)")
        self.assertEqual(catalog.lookup("gemini-4-argon-high(low)"), (ARGON_FAMILY, "low"))
        self.assertEqual(catalog.lookup("gemini-4-argon-high"), (ARGON_FAMILY, "high"))
        self.assertEqual(Catalog.from_json(catalog.to_json()), catalog)

    def test_translator_shows_the_family_and_sends_the_wire_id(self) -> None:
        translator = Translator(Catalog((CARBON, ARGON_FAMILY), ()))
        agent_ids = [CARBON.variant(lvl) for lvl in CARBON.levels] + [ARGON_FAMILY.variant(lvl) for lvl in ARGON_FAMILY.levels]
        message: dict[str, object] = {
            "id": 3,
            "result": {"sessionId": "s1", "configOptions": [model_option("gemini-4-argon-high(low)", agent_ids)]},
        }
        options = options_by_id(as_dict(translator.from_agent(message).get("result")) or {})
        self.assertEqual(values(options["model"]), ["gemini-4-carbon", "gemini-4-argon"])
        self.assertEqual(options["model"]["currentValue"], "gemini-4-argon")
        self.assertEqual(options["thinking_level"]["currentValue"], "low")
        request: dict[str, object] = {
            "id": 4,
            "method": "session/set_config_option",
            "params": {"sessionId": "s1", "configId": "thinking_level", "value": "high"},
        }
        params = as_dict(translator.from_client(request).get("params")) or {}
        self.assertEqual(params.get("value"), "gemini-4-argon-high(high)")

    def test_saved_wire_id_keeps_its_own_level(self) -> None:
        translator = Translator(Catalog((ARGON_FAMILY,), ()))
        agent_ids = [ARGON_FAMILY.variant(lvl) for lvl in ARGON_FAMILY.levels]
        session: dict[str, object] = {
            "id": 3,
            "result": {"sessionId": "s1", "configOptions": [model_option("gemini-4-argon-high(low)", agent_ids)]},
        }
        requests: list[tuple[str, dict[str, object], str]] = [
            ("session/set_config_option", {"sessionId": "s1", "configId": "model", "value": "gemini-4-argon-high"}, "value"),
            ("session/set_model", {"sessionId": "s1", "modelId": "gemini-4-argon-high"}, "modelId"),
        ]
        for method, params, field in requests:
            with self.subTest(method=method):
                _ = translator.from_agent(session)  # Back to "low", so each method is checked on its own.
                message: dict[str, object] = {"id": 5, "method": method, "params": params}
                sent = as_dict(translator.from_client(message).get("params")) or {}
                self.assertEqual(sent.get(field), "gemini-4-argon-high(high)")
        family: dict[str, object] = {
            "id": 6,
            "method": "session/set_config_option",
            "params": {"sessionId": "s1", "configId": "model", "value": "gemini-4-argon"},
        }
        sent = as_dict(translator.from_client(family).get("params")) or {}
        self.assertEqual(sent.get("value"), "gemini-4-argon-high(high)")

    def test_probe_failure_aborts_discovery(self) -> None:
        def probe(model_id: str) -> tuple[str, ...] | None:
            raise GatewayError(f"probing {model_id} returned HTTP 503")

        with self.assertRaises(GatewayError):
            _ = build_catalog([("gemini-4-carbon", None)], PATTERN, probe)


class FitTest(unittest.TestCase):
    def test_keeps_whole_families_and_the_default_within_the_limit(self) -> None:
        families = tuple(Family(f"gemini-4-f{i:02}", f"F{i}", ("low", "medium", "high")) for i in range(11))
        default = families[-1].variant("high")
        fitted = Catalog(families, ()).fit(default)
        allowlist = fitted.gateway_models(default)
        self.assertLessEqual(len(allowlist), MAX_GATEWAY_MODELS)
        self.assertEqual(allowlist[0], default)
        self.assertIn(families[-1], fitted.families)
        for family in fitted.families:
            self.assertTrue(all(family.variant(lvl) in allowlist for lvl in family.levels))

    def test_small_catalog_is_unchanged(self) -> None:
        self.assertEqual(CATALOG.fit("gemini-4-carbon(high)"), CATALOG)


class FakeGateway:
    """A local stand-in for CLIProxyAPI that serves canned responses."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        self.routes: dict[str, tuple[int, bytes]] = routes
        gateway = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                gateway.reply(self)

            def do_POST(self) -> None:
                _ = self.rfile.read(int(self.headers.get("content-length") or 0))
                gateway.reply(self)

        self.server: http.server.ThreadingHTTPServer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url: str = f"http://127.0.0.1:{self.server.server_port}"

    def reply(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        status, body = self.routes.get(urllib.parse.unquote(handler.path), (404, b"{}"))
        handler.send_response(status)
        handler.send_header("content-length", str(len(body)))
        handler.end_headers()
        _ = handler.wfile.write(body)

    def __enter__(self) -> Gateway:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return Gateway(self.url, "test-key")

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def probe_path(model_id: str) -> str:
    return f"/v1beta/models/{model_id}(__probe__):generateContent"


class GatewayHttpTest(unittest.TestCase):
    def test_list_models_follows_pages(self) -> None:
        page1 = json.dumps({"models": [{"name": "models/a", "displayName": "A"}], "nextPageToken": "t2"}).encode()
        page2 = json.dumps({"models": [{"name": "models/b"}]}).encode()
        routes = {
            "/v1beta/models?pageSize=1000": (200, page1),
            "/v1beta/models?pageSize=1000&pageToken=t2": (200, page2),
        }
        with contextlib.redirect_stderr(io.StringIO()), FakeGateway(routes) as gateway:
            self.assertEqual(gateway.list_models(), [("a", "A"), ("b", None)])

    def test_probe_statuses(self) -> None:
        rejection = b'{"error":{"message":"level \\"__probe__\\" not supported, valid levels: low, high"}}'
        routes = {
            probe_path("levels"): (400, rejection),
            probe_path("unknown"): (400, b'{"error":{"message":"unknown provider for model"}}'),
            probe_path("busy"): (503, b"{}"),
            probe_path("limited"): (429, b"{}"),
        }
        with contextlib.redirect_stderr(io.StringIO()), FakeGateway(routes) as gateway:
            self.assertEqual(gateway.probe_levels("levels"), ("low", "high"))
            self.assertIsNone(gateway.probe_levels("unknown"))
            self.assertIsNone(gateway.probe_levels("missing"))
            for model_id in ("busy", "limited"):
                with self.assertRaises(GatewayError):
                    _ = gateway.probe_levels(model_id)

    def test_unreachable_gateway_raises(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = cast("tuple[str, int]", sock.getsockname())[1]
        with self.assertRaises(GatewayError):
            _ = Gateway(f"http://127.0.0.1:{port}", "k").probe_levels("x")


class MainDispatchTest(unittest.TestCase):
    def test_cli_flags_are_recognised_in_any_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _ = (root / LAUNCHER_NAME).write_text("#!/bin/sh\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--python", sys.executable, "--install", "--install-root", str(root)])
            self.assertEqual(code, 0)
            self.assertTrue((root / INSTALL_NAME).exists())


FAKE_AGENT = textwrap.dedent(
    """\
    import json, os, sys
    models = os.environ.get("AGY_LLM_GATEWAY_MODELS", "").split(",")
    log = open(os.environ["FAKE_AGENT_LOG"], "a")
    for line in sys.stdin:
        msg = json.loads(line)
        log.write(json.dumps(msg) + "\\n"); log.flush()
        if msg.get("method") == "session/new":
            option = {"id": "model", "category": "model", "type": "select", "currentValue": models[0],
                      "options": [{"value": m, "name": m} for m in models]}
            result = {"sessionId": "s1", "configOptions": [option]}
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
    """
)


class ShimSandbox:
    """An install root with a fake agent behind the installed shim; no network is used."""

    def __init__(self, base: Path) -> None:
        root = base / "custom-tools"
        root.mkdir()
        agent = base / "fake_agent.py"
        _ = agent.write_text(FAKE_AGENT, encoding="utf-8")
        launcher = root / LAUNCHER_NAME
        _ = launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{agent}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
        self.shim: Path = install(sys.executable, root)
        self.home: Path = base / "gemini-home"
        self.log: Path = base / "agent.log"
        self.env: dict[str, str] = {
            "PATH": os.environ.get("PATH", ""),
            "GEMINI_HOME": str(self.home),
            "FAKE_AGENT_LOG": str(self.log),
            # Port 9 (discard) is never contacted: tests seed a fresh cache or drop the key.
            "GOOGLE_GEMINI_BASE_URL": "http://127.0.0.1:9",
            "GEMINI_API_KEY": "test-key",
            "AGY_ACP_DEFAULT_MODEL": "gemini-4-hydrogen-xhigh",
        }

    def seed_cache(self) -> None:
        key = "http://127.0.0.1:9\n" + DEFAULT_MODEL_PATTERN
        write_cache(self.home / INSTALL_NAME / "catalog.json", key, CATALOG, time.time())

    def run(self, messages: list[dict[str, object]]) -> list[dict[str, object]]:
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        done = subprocess.run(
            [str(self.shim)], input=stdin, capture_output=True, text=True, env=self.env, timeout=60, check=True
        )
        return [as_dict(parse_json(line)) or {} for line in done.stdout.splitlines()]

    def agent_saw(self) -> list[dict[str, object]]:
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [as_dict(parse_json(line)) or {} for line in lines]


AUTHENTICATE: dict[str, object] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "authenticate",
    "params": {"methodId": "gemini-api-key"},
}


class ProxyProcessTest(unittest.TestCase):
    def test_install_writes_executable_with_interpreter_shebang(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            box = ShimSandbox(Path(tmp))
            first_line = box.shim.read_text(encoding="utf-8").split("\n", 1)[0]
            self.assertEqual(first_line, f"#!{shutil.which(sys.executable) or sys.executable} -I")
            self.assertTrue(os.access(box.shim, os.X_OK))
            self.assertEqual(box.shim.name, INSTALL_NAME)

    def test_proxy_translates_both_directions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            box = ShimSandbox(Path(tmp))
            box.seed_cache()
            replies = box.run([
                AUTHENTICATE,
                {"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": "/", "mcpServers": []}},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "session/set_config_option",
                    "params": {"sessionId": "s1", "configId": "model", "value": "gemini-4-carbon"},
                },
            ])
            seen = box.agent_saw()
            self.assertEqual(seen[0]["params"], {"methodId": "gateway"})
            self.assertEqual((as_dict(seen[2].get("params")) or {}).get("value"), "gemini-4-carbon(high)")
            options = options_by_id(as_dict(replies[1].get("result")) or {})
            self.assertEqual(options["model"]["currentValue"], "gemini-4-hydrogen")
            self.assertEqual(options["thinking_level"]["currentValue"], "xhigh")

    def test_without_catalog_runs_the_stock_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            box = ShimSandbox(Path(tmp))
            del box.env["GEMINI_API_KEY"]
            replies = box.run([AUTHENTICATE])
            self.assertEqual(box.agent_saw()[0]["params"], {"methodId": "gemini-api-key"})
            self.assertEqual(replies[0].get("result"), {})


if __name__ == "__main__":
    _ = unittest.main()
