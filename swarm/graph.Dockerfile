# The graph job: reads the swarm's bucket, writes Neo4j (graph/). Build context
# is the repo root (see swarm/compose.yaml). Same shape as the pipeline images.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app/graph
COPY graph/requirements.txt ./
RUN pip install -r requirements.txt \
 && useradd --system --uid 10001 graph
COPY graph/ ./
USER graph

# --root s3 is fixed: the job reads the bucket. Extra flags (--dry-run, --keep) append.
ENTRYPOINT ["python", "build_swarm.py", "--root", "s3"]
