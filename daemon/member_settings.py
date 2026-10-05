"""
Read and edit Moot Members' HAIKU plugin settings from the UI.

Each member's settings live in <settings_dir>/<name>.settings.json, under
pluginConfigs.<block>.options, where <block> is "haiku" and/or
"haiku@inline" (Claude Code reads whichever matches how the plugin was
loaded, so both blocks must carry the same value).

Only a fixed allowlist of options can be read or written through here.
participantToken is never returned, not even masked: has_token says whether
one is present, and that is all the browser ever learns. Name, address and
home options are shown read-only, because changing them changes who a
session IS. Writes keep a timestamped backup, are atomic (temp file in the
same folder, then replace), and refuse anything that doesn't validate.
Options are read when a session launches, so an edit takes effect on the
member's next relaunch.

No HTTP and no db in here: server.py does the human-only auth check and
calls these functions.
"""

import json
import os
import re
import tempfile
import time
from pathlib import Path

DEFAULT_SETTINGS_DIR = Path.home() / ".claude" / "haiku"

# name.settings.json only. Backups and old copies (.old, .before-token, ...)
# end differently and never match.
_FILE_RE = re.compile(r"^(?P<name>[A-Za-z0-9_-]+)\.settings\.json$")
_MAX_STR = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

READ_ONLY = ("participantName", "expectedName", "expectedHome", "daemonUrl")


class SettingsError(Exception):
    """A request the caller should see as a clear 400, not a 500."""


def _thresholds(v):
    if not isinstance(v, str):
        raise SettingsError("reminderThresholds must be a string such as \"60,75,85,92\" or \"off\"")
    s = v.strip()
    if s.lower() == "off":
        return "off"
    parts = [p.strip() for p in s.split(",")]
    for p in parts:
        try:
            n = float(p)
        except ValueError:
            raise SettingsError(f"reminderThresholds: {p!r} is not a number") from None
        if not (0 < n <= 100):
            raise SettingsError(f"reminderThresholds: {p} must be above 0 and at most 100")
    return ",".join(parts)


def _bool(key):
    def check(v):
        if not isinstance(v, bool):
            raise SettingsError(f"{key} must be true or false")
        return v
    return check


def _path_str(key):
    def check(v):
        if not isinstance(v, str) or not v.strip():
            raise SettingsError(f"{key} must be a non-empty string (send null to remove it)")
        v = v.strip()
        if len(v) > _MAX_STR or _CONTROL.search(v):
            raise SettingsError(f"{key} is too long or has control characters")
        return v
    return check


def _kind(v):
    if v not in ("git", "file"):
        raise SettingsError('eotKind must be "git" or "file"')
    return v


def _minutes(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0 < v <= 1440):
        raise SettingsError("eotMaxAgeMinutes must be a number above 0 and at most 1440")
    return v


# option -> validator. The one allowlist for both reading and writing.
EDITABLE = {
    "autoWake": _bool("autoWake"),
    "reminderThresholds": _thresholds,
    "eotDir": _path_str("eotDir"),
    "eotKind": _kind,
    "eotRepo": _path_str("eotRepo"),
    "eotAllowedSigners": _path_str("eotAllowedSigners"),
    "eotMaxAgeMinutes": _minutes,
    "eotCycleLive": _bool("eotCycleLive"),
    "usageDataDir": _path_str("usageDataDir"),
}


def _path_for(settings_dir, name):
    if not _FILE_RE.match(f"{name}.settings.json"):
        raise SettingsError("no such member")
    p = Path(settings_dir) / f"{name}.settings.json"
    if not p.is_file():
        raise SettingsError("no such member")
    return p


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise SettingsError(f"{path.name} cannot be read as JSON: {type(e).__name__}") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{path.name} is not a JSON object")
    return data


def _blocks(data):
    """The option dicts for every haiku block present, 'haiku' first."""
    configs = data.get("pluginConfigs")
    if not isinstance(configs, dict):
        return []
    out = []
    for key in sorted(configs, key=lambda k: (k != "haiku", k)):
        if key == "haiku" or key.startswith("haiku@"):
            opts = (configs[key] or {}).get("options") if isinstance(configs[key], dict) else None
            if isinstance(opts, dict):
                out.append((key, opts))
    return out


def _view(name, data):
    blocks = _blocks(data)
    options, read_only, in_sync, has_token = {}, {}, True, False
    for opt in list(EDITABLE) + list(READ_ONLY):
        vals = [(b, o[opt]) for b, o in blocks if opt in o]
        if not vals:
            continue
        if opt in EDITABLE and any(v != vals[0][1] for _, v in vals):
            in_sync = False  # the blocks disagree; the UI shows the 'haiku' block's value
        (options if opt in EDITABLE else read_only)[opt] = vals[0][1]
    for _, o in blocks:
        if o.get("participantToken"):
            has_token = True
    return {
        "name": name,
        "blocks": [b for b, _ in blocks],
        "has_token": has_token,
        "in_sync": in_sync,
        "options": options,
        "read_only": read_only,
    }


def list_members(settings_dir):
    d = Path(settings_dir)
    members = []
    if d.is_dir():
        for p in sorted(d.iterdir()):
            m = _FILE_RE.match(p.name)
            if m and p.is_file():
                try:
                    members.append(_view(m.group("name"), _load(p)))
                except SettingsError as e:
                    members.append({"name": m.group("name"), "error": str(e)})
    return {"members": members, "editable": sorted(EDITABLE), "note": "Options are read when a session launches; edits take effect on the member's next relaunch."}


def update_member(settings_dir, name, changes):
    """Apply {option: value, option: None (removes it)} to every haiku block.
    All-or-nothing: any bad key or value rejects the whole request."""
    if not isinstance(changes, dict) or not changes:
        raise SettingsError("changes must be a non-empty object")
    clean = {}
    for key, value in changes.items():
        if key not in EDITABLE:
            raise SettingsError(f"{key} is not an editable option")
        clean[key] = None if value is None else EDITABLE[key](value)

    path = _path_for(settings_dir, name)
    data = _load(path)
    blocks = _blocks(data)
    if not blocks:
        raise SettingsError(f"{path.name} has no haiku plugin options block")

    backup = path.with_name(f"{path.name}.before-ui-edit-{time.strftime('%Y%m%d-%H%M%S')}")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.before-ui-edit-{time.strftime('%Y%m%d-%H%M%S')}-{n}")
        n += 1
    backup.write_bytes(path.read_bytes())

    for _, opts in blocks:
        for key, value in clean.items():
            if value is None:
                opts.pop(key, None)
            else:
                opts[key] = value

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        json.loads(Path(tmp).read_text(encoding="utf-8"))  # must parse before it replaces anything
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return _view(name, data)
