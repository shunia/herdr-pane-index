#!/usr/bin/env python3
"""Label each pane border with its position: w<workspace>:t<tab>:p<pane>.

Herdr exposes no pane ordinal, so it is derived from two things that are:

  * a pane id suffix is a monotonic per-workspace counter, so decoding it yields
    creation order without the plugin keeping a registry of its own. A pane moved
    in from another tab is older, so it takes an earlier position. If the ids ever
    stop decoding the plugin falls back to layout position (top to bottom, left to
    right) rather than emitting arbitrary numbers;
  * the snapshot lists workspaces, and the tabs inside each workspace, in display
    order -- the same order the tab bar numbers them. `tab.number` is the id
    counter, not the position, so it is deliberately not used.

The label is published through pane metadata `--title`, because that is the only
channel that outranks a manual pane name on the border. Agents (Command Code,
Claude Code) report their session title into that same channel, so a title the
pane already carries is preserved as a suffix.

Settings live in $HERDR_PLUGIN_CONFIG_DIR/config.toml: `separator`,
`title_separator`, `max_title_width`, `keep_title`.
"""
try:
    import fcntl
except ImportError:  # only linux and macos are declared, but do not crash elsewhere
    fcntl = None
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata

HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
# The plugin id, and the metadata source every reported title is attributed to.
SOURCE = "shunia.pane-index"
STATE_VERSION = 2
STATE_FIELDS = ("title", "shown", "label")
ELLIPSIS = "…"

# herdr normalises a reported title to 80 characters after stripping control
# characters (app/api/panes.rs, normalize_presentation_text). A longer label
# comes back shorter than we wrote it and looks like somebody else's edit, so
# the plugin trims to the same budget before publishing.
TITLE_CHAR_CAP = 80
# An error string ends up on one log line, so keep it short.
ERROR_CHAR_CAP = 200
# A wedged server must not stall a hook forever while it holds the reconcile lock.
COMMAND_TIMEOUT_SECONDS = 20
# Coalescing settles in one or two passes; the cap is only here so a pathological
# event stream cannot spin forever.
COALESCE_LIMIT = 10

# Herdr's public-id alphabet, how it encodes counters (mirrors workspace.rs).
ALPHABET = "123456789ABCDEFGHJKMNPQRSTVWXYZ0"
ID_SUFFIX = re.compile(r"^[wtp]([0-9A-Z]+)$")
# Recovery path only, for a state file that was wiped or written by an older
# version. The primary recognition is an exact match against the label recorded
# in the state, which survives every setting change; this shape is only a guess.
OWN_HEAD = re.compile(r"^w[0-9]+[^0-9A-Za-z]*t[0-9]+[^0-9A-Za-z]*p[0-9]+")

# Characters a one-line border label cannot show or must not carry: control,
# surrogate, and the line and paragraph separators, plus the explicit
# bidirectional overrides, which can reorder or hide the rest of a label. Wider
# format characters are deliberately kept -- a zero-width joiner is part of
# ordinary emoji sequences, and herdr keeps those too, so a label carrying one
# still round-trips.
UNSHOWABLE_CATEGORIES = ("Cc", "Cs", "Zl", "Zp")
BIDI_OVERRIDES = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")

DEFAULTS = {
    "separator": ":",
    "title_separator": " - ",
    "max_title_width": 0,
    "keep_title": True,
}


def unusable_char(char):
    return unicodedata.category(char) in UNSHOWABLE_CATEGORIES or char in BIDI_OVERRIDES


def sanitize(value):
    """Drop what a label cannot carry, the way herdr normalises a title.

    herdr strips control characters before it stores a reported title; the set
    here is a superset of that, so whatever this function leaves alone is what
    herdr stores unchanged and a later run recognises as its own.

    This is also what keeps a hand-edited state file from putting a NUL or a lone
    surrogate into argv, which subprocess rejects outright.
    """
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if not unusable_char(char))


def warn(message):
    # herdr records hook stderr in `herdr plugin log list`.
    print(f"{SOURCE}: {message}", file=sys.stderr)


def config_path():
    root = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".config", "herdr",
                            "plugins", "config", SOURCE)
    return os.path.join(root, "config.toml")


