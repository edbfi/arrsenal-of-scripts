#!/usr/bin/env python3
"""Let T3 Code pick Antigravity models and their thinking level through a gateway.

T3 Code's Antigravity provider authenticates with ``gemini-api-key``, whose
model list is hard-coded, so behind CLIProxyAPI only one model can be pinned
with ``AGY_ACP_DEFAULT_MODEL``. Point T3's *Binary path* at this shim instead.
It runs the stock launcher (``antigravity-acp``, in the same directory) and:

1. discovers model families and their thinking levels from the gateway:
   ``GET /v1beta/models``, then one probe per family that CLIProxyAPI rejects
   locally with the family's valid levels. The result is cached;
2. starts ACP in its gateway mode, offering one ``family(level)`` id per
   variant. CLIProxyAPI applies that suffix over the request's own thinking
   level, which ACP always sends as ``high`` for gateway models;
3. rewrites T3's ``authenticate`` from ``gemini-api-key`` to ``gateway``;
4. shows T3 one model per family plus a thinking-level picker, and maps T3's
   choices back to the matching ``family(level)`` id.

Every other message passes through unchanged. Without a gateway URL and key,
or when discovery fails and nothing is cached, it runs the stock launcher
as-is.

Environment, normally set on the T3 provider instance:

``GOOGLE_GEMINI_BASE_URL`` / ``GEMINI_API_KEY``
    The gateway URL and key T3 already passes (*API key* field). Override with
    ``AGY_LLM_GATEWAY_URL`` / ``AGY_LLM_GATEWAY_API_KEY``.
``AGY_ACP_DEFAULT_MODEL``
    Optional default: ``gemini-4-carbon``, ``gemini-4-carbon-high`` or
    ``gemini-4-carbon(high)``. Defaults to the first family at ``high``.
``AGY_GATEWAY_MODEL_PATTERN``
    Regular expression selecting the gateway models to offer. Defaults to
    Gemini 4 and later.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, cast

DESCRIPTION = "Let T3 Code pick Antigravity models and their thinking level through a gateway."
LAUNCHER_NAME = "antigravity-acp"
INSTALL_NAME = "antigravity-acp-gateway"
DEFAULT_INSTALL_ROOT = Path.home() / ".t3" / "custom-tools"
DEFAULT_MODEL_PATTERN = r"^gemini-(?:[4-9]|[1-9][0-9])(?:[.-]|$)"
PREFERRED_LEVEL = "high"
# Ascending, as ACP orders its own thinking-level picker.
LEVEL_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")
LEVEL_NAMES = {
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra",
    "max": "Max",
}
PROBE_LEVEL = "__probe__"
VALID_LEVELS = re.compile(r"valid levels:\s*([a-z_]+(?:\s*,\s*[a-z_]+)*)")
# ACP offers at most this many custom gateway models (model_discovery.MAX_CUSTOM_MODELS).
MAX_GATEWAY_MODELS = 32
CATALOG_TTL_SECONDS = 6 * 60 * 60
CATALOG_SCHEMA = 2
HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_MODEL_PAGES = 20
# Probe answers that say nothing about the model: retry on the next refresh.
RETRYABLE_STATUSES = frozenset({401, 403, 408, 429})
CLI_FLAGS = frozenset({"--install", "--catalog", "-h", "--help"})
# ACP's own ids for the two pickers (acp_server config_options).
MODEL_OPTION_ID = "model"
THINKING_OPTION_ID = "thinking_level"
THOUGHT_LEVEL_CATEGORY = "thought_level"
FORWARDED_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


class GatewayError(RuntimeError):
    """The gateway could not be queried."""


def log(message: str) -> None:
    """Writes a diagnostic to stderr; stdout carries the ACP stream."""
    _ = sys.stderr.write(f"{INSTALL_NAME}: {message}\n")
    _ = sys.stderr.flush()


# ---------------------------------------------------------------- JSON helpers


def as_dict(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return cast("dict[str, object]", value)
    return None


def as_list(value: object) -> list[object]:
    if isinstance(value, list):
        return cast("list[object]", value)
    return []


def as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def as_request_id(value: object) -> int | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | str):
        return value
    return None


def parse_json(raw: bytes | str) -> object:
    return cast("object", json.loads(raw))


# --------------------------------------------------------------------- catalog


def level_rank(level: str) -> int:
    return LEVEL_ORDER.index(level) if level in LEVEL_ORDER else len(LEVEL_ORDER)


def sort_levels(levels: Iterable[str]) -> tuple[str, ...]:
    unique = list(dict.fromkeys(levels))
    return tuple(sorted(unique, key=lambda level: (level_rank(level), unique.index(level))))


def split_variant(model_id: str) -> tuple[str, str | None]:
    """Splits ``family(level)`` or ``family-level`` into family and level."""
    if model_id.endswith(")") and "(" in model_id:
        family, _, level = model_id[:-1].rpartition("(")
        return family, level
    family, sep, level = model_id.rpartition("-")
    if sep and family and level in LEVEL_ORDER:
        return family, level
    return model_id, None


def nearest_level(levels: Sequence[str], wanted: str | None) -> str:
    """Picks ``wanted`` if offered, else the closest level, preferring the higher one."""
    if wanted is not None and wanted in levels:
        return wanted
    target = level_rank(wanted if wanted is not None else PREFERRED_LEVEL)
    return min(levels, key=lambda level: (abs(level_rank(level) - target), -level_rank(level)))


def display_name(model_id: str, listed_name: str | None) -> str:
    if listed_name:
        return re.sub(r"\s*\([^)]*\)$", "", listed_name).strip() or model_id
    return " ".join(part[:1].upper() + part[1:] for part in model_id.split("-"))


@dataclasses.dataclass(frozen=True)
class Family:
    """A model whose thinking level is chosen separately."""

    family_id: str
    name: str
    levels: tuple[str, ...]
    # The id CLIProxyAPI accepts with a ``(level)`` suffix, when it is not
    # ``family_id``: a gateway may list only ``x-high``, not the bare ``x``.
    wire_id: str = ""

    @property
    def wire(self) -> str:
        return self.wire_id or self.family_id

    def variant(self, level: str) -> str:
        return f"{self.wire}({level})"


@dataclasses.dataclass(frozen=True)
class PlainModel:
    """A gateway model offered as-is, with no thinking-level picker."""

    model_id: str
    name: str


@dataclasses.dataclass(frozen=True)
class Catalog:
    families: tuple[Family, ...]
    plain: tuple[PlainModel, ...]

    def family(self, family_id: str) -> Family | None:
        return next((f for f in self.families if family_id in (f.family_id, f.wire)), None)

    def lookup(self, model_id: str) -> tuple[Family, str] | None:
        """Returns the family and level an id names, in any accepted spelling."""
        family_id, level = split_variant(model_id)
        family = self.family(family_id)
        if family is None or level is None or level not in family.levels:
            return None
        return family, level

    def resolve_default(self, hint: str | None) -> str:
        """Returns the gateway id a session starts on."""
        if hint:
            found = self.lookup(hint.strip())
            if found is not None:
                return found[0].variant(found[1])
            family = self.family(hint.strip())
            if family is not None:
                return family.variant(nearest_level(family.levels, None))
            plain = next((p for p in self.plain if p.model_id == hint.strip()), None)
            if plain is not None:
                return plain.model_id
            log(f"AGY_ACP_DEFAULT_MODEL={hint!r} is not offered by the gateway; ignoring it")
        if self.families:
            first = self.families[0]
            return first.variant(nearest_level(first.levels, None))
        return self.plain[0].model_id

    def fit(self, default: str) -> Catalog:
        """Keeps whole families, the default's first, within ACP's model limit.

        Dropping single variants instead would leave the translator choosing
        levels ACP was never offered.
        """
        entries: list[Family | PlainModel] = [*self.families, *self.plain]
        found = self.lookup(default)

        def is_default(entry: Family | PlainModel) -> bool:
            if isinstance(entry, Family):
                return found is not None and entry == found[0]
            return entry.model_id == default

        budget = MAX_GATEWAY_MODELS
        kept: list[Family | PlainModel] = []
        for entry in sorted(entries, key=lambda e: not is_default(e)):
            size = len(entry.levels) if isinstance(entry, Family) else 1
            if size <= budget:
                kept.append(entry)
                budget -= size
        if len(kept) < len(entries):
            log(f"ACP takes {MAX_GATEWAY_MODELS} gateway models; offering {len(kept)} of {len(entries)} models")
        return Catalog(
            tuple(f for f in self.families if f in kept),
            tuple(p for p in self.plain if p in kept),
        )

    def gateway_models(self, default: str) -> list[str]:
        """The ``AGY_LLM_GATEWAY_MODELS`` allowlist; ACP starts on its first entry.

        Call on a catalog already passed through ``fit``.
        """
        ids = [f.variant(level) for f in self.families for level in f.levels]
        ids += [p.model_id for p in self.plain]
        return list(dict.fromkeys([default, *ids]))

    def to_json(self) -> dict[str, object]:
        return {
            "families": [
                {"family_id": f.family_id, "name": f.name, "levels": list(f.levels), "wire_id": f.wire_id}
                for f in self.families
            ],
            "plain": [{"model_id": p.model_id, "name": p.name} for p in self.plain],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> Catalog | None:
        families: list[Family] = []
        for raw in as_list(data.get("families")):
            entry = as_dict(raw) or {}
            family_id, name = as_str(entry.get("family_id")), as_str(entry.get("name"))
            levels = tuple(level for level in as_list(entry.get("levels")) if isinstance(level, str))
            if not family_id or not name or not levels:
                return None
            families.append(Family(family_id, name, levels, as_str(entry.get("wire_id")) or ""))
        plain: list[PlainModel] = []
        for raw in as_list(data.get("plain")):
            entry = as_dict(raw) or {}
            model_id, name = as_str(entry.get("model_id")), as_str(entry.get("name"))
            if not model_id or not name:
                return None
            plain.append(PlainModel(model_id, name))
        if not families and not plain:
            return None
        return cls(tuple(families), tuple(plain))


def build_catalog(
    listed: Sequence[tuple[str, str | None]],
    pattern: re.Pattern[str],
    probe: Callable[[str], tuple[str, ...] | None],
) -> Catalog:
    """Groups the gateway's models into families using ``probe`` for their levels.

    The family name is the listed id without a level suffix. If the gateway
    does not know that bare name, the listed ids are probed instead and the
    first that takes a level becomes the family's ``wire_id``.

    Args:
      listed: ``(model id, display name)`` pairs in the gateway's order.
      pattern: Selects the models to offer.
      probe: Returns a model's thinking levels, None if it has none, or raises
        GatewayError if the gateway could not say.

    Returns:
      The catalog. Listed ids that belong to no family are offered as-is.
    """
    names = dict(listed)
    matching = [model_id for model_id, _ in listed if pattern.search(model_id)]
    listed_by_base: dict[str, list[str]] = {}
    for model_id in matching:
        listed_by_base.setdefault(split_variant(model_id)[0], []).append(model_id)

    def find_levels(base: str) -> tuple[str, tuple[str, ...]] | None:
        for candidate in dict.fromkeys([base, *listed_by_base[base]]):
            levels = probe(candidate)
            if levels:
                return candidate, levels
        return None

    bases = list(listed_by_base)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        found_by_base = dict(zip(bases, pool.map(find_levels, bases), strict=True))
    families = tuple(
        Family(
            base,
            display_name(base, names.get(base) or names.get(found[0])),
            found[1],
            "" if found[0] == base else found[0],
        )
        for base, found in found_by_base.items()
        if found is not None
    )
    family_ids = {f.family_id for f in families}
    plain = tuple(
        PlainModel(model_id, display_name(model_id, names.get(model_id)))
        for model_id in matching
        if split_variant(model_id)[0] not in family_ids
    )
    return Catalog(families, plain)


# --------------------------------------------------------------------- gateway


@dataclasses.dataclass(frozen=True)
class Gateway:
    base_url: str
    api_key: str

    def request(self, method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
        parts = urllib.parse.urlsplit(self.base_url.rstrip("/") + path)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise GatewayError("gateway URL must be http(s) with a host")
        connection_type = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_type(parts.hostname, parts.port, timeout=HTTP_TIMEOUT_SECONDS)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {"x-goog-api-key": self.api_key, "content-type": "application/json"}
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(MAX_RESPONSE_BYTES)
        except (OSError, http.client.HTTPException) as e:
            raise GatewayError(f"{method} {parts.path} failed: {type(e).__name__}") from e
        finally:
            connection.close()

    def list_models(self) -> list[tuple[str, str | None]]:
        """Returns ``(model id, display name)`` for every model the gateway lists."""
        models: list[tuple[str, str | None]] = []
        token = ""
        for _ in range(MAX_MODEL_PAGES):
            query = urllib.parse.urlencode({"pageSize": 1000, **({"pageToken": token} if token else {})})
            status, body = self.request("GET", f"/v1beta/models?{query}")
            if status != 200:
                raise GatewayError(f"listing models returned HTTP {status}")
            try:
                page = as_dict(parse_json(body)) or {}
            except ValueError as e:
                raise GatewayError("listing models returned invalid JSON") from e
            for raw in as_list(page.get("models")):
                entry = as_dict(raw) or {}
                name = as_str(entry.get("name"))
                if name:
                    models.append((name.removeprefix("models/"), as_str(entry.get("displayName"))))
            token = as_str(page.get("nextPageToken")) or ""
            if not token:
                return models
        raise GatewayError("listing models did not finish paging")

    def probe_levels(self, family_id: str) -> tuple[str, ...] | None:
        """Returns a model's thinking levels, read from CLIProxyAPI's rejection.

        The probe names a level no model has, so CLIProxyAPI answers 400 with
        the valid ones before contacting any upstream. The tiny body and
        one-token limit bound the cost should a gateway forward it anyway.

        Returns None when the gateway answers that the model takes no level
        or is unknown. Raises GatewayError when it could not answer, so a
        transient failure never caches a family as level-less.
        """
        body = json.dumps({
            "contents": [{"role": "user", "parts": [{"text": "."}]}],
            "generationConfig": {"maxOutputTokens": 1},
        }).encode()
        model = urllib.parse.quote(f"{family_id}({PROBE_LEVEL})", safe="")
        status, response = self.request("POST", f"/v1beta/models/{model}:generateContent", body)
        if status in RETRYABLE_STATUSES or status >= 500:
            raise GatewayError(f"probing {family_id} returned HTTP {status}")
        return parse_valid_levels(response) if status == 400 else None


def parse_valid_levels(body: bytes) -> tuple[str, ...] | None:
    """Reads the levels from CLIProxyAPI's ``level "x" not supported, valid levels: ...``."""
    match = VALID_LEVELS.search(body.decode("utf-8", "replace"))
    if match is None:
        return None
    return sort_levels(level.strip() for level in match.group(1).split(","))


