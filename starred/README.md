# Starred runs

`starred.json` in this folder lists the runs to feature in the public gallery
(the **Gallery** tab, open without sign-in). They are shown first, in the order
you give, with a "Featured" badge.

Nothing is copied. Each entry points at a run, and the gallery reads that run
from its own project folder (`data/storage/projects/<project_id>/work/run_<n>/`).

A bad entry is skipped and logged. It cannot break the gallery or the app.

## Format

```json
{
  "starred": [
    {
      "title": "Sanjay Van Spot-1",
      "place": "Sanjay Van, South Delhi",
      "order": 1,
      "project_id": "4ed48e2c-87a8-450e-90e7-fa21b6b2d923",
      "run": 1
    }
  ]
}
```

Only `project_id` and `run` are required (or `run_id` in their place).

| Field | Meaning |
|---|---|
| `project_id`, `run` | Which run to feature: the project's id and the run number. |
| `run_id` | The run's id, as an alternative to the two fields above. It is the last part of the run's gallery URL. |
| `title` | Card and page heading. Default: the run's public title, then the project name. |
| `place` | Shown under the title. |
| `date` | `YYYY-MM-DD`. Default: the day the run finished. |
| `order` | Smaller comes first. Entries without it follow, in file order. |
| `share` | `"all"` or `"crowns"`. Default: what the owner agreed to; if the owner gave no answer, `"all"`. |
| `description` | Short text under the title. |
| `how` | "How it was made" line: a string, or a list of strings. Default: the run's own settings. |
| `files_url` | A link (`http://` or `https://`) to the full output. Default: the run's FileBrowser share, if it has one. Only shown with `"all"`. |
| `crowns`, `area_km2`, `resolution_cm` | Override the numbers worked out from the run's files. Area is in square kilometres. |
| `hidden` | `true` keeps the entry in the file but takes it out of the gallery. |

## Things to know

- **Listing a run here makes it public**, even if its owner never answered the
  consent question. Only list your own runs, or runs whose owner agreed.
- **Edits show up on the next page load.** No restart is needed.
- **Relabelling a run updates its card** once Finalize has run again.
- **Retention keeps projects that have a starred run.** If you take an entry
  out, its project follows the normal retention rules again.
- If a run or its project is deleted, the entry is skipped.

## Check it

```
docker compose exec api python scripts/check_starred.py
```

It lists each entry as shown or skipped, with the reason.
