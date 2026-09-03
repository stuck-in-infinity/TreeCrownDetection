# Pipeline Walkthrough — what was added to `frontend/index.html`

Two rounds of work, cumulative. **Round 1** added a fourth top-level view,
`#/walkthrough`, holding a five-stage conceptual pass through the pipeline.
**Round 2 (this round)** put a literal *this is what you click, in order* tour
in front of it: a 2 min 52 s screen recording of one real run, followed by nine
numbered steps built from cropped screengrabs of that same recording.

The page now reads in two halves:

| | |
|---|---|
| **Watch it happen** | The video, then Steps 1–9 with real screenshots. New this round. |
| **Understand what happened** | The five conceptual stages, the DBSCAN player, the result figures, all the caveats. Unchanged from round 1. |

A one-line bridge (`.wts-bridge`) sits between them so the handover is deliberate.

## Files

| File | What it is |
|---|---|
| `index.html` | The complete modified app. Original functionality untouched. |
| `index.original-backup.html` | Byte-identical copy of the original. Restore by copying over `index.html`. |
| `walkthrough-standalone.html` | The walkthrough alone, self-contained, for opening or sharing without the app. |
| `walkthrough_web.mp4` | The screen recording. **Ships as a file, is not embedded.** Both pages reference it as a relative `src="walkthrough_web.mp4"`. |
| `steps/*.jpg` | The nine cropped step images as separate files, so they can be reused in slides. Byte-identical to what is embedded in the pages. |
| `CHANGES.md` | This file. |

## Size

| | Original | After round 1 | After round 2 |
|---|---|---|---|
| `index.html` | 109,044 B (106 KB) | 1,183,985 B (1.13 MB) | **1,794,614 B (1.71 MB)** |
| `walkthrough-standalone.html` | — | 1,076,301 B (1.03 MB) | **1,686,930 B (1.61 MB)** |

Round 2 adds **610,629 B** to each page: 371 KB of step-image JPEG plus a 54 KB
video poster (together 425 KB raw → 567 KB as base64) and ~43 KB of markup, CSS
and JavaScript. Well inside the ~4 MB budget. `walkthrough_web.mp4` is a further
1,892,496 B (1.80 MB) **outside** the page — a 2.5 MB data URI inside a 1.2 MB
document would have bought nothing and cost seeking and buffering.

## Where the edits are

Every edit to the original is an **insertion** except three lines, each replaced
by a superset of itself. `diff index.original-backup.html index.html` reports
**zero pure deletions**, 2,038 inserted lines and exactly three replaced lines —
mechanically re-verified this round.

### Round 1 (unchanged)

| # | Location | Edit |
|---|---|---|
| 1 | End of the second `<style>` block | ~300 lines of CSS, all scoped to `#wt-page` or prefixed `.wt-` / `#wt-`. |
| 2 | After `</div><!-- /guide-page -->` | The `<div id="wt-page">…</div>` markup. |
| 3 | App header | A new `<a class="wt-cta" href="#/walkthrough">Walkthrough</a>`. |
| 4 | `#guide-page` header | The back-link is now a `<span>` holding `Pipeline Walkthrough →` plus the original `← Back to pipeline`. |
| 5 | FAQ sidebar header | A second line: `Want the whole picture first? Take the Walkthrough →`. |
| 6 | `route()` | Four lines added (`// + walkthrough`), one extended (`// ~ walkthrough`). |
| 7 | After the app's `</script>` | The walkthrough's own `<script>` block. |

The only changed line of existing JavaScript remains:

```js
const anchor = (onGuide || onWalk) ? location.hash.split("#")[2] : null;  // ~ walkthrough
```

### Round 2 (new)

All five insertions land **inside** the round-1 feature. Nothing outside
`#wt-page` was touched, and no round-1 line was modified.

