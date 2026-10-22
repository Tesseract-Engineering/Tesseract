# One image per pipeline; every pipeline image has this shape. Build context
# is the repo root (see swarm/compose.yaml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    SWARM_ONTOLOGY=/app/ontology.yaml

WORKDIR /app
# Only the swarm extra's dependencies: the graph side's (neo4j, model SDKs)
# never enter a pipeline image.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['swarm']))" > requirements.txt \
 && pip install -r requirements.txt \
 && useradd --system --uid 10001 swarm
COPY ontology.yaml ./
COPY swarm/ swarm/
COPY pipelines/drive/main.py pipelines/drive/main.py
USER swarm

ENTRYPOINT ["python", "pipelines/drive/main.py"]
