"""Trusting a folder for both CLIs before an agent starts there, so neither stops at its trust dialog:
hasTrustDialogAccepted in the Claude login's .claude.json, and trust_level in Codex's config.toml."""
import json
from pathlib import Path

from . import commands, jsonfile, registry

CODEX_CONFIG = registry.CODEX_HOME / "config.toml"


def claude_json_for(config_dir):
    """Each login keeps its own .claude.json (project trust included): ~/.claude.json, or inside its config dir."""
    return Path(config_dir) / ".claude.json" if config_dir else registry.CLAUDE_JSON


def trust_claude(path, claude_json):
    """Mark path trusted in claude_json (new git repos under a trusted parent still prompt; this is the trust the docs
    describe). True if it wrote that, False if path was trusted already. Rewrites the file atomically, owner-only.
    Raises OSError, or ValueError for a file that is not JSON, when it cannot."""
    d = jsonfile.read(claude_json)
    entry = d.setdefault("projects", {}).setdefault(path, {})
    if entry.get("hasTrustDialogAccepted"):
        return False
    entry["hasTrustDialogAccepted"] = True
    jsonfile.write(claude_json, d, jsonfile.PRIVATE, 2)
    return True


def trust_codex(path):
    """Mark path trusted in Codex's config.toml. True if it wrote that, False if path was trusted already. Rewrites
    the file atomically, owner-only. Raises OSError when it cannot."""
    text = CODEX_CONFIG.read_text() if CODEX_CONFIG.exists() else ""
    header = "[projects.%s]" % json.dumps(path, ensure_ascii=False)   # a JSON string is a valid TOML basic string
    if header in text:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    jsonfile.write_text(CODEX_CONFIG, text + '\n%s\ntrust_level = "trusted"\n' % header, jsonfile.PRIVATE)
    return True


def ensure_trust(path, claude_json):
    """Trust the git repo root of `path` (or `path` itself) for both CLIs: {"claude": wrote it, "codex": wrote it},
    False where it was trusted already. Worktrees inherit trust from the main checkout, so this is a no-op for them.
    claude_json: the Claude login's own .claude.json (claude_json_for); a login other than the default one gets no
    .claude.json made for it, and no "claude" entry, if it has none. Raises OSError or ValueError when a trust file
    cannot be read or written."""
    main_root = commands.repo_root(path)
    out = {}
    if claude_json == registry.CLAUDE_JSON or claude_json.exists():
        out["claude"] = trust_claude(main_root, claude_json)
    out["codex"] = trust_codex(main_root)
    return out
