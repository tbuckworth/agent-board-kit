"""Inner half of creds.run_as. Takes no arguments (see creds.py).

1. read the command, the allowlist of ordinary variable names, the remap table, the names the
   provider needs and (env-file backend) the account's env file from CW_CHILD
2. build a fresh environment: allowlisted names + this account's keys, nothing else
3. exec the command

Secrets come from the env file if one is named, else from this process's environment (what `bws run`
injected, or what the parent passed for the environ backend). With a remap table only the mapped
names are taken; without one (env-file only) every key in the file is.
"""
import base64
import json
import os
import sys


def read_env_file(path):
    """KEY=value lines; blank lines, `# comments` and a leading `export ` are allowed; quotes are stripped."""
    out = {}
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[key] = value
    return out


def build_env(payload, environ):
    """(env, missing names)."""
    env = {k: environ[k] for k in payload["keep"] if k in environ}
    src = read_env_file(payload["env_file"]) if payload.get("env_file") else environ
    where = payload.get("env_file") or "the secret store"
    missing = []
    if payload.get("remap"):
        for want, stored in payload["remap"].items():
            if stored in src:
                env[want] = src[stored]
            else:
                missing.append(stored)
    elif payload.get("env_file"):
        env.update(src)
    missing += [n for n in payload.get("need") or [] if n not in env and n not in (payload.get("remap") or {})]
    return env, ["%s (in %s)" % (m, where) for m in missing]


def main():
    blob = os.environ.get("CW_CHILD")
    if not blob:
        sys.exit("childexec: no CW_CHILD payload")
    payload = json.loads(base64.b64decode(blob).decode())
    try:
        env, missing = build_env(payload, os.environ)
    except OSError as e:
        sys.exit("childexec: cannot read %s: %s" % (payload.get("env_file"), e.strerror))
    if missing:
        sys.exit("childexec: no secret named %s" % ", ".join(missing))
    argv = payload["argv"]
    os.execvpe(argv[0], argv, env)


if __name__ == "__main__":
    main()
