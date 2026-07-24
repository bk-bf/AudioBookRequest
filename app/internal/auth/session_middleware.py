from typing import final
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.util.time import Second


@final
class DynamicSessionMiddleware:
    """
    A wrapper around the Starlette SessionMiddleware with the ability to
    change options during run-time
    https://www.starlette.io/middleware/#sessionmiddleware
    """

    def __init__(
        self,
        app: ASGIApp,
        secret_key: str,
        linker: "DynamicMiddlewareLinker",
        max_age: Second | None = None,
    ):
        self.app = app
        self.secret_key = secret_key
        # Normalize to the effective lifetime so it is never None. A None max_age
        # makes Starlette emit a cookie without Max-Age (a session cookie that
        # dies on browser close), which is what logged users out of Brave mobile.
        self.expiry = max_age or Second(60 * 60 * 24 * 14)
        self.session_middleware = self._build()
        linker.add_middleware(self)

    def _build(self) -> SessionMiddleware:
        return SessionMiddleware(
            self.app,
            self.secret_key,
            # "lax", not "strict": Strict withholds the session cookie on any
            # top-level navigation that isn't already same-site, so reopening the
            # app from a bookmark / home-screen shortcut / external link (the norm
            # on mobile) sends no cookie and the user gets bounced to /login. This
            # is what logged people out of Brave on mobile after closing the tab.
            # Lax still sends the cookie on top-level GETs while blocking cross-site
            # POST/subresource CSRF.
            same_site="lax",
            max_age=self.expiry,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        return await self.session_middleware(scope, receive, send)

    def update_secret(self, secret_key: str):
        self.secret_key = secret_key
        self.session_middleware = self._build()

    def update_max_age(self, max_age: Second):
        self.expiry = max_age
        self.session_middleware = self._build()


class DynamicMiddlewareLinker:
    """
    Linker is passed in as an argument to the DynamicSessionMiddleware so
    wherever FastAPI initializes the middleware, we can update
    the options to take effect immediately instead of having to restart the server
    """

    middlewares: list[DynamicSessionMiddleware] = []

    def add_middleware(self, middleware: DynamicSessionMiddleware):
        self.middlewares.append(middleware)

    def update_secret(self, secret_key: str):
        for middleware in self.middlewares:
            middleware.update_secret(secret_key)

    def update_max_age(self, expiry: Second):
        for middleware in self.middlewares:
            middleware.update_max_age(expiry)


middleware_linker = DynamicMiddlewareLinker()
