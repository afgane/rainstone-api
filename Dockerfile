FROM node:22.19.0-alpine AS frontend-deps
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

FROM frontend-deps AS frontend-dev
COPY frontend/ ./
EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

FROM frontend-deps AS frontend-build
COPY frontend/ ./
RUN npm run build

FROM python:3.12.11-slim AS backend-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/backend
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/ backend/
RUN pip install --no-cache-dir --no-deps .
COPY alembic/ alembic/
COPY alembic.ini fixtures/phase1.json catalog/gcp-2026-09-19.json ./

FROM backend-base AS backend-dev
COPY fixtures/ fixtures/
COPY catalog/ catalog/
COPY scripts/ scripts/

FROM backend-base AS runtime
COPY --from=frontend-build /app/frontend/dist /app/static
COPY fixtures/ fixtures/
COPY catalog/ catalog/
RUN useradd --create-home --uid 10001 rainstone && chown -R rainstone:rainstone /app
USER rainstone
EXPOSE 8000
CMD ["uvicorn", "rainstone.main:app", "--host", "0.0.0.0", "--port", "8000"]
