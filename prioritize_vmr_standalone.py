#!/usr/bin/env python3
"""
prioritize_vmr_standalone.py

All-in-one version of the PMM model-priority tool: CLI + GUI, frozen into
one standalone .exe with PyInstaller and shared with people who don't
have Python installed.

The GUI is built with pywebview: a small native window that renders
app.html/app.css/app.js (next to this script) using your OS's built-in
web engine (WebView2 on Windows, already installed on virtually every
Windows 10/11 machine since it ships with Edge). Python does all the
actual file processing; the web page is just the visual shell, talking
to Python through a small JS <-> Python bridge (the Api class below).
PMM_Priority_Tool.spec bundles those three files via datas=[] - if you
ever add a new frontend asset file, add it there too or it won't ship
in the built .exe.

Important: vPilot picks randomly among candidates listed inside a single
ModelName="A//B//C" rule - reordering that list has no effect. Real
prioritization only happens between separate custom rule set files
(vPilot checks them in the order you arrange in its own Settings >
Model Matching > Custom Rules screen). So this tool SPLITS the input
into one .vmr file per library (named "1 - FS Traffic.vmr", "2 - FSLTL.vmr",
etc.) instead of producing one merged file. Load all of them into vPilot
as separate custom rule sets, in that same numeric order, once - vPilot
then handles the actual priority/fallback behavior natively.

Run with no arguments -> opens the GUI.

Run with a .vmr path as an argument -> runs headless:
    prioritize_vmr_standalone.exe PerfectModelMatching.vmr
    prioritize_vmr_standalone.exe PerfectModelMatching.vmr --order "FSLTL,FS Traffic,AIG,GAmod"
    prioritize_vmr_standalone.exe PerfectModelMatching.vmr OUTPUT_DIR --order "AIG,FSLTL"

The GUI saves whatever library list/order you set up to a small JSON
file (pmm_libraries.json) next to the .exe, and reloads it automatically
next time - including any libraries you added yourself. The CLI reads
the same file if present, so --order works with your custom libraries
too. Delete pmm_libraries.json to reset to the built-in defaults.

Building the .exe (do this on Windows, where you'll run it):
    pip install pywebview pyinstaller
    python -m PyInstaller PMM_Priority_Tool.spec

The finished app will be at: dist\\PMM_Priority_Tool\\PMM_Priority_Tool.exe
It's a one-directory ("onedir") build rather than a single file - the .exe
needs the _internal folder next to it, so share/zip the whole
dist\\PMM_Priority_Tool folder, not just the .exe. This launches in well
under 200ms instead of the ~650ms a --onefile build takes (onefile
re-extracts its whole bundle to a temp folder on every launch).

Note: pywebview needs the Microsoft Edge WebView2 Runtime on Windows.
It's preinstalled on virtually all Windows 10/11 machines (it ships
with Edge). If it's ever missing, it's a free download from Microsoft:
https://developer.microsoft.com/microsoft-edge/webview2/

Publishing a new release (for the in-app auto-updater):
    1. Bump APP_VERSION below AND MyAppVersion in installer.iss - both
       must match the release tag or the updater won't detect it right.
    2. Rebuild: python -m PyInstaller PMM_Priority_Tool.spec, then compile
       installer.iss with Inno Setup to get Installer\\PMM_Priority_Tool_Setup.exe.
    3. On GitHub, create a release tagged "vX.Y.Z" (must match step 1),
       write the changelog in the release notes, and attach
       PMM_Priority_Tool_Setup.exe as a release asset.
    That's it - running apps pick it up on their next launch.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.request
from collections import Counter
from pathlib import Path

APP_VERSION = "1.0.7"

# GitHub repo the auto-updater checks for new releases (owner/repo). Each
# release's tag must be "vX.Y.Z" matching APP_VERSION below, with the built
# PMM_Priority_Tool_Setup.exe attached as a release asset and the changelog
# in the release notes. Leave the placeholder in place until that repo
# exists - update checks no-op until it's a real owner/repo.
UPDATE_REPO = "iFrezeTiger/PMM-Priority-Tool"

# ---------------------------------------------------------------------------
# Built-in default libraries, highest priority first. Each entry:
#   label:    display name
#   mode:     "prefix"   -> matches if a model name STARTS WITH any pattern
#             "contains" -> matches if a model name CONTAINS any pattern
#   patterns: list of strings to match against (case-sensitive)
# ---------------------------------------------------------------------------
def build_default_libraries() -> list:
    return [
        {"label": "FS Traffic", "mode": "prefix", "patterns": ["JustFlight_AI"]},
        {"label": "FSLTL", "mode": "prefix", "patterns": ["FSLTL"]},
        {"label": "AIG", "mode": "prefix", "patterns": ["AIGAIM", "AIG_"]},
        {"label": "GAmod", "mode": "contains", "patterns": ["GAmod"]},
    ]


EXCLUDE_CONTAINS = ["STUB"]
CONFIG_FILENAME = "pmm_libraries.json"

FULL_RULE_RE = re.compile(r'[ \t]*<ModelMatchRule\b[^>]*/>\r?\n?')
MODEL_RULE_RE = re.compile(r'(ModelName=")([^"]+)(")')


# ---------------------------------------------------------------------------
# Config persistence - remembers the library list PER INPUT FILE (keyed by
# absolute path), so loading the same .vmr again later restores whatever
# priority order/edits you last set up for it, instead of one global list.
# ---------------------------------------------------------------------------
def get_config_path() -> Path:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent
    return base_dir / CONFIG_FILENAME


def get_asset_path(name: str) -> Path:
    """Locates a bundled frontend file (app.html/app.css/app.js). Unlike
    get_config_path() above, this deliberately reads from PyInstaller's
    bundled resource dir (sys._MEIPASS, the "_internal" folder in an onedir
    build) rather than next to the .exe - these are versioned, read-only
    assets that ship with each release, not a user-writable config file."""
    if getattr(sys, "frozen", False):
        base_dir = Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).resolve().parent)
    else:
        base_dir = Path(__file__).resolve().parent
    return base_dir / name


def _normalize_key(input_path) -> str:
    return str(Path(input_path).resolve())


def _load_raw_config() -> dict:
    path = get_config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_raw_config(data: dict) -> None:
    """Writes via a temp file + os.replace() rather than a direct write_text(),
    so a crash/kill mid-write can't leave pmm_libraries.json truncated/corrupt -
    a corrupt file would otherwise make _load_raw_config's broad except silently
    wipe every saved per-file library config and the saved window state."""
    path = get_config_path()
    try:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        pass  # non-fatal, e.g. install folder is read-only


def _load_all_file_configs() -> dict:
    files = _load_raw_config().get("files")
    return files if isinstance(files, dict) else {}


def get_file_config(input_path) -> list:
    """Returns the saved library list for this file, or None if never saved."""
    return _load_all_file_configs().get(_normalize_key(input_path))


def save_file_config(input_path, library_defs: list) -> None:
    data = _load_raw_config()
    all_configs = _load_all_file_configs()
    all_configs[_normalize_key(input_path)] = library_defs
    data["files"] = all_configs
    _save_raw_config(data)


def get_window_state() -> dict:
    """Returns the last-saved window position/size, or {} if never saved."""
    window = _load_raw_config().get("window")
    return window if isinstance(window, dict) else {}


def save_window_state(x, y, width, height) -> None:
    data = _load_raw_config()
    data["window"] = {"x": x, "y": y, "width": width, "height": height}
    _save_raw_config(data)


def _clamp_window_to_screens(x: int, y: int, width: int, height: int, screens: list) -> tuple:
    """Keeps a restored window position from landing somewhere unreachable -
    e.g. it was last positioned on a second monitor that's since been
    unplugged, or a monitor arrangement that's since changed. Neither
    pywebview nor Windows itself guards against this: a window created at
    coordinates outside every connected screen is placed exactly there and
    left inaccessible, confirmed by testing a raw WinForms window the same
    way. Only nudges the position when it wouldn't be reachable on any
    currently connected screen; otherwise leaves it untouched."""
    if not screens:
        return x, y

    visible = 40  # require at least this many px of the window on some screen
    if any(
        min(x + width, s.x + s.width) - max(x, s.x) >= visible
        and min(y + height, s.y + s.height) - max(y, s.y) >= visible
        for s in screens
    ):
        return x, y

    nearest = min(screens, key=lambda s: (s.x - x) ** 2 + (s.y - y) ** 2)
    clamped_x = min(max(x, nearest.x), nearest.x + max(nearest.width - width, 0))
    clamped_y = min(max(y, nearest.y), nearest.y + max(nearest.height - height, 0))
    return clamped_x, clamped_y


def _save_pending_update(version: str, changelog: str) -> None:
    """Records that an update to `version` was just installed, so the next
    launch (the relaunch after install) can show its changelog once."""
    data = _load_raw_config()
    data["update"] = {"pending_version": version, "pending_changelog": changelog}
    _save_raw_config(data)


def _pop_pending_changelog() -> dict:
    """If the version we just relaunched into matches a pending update
    recorded by _save_pending_update, returns its changelog and clears the
    marker so it only shows once. Otherwise returns {}."""
    data = _load_raw_config()
    pending = data.get("update")
    if not isinstance(pending, dict) or pending.get("pending_version") != APP_VERSION:
        return {}
    result = {
        "version": pending.get("pending_version", ""),
        "changelog": pending.get("pending_changelog", ""),
    }
    data.pop("update", None)
    _save_raw_config(data)
    return result


def load_or_detect_libraries(input_path) -> list:
    """Used by the CLI: saved config for this file if we have one, else
    fresh detection from the file's own contents."""
    saved = get_file_config(input_path)
    if saved:
        return saved
    return detect_libraries_from_file(Path(input_path))


