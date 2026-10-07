# Third-party notices

## UI font comparison assets (development only)

`artifacts/ui-shots/00-baseline/font-study/` contains unmodified WOFF2 subsets
from the following Fontsource packages, pinned to package version 5.3.0. These
six families are comparison assets; they are not loaded by the product page.

| Family | Upstream authors/source | License |
|---|---|---|
| Inter | Rasmus Andersson and Inter contributors; https://github.com/rsms/inter | SIL Open Font License 1.1 |
| Geist | The Geist Project Authors; https://github.com/vercel/geist-font | SIL Open Font License 1.1 |
| Be Vietnam Pro | The Be Vietnam Pro Project Authors; https://github.com/bettergui/BeVietnamPro | SIL Open Font License 1.1 |
| JetBrains Mono | The JetBrains Mono Project Authors; https://github.com/JetBrains/JetBrainsMono | SIL Open Font License 1.1 |
| Geist Mono | The Geist Project Authors; https://github.com/vercel/geist-font | SIL Open Font License 1.1 |
| IBM Plex Mono | IBM Corp.; https://github.com/IBM/plex | SIL Open Font License 1.1 |

Each family directory retains its original `LICENSE`, including its exact
copyright notice. Package URLs, versions and archive SHA-256 digests are recorded
in `font-study/sources.json`. No font has been renamed or modified.

## Accessibility audit tool (development only)

`artifacts/ui-shots/00-baseline/font-study/axe-axe.min.js` is the unmodified
axe-core 4.10.3 distribution by Deque Systems, Inc. It is injected by Playwright
for development audits and is not served to product users.

License: Mozilla Public License 2.0, reproduced in `font-study/axe-LICENSE`.
Corresponding source: https://github.com/dequelabs/axe-core/tree/v4.10.3.
Distribution package: https://registry.npmjs.org/axe-core/-/axe-core-4.10.3.tgz.
No changes were made to axe-core.

## Product fonts

`src/ai_orchestrator/web/assets/fonts/` contains unmodified Inter and JetBrains
Mono WOFF2 subsets from Fontsource 5.3.0 (latin, latin-ext, vietnamese;
weights 400, 500, 600). Both use SIL Open Font License 1.1. Exact notices are
preserved in `inter-LICENSE` and `jetbrains-mono-LICENSE`; package/archive and
individual asset hashes are in `sources.json` alongside the assets.

Fallback font metrics were measured locally. Arial and Courier New are not
redistributed; the CSS uses a local font if available, then a system fallback.

## Product icon sprite

`src/ai_orchestrator/web/assets/icons/` retains 30 unmodified SVG source files
from `lucide-static` 0.468.0, the complete LICENSE and `sources.json` with the
pinned package URL/archive hash and individual SVG hashes. Lucide uses ISC;
its license also includes MIT notices for Feather-derived portions.
Upstream: https://lucide.dev/license and https://github.com/lucide-icons/lucide.

`web/icons.svg` combines those paths into local symbols. Presentation uses
currentColor and 1.5px stroke; original raw sources and notices are retained.
No runtime package download, CDN or icon font is required.
