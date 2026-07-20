---
name: Nexus Technical Admin
colors:
  surface: '#081425'
  surface-dim: '#081425'
  surface-bright: '#2f3a4c'
  surface-container-lowest: '#040e1f'
  surface-container-low: '#111c2d'
  surface-container: '#152031'
  surface-container-high: '#1f2a3c'
  surface-container-highest: '#2a3548'
  on-surface: '#d8e3fb'
  on-surface-variant: '#c3c6d7'
  inverse-surface: '#d8e3fb'
  inverse-on-surface: '#263143'
  outline: '#8d90a0'
  outline-variant: '#434655'
  surface-tint: '#b4c5ff'
  primary: '#b4c5ff'
  on-primary: '#002a78'
  primary-container: '#2563eb'
  on-primary-container: '#eeefff'
  inverse-primary: '#0053db'
  secondary: '#b7c8e1'
  on-secondary: '#213145'
  secondary-container: '#3a4a5f'
  on-secondary-container: '#a9bad3'
  tertiary: '#bec6e0'
  on-tertiary: '#283044'
  tertiary-container: '#656d84'
  on-tertiary-container: '#eef0ff'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#dbe1ff'
  primary-fixed-dim: '#b4c5ff'
  on-primary-fixed: '#00174b'
  on-primary-fixed-variant: '#003ea8'
  secondary-fixed: '#d3e4fe'
  secondary-fixed-dim: '#b7c8e1'
  on-secondary-fixed: '#0b1c30'
  on-secondary-fixed-variant: '#38485d'
  tertiary-fixed: '#dae2fd'
  tertiary-fixed-dim: '#bec6e0'
  on-tertiary-fixed: '#131b2e'
  on-tertiary-fixed-variant: '#3f465c'
  background: '#081425'
  on-background: '#d8e3fb'
  surface-variant: '#2a3548'
typography:
  display-lg:
    fontFamily: Geist
    fontSize: 36px
    fontWeight: '700'
    lineHeight: 44px
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Geist
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Geist
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
  body-lg:
    fontFamily: Geist
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Geist
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Geist
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 18px
  label-mono:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.02em
  label-caps:
    fontFamily: Geist
    fontSize: 11px
    fontWeight: '700'
    lineHeight: 16px
    letterSpacing: 0.05em
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  unit: 4px
  container-padding: 24px
  gutter: 16px
  sidebar-width: 260px
  stack-sm: 8px
  stack-md: 16px
  stack-lg: 24px
---

## Brand & Style
The design system is engineered for high-performance documentation management and RAG ingestion workflows. It balances technical precision with a modern, sophisticated aesthetic. The personality is authoritative, transparent, and efficient, catering to developers and technical administrators who require clarity in complex data environments.

The visual style merges **Corporate Minimalism** with **Subtle Glassmorphism**. This combination ensures the interface feels lightweight and contemporary without sacrificing the structural integrity required for a data-dense admin portal. The UI leverages a "layer-based" logic where depth is communicated through varying levels of transparency and background blurs rather than heavy shadows.

## Colors
The palette is rooted in a deep navy and slate foundation to reduce eye strain during long-duration technical tasks.
- **Primary:** A vibrant "Electric Blue" used exclusively for primary actions, progress indicators, and active states.
- **Surface:** A spectrum of Slates (`#0F172A` to `#1E293B`) defines the hierarchy of containers.
- **Accents:** Success Green and Warning Amber are utilized with high saturation to ensure instant recognizability against the dark background.
- **Transparency:** Use 80% opacity for card backgrounds to allow for subtle glassmorphism effects against background gradients.

## Typography
This design system utilizes **Geist** for its exceptional clarity and technical "ink-trap" aesthetic, which performs well in high-density layouts. **JetBrains Mono** is introduced for metadata, status codes, and RAG ingestion paths to provide a distinct visual cue for technical data points. 

Use `label-caps` for section headers in the sidebar and small table headers. Use `body-sm` for the majority of data grid content to maximize information density while maintaining legibility.

## Layout & Spacing
The layout follows a **Rigid Dashboard Grid**. A persistent sidebar on the left handles primary navigation, while the main content area utilizes a fluid 12-column grid.

- **Data Density:** Use a tight 4px baseline unit. 
- **Margins:** Main content containers should maintain 24px of internal padding.
- **Responsive Reflow:** On screens smaller than 1024px, the sidebar collapses into an icon-only rail or a hidden drawer. Data grids should transition to horizontal scrolling to preserve column integrity.

## Elevation & Depth
Depth is created through **Subtle Glassmorphism** and tonal layering. 
- **Level 0 (Background):** Solid `#020617`.
- **Level 1 (Cards/Sidebar):** Surface `#0F172A` with 80% opacity and a 1px border of `#1E293B`. Apply a `backdrop-filter: blur(12px)`.
- **Level 2 (Modals/Popovers):** Surface `#1E293B` with 95% opacity, a 1px border of `#334155`, and a subtle 10% opacity blue-tinted shadow (0px 10px 30px).
- **Interactive States:** Hovering over a card should increase the border brightness rather than increasing shadow depth.

## Shapes
To maintain a professional and technical appearance, the design system uses a **Soft** corner radius. 
- **Standard Elements:** 4px (0.25rem) for buttons, input fields, and small UI widgets.
- **Large Elements:** 8px (0.5rem) for cards, modals, and the main content area containers.
- **Pills:** Used exclusively for status indicators (tags/chips) to distinguish them from interactive buttons.

## Components
- **Buttons:** Primary buttons are solid Blue. Secondary buttons use a ghost style with a subtle Slate border that intensifies on hover.
- **Data Grids:** Use 40px row heights for high density. Zebra striping should be subtle (2% opacity difference). Header cells use `label-caps` typography.
- **Status Indicators:** Use small circular dots paired with `label-mono` text. For ingestion status (e.g., "Indexing"), use a subtle pulse animation on the indicator dot.
- **Input Fields:** Dark backgrounds (`#020617`) with 1px borders. Focus states use a 1px Primary Blue border with a 2px outer "glow" (0.15 opacity).
- **Sidebar:** Icons should be 20px, stroke-based, and utilize the `secondary_color` unless active. Active states use a vertical 2px line on the far left of the nav item.
- **Inclusion Cards:** For RAG source management, use cards with a dedicated "header" section containing the source icon and a "footer" for metadata like "Last Synced."