# ---------------------------------------------------------------------------
# Core matching / processing logic
# ---------------------------------------------------------------------------
def matches(model_name: str, entry: dict) -> bool:
    patterns = [p for p in entry.get("patterns", []) if p]
    if not patterns:
        return False
    if entry.get("mode") == "contains":
        return any(p in model_name for p in patterns)
    return any(model_name.startswith(p) for p in patterns)  # default: prefix


def is_excluded(model_name: str) -> bool:
    upper = model_name.upper()
    return any(term.upper() in upper for term in EXCLUDE_CONTAINS)


def sanitize_filename(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")
    return cleaned or "Library"


def _extend_prefix(prefix: str, members: list, threshold: float = 0.9, max_extensions: int = 2) -> str:
    """
    Given a starting prefix and the model names that share it, tries to
    grow the prefix by one more delimiter-separated segment at a time -
    but only while a single continuation covers almost all the members
    (e.g. "JustFlight" -> "JustFlight_AI" when ~all of them continue with
    "_AI"). Stops as soon as the continuations diverge (e.g. FSLTL's next
    segment is an aircraft type code, which varies too much to extend
    into), so it won't accidentally swallow per-aircraft data into the
    match pattern.
    """
    current = prefix
    for _ in range(max_extensions):
        continuations = Counter()
        unextendable = 0
        for name in members:
            rest = name[len(current):]
            if not rest:
                unextendable += 1
                continue
            delim, tail = rest[0], rest[1:]
            if delim not in " _":
                unextendable += 1
                continue
            next_word_match = re.match(r"[^ _]+", tail)
            if not next_word_match or not next_word_match.group(0):
                unextendable += 1
                continue
            continuations[(delim, next_word_match.group(0))] += 1

        if not continuations:
            break
        (best_delim, best_word), best_count = continuations.most_common(1)[0]
        if best_count / len(members) < threshold:
            break
        current = current + best_delim + best_word
    return current


def _read_vmr_text(input_path: Path) -> str:
    """Reads a .vmr file as text, translating the raw OS/decode exceptions
    into a message a non-programmer can act on - e.g. a binary file picked
    via the "All files" option in the file dialog would otherwise surface
    a bare `UnicodeDecodeError` straight to the on-screen log."""
    try:
        return input_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(
            f"{input_path.name} doesn't look like a valid .vmr file (couldn't read it as "
            "text) - make sure you selected an actual vPilot model-matching rule file."
        ) from None
    except FileNotFoundError:
        raise ValueError(f"File not found: {input_path}") from None
    except PermissionError:
        raise ValueError(
            f"Couldn't open {input_path.name} - it may be open in another program, or you "
            "may not have permission to read it."
        ) from None
    except IsADirectoryError:
        raise ValueError(f"{input_path.name} is a folder, not a file.") from None


def detect_libraries_from_file(input_path: Path, min_count: int = 5, max_candidates: int = 12) -> list:
    """
    Scans a PMM .vmr file and guesses which "libraries" it contains, by
    clustering model names on a shared leading prefix - e.g. everything
    starting "FSLTL" or "JustFlight_AI". The prefix length is detected
    automatically per cluster (see _extend_prefix) rather than always
    using a fixed number of segments, since different packages use
    different naming depths for their brand vs. per-aircraft data.
    Returns candidates sorted by how many models matched, most common
    first, so the caller can decide how many to actually use.
    """
    text = _read_vmr_text(input_path)
    if not FULL_RULE_RE.search(text):
        raise ValueError(
            f"{input_path.name} isn't a .vmr file (no ModelMatchRule entries found) - "
            "select a vPilot model-matching rule file instead."
        )

    names_by_first_token: dict = {}
    for model_match in MODEL_RULE_RE.finditer(text):
        for name in model_match.group(2).split("//"):
            name = name.strip()
            if not name or is_excluded(name):
                continue
            token = re.split(r"[ _]", name, maxsplit=1)[0]
            if token:
                names_by_first_token.setdefault(token, []).append(name)

    candidates = []
    for token, members in names_by_first_token.items():
        if len(members) < min_count:
            continue
        extended_prefix = _extend_prefix(token, members)
        candidates.append({
            "label": extended_prefix,
            "mode": "prefix",
            "patterns": [extended_prefix],
            "count": len(members),
        })

    candidates.sort(key=lambda c: c["count"], reverse=True)
    return candidates[:max_candidates]


def _bucket_rules(text: str, library_defs: list) -> dict:
    """
    Buckets each ModelMatchRule in `text` by whichever library (in priority
    order) has at least one matching candidate - the first match wins the
    whole rule, same as vPilot only ever consulting one custom rule set's
    winning candidate(s). A rule with no match goes to the trailing "Other"
    bucket (index len(library_defs)). Shared by process_split (which writes
    the buckets out to files) and the Add/Edit Library dialog's live
    match-count preview (which only needs bucket sizes, with no file I/O -
    and needs to work before a library even has a confirmed unique label,
    which is why buckets are keyed by index into library_defs rather than
    by label).
    """
    rule_matches = list(FULL_RULE_RE.finditer(text))
    if not rule_matches:
        raise ValueError("No ModelMatchRule entries found - is this a valid vPilot .vmr file?")

    other_index = len(library_defs)
    buckets = {i: [] for i in range(other_index + 1)}
    total_rules = 0
    dropped_rules = 0

    for rule_match in rule_matches:
        rule_text = rule_match.group(0)
        model_match = MODEL_RULE_RE.search(rule_text)
        if not model_match:
            buckets[other_index].append(rule_text)
            continue

        total_rules += 1
        prefix, model_list, suffix = model_match.groups()
        models = [m for m in model_list.split("//") if not is_excluded(m)]
        if not models:
            dropped_rules += 1
            continue

        winner_index = None
        winner_models = []
        for i, entry in enumerate(library_defs):
            matched = [m for m in models if matches(m, entry)]
            if matched:
                winner_index = i
                winner_models = matched
                break
        if winner_index is None:
            winner_index = other_index
            winner_models = models

        new_attr = f"{prefix}{'//'.join(winner_models)}{suffix}"
        new_rule_text = rule_text[:model_match.start()] + new_attr + rule_text[model_match.end():]
        buckets[winner_index].append(new_rule_text)

    return {
        "buckets": buckets,
        "other_index": other_index,
        "rule_matches": rule_matches,
        "total": total_rules,
        "dropped": dropped_rules,
    }


def process_split(input_path: Path, output_dir: Path, library_defs: list = None) -> dict:
    """
    Splits a PMM .vmr file into one rule set per library, ordered so that
    vPilot's own custom-rule-set priority (loaded in Settings > Model
    Matching > Custom Rules, then arranged with Move Up/Move Down) decides
    which library wins - since vPilot picks randomly among the candidates
    *within* a single ModelName="A//B//C" list, reordering that list does
    nothing. Splitting into separate files is what actually lets one
    library take priority over another.

    For each original rule, whichever defined library (in priority order)
    has at least one matching candidate "wins" that rule entirely - only
    that library's candidate(s) are kept, written into that library's file.
    If none of the defined libraries match, the rule (with all of its
    non-STUB candidates) goes into an "Other" file. Rules that become
    empty after STUB-style exclusion are dropped, same as before.
    """
    library_defs = library_defs if library_defs is not None else build_default_libraries()
    labels = [entry["label"] for entry in library_defs]
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise ValueError(
            "Duplicate library name(s): " + ", ".join(duplicates)
            + " - each library needs a unique name (this usually means "
            "pmm_libraries.json was hand-edited)."
        )
    text = _read_vmr_text(input_path)
    try:
        bucketed = _bucket_rules(text, library_defs)
    except ValueError as exc:
        raise ValueError(f"{exc} ({input_path.name})") from None

    rule_matches = bucketed["rule_matches"]
    header = text[:rule_matches[0].start()]
    footer = text[rule_matches[-1].end():]
    buckets = bucketed["buckets"]
    order_labels = labels + ["Other"]

    output_dir.mkdir(parents=True, exist_ok=True)
    written_files = []
    per_library_counts = {}
    for idx, label in enumerate(order_labels):
        rules = buckets[idx]
        per_library_counts[label] = len(rules)
        if not rules:
            continue
        filename = f"{idx + 1} - {sanitize_filename(label)}.vmr"
        path = output_dir / filename
        content = header + "".join(rules) + footer
        path.write_text(content, encoding="utf-8")
        written_files.append({"path": str(path), "label": label, "count": len(rules)})

    return {
        "total": bucketed["total"],
        "dropped": bucketed["dropped"],
        "per_library": per_library_counts,
        "files": written_files,
        "output_dir": str(output_dir),
    }


# ---------------------------------------------------------------------------
# CLI mode
# ---------------------------------------------------------------------------
def parse_order_arg(raw: str, library_defs: list) -> list:
    by_label = {entry["label"]: entry for entry in library_defs}
    labels = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [label for label in labels if label not in by_label]
    if unknown:
        known = ", ".join(by_label.keys())
        print(f"Warning: unknown library name(s) {unknown}, ignoring them. Known libraries: {known}")
        labels = [label for label in labels if label in by_label]
    return [by_label[label] for label in labels]


def run_cli(argv) -> None:
    order_arg = None
    positional = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--order":
            if i + 1 >= len(argv):
                print("Error: --order requires a comma-separated list, e.g. --order \"FSLTL,FS Traffic\"")
                sys.exit(1)
            order_arg = argv[i + 1]
            i += 2
        else:
            positional.append(arg)
            i += 1

    if not positional:
        print("Usage: prioritize_vmr_standalone.exe INPUT.vmr [OUTPUT_DIR] [--order \"Lib1,Lib2,...\"]")
        sys.exit(1)

    input_path = Path(positional[0])
    if not input_path.exists():
        print(f"Error: {input_path} not found.")
        sys.exit(1)

    saved = get_file_config(input_path)
    if saved:
        library_defs = saved
        print(f"Using saved priority order for {input_path.name}.")
    else:
        library_defs = detect_libraries_from_file(input_path)
        print(f"No saved config for {input_path.name} - detected {len(library_defs)} librar"
              + ("y" if len(library_defs) == 1 else "ies") + " from the file.")

    order = parse_order_arg(order_arg, library_defs) if order_arg else library_defs

    output_dir = (
        Path(positional[1]) if len(positional) >= 2
        else input_path.with_name(input_path.stem + "_split")
    )

    result = process_split(input_path, output_dir, order)
    save_file_config(input_path, order)
    print(f"Processed {result['total']} ModelMatchRule entries.")
    if EXCLUDE_CONTAINS:
        print(f"Excluded model tokens containing: {', '.join(EXCLUDE_CONTAINS)}")
        print(f"Dropped {result['dropped']} rules that had no models left after exclusion.")
    print(f"Priority order applied: {' > '.join(e['label'] for e in order)} > (everything else)")
    print(f"Output folder: {result['output_dir']}")
    for f in result["files"]:
        print(f"  {Path(f['path']).name} - {f['count']} rules")
    print()
    print("Next step: in vPilot, go to Settings > Model Matching > Custom Rules,")
    print("click 'Add Custom Rule Set(s)', select all the files above, then use")
    print("Move Up/Move Down so they're in the same order as the numbers above.")


# ---------------------------------------------------------------------------
# Auto-updater - checks GitHub Releases for a newer version, and on request
# downloads + silently runs the installer, then relaunches. The install step
# only works in the built/frozen .exe (it replaces the running app's own
# files), so it's a no-op-with-error in a plain `python prioritize_vmr_standalone.py`
# dev run past the download step.
# ---------------------------------------------------------------------------
def _version_tuple(v: str) -> tuple:
    v = v.strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    parts = []
    for piece in v.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_update_impl() -> dict:
    if "REPLACE_WITH" in UPDATE_REPO or "/" not in UPDATE_REPO:
        return {"available": False}

    req = urllib.request.Request(
        f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "PMM-Priority-Tool-Updater"},
    )
    with urllib.request.urlopen(req, timeout=6) as resp:
        release = json.loads(resp.read().decode("utf-8"))

    tag = release.get("tag_name") or ""
    latest_version = tag[1:] if tag[:1] in ("v", "V") else tag
    if not latest_version or _version_tuple(latest_version) <= _version_tuple(APP_VERSION):
        return {"available": False}

    asset = next(
        (a for a in release.get("assets", []) if a.get("name", "").lower().endswith("setup.exe")),
        None,
    )
    if not asset:
        return {"available": False}

    return {
        "available": True,
        "version": latest_version,
        "changelog": release.get("body") or "",
        "download_url": asset["browser_download_url"],
        "asset_size": asset.get("size", 0),
        # GitHub computes this server-side on upload, formatted "sha256:<hex>".
        # Can be null for assets uploaded before GitHub added the field, or via
        # some third-party upload paths - verification below falls back to a
        # size-only check in that case.
        "asset_digest": asset.get("digest"),
    }