| # | Location | Edit |
|---|---|---|
| 8 | Immediately after the round-1 CSS block | ~130 lines of tour CSS, all prefixed `.wtv-` / `.wts-` or scoped to `#wt-tour` / `#wt-steps` / `#wts-body`. |
| 9 | In `#wt-rail`, after the `Start here` entry | One new rail item: `▶ Watch a run · Video + 9 steps → #wt-tour`. |
| 10 | Between `</section>` of `#wt-intro` and `<!-- ── stage 1 ── -->` | `<section id="wt-tour">` (video), `<section id="wt-steps">` (the nine steps), and the `.wts-bridge` paragraph. |
| 11 | Just before `</div><!-- /wt-page -->` | The `#wts-lb` lightbox dialog. Deliberately inside `#wt-page`, so it disappears with the route. |
| 12 | Inside the walkthrough IIFE | `wireTour()`, plus one line — `wireTour();` — added to the existing `window.wtInit`. |

## Namespacing

Round 2 adds **23 ids**, all `wtv-`, `wts-`, `wt-tour` or `wt-steps`, and ~26
classes in the same namespaces. Verified mechanically on both pages:

- 0 duplicate ids (123 ids in `index.html`, 53 in the standalone).
- 0 ids added outside the `wt*` namespace.
- 0 original ids removed or renamed.

The 23 new ids: `wt-tour`, `wt-steps`, `wts-1`…`wts-9`, `wts-body`, `wts-prev`,
`wts-next`, `wts-pos`, `wts-lb`, `wts-lbimg`, `wts-lbttl`, `wts-lbzoom`,
`wts-lbclose`, `wtv-video`, `wtv-frame`, `wtv-miss`. The control bar, the step
cards and the lightbox stage are class-only.

### One structural note worth keeping

`#wt-steps` carries **both** `wt-stage` and `wts-sec`. It needs `wt-stage`
because the round-1 collapse handler does `btn.closest(".wt-stage")` and would
throw on a header that has no such ancestor — that was a real bug, caught in
testing. It needs `wts-sec` to undo one thing `.wt-stage` does: `overflow:hidden`,
which turns the section into a scroll container and silently kills
`position:sticky` on the control bar inside it. Hence `.wts-sec{overflow:visible}`.

## The step tour

Nine steps, numbered 1–9 in reading order. The app numbers **its own** steps 1–5,
so every tour step carries a pill saying which app step it belongs to, and a
letter where one app step takes more than one move. No competing numbering is
invented.

| Tour | App step | Video | Caption |
|---|---|---|---|
| 1 | Step 1 | 0:02 | Name the project, then Create project |
| 2 | Step 2 · a | 0:06 | Click Choose File and pick one ortho GeoTIFF |
| 3 | Step 2 · b | 0:11 | Click Upload GeoTIFF and wait for “uploading…” to clear |
| 4 | Step 3 · a | 0:27 | Name the run — then open the Parameter Guide before you touch a setting |
| 5 | Step 3 · b | 1:49 | Work down the three parameter groups, then click Run analysis |
| 6 | Step 3 · c | 2:09 | Let the run finish, then use “Compare a k” to inspect each candidate |
| 7 | Step 4 · a | 2:31 | Confirm Chosen k, then type your species names as a comma-separated list |
| 8 | Step 4 · b | 2:37 | Assign a species to every row, then click Submit labels |
| 9 | Step 5 | 2:45 | Click Finalize & export and collect the four downloads |

Each step is deep-linkable as `#/walkthrough#wts-4`, has a sticky prev / next /
`Step n of 9` / jump-dot bar above it, responds to ← and → once focus is anywhere
inside the section, and highlights itself as you scroll (IntersectionObserver).
Clicking a screenshot opens it 1:1 in the lightbox, with a 200 % toggle, Esc to
close, focus parked on Close and returned to the thumbnail afterwards.

### The screenshots

Source: nine 1600×950 JPEG frames pulled from the recording. Each was **cropped
to the panel that matters** — no browser chrome, no taskbar, no right-hand FAQ
rail — and saved at native crop resolution, JPEG q86, progressive.

