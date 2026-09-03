# Tree-Crown Docker Deploy

Use the prebuilt Docker Hub images to run the workstation on a machine where
the model files live outside the repo.

## 1. Put Models In A Separate Folder

Example:

```bash
mkdir -p /opt/treecrown-models
```

Place these files in that folder:

```text
urban_trees_Cambridge_20230630.pth
220723_withParacouUAV.pth
230103_randresize_full.pth
```

The container mounts that folder as `/models`.

## 2. Create `.env`

```bash
cp .env.example .env
```

Set these values:

```env
HOST_MODELS_DIR=/opt/treecrown-models
IMAGE_API=uavforaliens/treecrown-workstation:latest
IMAGE_FRONTEND=uavforaliens/treecrown-frontend:latest
```

If you are not using Airflow yet, leave this blank:

```env
TCP_AIRFLOW_BASE_URL=
```

## 3. Start The Stack

If the Docker Hub images are private, log in first:

```bash
docker login
```

Then run:

```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
```

## 4. Check It

```bash
docker compose -f docker-compose.hub.yml ps
docker compose -f docker-compose.hub.yml logs -f api
docker compose -f docker-compose.hub.yml exec api ls -lh /models
docker compose -f docker-compose.hub.yml exec api curl -s http://localhost:8123/api/v1/detectors
```

Open:

- Frontend: http://localhost:8200
- API docs: http://localhost:8123/docs
- Health: http://localhost:8123/livez

## 5. Update Models Later

To change a model, replace the `.pth` file in the folder from step 1, then
restart the API:

```bash
docker compose -f docker-compose.hub.yml restart api
```

## 6. Stop

```bash
docker compose -f docker-compose.hub.yml down
```
