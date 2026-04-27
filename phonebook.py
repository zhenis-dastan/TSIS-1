"""
PhoneBook – Extended Contact Management  (TSIS 1)
=================================================
Features added on top of Practice 7 & 8:
  • Extended schema: phones table, groups table, email, birthday
  • Filter by group / search by email / sort results
  • Paginated console navigation (next / prev / quit)
  • Export contacts to JSON
  • Import contacts from JSON with duplicate handling
  • Stored procedure callers: add_phone, move_to_group
  • Extended search_contacts (name + email + all phones)
  • Extended CSV import (email, birthday, group, phone type)
"""

import csv
import json
import sys
from datetime import date, datetime

import psycopg2
from psycopg2.extras import RealDictCursor

from connect import get_connection

# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _json_serial(obj):
    """JSON serialiser for date / datetime objects."""
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} is not JSON serialisable")


def _print_contact(row: dict, idx: int | None = None):
    prefix = f"[{idx}] " if idx is not None else ""
    phones = row.get("phones") or []
    phone_str = ", ".join(f"{p['phone']} ({p['type']})" for p in phones) if phones else "—"
    print(
        f"{prefix}{row.get('first_name','')} {row.get('last_name','') or ''}"
        f" | {row.get('email') or '—'}"
        f" | bday: {row.get('birthday') or '—'}"
        f" | group: {row.get('group_name') or '—'}"
        f" | phones: {phone_str}"
    )


def _fetch_contact_phones(cur, contact_id: int) -> list[dict]:
    cur.execute(
        "SELECT phone, type FROM phones WHERE contact_id = %s ORDER BY id",
        (contact_id,),
    )
    return [dict(r) for r in cur.fetchall()]


# ──────────────────────────────────────────────────────────────
# 3.1  Schema initialisation
# ──────────────────────────────────────────────────────────────

def init_schema(conn):
    """Apply schema.sql and procedures.sql if they exist alongside this file."""
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    with conn.cursor() as cur:
        for filename in ("schema.sql", "procedures.sql"):
            path = os.path.join(base, filename)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    cur.execute(f.read())
    conn.commit()
    print("Schema and procedures applied.")


# ──────────────────────────────────────────────────────────────
# 3.2  Advanced Console Search & Filter
# ──────────────────────────────────────────────────────────────

def filter_by_group(conn):
    """Show contacts that belong to a chosen group."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, name FROM groups ORDER BY name")
        groups = cur.fetchall()

    if not groups:
        print("No groups found.")
        return

    print("\nAvailable groups:")
    for g in groups:
        print(f"  [{g['id']}] {g['name']}")
    choice = input("Enter group ID (or name): ").strip()

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        if choice.isdigit():
            cur.execute(
                """
                SELECT c.id, c.first_name, c.last_name, c.email, c.birthday,
                       g.name AS group_name
                FROM contacts c
                LEFT JOIN groups g ON g.id = c.group_id
                WHERE c.group_id = %s
                ORDER BY c.first_name
                """,
                (int(choice),),
            )
        else:
            cur.execute(
                """
                SELECT c.id, c.first_name, c.last_name, c.email, c.birthday,
                       g.name AS group_name
                FROM contacts c
                LEFT JOIN groups g ON g.id = c.group_id
                WHERE LOWER(g.name) = LOWER(%s)
                ORDER BY c.first_name
                """,
                (choice,),
            )
        rows = cur.fetchall()

    if not rows:
        print("No contacts found for that group.")
        return

    print(f"\n{len(rows)} contact(s) found:")
    for i, row in enumerate(rows, 1):
        row["phones"] = _fetch_contact_phones(cur if False else conn.cursor(cursor_factory=RealDictCursor).__enter__(), row["id"])
        # re-fetch phones cleanly
        with conn.cursor(cursor_factory=RealDictCursor) as ph_cur:
            row["phones"] = _fetch_contact_phones(ph_cur, row["id"])
        _print_contact(row, i)


def search_by_email(conn):
    """Partial-match search on the email column."""
    query = input("Enter email fragment to search: ").strip()
    if not query:
        print("Empty query.")
        return

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT c.id, c.first_name, c.last_name, c.email, c.birthday,
                   g.name AS group_name
            FROM contacts c
            LEFT JOIN groups g ON g.id = c.group_id
            WHERE c.email ILIKE %s
            ORDER BY c.first_name
            """,
            (f"%{query}%",),
        )
        rows = cur.fetchall()

    if not rows:
        print("No contacts matched.")
        return

    print(f"\n{len(rows)} contact(s) found:")
    for i, row in enumerate(rows, 1):
        with conn.cursor(cursor_factory=RealDictCursor) as ph_cur:
            row["phones"] = _fetch_contact_phones(ph_cur, row["id"])
        _print_contact(row, i)


