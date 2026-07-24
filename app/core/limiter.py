"""
Shared slowapi rate-limiter instance.

Imported by both main.py (to register the middleware and error handler)
and the router (to apply limits to individual endpoints).  A singleton
avoids duplicate state and ensures the counter is shared across all routes.

The key function uses the client's real IP address.  On Render (or any
reverse-proxy deployment) the actual client IP arrives in X-Forwarded-For;
slowapi reads that header automatically when it's present.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
