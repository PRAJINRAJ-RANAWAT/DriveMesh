# 08 — UI Reference and Design System

(Numbered 08 because 07 is the threat model.)

## 1. Reference study (inspiration only)
**Reference:** a public landing page for an "agent ops" product, studied 2026-11-09.

**Method:**
- Rendered with Playwright (Chromium/Brave, software WebGL) at 1440 px and 390 px.
- Scrolled in 120 px steps, capturing a frame every 840 px.
- Extracted `getComputedStyle` statistics over every visible element: font families/sizes/weights, colours, radii, shadows, background images, transitions, animations, backdrop filters.

**Not reproduced:**
- Reference screenshots are **not committed**; they're someone else's work.
- No HTML, CSS, JS, copy, imagery, icons, logo or 3D model was copied.
- Only the *design language* below informed DriveMesh.

### What makes it feel premium (measured)
| Aspect | Observation (computed values) | What DriveMesh borrows (the idea, not the asset) |
|---|---|---|
| Typography contrast | Condensed display face for headlines (H1 ≈ 89 px, line-height ≈ 0.88, tracking ≈ −0.02 em); monospace for 253 of ~380 text nodes (10–13 px, uppercase, tracking ≈ +1.8 px) | Large, tight Space Grotesk headlines (−0.035 em) against tiny uppercase **mono micro-labels** for eyebrows, buttons, data and nav |
| Two-tone headline | Last line of a headline in the accent colour | Accent used on the punchline line, never body copy |
| Hairline structure | `1px solid` at 8–17 % white everywhere; card radius mostly 16 px, 12 px, 8 px | 1 px hairlines at 12 % ink; 16 px cards, 6 px buttons |
| Depth | Soft large shadows (`0 8px 32px rgba(0,0,0,.35)`), radial glows, an SVG-noise texture, faint grid lines, edge vignettes | Faint grid and a radial accent glow behind the hero; soft shadows on cards. No noise texture (it costs paint time) |
| Navbar | `position: fixed`, transparent at the top; after scrolling `rgba(0,0,0,.88)` + `backdrop-filter: blur(24px)` + hairline bottom border | Same behaviour in base colour: transparent → translucent off-white + blur + hairline |
| Storytelling | Pinned full-viewport scenes; a giant low-opacity background word behind each scene; floating data "chips"; "SCROLL ↓" hints | A pinned, scroll-driven "account dies" scene with a giant background word and step captions |
| Emphasis by inversion | The highlighted pricing card flips to a light surface on a dark page | Alternating **base** and **charcoal** bands; the final CTA band is solid accent |
| Motion | Transitions 0.15–0.4 s `ease`; reveals 0.4–0.5 s; ambient loops 1.5–8 s; a 30 s marquee | 150/250/450/800 ms tokens; ambient loops ≥ 3 s; everything off under `prefers-reduced-motion` |
| Rhythm | Sections ≈ 100–128 px vertical padding, 32 px side padding, small gaps (8–16 px) inside components | 96/128 px section spacing, 4-px-based spacing scale |
| Buttons | Square, outlined, uppercase mono labels with an arrow (→ / ↗) | 6 px radius, mono uppercase label, arrow glyph; primary is solid accent |

## 2. Design system (`drivemesh/web/static/css/tokens.css`)
Single source of truth for colour: **base `#F5F3EE` · surface (charcoal) `#1C1C1C` · accent `#FF5A1F`**. The 60-30-10 rule: base ≈ 60 %, charcoal (text and dark bands) ≈ 30 %, accent ≤ 10 %
(primary CTAs, key stats, punchlines).

### Colour tokens and contrast (WCAG 2.1, computed)
| Token | Value | Use | Contrast |
|---|---|---|---|
| `--color-base` | `#F5F3EE` | page background | — |
| `--color-surface` | `#1C1C1C` | text on base; dark bands | 15.37 : 1 on base |
| `--color-accent` | `#FF5A1F` | CTA fill, stats and punchlines **on charcoal**, decorative strokes | 5.47 : 1 on charcoal; **2.81 : 1 on base (fails)** |
| `--color-on-accent` | `#1C1C1C` | text on accent buttons/bands | 5.47 : 1 |
| `--color-accent-ink` | `#B8400F` | rare accent-coloured *text on base* (darker tone of the accent) | 5.01 : 1 |
| `--color-ink-muted` | `#5E5A53` | secondary text on base | 6.18 : 1 |
| `--color-on-surface-muted` | `#A8A39A` | secondary text on charcoal | 6.79 : 1 |
| `--color-ok / -warn / -danger` | `#2E6B45` / `#8A5600` / `#B42318` | status badges on base (functional, not decorative) | all ≥ 4.5 : 1 |

Rule that follows from the table: **orange text never sits on off-white.** On light sections the accent appears only as fills, buttons and non-text
decoration (underlines, glows, packets). Orange headlines and stats live on charcoal.

### Type
| Token | Value |
|---|---|
| `--font-display` | Space Grotesk 500–700 (self-hosted, latin subset, `font-display: swap`) |
| `--font-body` | Inter 400–700 |
| `--font-mono` | JetBrains Mono 400–600: labels, buttons, data |
| Scale | `--fs-display` clamp(3rem, 8.4vw, 7rem) · `--fs-h1` clamp(2.4rem, 5.5vw, 4.5rem) · `--fs-h2` clamp(2rem, 4vw, 3.25rem) · `--fs-h3` 1.375rem · `--fs-lead` 1.2rem · `--fs-body` 1rem · `--fs-small` .875rem · `--fs-micro` .72rem |
| Tracking | `--tracking-tight` −0.035em (display) · `--tracking-label` .16em (mono labels) |

### Space, radius, depth, motion
- **Spacing** (4 px base): `--space-1` 4 · `-2` 8 · `-3` 12 · `-4` 16 · `-5` 24 · `-6` 32 · `-7` 48 · `-8` 64 · `-9` 96 · `-10` 128.
- **Radii:** `--radius-sm` 6 (buttons, inputs) · `-md` 12 · `-lg` 16 (cards) · `-xl` 24 (stages) · `-pill`.
- **Shadows:**
  - `--shadow-1` 0 1px 2px / 6 % (hairline lift)
  - `--shadow-2` 0 8px 32px / 10 % (cards)
  - `--shadow-3` 0 24px 80px / 18 % (floating punchlines)
  - `--glow-accent` (focus/CTA hover halo)
- **Motion:**
  - durations `--dur-1` 150 ms · `-2` 250 ms · `-3` 450 ms · `-4` 800 ms
  - easings `--ease-out` cubic-bezier(.2,.8,.2,1) · `--ease-in-out` cubic-bezier(.65,0,.35,1)
  - ambient loops ≥ 3 s; all non-essential motion off under `prefers-reduced-motion`.

### Component rules
- **Buttons:** mono uppercase label + arrow.
  - Primary = accent fill with charcoal text.
  - Ghost = 1 px ink outline.
  - On charcoal: primary stays accent, ghost uses an off-white outline.
- **Cards:** base-2 (`#ECE9E2`) or white-ish surface, 1 px line, 16 px radius, `--shadow-2` on hover.
- **Eyebrows:** mono micro-label in a hairline-bordered chip.
- **Focus:** 2 px accent-ink outline + 3 px offset on every interactive element.

## 3. Screenshots
`ui-reference/before-*.png` (the Midnight Vault UI) and `ui-reference/after-*.png` (this design), at 1440 px and 390 px.