def gateway_from_env(env: Mapping[str, str]) -> Gateway | None:
    base_url = env.get("AGY_LLM_GATEWAY_URL") or env.get("GOOGLE_GEMINI_BASE_URL") or ""
    api_key = env.get("AGY_LLM_GATEWAY_API_KEY") or env.get("GEMINI_API_KEY") or ""
    if not base_url.strip() or not api_key.strip():
        return None
    return Gateway(base_url.strip(), api_key.strip())


def catalog_path(env: Mapping[str, str]) -> Path:
    # T3 gives each provider instance its own GEMINI_HOME.
    gemini_home = env.get("GEMINI_HOME")
    root = Path(gemini_home) if gemini_home else Path.home() / ".cache"
    return root / INSTALL_NAME / "catalog.json"


def read_cache(path: Path, key: str) -> tuple[Catalog, float] | None:
    try:
        data = as_dict(parse_json(path.read_text(encoding="utf-8"))) or {}
    except (OSError, ValueError):
        return None
    fetched_at = data.get("fetched_at")
    catalog_data = as_dict(data.get("catalog"))
    if data.get("schema") != CATALOG_SCHEMA or data.get("key") != key or catalog_data is None:
        return None
    if not isinstance(fetched_at, int | float) or isinstance(fetched_at, bool):
        return None
    catalog = Catalog.from_json(catalog_data)
    return (catalog, float(fetched_at)) if catalog is not None else None


