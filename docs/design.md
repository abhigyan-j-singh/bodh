# The design system

The dashboard's tokens, type, spacing, icons, components and states, defined once in
`src/bodh/web/tokens.py` and emitted as CSS custom properties; the page, the static export
and the Mac app's web view are the same page. The living style guide is `/design` on the
console (`bodh console`, then http://127.0.0.1:8766/design); the visual tests screenshot it.
`scripts/validate_palette.py` checks the colours by numbers; its report is at the end.

## Direction

Quiet, editorial, data-first: a reading-room page, not a monitoring console. The system font
stack, generous whitespace, one accent, colour only where it carries meaning and the same
meaning everywhere. Readable at 390 px and at 1440 px, in light and in dark, with the
keyboard, with a screen reader, with Reduce Motion.

## Colour, by role

| Token | Light | Dark | Where |
| --- | --- | --- | --- |
| `--surface-1` | `#fbfaf8` | `#161513` | the page |
| `--surface-2` | `#f3f1ee` | `#1f1e1b` | tiles, table stripes, the log |
| `--surface-3` | `#eae7e2` | `#2a2825` | a pressed control, a skeleton |
| `--text-primary` | `#1c1a17` | `#f0ede8` | text |
| `--text-secondary` | `#5c5750` | `#b3aea6` | leads, captions, meta |
| `--text-muted` | `#6f6962` | `#8f8a82` | counts in the cloud, the footer |
| `--line` | `#dedbd6` | `#33312e` | hairlines |
| `--line-strong` | `#88827a` | `#6e6961` | lines that must read alone (3:1): axes, input borders |
| `--accent` | `#047460` | `#6ccbb3` | links, the current section, the meter, the selected filter, a single series |
| `--accent-wash` | `#e6f1ee` | `#1c2a27` | selection and hover |
| `--accent-track` | `#cfe4de` | `#274741` | the meter's track; the second series of the calibration bars |
| `--good` / `--warning` / `--critical` | `#318f5a` / `#ab7302` / `#90101a` | `#80dba2` / `#f0b135` / `#ff7f77` | status marks (an icon, a border, a bar) beside a word, never a series |
| `--good-wash` / `--warning-wash` / `--critical-wash` | `#e4f2e8` / `#f7eedb` / `#f8e4e3` | `#1c2f22` / `#332a14` / `#3a1f1e` | banners and status chips; the word on them is text-primary |
| `--series-muted` | `#88827a` | `#6e6961` | context series (the sparkline's past) |
| `--band-90` … `--band-0` | `#1c2d72` `#425ba7` `#718bd1` `#a8bdf2` `#eaf0ff` | `#dae4ff` `#95b0f9` `#637fce` `#3a5199` `#222e55` | the band scale: one indigo hue stepped so that more reads as more contrast |
| `--band-90-wash` … `--band-0-wash` | `#c0d2ff` `#cedcff` `#dce6ff` `#e8eeff` `#f3f7ff` | `#3a4971` `#323e60` `#2a344f` `#232b3f` `#1b2130` | a chip's background, text-primary on it, the band's number printed |
| `--focus` | `#047460` | `#6ccbb3` | the focus ring, 2 px, offset 2 px |

The band colours appear in chips and in the grades grid (its count scale reuses the washes)
and nowhere else; the accent carries every single series; status colours never carry a
series. Text never wears a series colour. The two palest band steps are chip washes only:
the band's number is always printed beside the colour, and a chip has a hairline border.

Dark values are selected steps of the same ramps against the dark surface, declared under
`prefers-color-scheme: dark` (unless `data-theme="light"` is stamped) and under
`data-theme="dark"`, which the Mac app may set; the page offers no toggle of its own.

## Type, space, shape, motion

Type: the system stack (`-apple-system, BlinkMacSystemFont, Segoe UI, Inter, Roboto,
sans-serif`); sizes 13 (table body, captions), 15 (body), 17 (section lead), 22 (section
title), 48 (the hero figure); line heights 1.45 for prose and 1.25 for headings; weights 400
and 600 only; sentence case everywhere; `tabular-nums` in table columns, tiles and axis
ticks only; no web fonts. Space: 4, 8, 16, 24, 32, 48 (16 inside tiles, 24 between
elements, 48 between sections); a 720 px column for prose and the list, 1100 px for tables
and charts; 16 px side gutters on the phone. Shape: 8 px radius on tiles and chips, 4 px on
buttons and inputs; no shadows, no gradients; hairline borders and the surface step do the
work. Rows 36 px on desktop, 44 px tap targets on the phone. Motion: 150 ms ease-out on
opacity and transforms, a 300 ms wash on a changed number, both 0 ms under Reduce Motion.
Breakpoints: 390, 768, 1100.

## Icons

Sixteen-pixel SVG paths, stroke 1.5, `currentColor`: run, pause, stop, star, link, chevron,
chevron-down, warning, check, close, circle (`src/bodh/web/icons.py`). Decorative icons are
`aria-hidden`; an icon that carries meaning has a label.

## States

Loading (the server renders every panel, so the first paint is the page; no skeleton),
refreshing (the previous render is held and nodes are swapped in place; a changed number
carries a wash that fades, off under Reduce Motion), stale (the "updated … ago" stamp turns to
the warning colour after two missed heartbeats and the state line says the console is not
answering and the time the numbers are from; the render stays at full opacity, it is still true),
empty (a sentence that says what will appear and what causes it, per part), error (the
engine's own sentence in the notice line and the one action that helps), offline (the static
export marks its time and that it is a snapshot). Each is on the `/design` page and in the
visual tests' screenshots.

## The live layer

`src/bodh/web/dashboard.js`, under 400 lines, no build step, no dependency, runs in the
console and the app (the export is a snapshot and carries none). It adds the `js` class so
the filters, the verdict menus and the levers appear; without it the page is complete and
still. The ticks: `/api/events` streams a `tick` (the state and today's tiles while a run is
in progress, a heartbeat between), applied in place with `textContent`; when the tick's
`generation` differs from the page's, the page fetches itself again and morphs `main` node by
node (attributes, then text), keeping what is its own: the open `details`, the filters'
`aria-current`, the sort order, the grades given this session (rows are matched by article).
Polling every 30 s is the fallback, said in the footer. The verdict menu is built per row from
the row's `data-verdicts` words; the keys 1 to 8 and r act on the focused row. The levers post
to `/api/action`; the ones that write to BazQux ask first in a `dialog` whose sentence and
button come from `/api/plan` (the numbers, the mode, the reversibility, the time). Tooltips
are one node, values first, from `data-tip`, on hover and on focus; the spend chart snaps to
the nearest day. The hovered mark takes a stroke in the text colour, never a status colour.

## Accessibility

Landmarks (`header`, `nav`, `main` with one `section` per question, each with an `h2`); a
skip link; visible focus rings on every interactive element; every table with a `caption` and
`th scope`; charts as `figure` with a `figcaption` stating the finding and a "Show as table"
twin; colour never the only carrier (bands print their number, status has a word and an icon,
the current filter has a check); contrast at AA for text and 3:1 for lines and icons; 44 px
targets on the phone; the page reads in order without CSS. The checklist for the panels
(contrast, keyboard, touch targets, screen reader) is completed below as each lands.

| Check | State |
| --- | --- |
| Contrast: text AA, lines and icons 3:1 | the tokens pass (report below); axe-core 4.10 finds no violation at WCAG 2.1 AA (and its best-practice rules) in any state, light or dark, 390 or 1280 px, the menus open; a stat tile's delta colours a mark beside its text, not the text, since 13 px in a status colour on the tile's surface is under 4.5:1 |
| Keyboard: reading order equals focus order, Enter and Space operate everything | `tests/visual/test_keyboard_order_follows_the_reading_order`: the Tab order descends the page, a list row counted as one unit (its verdict menu sits top right and follows its content); every lever, filter, header, chart mark and toggle is reached |
| Touch targets 44 px on the phone | rows, buttons and tabs at 44 px under 768 px |
| Screen reader: VoiceOver on macOS, NVDA on Windows | to be checked by a person before 1.0 (owner's live checks) |

## Screenshots

`scripts/screenshots.py` renders the page and the style guide from the seeded fixture with
Playwright, light and dark, 390 and 1280 px, into `docs/images/`; the README's pictures come
from there and are never stale. The visual tests (`tests/visual/`, `BODH_BROWSER=1`) keep the
first screen of every state at 390, 768 and 1280 px, light and dark, Reduce Motion on, under
`tests/visual/baseline/` and fail at 0.1 % of pixels changed; the whole page and the style
guide once. axe-core is not vendored yet (550 KB of JavaScript, or a package): the audit above
was run by hand with Playwright and is to be wired into the browser tests once that choice is
made.

## The palette report

`scripts/validate_palette.py` on the tokens above (OKLab, ΔE × 100; WCAG 2.x contrast):

```
bodh palette validation (OKLab; ΔE × 100; WCAG 2.x contrast)

light: the band ramp #eaf0ff #a8bdf2 #718bd1 #425ba7 #1c2d72
pass  lightness band: L 0.96 0.80 0.65 0.49 0.33
pass  chroma floor (the three mark steps; the two palest are chip washes, their names printed): C 0.021 0.079 0.109 0.125 0.121
pass  one direction: lightness runs one way, so more reads as more
pass  adjacent separation, normal vision: ΔE 16.4 15.8 15.6 16.1 (floor 15)
pass  adjacent separation, protan: ΔE 16.1 15.4 15.8 16.7 (floor 8)
pass  adjacent separation, deutan: ΔE 16.8 15.8 15.6 16.1 (floor 8)
pass  adjacent separation, tritan: ΔE 16.1 16.0 15.7 16.0 (floor 8)
pass  mark contrast (the three strongest steps, used as marks): 3.2 6.1 12.1 against #fbfaf8 (floor 3:1)
pass  chip text on every wash: 11.5 12.7 13.9 15.0 16.2 (floor 4.5:1)

light: the status trio {'good': '#318f5a', 'warning': '#ab7302', 'critical': '#90101a'}
pass  pairwise separation: good/warning 15.9 good/critical 30.1 warning/critical 21.9
pass  pairwise separation, protan: 8.1 29.5 25.6
pass  pairwise separation, deutan: 8.8 15.7 18.1
pass  as a mark on the page (an icon, a border, a bar; the word beside it is text-primary): good 3.9 warning 3.9 critical 8.8 (floor 3:1)
pass  text on each wash: good 15.0 warning 15.1 critical 14.2

light: text and lines
pass  text-primary on surface-1: 16.6 (floor 7.0:1)
pass  text-primary on surface-2: 15.4 (floor 7.0:1)
pass  text-secondary on surface-1: 6.9 (floor 4.5:1)
pass  text-secondary on surface-2: 6.3 (floor 4.5:1)
pass  text-muted on surface-1: 5.2 (floor 4.5:1)
pass  text-muted on surface-2: 4.8 (floor 4.5:1)
pass  accent on surface-1: 5.5 (floor 4.5:1)
pass  accent on surface-2: 5.1 (floor 4.5:1)
pass  line-strong on surface-1: 3.6 (floor 3:1)
pass  series-muted on surface-1: 3.6 (floor 3:1)
pass  focus on surface-1: 5.5 (floor 3:1)
pass  meter fill on its track: 4.3
pass  text on accent-wash: 15.0

dark: the band ramp #222e55 #3a5199 #637fce #95b0f9 #dae4ff
pass  lightness band: L 0.31 0.45 0.61 0.77 0.92
pass  chroma floor (the three mark steps; the two palest are chip washes, their names printed): C 0.072 0.120 0.125 0.110 0.038
pass  one direction: lightness runs one way, so more reads as more
pass  adjacent separation, normal vision: ΔE 15.1 15.6 15.5 17.0 (floor 15)
pass  adjacent separation, protan: ΔE 15.7 15.7 15.3 16.5 (floor 8)
pass  adjacent separation, deutan: ΔE 15.1 15.7 15.5 17.1 (floor 8)
pass  adjacent separation, tritan: ΔE 13.9 15.6 15.7 16.7 (floor 8)
pass  mark contrast (the three strongest steps, used as marks): 4.7 8.6 14.4 against #161513 (floor 3:1)
pass  chip text on every wash: 7.6 9.0 10.6 12.1 13.8 (floor 4.5:1)

dark: the status trio {'good': '#80dba2', 'warning': '#f0b135', 'critical': '#ff7f77'}
pass  pairwise separation: good/warning 16.8 good/critical 26.3 warning/critical 15.2
pass  pairwise separation, protan: 12.9 19.4 15.2
pass  pairwise separation, deutan: 11.8 8.1 9.4
pass  as a mark on the page (an icon, a border, a bar; the word beside it is text-primary): good 10.9 warning 9.6 critical 7.4 (floor 3:1)
pass  text on each wash: good 12.2 warning 12.1 critical 12.9

dark: text and lines
pass  text-primary on surface-1: 15.6 (floor 7.0:1)
pass  text-primary on surface-2: 14.3 (floor 7.0:1)
pass  text-secondary on surface-1: 8.3 (floor 4.5:1)
pass  text-secondary on surface-2: 7.6 (floor 4.5:1)
pass  text-muted on surface-1: 5.3 (floor 4.5:1)
pass  text-muted on surface-2: 4.9 (floor 4.5:1)
pass  accent on surface-1: 9.4 (floor 4.5:1)
pass  accent on surface-2: 8.6 (floor 4.5:1)
pass  line-strong on surface-1: 3.4 (floor 3:1)
pass  series-muted on surface-1: 3.4 (floor 3:1)
pass  focus on surface-1: 9.4 (floor 3:1)
pass  meter fill on its track: 5.2
pass  text on accent-wash: 12.8

every check passed
```
