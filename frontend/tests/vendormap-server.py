"""Real API browser review against an isolated disposable database."""
import os
import sys
import tempfile
import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.update(DATABASE_URL='', APP_ENV='development', AUTH_HMAC_SECRET='isolated-vendormap-test-secret', SESSION_COOKIE_SECURE='false', OTP_PROVIDER='development', OTP_DEBUG='false', NMC_ADMIN_USERNAME='review-admin', NMC_ADMIN_PASSWORD='review-only-password', CORS_ORIGINS='http://127.0.0.1:5193', ALLOW_DESTRUCTIVE_DB_RESET='false')
from backend.repositories import database as db
temporary = tempfile.TemporaryDirectory(prefix='vendormap-test-')
db.DATABASE_PATH = Path(temporary.name) / 'review.db'
from backend.application import app
from backend.core import lifecycle
from backend.services import authentication
from backend.core.data_paths import dataset_path
db.initialize_database(db.DATABASE_PATH)
# Copy geometry without operational identities or assignments.
with closing(sqlite3.connect(dataset_path('database').resolve().as_uri()+'?mode=ro', uri=True)) as source, closing(db.connect(db.DATABASE_PATH)) as target:
    source.row_factory = sqlite3.Row
    for table in ['pilot_slot_layouts', 'pilot_slots']:
        for row in source.execute('SELECT * FROM '+table):
            values = dict(row)
            if table == 'pilot_slots':
                values.update(assigned_vendor_id=None, status='AVAILABLE', pending_request_id=None, active_allocation_id=None)
            columns = list(values)
            target.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) ON CONFLICT DO NOTHING", list(values.values()))
    target.commit()
lifecycle.initialize_application = lambda path: db.initialize_database(path)
authentication.generate_otp = lambda: '123456'
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8093, log_level='warning')