def write_cache(path: Path, key: str, catalog: Catalog, now: float) -> None:
    payload = {"schema": CATALOG_SCHEMA, "key": key, "fetched_at": now, "catalog": catalog.to_json()}
    temp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".catalog-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        _ = os.replace(temp, path)
    except OSError as e:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)
        log(f"could not cache the model catalog: {e}")


def catalog_key(gateway: Gateway, pattern: re.Pattern[str]) -> str:
    # Neither the API key nor anything derived from it is stored.
    return f"{gateway.base_url.rstrip('/')}\n{pattern.pattern}"


def discover(gateway: Gateway, pattern: re.Pattern[str]) -> Catalog:
    return build_catalog(gateway.list_models(), pattern, gateway.probe_levels)


def load_catalog(
    key: str,
    rediscover: Callable[[], Catalog],
    cache: Path,
    *,
    refresh: bool = False,
    now: float | None = None,
) -> Catalog | None:
    """Returns the cached catalog while fresh, else calls ``rediscover``.

    A failed or empty rediscovery falls back to a stale cache rather than
    failing the session.
    """
    now = time.time() if now is None else now
    cached = read_cache(cache, key)
    if cached is not None and not refresh and now - cached[1] < CATALOG_TTL_SECONDS:
        return cached[0]
    try:
        catalog = rediscover()
    except GatewayError as e:
        log(f"model discovery failed: {e}")
        return cached[0] if cached is not None else None
    if not catalog.families and not catalog.plain:
        log("the gateway lists no models matching the pattern")
        return cached[0] if cached is not None else None
    write_cache(cache, key, catalog, now)
    return catalog


