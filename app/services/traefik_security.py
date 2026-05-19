from __future__ import annotations

from pathlib import Path

from config import Settings

VALID_LEVELS = frozenset({"off", "standard", "high", "under_attack"})
DEFAULT_LEVEL = "standard"


def normalize_level(level: str | None) -> str:
    if level in VALID_LEVELS:
        return level
    return DEFAULT_LEVEL


def needs_browser_challenge(level: str | None) -> bool:
    """Browser verification page + forward-auth (only under_attack)."""
    return normalize_level(level) == "under_attack"


def needs_browser_protection(level: str | None) -> bool:
    return needs_browser_challenge(level)


def needs_challenge_router(level: str | None) -> bool:
    return needs_browser_challenge(level)


def middleware_csv(level: str | None, *, noindex: bool = False) -> str:
    level = normalize_level(level)
    parts: list[str] = []
    if noindex:
        parts.append("pushify-noindex@file")
    if needs_browser_protection(level):
        parts.append("pushify-forward-auth@file")
    if level == "standard":
        parts.extend(
            ["pushify-inflight-deploy@file", "pushify-ratelimit-standard@file"]
        )
    elif level == "high":
        parts.extend(
            [
                "pushify-inflight-deploy@file",
                "pushify-ratelimit-high@file",
                "pushify-headers@file",
            ]
        )
    elif level == "under_attack":
        parts.extend(
            ["pushify-inflight-attack@file", "pushify-ratelimit-attack@file"]
        )
    parts.append("devpush-errors@docker")
    return ",".join(parts)


def entrypoints(settings: Settings) -> list[str]:
    if settings.url_scheme == "https":
        return ["websecure"]
    return ["web"]


def tls_block(settings: Settings, *, http01: bool = False) -> dict | None:
    if settings.url_scheme != "https":
        return None
    resolver = "lehttp" if http01 else "le"
    return {"certResolver": resolver}


def challenge_router(
    name: str,
    host: str,
    settings: Settings,
    *,
    http01: bool = False,
) -> dict:
    cfg: dict = {
        "rule": f"Host(`{host}`) && PathPrefix(`/.pushify`)",
        "priority": 20,
        "service": "pushify-security@file",
        "entryPoints": entrypoints(settings),
    }
    tls = tls_block(settings, http01=http01)
    if tls:
        cfg["tls"] = tls
    return cfg


