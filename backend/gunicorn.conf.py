"""Gunicorn settings for production (the image's default command uses this file)."""

import os

bind = "0.0.0.0:8100"
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))
threads = int(os.environ.get("GUNICORN_THREADS", "2"))
timeout = 60
graceful_timeout = 30
keepalive = 5
# Recycle workers periodically to contain any slow memory growth.
max_requests = 1000
max_requests_jitter = 100
# Only nginx can reach this port (private Docker network), so trust its X-Forwarded-* headers.
forwarded_allow_ips = "*"

accesslog = "-"
# Path only: query strings can carry search terms (phone/PAN) and are never logged. No referrer/user agent.
access_log_format = '%({x-request-id}i)s %(h)s "%(m)s %(U)s" %(s)s %(B)s %(M)sms'
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