def list_sorted(conn):
    """List all contacts sorted by name, birthday, or date added."""
    print("\nSort by:  1) Name  2) Birthday  3) Date Added")
    choice = input("Choice: ").strip()
    order_col = {
        "1": "c.first_name, c.last_name",
        "2": "c.birthday NULLS LAST",
        "3": "c.created_at",
    }.get(choice, "c.first_name")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT c.id, c.first_name, c.last_name, c.email, c.birthday,
                   g.name AS group_name, c.created_at
            FROM contacts c
            LEFT JOIN groups g ON g.id = c.group_id
            ORDER BY {order_col}
            """
        )
        rows = cur.fetchall()

    if not rows:
        print("No contacts found.")
        return

    for i, row in enumerate(rows, 1):
        with conn.cursor(cursor_factory=RealDictCursor) as ph_cur:
            row["phones"] = _fetch_contact_phones(ph_cur, row["id"])
        _print_contact(row, i)


# ──────────────────────────────────────────────────────────────
# Paginated navigation (uses the DB function from Practice 8)
# ──────────────────────────────────────────────────────────────

def _get_page(conn, page: int, page_size: int) -> list[dict]:
    """
    Call the paginated query function from Practice 8.
    Falls back to a plain LIMIT/OFFSET if the function does not exist.
    """
    offset = (page - 1) * page_size
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        try:
            # Practice 8 function signature (adjust name if yours differs):
            cur.execute("SELECT * FROM get_contacts_paginated(%s, %s)", (page_size, offset))
        except psycopg2.errors.UndefinedFunction:
            conn.rollback()
            cur.execute(
                """
                SELECT c.id, c.first_name, c.last_name, c.email, c.birthday,
                       g.name AS group_name
                FROM contacts c
                LEFT JOIN groups g ON g.id = c.group_id
                ORDER BY c.first_name
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def paginated_navigation(conn):
    """Console loop: navigate contacts page by page."""
    page_size = 5
    page = 1
    while True:
        rows = _get_page(conn, page, page_size)
        if not rows and page == 1:
            print("No contacts in the database.")
            return
        if not rows:
            print("No more contacts.")
            page -= 1
            continue

        print(f"\n── Page {page} ──────────────────────────────────")
        for i, row in enumerate(rows, 1):
            with conn.cursor(cursor_factory=RealDictCursor) as ph_cur:
                row["phones"] = _fetch_contact_phones(ph_cur, row["id"])
            _print_contact(row, (page - 1) * page_size + i)

        cmd = input("\n[n]ext  [p]rev  [q]uit › ").strip().lower()
        if cmd in ("n", "next"):
            page += 1
        elif cmd in ("p", "prev"):
            if page > 1:
                page -= 1
            else:
                print("Already on the first page.")
        elif cmd in ("q", "quit"):
            break


# ──────────────────────────────────────────────────────────────
# 3.3  Import / Export
# ──────────────────────────────────────────────────────────────

def export_to_json(conn, filepath: str = "contacts_export.json"):
    """Export all contacts (with phones and group) to a JSON file."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT c.id, c.first_name, c.last_name, c.email,
                   c.birthday, c.created_at, g.name AS group_name
            FROM contacts c
            LEFT JOIN groups g ON g.id = c.group_id
            ORDER BY c.id
            """
        )
        contacts = [dict(r) for r in cur.fetchall()]

        for contact in contacts:
            cur.execute(
                "SELECT phone, type FROM phones WHERE contact_id = %s ORDER BY id",
                (contact["id"],),
            )
            contact["phones"] = [dict(p) for p in cur.fetchall()]
            del contact["id"]  # don't export internal PK

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(contacts, f, ensure_ascii=False, indent=2, default=_json_serial)

    print(f"Exported {len(contacts)} contact(s) to '{filepath}'.")


