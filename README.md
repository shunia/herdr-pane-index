# Pane Index

Label every pane border with its position:

```
┌ w1:t2:p3 - Refactor auth middleware ───┐
│ $                                      │
└────────────────────────────────────────┘
```

`w<workspace>:t<tab>:p<pane>` are **positions**, not the public ids herdr shows
elsewhere (`wP:p4`). The trailing part is whatever title the pane already
carried, so an agent's session name survives.

## Install

Requires **Python 3** on `PATH`. Every entry point the manifest declares — the
startup hook, both actions and all fourteen event hooks — is launched as
`python3 pane-index.py`, so without it nothing runs and the only trace is an
entry in `herdr plugin log list`.

```bash
herdr plugin install <owner>/herdr-pane-index
# or, from a checkout:
herdr plugin link ~/thirdparty/herdr-pane-index
```

Then make lone panes draw a border. Without this a single-pane tab has no top
border, so there is nothing to write on and the plugin looks broken:

```toml
# ~/.config/herdr/config.toml
[ui]
pane_borders = "always"
pane_outer_borders = true
```

If you already have a `[ui]` table, add the two keys to it rather than pasting a
second table header.

```bash
herdr server reload-config
```

A tab with two or more panes already has borders and needs none of this.

Background tabs are only resized when you switch to them, so a pane in an
inactive tab can look unchanged until you focus it.

### Actions

Both are in herdr's menu under the plugin's name, and can be bound with
`type = "plugin_action"`:

```toml
[[keys.command]]
key = "prefix+alt+i"
type = "plugin_action"
command = "shunia.pane-index.sync"
description = "resync pane labels"
```

* **resync labels** (`shunia.pane-index.sync`) — run a reconcile now, without
  waiting for the next event.
* **drop labels** (`shunia.pane-index.clear`) — drop this plugin's titles so the
  panes' own titles show through again. The labels are then re-derived from those
  titles on the next reconcile, so this repairs a stuck label; it is not a way to
  switch the plugin off. `herdr plugin disable shunia.pane-index` is.

## Settings

Optional, in `herdr plugin config-dir shunia.pane-index` / `config.toml`. See
`config.example.toml`.

| Key | Default | Meaning |
| --- | --- | --- |
| `separator` | `":"` | Between the positions. `"."` → `w1.t2.p3`, `""` → `w1t2p3`. |
| `title_separator` | `" - "` | Between the positions and the pane's title. |
| `max_title_width` | `0` | Truncate the title to this many **terminal columns**. `0` keeps it whole. |
| `keep_title` | `true` | Keep the pane's existing title as the suffix. `false` labels panes with the positions only, and stops tracking titles. |

`max_title_width` counts display columns, not characters, and leaves an `…`
behind — the same unit and shape herdr uses for its own truncation. Herdr
hard-truncates the finished label to `pane_width - 4` columns and offers **no
setting for that**, so this value can only ever be tighter than herdr's, never
looser. Position first also means a narrow pane truncates the title, not the
position.

String values are quoted and taken literally: there are no escape sequences, so
`"\t"` is a backslash and a `t`, not a tab. A value that does not fit its setting
is ignored and the default is used — a control character or line separator in a
`separator`, a width that is not a non-negative integer, a quoted `"false"` where
a boolean is expected. Each rejection is logged, and
`herdr plugin log list --plugin shunia.pane-index` shows it.

Herdr separately caps any reported title at **80 characters**, so the plugin
trims to the same budget before publishing; without that, what it wrote and what
it read back would disagree and a truncated title would look like an edit by
someone else.

Turning `keep_title` off does not forget titles already seen; turning it back on
restores them.

## Appearance

The plugin supplies text and nothing else. Herdr decides how that text is drawn,
and there are exactly three controls, all of them herdr's and none of them
per-label:

* the focused pane's label colour, from `[theme.custom] accent`
* every other pane's label colour, from `[theme.custom] overlay0`
* bold, applied automatically to the focused pane's label

