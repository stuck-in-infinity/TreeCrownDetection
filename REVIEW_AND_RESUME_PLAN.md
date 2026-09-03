# Plan — review view, run-params populate, sign-in UX, cleanup

Decisions taken (2 Sep): precompute thumbnails at analyze with on-demand
fallback; "top 5" = nearest to cluster centroid; sign-in stays on the unverified
header, UX only; opening a run prefills its params and Run analysis starts a new
run on that run's ortho.

---

## 0. Already done — not in this plan

Built and on disk this session, with tests:

| Ask | Where |
|---|---|
| Sign in → past projects → click one | `loadMyProjects`, `openProject`, `restoreOpenProject` |
| Shows its orthos, allows more uploads | ortho library, append-only |
| Past runs per ortho, with FileBrowser link | `GET /project/runs?ortho_id=`, `runs` table, `run_share_url` |
| Pick a run, label it, finalize it | `POST /project/runs/{n}/labels`, `/runs/{n}/finalize`, `ACTIVE_RUN` |
| DB records ortho ↔ runs | `runs.ortho_id`, `cluster_labels.run_id` |

What follows is the remainder.

---

## 1. Findings that shape the plan

**The review endpoints exist and the frontend does not use them.**
`clustering.py` already serves `k-selection.png`, `{k}/tsne.png`,
`{k}/clusters?samples=N` (per-cluster crown URLs) and `crowns/{name}`
(GeoTIFF → PNG on demand). `renderClusterReview` in `index.html` ignores all of
it and pulls plots from the FileBrowser *raw download* URL
(`/api/public/dl/<hash>/…`) instead. That is why the walkthrough video shows
"k-selection plot couldn't be loaded" — cross-origin, and dead when FileBrowser
is off. Crowns are skipped entirely with a "go open the folder" note.

So the view work is mostly **rewiring**, not building.

**Everything in `clustering.py` is keyed off `project.current_run`.** Open run
3 while run 5 is newest and every plot and crown comes from run 5. Must become
run-aware, same as labels/finalize were.

**"Top 5" today is `sorted(os.listdir)[:5]`** — alphabetical by filename,
which is detection order, which is tile order. Says nothing about the cluster.

**`step1_cluster` has the centroid distances and throws them away.**
`km.fit_predict(X)` — `km.transform(X)` gives distance to every centroid.
One extra column in `k{k}_assignments.csv` and "most typical 5" is a sort.

**Disk: `COPY_TO_CLUSTER_FOLDERS` duplicates every crown TIF into every k
folder.** `k_list=2,4,6,8` → four full copies of all crops per run, plus the
original in `crowns/`. For the disk conversation: this is the single biggest
avoidable cost, and the cluster folders are only there so a human can browse
them in FileBrowser. Not changed in this plan; flagged.

---

## 2. Pipeline — `tree_crown_pipeline.py`, `step1_cluster`

Two additions inside the existing `for k in config.K_LIST` loop, after
`cl = km.fit_predict(X)`:

**a. Centroid distance.**
```python
dist = km.transform(X)                       # (n, k)
cl_df["dist_to_centroid"] = dist[np.arange(len(cl)), cl]
```
Written into `k{k}_assignments.csv` alongside `cluster`. Additive column; every
existing reader of that CSV keeps working.

**b. Thumbnails for the top-N per cluster.**
After the assignments are written, for each cluster take the N lowest
`dist_to_centroid` rows and render `crowns/<name>.tif` →
`clustering/k{k}/thumbs/<name>.png`, longest side 200 px, using the same
normalisation `_tif_to_png_bytes` in `clustering.py` does (move that function
into a shared `app/services/thumbs.py` so pipeline and API render identically).

`N` = new config `THUMBS_PER_CLUSTER`, default 5, exposed as
`TCP_THUMBS_PER_CLUSTER`. Rendered for **every k in K_LIST**, not only the
recommended one — the user compares k values, and re-rendering on demand for
the others defeats the point.

Cost: 5 × k × ~20 KB. `k_list=2,4,6,8` → 100 PNGs, ~2 MB per run. Negligible
next to the TIF copies above.

Failure here must **not** fail the run: wrap per-crown, log at warning, carry
on. The API falls back to on-demand rendering for anything missing.

---

## 3. API — `clustering.py` becomes run-aware and serves the new shape

