# org-backend deployment

## Image release flow

- `main` pushes and manual dispatch run `.github/workflows/release-image.yml`.
- Images are published to `ghcr.io/juliancoy/org-backend`.
- Published tags include:
  - `latest` (default branch only)
  - `sha-<short-commit>`
  - `v*` tags when a git tag is pushed

## Single-node runtime behavior

- `org` container (prod) uses a release image (`ORG_PROD_IMAGE`, default: `ghcr.io/juliancoy/org-backend:latest`).
- `org-dev` container mounts local source and runs `uvicorn --reload`.

## Commit pinning

Set `ORG_PROD_IMAGE` to a commit tag, for example:

`ghcr.io/juliancoy/org-backend:sha-70d07eb`

The root launcher forwards `ORG_PROD_IMAGE` into `org/run.py` when this value is present in environment config.
