#!/usr/bin/env python3
"""
prioritize_vmr_standalone.py

All-in-one version of the PMM model-priority tool: CLI + GUI in a single
file, so it can be frozen into one standalone .exe with PyInstaller and
shared with people who don't have Python installed.

The GUI is built with pywebview: a small native window that renders
HTML/CSS/JS using your OS's built-in web engine (WebView2 on Windows,
already installed on virtually every Windows 10/11 machine since it
ships with Edge). Python does all the actual file processing; the web
page is just the visual shell, talking to Python through a small
JS <-> Python bridge (the Api class below).

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

APP_VERSION = "1.0.4"

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
    path = get_config_path()
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
    text = _read_vmr_text(input_path)

    rule_matches = list(FULL_RULE_RE.finditer(text))
    if not rule_matches:
        raise ValueError(
            f"No ModelMatchRule entries found in {input_path.name} - is this a valid "
            "vPilot .vmr file?"
        )
    header = text[:rule_matches[0].start()]
    footer = text[rule_matches[-1].end():]

    order_labels = [entry["label"] for entry in library_defs] + ["Other"]
    buckets = {label: [] for label in order_labels}
    per_library_counts = {label: 0 for label in order_labels}

    total_rules = 0
    dropped_rules = 0
    unclassified_rules = 0  # rules with no ModelName attribute at all

    for rule_match in rule_matches:
        rule_text = rule_match.group(0)
        model_match = MODEL_RULE_RE.search(rule_text)
        if not model_match:
            unclassified_rules += 1
            buckets["Other"].append(rule_text)
            per_library_counts["Other"] += 1
            continue

        total_rules += 1
        prefix, model_list, suffix = model_match.groups()
        models = [m for m in model_list.split("//") if not is_excluded(m)]
        if not models:
            dropped_rules += 1
            continue

        winner_label = None
        winner_models = []
        for entry in library_defs:
            matched = [m for m in models if matches(m, entry)]
            if matched:
                winner_label = entry["label"]
                winner_models = matched
                break
        if winner_label is None:
            winner_label = "Other"
            winner_models = models

        new_attr = f"{prefix}{'//'.join(winner_models)}{suffix}"
        new_rule_text = rule_text[:model_match.start()] + new_attr + rule_text[model_match.end():]
        buckets[winner_label].append(new_rule_text)
        per_library_counts[winner_label] += 1

    output_dir.mkdir(parents=True, exist_ok=True)
    written_files = []
    for idx, label in enumerate(order_labels, start=1):
        rules = buckets[label]
        if not rules:
            continue
        filename = f"{idx} - {sanitize_filename(label)}.vmr"
        path = output_dir / filename
        content = header + "".join(rules) + footer
        path.write_text(content, encoding="utf-8")
        written_files.append({"path": str(path), "label": label, "count": len(rules)})

    return {
        "total": total_rules,
        "dropped": dropped_rules,
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
        html=HTML.replace("__APP_VERSION__", APP_VERSION),
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


HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root {
    --bg-main: #141416;
    --bg-card: #202022;
    --bg-row-hover: #28282a;
    --border: #323234;
    --text-primary: #fafafb;
    --text-muted: #b8b8bd;
    --text-faint: #8f8f95;
    --accent-blue: #0a84ff;
    --accent-green: #2ecc71;
    --accent-amber: #f0a020;
    --accent-red: #e5484d;
    --accent-blue-hover: #0074e0;
    --ease-out: cubic-bezier(.22, 1, .36, 1);
    --radius-sm: 14px;
    --radius-md: 28px;
    --radius-pill: 999px;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; height: 100%; background: var(--bg-main); color: var(--text-primary);
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    -webkit-user-select: none; user-select: none; overflow: hidden;
  }
  #app { display: flex; flex-direction: column; height: 100vh; padding: 12px 20px 20px; gap: 12px; }

  header { display: flex; align-items: center; gap: 10px; }
  .bars { display: flex; flex-direction: column; justify-content: center; gap: 2px; height: 22px; }
  .bar { height: 6px; border-radius: 3px; }
  .bar:nth-child(1) { width: 20px; background: var(--accent-blue); }
  .bar:nth-child(2) { width: 15px; background: var(--accent-green); }
  .bar:nth-child(3) { width: 10px; background: var(--accent-amber); }
  .title-block { display: flex; align-items: baseline; }
  .app-title { font-size: 19px; font-weight: 700; }
  .app-subtitle { font-size: 14px; color: var(--text-muted); margin-left: 6px; }
  .app-version { font-size: 11px; font-weight: 700; color: var(--text-faint); margin-left: 8px; }
  header .spacer { flex: 1; }
  .ghost-btn {
    background: var(--bg-card); border: 1px solid var(--border); color: var(--text-muted);
    padding: 6px 14px; border-radius: var(--radius-pill); font-size: 12px; cursor: pointer;
  }
  .ghost-btn:not(:disabled):hover { background: var(--bg-row-hover); }
  .ghost-btn:disabled { opacity: .35; cursor: default; }

  .status-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 4px 0; }
  .status-row { display: flex; align-items: center; padding: 10px 22px; font-size: 13px; }
  .status-row .dot { width: 8px; height: 8px; border-radius: 50%; margin-right: 10px; flex-shrink: 0; }
  .status-row .label { flex: 1; }
  .status-row .value { color: var(--text-muted); font-size: 12px; }

  .tabs {
    position: relative; display: flex; gap: 6px;
    background: var(--bg-main); border-radius: var(--radius-pill); padding: 3px;
  }
  .tab-indicator {
    position: absolute; top: 3px; bottom: 3px; left: 0; width: 0;
    border-radius: var(--radius-pill);
    background: linear-gradient(180deg, #ffffff, #f0f0f2 100%);
    box-shadow:
      0 1px 3px rgba(0,0,0,.35),
      0 0 0 1px rgba(255,255,255,.5) inset,
      0 1px 0 rgba(255,255,255,.8) inset;
    backdrop-filter: blur(6px) saturate(160%);
    -webkit-backdrop-filter: blur(6px) saturate(160%);
    transform: translateX(var(--indicator-x, 0px)) scale(var(--indicator-scale, 1));
    transition: transform .32s var(--ease-out), width .32s var(--ease-out);
    pointer-events: none;
  }
  .tabs:has(.tab-btn.active:active) .tab-indicator {
    --indicator-scale: .97;
    transition-duration: .05s;
  }
  .tab-btn {
    position: relative; z-index: 1;
    background: transparent; border: none; color: var(--text-muted); font-weight: 700;
    font-size: 12px; padding: 7px 18px; border-radius: var(--radius-pill); cursor: pointer;
    transition: color .3s ease;
  }
  .tab-btn.active { color: var(--bg-main); }

  /* grid-rows (fr-unit) collapse: unlike max-height, this tracks the toolbar's real
     height the whole way through, so the shrink/grow reads as one smooth motion
     instead of sitting still and then snapping once max-height dips below content size. */
  .toolbar-collapse {
    display: grid; grid-template-rows: 1fr; overflow: hidden;
    margin-top: 0; margin-bottom: 0;
    transition: grid-template-rows .32s var(--ease-out),
      margin-bottom .32s var(--ease-out), margin-top .32s var(--ease-out);
  }
  /* also cancels one of the flex column's two 12px gaps around the collapsed toolbar,
     so tabs-to-content-box spacing matches the status-card-to-tabs spacing exactly */
  .toolbar-collapse.collapsed { grid-template-rows: 0fr; margin-bottom: 0; margin-top: -12px; pointer-events: none; }
  .toolbar {
    display: flex; align-items: center; gap: 6px; min-height: 0; overflow: hidden;
    opacity: 1; transition: opacity .32s var(--ease-out);
  }
  .toolbar-collapse.collapsed .toolbar { opacity: 0; }
  .icon-btn {
    width: 32px; height: 28px; border-radius: var(--radius-pill); background: var(--bg-card);
    border: 1px solid var(--border); color: var(--text-muted); cursor: pointer; font-size: 12px;
  }
  .icon-btn:disabled { opacity: .35; cursor: default; }
  .icon-btn:not(:disabled):hover { background: var(--bg-row-hover); }
  .toolbar .spacer { flex: 1; }
  #btn-add, #btn-load {
    height: 28px; border-radius: var(--radius-pill); font-size: 12px; padding: 0 14px; cursor: pointer;
  }
  #btn-add {
    background: var(--bg-card); border: 1px solid var(--border); color: var(--text-muted);
  }
  #btn-add:not(:disabled):hover { background: var(--bg-row-hover); }
  #btn-add:disabled { opacity: .35; cursor: default; }
  #btn-load {
    background: var(--accent-blue); border: 1px solid var(--accent-blue); color: #fff; font-weight: 700;
  }
  #btn-load:not(:disabled):hover { background: var(--accent-blue-hover); }
  #btn-load:disabled { opacity: .35; cursor: default; }

  .content-box {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md);
    flex: 1; min-height: 0; overflow: hidden; position: relative;
  }
  .view {
    position: absolute; inset: 0; padding: 8px; box-sizing: border-box;
    display: flex; flex-direction: column; opacity: 0; pointer-events: none;
    transition: opacity .15s ease;
  }
  .view.active { opacity: 1; pointer-events: auto; }
  /* display:block (overriding .view's flex) because a scrolling flex container's own
     end-padding is excluded from the scrollable area for absolutely-positioned overflow
     content (our pills) - it would otherwise clip the bottom gap once scrolled to the end. */
  #priority-view { overflow: auto; display: block; }
  #list-container { position: relative; }

  /* Thin themed scrollbar (replaces the default OS one, which reads as a grey
     rectangular widget next to this app's dark pill-based rounded UI). */
  #priority-view::-webkit-scrollbar, #log-view pre::-webkit-scrollbar { width: 8px; }
  #priority-view::-webkit-scrollbar-track, #log-view pre::-webkit-scrollbar-track { background: transparent; }
  #priority-view::-webkit-scrollbar-thumb, #log-view pre::-webkit-scrollbar-thumb {
    background: var(--border); border-radius: var(--radius-pill); border: 2px solid var(--bg-card);
  }
  #priority-view::-webkit-scrollbar-thumb:hover, #log-view pre::-webkit-scrollbar-thumb:hover { background: var(--text-faint); }

  .empty-state {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center; padding: 24px;
    color: var(--text-muted);
  }
  .empty-state .icon { margin-bottom: 12px; color: var(--text-faint); }
  .empty-state .icon svg { width: 34px; height: 34px; display: block; }
  .empty-state .title { font-size: 14px; font-weight: 700; color: var(--text-primary); margin-bottom: 4px; }
  .empty-state .subtitle {
    font-size: 12px; line-height: 1.4; color: var(--text-muted); max-width: 260px; min-height: calc(1.4em * 2);
  }

  .loading-overlay {
    position: absolute; inset: 0; background: var(--bg-card); border-radius: var(--radius-md);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 12px; z-index: 30;
  }
  .loading-overlay.hidden { display: none; }
  .spinner {
    width: 28px; height: 28px; border-radius: 50%;
    border: 3px solid var(--border); border-top-color: var(--accent-blue);
    animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading-overlay .loading-text { font-size: 12px; color: var(--text-muted); }

  .pill {
    position: absolute; left: 0; right: 0; height: 70px;
    /* concentric with .view's 8px padding: inner radius = outer radius - padding */
    background: var(--bg-card); border: 1px solid var(--border); border-radius: calc(var(--radius-md) - 8px);
    display: flex; align-items: center; padding: 0 10px;
    transition: transform .18s var(--ease-out), background .15s, border-color .15s;
    cursor: grab; touch-action: none;
  }
  .pill.selected { background: var(--bg-row-hover); border-color: var(--accent-blue); }
  .pill.dragging {
    transition: background .15s, border-color .15s;
    cursor: grabbing; box-shadow: 0 10px 28px rgba(0,0,0,.55); z-index: 20;
  }
  .pill .handle { color: var(--text-faint); font-size: 15px; width: 18px; text-align: center; margin-right: 6px; flex-shrink: 0; }
  .pill .badge {
    width: 20px; height: 20px; border-radius: 50%; display: flex; align-items: center;
    justify-content: center; font-size: 10px; font-weight: 700; color: var(--bg-main); margin-right: 10px; flex-shrink: 0;
  }
  .pill .info { flex: 1; min-width: 0; }
  .pill .name { font-size: 14px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pill .caption { font-size: 12px; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
  .pill .edit, .pill .remove {
    display: none; width: 26px; height: 26px; border-radius: var(--radius-pill); border: none;
    background: transparent; cursor: pointer; font-size: 13px; flex-shrink: 0;
  }
  .pill .edit { color: var(--text-muted); }
  .pill .remove { color: var(--accent-red); }
  .pill.selected .edit, .pill.selected .remove { display: block; }
  .pill .edit:hover { background: var(--bg-main); }
  .pill .remove:hover { background: #3a1f22; }

  .hint { font-size: 12px; color: var(--text-muted); line-height: 1.4; }

  .primary-btn {
    height: 46px; border-radius: var(--radius-pill); background: var(--accent-blue); border: none;
    color: #fff; font-size: 14px; font-weight: 700; cursor: pointer;
  }
  .primary-btn:not(:disabled):hover { background: var(--accent-blue-hover); }
  .primary-btn:disabled { background: var(--bg-card); color: var(--text-faint); cursor: default; }

  #log-view { padding: 14px 22px; }
  #log-view pre {
    flex: 1; margin: 0; font-family: Consolas, "Courier New", monospace; font-size: 12px;
    overflow: auto; white-space: pre-wrap; color: var(--text-primary);
    -webkit-user-select: text; user-select: text; cursor: text;
  }

  .dialog-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.55);
    display: flex; align-items: center; justify-content: center; z-index: 50;
  }
  .dialog-overlay.hidden { display: none; }
  .dialog { background: var(--bg-main); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 18px; width: 320px; }
  .dialog h3 { margin: 0 0 4px; font-size: 15px; }
  .dialog label { font-size: 12px; color: var(--text-muted); display: block; margin: 12px 0 4px; }
  .dialog input, .dialog select {
    width: 100%; background: var(--bg-card); border: 1px solid var(--border); color: var(--text-primary);
    border-radius: var(--radius-sm); padding: 9px; font-size: 13px; font-family: inherit;
  }
  .dialog select {
    appearance: none; -webkit-appearance: none; -moz-appearance: none;
    padding-right: 30px; cursor: pointer;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23b8b8bd' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 9px center; background-size: 16px;
  }
  .dialog input:focus, .dialog select:focus { outline: 1px solid var(--accent-blue); }
  .dialog-error { color: var(--accent-red); font-size: 11px; min-height: 14px; margin-top: 8px; }
  .dialog-buttons { display: flex; gap: 8px; margin-top: 16px; }
  .dialog-buttons button { flex: 1; padding: 9px; border-radius: var(--radius-pill); border: 1px solid var(--border); cursor: pointer; font-size: 13px; font-family: inherit; }
  #add-cancel { background: var(--bg-card); color: var(--text-muted); }
  #add-submit { background: var(--accent-blue); color: #fff; border: none; }
  #add-submit:hover { background: var(--accent-blue-hover); }

  .whatsnew-dialog { width: 360px; }
  .whatsnew-dialog pre {
    white-space: pre-wrap; font-family: inherit; font-size: 12px; color: var(--text-muted);
    max-height: 280px; overflow-y: auto; margin: 12px 0 0;
  }
  #whatsnew-close { background: var(--accent-blue); color: #fff; border: none; }
  #whatsnew-close:hover { background: var(--accent-blue-hover); }

  .drop-overlay {
    position: absolute; inset: 0; z-index: 40; background: var(--bg-card); border-radius: var(--radius-md);
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    text-align: center; padding: 24px;
    opacity: 1; transition: opacity .18s var(--ease-out);
  }
  .drop-overlay.hidden { opacity: 0; pointer-events: none; }
  /* Dims everything but the content-box while a file is dragged over the window (same
     rgba(0,0,0,.55) scrim as .dialog-overlay, projected outward from the content-box's
     own edges instead of a separate full-window element, so the "hole" stays exactly
     the content-box's shape), and cross-fades that box's own content with the
     drop-overlay above it, so the drop target reads as the one thing that matters. */
  .content-box { transition: box-shadow .18s var(--ease-out); }
  #app.dragging .content-box { box-shadow: 0 0 0 9999px rgba(0,0,0,.55); }
  #app.dragging .content-box .view.active { opacity: .12; }
  .drop-overlay .frame {
    position: absolute; inset: 8px; border: 2px dashed var(--accent-blue); border-radius: calc(var(--radius-md) - 6px);
    pointer-events: none;
  }
  .drop-overlay.invalid .frame { border-color: var(--accent-red); }
  .drop-overlay .icon { color: var(--accent-blue); margin-bottom: 12px; }
  .drop-overlay.invalid .icon { color: var(--accent-red); }
  .drop-overlay .icon svg { width: 34px; height: 34px; display: block; }
  .drop-overlay .title { font-size: 14px; font-weight: 700; color: var(--text-primary); margin-bottom: 4px; }
  .drop-overlay .subtitle {
    font-size: 12px; line-height: 1.4; color: var(--text-muted); max-width: 280px; min-height: calc(1.4em * 2);
  }

  /* grid-rows collapse (see .toolbar-collapse note above) so the banner's
     appearance/dismissal is one continuous motion, not a hard cut.
     Important: the grid ITEM itself (.update-banner, .update-changelog-inner)
     must carry no padding/border/margin of its own - a 0fr/1fr row's size
     floors out at its item's own box-model additions regardless of
     min-height:0, so any spacing/border/background has to live one level
     deeper, on a plain child that isn't itself the grid item. */
  .update-collapse {
    display: grid; grid-template-rows: 0fr; overflow: hidden;
    margin-top: -12px;
    transition: grid-template-rows .32s var(--ease-out),
      margin-bottom .32s var(--ease-out), margin-top .32s var(--ease-out);
  }
  .update-collapse.open { grid-template-rows: 1fr; margin-top: 0; }
  .update-banner {
    min-height: 0; overflow: hidden; opacity: 0; transition: opacity .32s var(--ease-out);
  }
  .update-collapse.open .update-banner { opacity: 1; }
  .update-banner-inner {
    background: var(--bg-card); border: 1px solid var(--accent-blue);
    border-radius: var(--radius-md); padding: 12px 16px; display: flex; flex-direction: column;
    gap: 8px;
  }
  .update-banner-row { display: flex; align-items: center; gap: 8px; }
  .update-banner-title { font-size: 13px; font-weight: 700; flex: 1; }
  .update-banner-title .v { color: var(--text-faint); font-weight: 700; }
  .update-now-btn {
    height: 28px; border-radius: var(--radius-pill); padding: 0 14px; font-size: 12px; font-weight: 700;
    background: var(--accent-blue); border: 1px solid var(--accent-blue); color: #fff; cursor: pointer;
  }
  .update-now-btn:not(:disabled):hover { background: var(--accent-blue-hover); }
  .update-now-btn:disabled { opacity: .5; cursor: default; }
  .update-changelog {
    display: grid; grid-template-rows: 0fr; overflow: hidden;
    margin-top: -8px;
    transition: grid-template-rows .32s var(--ease-out), margin-top .32s var(--ease-out);
  }
  .update-changelog.open { grid-template-rows: 1fr; margin-top: 0; }
  .update-changelog-inner { min-height: 0; overflow: hidden; }
  .update-changelog-inner pre {
    margin: -4px 0 0; font-family: inherit; font-size: 12px; color: var(--text-muted);
    white-space: pre-wrap; max-height: 160px; overflow-y: auto;
  }
  .update-progress {
    height: 4px; margin-top: 4px; border-radius: var(--radius-pill); background: var(--bg-main); overflow: hidden;
  }
  .update-progress.hidden { display: none; }
  .update-progress-fill {
    height: 100%; background: var(--accent-blue); width: 0%; transition: width .2s var(--ease-out);
  }
  .update-error { color: var(--accent-red); font-size: 11px; }
  .update-error.status { color: var(--text-muted); }
  .update-error:empty { margin-top: -8px; }
  /* height/line-height on .pct-digit-col and .pct-digit-val are set inline
     from JS, measured off the real rendered text - see ensurePctRoll(). A
     guessed CSS multiplier (line-height: 1.4 etc.) doesn't reliably match
     Segoe UI's actual metrics and made the whole banner render taller. */
  .pct-roll { display: inline-flex; vertical-align: text-bottom; }
  .pct-digit-col { display: inline-block; overflow: hidden; position: relative; }
  .pct-digit-track { display: flex; flex-direction: column; transition: transform .18s var(--ease-out); }
  .pct-digit-val { display: block; text-align: center; font-variant-numeric: tabular-nums; }

  /* Fluid press feedback, applied to every clickable button in the app. */
  .ghost-btn, .tab-btn, .icon-btn, #btn-add, #btn-load, .primary-btn,
  .dialog-buttons button, .pill .edit, .pill .remove, .update-now-btn {
    transition: transform .15s var(--ease-out), opacity .1s ease,
      background-color .15s ease, border-color .15s ease;
  }
  .ghost-btn:not(:disabled):active, .tab-btn:active, .icon-btn:not(:disabled):active,
  #btn-add:not(:disabled):active, #btn-load:not(:disabled):active, .primary-btn:not(:disabled):active,
  .dialog-buttons button:active, .pill .edit:active, .pill .remove:active,
  .update-now-btn:not(:disabled):active {
    transform: scale(.97);
    opacity: .85;
    transition-duration: .05s;
  }
</style>
</head>
<body>
<div id="app">

  <header>
    <div class="bars"><div class="bar"></div><div class="bar"></div><div class="bar"></div></div>
    <div class="title-block"><span class="app-title">PMM</span><span class="app-subtitle">Priority Tool</span><span class="app-version">v__APP_VERSION__</span></div>
    <div class="spacer"></div>
    <button class="ghost-btn" id="btn-reset" disabled>Re-detect from File</button>
  </header>

  <div class="update-collapse" id="update-collapse">
    <div class="update-banner" id="update-banner">
      <div class="update-banner-inner">
        <div class="update-banner-row">
          <div class="update-banner-title">Update available <span class="v" id="update-version"></span></div>
          <button class="ghost-btn" id="update-toggle-log" type="button">What's new</button>
          <button class="update-now-btn" id="update-now-btn" type="button">Update Now</button>
          <button class="icon-btn" id="update-dismiss" title="Dismiss" type="button">&#10005;</button>
        </div>
        <div class="update-changelog" id="update-changelog-collapse">
          <div class="update-changelog-inner">
            <pre id="update-changelog-text"></pre>
          </div>
        </div>
        <div class="update-progress hidden" id="update-progress">
          <div class="update-progress-fill" id="update-progress-fill"></div>
        </div>
        <div class="update-error" id="update-error"></div>
      </div>
    </div>
  </div>

  <div class="status-card">
    <div class="status-row">
      <div class="dot" id="file-dot" style="background: var(--text-faint)"></div>
      <div class="label">File</div>
      <div class="value" id="file-name">none loaded</div>
    </div>
    <div class="status-row">
      <div class="dot" id="lib-dot" style="background: var(--text-faint)"></div>
      <div class="label">Libraries</div>
      <div class="value" id="lib-count">-</div>
    </div>
    <div class="status-row">
      <div class="dot" id="status-dot" style="background: var(--text-faint)"></div>
      <div class="label">Status</div>
      <div class="value" id="status-text">waiting for a file</div>
    </div>
  </div>

  <div class="tabs">
    <div class="tab-indicator" id="tab-indicator"></div>
    <button class="tab-btn active" data-tab="priority">Priority</button>
    <button class="tab-btn" data-tab="log">Log</button>
  </div>

  <div class="toolbar-collapse" id="toolbar-collapse">
    <div class="toolbar">
      <button class="icon-btn" id="btn-up" disabled>&#9650;</button>
      <button class="icon-btn" id="btn-down" disabled>&#9660;</button>
      <div class="spacer"></div>
      <button id="btn-load">Load .vmr File...</button>
      <button id="btn-add" disabled>+ Add Library...</button>
    </div>
  </div>

  <div class="content-box">
    <div id="priority-view" class="view active">
      <div id="list-container"></div>
      <div class="loading-overlay hidden" id="loading-overlay">
        <div class="spinner"></div>
        <div class="loading-text" id="loading-text">Working...</div>
      </div>
    </div>

    <div id="log-view" class="view">
      <pre id="log-output"></pre>
    </div>

    <div class="drop-overlay hidden" id="drop-overlay">
      <div class="frame"></div>
      <div class="icon" id="drop-icon"></div>
      <div class="title" id="drop-title">Drop to load</div>
      <div class="subtitle" id="drop-subtitle"></div>
    </div>
  </div>

  <div class="hint">Anything not matched by a library above is kept in its original order and tried last.</div>

  <button class="primary-btn" id="btn-choose" disabled>Load a .vmr file first</button>

</div>

<div class="dialog-overlay hidden" id="add-dialog-overlay">
  <div class="dialog">
    <h3 id="dialog-title">Add Library</h3>
    <label>Library name</label>
    <input id="add-name" placeholder="e.g. My Custom Pack">
    <label>Match type</label>
    <select id="add-mode">
      <option>Starts with</option>
      <option>Contains</option>
    </select>
    <label>Match text (comma-separated for more than one)</label>
    <input id="add-patterns" placeholder="e.g. MyPack_, MYPK">
    <div class="dialog-error" id="add-error"></div>
    <div class="dialog-buttons">
      <button id="add-cancel">Cancel</button>
      <button id="add-submit">Add</button>
    </div>
  </div>
</div>

<div class="dialog-overlay hidden" id="whatsnew-overlay">
  <div class="dialog whatsnew-dialog">
    <h3>What's new in v<span id="whatsnew-version"></span></h3>
    <pre id="whatsnew-text"></pre>
    <div class="dialog-buttons">
      <button id="whatsnew-close">Got it</button>
    </div>
  </div>
</div>

<script>
  const DOT_GRADIENTS = [
    "linear-gradient(135deg, #5aa9ff, #0a67cc)",
    "linear-gradient(135deg, #58e08a, #1ea656)",
    "linear-gradient(135deg, #ffbf4d, #d17f00)",
    "linear-gradient(135deg, #c084fc, #8b2fd9)",
    "linear-gradient(135deg, #f472b6, #d1266f)",
    "linear-gradient(135deg, #5eead4, #0d9488)",
  ];
  const ITEM_H = 70, GAP = 8, SLOT = ITEM_H + GAP;
  const DROP_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    + 'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M4 19h16"/></svg>';
  const DROP_WARN_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
    + 'stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4"/><path d="M12 17h.01"/>'
    + '<path d="M10.3 3.9 1.9 18a1.5 1.5 0 0 0 1.3 2.2h17.6a1.5 1.5 0 0 0 1.3-2.2L13.7 3.9a1.5 1.5 0 0 0-2.6 0z"/></svg>';

  let libraries = [];      // [{label, mode, patterns, _uid}]
  let selectedUid = null;
  let uidCounter = 1;
  let dragState = null;    // {uid}
  const pillElements = new Map(); // uid -> DOM node

  let loadedFilePath = null;
  let loadedFileName = null;

  function describe(entry) {
    const verb = entry.mode === "contains" ? "contains" : "starts with";
    return verb + " \u00b7 " + entry.patterns.join(", ");
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.innerText = s;
    return d.innerHTML;
  }

  function withUids(list) {
    return list.map((e) => Object.assign({}, e, { _uid: uidCounter++ }));
  }

  function stripUids(list) {
    return list.map(({ _uid, count, ...rest }) => rest);
  }

  function buildPill(entry) {
    const node = document.createElement("div");
    node.className = "pill";
    node.dataset.uid = entry._uid;
    node.innerHTML =
      '<div class="handle">&#10247;</div>' +
      '<div class="badge"></div>' +
      '<div class="info"><div class="name"></div><div class="caption"></div></div>' +
      '<button class="edit" title="Edit">&#9998;</button>' +
      '<button class="remove" title="Remove">&#10005;</button>';
    node.querySelector(".name").textContent = entry.label;
    node.querySelector(".caption").textContent = describe(entry);
    node.querySelector(".edit").addEventListener("click", (e) => {
      e.stopPropagation();
      openDialog(entry._uid);
    });
    node.querySelector(".remove").addEventListener("click", (e) => {
      e.stopPropagation();
      removeLibrary(entry._uid);
    });
    node.addEventListener("pointerdown", (e) => onPointerDown(e, entry._uid));
    pillElements.set(entry._uid, node);
    return node;
  }

  function fullRender() {
    const container = document.getElementById("list-container");
    container.innerHTML = "";
    pillElements.clear();

    if (libraries.length === 0) {
      const FOLDER_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
        + 'stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>';
      const SEARCH_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
        + 'stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>';
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.innerHTML = loadedFilePath
        ? '<div class="icon">' + SEARCH_ICON + '</div><div class="title">No libraries detected</div>'
          + '<div class="subtitle">Try "Re-detect from File", or add one manually.</div>'
        : '<div class="icon">' + FOLDER_ICON + '</div><div class="title">No file loaded</div>'
          + '<div class="subtitle">Click "Load .vmr File...", or drop a file anywhere in this window.</div>';
      container.appendChild(empty);
      container.style.height = "100%";
      updateAllPositions();
      return;
    }

    libraries.forEach((entry, i) => {
      const node = buildPill(entry);
      node.style.transition = "none";
      node.style.transform = "translateY(" + (i * SLOT) + "px)";
      container.appendChild(node);
    });
    void container.offsetHeight; // force reflow before re-enabling transitions
    pillElements.forEach((n) => { n.style.transition = ""; });
    container.style.height = Math.max(0, libraries.length * SLOT - GAP) + "px";
    updateAllPositions();
  }

  function updateAllPositions() {
    libraries.forEach((entry, i) => {
      const node = pillElements.get(entry._uid);
      if (!node) return;
      const isDragged = dragState && dragState.uid === entry._uid;
      if (!isDragged) node.style.transform = "translateY(" + (i * SLOT) + "px)";
      const badge = node.querySelector(".badge");
      badge.textContent = i + 1;
      badge.style.background = DOT_GRADIENTS[i % DOT_GRADIENTS.length];
      const isSelected = entry._uid === selectedUid && !isDragged;
      node.classList.toggle("selected", isSelected);
    });
    document.getElementById("lib-count").textContent = loadedFilePath ? String(libraries.length) : "-";
    setDot("lib-dot", libraries.length > 0 ? "ok" : "idle");
    updateToolbar();
  }

  function updateToolbar() {
    const idx = selectedUid === null ? null : libraries.findIndex((l) => l._uid === selectedUid);
    document.getElementById("btn-up").disabled = (idx === null || idx === 0);
    document.getElementById("btn-down").disabled = (idx === null || idx === libraries.length - 1);
  }

  function moveSelected(delta) {
    if (selectedUid === null) return;
    const idx = libraries.findIndex((l) => l._uid === selectedUid);
    const newIdx = idx + delta;
    if (newIdx < 0 || newIdx >= libraries.length) return;
    const tmp = libraries[idx];
    libraries[idx] = libraries[newIdx];
    libraries[newIdx] = tmp;
    updateAllPositions();
    persist();
  }

  function removeLibrary(uid) {
    libraries = libraries.filter((l) => l._uid !== uid);
    if (selectedUid === uid) selectedUid = null;
    fullRender();
    persist();
  }

  function onPointerDown(e, uid) {
    if (e.target.closest(".remove") || e.target.closest(".edit")) return;
    const node = pillElements.get(uid);
    const startIdx = libraries.findIndex((l) => l._uid === uid);
    const dragStartY = e.clientY;
    const dragBaseY = startIdx * SLOT;
    let moved = false;
    dragState = { uid };
    selectedUid = uid;
    node.classList.add("dragging");
    node.setPointerCapture(e.pointerId);
    updateAllPositions();

    function onMove(ev) {
      const dy = ev.clientY - dragStartY;
      if (Math.abs(dy) > 3) moved = true;
      node.style.transform = "translateY(" + (dragBaseY + dy) + "px)";
      const targetIdx = Math.max(0, Math.min(libraries.length - 1, Math.round((dragBaseY + dy) / SLOT)));
      const curIdx = libraries.findIndex((l) => l._uid === uid);
      if (targetIdx !== curIdx) {
        const item = libraries.splice(curIdx, 1)[0];
        libraries.splice(targetIdx, 0, item);
        updateAllPositions();
      }
    }

    function onUp() {
      node.removeEventListener("pointermove", onMove);
      node.removeEventListener("pointerup", onUp);
      node.classList.remove("dragging");
      dragState = null;
      const finalIdx = libraries.findIndex((l) => l._uid === uid);
      node.style.transform = "translateY(" + (finalIdx * SLOT) + "px)";
      selectedUid = uid;
      updateAllPositions();
      persist();
    }

    node.addEventListener("pointermove", onMove);
    node.addEventListener("pointerup", onUp);
  }

  function persist() {
    if (window.pywebview && loadedFilePath) {
      window.pywebview.api.save_libraries(loadedFilePath, stripUids(libraries));
    }
  }

  // Shared dot vocabulary for the whole status card:
  //   idle = grey (not ready), busy = blue (working), ok = green (good), error = red
  function dotColor(kind) {
    return kind === "ok" ? "var(--accent-green)"
      : kind === "error" ? "var(--accent-red)"
      : kind === "busy" ? "var(--accent-blue)"
      : "var(--text-faint)";
  }

  function setDot(id, kind) {
    document.getElementById(id).style.background = dotColor(kind);
  }

  const READY_TEXT = "ready - set priority order, then Create VMR";

  function showStatus(kind, text) {
    setDot("status-dot", kind);
    document.getElementById("status-text").textContent = text;
  }

  function switchTab(name) {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === name + "-view"));
    document.getElementById("toolbar-collapse").classList.toggle("collapsed", name === "log");
    moveTabIndicator(name);
  }

  function moveTabIndicator(name) {
    const btn = document.querySelector('.tab-btn[data-tab="' + name + '"]');
    const indicator = document.getElementById("tab-indicator");
    if (!btn || !indicator) return;
    indicator.style.width = btn.offsetWidth + "px";
    indicator.style.setProperty("--indicator-x", btn.offsetLeft + "px");
  }

  let editingUid = null;

  function openDialog(uid) {
    editingUid = uid || null;
    const entry = editingUid ? libraries.find((l) => l._uid === editingUid) : null;
    document.getElementById("dialog-title").textContent = entry ? "Edit Library" : "Add Library";
    document.getElementById("add-submit").textContent = entry ? "Save" : "Add";
    document.getElementById("add-name").value = entry ? entry.label : "";
    document.getElementById("add-mode").value = entry && entry.mode === "contains" ? "Contains" : "Starts with";
    document.getElementById("add-patterns").value = entry ? entry.patterns.join(", ") : "";
    document.getElementById("add-error").textContent = "";
    document.getElementById("add-dialog-overlay").classList.remove("hidden");
    document.getElementById("add-name").focus();
  }

  function closeDialog() {
    document.getElementById("add-dialog-overlay").classList.add("hidden");
    editingUid = null;
  }

  function submitDialog() {
    const name = document.getElementById("add-name").value.trim();
    const mode = document.getElementById("add-mode").value === "Contains" ? "contains" : "prefix";
    const patterns = document.getElementById("add-patterns").value.split(",").map((s) => s.trim()).filter(Boolean);
    const errEl = document.getElementById("add-error");
    if (!name) { errEl.textContent = "Please enter a library name."; return; }
    if (!patterns.length) { errEl.textContent = "Please enter at least one match text."; return; }
    const duplicate = libraries.some((l) => l.label === name && l._uid !== editingUid);
    if (duplicate) { errEl.textContent = "A library with that name already exists."; return; }

    if (editingUid) {
      const entry = libraries.find((l) => l._uid === editingUid);
      entry.label = name;
      entry.mode = mode;
      entry.patterns = patterns;
      selectedUid = editingUid;
      fullRender();
    } else {
      const entry = { label: name, mode, patterns, _uid: uidCounter++ };
      libraries.push(entry);
      selectedUid = entry._uid;
      fullRender();
    }
    persist();
    closeDialog();
  }

  function setFileStatus(kind, name) {
    setDot("file-dot", kind);
    document.getElementById("file-name").textContent = name;
  }

  function setCreateButtonEnabled(enabled) {
    const btn = document.getElementById("btn-choose");
    btn.disabled = !enabled;
    btn.textContent = enabled ? "Create VMR" : "Load a .vmr file first";
  }

  function setFileDependentControlsEnabled(enabled) {
    document.getElementById("btn-add").disabled = !enabled;
    document.getElementById("btn-reset").disabled = !enabled;
    setCreateButtonEnabled(enabled);
  }

  function showLoading(text) {
    document.getElementById("loading-text").textContent = text;
    document.getElementById("loading-overlay").classList.remove("hidden");
    document.getElementById("btn-load").disabled = true;
    document.getElementById("btn-reset").disabled = true;
    document.getElementById("btn-choose").disabled = true;
  }

  function hideLoading() {
    document.getElementById("loading-overlay").classList.add("hidden");
    document.getElementById("btn-load").disabled = false;
    if (loadedFilePath) {
      document.getElementById("btn-reset").disabled = false;
      document.getElementById("btn-choose").disabled = false;
    }
  }

  function appendDetectedLog(name, detected, headline) {
    const logEl = document.getElementById("log-output");
    logEl.textContent += headline + "\n";
    logEl.textContent += "Detected " + detected.length + " librar" + (detected.length === 1 ? "y" : "ies") + ":\n";
    detected.forEach(function(d) {
      const countText = ("count" in d) ? " - " + d.count + " models" : "";
      logEl.textContent += "  " + d.label + countText + " (starts with \"" + d.patterns[0] + "\")\n";
    });
    logEl.textContent += "-".repeat(50) + "\n";
    logEl.scrollTop = logEl.scrollHeight;
  }

  async function loadFile() {
    if (!window.pywebview) return;
    showStatus("busy", "waiting for file selection...");
    const picked = await window.pywebview.api.pick_file();
    if (picked.cancelled) {
      showStatus("idle", loadedFilePath ? READY_TEXT : "waiting for a file");
      return;
    }
    await loadFromPath(picked.path, picked.name);
  }

  async function loadFromPath(path, name) {
    setFileStatus("busy", name);
    showLoading("Scanning " + name + "...");
    const result = await window.pywebview.api.detect_or_load(path);
    const logEl = document.getElementById("log-output");
    if (result.error) {
      logEl.textContent += "ERROR: " + result.error + "\n";
      showStatus("error", "error - see log");
      setFileStatus("error", name);
      switchTab("log");
      hideLoading();
      return;
    }

    loadedFilePath = path;
    loadedFileName = name;
    setFileStatus("ok", name);
    setFileDependentControlsEnabled(true);

    libraries = withUids(result.libraries);
    selectedUid = null;
    fullRender();
    persist();

    const headline = result.source === "saved"
      ? "Loaded: " + name + " (using your saved priority order)"
      : "Loaded: " + name + " (no saved order yet, detected from file)";
    appendDetectedLog(name, result.libraries, headline);
    hideLoading();
    showStatus("idle", READY_TEXT);
  }

  function setDropOverlay(mode, subtitle) {
    const overlay = document.getElementById("drop-overlay");
    overlay.classList.toggle("invalid", mode === "invalid");
    document.getElementById("drop-icon").innerHTML = mode === "invalid" ? DROP_WARN_ICON : DROP_ICON;
    document.getElementById("drop-title").textContent = mode === "invalid" ? "Not a .vmr file" : "Drop to load";
    document.getElementById("drop-subtitle").textContent = subtitle;
    overlay.classList.remove("hidden");
    document.getElementById("app").classList.add("dragging");
  }

  function hideDropOverlay() {
    document.getElementById("drop-overlay").classList.add("hidden");
    document.getElementById("app").classList.remove("dragging");
  }

  // A dialog (Add/Edit Library, What's new) covers the same window a file could be
  // dropped onto - dragging or dropping while one is open would show the overlay
  // behind/around the dialog and could still swap out the loaded file underneath it.
  function isModalOpen() {
    return !document.getElementById("add-dialog-overlay").classList.contains("hidden")
      || !document.getElementById("whatsnew-overlay").classList.contains("hidden");
  }

  // Called from Python (run_gui's document.on("drop", ...) handler) once it has
  // resolved the real filesystem path - the plain browser drop event below only
  // ever sees a filename, never a usable path.
  function handleDroppedFile(path) {
    hideDropOverlay();
    if (isModalOpen()) return;
    loadFromPath(path, path.split(/[\\/]/).pop());
  }

  function wireDragAndDrop() {
    let dragDepth = 0;
    let revertTimer = null;

    document.addEventListener("dragenter", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files") || isModalOpen()) return;
      e.preventDefault();
      dragDepth++;
      clearTimeout(revertTimer);
      setDropOverlay("hover", loadedFilePath ? "Replaces the current session." : "Release to load this .vmr.");
    });
    document.addEventListener("dragover", (e) => {
      if (e.dataTransfer && e.dataTransfer.types.includes("Files") && !isModalOpen()) e.preventDefault();
    });
    document.addEventListener("dragleave", () => {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) hideDropOverlay();
    });
    document.addEventListener("drop", (e) => {
      if (!e.dataTransfer || !e.dataTransfer.types.includes("Files")) return;
      dragDepth = 0;
      if (isModalOpen()) { e.preventDefault(); return; }
      e.preventDefault();
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file && !/\.vmr$/i.test(file.name)) {
        setDropOverlay("invalid", '"' + file.name + '" - drop a .vmr file instead.');
        revertTimer = setTimeout(hideDropOverlay, 1800);
        return;
      }
      // A valid-looking drop falls through to Python's own drop listener, which
      // resolves the real path and calls handleDroppedFile() above.
    });
  }

  async function redetect() {
    if (!window.pywebview || !loadedFilePath) return;
    setFileStatus("busy", loadedFileName);
    showLoading("Re-scanning " + loadedFileName + "...");
    const result = await window.pywebview.api.redetect(loadedFilePath);
    const logEl = document.getElementById("log-output");
    if (result.error) {
      logEl.textContent += "ERROR: " + result.error + "\n";
      showStatus("error", "error - see log");
      setFileStatus("error", loadedFileName);
      switchTab("log");
      hideLoading();
      return;
    }
    libraries = withUids(result.libraries);
    selectedUid = null;
    fullRender();
    persist();
    appendDetectedLog(loadedFileName, result.libraries, "Re-detected libraries from: " + loadedFileName);
    setFileStatus("ok", loadedFileName);
    hideLoading();
    showStatus("idle", READY_TEXT);
  }

  async function createVmr() {
    if (!window.pywebview || !loadedFilePath) return;
    showLoading("Processing " + loadedFileName + "...");
    showStatus("busy", "processing " + loadedFileName + "...");
    const clean = stripUids(libraries);
    const result = await window.pywebview.api.create_vmr(loadedFilePath, clean);
    const logEl = document.getElementById("log-output");
    if (result.error) {
      logEl.textContent += "ERROR: " + result.error + "\n";
      showStatus("error", "error - see log");
      switchTab("log");
    } else {
      const banner = "=".repeat(50);
      logEl.textContent += banner + "\n";
      logEl.textContent += "Priority order: " + result.order.join(" > ") + " > (everything else)\n";
      logEl.textContent += "Processing: " + result.input_name + "\n";
      logEl.textContent += "Total rules processed: " + result.total + "\n";
      logEl.textContent += "Excluded tokens containing: " + result.excluded.join(", ") + "\n";
      logEl.textContent += "Rules dropped (empty after exclusion): " + result.dropped + "\n";
      logEl.textContent += "Output folder: " + result.output_dir + "\n";
      result.files.forEach(function(f) {
        logEl.textContent += "  " + f.label + " - " + f.count + " rules -> " + f.path + "\n";
      });
      logEl.textContent += "\nNext: in vPilot, go to Settings > Model Matching > Custom Rules,\n";
      logEl.textContent += "click 'Add Custom Rule Set(s)', select all the files above, then\n";
      logEl.textContent += "use Move Up/Move Down so they match the order shown here.\n";
      logEl.textContent += "\nVMR CREATED - " + result.files.length + " file"
        + (result.files.length === 1 ? "" : "s") + " written\n";
      logEl.textContent += banner + "\n\n";
      logEl.scrollTop = logEl.scrollHeight;
      showStatus("ok", "done - " + result.files.length + " files written");
      switchTab("log");
    }
    hideLoading();
  }

  let updateInfo = null;
  let updateChangelogOpen = false;
  let updatePolling = null;

  function showUpdateBanner(info) {
    updateInfo = info;
    document.getElementById("update-version").textContent = "v" + info.version;
    document.getElementById("update-changelog-text").textContent = info.changelog || "No changelog provided.";
    const errEl = document.getElementById("update-error");
    errEl.textContent = "";
    errEl.classList.remove("status");
    document.getElementById("update-progress").classList.add("hidden");
    document.getElementById("update-collapse").classList.add("open");
  }

  function toggleUpdateChangelog() {
    updateChangelogOpen = !updateChangelogOpen;
    document.getElementById("update-changelog-collapse").classList.toggle("open", updateChangelogOpen);
    document.getElementById("update-toggle-log").textContent = updateChangelogOpen ? "Show less" : "What's new";
  }

  function dismissUpdateBanner() {
    document.getElementById("update-collapse").classList.remove("open");
  }

  // Per-digit odometer for the "NN%" in the downloading status line -
  // swapping the number via textContent every poll tick reads as flicker, so
  // instead each digit that actually changed slides out while its new digit
  // slides in (50 -> 60 only rolls the tens digit; the "%" is a static text
  // node outside the roller and is never touched by an update).
  let pctRoll = null; // {wrapper, cols: [{col, track, current}], value} once
                       // built; torn down whenever setUpdateStatusText/error
                       // path replaces #update-error's content

  function setUpdateStatusText(text) {
    const el = document.getElementById("update-error");
    el.textContent = text;
    el.classList.add("status");
    pctRoll = null; // el.textContent just tore down any roller markup
  }

  function buildDigitCol(digit, lineHeightPx) {
    const col = document.createElement("span");
    col.className = "pct-digit-col";
    col.style.height = lineHeightPx + "px";
    col.style.lineHeight = lineHeightPx + "px";
    const track = document.createElement("span");
    track.className = "pct-digit-track";
    const current = document.createElement("span");
    current.className = "pct-digit-val";
    current.style.height = lineHeightPx + "px";
    current.style.lineHeight = lineHeightPx + "px";
    current.textContent = digit;
    track.appendChild(current);
    col.appendChild(track);
    return { col, track, current };
  }

  function ensurePctRoll() {
    const el = document.getElementById("update-error");
    if (pctRoll && pctRoll.wrapper.parentElement === el) return pctRoll;
    el.textContent = "";
    el.classList.add("status");
    el.appendChild(document.createTextNode("downloading update... "));
    // Match the roller's row height to how this same text actually renders,
    // rather than guessing a CSS line-height multiplier - a guess doesn't
    // reliably match Segoe UI's real metrics and made the whole banner
    // render taller than it should.
    const lineHeightPx = el.getBoundingClientRect().height;
    const wrapper = document.createElement("span");
    wrapper.className = "pct-roll";
    el.appendChild(wrapper);
    el.appendChild(document.createTextNode("%")); // static - never rebuilt by a digit update
    pctRoll = { wrapper, cols: [], value: null, lineHeightPx };
    return pctRoll;
  }

  // Settles a column to a single child at rest, discarding any digit left
  // mid-flight by a poll tick arriving before the previous one's
  // transitionend fired.
  function settleDigitCol(colObj) {
    Array.from(colObj.track.children).forEach((child) => { if (child !== colObj.current) child.remove(); });
    colObj.track.style.transition = "none";
    colObj.track.style.transform = "translateY(0)";
    void colObj.track.offsetHeight;
    colObj.track.style.transition = "";
  }

  function animateDigitCol(colObj, newDigit, goingUp, lineHeightPx) {
    settleDigitCol(colObj);
    const outgoing = colObj.current;
    const incoming = document.createElement("span");
    incoming.className = "pct-digit-val";
    incoming.style.height = lineHeightPx + "px";
    incoming.style.lineHeight = lineHeightPx + "px";
    incoming.textContent = newDigit;

    if (goingUp) {
      colObj.track.appendChild(incoming);
      requestAnimationFrame(() => { colObj.track.style.transform = "translateY(-50%)"; });
    } else {
      colObj.track.insertBefore(incoming, outgoing);
      colObj.track.style.transition = "none";
      colObj.track.style.transform = "translateY(-50%)";
      void colObj.track.offsetHeight;
      colObj.track.style.transition = "";
      requestAnimationFrame(() => { colObj.track.style.transform = "translateY(0)"; });
    }
    colObj.current = incoming;

    colObj.track.addEventListener("transitionend", function onEnd() {
      colObj.track.removeEventListener("transitionend", onEnd);
      outgoing.remove();
      colObj.track.style.transition = "none";
      colObj.track.style.transform = "translateY(0)";
      void colObj.track.offsetHeight;
      colObj.track.style.transition = "";
    });
  }

  function setDownloadPercent(pct) {
    const roll = ensurePctRoll();
    if (pct === roll.value) return;

    const newDigits = String(pct).split("");

    // Digit count changed (crossing 9<->10, 99<->100, or the first build) -
    // rare enough that a plain rebuild (no per-digit roll) is fine rather
    // than animating a column sliding in/out of existence.
    if (roll.cols.length !== newDigits.length) {
      roll.wrapper.innerHTML = "";
      roll.cols = newDigits.map((d) => {
        const built = buildDigitCol(d, roll.lineHeightPx);
        roll.wrapper.appendChild(built.col);
        return built;
      });
      roll.value = pct;
      return;
    }

    // roll.cols.length matching newDigits.length means String(roll.value) is
    // necessarily the same length too, so no alignment/padding is needed here.
    const oldDigits = String(roll.value).split("");
    const goingUp = pct > roll.value;
    for (let i = 0; i < newDigits.length; i++) {
      if (oldDigits[i] === newDigits[i]) continue; // this digit didn't change - leave it alone
      animateDigitCol(roll.cols[i], newDigits[i], goingUp, roll.lineHeightPx);
    }
    roll.value = pct;
  }

  function pollUpdateProgress() {
    if (updatePolling) clearInterval(updatePolling);
    updatePolling = setInterval(async () => {
      const state = await window.pywebview.api.get_update_progress();
      if (state.phase === "downloading") {
        const pct = state.total ? Math.min(100, Math.round((state.downloaded / state.total) * 100)) : 0;
        document.getElementById("update-progress-fill").style.width = pct + "%";
        setDownloadPercent(pct);
      } else if (state.phase === "verifying") {
        document.getElementById("update-progress-fill").style.width = "100%";
        setUpdateStatusText("verifying download...");
      } else if (state.phase === "restarting") {
        document.getElementById("update-progress-fill").style.width = "100%";
        setUpdateStatusText("restarting...");
        // App is about to close itself and relaunch as the new version - nothing else to do.
      } else if (state.phase === "error") {
        clearInterval(updatePolling);
        updatePolling = null;
        const el = document.getElementById("update-error");
        el.classList.remove("status");
        el.textContent = state.error || "Update failed.";
        pctRoll = null; // el.textContent just tore down any roller markup
        document.getElementById("update-now-btn").disabled = false;
        document.getElementById("update-dismiss").disabled = false;
      }
    }, 400);
  }

  async function startUpdate() {
    if (!updateInfo || !window.pywebview) return;
    document.getElementById("update-now-btn").disabled = true;
    document.getElementById("update-dismiss").disabled = true;
    setDownloadPercent(0);
    document.getElementById("update-progress-fill").style.width = "0%";
    document.getElementById("update-progress").classList.remove("hidden");
    const res = await window.pywebview.api.start_update(
      updateInfo.download_url, updateInfo.version, updateInfo.changelog,
      updateInfo.asset_size, updateInfo.asset_digest
    );
    if (!res.started) return;
    pollUpdateProgress();
  }

  async function checkForUpdate() {
    if (!window.pywebview) return;
    try {
      const info = await window.pywebview.api.check_for_update();
      if (info && info.available) {
        showUpdateBanner(info);
      } else if (info && info.error) {
        document.getElementById("log-output").textContent += "Update check failed: " + info.error + "\n";
      }
    } catch (e) {
      document.getElementById("log-output").textContent += "Update check failed: " + e + "\n";
    }
  }

  async function checkPendingChangelog() {
    if (!window.pywebview) return;
    const pending = await window.pywebview.api.get_pending_changelog();
    if (pending && pending.version) {
      document.getElementById("whatsnew-version").textContent = pending.version;
      document.getElementById("whatsnew-text").textContent = pending.changelog || "No changelog provided.";
      document.getElementById("whatsnew-overlay").classList.remove("hidden");
    }
  }

  function wireStaticEvents() {
    document.getElementById("btn-up").addEventListener("click", () => moveSelected(-1));
    document.getElementById("btn-down").addEventListener("click", () => moveSelected(1));
    document.getElementById("btn-add").addEventListener("click", () => openDialog(null));
    document.getElementById("btn-load").addEventListener("click", loadFile);
    document.getElementById("btn-reset").addEventListener("click", redetect);
    document.getElementById("btn-choose").addEventListener("click", createVmr);
    document.getElementById("add-cancel").addEventListener("click", closeDialog);
    document.getElementById("add-submit").addEventListener("click", submitDialog);
    document.getElementById("add-dialog-overlay").addEventListener("click", (e) => {
      if (e.target.id === "add-dialog-overlay") closeDialog();
    });
    document.getElementById("update-toggle-log").addEventListener("click", toggleUpdateChangelog);
    document.getElementById("update-now-btn").addEventListener("click", startUpdate);
    document.getElementById("update-dismiss").addEventListener("click", dismissUpdateBanner);
    document.getElementById("whatsnew-close").addEventListener("click", () => {
      document.getElementById("whatsnew-overlay").classList.add("hidden");
    });
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
    wireDragAndDrop();
  }

  function init() {
    wireStaticEvents();
    libraries = [];
    fullRender();
    const indicator = document.getElementById("tab-indicator");
    indicator.style.transition = "none";
    moveTabIndicator("priority");
    requestAnimationFrame(() => { indicator.style.transition = ""; });
    checkPendingChangelog();
    checkForUpdate();
  }

  // pywebview injects window.pywebview asynchronously, and it can arrive a
  // few ms after this script starts running - checking it once, synchronously,
  // to decide whether to listen for "pywebviewready" is a race: if it loses,
  // no listener is ever attached and the ready event fires to nobody. Always
  // listen for it, and use DOMContentLoaded only as a timed fallback for
  // viewing this HTML directly in a plain browser with no Python bridge.
  let inited = false;
  function safeInit() {
    if (inited) return;
    inited = true;
    init();
  }
  window.addEventListener("pywebviewready", safeInit);
  if (window.pywebview) {
    safeInit();
  } else {
    window.addEventListener("DOMContentLoaded", () => {
      let attempts = 0;
      const poll = setInterval(() => {
        attempts++;
        if (inited) { clearInterval(poll); return; }
        if (window.pywebview || attempts >= 20) {
          clearInterval(poll);
          safeInit();
        }
      }, 100);
    });
  }
</script>
</body>
</html>
"""


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()
