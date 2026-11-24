#!/usr/bin/env python3
import getpass
import sys

from werkzeug.security import generate_password_hash


def main() -> int:
    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    if len(password) < 12:
        print("Use a password with at least 12 characters.", file=sys.stderr)
        return 1
    print("{}\t{}".format(username, generate_password_hash(password, method="scrypt")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
