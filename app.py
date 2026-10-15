import logging
import os
from http.cookiejar import CookiePolicy

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

load_dotenv()
logger = logging.getLogger("openai-proxy")

DEFAULT_UPSTREAM = "https://api.openai.com"
HOP_BY_HOP = {"host", "connection", "keep-alive", "transfer-encoding",
              "te", "trailer", "upgrade", "proxy-authenticate", "proxy-authorization",
              "x-real-ip"}


class _NoCookies(CookiePolicy):
    """Never store or send cookies: the proxy is shared by callers and must not
    leak one caller's upstream (e.g. Cloudflare) cookies onto another's requests."""
    netscape = True
    rfc2965 = hide_cookie2 = False

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

    def domain_return_ok(self, domain, request):
        return False

    def path_return_ok(self, path, request):
        return False


def _filter_request_headers(headers) -> dict:
    return {k: v for k, v in headers.items()
            if k.lower() not in HOP_BY_HOP and not k.lower().startswith("x-forwarded-")}


def _has_body(request: Request) -> bool:
    return "content-length" in request.headers or "transfer-encoding" in request.headers


def create_app(transport=None, upstream=None) -> FastAPI:
    upstream = (upstream or os.getenv("UPSTREAM_BASE_URL", DEFAULT_UPSTREAM)).rstrip("/")
    app = FastAPI()
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10),
    )
    client.cookies.jar.set_policy(_NoCookies())

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def forward(path: str, request: Request):
        headers = _filter_request_headers(request.headers)
        # Ask upstream for identity so the bytes we relay match the headers we relay.
        headers["accept-encoding"] = "identity"
        # Forward path and query exactly as received (no decode/re-encode).
        raw_path = request.scope.get("raw_path") or request.url.path.encode()
        query = request.scope.get("query_string", b"")
        url = upstream + raw_path.decode("latin-1")
        if query:
            url += "?" + query.decode("latin-1")
        try:
            req = client.build_request(
                request.method, url, headers=headers,
                content=request.stream() if _has_body(request) else None,
            )
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            logger.warning("upstream error: %s", type(exc).__name__)
            return JSONResponse({"error": f"upstream request failed: {type(exc).__name__}"},
                                status_code=502)

        out_headers = {k: v for k, v in resp.headers.items()
                       if k.lower() not in HOP_BY_HOP | {"content-length", "content-encoding"}}
        out_headers["X-Accel-Buffering"] = "no"
        return StreamingResponse(resp.aiter_bytes(), status_code=resp.status_code,
                                 headers=out_headers, background=BackgroundTask(resp.aclose))

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8080")))