def state_dir():
    """The plugin's own directory, created if needed.

    Every run needs it -- the reconcile lock lives here -- so creating it is not
    a stray side effect but the first thing the plugin does. Mode 0700 because
    titles.json holds pane titles, which are agent session names.
    """
    root = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if not root:
        root = os.path.join(os.path.expanduser("~"), ".local", "state",
                            "herdr", "plugins", SOURCE)
    os.makedirs(root, mode=0o700, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        # Not ours to tighten (a shared or root-owned location); the write itself
        # is still private, and warning on every run would only be noise.
        pass
    return root


def state_path():
    return os.path.join(state_dir(), "titles.json")


def plugin_file(name):
    return os.path.join(state_dir(), name)


def parse_value(raw):
    raw = raw.strip()
    if raw.startswith('"'):
        end = raw.rfind('"')
        if end > 0:
            return raw[1:end]
    raw = raw.split("#", 1)[0].strip()
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    try:
        return int(raw)
    except ValueError:
        # read_config reports the mismatch against the setting's expectation,
        # which is more use than "unparsable" here.
        return None


def value_is_usable(key, value):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        # bool is an int subclass, so True would arrive as a 1-column cap.
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    # The same characters sanitize drops: herdr would not store one, so a
    # separator carrying it could never match on the next read.
    return isinstance(value, str) and not any(unusable_char(char) for char in value)


def read_config():
    """Flat `key = value` lines, so the plugin stays dependency free."""
    settings = dict(DEFAULTS)
    path = config_path()
    try:
        # utf-8-sig tolerates a BOM an editor may prepend. A file that is not
        # UTF-8 at all raises ValueError, which must not reach the caller.
        with open(path, encoding="utf-8-sig") as handle:
            lines = handle.readlines()
    except (OSError, ValueError) as error:
        warn(f"cannot read {path}: {error}")
        return settings
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key not in DEFAULTS:
            warn(f"unknown setting {key!r} in {path}")
            continue
        value = parse_value(raw)
        if isinstance(DEFAULTS[key], bool):
            # A quoted "false" is a non-empty string and would read as true.
            expected = "true or false"
        elif isinstance(DEFAULTS[key], int):
            expected = "a non-negative integer"
        else:
            expected = "a quoted string without control characters"
        if not value_is_usable(key, value):
            warn(f"{key} must be {expected}, not {raw.strip()!r}")
            continue
        settings[key] = value
    return settings


def stored_field_intact(value):
    """True when a stored field is absent, or a string sanitize would not change."""
    return value is None or (isinstance(value, str) and sanitize(value) == value)


def load_titles():
    path = state_path()
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        # An older or newer layout: start clean rather than misread it.
        warn(f"ignoring {path}: not state version {STATE_VERSION}")
        return {}
    titles = raw.get("titles")
    if not isinstance(titles, dict):
        warn(f"ignoring {path}: titles is not an object")
        return {}
    clean = {}
    dropped = 0
    repaired = 0
    for pane_id, entry in titles.items():
        if not isinstance(pane_id, str) or not isinstance(entry, dict):
            dropped += 1
            continue
        if not all(stored_field_intact(entry.get(field)) for field in STATE_FIELDS):
            # Fields we had to clean or ignore. Recoverable, but the user should
            # hear about it rather than wonder why a suffix went missing.
            repaired += 1
        title = sanitize(entry.get("title"))
        label = sanitize(entry.get("label"))
        if not title and not label:
            dropped += 1
            continue
        clean[pane_id] = {
            "title": title,
            "shown": sanitize(entry.get("shown")) if title else "",
            "label": label,
        }
    if dropped:
        warn(f"dropped {dropped} unusable entries from {path}")
    if repaired:
        warn(f"repaired {repaired} damaged entries in {path}")
    return clean


def remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def save_titles(titles):
    """Write the state file atomically, through a fresh private temp file.

    mkstemp creates with O_EXCL and mode 0600, so a symlink planted in the state
    directory cannot redirect this write, and pane titles are not left readable
    by other users.
    """
    temp = None
    try:
        descriptor, temp = tempfile.mkstemp(dir=state_dir(), prefix="titles.")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": STATE_VERSION, "titles": titles}, handle,
                      indent=1, sort_keys=True)
        os.replace(temp, state_path())
    except BaseException:
        if temp:
            remove_quietly(temp)
        raise


def call(*args):
    """Run one herdr command."""
    return subprocess.run([HERDR, *args], capture_output=True, text=True,
                          timeout=COMMAND_TIMEOUT_SECONDS)


def snapshot():
    result = call("api", "snapshot")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:ERROR_CHAR_CAP])
    return json.loads(result.stdout)["result"]["snapshot"]


def counter(entity_id):
    """Decode the counter in a public id suffix; None when it does not decode.

    Requires the qualified form (w1:p3). A bare "wP" has no suffix counter, and
    reading its own character as one would silently order panes wrongly.
    """
    if ":" not in entity_id:
        return None
    match = ID_SUFFIX.match(entity_id.rsplit(":", 1)[-1])
    if not match:
        return None
    total = 0
    for char in match.group(1):
        index = ALPHABET.find(char)
        if index < 0:
            return None
        total = total * len(ALPHABET) + index
    return total


