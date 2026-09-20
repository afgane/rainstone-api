from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
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
    def spa(path: str, request: Request):
        candidate = static_dir / path
        if candidate.is_file() and static_dir.resolve() in candidate.resolve().parents:
            return FileResponse(candidate)
        prefix = request.headers.get("x-forwarded-prefix", settings.root_path).rstrip("/")
        html = (static_dir / "index.html").read_text().replace(
            '<meta name="rainstone-base" content="/">',
            f'<meta name="rainstone-base" content="{prefix or "/"}">',
        )
        html = html.replace(
            '<meta name="rainstone-base" content="/" />',
            f'<meta name="rainstone-base" content="{prefix or "/"}" />',
        )
        return HTMLResponse(html)
