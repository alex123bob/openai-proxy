import os
import time
from contextlib import asynccontextmanager
from http.cookiejar import CookiePolicy

import anyio
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from diagnostics import RequestDiagnostics, configure_logging

load_dotenv()

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


class _ShieldedStream(httpx.AsyncByteStream):
    """Protect httpx's automatic EOF close from Starlette's disconnect cancellation."""

    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        with anyio.CancelScope(shield=True):
            await self.stream.aclose()


class _DiagnosticApp(FastAPI):
    """Observe the entire ASGI stack, including FastAPI-generated 500 responses."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.diagnostics = RequestDiagnostics(super().__call__)

    async def __call__(self, scope, receive, send):
        await self.diagnostics(scope, receive, send)


class _UpstreamResponse(StreamingResponse):
    def __init__(self, upstream_response, metrics, **kwargs):
        self.upstream_response = upstream_response

        async def chunks():
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            except httpx.HTTPError as exc:
                metrics["outcome"] = "stream_error"
                metrics["error_type"] = type(exc).__name__
                # Uvicorn may print the traceback; don't include upstream URLs or secrets.
                raise RuntimeError("upstream response stream failed") from None

        super().__init__(chunks(), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.upstream_response.aclose()


def create_app(transport=None, upstream=None) -> FastAPI:
    configure_logging()
    upstream = (upstream or os.getenv("UPSTREAM_BASE_URL", DEFAULT_UPSTREAM)).rstrip("/")
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10),
    )
    client.cookies.jar.set_policy(_NoCookies())

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await client.aclose()

    app = _DiagnosticApp(lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def forward(path: str, request: Request):
        metrics = request.state.diagnostics
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
            resp.stream = _ShieldedStream(resp.stream)
            metrics["upstream_headers_ms"] = (time.perf_counter() - metrics["started"]) * 1000
            metrics["upstream_status"] = resp.status_code
        except httpx.HTTPError as exc:
            metrics["outcome"] = "upstream_error"
            metrics["error_type"] = type(exc).__name__
            return JSONResponse({"error": f"upstream request failed: {type(exc).__name__}"},
                                status_code=502)

        try:
            out_headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in HOP_BY_HOP | {"content-length", "content-encoding"}}
            out_headers["X-Accel-Buffering"] = "no"
            return _UpstreamResponse(resp, metrics, status_code=resp.status_code,
                                     headers=out_headers)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await resp.aclose()
            raise

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8080")),
                access_log=False)
