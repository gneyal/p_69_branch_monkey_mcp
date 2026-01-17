"""
Dev Proxy for Branch Monkey

Runs a reverse proxy on the auth-allowed port (5176) that forwards
to any local dev server. This allows testing worktree branches
without needing to add each port to Supabase's redirect URL allowlist.

Usage:
    branch-monkey-proxy 5789        # Proxy 5176 -> 5789
    branch-monkey-proxy 5789 5176   # Explicit: proxy_port -> target_port
"""

import asyncio
import os
import sys
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
import uvicorn


def create_proxy_app(target_port: int) -> FastAPI:
    """Create a FastAPI app that proxies all requests to target_port."""
    app = FastAPI(title="Branch Monkey Dev Proxy")

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
    async def proxy(request: Request, path: str):
        """Proxy all requests to the target server."""
        target_url = f"http://localhost:{target_port}/{path}"

        # Get query string
        if request.url.query:
            target_url += f"?{request.url.query}"

        # Forward headers (except host)
        headers = dict(request.headers)
        headers.pop("host", None)

        # Get body for methods that have one
        body = None
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                # Make the proxied request
                response = await client.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    content=body,
                    follow_redirects=False
                )

                # Build response headers
                response_headers = dict(response.headers)
                # Remove hop-by-hop headers
                for header in ["transfer-encoding", "connection", "keep-alive"]:
                    response_headers.pop(header, None)

                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    headers=response_headers,
                    media_type=response.headers.get("content-type")
                )

            except httpx.ConnectError:
                return Response(
                    content=f"Could not connect to target server on port {target_port}",
                    status_code=502,
                    media_type="text/plain"
                )
            except Exception as e:
                return Response(
                    content=f"Proxy error: {str(e)}",
                    status_code=500,
                    media_type="text/plain"
                )

    return app


def run_proxy(target_port: int, proxy_port: int = 5176):
    """Run the dev proxy server."""
    print(f"\n🐵 Branch Monkey Dev Proxy")
    print(f"   Listening on: http://localhost:{proxy_port}")
    print(f"   Forwarding to: http://localhost:{target_port}")
    print(f"\n   Access your app at http://localhost:{proxy_port}")
    print(f"   (Auth will work because Supabase allows port {proxy_port})\n")

    app = create_proxy_app(target_port)

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=proxy_port,
        log_level="warning"
    )


def main():
    """CLI entry point for branch-monkey-proxy."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Proxy requests from auth-allowed port to your dev server",
        usage="branch-monkey-proxy TARGET_PORT [PROXY_PORT]"
    )
    parser.add_argument(
        "target_port",
        type=int,
        help="Port your dev server is running on (e.g., 5789)"
    )
    parser.add_argument(
        "proxy_port",
        type=int,
        nargs="?",
        default=5176,
        help="Port to listen on (default: 5176, the Supabase-allowed port)"
    )

    args = parser.parse_args()

    run_proxy(args.target_port, args.proxy_port)


if __name__ == "__main__":
    main()
