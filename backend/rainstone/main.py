from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from rainstone.api.routes import router
from rainstone.config import get_settings

settings = get_settings()
app = FastAPI(title="Rainstone", version="2.0.0a1", root_path=settings.root_path)
app.include_router(router)

static_dir = settings.static_dir
if static_dir.exists():
    assets = static_dir / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        candidate = static_dir / path
        if candidate.is_file() and static_dir.resolve() in candidate.resolve().parents:
            return FileResponse(candidate)
        return FileResponse(static_dir / "index.html")
