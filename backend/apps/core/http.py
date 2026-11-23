from django.conf import settings


def client_ip(request) -> str | None:
    """Client IP, trusting X-Forwarded-For only for the configured number of reverse proxies.

    With TRUSTED_PROXY_COUNT = 1 (nginx), the right-most XFF entry is the one nginx appended and is
    trusted; anything to its left is client-supplied and ignored.
    """
    hops = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    if hops > 0:
        forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
        if len(forwarded) >= hops:
            return forwarded[-hops]
    return request.META.get("REMOTE_ADDR")
