"""The production .env, written from the GitHub environment and checked on the server.

    python3 -I scripts/deploy/envfile.py write <path>                  # the deploy job, on the runner
    python3 -I scripts/deploy/envfile.py merge <release> <host> <out>  # odt-deploy, on the server
    python3 -I scripts/deploy/envfile.py check <old> <new>             # release.sh, on the server

`write` turns every variable of the `production` environment (VARS_JSON, the job's
toJSON(vars)) into one line under its own name, then the secrets listed in SECRETS, which
the job passes one by one: toJSON(secrets) would hand the step every secret, GitHub's
token included. Variables named DEPLOY_* configure the deploy job itself and stay out of
the file. Values go in double quotes with \\, \" and $ escaped ($$), which Compose reads
back as the exact value through env_file and ${...} alike; single quotes break on a value
that ends in a backslash. A line break is refused.

`merge` builds the .env a release installs: the keys of the release's .env (standard input
of odt-deploy), which must all be in RELEASE_KEYS, then every key of the server's host file
(/etc/odt/backend-host.env, written by root), which wins. A release .env with any other key
is refused, not trimmed. The host file holds what decides what and how containers run on
the host: user ids, host paths, images, container names, Compose settings and every value
Compose interpolates into docker-compose*.yml, resource limits, service addresses and
DJANGO_SETTINGS_MODULE.

`check` refuses a new .env that changes or drops FEED_AUTH_ENCRYPTION_KEY or
POSTGRES_PASSWORD: the first makes every stored feed credential undecryptable, the second
does not change the password of an initialized database, so the app can no longer log in.
A new FEED_AUTH_ENCRYPTION_KEY passes when the previous one is in FEED_AUTH_ENCRYPTION_OLD_KEYS,
which still decrypts: a key rotation. To change either on purpose, follow docs/deployment.md
(section 20).
"""

import json
import os
import re
import sys

# Secrets of the environment; each becomes a .env line under the same name. The deploy
# job in .github/workflows/ci.yml passes each one by name: change both together.
REQUIRED_SECRETS = (
    "DJANGO_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "FEED_AUTH_ENCRYPTION_KEY",
    "REDIS_PASSWORD",
)
OPTIONAL_SECRETS = ("PLANNER_INTERNAL_TOKEN", "FEED_AUTH_ENCRYPTION_OLD_KEYS")
SECRETS = REQUIRED_SECRETS + OPTIONAL_SECRETS
# Without these the containers fall back to the development values in .env.example (also
# an env_file) and the release still looks healthy, so a missing one stops it.
REQUIRED_VARIABLES = (
    "POSTGRES_DB",
    "POSTGRES_USER",
    "DJANGO_ALLOWED_HOSTS",
    "CSRF_TRUSTED_ORIGINS",
    "CORS_ALLOWED_ORIGINS",
    "TRUSTED_PROXY_CIDRS",
)
# What a release may set besides the secrets: application settings read by Django that do
# not change what or how containers run on the host. Anything else goes in the host file.
RELEASE_VARIABLES = REQUIRED_VARIABLES + (
    "ALLOW_UNTRUSTED_PROXY_IPS",
    "CORS_ALLOWED_ORIGIN_REGEXES",
    "CORS_URLS_REGEX",
    "DJANGO_SECURE_SSL_REDIRECT",
    "DJANGO_SECURE_HSTS_SECONDS",
    "DJANGO_SECURE_REFERRER_POLICY",
    "DJANGO_SECURE_COOP",
    "DJANGO_LOG_LEVEL",
    "PUBLIC_BASE_URL",
    "POSTGRES_CONN_MAX_AGE",
    "JWT_ACCESS_MINUTES",
    "JWT_REFRESH_DAYS",
    "THROTTLE_LOGIN",
    "THROTTLE_FEED_DOWNLOAD",
    "THROTTLE_REACTION",
    "FILE_UPLOAD_MAX_MEMORY_SIZE",
    "DATA_UPLOAD_MAX_MEMORY_SIZE",
    "MAX_IMAGE_FILE_SIZE_BYTES",
    "MAX_FEED_FILE_SIZE_BYTES",
    "MAX_REALTIME_FEED_BYTES",
    "FEED_FETCH_TOTAL_TIMEOUT_SECONDS",
    "REALTIME_FETCH_TOTAL_TIMEOUT_SECONDS",
    "CELERY_WORKER_MAX_TASKS_PER_CHILD",
    "CELERY_WORKER_MAX_MEMORY_PER_CHILD",
    "CELERY_WORKER_PREFETCH_MULTIPLIER",
    "PLANNER_OSM_MAX_BYTES",
    "PLANNER_REQUEST_LOG_RETENTION_DAYS",
)
RELEASE_KEYS = frozenset(RELEASE_VARIABLES + SECRETS)
# Without these in the host file the containers would run with the development settings
# of .env.example and Compose's default user ids.
REQUIRED_HOST_KEYS = ("DJANGO_SETTINGS_MODULE", "UID", "GID")
# Changing these in GitHub alone breaks production (see the module docstring).
PINNED = ("FEED_AUTH_ENCRYPTION_KEY", "POSTGRES_PASSWORD")
DEPLOY_PREFIX = "DEPLOY_"
# docker-compose.yml puts REDIS_PASSWORD into redis:// URLs unencoded.
URL_SAFE = re.compile(r"[A-Za-z0-9_-]+")


