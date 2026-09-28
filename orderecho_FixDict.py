"""FIX tag and enumeration names.

The base dictionary is FIXReader's `dictionaries/fix_tags.json`, which is read
as-is and never modified.  A small hand-written overlay guarantees names for
the tags the viewer leans on, and supplies the one thing the base file has no
room for: names that differ *by FIX version*.

Nothing here raises.  A missing or unreadable base file degrades to
overlay-only, with a warning the caller can show.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
DICT_DIR = os.path.join(HERE, "dictionaries")
BASE_PATH = os.path.join(DICT_DIR, "fix_tags.json")
OVERLAY_PATH = os.path.join(DICT_DIR, "overlay.json")


@dataclass
class TagInfo:
    """What we know about one tag."""

    tag: int
    name: str
    data_type: str | None = None
    description: str | None = None
    values: dict = field(default_factory=dict)          # value -> description
    versions: list = field(default_factory=list)        # "4.2", "4.4", ...
    version_names: dict = field(default_factory=dict)   # "FIX.4.2" -> name


def _normalize_version(version):
    """`FIX.4.2`, `4.2` and `FIXT.1.1` all reduce to a bare number."""
    if not version:
        return None
    text = str(version)
    for prefix in ("FIXT.", "FIX."):
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


class FixDictionary:
    """Tag -> name, (tag, value) -> enum name, with version awareness."""

    def __init__(self, base_path: str | None = None,
                 overlay_path: str | None = None) -> None:
        self.warnings: list = []
        self.tags: dict = {}
        self._overlay_enums: dict = {}
        self._version_enums: dict = {}
        self._load_base(BASE_PATH if base_path is None else base_path)
        self._load_overlay(OVERLAY_PATH if overlay_path is None
                           else overlay_path)

    # ------------------------------------------------------------- loading

    def _load_base(self, path) -> None:
        """The FIXReader file: a JSON *list* of tag objects.

        Each item carries `tag` (int), `name`, `description`, `data_type`,
        `required_in`/`optional_in` (lists of {code, name}), `valid_values`
        (a value -> prose map, empty for non-enumerated tags),
        `fix_version_added`, `notes` and `fix_versions` (a list like
        ["4.2", "4.4", ...]).
        """
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            self.warnings.append(
                f"FIX dictionary not found at {path}; using the built-in "
                f"overlay only, so some tag names will be missing"
            )
            return
        except (OSError, ValueError) as exc:
            self.warnings.append(
                f"FIX dictionary at {path} could not be read ({exc}); using "
                f"the built-in overlay only"
            )
            return

        if not isinstance(data, list):
            self.warnings.append(
                f"FIX dictionary at {path} is not the expected list of tag "
                f"objects; using the built-in overlay only"
            )
            return

        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                tag = int(item["tag"])
            except (KeyError, TypeError, ValueError):
                continue
            values = item.get("valid_values")
            self.tags[tag] = TagInfo(
                tag=tag,
                name=str(item.get("name") or f"Tag{tag}"),
                data_type=item.get("data_type"),
                description=item.get("description"),
                values=dict(values) if isinstance(values, dict) else {},
                versions=list(item.get("fix_versions") or []),
            )

    def _load_overlay(self, path) -> None:
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.warnings.append(f"overlay at {path} unreadable ({exc})")
            return

        for raw_tag, spec in (data.get("tags") or {}).items():
            try:
                tag = int(raw_tag)
            except (TypeError, ValueError):
                continue
            info = self.tags.get(tag)
            if info is None:
                info = TagInfo(tag=tag, name=str(spec.get("name")
                                                 or f"Tag{tag}"))
                self.tags[tag] = info
            info.version_names.update(spec.get("versions") or {})

        for raw_tag, values in (data.get("enums") or {}).items():
            try:
                tag = int(raw_tag)
            except (TypeError, ValueError):
                continue
            self._overlay_enums[tag] = dict(values)
            if tag not in self.tags:
                self.tags[tag] = TagInfo(tag=tag, name=f"Tag{tag}")

        for version, tag_map in (data.get("version_enums") or {}).items():
            bucket = self._version_enums.setdefault(
                _normalize_version(version), {}
            )
            for raw_tag, values in (tag_map or {}).items():
                try:
                    bucket[int(raw_tag)] = dict(values)
                except (TypeError, ValueError):
                    continue

    # ------------------------------------------------------------ queries

    def tag_name(self, tag, version: str | None = None) -> str:
        try:
            tag = int(tag)
        except (TypeError, ValueError):
            return str(tag)
        info = self.tags.get(tag)
        if info is None:
            return f"Tag{tag}"
        if version and info.version_names:
            # A per-version name is an axis the base file does not have, so
            # it wins for that version (tag 32 is LastShares in 4.2).
            for candidate in (version, f"FIX.{_normalize_version(version)}"):
                if candidate in info.version_names:
                    return info.version_names[candidate]
        return info.name

    def enum_name(self, tag, value, version: str | None = None):
        """The short name for a value, or None if the tag is not enumerated."""
        try:
            tag = int(tag)
        except (TypeError, ValueError):
            return None
        if value is None:
            return None
        value = str(value)

        version_key = _normalize_version(version)
        if version_key:
            by_tag = self._version_enums.get(version_key) or {}
            named = (by_tag.get(tag) or {}).get(value)
            if named:
                return named

        info = self.tags.get(tag)
        if info and value in info.values:
            # Base prose can be a sentence; keep the part before an em dash
            # or a parenthesis so it reads as a name.
            return _short(info.values[value])

        return (self._overlay_enums.get(tag) or {}).get(value)

    def describe(self, tag, value, version: str | None = None) -> str:
        """`54 Side = 1 (Buy)` style, for the decoded view."""
        name = self.tag_name(tag, version)
        enum = self.enum_name(tag, value, version)
        text = f"{tag} {name} = {value}"
        return f"{text} ({enum})" if enum else text

    def is_enumerated(self, tag) -> bool:
        try:
            tag = int(tag)
        except (TypeError, ValueError):
            return False
        info = self.tags.get(tag)
        return bool((info and info.values) or self._overlay_enums.get(tag))

    def description(self, tag):
        info = self.tags.get(int(tag)) if str(tag).isdigit() else None
        return info.description if info else None

    def __len__(self) -> int:
        return len(self.tags)


def _short(text: str) -> str:
    """`New — order acknowledged` -> `New`."""
    if not text:
        return text
    for separator in ("—", " - ", " ("):
        if separator in text:
            text = text.split(separator)[0]
    return text.strip()


_DEFAULT: FixDictionary | None = None


def default_dictionary() -> FixDictionary:
    """One shared dictionary; loading it twice helps nobody."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = FixDictionary()
    return _DEFAULT
