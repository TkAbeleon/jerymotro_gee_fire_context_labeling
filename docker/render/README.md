# Docker Render

This directory contains only the Docker files dedicated to the Render Cron Job.

The Docker build context must remain the repository root.

Render settings:

- Runtime: Docker
- Root Directory: empty
- Dockerfile Path: docker/render/Dockerfile
- Docker build context: repository root (default)
- Schedule: 0 */6 * * *
- Docker Command: leave empty

The entrypoint reads the Render Secret File:

/etc/secrets/gee-service-account.json

and starts:

python /app/gee_fire_context_labeling.py --once