def fail(message):
    print(f"::error::{message}")
    sys.exit(1)


def quote(name, value):
    if "\n" in value or "\r" in value:
        fail(f"{name} holds a line break.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def render(variables, secrets):
    """The .env text for the environment's variables and secrets."""
    for name in REQUIRED_VARIABLES:
        if not variables.get(name):
            fail(f"The production environment has no variable {name}.")
    lines = []
    for name in sorted(variables):
        if name.startswith(DEPLOY_PREFIX):
            continue
        if name in SECRETS:
            fail(f"{name} is a variable; it must be a secret.")
        lines.append(f"{name}={quote(name, str(variables[name]))}")
    for name in SECRETS:
        value = secrets.get(name, "")
        if not value:
            if name in REQUIRED_SECRETS:
                fail(f"The production environment has no secret {name}.")
            continue
        if name == "REDIS_PASSWORD" and not URL_SAFE.fullmatch(value):
            fail(f"{name} may hold only letters, digits, - and _.")
        lines.append(f"{name}={quote(name, value)}")
    return "\n".join(lines) + "\n"


def parse(text):
    """Key -> value of a .env, as Compose reads the forms used here and by hand."""
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value.startswith("'") and "'" in value[1:]:
            value = value[1 : value.index("'", 1)]
        elif value.startswith('"'):
            # Up to the closing quote; a comment may follow it.
            out, chars = [], iter(value[1:])
            for char in chars:
                if char == '"':
                    break
                out.append(next(chars, "") if char == "\\" else char)
            value = "".join(out).replace("$$", "$")
        else:
            value = value.split(" #", 1)[0].strip().replace("$$", "$")
        values[key] = value
    return values


def load(path):
    with open(path, encoding="utf-8") as env_file:
        return parse(env_file.read())


def save(path, text):
    """Writes text to path, readable by its owner only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w", encoding="utf-8") as env_file:
        env_file.write(text)


def write(path):
    variables = json.loads(os.environ.get("VARS_JSON") or "{}")
    text = render(variables, os.environ)
    save(path, text)
    print(f"envfile: {text.count(chr(10))} lines -> {path}")


def merge(release_path, host_path, out_path):
    if not os.path.isfile(host_path):
        fail(f"No host file {host_path}; see docs/deployment.md section 20.")
    release, host = load(release_path), load(host_path)
    refused = sorted(set(release) - RELEASE_KEYS)
    if refused:
        fail(
            f"A release may not set {', '.join(refused)}; set it in {host_path} on the server"
            " (docs/deployment.md section 20)."
        )
    for name in REQUIRED_HOST_KEYS:
        if not host.get(name):
            fail(f"{host_path} has no {name}.")
    # Rendered again from the parsed values: only the keys checked above reach the file.
    text = "".join(f"{name}={quote(name, value)}\n" for name, value in {**release, **host}.items())
    save(out_path, text)


def check(old_path, new_path):
    if not os.path.exists(old_path):
        return
    old, new = load(old_path), load(new_path)
    old_keys = [key.strip() for key in new.get("FEED_AUTH_ENCRYPTION_OLD_KEYS", "").split(",")]
    for name in PINNED:
        if old.get(name) and old[name] != new.get(name):
            if name == "FEED_AUTH_ENCRYPTION_KEY" and old[name] in old_keys:
                continue  # a rotation: the previous key still decrypts
            fail(f"{name} differs from the server's .env; see docs/deployment.md section 20.")


if __name__ == "__main__":
    if sys.argv[1:2] == ["write"] and len(sys.argv) == 3:
        write(sys.argv[2])
    elif sys.argv[1:2] == ["merge"] and len(sys.argv) == 5:
        merge(*sys.argv[2:])
    elif sys.argv[1:2] == ["check"] and len(sys.argv) == 4:
        check(sys.argv[2], sys.argv[3])
    else:
        fail(__doc__.split("\n\n")[1])