# ------------------------------------------------------------------ translator


class Translator:
    """Rewrites the ACP stream between T3 (client) and the agent.

    The agent speaks gateway ids (``family(level)``); the client sees one model
    per family plus a thinking-level picker. Thread-safe: each direction runs
    on its own thread.
    """

    def __init__(self, catalog: Catalog) -> None:
        self._catalog: Catalog = catalog
        self._lock: threading.Lock = threading.Lock()
        self._current: dict[str, str] = {}
        self._pending: dict[int | str, str] = {}

    # client -> agent

    def from_client(self, message: dict[str, object]) -> dict[str, object]:
        method = as_str(message.get("method"))
        params = as_dict(message.get("params"))
        if method is None or params is None:
            return message
        session_id = as_str(params.get("sessionId"))
        request_id = as_request_id(message.get("id"))
        if session_id is not None and request_id is not None:
            with self._lock:
                self._pending[request_id] = session_id
        if method == "authenticate" and params.get("methodId") == "gemini-api-key":
            return {**message, "params": {"methodId": "gateway"}}
        if session_id is None:
            return message
        if method == "session/set_config_option":
            return {**message, "params": self._translate_set_option(session_id, params)}
        if method == "session/set_model":
            model_id = as_str(params.get("modelId"))
            if model_id is not None:
                target = self._target_for_model(session_id, model_id)
                self._remember(session_id, target)
                return {**message, "params": {**params, "modelId": target}}
        return message

    def _translate_set_option(self, session_id: str, params: dict[str, object]) -> dict[str, object]:
        config_id, value = as_str(params.get("configId")), as_str(params.get("value"))
        if value is None:
            return params
        if config_id == MODEL_OPTION_ID:
            target = self._target_for_model(session_id, value)
        elif config_id == THINKING_OPTION_ID:
            with self._lock:
                current = self._current.get(session_id)
            found = self._catalog.lookup(current) if current is not None else None
            if found is None:
                return params
            target = found[0].variant(nearest_level(found[0].levels, value))
        else:
            return params
        self._remember(session_id, target)
        return {**params, "configId": MODEL_OPTION_ID, "value": target}

    def _target_for_model(self, session_id: str, value: str) -> str:
        """Maps any variant spelling to its gateway id, or a family to it at the session's level.

        Variants are resolved first: a wire id such as ``x-high`` also names
        its family, but a saved ``x-high`` means level ``high``.
        """
        found = self._catalog.lookup(value)
        if found is not None:
            return found[0].variant(found[1])
        family = self._catalog.family(value)
        if family is None:
            return value
        with self._lock:
            current = self._current.get(session_id)
        current_found = self._catalog.lookup(current) if current is not None else None
        return family.variant(nearest_level(family.levels, current_found[1] if current_found else None))

    def _remember(self, session_id: str, model_id: str) -> None:
        with self._lock:
            self._current[session_id] = model_id

    # agent -> client

    def from_agent(self, message: dict[str, object]) -> dict[str, object]:
        if "method" in message:
            params = as_dict(message.get("params"))
            update = as_dict(params.get("update")) if params is not None else None
            if params is None or update is None:
                return message
            session_id = as_str(params.get("sessionId"))
            new_update = self._rewrite_state(update, session_id)
            return message if new_update is update else {**message, "params": {**params, "update": new_update}}
        request_id = as_request_id(message.get("id"))
        with self._lock:
            session_id = self._pending.pop(request_id, None) if request_id is not None else None
        result = as_dict(message.get("result"))
        if result is None:
            return message
        session_id = session_id or as_str(result.get("sessionId"))
        new_result = self._rewrite_state(result, session_id)
        return message if new_result is result else {**message, "result": new_result}

    def _rewrite_state(self, container: dict[str, object], session_id: str | None) -> dict[str, object]:
        rewritten = dict(container)
        changed = False
        if "configOptions" in container:
            new_options = self._rewrite_options(as_list(container.get("configOptions")), session_id)
            if new_options is not None:
                rewritten["configOptions"] = new_options
                changed = True
        models = as_dict(container.get("models"))
        if models is not None:
            new_models = self._rewrite_models(models, session_id)
            if new_models is not None:
                rewritten["models"] = new_models
                changed = True
        return rewritten if changed else container

    def _rewrite_options(self, options: list[object], session_id: str | None) -> list[object] | None:
        entries = [as_dict(option) for option in options]
        if any(e is not None and e.get("category") == THOUGHT_LEVEL_CATEGORY for e in entries):
            return None  # The agent already groups these models.
        index = next(
            (
                i
                for i, e in enumerate(entries)
                if e is not None
                and e.get("type") == "select"
                and (e.get("id") == MODEL_OPTION_ID or e.get("category") == MODEL_OPTION_ID)
            ),
            None,
        )
        if index is None:
            return None
        model_option = entries[index] or {}
        values = flatten_select_options(as_list(model_option.get("options")))
        current = as_str(model_option.get("currentValue"))
        if current is not None and session_id is not None:
            self._remember(session_id, current)
        grouped, offered = self._group(values)
        found = self._catalog.lookup(current) if current is not None else None
        new_model = {
            **model_option,
            "options": grouped,
            "currentValue": found[0].family_id if found is not None else current,
        }
        result: list[object] = [*options[:index], new_model]
        if found is not None:
            family, level = found
            levels = [lvl for lvl in family.levels if family.variant(lvl) in offered]
            result.append({
                "id": THINKING_OPTION_ID,
                "name": "Thinking Level",
                "category": THOUGHT_LEVEL_CATEGORY,
                "type": "select",
                "currentValue": level,
                "options": [{"value": lvl, "name": LEVEL_NAMES.get(lvl, lvl)} for lvl in levels],
            })
        result.extend(options[index + 1 :])
        return result

    def _group(self, values: Sequence[dict[str, object]]) -> tuple[list[object], set[str]]:
        """Collapses offered variants into one option per family, in catalog order."""
        offered = {v for raw in values if (v := as_str(raw.get("value"))) is not None}
        families_offered = {
            found[0].family_id for value in offered if (found := self._catalog.lookup(value)) is not None
        }
        grouped: list[object] = [
            {"value": f.family_id, "name": f.name}
            for f in self._catalog.families
            if f.family_id in families_offered
        ]
        grouped += [
            raw for raw in values if (v := as_str(raw.get("value"))) is not None and self._catalog.lookup(v) is None
        ]
        return grouped, offered

    def _rewrite_models(self, models: dict[str, object], session_id: str | None) -> dict[str, object] | None:
        current = as_str(models.get("currentModelId"))
        available = [entry for raw in as_list(models.get("availableModels")) if (entry := as_dict(raw)) is not None]
        if current is not None and session_id is not None:
            self._remember(session_id, current)
        ids = [model_id for e in available if (model_id := as_str(e.get("modelId"))) is not None]
        if not any(self._catalog.lookup(model_id) is not None for model_id in ids):
            return None
        families_offered = {found[0].family_id for i in ids if (found := self._catalog.lookup(i)) is not None}
        new_available: list[object] = [
            {
                "modelId": f.family_id,
                "name": f.name,
                "description": "Thinking: " + ", ".join(LEVEL_NAMES.get(lvl, lvl) for lvl in f.levels),
            }
            for f in self._catalog.families
            if f.family_id in families_offered
        ]
        new_available += [e for e in available if self._catalog.lookup(as_str(e.get("modelId")) or "") is None]
        found = self._catalog.lookup(current) if current is not None else None
        return {
            **models,
            "availableModels": new_available,
            "currentModelId": found[0].family_id if found is not None else current,
        }


