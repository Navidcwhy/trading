"""Loads config.json and .env (keys never live in config.json).

Profiles: a second desk runs from the same code with its own rules. `--profile NAME`
(or DESK_PROFILE) merges config.NAME.json over config.json and gives that desk its own
state (data/<mode>-NAME/) and logs (logs/NAME/). No profile = the main desk, exactly as
before. Profiles are paper-only: two desks on one exchange account would trade against
each other's positions.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_CONFIRM_PHRASE = "I_ACCEPT_REAL_MONEY_RISK"
PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,23}")


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def data_path(mode: str, profile: str = "") -> Path:
    """Folder holding a desk's state (not created here)."""
    return ROOT / "data" / (f"{mode}-{profile}" if profile else mode)


def profile_names() -> list[str]:
    """Profiles that have a config.<name>.json next to config.json."""
    out = []
    for p in sorted(ROOT.glob("config.*.json")):
        name = p.name[len("config."):-len(".json")].lower()
        if PROFILE_RE.fullmatch(name) and name != "main":
            out.append(name)
    return out


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


class Config(dict):
    """dict with attribute-ish helpers."""

    @property
    def mode(self) -> str:
        return self["mode"]

    @property
    def profile(self) -> str:
        """'' = the main desk."""
        return self.get("profile") or ""

    @property
    def desk_name(self) -> str:
        return self.profile or "main"

    def section(self, name: str) -> dict:
        return self.get(name, {})

    @property
    def data_dir(self) -> Path:
        d = data_path(self.mode, self.profile)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def log_dir(self) -> Path:
        d = ROOT / "logs" / self.profile if self.profile else ROOT / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d


def env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


def load_config(path: str | None = None, profile: str | None = None) -> Config:
    load_env(ROOT / ".env")
    p = Path(path) if path else ROOT / "config.json"
    cfg = Config(json.loads(p.read_text(encoding="utf-8")))
    if profile is None:
        profile = env("DESK_PROFILE") or ""
    profile = profile.strip().lower()
    if profile == "main":
        profile = ""
    if profile:
        if not PROFILE_RE.fullmatch(profile):
            raise SystemExit(f"Invalid profile name {profile!r} (a-z, 0-9, - and _, max 24 chars)")
        over = ROOT / f"config.{profile}.json"
        if not over.exists():
            raise SystemExit(f"Unknown profile {profile!r}: {over.name} not found")
        _merge(cfg, json.loads(over.read_text(encoding="utf-8")))
    cfg["profile"] = profile            # set after the merge so the file cannot spoof it
    override = env("DESK_MODE")
    if override:
        cfg["mode"] = override
    if cfg["mode"] not in ("paper", "testnet", "live"):
        raise SystemExit(f"Unknown mode {cfg['mode']!r} (paper | testnet | live)")
    if profile and cfg["mode"] != "paper":
        raise SystemExit(f"Profile {profile!r} is paper-only (mode is {cfg['mode']!r}): two desks on one "
                         "exchange account would trade against each other's positions.")
    return cfg
