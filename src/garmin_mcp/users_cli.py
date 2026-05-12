"""CLI for managing Garmin MCP users stored in the volume database.

Usage:
  garmin-mcp-users           # interactive add
  garmin-mcp-users add       # interactive add
  garmin-mcp-users list      # show all users
  garmin-mcp-users delete    # interactive delete
"""

import os
import sys


def _hr():
    print("-" * 60)


def cmd_add():
    from garmin_mcp.users_db import add_user, generate_key
    import sqlite3

    print("\n" + "=" * 60)
    print("  Add a Garmin MCP User")
    print("=" * 60)

    print("\nAPI key — press Enter to generate one automatically.")
    key = input("  Key: ").strip()
    if not key:
        key = generate_key()
        print(f"  Generated: {key}")

    email = input("\nGarmin account email: ").strip().lower()
    if not email:
        print("Error: email is required.")
        sys.exit(1)

    name = input("Display name (e.g. Jason): ").strip()
    if not name:
        name = email.split("@")[0].capitalize()

    tm = input("Enable training memory? [y/N]: ").strip().lower()
    training_memory = tm in ("y", "yes")

    try:
        add_user(key, name, email, training_memory)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)

    server_url = os.getenv("MCP_SERVER_URL", "").rstrip("/")

    print()
    _hr()
    print(f"  ✓ User added")
    print(f"  Name:            {name}")
    print(f"  Email:           {email}")
    print(f"  Training memory: {'yes' if training_memory else 'no'}")
    print(f"  API key:         {key}")
    if server_url:
        print(f"\n  Connect URL:")
        print(f"  {server_url}/connect?key={key}")
    _hr()
    print()


def cmd_list():
    from garmin_mcp.users_db import list_users

    users = list_users()
    if not users:
        print("\nNo users in database.\n")
        return

    print()
    _hr()
    header = f"  {'Name':<18} {'Email':<32} {'TM':<4} {'Connected'}"
    print(header)
    _hr()
    for u in users:
        tm = "✓" if u["training_memory"] else ""
        connected = (u.get("connected_at") or "never")[:19]
        print(f"  {u['name']:<18} {u['email']:<32} {tm:<4} {connected}")
        print(f"  {'Key:':<5} {u['key']}")
        print()
    _hr()
    print()


def cmd_delete():
    from garmin_mcp.users_db import get_user_by_key, delete_user

    print("\nDelete a user")
    _hr()
    key = input("API key: ").strip()
    if not key:
        print("Cancelled.")
        return

    user = get_user_by_key(key)
    if not user:
        print(f"No user found with that key.")
        sys.exit(1)

    print(f"\n  Name:  {user['name']}")
    print(f"  Email: {user['email']}")
    confirm = input("\nDelete this user? [y/N]: ").strip().lower()
    if confirm in ("y", "yes"):
        delete_user(key)
        print("✓ User deleted.\n")
    else:
        print("Cancelled.\n")


def main():
    from garmin_mcp.users_db import init_db

    init_db()

    command = sys.argv[1] if len(sys.argv) > 1 else "add"

    if command == "add":
        cmd_add()
    elif command == "list":
        cmd_list()
    elif command == "delete":
        cmd_delete()
    else:
        print(f"Unknown command: {command}")
        print("Usage: garmin-mcp-users [add|list|delete]")
        sys.exit(1)


if __name__ == "__main__":
    main()