def flatten_select_options(options: list[object]) -> list[dict[str, object]]:
    """Returns the leaf options of an ACP select, whether flat or grouped."""
    flat: list[dict[str, object]] = []
    for raw in options:
        entry = as_dict(raw)
        if entry is None:
            continue
        if "value" in entry:
            flat.append(entry)
        else:
            flat.extend(flatten_select_options(as_list(entry.get("options"))))
    return flat


def rewrite_line(line: bytes, translate: Callable[[dict[str, object]], dict[str, object]]) -> bytes:
    """Applies ``translate`` to one newline-delimited JSON-RPC message."""
    try:
        message = as_dict(parse_json(line))
    except ValueError:
        return line
    if message is None:
        return line
    translated = translate(message)
    if translated is message:
        return line
    # ASCII escapes keep the bytes valid even for a lone surrogate, which json.loads accepts.
    return json.dumps(translated, separators=(",", ":")).encode() + b"\n"


# ----------------------------------------------------------------------- proxy


def agent_environment(env: Mapping[str, str], gateway: Gateway, models: Sequence[str]) -> dict[str, str]:
    agent_env = dict(env)
    agent_env.update({
        "AGY_ACP_ENABLE_GATEWAY_AUTH": "1",
        "AGY_LLM_GATEWAY_URL": gateway.base_url,
        "AGY_LLM_GATEWAY_API_KEY": gateway.api_key,
        "AGY_LLM_GATEWAY_MODELS": ",".join(models),
    })
    # The gateway path ignores these; keep them from reaching any fallback.
    for name in ("AGY_ACP_DEFAULT_MODEL", "GOOGLE_GEMINI_BASE_URL"):
        _ = agent_env.pop(name, None)
    return agent_env


