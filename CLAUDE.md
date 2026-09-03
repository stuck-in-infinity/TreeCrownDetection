# CLAUDE.md

## Orientation

Tree-Crown Species Pipeline: drone orthomosaic → Detectree2 crown detection →
DINOv2 features → KMeans clustering → human species labelling → KMZ/CSV/STAC
export. FastAPI backend (`:8123`) + static single-file frontend (`:8200`) +
optional external Airflow (`:8080`) that orchestrates but never computes.

**Read [docs/CODEBASE_MAP.md](docs/CODEBASE_MAP.md) before searching the
codebase.** It is a navigation index: a "where to look for X" table, a
file→responsibility map for every module, the on-disk artifact layout, a region
map of the 1200-line `frontend/index.html`, and the non-obvious invariants.
Going there first avoids re-grepping structure that is already written down.

Keep it current: when you move responsibilities between files, add a route area,
change the storage layout, or land the in-flight work in its §10 snapshot, update
the map in the same change.

## Other docs

- `README.md` — deploy and run
- `project_outline.md` — architecture with diagrams
- `docs/INTEGRATION_GUIDE.md`, `docs/FRONTEND_BACKEND_FLOW.md`
- `docs/filebrowser_*.md` — FileBrowser output sharing
