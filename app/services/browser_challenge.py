from __future__ import annotations

import hashlib
import json
import secrets
import time
from urllib.parse import quote, unquote

from fastapi import Request, Response
from fastapi.responses import RedirectResponse
import jwt as pyjwt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from redis.asyncio import Redis

from config import Settings
from services.traefik_security import normalize_level

CHALLENGE_PREFIX = "sec:challenge:"
BOOT_PREFIX = "sec:chboot:"
VERIFY_LIMIT_PREFIX = "sec:verifylim:"
CHALLENGE_LOAD_KEY = "sec:challenge_load"
COOKIE_NAME = "pushify_sec"
BOOT_COOKIE = "pushify_ch_sess"
_PASS_OK_CACHE: dict[str, float] = {}
_PASS_CACHE_TTL_SEC = 45.0
CHALLENGE_PREFIX_PATH = "/.pushify"
PANEL_ONLY_PREFIXES = ("/auth/", "/admin/", "/user/", "/api/")
STATIC_PATHS = frozenset({"/favicon.ico", "/robots.txt", "/sitemap.xml", "/apple-touch-icon.png"})
STATIC_SUFFIXES = (
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".avif",
    ".wasm",
    ".json",
)

BOT_USER_AGENTS = frozenset({
    "curl", "wget", "python-requests", "python-urllib", "libwww-perl",
    "httpclient", "java", "scrapy", "selenium", "phantomjs", "puppeteer",
    "headlesschrome", "headless", "bot", "crawler", "spider", "scraper",
    "facebookexternalhit", "facebot", "googlebot", "bingbot", "yandexbot",
    "slurp", "duckduckbot", "baiduspider", "ahrefsbot", "semrushbot",
    "mj12bot", "dotbot", "gigabot", "exabot", "sogou", "teoma",
    "ia_archiver", "alexabot", "curl", "wget", "python", "go-http",
    "ruby", "perl", "libwww", "httpie", "postman", "insomnia",
    "zgrab", "masscan", "nmap", "sqlmap", "nikto", "burp", "owasp",
    "cloudflare-warp", "cf-worker", "vercel", "netlify", "render",
})


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="pushify-browser-challenge")


def normalize_host(host: str | None) -> str:
    if not host:
        return ""
    return host.split(",")[0].strip().split(":")[0].lower()


def client_host(request: Request) -> str:
    for header in ("x-forwarded-host", "x-real-host", "host"):
        raw = request.headers.get(header)
        if not raw:
            continue
        host = normalize_host(raw)
        if host:
            return host
    return ""


def client_ip(request: Request) -> str:
    from config import get_settings
    from utils.http import client_ip as resolve_client_ip

    settings = get_settings()
    return resolve_client_ip(request, behind_cloudflare=settings.behind_cloudflare)


def ua_hash(request: Request) -> str:
    ua = (request.headers.get("user-agent") or "")[:512]
    return hashlib.sha256(ua.encode()).hexdigest()[:16]


def _normalize_language_tag(tag: str) -> str:
    return (tag or "").strip().split(";")[0].strip().lower()[:64]


def _language_tags_match(stored: str, reported: str) -> bool:
    a = _normalize_language_tag(stored)
    b = _normalize_language_tag(reported)
    if not a or not b:
        return True
    if a == b:
        return True
    return a.split("-", 1)[0] == b.split("-", 1)[0]


def accept_language_tag(request: Request) -> str:
    raw = (request.headers.get("accept-language") or "").split(",")[0].strip()
    return _normalize_language_tag(raw)


def is_static_asset_path(path: str | None) -> bool:
    if not path:
        return False
    base = path.split("?", 1)[0].lower()
    if base in STATIC_PATHS:
        return True
    return any(base.endswith(suffix) for suffix in STATIC_SUFFIXES)


