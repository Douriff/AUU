from fastapi import Response
from fastapi.responses import JSONResponse


API_VERSION = "1"


def ok(data, status: int = 200) -> JSONResponse:
    resp = JSONResponse(content={"ok": True, "data": data}, status_code=status)
    resp.headers["X-Api-Version"] = API_VERSION
    return resp


def err(code: str, message: str, status: int = 400, extra: dict | None = None) -> JSONResponse:
    error = {"code": code, "message": message}
    if extra:
        error.update(extra)
    resp = JSONResponse(
        content={"ok": False, "error": error},
        status_code=status,
    )
    resp.headers["X-Api-Version"] = API_VERSION
    return resp


def add_version_header(response: Response) -> None:
    response.headers["X-Api-Version"] = API_VERSION
