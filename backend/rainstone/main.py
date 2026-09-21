import re

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from rainstone.api.routes import router
from rainstone.config import get_settings

settings = get_settings()
app = FastAPI(title="Rainstone", version="2.0.0b1", root_path=settings.root_path)
app.include_router(router)

static_dir = settings.static_dir


def _meta(html: str, name: str, value: str) -> str:
    return re.sub(
        rf'(<meta name="{name}" content=")[^"]*(")', rf"\g<1>{value}\g<2>", html, count=1
    )


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
        html = (static_dir / "index.html").read_text()
        html = _meta(html, "rainstone-base", prefix or "/")
        # The browser only sends fixture identity headers in development mode.
        html = _meta(html, "rainstone-auth-mode", settings.auth_mode)
        return HTMLResponse(html)
