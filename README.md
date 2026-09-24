# Pane Index

Labels every herdr pane's top border with its position, keeping the title the
pane already had:

```
┌ w1:t2:p3 - Refactor auth middleware ───┐
│ $                                      │
└────────────────────────────────────────┘
```

* `w1:t2:p3` is a **position** — first workspace, second tab, third pane — not
  the public id herdr uses elsewhere (`wP:p4`).
* The trailing part is whatever the pane was already called, so an agent's
  session name shows for as long as that agent is running — and goes away with
  the session instead of lingering on the border. An agent that publishes no
  pane title (Codex writes only a terminal title) is read from the terminal
  title instead.
* Updates itself on every layout change. Nothing to run by hand.
* Four optional settings: two separators, a title width cap, and whether to keep
  the pane's title.

## Install

Needs **Python 3** on `PATH`. Every command in the manifest calls `python3`,
so that is the name to provide: the python.org installer for Windows only
ships `python` and `py`, and needs a `python3` alias alongside them.

```bash
herdr plugin install shunia/herdr-pane-index
```

Also give lone panes a border — a single-pane tab has none by default, so there
is nothing to write on:

```toml
# ~/.config/herdr/config.toml, or %APPDATA%\herdr\config.toml on Windows
[ui]
pane_borders = "always"
pane_outer_borders = true
```

```bash
herdr server reload-config
```

A tab with two or more panes already has borders and needs none of this.

## Configure

`herdr plugin config-dir shunia.pane-index` → `config.toml`. Every setting is
optional; this is the whole file at its defaults.

```toml
# Between the positions. "." -> w1.t2.p3, "" -> w1t2p3
separator = ":"

# Between the positions and the pane's own title.
title_separator = " - "

# Truncate that title to this many terminal columns; 0 keeps it whole. Herdr
# caps the finished label at pane_width - 4 columns with no setting of its own,
# so this can only be tighter, never looser. The positions come first, so a
# narrow pane cuts the title, not the position.
max_title_width = 0

# false labels panes with the positions only, and stops tracking titles.
keep_title = true
```

What those do:

| Config | Label |
| --- | --- |
| defaults | `w1:t2:p3 - Refactor auth middleware` |
| `separator = "."`, `title_separator = " / "`, `max_title_width = 20` | `w1.t2.p3 / Refactor auth middl…` |
| `separator = ""` | `w1t2p3 - Refactor auth middleware` |
| `keep_title = false` | `w1:t2:p3` |
| `max_title_width = 14`, CJK title | `w1:t2:p3 - 重写认证中间…` |

A value that does not fit its setting is ignored, with a line in
`herdr plugin log list`, and the default is used.

## Actions

Both are in herdr's menu, and bindable with `type = "plugin_action"` and
`command = "shunia.pane-index.sync"`.

* **resync labels** — reconcile now rather than waiting for the next event.
* **drop labels** — drop the plugin's titles so the panes' own titles show
  through, then re-derive from them. Repairs a stuck label; to switch the plugin
  off, `herdr plugin disable shunia.pane-index`.

## Notes

* **Positions are creation order.** A pane id suffix is a per-workspace counter,
  so a pane moved in from another tab takes an earlier position, and closing a
  pane renumbers its siblings.
* **Don't combine with pane-id plugins** (`imtim/herdr-pane-id`,
  `Haichiu/herdr-pane-id-border`, `4Born/herdr-pane-id-labeler`, …). They publish
  through `pane rename`, which this outranks, so theirs becomes invisible.
* **Font size is not settable**, here or anywhere: a terminal cell has no size of
  its own. Colours are herdr's — `[theme.custom] accent` for the focused pane,
  `overlay0` for the rest.
* Tested on herdr 0.9.0 on linux and macos, and on 0.9.1-preview on windows.
  `min_herdr_version` stays at 0.9.0.

## Licence

MIT.