def char_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text):
    return sum(char_width(char) for char in text)


def truncate_width(text, max_width):
    """Herdr's own truncate_end shape: keep max_width-1 columns, then an ellipsis.

    Counting display columns rather than characters keeps this consistent with
    the truncation herdr applies itself, so a CJK title is not cut twice as far.
    """
    if max_width <= 0 or display_width(text) <= max_width:
        return text
    if max_width == 1:
        return ELLIPSIS
    kept = ""
    width = 0
    for char in text:
        step = char_width(char)
        if width + step > max_width - 1:
            break
        kept += char
        width += step
    return kept + ELLIPSIS


def fit_title(title, max_width, max_chars):
    """Fit a title into herdr's character cap and the configured column cap.

    herdr counts characters for its own cap and the plugin counts columns for
    max_title_width, so both are applied; the character pass goes first so only
    one ellipsis can be appended.
    """
    if max_chars < 1:
        return ""
    if len(title) > max_chars:
        if max_chars == 1:
            title = ELLIPSIS
        else:
            title = title[:max_chars - 1] + ELLIPSIS
    return truncate_width(title, max_width)


def order_panes(pane_list, rects):
    """Creation order, degrading to layout order if the ids do not decode."""
    counters = [counter(p["pane_id"]) for p in pane_list]
    if None in counters or len(set(counters)) != len(counters):
        warn(f"pane ids did not decode uniquely in {pane_list[0].get('tab_id')} "
             f"({counters}); using layout order")
        return sorted(pane_list, key=lambda p: rects.get(p["pane_id"], (0, 0)))
    return [pane for _, pane in sorted(zip(counters, pane_list))]


def set_title(pane_id, title):
    # report-metadata prints nothing on success, so only the exit code is a signal.
    result = call("pane", "report-metadata", pane_id, "--source", SOURCE,
                  "--title", title)
    return result.returncode == 0


def clear_title(pane_id):
    result = call("pane", "report-metadata", pane_id, "--source", SOURCE,
                  "--clear-title")
    return result.returncode == 0


def layout_rects(snapshot_data):
    rects = {}
    for layout in snapshot_data.get("layouts", []):
        for pane in layout.get("panes", []):
            rect = pane.get("rect") or {}
            rects[pane["pane_id"]] = (rect.get("y", 0), rect.get("x", 0))
    return rects


def iter_position_groups(snapshot_data):
    """(workspace position, tab position, panes by creation order) per tab."""
    rects = layout_rects(snapshot_data)
    tabs_of = {}
    for tab in snapshot_data["tabs"]:
        tabs_of.setdefault(tab["workspace_id"], []).append(tab)
    panes_of = {}
    for pane in snapshot_data["panes"]:
        panes_of.setdefault(pane["tab_id"], []).append(pane)

    for workspace_index, workspace in enumerate(snapshot_data["workspaces"], start=1):
        tabs = tabs_of.get(workspace["workspace_id"], [])
        for tab_index, tab in enumerate(tabs, start=1):
            panes = panes_of.get(tab["tab_id"], [])
            yield workspace_index, tab_index, order_panes(panes, rects) if panes else []


def split_label(current, title_separator):
    """(<our suffix>, <title we did not write>) for a pane's current title.

    Used when the state has no exact record of this label -- a wiped state file,
    or a label written by an older version. The positions are matched by shape
    rather than by the configured separator, so changing the separator does not
    make our own label look foreign and get appended to itself.
    """
    if not current.strip():
        return None, None
    match = OWN_HEAD.match(current)
    if not match:
        return None, current
    rest = current[match.end():]
    if not rest.strip():
        return None, None
    if title_separator and rest.startswith(title_separator):
        rest = rest[len(title_separator):]
    else:
        # Accept any punctuation run so a title_separator change is tolerated.
        rest = re.sub(r"^[^0-9A-Za-z]+", "", rest)
    return (rest or None), None