_UPDATE_LOCK = threading.Lock()
_UPDATE_STATE = {"phase": "idle", "downloaded": 0, "total": 0, "error": None}


def _set_update_state(**kwargs) -> None:
    with _UPDATE_LOCK:
        _UPDATE_STATE.update(kwargs)


def _get_update_state() -> dict:
    with _UPDATE_LOCK:
        return dict(_UPDATE_STATE)


def _verify_download(setup_path: Path, expected_size: int, expected_digest: str) -> str:
    """Returns an error string if the downloaded installer doesn't check out,
    or "" if it's good to run. Checked before the installer ever executes,
    since a truncated/corrupted download would otherwise get silently run
    with /VERYSILENT."""
    actual_size = setup_path.stat().st_size
    if expected_size and actual_size != expected_size:
        return f"Downloaded file size ({actual_size} bytes) doesn't match the expected size ({expected_size} bytes)."

    if expected_digest and expected_digest.startswith("sha256:"):
        expected_hex = expected_digest.split(":", 1)[1].lower()
        hasher = hashlib.sha256()
        with open(setup_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != expected_hex:
            return "Downloaded file's checksum doesn't match GitHub's - the download may be corrupted."

    return ""


def _run_update(download_url: str, version: str, changelog: str, expected_size: int = 0, expected_digest: str = "") -> None:
    try:
        _set_update_state(phase="downloading", downloaded=0, total=0, error=None)
        tmp_dir = Path(tempfile.gettempdir())
        setup_path = tmp_dir / f"PMM_Priority_Tool_Setup_{version}.exe"

        req = urllib.request.Request(download_url, headers={"User-Agent": "PMM-Priority-Tool-Updater"})
        with urllib.request.urlopen(req, timeout=15) as resp, open(setup_path, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0) or expected_size
            _set_update_state(total=total)
            downloaded = 0
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                _set_update_state(downloaded=downloaded)

        _set_update_state(phase="verifying")
        verify_error = _verify_download(setup_path, expected_size, expected_digest)
        if verify_error:
            setup_path.unlink(missing_ok=True)
            _set_update_state(phase="error", error=verify_error)
            return

        if not getattr(sys, "frozen", False):
            _set_update_state(phase="error", error="Auto-install only works in the built .exe, not a dev run.")
            return

        app_exe = str(Path(sys.executable).resolve())
        pid = os.getpid()
        bat_path = tmp_dir / f"pmm_update_{version}.bat"
        log_path = tmp_dir / "pmm_update_debug.log"
        # A batch loop piping tasklist into find (or findstr) to poll for our
        # PID's exit is unreliable here: combining CREATE_NO_WINDOW with
        # DETACHED_PROCESS below is contradictory about console allocation,
        # and on some Windows builds the piped `find` stage ends up attached
        # to its own orphaned console waiting on stdin instead of the pipe,
        # hanging forever. A single PowerShell wait avoids the pipe entirely.
        bat_lines = [
            "@echo off",
            f'set LOG="{log_path}"',
            "echo [start] waiting for app to exit > %LOG%",
            f'powershell -NoProfile -WindowStyle Hidden -Command '
            f'"while (Get-Process -Id {pid} -ErrorAction SilentlyContinue) '
            f'{{ Start-Sleep -Milliseconds 300 }}"',
            "echo [app exited] running installer >> %LOG%",
            f'"{setup_path}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS',
            "echo [installer done] exit code %errorlevel% >> %LOG%",
            f'echo [starting app] "{app_exe}" >> %LOG%',
            f'start "" "{app_exe}"',
            "echo [start issued] >> %LOG%",
            f'del "{setup_path}"',
            'del "%~f0"',
        ]
        bat_path.write_text("\r\n".join(bat_lines) + "\r\n", encoding="utf-8")

        _save_pending_update(version, changelog)
        _set_update_state(phase="restarting")

        subprocess.Popen(
            ["cmd", "/c", str(bat_path)],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )

        import webview  # deferred, same as run_gui() - keeps CLI-only usage dependency-free

        try:
            webview.windows[0].destroy()
        except Exception:
            os._exit(0)
    except Exception as exc:  # noqa: BLE001
        _set_update_state(phase="error", error=str(exc))


# ---------------------------------------------------------------------------
# GUI mode (pywebview)
# ---------------------------------------------------------------------------
def run_gui() -> None:
    import webview

    class Api:
        def pick_file(self):
            window = webview.windows[0]
            result = window.create_file_dialog(
                webview.FileDialog.OPEN,
                file_types=("VMR Files (*.vmr)", "All files (*.*)"),
            )
            if not result:
                return {"cancelled": True}
            input_path = Path(result[0])
            return {"path": str(input_path), "name": input_path.name}

        def detect_or_load(self, path):
            input_path = Path(path)
            try:
                saved = get_file_config(input_path)
                if saved:
                    return {"libraries": saved, "source": "saved"}
                return {"libraries": detect_libraries_from_file(input_path), "source": "detected"}
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}

        def redetect(self, path):
            input_path = Path(path)
            try:
                detected = detect_libraries_from_file(input_path)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}
            return {"libraries": detected}

        def save_libraries(self, path, libs):
            save_file_config(path, libs)
            return {"ok": True}

        def create_vmr(self, path, libs):
            input_path = Path(path)
            if not input_path.exists():
                return {"error": f"File not found: {input_path}"}
            output_dir = input_path.with_name(input_path.stem + "_split")
            try:
                split_result = process_split(input_path, output_dir, libs)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}
            save_file_config(input_path, libs)
            return {
                "total": split_result["total"],
                "dropped": split_result["dropped"],
                "excluded": EXCLUDE_CONTAINS,
                "input_name": input_path.name,
                "output_dir": split_result["output_dir"],
                "files": split_result["files"],
                "order": [entry["label"] for entry in libs],
            }

        def preview_match_counts(self, path, libs):
            try:
                input_path = Path(path)
                text = _read_vmr_text(input_path)
                bucketed = _bucket_rules(text, libs)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}
            return {"counts": [len(bucketed["buckets"][i]) for i in range(len(libs))]}

        def open_output_folder(self, path):
            try:
                os.startfile(path)
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}
            return {"ok": True}

        def check_for_update(self):
            try:
                return check_for_update_impl()
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}

        def start_update(self, download_url, version, changelog, asset_size=0, asset_digest=""):
            if _get_update_state()["phase"] in ("downloading", "verifying", "restarting"):
                return {"started": False}
            threading.Thread(
                target=_run_update,
                args=(download_url, version, changelog, asset_size or 0, asset_digest or ""),
                daemon=True,
            ).start()
            return {"started": True}

        def get_update_progress(self):
            return _get_update_state()

        def get_pending_changelog(self):
            return _pop_pending_changelog()

        def get_app_version(self):
            return APP_VERSION

    saved_window = get_window_state()
    window_kwargs = {
        "width": saved_window.get("width", 680),
        "height": saved_window.get("height", 800),
        "min_size": (580, 620),
        "background_color": "#141416",
    }
    if "x" in saved_window and "y" in saved_window:
        try:
            screens = webview.screens
        except Exception:
            screens = []
        window_kwargs["x"], window_kwargs["y"] = _clamp_window_to_screens(
            saved_window["x"], saved_window["y"],
            window_kwargs["width"], window_kwargs["height"],
            screens,
        )

    window = webview.create_window(
        "PMM Priority Tool",
        url=str(get_asset_path("app.html")),
        js_api=Api(),
        **window_kwargs,
    )

    def on_closing():
        try:
            save_window_state(window.x, window.y, window.width, window.height)
        except Exception:
            pass

    def _handle_dropped_file(event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        path = next((f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")), None)
        if path:
            window.evaluate_js(f"handleDroppedFile({json.dumps(path)})")

    def _enable_drag_drop():
        from webview.dom import DOMEventHandler
        window.dom.document.on("drop", DOMEventHandler(_handle_dropped_file, prevent_default=True))

    window.events.loaded += _enable_drag_drop
    window.events.closing += on_closing
    webview.start()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()
