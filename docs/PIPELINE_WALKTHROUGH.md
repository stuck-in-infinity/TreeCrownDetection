# Running the Pipeline — Walkthrough

How to take a drone orthomosaic to a species-labelled map, start to finish.

Five steps on one page at `http://<workstation>:8200`.

**Before you start:** a GeoTIFF (`.tif`) from your survey, a Google account
authorised for this deployment.

---

## 0. Sign in

Open `http://<workstation>:8200`. The landing page covers the app until you
authenticate — click **Sign in with Google** and complete the consent popup.

If the button reads "Loading Google sign-in…" and never becomes
active, the page cannot reach Google's SDK — check the workstation's network
before anything else.

---

## 1. Name the project

Give the survey a name you will recognise later — *North block, June survey*.

The server generates a unique **Project ID**; every upload, run and output is
stored under it. A **Browse output files** link appears once the project
exists — that is the FileBrowser share for this project.

---

## 2. Upload the orthomosaic

One GeoTIFF per project.

- **Under ~500 MB** — use **Upload GeoTIFF** and pick the file.
- **Larger** — paste a public Google Drive link and use **Fetch from Drive**.
  The server downloads it directly

Once the upload registers you will see the filename.

> **The image locks after the first analysis.** You can re-run with different
> parameters as often as you like, but you cannot swap the image afterwards.
> For a different area, start a new project.

---

## 3. Configure & analyze

Give the run a name for your own reference, then press **Run analysis**. The
defaults are a reasonable starting point for urban drone imagery:

| Setting | Default | What it does |
|---|---|---|
| Detector model | `urban_cambridge` | which Detectree2 weights to use |
| `tile_size` | 10 | tile edge in metres; the biggest lever on coverage |
| `buffer` | 10 | tile overlap, so crowns on a seam are not cut in half |
| `iou_threshold` | 0.9 | how aggressively overlapping duplicates are merged |
| `conf_threshold` | 0.85 | minimum detector confidence to keep a crown |
| `detections_per_image` | 6 | cap per tile |
| `area_min` / `area_max` | 4 / 2000 m² | discards specks and whole-canopy blobs |
| Feature extractor | DINOv2 | embeds each crown crop |
| `pca_components` | 50 | dimensionality before clustering |
| `k_list` | 2,4,6,8,10 | cluster counts to try — you pick one in step 4 |

If the GeoTIFF carries no CRS you will be asked for an **EPSG code** before the
run can start; the analysis is blocked until you supply one.

Expect **few minutes** for a large survey. 


---

## 4. Label the clusters

**This is the step only a person can do.** 

The page shows, for each value of *k* you asked for, a set of sample crown
images per cluster. Work through them in the **Browse output files** tab
alongside:

1. **Pick a *k*.** Look at the k-selection plot and the sample images. Too low
   and distinct species get merged into one group; too high and a single
   species splits across several. Choose the value where each group looks
   internally consistent.
2. **Name each cluster.** Open the sample crowns for a cluster, identify the
   species, and type the name. Use the same spelling every time — the labels
   are exported verbatim.
3. Leave a cluster blank if you genuinely cannot tell. It is better than a
   guess you will not be able to defend later.

Submit the labels when every cluster you intend to name has a name.

---

## 5. Finalize & export

Press **Finalize & export**. This applies your labels to every detected crown
and writes the outputs. It is much faster than step 3.

When it completes you get:

| File | Use |
|---|---|
| `species_map.kmz` | open in Google Earth Pro — every crown outlined and coloured by species |
| `species_map.csv` | per-crown table: coordinates, species, confidence |
| Distribution summary | crown count per species |
| STAC item | machine-readable catalogue entry for the run |

Download links appear on the page. **Browse all output files** gives you
everything the run produced, including the intermediates.

If you supplied ground truth when setting up the project, a validation summary
and confusion matrix are produced here too.

---

## Re-running

You do not have to start over to change your mind. From the same project you
can re-run step 3 with different parameters — each run is stored separately as
`run_1`, `run_2`, … and past runs stay downloadable from the project's run
history. Only the orthomosaic is fixed.

Common reasons to re-run:

- **Large trees missing, or the canopy under-outlined** — raise `tile_size`.
- **Too many spurious detections** — raise `conf_threshold`.
- **One species split across several clusters** — re-label at a lower *k*.

The in-app **Guide & FAQ** has a symptom-to-setting table covering the rest.