def pump(
    source: IO[bytes],
    sink: IO[bytes],
    translate: Callable[[dict[str, object]], dict[str, object]],
) -> None:
    """Copies messages from ``source`` to ``sink`` until EOF, then closes ``sink``."""
    try:
        for line in iter(source.readline, b""):
            _ = sink.write(rewrite_line(line, translate))
            sink.flush()
    except BrokenPipeError:
        pass  # The other side exited.
    except (ValueError, OSError) as e:
        log(f"stopped relaying messages: {type(e).__name__}: {e}")
    finally:
        with contextlib.suppress(OSError, ValueError):
            sink.close()


def run_proxy(launcher: Path, args: Sequence[str], env: Mapping[str, str], translator: Translator) -> int:
    child = subprocess.Popen(
        [str(launcher), *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=dict(env),
    )
    child_stdin, child_stdout = child.stdin, child.stdout
    if child_stdin is None or child_stdout is None:
        raise RuntimeError("the agent's pipes were not created")

    def forward(signum: int, _frame: object) -> None:
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signum)

    for signum in FORWARDED_SIGNALS:
        _ = signal.signal(signum, forward)
    inbound = threading.Thread(
        target=pump, args=(sys.stdin.buffer, child_stdin, translator.from_client), daemon=True
    )
    inbound.start()
    pump(child_stdout, sys.stdout.buffer, translator.from_agent)
    code = child.wait()
    return 128 - code if code < 0 else code


