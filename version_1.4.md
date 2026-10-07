# Version 1.4 — Changelog

Covers everything since the 1.3 changelog up to `b780d75`, plus the site-picture
work that follows it.

| Section | Contents |
|---|---|
| [1. Code changes](#1-code-changes) | what changed, briefly |
| [2. Upgrading from 1.3](#2-upgrading-from-13) | what happens on the first start |

---

## 1. Code changes

### 1.1 Headline — every run shows the site it mapped

Before 1.4 a run's only whole-site output a browser could show was nothing at
all: the detection overlay was written but never displayed, and the species
result existed only as a KMZ for Google Earth. Opening an older run gave plots
and crown thumbnails, but no picture of the site itself.

In 1.4 every run shows its *Detected crowns* overlay, and every finalized run
shows a *Species map*.

### 1.2 Frontend (`frontend/index.html`)

- **Detected crowns** — the unlabelled overlay now opens the cluster review,
  above the plots and the clusters.
- **Species map** — a finalized run shows its crowns coloured by species, with
  a legend, above the downloads. It is also listed as a download.
- **Current and past runs alike.** Both pictures appear for the run just
  analysed or finalized, for any past run opened from the run list, and when a
  project is reopened.
- **Failed runs can be opened.** A run that failed after detection still has
  its overlay, so its *Open* button is enabled and shows that picture instead
  of an error.
- **Light in the page, full on click.** The page loads a screen-sized preview;
  clicking opens the full image in the existing lightbox.
- All image URLs go through `assetUrl()`, like the plots and crown images.

---

## 2. Upgrading from 1.3

`docker compose pull` + `up -d`. No schema change and no new settings.

Runs finalized before 1.4 have no species map on disk. It is drawn the first
time someone opens the run, which takes a few seconds once.
