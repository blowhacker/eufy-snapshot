# wanyard.com

The marketing site. Static files, no build step.

```
site/
├── index.html      ← copy lives here
├── style.css       ← visuals live here
└── assets/
    ├── feed.jpg                 ← detection-feed screenshot
    └── spatial-synthetic.webp  ← fictional point-cloud scene; safe for public use
```

## Deploy

**Cloudflare Pages** (recommended — free, instant cache invalidation, custom domain in one click):

1. Push the wanyard repo to GitHub.
2. Cloudflare → Pages → Connect to Git → pick the repo.
3. Build settings:
   - Framework preset: **None**
   - Build command: *(leave empty)*
   - Build output directory: `site`
4. Add `wanyard.com` as a custom domain.

**GitHub Pages** (alternative — also free, slightly slower to update):

Settings → Pages → Source: `main` / folder: `/site`.

Both rebuild on every push to `main`.

## Edit

Need to change a paragraph? Open `index.html`, find it, save, push. There is no framework. There is no bundler. There is no `node_modules`.

Need to tweak the dark/amber palette? Open `style.css`, change the variables at the top of `:root`. Done.

Need to swap the accent colour permanently? Set `data-accent="green"` (or `blue`, `red`) on `<html>` — the alt-palettes are already defined.

## What's intentionally NOT here

- No analytics. Add Plausible / Umami / Fathom yourself if you want.
- No service worker, no PWA. It's a one-page site (the webmanifest is just for nice install icons).

## Favicon set

```
site/favicon.ico                ← multi-res .ico (16/32/48)
site/site.webmanifest           ← PWA / Android home-screen icon
site/assets/favicon.svg         ← modern browsers, scales perfectly
site/assets/favicon-16.png      ← legacy fallback
site/assets/favicon-32.png      ← legacy fallback
site/assets/favicon-48.png      ← legacy fallback
site/assets/apple-touch-icon.png  (180×180)  ← iOS home screen
site/assets/icon-192.png        ← Android home screen
site/assets/icon-512.png        ← high-res / maskable
site/assets/og-mark.png         (1200×1200)  ← social share preview
```

To redesign the mark: edit `assets/favicon.svg` (the source of truth), then regenerate the PNGs from it. Quick-and-dirty: run `rsvg-convert favicon.svg -w 32 -o favicon-32.png` for each size, or just open the SVG in any vector editor and export.
