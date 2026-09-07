"""
CSV-backed login.

Users live in one file, users.csv, with the columns

    username,password_hash,display_name,role

password_hash is PBKDF2-HMAC-SHA256 in the portable form

    pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>

Passwords are NEVER stored in the clear, and the hash is compared with
hmac.compare_digest so a wrong guess costs the same time as a right one.
Everything here is stdlib - no password library to keep up to date.

SCOPE: this is a small internal tool's login. It gates access to the verifier
for a known list of colleagues; it is not hardened for the open internet (no
rate limiting, no lockout, no password rotation, no MFA). Put it behind a VPN
or a reverse proxy that does those things before exposing it.

Manage users from the command line:

    python -m api.auth add  alice --name "Alice Ng" --role engineer
    python -m api.auth list
    python -m api.auth passwd alice
    python -m api.auth remove alice
"""

import csv
import hashlib
import hmac
import os
import secrets

FIELDNAMES = ["username", "password_hash", "display_name", "role"]
ITERATIONS = 240_000
DEFAULT_USERS_CSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "users.csv")

# Compared against when a username does not exist, so a bad username and a bad
# password take the same time and cannot be told apart by timing.
_DUMMY_HASH = None


def hash_password(password, *, iterations=ITERATIONS, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    """Constant-time check of a password against a stored hash string."""
    try:
        algorithm, iterations, salt_hex, hash_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def load_users(path=DEFAULT_USERS_CSV):
    """{username_lower: row}. Missing file -> no users (the API says so plainly)."""
    if not os.path.exists(path):
        return {}
    users = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("username") or "").strip()
            if name:
                users[name.lower()] = {k: (row.get(k) or "").strip()
                                       for k in FIELDNAMES}
    return users


def save_users(users, path=DEFAULT_USERS_CSV):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        w.writeheader()
        for name in sorted(users):
            w.writerow(users[name])


def authenticate(username, password, path=DEFAULT_USERS_CSV):
    """Return the user row on success, None on failure. Never says which of the
    username or the password was wrong."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("_")
    user = load_users(path).get((username or "").strip().lower())
    if user is None:
        verify_password(password or "", _DUMMY_HASH)  # equalise timing
        return None
    if not verify_password(password or "", user["password_hash"]):
        return None
    return user


def _cli(argv=None):
    import argparse
    import getpass

    p = argparse.ArgumentParser(description="Manage users.csv for the BMS verifier.")
    p.add_argument("--file", default=DEFAULT_USERS_CSV)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a user (prompts for the password)")
    a.add_argument("username")
    a.add_argument("--name", default="")
    a.add_argument("--role", default="engineer")

    sub.add_parser("list", help="list users")
    pw = sub.add_parser("passwd", help="change a password")
    pw.add_argument("username")
    rm = sub.add_parser("remove", help="delete a user")
    rm.add_argument("username")

    args = p.parse_args(argv)
    users = load_users(args.file)

    if args.cmd == "list":
        if not users:
            print(f"No users in {args.file}")
        for name, row in sorted(users.items()):
            print(f"{row['username']:20} {row['role']:12} {row['display_name']}")
        return 0

    if args.cmd == "add":
        key = args.username.strip().lower()
        if key in users:
            print(f"{args.username} already exists - use 'passwd' to change it.")
            return 1
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.")
            return 1
        users[key] = {"username": args.username.strip(),
                      "password_hash": hash_password(password),
                      "display_name": args.name or args.username,
                      "role": args.role}
        save_users(users, args.file)
        print(f"Added {args.username} -> {args.file}")
        return 0

    key = args.username.strip().lower()
    if key not in users:
        print(f"No such user: {args.username}")
        return 1

    if args.cmd == "passwd":
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match.")
            return 1
        users[key]["password_hash"] = hash_password(password)
        save_users(users, args.file)
        print(f"Password updated for {args.username}")
        return 0

    del users[key]
    save_users(users, args.file)
    print(f"Removed {args.username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