def wants_html_challenge(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        return True
    dest = (request.headers.get("sec-fetch-dest") or "").lower()
    if dest in ("document", "iframe"):
        return True
    if request.method == "GET" and ("*/*" in accept or not accept):
        mode = (request.headers.get("sec-fetch-mode") or "").lower()
        if mode in ("navigate", ""):
            return True
    return False


def is_challenge_path(path: str | None) -> bool:
    if not path:
        return False
    base = path.split("?", 1)[0].rstrip("/") or "/"
    if base == CHALLENGE_PREFIX_PATH:
        return True
    return base.startswith(f"{CHALLENGE_PREFIX_PATH}/")


def is_panel_only_path(path: str | None) -> bool:
    if not path:
        return False
    base = path.split("?", 1)[0]
    return any(base.startswith(prefix) for prefix in PANEL_ONLY_PREFIXES)


def safe_next_path(next_url: str | None) -> str:
    if not next_url:
        return "/"
    path = next_url
    for _ in range(8):
        if not path.startswith("/") or path.startswith("//"):
            return "/"
        if is_challenge_path(path):
            return "/"
        if "%" not in path:
            break
        path = unquote(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or is_challenge_path(path)
        or is_static_asset_path(path)
        or is_panel_only_path(path)
    ):
        return "/"
    return path[:2048]


async def _challenge_load(redis: Redis) -> int:
    raw = await redis.get(CHALLENGE_LOAD_KEY)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


async def _bump_challenge_load(redis: Redis) -> int:
    pipe = redis.pipeline()
    pipe.incr(CHALLENGE_LOAD_KEY)
    pipe.expire(CHALLENGE_LOAD_KEY, 120)
    load, _ = await pipe.execute()
    return int(load)


async def _drop_challenge_load(redis: Redis) -> None:
    val = await redis.decr(CHALLENGE_LOAD_KEY)
    if val is not None and int(val) < 0:
        await redis.set(CHALLENGE_LOAD_KEY, 0, ex=120)


async def difficulty_for_level(
    level: str | None, settings: Settings, redis: Redis | None = None
) -> int:
    level = normalize_level(level)
    if level != "under_attack":
        return settings.security_pow_difficulty
    base = 4
    if redis is None:
        return base
    load = await _challenge_load(redis)
    if load >= settings.security_pow_load_high:
        return min(6, base + 2)
    if load >= settings.security_pow_load_elevated:
        return min(5, base + 1)
    return base


def max_solve_ms_for_difficulty(difficulty: int) -> int:
    return min(180_000, max(30_000, difficulty * 30_000))


def is_likely_bot_ua(user_agent: str) -> bool:
    ua_lower = (user_agent or "").lower()
    return any(bot in ua_lower for bot in BOT_USER_AGENTS)


def has_browser_headers(request: Request) -> bool:
    ua = request.headers.get("user-agent") or ""
    if len(ua) < 12:
        return False
    ua_lower = ua.lower()
    if ua_lower.startswith(("curl/", "wget/", "python-", "go-http", "ruby", "perl")):
        return False
    if is_likely_bot_ua(ua):
        return False
    if request.method == "POST":
        site = (request.headers.get("sec-fetch-site") or "").lower()
        if site and site not in ("same-origin", "same-site", "none"):
            return False
        mode = (request.headers.get("sec-fetch-mode") or "").lower()
        if mode and mode not in ("cors", "same-origin", "navigate", "no-cors"):
            return False
    return True


def validate_fingerprint(fp: dict | None, bind: str, al_tag: str = "") -> bool:
    if not isinstance(fp, dict):
        return False

    if fp.get("w") is True:
        return False

    if not fp.get("lg"):
        return False

    if not fp.get("ch") or len(str(fp.get("ch"))) < 8:
        return False

    if fp.get("sft") != bind:
        return False

    if al_tag and fp.get("alt") and not _language_tags_match(al_tag, str(fp.get("alt"))):
        return False

    try:
        if int(fp.get("hc", 0)) < 1:
            return False
    except (TypeError, ValueError):
        return False

    try:
        if int(fp.get("dm", 0)) < 1:
            pass
    except (TypeError, ValueError):
        pass

    browser_fp = fp.get("fp")
    if browser_fp and isinstance(browser_fp, dict):
        if browser_fp.get("hardwareConcurrency", 0) == 0:
            return False
        if browser_fp.get("screenWidth", 0) == 0:
            return False
        if browser_fp.get("screenHeight", 0) == 0:
            return False

    return True


def is_bot_fingerprint(fp: dict | None) -> bool:
    if not isinstance(fp, dict):
        return True

    if fp.get("w") is True:
        return True

    browser_fp = fp.get("fp")
    if browser_fp and isinstance(browser_fp, dict):
        if browser_fp.get("screenWidth", 0) == 0:
            return True
        if browser_fp.get("screenHeight", 0) == 0:
            return True

    return False


async def check_verify_rate_limit(redis: Redis, ip: str, settings: Settings) -> bool:
    if not ip:
        return True
    key = f"{VERIFY_LIMIT_PREFIX}{ip}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    return count <= settings.security_verify_rate_limit


async def issue_challenge(
    redis: Redis,
    host: str,
    settings: Settings,
    request: Request,
    *,
    level: str = "standard",
) -> dict[str, str | int]:
    host = normalize_host(host)
    challenge_id = secrets.token_urlsafe(16)
    bind = secrets.token_hex(8)
    nonce = secrets.token_hex(8)
    await _bump_challenge_load(redis)
    difficulty = await difficulty_for_level(level, settings, redis)
    issued_at = time.time()
    al_tag = accept_language_tag(request)
    payload = {
        "host": host,
        "ip": client_ip(request),
        "ua": ua_hash(request),
        "bind": bind,
        "nonce": nonce,
        "al": al_tag,
        "difficulty": difficulty,
        "issued_at": issued_at,
    }
    await redis.setex(
        f"{CHALLENGE_PREFIX}{challenge_id}",
        settings.security_challenge_ttl_seconds,
        json.dumps(payload),
    )
    proof = _serializer(settings).dumps(
        {"id": challenge_id, "host": host, "bind": bind, "nonce": nonce, "v": 3}
    )
    return {
        "id": challenge_id,
        "difficulty": difficulty,
        "proof": proof,
        "bind": bind,
        "issued_at": int(issued_at * 1000),
    }


async def create_boot_session(
    redis: Redis,
    *,
    host: str,
    next_path: str,
    issue: dict[str, str | int],
    min_ms: int,
    max_ms: int,
    settings: Settings,
) -> str:
    token = secrets.token_urlsafe(32)
    payload = {
        "host": normalize_host(host),
        "next": next_path,
        "issue": {
            "id": issue["id"],
            "proof": issue["proof"],
            "bind": issue["bind"],
            "difficulty": issue["difficulty"],
            "issued_at": issue["issued_at"],
        },
        "min_ms": min_ms,
        "max_ms": max_ms,
    }
    await redis.setex(
        f"{BOOT_PREFIX}{token}",
        settings.security_challenge_ttl_seconds,
        json.dumps(payload),
    )
    return token


async def read_boot_session(redis: Redis, token: str, host: str) -> dict | None:
    raw = await redis.get(f"{BOOT_PREFIX}{token}")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if normalize_host(data.get("host")) != normalize_host(host):
        return None
    issue = data.get("issue")
    if not isinstance(issue, dict) or not issue.get("id") or not issue.get("proof"):
        return None
    return data


async def clear_boot_session(redis: Redis, token: str) -> None:
    if token:
        await redis.delete(f"{BOOT_PREFIX}{token}")


def boot_cookie_value(request: Request) -> str:
    return (request.cookies.get(BOOT_COOKIE) or "").strip()


def set_boot_cookie(
    response: Response, token: str, settings: Settings, request: Request
) -> None:
    proto = (request.headers.get("x-forwarded-proto") or settings.url_scheme or "").lower()
    response.set_cookie(
        BOOT_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=proto == "https",
        max_age=settings.security_challenge_ttl_seconds,
        path="/.pushify",
    )


def clear_boot_cookie(response: Response) -> None:
    response.delete_cookie(BOOT_COOKIE, path="/.pushify")


def challenge_auth_redirect(
    request: Request, host: str, settings: Settings, next_path: str | None = None
) -> Response:
    redirect = challenge_redirect(request, host, settings, next_path)
    location = redirect.headers.get("location")
    if not location:
        location = (
            f"{settings.url_scheme}://{host}{CHALLENGE_PREFIX_PATH}/challenge?next=%2F"
        )
    return Response(status_code=302, headers={"Location": location})


def verify_issue_proof(
    proof: str, challenge_id: str, host: str, bind: str, nonce: str, settings: Settings
) -> bool:
    try:
        data = _serializer(settings).loads(
            proof, max_age=settings.security_challenge_ttl_seconds
        )
    except (BadSignature, SignatureExpired):
        return False
    return (
        isinstance(data, dict)
        and data.get("id") == challenge_id
        and normalize_host(data.get("host")) == normalize_host(host)
        and data.get("bind") == bind
        and data.get("nonce") == nonce
        and int(data.get("v", 0)) >= 3
    )


def verify_pow(challenge_id: str, counter: int, difficulty: int) -> bool:
    if difficulty < 1:
        return False
    digest = hashlib.sha256(f"{challenge_id}:{counter}".encode()).digest()
    zero_bytes = difficulty // 2
    if digest[:zero_bytes] != b"\x00" * zero_bytes:
        return False
    if difficulty % 2:
        return (digest[zero_bytes] >> 4) == 0
    return True


async def _take_challenge(redis: Redis, key: str) -> bytes | str | None:
    getdel = getattr(redis, "getdel", None)
    if getdel is not None:
        return await getdel(key)
    raw = await redis.get(key)
    if raw:
        await redis.delete(key)
    return raw


async def verify_challenge(
    redis: Redis,
    *,
    challenge_id: str,
    counter: int,
    host: str,
    proof: str,
    fp: dict | None,
    elapsed_ms: int,
    request: Request,
    settings: Settings,
) -> tuple[bool, str]:
    if not has_browser_headers(request):
        return False, "invalid_client"

    ip = client_ip(request)
    if not await check_verify_rate_limit(redis, ip, settings):
        return False, "rate_limited"

    if is_bot_fingerprint(fp):
        return False, "bot_detected"

    request_host = normalize_host(host)
    key = f"{CHALLENGE_PREFIX}{challenge_id}"
    raw = await _take_challenge(redis, key)
    if not raw:
        return False, "challenge_expired"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await _drop_challenge_load(redis)
        return False, "challenge_expired"

    stored_host = normalize_host(data.get("host"))
    if not stored_host or stored_host != request_host:
        await _drop_challenge_load(redis)
        return False, "host_mismatch"

    bind = data.get("bind") or ""
    nonce = data.get("nonce") or ""
    if not verify_issue_proof(proof, challenge_id, stored_host, bind, nonce, settings):
        await _drop_challenge_load(redis)
        return False, "invalid_proof"

    if data.get("ip") and data.get("ip") != ip:
        await _drop_challenge_load(redis)
        return False, "session_changed"
    if data.get("ua") and data.get("ua") != ua_hash(request):
        await _drop_challenge_load(redis)
        return False, "session_changed"

    if not validate_fingerprint(fp, bind, data.get("al") or ""):
        await _drop_challenge_load(redis)
        return False, "invalid_fingerprint"

    issued_at = float(data.get("issued_at", 0))
    age = time.time() - issued_at
    min_ms = int(settings.security_pow_min_seconds * 1000)
    if age * 1000 < min_ms:
        await _drop_challenge_load(redis)
        return False, "too_fast"

    difficulty = int(data.get("difficulty", settings.security_pow_difficulty))
    max_ms = max_solve_ms_for_difficulty(difficulty)
    if elapsed_ms < min_ms:
        await _drop_challenge_load(redis)
        return False, "too_fast"
    if elapsed_ms > max_ms:
        await _drop_challenge_load(redis)
        return False, "too_slow"

    if not verify_pow(challenge_id, counter, difficulty):
        await _drop_challenge_load(redis)
        return False, "invalid_pow"

    await _drop_challenge_load(redis)
    return True, "ok"


def _pass_claims_match(
    data: dict, host: str, request: Request, *, min_version: int = 2
) -> bool:
    if not isinstance(data, dict):
        return False
    if normalize_host(data.get("host")) != normalize_host(host):
        return False
    if int(data.get("v", 1)) < min_version:
        return False
    if data.get("ip") and data.get("ip") != client_ip(request):
        return False
    if data.get("ua") and data.get("ua") != ua_hash(request):
        return False
    return True


def _pass_cache_key(host: str, token: str) -> str:
    return hashlib.sha256(host.encode() + b"\0" + token.encode()).hexdigest()[:32]


def issue_pass_cookie(host: str, settings: Settings, request: Request) -> str:
    now = int(time.time())
    return pyjwt.encode(
        {
            "host": host,
            "ip": client_ip(request),
            "ua": ua_hash(request),
            "v": 3,
            "typ": "pushify_sec",
            "iat": now,
            "exp": now + settings.security_pass_ttl_seconds,
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _verify_pass_cookie_legacy(
    token: str, host: str, settings: Settings, request: Request
) -> bool:
    try:
        data = _serializer(settings).loads(
            token, max_age=settings.security_pass_ttl_seconds
        )
    except (BadSignature, SignatureExpired):
        return False
    return _pass_claims_match(data, host, request, min_version=2)


def _verify_pass_cookie_uncached(
    token: str, host: str, settings: Settings, request: Request
) -> bool:
    if token.count(".") == 2:
        try:
            data = pyjwt.decode(
                token,
                settings.secret_key,
                algorithms=["HS256"],
                options={"require": ["exp", "iat"]},
            )
            if data.get("typ") != "pushify_sec":
                return False
            if _pass_claims_match(data, host, request, min_version=3):
                return True
        except pyjwt.PyJWTError:
            pass
    return _verify_pass_cookie_legacy(token, host, settings, request)


def verify_pass_cookie(request: Request, host: str, settings: Settings) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    cache_key = _pass_cache_key(normalize_host(host), token)
    now = time.monotonic()
    expires = _PASS_OK_CACHE.get(cache_key)
    if expires is not None and expires > now:
        return True
    ok = _verify_pass_cookie_uncached(token, host, settings, request)
    if ok:
        if len(_PASS_OK_CACHE) > 8192:
            _PASS_OK_CACHE.clear()
        _PASS_OK_CACHE[cache_key] = now + _PASS_CACHE_TTL_SEC
    return ok


def set_pass_cookie(response: Response, host: str, settings: Settings, request: Request) -> None:
    response.set_cookie(
        COOKIE_NAME,
        issue_pass_cookie(host, settings, request),
        httponly=True,
        samesite="lax",
        secure=(settings.url_scheme == "https"),
        max_age=settings.security_pass_ttl_seconds,
        path="/",
    )


def challenge_redirect(
    request: Request, host: str, settings: Settings, next_path: str | None = None
) -> RedirectResponse:
    proto = request.headers.get("x-forwarded-proto", settings.url_scheme)
    if next_path is not None:
        raw_next = next_path
    else:
        raw_next = request.headers.get("x-forwarded-uri") or request.url.path or "/"
    if is_challenge_path(raw_next.split("?", 1)[0]):
        raw_next = "/"
    path = safe_next_path(raw_next)
    url = f"{proto}://{host}{CHALLENGE_PREFIX_PATH}/challenge?next={quote(path)}"
    return RedirectResponse(url, status_code=302)
