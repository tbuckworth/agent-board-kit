"""Verify the Researcher files installed for the exact receiving CLI and host.

Researcher is the `researcher` Claude Code and Codex plugin (PLUGIN_URL), installed from the marketplace config
researcher_marketplaces names for each CLI (README.md, "Researcher mode", says how). When it is not installed there,
or is older than MINIMUM, the switch says so in the Mail tab (Unavailable, its message beginning FAILURE_PREFIX)."""
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from subprocess import DEVNULL, PIPE, TimeoutExpired, run
from typing import NamedTuple

from . import config, registry, remote

MINIMUM = (0, 7, 0)
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\+([A-Za-z0-9.-]+))?")
METHODS_HELPER = "scripts/methods_review.py"
COMMON_FILES = ("commands/researcher.md", "docs/WORKFLOW.md", METHODS_HELPER)
CODEX_FILES = ("skills/researcher/SKILL.md", "templates/codex-adapter.md")
FAILURE_PREFIX = "Researcher payload unavailable"
PLUGIN_URL = "https://github.com/tbuckworth/ai-safety-researcher"
HELP_TIMEOUT = 3


class Unavailable(ValueError):
    """The target cannot supply a verified Researcher payload."""


class NoInterpreter(Unavailable):
    """The installed helper could not run with any candidate interpreter."""


class Payload(NamedTuple):
    root: str
    version: str
    python: str
    fingerprint: str


def version_key(version: str) -> tuple:
    match = VERSION.fullmatch(version)
    if not match:
        raise Unavailable("invalid installed version in plugin metadata")
    major, minor, patch, build = match.groups()
    return int(major), int(minor), int(patch), build or ""


def installations(cfg: dict, cli: str, session_id: str) -> list[tuple[str, Path]]:
    """Find versions in Codex's cache or the receiving Claude login's installation registry."""
    marketplace = cfg["researcher_marketplaces"][cli]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", marketplace):
        raise Unavailable("invalid Researcher marketplace in config")
    if cli == "codex":
        cache = registry.CODEX_HOME / "plugins" / "cache" / marketplace / "researcher"
        return [(p.name, p) for p in cache.glob("*")
                if p.is_dir() and VERSION.fullmatch(p.name) and p.resolve().is_relative_to(cache.resolve())]
    session, code, _ = registry.by_id(registry.claude_sessions(), session_id)
    if code:
        raise Unavailable("the Claude session's login could not be located")
    home = registry.claude_home(session["config_dir"])
    index = home / "plugins" / "installed_plugins.json"
    if not index.exists():   # no plugin was ever installed on this login
        return []
    records = json.loads(index.read_text(encoding="utf-8"))["plugins"].get("researcher@" + marketplace, [])
    cache_roots = {(h / "plugins" / "cache" / marketplace / "researcher").resolve()
                   for h in (home, registry.DEFAULT_CLAUDE_HOME)}
    cwd = Path(session["cwd"]).resolve() if session.get("cwd") else None
    eligible = []
    for record in records:
        project = record.get("projectPath")
        if record["scope"] != "user" and (not project or cwd is None or
                                          not cwd.is_relative_to(Path(project).resolve())):
            continue
        path = Path(record["installPath"]).resolve()
        if any(path.is_relative_to(cache) for cache in cache_roots):
            eligible.append((record["version"], path))
    return eligible


def compatible_python(cfg: dict, root: Path) -> Path:
    """Run the installed helper's --help and return the first Python that exposes the approval commands."""
    candidates = cfg["researcher_python"]
    if candidates is None:
        candidates = (*config.RESEARCHER_PLATFORM_PYTHONS.get(sys.platform, ()), shutil.which("python3"), sys.executable)
    elif not isinstance(candidates, list) or not all(isinstance(p, str) for p in candidates):
        raise NoInterpreter("configure researcher_python as a list of absolute interpreter paths")
    for candidate in dict.fromkeys(candidates):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute() or not os.access(path, os.X_OK) or not path.is_file():
            continue
        try:
            result = run([str(path), "-B", str(root / METHODS_HELPER), "--help"], cwd=root, text=True,
                         errors="replace", stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL, timeout=HELP_TIMEOUT)
        except (OSError, TimeoutExpired):
            continue
        if result.returncode == 0 and all(action in result.stdout for action in ("approve-human", "verify")):
            return path
    raise NoInterpreter(
        "no available Python could run methods_review.py --help. Install a compatible Python such as "
        "Homebrew Python 3.12 on Mac, or add a working absolute path to researcher_python on the receiving host"
    )


