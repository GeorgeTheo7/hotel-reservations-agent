"""The CSRF guard and the security headers (D82, D83), as one HTTP middleware.

CSRF ("cross-site request forgery"): another site makes the owner's browser send a
request that carries the owner's cookie. SameSite=Strict already stops the browser
attaching the cookie; this guard is a second, independent check. Every request that
can change something (any method but GET, HEAD, OPTIONS) must carry
`X-Anemos-Client: web`, which a plain cross-site form cannot add and a cross-site
fetch can only add after a CORS preflight that this API never grants; and an `Origin`
header, when present, must be the request's own origin. It runs before routing, so it
covers login and paths that do not exist, and answers before any database connection.
"""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse

CLIENT_HEADER = "X-Anemos-Client"
CLIENT_VALUE = "web"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

SECURITY_HEADERS = {
    # Only our own origin may load scripts, styles, images or be fetched; no inline
    # script; no page may frame this one (clickjacking on the Approve button).
    "Content-Security-Policy": (
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def csrf_refusal(request: Request) -> str | None:
    """Why the request is refused, or None if it passes."""
    if request.method in SAFE_METHODS:
        return None
    if request.headers.get(CLIENT_HEADER) != CLIENT_VALUE:
        return f"missing {CLIENT_HEADER}"
    origin = request.headers.get("origin")
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin is not None and origin != own:
        return "foreign origin"
    return None


async def guard_and_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response: Response
    if csrf_refusal(request) is not None:
        response = JSONResponse({"error": "csrf"}, status_code=403)
    else:
        response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    return response