**Every route gains an optional `run` query parameter**, defaulting to the
active run, resolved through `run_registry.get_run`. Path helpers take the run
number instead of calling `_run(project)`. `_require_review` checks
`Run.state`, not `Project.state` — an older run in `COMPLETED` is reviewable
while the project is `ANALYZING` a new one.

**`GET /project/clustering?run=N`** returns, for the run:

```json
{
  "run": 3, "run_id": "…", "ortho_id": "…",
  "recommended_k": 4, "available_k": [2,4,6,8],
  "k_selection_plot_url": "…/clustering/k-selection.png?run=3",
  "overlay_url":          "…/detection/overlay.png?run=3",
  "per_k": {
    "4": {
      "tsne_plot_url": "…/clustering/4/tsne.png?run=3",
      "clusters": [
        {"cluster": 0, "count": 212,
         "crowns": [{"name": "S3C_017.tif", "dist": 0.41,
                     "thumb_url": "…/crowns/S3C_017.png?run=3&k=4"}, …5]},
        …
      ]
    }
  }
}
```

`per_k` is filled for the **recommended k only** in this response; other k
values are fetched by `GET /project/clustering/{k}/clusters?run=N` on click,
exactly as today. Keeps the first paint one request.

**`GET /project/crowns/{name}.png?run=N&k=K`** — serves
`clustering/k{K}/thumbs/<name>.png` if present, otherwise renders from
`crowns/<name>.tif` on demand and writes the PNG beside it so the next request
is a file read. `Cache-Control: public, max-age=86400` — a crown never changes
once a run is done.

**Ordering** in `clusters` = ascending `dist_to_centroid` from the CSV. If the
column is absent (a run from before this change) fall back to filename order
and say so: `"order": "filename"` vs `"order": "centroid"`.

---

## 4. Frontend — review panel

Replace `renderClusterReview` / `showKView` wholesale. They stop touching
FileBrowser URLs; every image is an `<img src>` to the API above, which is
same-origin under nginx and needs no CORS.

Layout, top to bottom:

```
Review — run 3 on north_a.tif            [Open run folder in FileBrowser →]
  k-selection plot
  Compare a k:  [2] [4•] [6] [8]        (• = recommended)
  k = 4 · t-SNE plot
  cluster_0 · 212 crowns   [img][img][img][img][img]
  cluster_1 ·  88 crowns   [img][img][img][img][img]
  …
  [Use k = 4 and name these groups ↓]   → scrolls to step 4, sets Chosen k
```

Thumbnails ~120 px, click opens the PNG full size in the existing lightbox.
Alt text = `cluster_0, crown S3C_017`. A cluster row with zero crowns says so
rather than rendering nothing.

Errors: a failed `<img>` shows a small "not available" tile, never an empty
gap. A failed clustering fetch shows `explainFull(e)` — the server's message
and hint, same as everywhere else.

**Trigger:** called after analyze completes (existing spot) **and** from
`openRun(n)`, so opening an old run shows that run's review, not the newest.

---

## 5. Frontend — run → parameters populate

`openRun(n)` today sets `ACTIVE_RUN` and refreshes. Add:

1. Fetch that run's entry from the cached `ORTHO_RUNS` (already has `params`,
   `model_key`, `run_name`, `ortho_id`), else `GET /project/runs?run=N`.
2. `applyRunParams(entry)` writes every step-3 field from `entry.params`:
   `p_tile p_buf p_iou p_conf p_det p_amin p_amax p_msize p_fullcov p_pca
   klist p_epsg modelKey backbone runname`. Unknown keys ignored; missing keys
   leave the field at its current value. Then `validateParams()`.
3. Tick the run's ortho: `toggleOrtho(entry.ortho_id, true)`.
4. Banner text becomes: **"Working on run 3 (north_a.tif). Run analysis will
   start run 6 from these settings."**

`analyzeProject()` already clears `ACTIVE_RUN` before firing — correct, since
the new run is the newest. Add `"based_on_run": 3` to the analyze body; the
server stores it in the new run's `params` so the lineage is visible in the run
list ("from run 3").

---

## 6. Frontend — remove the connection check

Delete: `#connBox` markup, `.conn*` CSS, `runConnectionCheck()`, `connRow()`,
and the auto-open on first transport failure.

Keep: the transport classification inside `explainFull` / the `api()` error
path — mixed-content, nginx-vs-API 404, unreachable. That text is what tells a
user *why* a request never arrived and it does not depend on the panel.

