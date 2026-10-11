# Starred runs

Each folder here is one hand-picked survey. The public gallery (the **Gallery**
tab, open without sign-in) lists these first, on every page 1, ahead of the
runs users have shared.

Nothing here is read by the pipeline. A folder that does not follow the format
below is skipped and logged; it cannot break the gallery or the app.

## Add one

1. Make a folder. The name is the survey's id in the URL: lower-case letters,
   digits, `-` and `_` only, at most 64 characters. Example: `sanjay-van-plot-3`.
2. Put at least the overlay picture in it. That alone is enough to be listed.
3. Add a `meta.json` if you want a proper title, a place, a date or an order.
4. Run the check (below). The survey appears within about 15 seconds; no restart.

**Anything you put in a folder here is public**, species files included, unless
its `meta.json` says `"share": "crowns"` or `"hidden": true`. Keep work in
progress in a folder whose name starts with `_`.

```
starred/
  sanjay-van-plot-3/
    meta.json            optional  title, place, date, order (see below)
    overlay.png          required  crowns outlined on the orthomosaic (.png or .jpg)
    ortho.jpg            optional  the orthomosaic with nothing drawn on it
    species_map.png      optional  crowns coloured by species (.png or .jpg)
    crowns.geojson       optional  crown outlines; gives the crown count and a download
    crown_master.csv     optional  one row per crown with a `species` column; gives the species bars
    species_map.kmz      optional  download
    stac_item.json       optional  download
```

Where each file comes from in a run folder (`data/storage/projects/<id>/work/run_<n>/`):

| File here | Copy from |
|---|---|
| `overlay.png` | `detectree/<name>/overlay.png` |
| `species_map.png`, `species_map.kmz`, `stac_item.json` | `step4_output/` |
| `crowns.geojson` | `polygons/<name>.geojson` |
| `crown_master.csv` | `step2_output/crown_master.csv` |

**Shortcut:** copy a whole `run_<n>` folder in as it is. The
run layout is recognised, so nothing needs renaming, and the orthomosaic
picture, area and resolution are worked out from the rasters in it. Flat files
win if both are present.

## meta.json

The whole file is optional, and so is every field in it. Without a `title` the
folder name is used: `sanjay-van-plot-3` is shown as "Sanjay van plot 3". A
`meta.json` that is present but not valid JSON hides the folder until it is fixed.

```json
{
  "title": "Sanjay Van, plot 3",
  "place": "Mehrauli, South Delhi",
  "date": "2026-09-04",
  "share": "all",
  "order": 1,
  "description": "One or two sentences shown under the title.",
  "how": "Detectree2 urban weights · tile 20 m · confidence 0.5 · 4 groups",
  "files_url": "https://files.example.org/share/AbCdEf12",
  "crowns": 154,
  "area_km2": 0.06,
  "resolution_cm": 2.0,
  "run_id": "",
  "hidden": false
}
```

| Field | Meaning |
|---|---|
| `title` | Card and page heading. Default: the folder name. |
| `place` | Shown under the title. |
| `date` | `YYYY-MM-DD`, shown as "processed 4 Sep 2026". |
| `share` | `"all"` (default) shows everything in the folder. `"crowns"` shows only the orthomosaic, the overlay and the outlines, whatever else is in the folder. |
| `order` | Smaller comes first. Folders without it follow, by title. |
| `description` | Short text under the title. |
| `how` | "How it was made" line: a string, or a list of strings. |
| `files_url` | A link (`http://` or `https://`) to the full output, for example a FileBrowser share you made for it. Only shown with `"share": "all"`. |
| `crowns`, `area_km2`, `resolution_cm` | Area is in square kilometres (6 ha is 0.06). Say these when the files that would give them are not in the folder. They override what is worked out. |
| `run_id` | If this is a copy of a run that is also shared in the gallery, put that run's id here (the last part of its gallery URL) so it is not listed twice. |
| `hidden` | `true` keeps the folder but takes it out of the gallery. |

A folder whose name starts with `_` or `.` is ignored: the place for work in
progress.

## Check before you rely on it

```
docker compose exec api python scripts/check_starred.py
```

It lists every folder as shown, shown with a warning, or skipped, with the
reason. The same reasons go to the app log under `app.gallery`.

## Only put here what may be public

Everything in a starred folder that the format names is served to anyone.
Use runs of your own, or runs whose owner chose "Yes, all data".
