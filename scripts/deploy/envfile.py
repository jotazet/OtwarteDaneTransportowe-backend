"""The production .env, written from the GitHub environment and checked on the server.

    python3 -I scripts/deploy/envfile.py write <path>       # the deploy job, on the runner
    python3 -I scripts/deploy/envfile.py check <old> <new>  # release.sh, on the server

`write` turns every variable of the `production` environment (VARS_JSON, the job's
toJSON(vars)) into one line under its own name, then the secrets listed in SECRETS, which
the job passes one by one: toJSON(secrets) would hand the step every secret, the deploy
key included. Variables named DEPLOY_* configure the deploy job itself and stay out of
the file. Values go in double quotes with \\, \" and $ escaped ($$), which Compose reads
back as the exact value through env_file and ${...} alike; single quotes break on a value
that ends in a backslash. A line break is refused.

`check` refuses a new .env that changes or drops FEED_AUTH_ENCRYPTION_KEY or
POSTGRES_PASSWORD: the first makes every stored feed credential undecryptable, the second
does not change the password of an initialized database, so the app can no longer log in.
To change either on purpose, follow docs/deployment.md (section 20).
"""

import json
import os
import sys

# Secrets of the environment; each becomes a .env line under the same name.
REQUIRED_SECRETS = ("DJANGO_SECRET_KEY", "POSTGRES_PASSWORD", "FEED_AUTH_ENCRYPTION_KEY")
OPTIONAL_SECRETS = ("PLANNER_INTERNAL_TOKEN",)
# Changing these in GitHub alone breaks production (see the module docstring).
PINNED = ("FEED_AUTH_ENCRYPTION_KEY", "POSTGRES_PASSWORD")
DEPLOY_PREFIX = "DEPLOY_"


def fail(message):
    print(f"::error::{message}")
    sys.exit(1)


def quote(name, value):
    if "\n" in value or "\r" in value:
        fail(f"{name} holds a line break.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$") + '"'


def render(variables, secrets):
    """The .env text for the environment's variables and secrets."""
    reserved = set(REQUIRED_SECRETS + OPTIONAL_SECRETS)
    lines = []
    for name in sorted(variables):
        if name.startswith(DEPLOY_PREFIX):
            continue
        if name in reserved:
            fail(f"{name} is a variable; it must be a secret.")
        lines.append(f"{name}={quote(name, str(variables[name]))}")
    for name in REQUIRED_SECRETS + OPTIONAL_SECRETS:
        value = secrets.get(name, "")
        if not value:
            if name in REQUIRED_SECRETS:
                fail(f"The production environment has no secret {name}.")
            continue
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
        if value.startswith("'") and value.endswith("'") and len(value) > 1:
            value = value[1:-1]
        elif value.startswith('"') and value.endswith('"') and len(value) > 1:
            out, chars = [], iter(value[1:-1])
            for char in chars:
                out.append(next(chars, "") if char == "\\" else char)
            value = "".join(out).replace("$$", "$")
        else:
            value = value.split(" #", 1)[0].strip().replace("$$", "$")
        values[key] = value
    return values


def write(path):
    variables = json.loads(os.environ.get("VARS_JSON") or "{}")
    text = render(variables, os.environ)
    old_umask = os.umask(0o077)
    try:
        with open(path, "w", encoding="utf-8") as env_file:
            env_file.write(text)
    finally:
        os.umask(old_umask)
    print(f"envfile: {text.count(chr(10))} lines -> {path}")


def check(old_path, new_path):
    if not os.path.exists(old_path):
        return
    with open(old_path, encoding="utf-8") as old_file, open(new_path, encoding="utf-8") as new_file:
        old, new = parse(old_file.read()), parse(new_file.read())
    for name in PINNED:
        if old.get(name) and old[name] != new.get(name):
            fail(f"{name} differs from the server's .env; see docs/deployment.md section 20.")


if __name__ == "__main__":
    if sys.argv[1:2] == ["write"] and len(sys.argv) == 3:
        write(sys.argv[2])
    elif sys.argv[1:2] == ["check"] and len(sys.argv) == 4:
        check(sys.argv[2], sys.argv[3])
    else:
        fail(__doc__.split("\n\n")[1])