def import_from_json(conn, filepath: str = "contacts_export.json"):
    """
    Import contacts from a JSON file.
    On duplicate first_name: ask the user — skip or overwrite.
    """
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            contacts = json.load(f)
    except FileNotFoundError:
        print(f"File '{filepath}' not found.")
        return
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        return

    inserted = skipped = overwritten = 0

    for contact in contacts:
        first_name = contact.get("first_name", "").strip()
        if not first_name:
            print("  Skipping record with no first_name.")
            skipped += 1
            continue

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM contacts WHERE LOWER(first_name) = LOWER(%s) LIMIT 1",
                (first_name,),
            )
            existing = cur.fetchone()

        if existing:
            action = input(
                f"  Duplicate: '{first_name}' already exists. "
                "[s]kip / [o]verwrite? › "
            ).strip().lower()
            if action not in ("o", "overwrite"):
                skipped += 1
                continue
            # Delete old record (cascade removes phones)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM contacts WHERE id = %s", (existing[0],))
            conn.commit()
            overwritten += 1

        _insert_contact_from_dict(conn, contact)
        inserted += 1

    print(f"JSON import done — inserted: {inserted}, overwritten: {overwritten}, skipped: {skipped}.")


def _resolve_or_create_group(conn, group_name: str) -> int | None:
    """Return the group id for group_name, creating it if necessary."""
    if not group_name:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM groups WHERE LOWER(name) = LOWER(%s)", (group_name,))
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute("INSERT INTO groups (name) VALUES (%s) RETURNING id", (group_name,))
        gid = cur.fetchone()[0]
    conn.commit()
    return gid


def _insert_contact_from_dict(conn, data: dict):
    """Insert a single contact (plus phones) from a dict."""
    group_id = _resolve_or_create_group(conn, data.get("group_name") or data.get("group"))

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contacts (first_name, last_name, email, birthday, group_id)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                data.get("first_name"),
                data.get("last_name") or None,
                data.get("email") or None,
                data.get("birthday") or None,
                group_id,
            ),
        )
        contact_id = cur.fetchone()[0]

        for phone in data.get("phones", []):
            cur.execute(
                "INSERT INTO phones (contact_id, phone, type) VALUES (%s, %s, %s)",
                (contact_id, phone.get("phone"), phone.get("type", "mobile")),
            )

    conn.commit()


# ──────────────────────────────────────────────────────────────
# Extended CSV import
# ──────────────────────────────────────────────────────────────