def local(cfg: dict, cli: str, session_id: str) -> Payload:
    """Read and hash the latest installed workflow and run its helper's --help with a compatible interpreter."""
    try:
        candidates = installations(cfg, cli, session_id)
        if not candidates:
            raise Unavailable("the researcher plugin is not installed for this %s login from marketplace %r (config "
                              "researcher_marketplaces.%s). Install it from %s as README.md, \"Researcher mode\", "
                              "says" % (cli, cfg["researcher_marketplaces"][cli], cli, PLUGIN_URL))
        version, root = max(candidates, key=lambda pair: version_key(pair[0]))
        if version_key(version)[:3] < MINIMUM:
            raise Unavailable("installed version %s is older than 0.7.0" % version)
        root = root.resolve(strict=True)
        manifest_path = ".%s-plugin/plugin.json" % cli
        paths = (manifest_path,) + COMMON_FILES + (CODEX_FILES if cli == "codex" else ())
        digest = hashlib.sha256(json.dumps([cli, str(root), version]).encode())
        for name in paths:
            path = root / name
            if not path.is_file() or not path.resolve().is_relative_to(root) or not (data := path.read_bytes()).strip():
                raise Unavailable("missing or empty installed file %s" % name)
            if name == manifest_path:
                manifest = json.loads(data)
                if manifest.get("name") != "researcher" or manifest.get("version") != version:
                    raise Unavailable("the installed manifest disagrees with the registered release")
            digest.update(name.encode() + b"\0" + hashlib.sha256(data).digest())
        python = compatible_python(cfg, root)
        binary = python.stat()
        digest.update(json.dumps([str(python), binary.st_dev, binary.st_ino, binary.st_mtime_ns, binary.st_size]).encode())
        return Payload(str(root), version, str(python), digest.hexdigest())
    except Unavailable:
        raise
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as e:
        raise Unavailable("the installed files or installation registry could not be read") from e


def on_host(cfg: dict, host: str, cli: str, session_id: str) -> Payload:
    """Verify on the receiving machine, using the existing authenticated SSH RPC when remote."""
    try:
        if host == cfg["host_id"]:
            return local(cfg, cli, session_id)
        result = remote.call(cfg, host, {"op": "researcher-payload", "cli": cli, "session_id": session_id})
        if "error" in result:
            error = NoInterpreter if result.get("missing_interpreter") is True else Unavailable
            raise error(result["error"])
        payload = Payload(**result)
        if (not Path(payload.root).is_absolute() or not Path(payload.python).is_absolute()
                or version_key(payload.version)[:3] < MINIMUM
                or not re.fullmatch(r"[0-9a-f]{64}", payload.fingerprint)):
            raise Unavailable("the remote verifier returned an invalid payload")
        return payload
    except (remote.RemoteError, TypeError, Unavailable) as e:
        if isinstance(e, NoInterpreter):
            raise NoInterpreter("%s for %s on %s: %s. Delivery will retry."
                                % (FAILURE_PREFIX, cli, host, e)) from e
        if isinstance(e, Unavailable):
            reason = str(e)
        else:
            reason = "could not verify on the target host. Check connectivity and update Agent Mail there"
        raise Unavailable(
            "%s for %s on %s: %s. Install or refresh the Researcher plugin (0.7 or newer); delivery will retry."
            % (FAILURE_PREFIX, cli, host, reason)
        ) from e
