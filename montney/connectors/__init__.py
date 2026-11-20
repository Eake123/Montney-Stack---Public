"""Source registry: loads YAML specs and builds connectors.

Specs are read from the repo's sources/ and then the user's home sources/
(a user spec with the same id overrides the built-in one). Set `enabled: false`
to switch a source off without deleting it.
"""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import yaml

from montney import paths
from montney.connectors.base import Connector, secret_names
from montney.connectors.http import HttpConnector


def spec_files() -> list[Path]:
    files = sorted(paths.BUILTIN_SOURCES.glob("*.y*ml"))
    files += sorted(paths.user_sources().glob("*.y*ml"))
    return files


def load_specs(include_disabled: bool = False) -> dict[str, dict]:
    specs: dict[str, dict] = {}
    for f in spec_files():
        spec = yaml.safe_load(f.read_text(encoding="utf-8"))
        if not isinstance(spec, dict) or "id" not in spec:
            continue
        spec["_file"] = str(f)
        specs[spec["id"]] = spec
    if not include_disabled:
        specs = {k: v for k, v in specs.items() if v.get("enabled", True)}
    return specs


def build(spec: dict) -> Connector:
    kind = spec.get("type", "http")
    if kind == "http":
        return HttpConnector(spec)
    if kind == "python":
        return _load_class(spec)(spec)
    raise ValueError(f"{spec['id']}: unknown source type {kind!r}")


def _load_class(spec: dict) -> type[Connector]:
    """`module: montney.connectors.open_meteo` or `file: plugins/foo.py`, plus `class: Name`."""
    cls_name = spec["class"]
    if "module" in spec:
        mod = importlib.import_module(spec["module"])
    else:
        # Plugin files are only loaded from the repo's or the user's plugins/ dir.
        f = Path(spec["file"])
        candidates = [paths.REPO_ROOT / f, paths.home() / f]
        path = next((c for c in candidates if c.is_file()), None)
        allowed = (paths.REPO_ROOT / "plugins", paths.home() / "plugins")
        if path is None or not any(_within(path, a) for a in allowed):
            raise FileNotFoundError(f"{spec['id']}: plugin file must live in plugins/: {f}")
        s = importlib.util.spec_from_file_location(f"montney_plugin_{spec['id']}", path)
        mod = importlib.util.module_from_spec(s)
        s.loader.exec_module(mod)
    return getattr(mod, cls_name)


def _within(p: Path, parent: Path) -> bool:
    try:
        p.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def required_secrets(spec: dict) -> set[str]:
    return secret_names(spec) | set(spec.get("secrets", []))


def parse_interval(s: str | int | float) -> float:
    """'30s' / '15m' / '2h' / '1d' / 900 -> seconds."""
    if isinstance(s, (int, float)):
        return float(s)
    s = s.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in mult:
        return float(s[:-1]) * mult[s[-1]]
    return float(s)
