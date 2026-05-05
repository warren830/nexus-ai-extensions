#!/usr/bin/env python3
"""
Generate the extension manifest from Nexus's default_config.yaml.

Why: the extension's ``externally_connectable.matches`` allowlist is the
gate that lets the Nexus Web UI push tokens into the extension via
``chrome.runtime.sendMessage``. In dev that's localhost; in staging it's
one internal hostname; in prod it's yet another. Baking all of them
into the source manifest.json works for our small deployment but
doesn't scale — and more importantly, it means forking the extension
source for each environment.

This script replaces the need for hand-edits: the runtime config says
which origins to allow, and the manifest is regenerated to match. Run
it right before ``scripts/build.sh`` (the build script will call it
automatically).

Usage:
    python3 scripts/gen_manifest.py                       # use repo default
    NEXUS_CONFIG=/path/to/other.yaml python3 ...          # custom config
    python3 scripts/gen_manifest.py --dry-run             # print, don't write

The script is idempotent — running it repeatedly with the same config
yields the same manifest.json.
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001
    sys.stderr.write(
        "gen_manifest.py requires PyYAML (pip install pyyaml). "
        "Skipping — extension will use manifest.json as-is.\n"
    )
    sys.exit(0)


SCRIPT_DIR = Path(__file__).resolve().parent
EXT_ROOT = SCRIPT_DIR.parent
MANIFEST_PATH = EXT_ROOT / "manifest.json"
# Walk up to the Nexus repo root — the extension lives at
# extensions/chrome/nexus-agent-chrome/, so ../../../ is the repo root.
DEFAULT_CONFIG_PATH = (
    EXT_ROOT / ".." / ".." / ".." / "config" / "default_config.yaml"
).resolve()


def load_config(path: Path) -> dict:
    """Load YAML config. Raises if the file is unreadable rather than
    silently falling back — a broken config should fail the build
    loudly so ops notices."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_web_origins(cfg: dict) -> list[str]:
    """Pull ``browser_agent.web_origins`` out of the config. Defaults
    to the dev list if missing — same list that's hard-coded in a fresh
    manifest.json so nothing changes for someone running on defaults.

    Config layout: ``default-config.browser_agent.web_origins``. The
    legacy path ``aws.browser_agent`` is also accepted for older configs.
    """
    dc = cfg.get("default-config", {}) or {}
    ba = (dc.get("browser_agent") or {}) or (
        (cfg.get("aws", {}) or {}).get("browser_agent", {}) or {}
    )
    origins = ba.get("web_origins")
    if not origins:
        # Neutral defaults — cover local dev + AWS-hosted deployments.
        # Operators self-hosting behind a custom domain override via
        # default-config.browser_agent.web_origins in their config.
        return [
            "http://localhost:*/*",
            "http://127.0.0.1:*/*",
            "https://*.cloudfront.net/*",
            "https://*.amazonaws.com/*",
        ]
    # Normalize: trim, drop empties, enforce strings.
    clean = [o.strip() for o in origins if isinstance(o, str) and o.strip()]
    if not clean:
        raise SystemExit(
            "aws.browser_agent.web_origins is set but contains no "
            "non-empty strings."
        )
    return clean


def rewrite_manifest(manifest: dict, origins: list[str]) -> dict:
    """Return a new manifest dict with externally_connectable.matches
    replaced. Does not mutate the input (easier to diff in tests)."""
    out = json.loads(json.dumps(manifest))  # deep copy via JSON roundtrip
    out.setdefault("externally_connectable", {})
    out["externally_connectable"]["matches"] = origins
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--config",
        default=os.environ.get("NEXUS_CONFIG", str(DEFAULT_CONFIG_PATH)),
        help=f"Path to config YAML (default: {DEFAULT_CONFIG_PATH})",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rewritten manifest to stdout, don't overwrite.",
    )
    args = ap.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.exists():
        sys.stderr.write(
            f"Config not found at {config_path}; leaving manifest untouched.\n"
        )
        return 0

    cfg = load_config(config_path)
    origins = resolve_web_origins(cfg)

    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    new_manifest = rewrite_manifest(manifest, origins)

    serialized = json.dumps(new_manifest, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        sys.stdout.write(serialized)
        return 0

    current = MANIFEST_PATH.read_text(encoding="utf-8")
    if current == serialized:
        print(f"manifest.json unchanged ({len(origins)} origin(s)).")
        return 0

    MANIFEST_PATH.write_text(serialized, encoding="utf-8")
    print(f"manifest.json updated: {len(origins)} origin(s) → {', '.join(origins)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
