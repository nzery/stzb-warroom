"""Remove game credentials from the copy of a game frame queued for upload."""

import hashlib
import json
import zlib

from .frames import CLIENT_HEADER, body, message_id

LOGIN = 99991
UPDATE = 90005
# Login variants, queue handshakes and session notices; account verification,
# migration and safety locks; service tickets, signatures and one-time codes.
PRIVATE_BODIES = {
    23, 90008, 90010, 96665, 96666, 98888, 99992, 99994, 99995, 99999,
    189, 331, 332, 696, 699, 830, 2311, 2540, 2541, 2542, 2543, 4929, 4935,
    4987, 5023, 5258, 40014,
    190, 1431, 1436, 2407, 3826, 3829, 3884, 3886, 3928, 4085, 4969, 4970,
    4971, 6073, 6074, 9008, 20011, 40016, 40021,
}
PRIVATE_COLUMNS = {
    "Tb_underage": {"underage_password"},
    "Tb_user_stuff": {"safe_lock_password"},
    "Tb_user_stuff_one": {"migrate_password", "io_server_ticket", "cloud_music_access_token",
                          "cloud_music_refresh_token"},
    "Tb_user_stuff_temp": {"auth_info"},
    "Tb_user_stuff_temp_ex": {"nile_token"},
}
FILTER_SERVER = PRIVATE_BODIES | {LOGIN, UPDATE}


def _snapshot(value, schema):
    schema.clear()
    if isinstance(value[4], str):  # a new role choosing a starting state has no tables
        return False
    snapshot = value[4][0]
    for definition in snapshot[0].split(";"):
        table, *columns = definition.split(",")
        if table in PRIVATE_COLUMNS:
            schema[table] = {i for i, name in enumerate(columns) if name in PRIVATE_COLUMNS[table]}
    changed = False
    for table, rows in snapshot[1:]:
        if table in PRIVATE_COLUMNS:
            for row in rows or ():
                for i in schema[table]:
                    if i < len(row) and row[i] is not None:
                        row[i] = None
                        changed = True
    return changed


def _updates(value, schema):
    kept = []
    changed = False
    for change in value:
        op, table, data = change
        if table in PRIVATE_COLUMNS and op != 3:
            if table not in schema or op not in (1, 2):
                changed = True  # a capture starting mid-session has no column definitions
                continue
            if op == 1:
                for row in data if data and isinstance(data[0], list) else [data]:
                    for i in schema[table]:
                        if i < len(row):
                            row[i] = None
                            changed = True
            else:
                for i in range(2, len(data), 2):
                    if data[i] in schema[table]:
                        data[i + 1] = None
                        changed = True
        kept.append(change)
    value[:] = kept
    return changed


def for_upload(frame, client, schema):
    command = message_id(frame, client)
    if command in PRIVATE_BODIES or (client and command == LOGIN):
        frame = frame[:CLIENT_HEADER if client else 12]
        frame = (len(frame) - 4).to_bytes(4, "big") + frame[4:]
    elif not client and command in (LOGIN, UPDATE):
        try:
            value = body(frame, False)[1]
            changed = _snapshot(value, schema) if command == LOGIN else _updates(value, schema)
            if changed:
                payload = zlib.compress(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode(), 1)
                frame = (len(payload) + 9).to_bytes(4, "big") + frame[4:12] + b"\x03" + payload
        except (ValueError, TypeError, IndexError, KeyError, AttributeError, RecursionError):
            if command == LOGIN:
                schema.clear()
            frame = (8).to_bytes(4, "big") + frame[4:12]
    if client:
        session = frame[12:44]
        digest = hashlib.sha256(b"session:" + session).digest() if session.isalnum() else bytes(32)
        frame = frame[:4] + b"STW-H256" + digest + frame[44:]
    return frame