def traefik_label_entrypoints(settings: Settings, router: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    if settings.url_scheme == "https":
        labels[f"traefik.http.routers.{router}.entrypoints"] = "websecure"
        labels[f"traefik.http.routers.{router}.tls"] = "true"
        labels[f"traefik.http.routers.{router}.tls.certresolver"] = "le"
    else:
        labels[f"traefik.http.routers.{router}.entrypoints"] = "web"
    return labels


def docker_challenge_labels(
    router: str, host: str, settings: Settings
) -> dict[str, str]:
    challenge = f"{router}-challenge"
    labels = {
        f"traefik.http.routers.{challenge}.rule": (
            f"Host(`{host}`) && PathPrefix(`/.pushify`)"
        ),
        f"traefik.http.routers.{challenge}.priority": "20",
        f"traefik.http.routers.{challenge}.service": "pushify-security@file",
        "traefik.docker.network": "devpush_runner",
    }
    labels.update(traefik_label_entrypoints(settings, challenge))
    return labels


SECURITY_MIDDLEWARES_VERSION = "v6a"


def _ip_criterion_yaml(*, behind_cloudflare: bool) -> str:
    if behind_cloudflare:
        return "          requestHeaderName: CF-Connecting-IP"
    return "          ipStrategy:\n            depth: 1"


def _trusted_proxy_cidrs(settings: Settings) -> list[str]:
    cidrs = ["127.0.0.1/32", "::1/128"]
    if settings.behind_cloudflare:
        cf_list = (
            Path(settings.host_app_dir or settings.app_dir)
            / "scripts"
            / "provision"
            / "cloudflare-ips.txt"
        )
        if cf_list.is_file():
            for line in cf_list.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    cidrs.append(line)
    extra = (settings.trusted_proxy_cidrs or "").strip()
    if extra and extra.lower() != "cloudflare":
        for part in extra.split(","):
            part = part.strip()
            if part:
                cidrs.append(part)
    return list(dict.fromkeys(cidrs))


def _entrypoint_block(cidrs: list[str]) -> list[str]:
    block = [
        "    http:",
        "      middlewares:",
        "        - pushify-edge-guard@file",
    ]
    if len(cidrs) > 2:
        block.extend(["    forwardedHeaders:", "      trustedIPs:"])
        block.extend(f'        - "{c}"' for c in cidrs)
    return block


def _entrypoints_yaml(settings: Settings) -> str:
    cidrs = _trusted_proxy_cidrs(settings)
    lines = ["entryPoints:", "  web:"]
    lines.extend(_entrypoint_block(cidrs))
    lines.extend(["  websecure:"])
    lines.extend(_entrypoint_block(cidrs))
    return "\n".join(lines)


def security_middlewares_yaml(
    *, behind_cloudflare: bool = False, enable_https: bool = True
) -> str:
    ip = _ip_criterion_yaml(behind_cloudflare=behind_cloudflare)
    mode = "cf" if behind_cloudflare else "direct"
    cf_header = "          - CF-Connecting-IP\n" if behind_cloudflare else ""
    redirect = ""
    if enable_https:
        redirect = """    pushify-redirect-https:
      redirectScheme:
        scheme: https
        permanent: true
"""
    return f"""# pushify-ratelimit-{SECURITY_MIDDLEWARES_VERSION} ({mode}) burst allows ~50 assets/page; average limits sustained flood
http:
  services:
    pushify-security:
      loadBalancer:
        servers:
          - url: http://app:8000
  middlewares:
{redirect}    pushify-edge-guard:
      chain:
        middlewares:
          - pushify-block-bad-ua@file
          - pushify-require-host@file
          - pushify-edge-total-inflight@file
          - pushify-edge-inflight@file
          - pushify-edge-ratelimit@file
    pushify-block-bad-ua:
      plugin:
        rewritebody:
          lastModified: false
    pushify-require-host:
      headers:
        customRequestHeaders:
          X-Require-Host: "true"
    pushify-edge-total-inflight:
      inFlightReq:
        amount: 150
    pushify-edge-inflight:
      inFlightReq:
        amount: 30
        sourceCriterion:
{ip}
    pushify-edge-ratelimit:
      rateLimit:
        average: 25
        burst: 80
        period: 1s
        sourceCriterion:
{ip}
    pushify-inflight-panel:
      inFlightReq:
        amount: 20
        sourceCriterion:
{ip}
    pushify-inflight-deploy:
      inFlightReq:
        amount: 4
        sourceCriterion:
{ip}
    pushify-inflight-attack:
      inFlightReq:
        amount: 2
        sourceCriterion:
{ip}
    pushify-ratelimit-panel:
      rateLimit:
        average: 30
        burst: 60
        period: 1s
        sourceCriterion:
{ip}
    pushify-ratelimit-standard:
      rateLimit:
        average: 40
        burst: 100
        period: 1s
        sourceCriterion:
{ip}
    pushify-ratelimit-high:
      rateLimit:
        average: 20
        burst: 60
        period: 1s
        sourceCriterion:
{ip}
    pushify-ratelimit-attack:
      rateLimit:
        average: 10
        burst: 30
        period: 1s
        sourceCriterion:
{ip}
    pushify-forward-auth:
      forwardAuth:
        address: http://app:8000/security/forward-auth
        trustForwardHeader: true
        authResponseHeaders:
          - X-Pushify-Verified
        authRequestHeaders:
          - Cookie
          - User-Agent
          - X-Forwarded-Host
          - X-Forwarded-Uri
          - X-Forwarded-For
          - X-Forwarded-Proto
{cf_header}          - Accept
          - Accept-Language
          - Sec-Fetch-Dest
          - Sec-Fetch-Mode
          - Sec-Fetch-Site
    pushify-headers:
      headers:
        browserXssFilter: true
        contentTypeNosniff: true
        frameDeny: true
        referrerPolicy: strict-origin-when-cross-origin
        customResponseHeaders:
          Server: ""
          X-Powered-By: ""
    pushify-noindex:
      headers:
        customResponseHeaders:
          X-Robots-Tag: "noindex, nofollow, noarchive"
"""


def security_middlewares_document(settings: Settings) -> str:
    ep = _entrypoints_yaml(settings)
    body = security_middlewares_yaml(
        behind_cloudflare=settings.behind_cloudflare,
        enable_https=settings.enable_https,
    )
    return f"{ep}\n{body}"


def ensure_security_middlewares_file(traefik_dir: str, settings: Settings | None = None) -> None:
    from config import get_settings

    settings = settings or get_settings()
    path = Path(traefik_dir) / "security-middlewares.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    marker = f"pushify-ratelimit-{SECURITY_MIDDLEWARES_VERSION}"
    want_cf = settings.behind_cloudflare
    want_https = settings.enable_https
    if path.exists():
        content = path.read_text(encoding="utf-8")
        has_cf = "CF-Connecting-IP" in content
        has_edge = "pushify-edge-guard:" in content
        has_redirect = "pushify-redirect-https:" in content
        has_block_ua = "pushify-block-bad-ua:" in content
        if (
            marker in content
            and "pushify-forward-auth:" in content
            and "pushify-challenge-redirect:" not in content
            and has_cf == want_cf
            and has_edge
            and has_redirect == want_https
            and has_block_ua
            and "burst: 80" in content
            and "            depth: 1" in content
        ):
            return
    path.write_text(security_middlewares_document(settings), encoding="utf-8")
