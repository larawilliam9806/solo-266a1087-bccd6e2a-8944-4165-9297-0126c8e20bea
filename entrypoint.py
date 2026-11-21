"""Container entrypoint: fix volume ownership, drop privileges, exec.

A fresh Docker named volume is mounted root-owned even though the image's
/data directory belongs to the app user.  When we start as root we align the
ownership of the data directory (and any existing database files) and then
drop to the unprivileged account before running the server.
"""

from __future__ import annotations

import os
import pwd
import sys

APP_USER = os.environ.get("APP_USER", "appuser")
DATA_DIR = os.environ.get("DATA_DIR", "/data")


def _drop_privileges() -> None:
    try:
        account = pwd.getpwnam(APP_USER)
    except KeyError:
        sys.exit(f"entrypoint: unknown user {APP_USER!r}")
    uid, gid = account.pw_uid, account.pw_gid

    os.makedirs(DATA_DIR, exist_ok=True)
    targets = [DATA_DIR]
    try:
        targets.extend(
            os.path.join(DATA_DIR, name) for name in os.listdir(DATA_DIR)
        )
    except OSError:
        pass
    for path in targets:
        try:
            os.chown(path, uid, gid)
        except OSError as exc:
            print(f"entrypoint: cannot chown {path}: {exc}", file=sys.stderr)

    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    if os.geteuid() == 0 and os.environ.get("DROP_PRIVILEGES", "1") != "0":
        _drop_privileges()
    if len(sys.argv) < 2:
        sys.exit("entrypoint: no command given")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