def derive_label(pane_id, current, positions, settings, titles):
    """(<label to publish>, <state entry to record>) for one pane.

    The entry is None whenever there is nothing worth recording, which is every
    pane under `keep_title = false`.
    """
    if not settings["keep_title"]:
        return positions, None

    entry = titles.get(pane_id) or {}
    if entry.get("label") == current:
        # Our own output, unchanged. Matching the whole label rather than its
        # shape survives every separator change, and keeps the untruncated title
        # so raising a cap later can widen the label again.
        full = entry.get("title")
    else:
        suffix, foreign = split_label(current, settings["title_separator"])
        if foreign:
            # Somebody else's title, usually an agent session name.
            full = foreign
        elif suffix is not None and suffix != entry.get("shown"):
            # Our label's shape, but not the text we last wrote.
            full = suffix
        else:
            full = entry.get("title")

    # herdr trims a published title, so store the trimmed form: a suffix carrying
    # padding would not match what a later read returns.
    full = sanitize(full).strip()
    if not full:
        return positions, {"label": positions}

    shown = fit_title(full, settings["max_title_width"],
                      TITLE_CHAR_CAP - len(positions)
                      - len(settings["title_separator"]))
    label = positions
    if shown:
        label = positions + settings["title_separator"] + shown
    label = label.strip()
    return label, {"title": full, "shown": shown, "label": label}


def reconcile():
    settings = read_config()
    titles = load_titles() if settings["keep_title"] else {}
    live = set()
    overflowed = False

    for workspace_index, tab_index, panes in iter_position_groups(snapshot()):
        for pane_index, pane in enumerate(panes, start=1):
            pane_id = pane["pane_id"]
            live.add(pane_id)
            positions = settings["separator"].join(
                (f"w{workspace_index}", f"t{tab_index}", f"p{pane_index}"))
            if len(positions) > TITLE_CHAR_CAP:
                # herdr stores only the first TITLE_CHAR_CAP characters, so a
                # label this long would come back different from what we wrote
                # and be rewritten on every event without ever settling.
                if not overflowed:
                    warn(f"separator is too long: the positions are "
                         f"{len(positions)} characters, over herdr's "
                         f"{TITLE_CHAR_CAP}-character limit; leaving labels alone")
                    overflowed = True
                continue

            current = sanitize(pane.get("title")).strip()
            label, entry = derive_label(pane_id, current, positions, settings, titles)

            # Record only once the write lands: a failed write must not leave the
            # state claiming a label that is not on the pane.
            if label == current or set_title(pane_id, label):
                if entry is not None:
                    titles[pane_id] = entry

    for pane_id in list(titles):
        if pane_id not in live:
            del titles[pane_id]
    if settings["keep_title"]:
        save_titles(titles)


def clear_all():
    """Drop this plugin's titles so the panes' own titles show through again.

    The labels are re-derived from those titles on the next reconcile, so this is
    a repair for a stuck label rather than a way to switch the plugin off --
    `herdr plugin disable` does that.
    """
    failed = 0
    for _, _, panes in iter_position_groups(snapshot()):
        for pane in panes:
            if not clear_title(pane["pane_id"]):
                failed += 1
    save_titles({})
    if failed:
        warn(f"could not clear {failed} pane labels")


def run_clear():
    """Clear under the same lock a reconcile takes.

    Without it a reconcile already in flight writes its state back over the
    cleared one and the labels look like they never went away. Blocking is right
    here: the user asked for this, so waiting beats skipping.
    """
    if fcntl is None:
        clear_all()
        return
    with open(plugin_file("reconcile.lock"), "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            remove_quietly(plugin_file("reconcile.pending"))
            clear_all()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_reconcile():
    """One reconcile at a time, with a burst collapsing into the run in flight.

    herdr dispatches the hooks for one operation concurrently -- a workspace
    create fires three events within a few milliseconds -- so without this every
    event would pay a full snapshot. A run that cannot take the lock flags the
    holder and returns; the holder re-checks the flag and runs again.

    That guarantees a reconcile after the burst, not after every individual
    event: a flag written between the holder's last check and its release is
    consumed by the next invocation instead. Making every caller block on the
    lock would close that window by giving up the coalescing altogether, which is
    not worth it for scheduling that settles on the next event anyway.
    """
    if fcntl is None:
        reconcile()
        return

    pending = plugin_file("reconcile.pending")
    # "a" rather than "w" for both files: a symlink planted at either path is then
    # never truncated, and only the path's existence matters here.
    with open(plugin_file("reconcile.lock"), "a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            with open(pending, "a", encoding="utf-8"):
                pass
            return
        try:
            for _ in range(COALESCE_LIMIT):
                remove_quietly(pending)
                reconcile()
                if not os.path.exists(pending):
                    # A loser's flag write is a single file create; a short pause
                    # keeps one landing just after the check from being missed.
                    time.sleep(0.05)
                    if not os.path.exists(pending):
                        return
            warn("coalescing did not settle; leaving the rest to the next event")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


if __name__ == "__main__":
    try:
        if "--clear" in sys.argv[1:]:
            run_clear()
        else:
            run_reconcile()
    except Exception as error:
        warn(str(error))
        raise SystemExit(1)
