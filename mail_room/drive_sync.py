"""Fetch file metadata from Google Drive and persist to SQLite."""

import datetime
import sys
from typing import Any, Dict, List, Optional

from mail_room import db
from mail_room.api import execute_with_retry

DEFAULT_QUERY = "'me' in owners and trashed = false"
LIST_PAGE_SIZE = 1000
LIST_FIELDS = (
    "nextPageToken, files(id,name,mimeType,size,modifiedTime,"
    "parents,owners(emailAddress),webViewLink,trashed)"
)


def _utc_now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _file_to_row(file_resource, fetched_at):
    # type: (Dict[str, Any], str) -> Optional[tuple]
    file_id = file_resource.get("id") or ""
    if not file_id:
        return None
    size = file_resource.get("size")
    if size is not None:
        size = int(size)
    parents = file_resource.get("parents") or []
    owners = file_resource.get("owners") or []
    owner_email = None
    if owners:
        owner_email = owners[0].get("emailAddress")
    return (
        file_id,
        file_resource.get("name") or "",
        file_resource.get("mimeType") or "",
        size,
        file_resource.get("modifiedTime"),
        ",".join(parents) if parents else None,
        owner_email,
        file_resource.get("webViewLink"),
        1 if file_resource.get("trashed") else 0,
        fetched_at,
    )


def run_drive_sync(
    service,  # type: Any
    conn,  # type: Any
    query,  # type: str
    max_files,  # type: Optional[int]
    verbose,  # type: bool
    incremental=True,  # type: bool
):
    # type: (...) -> int
    """List Drive files and store metadata. Returns number of rows written this run.

    Default is incremental: existing rows are kept and only missing IDs are stored.
    Pass incremental=False to delete stored Drive files and rebuild.
    Incremental sync does not refresh size of files already in the DB.
    """
    annotations = {}  # type: Dict[str, Any]
    if incremental:
        prev_query = db.get_sync_meta(conn, "drive_last_query")
        if prev_query is not None and prev_query != query and verbose:
            print(
                "Note: --query differs from last Drive sync (%r). "
                "The database may mix results from multiple queries; "
                "pass --overwrite to delete stored Drive files and rebuild."
                % (prev_query,),
                file=sys.stderr,
            )
    else:
        # Preserve user-authored review state and notes while rebuilding API metadata.
        annotations = db.drive_file_annotations(conn)
        db.clear_drive_files(conn)

    total_stored = 0
    page_token = None  # type: Optional[str]
    fetched_at = _utc_now_iso()

    while True:
        kwargs = {
            "q": query,
            "pageSize": LIST_PAGE_SIZE,
            "fields": LIST_FIELDS,
            "pageToken": page_token,
        }
        req = service.files().list(**kwargs)
        resp = execute_with_retry(req, verbose=verbose)
        files = resp.get("files") or []
        page_token = resp.get("nextPageToken")

        if incremental:
            ids = [f.get("id") for f in files if f.get("id")]
            have = db.drive_ids_present(conn, ids)
            to_store = [f for f in files if f.get("id") and f.get("id") not in have]
        else:
            to_store = files

        if max_files is not None:
            remaining = max_files - total_stored
            if remaining <= 0:
                break
            if len(to_store) > remaining:
                to_store = to_store[:remaining]

        rows = []
        for f in to_store:
            row = _file_to_row(f, fetched_at)
            if row:
                reviewed_at, comment, hidden_at = annotations.get(row[0], (None, None, None))
                row = row + (reviewed_at, comment, hidden_at)
                rows.append(row)
        if rows:
            db.insert_drive_files(conn, rows)
            conn.commit()
            total_stored += len(rows)
            if verbose:
                print(
                    "Stored %s Drive files (written this run: %s)..."
                    % (len(rows), total_stored),
                    file=sys.stderr,
                )

        if not page_token:
            break
        if max_files is not None and total_stored >= max_files:
            break

    db.set_sync_meta(conn, "drive_last_query", query)
    db.set_sync_meta(conn, "drive_last_sync_at", fetched_at)
    return total_stored
