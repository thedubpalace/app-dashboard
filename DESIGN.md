# Design

<!-- impeccable:design-schema 1 -->

## World

**iOS 26 "Liquid Glass"** — floating frosted-glass panels over a full-bleed
photo wallpaper (misty blue mountain ridgelines, `static/wallpaper.jpg`),
replacing the flat dark bordered-list theme this dashboard shipped with
before. Brief-pinned by the user ("ปรับ style ให้เป็น ios liquid glass" +
"ใช้ wallpaper นี้"); no concept tournament was run — a named, specific,
real design language beats the roll per the skill's own rule.

## Material

- **Glass panel**: `backdrop-filter: blur(28px) saturate(180%)` over a
  soft diagonal gradient fill (`--glass-fill`), 1px translucent border,
  a radial "sheen" pseudo-element (`::before`, `mix-blend-mode: overlay`)
  simulating light hitting the glass from the top-left, and a two-part
  shadow: offset+blurred outer depth shadow plus inset top/bottom
  hairlines for the specular edge. Corner radius 26px (panels), scaling
  down to 22px under 620px viewports. True iOS "continuous corner"
  squircles were not attempted — large border-radius is the honest,
  declared concession.
- **Rows**: no card-in-card nesting — each panel is one grouped list,
  rows separated by a 1px hairline, matching iOS grouped table view.
- **Buttons**: full pill (`border-radius: 999px`), tonal fill per action
  (`tone-start` green, `tone-stop` red, `tone-restart` orange,
  `tone-shell` blue), `transform: scale(0.94)` press feedback.
- **Toast**: top-center glass HUD pill (iOS "Copied"/"Connected"
  style), not the previous bottom-right corner stack.
- **Modal**: centered glass sheet, 28px radius, same material as panels.

## Color

Restrained strategy (Operate default) — neutral glass + translucent
white label hierarchy, with Apple's own dark-mode system colors carrying
every semantic state:

| Token | Value | Use |
|---|---|---|
| `--sys-green`  | `#30D158` | running / success / start |
| `--sys-red`    | `#FF453A` | stopped-as-danger / destructive |
| `--sys-orange` | `#FF9F0A` | restart / pending |
| `--sys-blue`   | `#0A84FF` | primary action / focus ring / shell |
| `--sys-teal`   | `#40C8E0` | Tailscale section icon |
| `--sys-indigo` | `#6A5CE6` | Apps section icon |
| `--sys-gray`   | `#98989F` | stopped / neutral / VMware icon |

The old amber accent theme was not a brand commitment (no logo/identity
existed — see PRODUCT.md) and was replaced outright; it is evidence of
the prior look, not carried forward.

## Type

`-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
"Inter", system-ui, "Segoe UI", Roboto, sans-serif` — the real Apple
system stack first, Inter as an explicit, deliberate fallback for
non-Apple browsers (Operate mode explicitly permits system-font
defaults; this is not the "Inter as generic AI-UI tell" pattern the
detector's `overused-font` warning targets, since Inter is never the
primary face here). Monospace data (paths, run commands, health-check
targets) uses `ui-monospace, "SF Mono", "JetBrains Mono", monospace`.

Fixed rem/px scale, no fluid clamp sizing (Operate rule): 26px header
title, 15px row names, 12.5px descriptions, 10.5–11.5px mono/labels,
12px uppercase 0.07em-tracked section headers.

## Status vocabulary

Replaced the prior 2px colored left border-bar (banned by craft-floor:
no colored border-left/right above 1px) with a small solid status dot
(`.status-dot`, 8px, green when running / gray when stopped — flat
fill, no glow halo) plus a trailing pill badge reading "Running" /
"Stopped" in full words (was the terser "● RUN"/"○ OFF").

## Motion

One authored moment: panels materialize in once on load
(`@keyframes materialize`, opacity/blur/translateY, ~480ms,
staggered only by DOM order, `prefers-reduced-motion`-safe). Everyday
interaction motion stays fast and state-only per Operate rules: 120–260ms
button press-scale, toast in/out, tab segment switch, refresh-icon spin
— no orchestrated page-load choreography beyond the one panel reveal.

## Layout

Single column, `max-width: 840px`, centered. Row actions sit in a
right-hand column (108px) on desktop; under 620px viewports rows switch
to a stacked column with a wrapping horizontal action-pill row —
structural responsive behavior, not fluid scaling, per Operate rules.

## Assets

`static/wallpaper.jpg` — user-supplied photo (misty blue mountain
ridgelines), resized from 4970×3318/4.3MB to 2400×1602/~225KB (Pillow,
JPEG q78) for reasonable load weight as a fixed full-viewport background.
Served via a new `/static` mount in `main.py` (`StaticFiles`).

## Known reductions from the full new-work.md pipeline

This is a personal single-file dashboard, not a production/client
launch, so the following steps from the skill's full flow were
deliberately skipped rather than run in a reduced form — disclosed here
per the skill's transparency rule:

- No `concept-seed.mjs` tournament / decision-page vote — the brief
  pinned the world by name, which the skill states beats the roll.
- No subagent finish-reviewer / documenter spawn — reviewed and
  documented in-thread instead (this file is that documentation).
- Screenshot verification used ad-hoc headless-Edge captures (desktop
  1440px and an effective ~390–500px mobile width) rather than the
  skill's formal batched finish-review round; one real mobile-viewport
  false alarm surfaced (headless Edge inflating `--window-size` by the
  host's ~1.26x OS display-scale factor) and was confirmed harmless via
  a `document.body.scrollWidth` probe before being ruled out.