---

## 7. Errors and logs — audit of the new routes

Rule already in force: every raise carries `code`, `message`, `hint`,
`project_id`, and now `run` where relevant; the message is written for the
person, the hint names the control to touch.

Gaps to close:

| Route | Gap |
|---|---|
| `POST /runs/{n}/labels` | per-run `INVALID_STATE` raises without a `log.warning`. Add `"409 INVALID_STATE project=%s run=%s state=%s"`. |
| `POST /runs/{n}/finalize` | same for the `INVALID_STATE` branch. |
| `GET /project/runs` | a `run` that does not exist returns an empty list today. Should be `404 RUN_NOT_FOUND` with the valid range in `details`. |
| `clustering/*` | `_require_review` message says "this project is …" — must say "run N is …". |
| `crowns/{name}` | on-demand render failure is `503 DEPENDENCY_MISSING` — correct, but log at `error` with the path so an admin finds the bad TIF. |

Add `RUN_NOT_FOUND` and `THUMB_UNAVAILABLE` to `ERROR_CODES`.

Every log line for a run-scoped action carries `run=` — grep-able per run.

---

## 8. Sign-in — UX pass only

No auth change. Verify with the browser suite that, signed in as an email:

* `/projects/mine` lists only that email's projects (it filters on `user_id`
  already);
* reopening a project, listing its runs, and every per-run action sends the
  same `X-User-Email`, so `get_project` never 403s on a project the user owns;
* a guest (`"default"`) sees only guest projects.

One real fix: `restoreOpenProject` runs before `onSignedIn` sets the email on
first load, so a reload with a project open can fire `GET /project` as guest and
get 403 → rolled back to "nothing open". Move the restore call *after* the
sign-in callback resolves.

---

## 9. Order of work

| # | Phase | Files | Est. |
|---|---|---|---|
| 1 | `dist_to_centroid` + thumbs in `step1_cluster`; shared `thumbs.py` | `tree_crown_pipeline.py`, `config.py`, `services/thumbs.py`, `settings.py` | 2–3 h |
| 2 | `clustering.py` run-aware; new overview shape; crown PNG route with cache | `api/v1/clustering.py`, `core/logging.py` | 2–3 h |
| 3 | Frontend review panel rewired to API; thumbnails; lightbox | `index.html` | 3 h |
| 4 | Run → params populate; `based_on_run`; banner | `index.html`, `runs.py` | 1–2 h |
| 5 | Remove connection check | `index.html` | 30 min |
| 6 | Error/log audit; `RUN_NOT_FOUND` | `labels.py`, `runs.py`, `results.py`, `clustering.py` | 1 h |
| 7 | Sign-in UX pass; restore-order fix | `index.html`, browser tests | 1 h |

**~1.5 days.** Phases 1–2 ship without the frontend and are testable with
curl. Phase 3 is where the user sees it.

---

## 10. Tests

Written to fail on the current code:

* `k{k}_assignments.csv` gains `dist_to_centroid`; the 5 returned per cluster
  are the 5 smallest, not the first 5 alphabetically.
* `clustering/k4/thumbs/` holds exactly 5 × 4 PNGs after a run with
  `k_list=[4]`; each ≤ 200 px on its longest side.
* `GET /project/clustering?run=3` while the project is `ANALYZING` run 5 returns
  run 3's plots — and the URLs in it carry `run=3`.
* `crowns/X.png?run=3` with the thumb deleted still returns a PNG (on-demand
  path) and writes it back.
* Browser: analyze → 4 cluster rows × 5 images render, none broken; click k=6 →
  6 rows; open run 3 → review shows run 3; step-3 fields equal run 3's params;
  ortho ticked; `#connBox` does not exist.
* Log capture: each 4xx on a run route emits one warning containing `run=`.

---

## 11. Not in scope, worth a line

* **Disk.** `COPY_TO_CLUSTER_FOLDERS` is the lever — turning it off and serving
  cluster membership from the CSV + thumbnails would remove the 4× duplication
  with no loss to the UI. FileBrowser browsing of per-cluster folders would go.
  For the brainstorm.
* **Real auth.** Deferred by decision. When it comes, it is a token check in
  `require_user` and a `TCP_GOOGLE_CLIENT_ID` setting; the frontend already
  holds the ID token.