def import_from_csv(conn, filepath: str = "contacts.csv"):
    """
    Import contacts from CSV.
    Expected columns: first_name, last_name, email, birthday, group, phone, phone_type
    """
    try:
        with open(filepath, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except FileNotFoundError:
        print(f"File '{filepath}' not found.")
        return

    inserted = skipped = 0
    for row in rows:
        first_name = row.get("first_name", "").strip()
        if not first_name:
            skipped += 1
            continue

        phone_val = row.get("phone", "").strip()
        phones = []
        if phone_val:
            phones = [{"phone": phone_val, "type": row.get("phone_type", "mobile").strip()}]

        data = {
            "first_name": first_name,
            "last_name": row.get("last_name", "").strip() or None,
            "email": row.get("email", "").strip() or None,
            "birthday": row.get("birthday", "").strip() or None,
            "group_name": row.get("group", "").strip() or None,
            "phones": phones,
        }
        _insert_contact_from_dict(conn, data)
        inserted += 1

    print(f"CSV import done — inserted: {inserted}, skipped: {skipped}.")


# ──────────────────────────────────────────────────────────────
# 3.4  Stored Procedure callers
# ──────────────────────────────────────────────────────────────

def call_add_phone(conn):
    """Console wrapper for the add_phone stored procedure."""
    name  = input("Contact first name: ").strip()
    phone = input("Phone number: ").strip()
    ptype = input("Phone type (home / work / mobile) [mobile]: ").strip() or "mobile"
    try:
        with conn.cursor() as cur:
            cur.execute("CALL add_phone(%s, %s, %s)", (name, phone, ptype))
        conn.commit()
        print("Phone added successfully.")
    except psycopg2.Error as e:
        conn.rollback()
        print(f"Error: {e.pgerror or e}")


def call_move_to_group(conn):
    """Console wrapper for the move_to_group stored procedure."""
    name  = input("Contact first name: ").strip()
    group = input("Target group name: ").strip()
    try:
        with conn.cursor() as cur:
            cur.execute("CALL move_to_group(%s, %s)", (name, group))
        conn.commit()
        print("Contact moved successfully.")
    except psycopg2.Error as e:
        conn.rollback()
        print(f"Error: {e.pgerror or e}")


def call_search_contacts(conn):
    """Console wrapper for the search_contacts DB function."""
    query = input("Search query (name / email / phone): ").strip()
    if not query:
        print("Empty query.")
        return
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM search_contacts(%s)", (query,))
            rows = cur.fetchall()
    except psycopg2.Error as e:
        conn.rollback()
        print(f"Error: {e.pgerror or e}")
        return

    if not rows:
        print("No results found.")
        return

    print(f"\n{len(rows)} result(s):")
    for i, row in enumerate(rows, 1):
        phone_str = f"{row['phone']} ({row['phone_type']})" if row.get("phone") else "—"
        print(
            f"[{i}] {row['first_name']} {row['last_name'] or ''}"
            f" | email: {row['email'] or '—'}"
            f" | bday: {row['birthday'] or '—'}"
            f" | group: {row['group_name'] or '—'}"
            f" | phone: {phone_str}"
        )


# ──────────────────────────────────────────────────────────────
# Main menu
# ──────────────────────────────────────────────────────────────

MENU = """
╔══════════════════════════════════════════╗
║        PhoneBook  –  TSIS 1 Menu         ║
╠══════════════════════════════════════════╣
║  1. Filter contacts by group             ║
║  2. Search contacts by email             ║
║  3. List contacts (sorted)               ║
║  4. Browse contacts (page navigation)    ║
╠══════════════════════════════════════════╣
║  5. Export contacts to JSON              ║
║  6. Import contacts from JSON            ║
║  7. Import contacts from CSV             ║
╠══════════════════════════════════════════╣
║  8. Add phone to a contact  (procedure)  ║
║  9. Move contact to group   (procedure)  ║
║ 10. Search contacts (all fields + DB fn) ║
╠══════════════════════════════════════════╣
║  0. Apply schema & procedures            ║
║  q. Quit                                 ║
╚══════════════════════════════════════════╝
"""


def main():
    try:
        conn = get_connection()
    except psycopg2.OperationalError as e:
        print(f"Cannot connect to database: {e}")
        sys.exit(1)

    print("Connected to PostgreSQL.")

    while True:
        print(MENU)
        choice = input("Choose an option › ").strip().lower()

        if choice == "0":
            init_schema(conn)
        elif choice == "1":
            filter_by_group(conn)
        elif choice == "2":
            search_by_email(conn)
        elif choice == "3":
            list_sorted(conn)
        elif choice == "4":
            paginated_navigation(conn)
        elif choice == "5":
            path = input("Output file [contacts_export.json]: ").strip() or "contacts_export.json"
            export_to_json(conn, path)
        elif choice == "6":
            path = input("Input file [contacts_export.json]: ").strip() or "contacts_export.json"
            import_from_json(conn, path)
        elif choice == "7":
            path = input("CSV file [contacts.csv]: ").strip() or "contacts.csv"
            import_from_csv(conn, path)
        elif choice == "8":
            call_add_phone(conn)
        elif choice == "9":
            call_move_to_group(conn)
        elif choice == "10":
            call_search_contacts(conn)
        elif choice in ("q", "quit", "exit"):
            print("Goodbye!")
            break
        else:
            print("Unknown option, try again.")

    conn.close()


if __name__ == "__main__":
    main()