def proxy_main(args: Sequence[str]) -> int:
    launcher = Path(__file__).absolute().parent / LAUNCHER_NAME
    env = dict(os.environ)
    gateway = gateway_from_env(env)
    catalog: Catalog | None = None
    if gateway is None:
        log("no gateway URL and key; running the stock launcher")
    else:
        pattern = re.compile(env.get("AGY_GATEWAY_MODEL_PATTERN") or DEFAULT_MODEL_PATTERN)
        catalog = load_catalog(
            catalog_key(gateway, pattern), lambda: discover(gateway, pattern), catalog_path(env)
        )
        if catalog is None:
            log("no model catalog; running the stock launcher")
    if gateway is None or catalog is None:
        os.execv(launcher, [str(launcher), *args])
    default = catalog.resolve_default(env.get("AGY_ACP_DEFAULT_MODEL"))
    catalog = catalog.fit(default)
    agent_env = agent_environment(env, gateway, catalog.gateway_models(default))
    return run_proxy(launcher, args, agent_env, Translator(catalog))


# ------------------------------------------------------------------------- CLI


def install(python: str, install_root: Path) -> Path:
    """Copies this script beside the stock launcher, run by ``python``."""
    interpreter = shutil.which(python) or python
    if not Path(interpreter).is_absolute() or not os.access(interpreter, os.X_OK):
        raise SystemExit(f"{python!r} is not an executable interpreter")
    if any(char.isspace() for char in interpreter):
        # The kernel splits a shebang line at the first space.
        raise SystemExit(f"{interpreter!r} contains whitespace, which a shebang line cannot hold")
    probe = subprocess.run(
        [interpreter, "-c", "import sys; print(sys.version_info >= (3, 11))"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.stdout.strip() != "True":
        raise SystemExit(f"{interpreter} must be Python 3.11 or newer")
    if not (install_root / LAUNCHER_NAME).exists():
        raise SystemExit(f"no {LAUNCHER_NAME} in {install_root}; run update_antigravity_acp.py first")
    source = Path(__file__).read_text(encoding="utf-8").split("\n", 1)[1]
    target = install_root / INSTALL_NAME
    fd, temp = tempfile.mkstemp(dir=install_root, prefix=f".{INSTALL_NAME}-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        _ = handle.write(f"#!{interpreter} -I\n{source}")
    os.chmod(temp, 0o755)
    _ = os.replace(temp, target)
    return target


def run_cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=INSTALL_NAME, description=DESCRIPTION)
    commands = parser.add_mutually_exclusive_group(required=True)
    _ = commands.add_argument("--install", action="store_true", help=f"install as {INSTALL_NAME} beside the launcher")
    _ = commands.add_argument("--catalog", action="store_true", help="discover and print the model catalog")
    _ = parser.add_argument("--python", default=sys.executable, help="interpreter for --install (default: this one)")
    _ = parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    _ = parser.add_argument("--refresh", action="store_true", help="with --catalog: ignore the cache")
    options = parser.parse_args(argv)
    if cast("bool", options.install):
        target = install(cast("str", options.python), cast("Path", options.install_root))
        print(f"installed {target}; set it as the T3 Antigravity binary path")
        return 0
    gateway = gateway_from_env(os.environ)
    if gateway is None:
        parser.error("set GOOGLE_GEMINI_BASE_URL and GEMINI_API_KEY (or the AGY_LLM_GATEWAY_* pair)")
    pattern = re.compile(os.environ.get("AGY_GATEWAY_MODEL_PATTERN") or DEFAULT_MODEL_PATTERN)
    catalog = load_catalog(
        catalog_key(gateway, pattern),
        lambda: discover(gateway, pattern),
        catalog_path(os.environ),
        refresh=cast("bool", options.refresh),
    )
    if catalog is None:
        return 1
    default = catalog.resolve_default(os.environ.get("AGY_ACP_DEFAULT_MODEL"))
    catalog = catalog.fit(default)
    print(json.dumps({**catalog.to_json(), "gateway_models": catalog.gateway_models(default)}, indent=2))
    return 0


def main(argv: Sequence[str]) -> int:
    if CLI_FLAGS.intersection(argv):
        return run_cli(argv)
    return proxy_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
