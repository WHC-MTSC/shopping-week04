import hashlib
import mimetypes
from pathlib import Path
from typing import override

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.types import Scope


class FreshStaticFiles(StaticFiles):
    @override
    def is_not_modified(self, response_headers: Headers, request_headers: Headers) -> bool:
        # FileResponse validators use size/mtime, which can survive a source archive replacement.
        return False

    @override
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        for name in ("etag", "last-modified"):
            if name in response.headers:
                del response.headers[name]
        return response


def mount_dashboard(application: FastAPI, directory: Path) -> None:
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    prefix = f"/assets/{digest.hexdigest()}"
    # A versioned directory also versions relative imports, not just the entry module.
    pages = {
        name: (directory / name).read_text(encoding="utf-8").replace('"/static/', f'"{prefix}/')
        for name in ("index.html", "logs.html")
    }
    mimetypes.add_type("text/javascript", ".mjs")
    application.mount(prefix, FreshStaticFiles(directory=directory), name="versioned-static")
    application.mount("/static", FreshStaticFiles(directory=directory), name="static")

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(pages["index.html"], headers={"Cache-Control": "no-store"})

    @application.get("/log-console", response_class=HTMLResponse, include_in_schema=False)
    def log_console() -> HTMLResponse:
        return HTMLResponse(pages["logs.html"], headers={"Cache-Control": "no-store"})