| File | Crop from the 1600×950 frame | Size | Bytes |
|---|---|---|---|
| `step01_t2.jpg` | (215,160)–(1080,545) | 865×385 | 24 KB |
| `step02_t6.jpg` | (6,0)–(800,500) | 794×500 | 49 KB |
| `step03_t11.jpg` | (215,168)–(1080,540) | 865×372 | 27 KB |
| `step04_t27.jpg` | (215,552)–(1080,942) | 865×390 | 41 KB |
| `step05_t109.jpg` | (215,85)–(1080,755) | 865×670 | 58 KB |
| `step06_t129.jpg` | (215,478)–(1080,838) | 865×360 | 28 KB |
| `step07_t151.jpg` | (215,428)–(1080,905) | 865×477 | 48 KB |
| `step08_t157.jpg` | (215,222)–(1080,948) | 865×726 | 59 KB |
| `step09_t165.jpg` | (215,338)–(1080,850) | 865×512 | 29 KB |

They display at 794 CSS px in the 1440 px layout — about 0.92× native, i.e. very
close to 1:1, and every button label is readable without the lightbox. That was
checked by rendering the page in headless Chromium and reading the result, not
assumed. Upscaling to a 2× asset was rejected: the 1600 px frames are themselves
a downscale of a 1920 px desktop, so there is no second pixel to serve.

At 390 px the same images display at 284 px, which is a thumbnail — that is what
the lightbox is for, and each one carries a visible `⤢ ENLARGE` badge.

### Every caption was checked against the image

The brief's frame descriptions were written from a low-resolution contact sheet
and several were wrong. Corrected here:

- The app spells it **“Configure & analyze”** and **“Finalize & export”**, not
  *analyse* / *finalise*.
- `step05_t109` does **not** show the model-weights dropdown, `tile_size`,
  `buffer` or `iou_threshold` — those are scrolled off the top. It shows
  `conf_threshold 0.85`, `detections_per_image 6`, `area_min 4`, `area_max 200`,
  the DINOv2 feature extractor, `pca_components 50` and `k_list 2,4,6,8`.
- `step06_t129` shows **no progress bar**. The run has already finished; what is
  new on screen is the **Review clusters** block, the `Compare a k` buttons and
  two “couldn’t be loaded” plot notices.
- The detector model is **“Urban trees, Cambridge UK (2023-06-30). (default)”**,
  not *Cambridge (UK 2021/2)*. (Read from the recording at ~1:30; it is named in
  step 5’s prose, not shown in step 5’s crop.)
- The download list is **`kmz`, `crown_master_csv`, `polygon_species_csv`,
  `stac_item_json`** — not *crowns_master.csv / polygons GeoJSON / cluster review*.
- The species labels really are spelt `acasia` / `non-acasia` in this run. Quoted
  verbatim rather than silently corrected, because that string is what lands in
  the exported files.
- The white “Saved info” panel over the cluster table in `step07` is the
  **browser’s autofill**, not part of the app. Said so in the caption.

## The video

`<video controls preload="metadata" playsinline>` with a poster frame, a single
`<source src="walkthrough_web.mp4" type="video/mp4">` and a `<p>` fallback inside
the element. **It never autoplays**, under any motion preference.

Each step's `▶ m:ss in the video` button sets `video.currentTime` and calls
`play()` — always from a click, so always a user gesture.

### Missing-file fallback

The mp4 ships as a separate file, so it can legitimately go missing (someone
mails the standalone html on its own). Two signals are watched: an `error` event
on the `<source>` and on the `<video>`, plus a 1.5 s check for
`networkState === NETWORK_NO_SOURCE`. On either, the player and its caption are
hidden, a bordered **“Video unavailable”** note appears in their place, and the
nine timestamp buttons are removed (`#wt-steps.wtv-dead .wts-ts{display:none}`) —
they would have nothing to seek.

The note covers both causes honestly: the file is missing, *or* this browser
cannot decode it. That second case is real — Playwright's bundled Chromium ships
without H.264, so the shipped page shows exactly this note when rendered there.

