# Frontend To Backend Flow

This doc shows what happens when you click a button in the web UI.

Route prefix used by the frontend:

```text
/api/v1
```

## Quick Map

| Frontend action | API call | Backend file |
|---|---|---|
| Load model lists | `GET /detectors` and `GET /feature-extractors` | `code/app/api/v1/projects.py` |
| Create project | `POST /projects` | `code/app/api/v1/projects.py` |
| Upload ortho file | `POST /project/orthomosaic` | `code/app/api/v1/projects.py` |
| Upload ortho from URL | `POST /project/orthomosaic/from-url` | `code/app/api/v1/projects.py` |
| Analyze button | `POST /project/analyze` | `code/app/api/v1/analyze.py` |
| Submit labels | `POST /project/labels` | `code/app/api/v1/labels.py` |
| Finalize button | `POST /project/runs/finalize` with body `{"project_id": "..."}` | `code/app/api/v1/runs.py` |
| Poll current project | `GET /project` | `code/app/api/v1/projects.py` |
| Poll run progress | `GET /project/runs/status` | `code/app/api/v1/runs.py` |
| Fetch final results | `GET /project/results` | `code/app/api/v1/results.py` |

## 1. Page Load

When the page opens, the frontend loads the available models:

```text
GET /api/v1/detectors
GET /api/v1/feature-extractors
```

These populate the detector and backbone dropdowns.

## 2. Create Project

Button: `Create project`

Frontend sends:

```http
POST /api/v1/projects
Content-Type: application/json

{
  "name": "my project"
}
```

Backend:

- creates a new project row
- assigns the default detector if `model_key` is not sent
- saves the project id in the UI

## 3. Upload Orthomosaic

Button: `Upload ortho`

Frontend sends a file upload:

```http
POST /api/v1/project/orthomosaic
```

Backend:

- stores the GeoTIFF under the project folder
- registers the ortho in the database
- blocks replacement once the project has already been analyzed

There is also an alternate button for Google Drive:

```http
POST /api/v1/project/orthomosaic/from-url
```

## 4. Analyze

Button: `Analyze`

Frontend sends:

```http
POST /api/v1/project/analyze
Content-Type: application/json

{
  "project_id": "82544806-87ff-4036-83bc-1d645b1bcfce",
  "run_name": null,
  "model_key": null,
  "source_epsg": null,
  "params": {
    "tile_size": 10,
    "buffer": 10,
    "iou_threshold": 0.9,
    "conf_threshold": 0.85,
    "pca_components": 50,
    "batch_size": 16,
    "img_size": 224,
    "k_list": [2,4,6,8,10],
    "model_name": "vit_base_patch14_dinov2.lvd142m"
  }
}
```

Backend file:

- [code/app/api/v1/analyze.py](/home/anunay/drone_docker/code/app/api/v1/analyze.py)

What the backend does:

- checks `project_id`
- validates the requested detector/backbone and parameters
- checks that an ortho exists
- moves the project into `ANALYZING`
- creates a job row
- runs the analysis task
- returns the clustering payload to the UI

Analyze writes one crown GeoJSON per uploaded ortho. The run keeps the
Detectree output and also copies the same GeoJSON into the run's polygons
folder for later steps.

Important:

- This frontend path currently calls the compute-style analyze endpoint directly.
- If `TCP_COMPUTE_TOKEN` is set, the browser will not be able to call this endpoint unless the frontend is also updated to send that token.

## 5. Submit Labels

Button: `Submit labels`

Frontend sends a CSV form upload:

```http
POST /api/v1/project/labels
```

Backend:

- saves the cluster-to-species labels
- updates the project state
- makes the project ready for finalize

## 6. Finalize

Button: `Finalize & export`

Frontend sends:

```http
POST /api/v1/project/runs/finalize
Content-Type: application/json

{
  "project_id": "82544806-87ff-4036-83bc-1d645b1bcfce"
}
```

Backend file:

- [code/app/api/v1/runs.py](/home/anunay/drone_docker/code/app/api/v1/runs.py)

What the backend does:

- checks `project_id`
- checks that labels exist
- moves the project into `FINALIZING`
- creates a job row
- dispatches finalize
- returns the run info to the UI

The direct `POST /api/v1/project/finalize` path follows the same rule: it
also needs `{"project_id": "..."}` in the body.

## 7. Poll Until Done

After Analyze or Finalize, the frontend polls:

```text
GET /api/v1/project
GET /api/v1/project/runs/status
```

Then, once finalize is done, it fetches:

```text
GET /api/v1/project/results
```

## 8. Final Outputs

The results page links to the exported files from the backend:

- KMZ
- CSV outputs
- STAC item if available

## 9. What To Remember

- The frontend keeps the current project id in memory.
- Analyze and finalize both send `project_id` explicitly.
- The browser talks to the `/project/*` routes.
- The backend then uses the project id to read and write the right run folder.
