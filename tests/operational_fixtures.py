"""Read-only geometry source for disposable operational API test databases."""
import sqlite3
from contextlib import closing

from backend.core.data_paths import dataset_path
from backend.repositories import database as db


def copy_saved_geometry(target_path):
    """Copy layouts/slots only; strip live assignments and request references."""
    source_path = dataset_path("database")
    with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True)) as source, db.connect(target_path) as target:
        source.row_factory = sqlite3.Row
        for table in ("pilot_slot_layouts", "pilot_slots"):
            for row in source.execute("SELECT * FROM " + table):
                values = dict(row)
                if table == "pilot_slots":
                    values.update(assigned_vendor_id=None, status="AVAILABLE", pending_request_id=None, active_allocation_id=None)
                columns = list(values)
                target.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", list(values.values()))
