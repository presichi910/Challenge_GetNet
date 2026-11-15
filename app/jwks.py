"""The identity provider's public signing keys, by `kid`, cached.

The API never holds a secret that can sign a token: it only fetches public keys (a JWKS document) and checks
signatures. What this cache guarantees:

- Only RS256 signing keys with a `kid` are kept (an IdP also publishes encryption keys: they are ignored).
- A known `kid` is answered from memory. The document is re-fetched after `ttl` seconds.
- An UNKNOWN `kid` may trigger a re-fetch (the IdP rotated its keys), but at most once every `min_refresh`
  seconds, whatever the callers send. Without that bound, anybody could make the API hit the IdP on every
  request just by sending random `kid` values.
- If the IdP is down, the keys already cached keep working (fail static): an outage of the IdP does not log
  every customer out. A cache that never got any key fails closed.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from jwt import PyJWK, PyJWKClient

log = logging.getLogger("auth.jwks")


class UnknownKid(Exception):
    """The token names a key the IdP does not publish (after the one allowed refresh)."""


class JwksUnavailable(Exception):
    """There is no usable key at all: the IdP could not be reached and nothing is cached."""


def fetch_jwks(url: str, timeout: float) -> dict:
    """GET the JWKS document. The only function here that touches the network (tests replace it)."""
    return PyJWKClient(url, timeout=timeout).fetch_data()


class JwksCache:
    def __init__(self, url: str, ttl: float = 600.0, min_refresh: float = 30.0, timeout: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self.url, self.ttl, self.min_refresh, self.timeout, self._clock = url, ttl, min_refresh, timeout, clock
        self._keys: dict[str, PyJWK] = {}
        self._loaded_at: float | None = None
        self._last_attempt: float | None = None
        self._lock = threading.Lock()

    def _refresh(self, now: float) -> None:
        self._last_attempt = now
        data = fetch_jwks(self.url, self.timeout)
        keys: dict[str, PyJWK] = {}
        for jwk in data.get("keys", []) if isinstance(data, dict) else []:
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or jwk.get("use") not in (None, "sig"):
                continue
            if jwk.get("alg") not in (None, "RS256") or not isinstance(jwk.get("kid"), str):
                continue
            try:
                keys[jwk["kid"]] = PyJWK.from_dict(jwk)
            except Exception:  # noqa: BLE001 - a malformed key must not hide the good ones
                log.warning("jwks: ignoring a malformed key")
        if not keys:
            raise JwksUnavailable("the JWKS has no usable RS256 key")
        self._keys, self._loaded_at = keys, now  # replaced as a whole: a rotated-out key stops working

    def key_for(self, kid: str):
        """The public key for `kid`. Raises UnknownKid or JwksUnavailable."""
        with self._lock:
            now = self._clock()
            fresh = self._loaded_at is not None and now - self._loaded_at < self.ttl
            if fresh and kid in self._keys:
                return self._keys[kid].key
            if self._last_attempt is None or now - self._last_attempt >= self.min_refresh:
                try:
                    self._refresh(now)
                except Exception as e:  # noqa: BLE001 - keep serving what is cached
                    log.warning("jwks refresh failed (%s); keeping the cached keys", type(e).__name__)
            if kid in self._keys:
                return self._keys[kid].key
            if not self._keys:
                raise JwksUnavailable("no key is cached and the IdP could not be reached")
            raise UnknownKid(kid)
