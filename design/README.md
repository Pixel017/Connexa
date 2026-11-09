# Connexa design system

The public site (templates in `connexa/site/templates/`) is a zero-build,
server-rendered system: **no CSS framework, no npm, no external requests.**
Colors, type and motion are controlled from one `:root` token block.

- Runtime CSS: inline in `site_base.html` (single-file theming, one request)
- Token mirror + documentation: [`tokens.css`](tokens.css) and this README
- Principle: every visual choice is an original design decision — the site is
  not derived from any competitor's assets, copy, or code

## Identity

Warm-paper light theme with an "engineered" texture: hairline dots, mono
eyebrows, indigo→teal gradient accents. The honesty system is part of the
brand: **real lanes glow green (`--ok`), demo/sample lanes badge amber
(`--warn`)** — both on the site and on every link page.

## Tokens

| Token | Value | Used for |
|---|---|---|
| `--bg` | `#f7f5ef` | page background |
| `--card` | `#fffdf8` | cards, panels |
| `--ink` | `#10141c` | headings, strongest text |
| `--line` | `#e6e1d3` | hairlines, borders |
| `--accent` / `--accent-ink` | `#4353ff` / `#3041d8` | links, buttons, CTAs |
| `--accent-2` | `#00b8d9` | gradient partner |
| `--ok` / `--warn` | `#0c9d61` / `#c05e00` | live / demo badges |
| `--code-bg` / `--code-ink` | `#101522` / `#9fd7ff` | code blocks |

Full list: [`tokens.css`](tokens.css). Change a token in `site_base.html`
`:root` and the whole site follows (child templates style themselves from the
same variables — nothing else to touch).

## Typography

| Role | Spec |
|---|---|
| H1 (hero) | clamp(38px → 56px), weight 800, letter-spacing −.02em |
| H2 (section) | clamp(30px → 40px), weight 800 |
| Body | 16px/1.65 `--body` |
| Eyebrow | 12.5px, mono, uppercase, .16em tracking, `--accent-ink` |
| Code | 13px/1.7 `--mono` on `--code-bg` |

## Motion (all respects `prefers-reduced-motion`)

- **Scroll reveal** — sections rise 18px + fade in once, via
  IntersectionObserver ([".reveal"] one-shot, threshold .12)
- **Scroll progress** — 2px gradient bar pinned to the viewport top
- **Marquee** — protocol strip loops ~30s, pauses on hover
- **Hero glow** — faint radial highlight tracks the pointer (fine pointers only)
- **Diagram pulses** — dashes crawl and dots ride the connection paths (SMIL)
- **Copy affordance** — every `pre.code` grows a copy button; feedback via a
  1.6s "copied" swap

Rules: entrances ≤ 640ms, hover moves ≤ 320ms, nothing loops faster than 1s,
and honest badges never animate (they're facts, not decoration).

## Component inventory

| Component | Template hook |
|---|---|
| Announcement bar | base (dismissible) |
| Mega-menu nav | base `.has-menu` (hover + focus-within) |
| Hero | `landing.html` `.hero-grid` |
| Plane diagram | inline SVG `.diagram` |
| Marquee / trust strip | `.marquee` |
| Audience panels | `.tinted` |
| Family showcase | `.step` rows (num/rail/copy/code) |
| Metric band | `.metrics` |
| Honesty strip | `.honesty` |
| CTA band | `.cta-band` (keeps inline-ghost compat) |
| Comparison tables | `table.cmp` (`tr.group`, `.y`, `.n`) |
| Status pills | `.pill-ok` / `.pill-dev` |
| Footer | 5-column + trust badges |

## Extending

New page = new Jinja template extending `site_base.html`, using the class
inventory above. No JS required; everything degrades to static rendering of
the same content.