Both paths were tested: a directory with the video (player visible, caption
visible, 9 timestamp buttons live, seek to 1:49 lands at 110 s) and a directory
without it (player hidden, note shown, timestamp buttons gone).

## Verification performed this round

Headless Chromium via Playwright, `PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers`,
served over a local HTTP server that answers `Range` requests (the stdlib one
does not, which makes any media file look unseekable and produces a false
failure).

| Check | Result |
|---|---|
| Step tour at 1440×900 and 390×844, screenshots read back | Legible at both; four crops tightened as a result (see below) |
| `document.documentElement.scrollWidth` vs `innerWidth` | 1440/1440 and 390/390 — no horizontal overflow |
| Prev / Next / ← / → / jump dots | Position indicator tracks, ends disable correctly, focus moves to the step |
| Deep link `#/walkthrough#wts-7` | Lands with the step 69 px from the top, clear of the sticky bar; indicator reads “Step 7 of 9” |
| Lightbox | Opens 1:1 at 865 px, 200 % toggle → 1730 px, Esc closes, focus returns to the thumbnail, body scroll restored |
| Collapse `#wt-steps` | `aria-expanded` flips, panel hides |
| `prefers-reduced-motion: reduce` | Video paused, DBSCAN player parked on “▶ Play”, scroll behaviour switches to `auto` |
| Duplicate ids | 0 on both pages |
| Original routes `#/`, `#/guide` | Body classes, gate/app/guide visibility identical to `index.original-backup.html` |
| Console | Identical to the untouched original — 5 errors on every route, all pre-existing |
| Diff vs original | 0 deletions, 3 replaced lines, all documented above |

### Console, in full

The five errors are the app's own and are present on the untouched
`index.original-backup.html` too, byte for byte the same count: `config.js` 404,
`/api/v1/detectors` 404, `/api/v1/feature-extractors` 404, `/api/v1/projects/mine`
404, and `accounts.google.com/gsi/client` blocked by the sandbox's proxy. There is
no backend and no outbound network in the test environment. **The tour itself logs
nothing.** A sixth line, `walkthrough_web.mp4 net::ERR_ABORTED`, appears only
because this Chromium build cannot decode H.264; it is the fallback working.

### Fixes made during verification

1. **Collapse handler threw.** `#wt-steps` was styled as its own section, so the
   round-1 handler's `btn.closest(".wt-stage")` returned `null` and
   `sec.classList.toggle` raised *Cannot read properties of null*. Fixed by giving
   the section both classes and overriding `overflow` — see the structural note above.
2. **False “video not found”.** The first wording claimed the file was missing.
   In this Chromium the file is present and simply undecodable, so the message now
   names both possibilities.
3. **Step 1's crop clipped the header CTA.** It originally included the app header
   so the “How to use this” button was half cut, and a wider crop dragged in a
   sliver of the FAQ rail. Re-cropped to the card only, matching the other eight.
4. **Three crops clipped a trailing line** (steps 4, 6 and 7 cut a caption or a
   button in half). Bottom edges moved to land between elements.

## Known limits

- Everything from round 1 still applies: the walkthrough sits **behind the
  sign-in gate** in the app, exactly like `#guide-page`; use
  `walkthrough-standalone.html` for pre-auth or external sharing.
- The step images are as sharp as their source allows. The recording was captured
  at 1920 px and delivered as 1600 px frames, so a true 2× retina asset does not
  exist and was not faked by upscaling.
- The recording shows a project that had already been created and an ortho already
  registered when it starts, so frame 1 shows the *result* of step 1 as well as its
  controls. The caption says what the success line means rather than pretending the
  click has not happened yet.
- The seek behaviour of the timestamp buttons depends on the server answering
  `Range` requests. Over `file://` and any normal web server this is fine; a server
  that ignores `Range` will play from the start instead.
- Codec support was verified with a VP9 stand-in, because headless Chromium has no
  H.264. The shipped mp4 was **not** played end-to-end in a browser here; its
  duration (172.2 s), dimensions (1280×760) and frame rate (15 fps) were read with
  `ffprobe`.