`ui.accent` also exists ("accent color for highlights, borders, and navigation
UI") and drives herdr's other chrome; the two border labels specifically read the
theme palette above.

**Font size is not available, to this plugin or to anything else.** A terminal
cell carries no size of its own — the glyph comes from the outer terminal's
font. The route a label takes also leaves no room to ask for one: the metadata
channel a title is reported through has no style field, herdr applies a fixed
per-cell style of its own, and a reported title is stripped of control
characters, so an escape sequence embedded in a label is removed rather than
interpreted.

## How the positions are derived

Herdr exposes no pane ordinal, so two facts are used:

* A pane id suffix is a monotonic counter per workspace, so decoding it gives
  creation order. No registry of our own is needed, and deleting a pane
  renumbers its siblings for free. **Creation order means a pane moved in from
  another tab is older and takes an earlier position**, which may not be what
  you expect if you want a visual left-to-right order.
* The socket snapshot lists workspaces, and the tabs inside each workspace, in
  display order — the same order the tab bar numbers them. `tab.number` is the
  id counter, **not** the position, so it is deliberately not used.

If the ids ever stop decoding (herdr changing its alphabet), or two panes in a
tab decode to the same value, the plugin logs a warning and falls back to layout
position — top to bottom, left to right — instead of emitting arbitrary numbers.

One caveat on `separator`: recognising its own label after the state file is lost
relies on delimiting the position groups by non-alphanumeric punctuation, so a
separator made of letters or digits cannot be delimited. Without a state file
and with such a separator, the positions are prepended to the title once; the
label then settles and stops growing. Recovery is exact whenever the state file
is present, and **drop labels** re-derives everything from the agents' own titles
if a label has picked up junk.

## Conflicts

* **Other pane-id plugins.** Several plugins label panes with their public ids
  (`imtim/herdr-pane-id`, `Haichiu/herdr-pane-id-border`,
  `4Born/herdr-pane-id-labeler`, `limars874/herdr-pane-id-metadata`,
  `Ghost-LZW/pane-identity`). They publish through `pane rename`, and herdr
  resolves the border title as *reported title > manual pane name > agent
  label*, so this plugin will silently win and make theirs invisible. Don't run
  both.
* **Agent integrations.** Command Code and Claude Code report their session
  title into the same metadata field, and herdr takes the newest report. This
  plugin re-applies on `pane.agent_status_changed` and folds the agent's title
  into the suffix, so the two settle rather than fight. A plugin that also
  writes `--title` on its own schedule would ping-pong with it.
* The label occupies the border title, so `pane rename` names and detected agent
  labels are no longer visible there. `terminal_title` is unaffected.

## Implementation notes

* The label goes through `pane report-metadata --title`, the only channel that
  outranks a manual pane name. `--agent` must be omitted: passing it makes the
  call succeed while storing no title.
* `pane report-metadata` is a documented command (`herdr pane report-metadata
  --help`, and the *Report display-only pane metadata* section of the CLI
  reference), including the 80-character cap and the `--source` character set.
  It is only unusual in that it is the surface agents use rather than one a
  human normally types.
* One `herdr api snapshot` per reconcile rather than per-pane reads: ~180 ms
  versus ~180 ms *each*.
* `report-metadata` prints nothing on success, so only the exit code is checked,
  and each command runs under a 20 s timeout so a wedged server cannot stall a
  hook while it holds the reconcile lock.
* Hooked events are the ones that can move a position: workspace, tab and pane
  create, close, move and rename, plus `workspace.reordered`, `pane.exited` and
  `pane.agent_status_changed`. There is no plugin hook for `layout.updated` or
  `pane.swap` — high-volume events are excluded — which is fine because
  creation-order numbering ignores both. `tab.closed` does not fire on 0.9.0
  when a tab holding panes closes; `pane.closed` covers it.
* herdr fires the hooks for one operation concurrently (a workspace create
  produces three events within milliseconds), so reconciles take an exclusive
  lock and a burst collapses into the run in flight. The guarantee is a
  reconcile *after the burst*, not after every individual event: a flag written
  between the holder's last check and its release is consumed by the next
  invocation. Making every caller block would close that window by giving up the
  coalescing, which is not worth it for scheduling that settles on the next
  event anyway.
* State is `$HERDR_PLUGIN_STATE_DIR/titles.json`, versioned, mapping pane id to
  the untruncated title, the text last shown, and the whole label last published.
  It is written atomically through `mkstemp` in a `0700` directory, so pane
  titles — agent session names — are not readable by other users. A suffix is
  kept while the pane lives: agents re-report their titles intermittently, so
  dropping it on a momentary absence would make the label flap.
* Recognising its own output matters, because a label mistaken for a title would
  get appended to itself. The recorded label is matched exactly, which survives
  any separator change. Only when the state has no record — a wiped file, or a
  label written by an older version — does a shape match take over. Nothing trims
  the end of a suffix, so a title such as "Fix the bug (v2)" is kept whole.
* A damaged state file is repaired or dropped entry by entry rather than being
  trusted, and the count is logged, so a corrupted file does not silently change
  what the labels say.

## Compatibility

**Only herdr 0.9.0 has been tested**, which is why `min_herdr_version = "0.9.0"`
makes herdr refuse to install it anywhere older. Reading the feature history
suggests 0.7.2 is the oldest plausible floor — `pane report-metadata --title`
landed in 0.6.3, plugin v1 in 0.7.0 and `herdr api snapshot` in 0.7.2 — but the
border renderer changed between 0.6.3 and 0.9.0, and whether `pane_borders =
"always"` is even accepted that far back is unverified. **Test on your version
before lowering it.**

`platforms` declares linux and macos. The only platform-specific code is
`fcntl` for the reconcile lock, which degrades to running without a lock where
it is missing; windows is untested rather than known-broken.

## Licence

MIT. See [LICENSE](LICENSE).
