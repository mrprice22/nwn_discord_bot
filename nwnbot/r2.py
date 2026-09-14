"""Cloudflare R2 as an :class:`~nwnbot.attachments.ImageStore`.

R2 speaks the S3 API, so this is SigV4 request signing and three verbs: HEAD
to skip work already done, PUT to store, and a public URL to read back. It is
deliberately stdlib only — `hmac` and `hashlib` are all SigV4 actually needs,
and pulling in boto3 (and botocore, and a transitive tail) to sign three
request shapes would be the larger cost, in a package that is stdlib elsewhere.

Reads go to a **custom domain** (`img.homerslotr.com`), never the signing
endpoint: the bucket is public for reading, and the object URL ends up written
into roadmap items, so it must be a plain durable link with no credential and
no expiry in it. That is the entire point of rehosting — see
:mod:`nwnbot.attachments` for what the signed Discord links did instead.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import urllib.error
import urllib.request
from dataclasses import dataclass

from nwnbot.attachments import TARGET_CONTENT_TYPE

#: R2 ignores the region but SigV4 requires one in the credential scope, and
#: "auto" is what Cloudflare documents.
REGION = "auto"
SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"

#: Characters that stay literal in a canonical URI. Note "/" is NOT escaped in
#: the path, but everything outside the unreserved set is — getting this wrong
#: produces a signature mismatch that reads like a credential problem.
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~")


class R2Error(Exception):
    """An R2 request failed."""


def _quote(value: str, safe: str = "") -> str:
    out = []
    for ch in value:
        if ch in _UNRESERVED or ch in safe:
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, date_stamp: str, region: str = REGION,
                service: str = SERVICE) -> bytes:
    """The SigV4 derived key.

    Separated out and tested against AWS's own published vector, because every
    other part of the signature is easy to eyeball and this one is not.
    """
    k_date = _sign(f"AWS4{secret}".encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def authorization_header(*, method: str, host: str, path: str,
                         access_key: str, secret_key: str,
                         payload_sha256: str, amz_date: str,
                         content_type: str = "",
                         region: str = REGION,
                         service: str = SERVICE) -> tuple[str, dict[str, str]]:
    """Build the Authorization header and the headers it commits to.

    Returns ``(authorization, headers)``. The headers are returned rather than
    assumed because the signature covers exactly this set, in this order: send
    a different set and the request is rejected.
    """
    date_stamp = amz_date[:8]
    canonical_uri = _quote(path, safe="/")

    signed = {"host": host,
              "x-amz-content-sha256": payload_sha256,
              "x-amz-date": amz_date}
    if content_type:
        signed["content-type"] = content_type

    names = sorted(signed)
    canonical_headers = "".join(f"{n}:{signed[n].strip()}\n" for n in names)
    signed_headers = ";".join(names)

    canonical_request = "\n".join([
        method, canonical_uri, "", canonical_headers, signed_headers,
        payload_sha256,
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        ALGORITHM, amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret_key, date_stamp, region, service),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    authorization = (f"{ALGORITHM} Credential={access_key}/{scope}, "
                     f"SignedHeaders={signed_headers}, Signature={signature}")
    return authorization, signed


@dataclass(repr=False)
class R2Store:
    """Object storage for rehosted screenshots.

    ``public_base_url`` is the custom domain the bucket is served on and is the
    only thing that ever ends up in a roadmap item. ``account_id`` +
    ``bucket`` build the signing endpoint, which is never published.
    """

    account_id: str
    bucket: str
    access_key: str
    secret_key: str
    public_base_url: str
    timeout: float = 60.0

    def __post_init__(self) -> None:
        missing = [n for n in ("account_id", "bucket", "access_key",
                               "secret_key", "public_base_url")
                   if not getattr(self, n)]
        if missing:
            raise ValueError(f"R2Store needs {', '.join(missing)}")
        self.public_base_url = self.public_base_url.rstrip("/")
        if "://" not in self.public_base_url:
            self.public_base_url = f"https://{self.public_base_url}"

    def __repr__(self) -> str:
        """Redacted. A dataclass repr would print the secret, and reprs end up
        in logs and tracebacks — the two places a credential must never be."""
        return (f"R2Store(account_id={self.account_id!r}, bucket={self.bucket!r}, "
                f"public_base_url={self.public_base_url!r}, "
                f"access_key=<redacted>, secret_key=<redacted>)")

    @property
    def host(self) -> str:
        return f"{self.account_id}.r2.cloudflarestorage.com"

    def url_for(self, key: str) -> str:
        return f"{self.public_base_url}/{_quote(key, safe='/')}"

    def _request(self, method: str, key: str, data: bytes | None = None,
                 content_type: str = "") -> int:
        path = f"/{self.bucket}/{key}"
        body = data or b""
        payload_sha256 = hashlib.sha256(body).hexdigest()
        amz_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        auth, headers = authorization_header(
            method=method, host=self.host, path=path,
            access_key=self.access_key, secret_key=self.secret_key,
            payload_sha256=payload_sha256, amz_date=amz_date,
            content_type=content_type)
        headers = dict(headers)
        headers["Authorization"] = auth
        if data is not None:
            headers["Content-Length"] = str(len(body))
        req = urllib.request.Request(f"https://{self.host}{path}",
                                     data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code == 404:
                return 404
            detail = ""
            try:
                detail = exc.read()[:400].decode("utf-8", "replace")
            except Exception:  # pragma: no cover - best effort only
                pass
            raise R2Error(f"{method} {key}: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise R2Error(f"{method} {key}: {exc.reason}") from exc

    def exists(self, key: str) -> bool:
        return self._request("HEAD", key) != 404

    def put(self, key: str, data: bytes,
            content_type: str = TARGET_CONTENT_TYPE) -> str:
        self._request("PUT", key, data=data, content_type=content_type)
        return self.url_for(key)


def from_env(env) -> R2Store | None:
    """Build a store from the environment, or ``None`` when not configured.

    ``None`` is a supported state, not an error: without it the bot still runs
    and simply reports images as present-but-not-kept. Half-configured IS an
    error, though — silently not storing images because one variable was
    mistyped is exactly the failure this whole module exists to prevent.
    """
    from nwnbot import config as cfg

    values = {name: (env.get(name) or "").strip() for name in (
        cfg.ENV_R2_ACCOUNT_ID, cfg.ENV_R2_BUCKET, cfg.ENV_R2_ACCESS_KEY_ID,
        cfg.ENV_R2_SECRET_ACCESS_KEY, cfg.ENV_R2_PUBLIC_BASE_URL)}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise cfg.ConfigError(
            "R2 is partly configured; these are missing: "
            + ", ".join(sorted(missing))
            + ". Set all of them or none — a half-configured store would drop "
              "screenshots silently.")
    return R2Store(account_id=values[cfg.ENV_R2_ACCOUNT_ID],
                   bucket=values[cfg.ENV_R2_BUCKET],
                   access_key=values[cfg.ENV_R2_ACCESS_KEY_ID],
                   secret_key=values[cfg.ENV_R2_SECRET_ACCESS_KEY],
                   public_base_url=values[cfg.ENV_R2_PUBLIC_BASE_URL])


__all__ = ["ALGORITHM", "R2Error", "R2Store", "REGION", "SERVICE",
           "authorization_header", "from_env", "signing_key"]
