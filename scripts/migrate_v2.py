"""
One-time migration: add lat/lon to Station, scheduling fields to TrainStatus,
then backfill coordinates from the existing lookup system.

NOTE: [OFFLINE-ONLY SCRIPT]
This script is intended exclusively for offline manual database maintenance.
The hardcoded migrations list uses static, trusted table/column names only.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from extensions import db
from routes.admin import _lookup_station_coords

app = create_app()

with app.app_context():
    # ── 1. Schema changes ─────────────────────────────────────────
    with db.engine.connect() as conn:
        migrations = [
            ("Station",     "latitude",               "FLOAT"),
            ("Station",     "longitude",              "FLOAT"),
            ("TrainStatus", "journey_start_datetime", "DATETIME"),
            ("TrainStatus", "journey_direction",      "VARCHAR(10) DEFAULT 'idle'"),
        ]
        for table, col, typ in migrations:
            try:
                conn.execute(db.text(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {typ}"))
                print(f"  + {table}.{col}")
            except Exception as e:
                print(f"  ~ {table}.{col}: {e}")
        conn.commit()

    print("\nSchema migration done. Backfilling station coordinates...")

    # ── 2. Backfill coordinates ───────────────────────────────────
    from models import Station
    stations = Station.query.all()
    updated = 0
    for s in stations:
        if s.latitude and s.longitude:
            continue  # already cached
        if s.station_name == 'Unassigned / Temporary Pool':
            continue
        lat, lon = _lookup_station_coords(s.station_id, s.station_name, s.city)
        if lat and lon:
            s.latitude  = lat
            s.longitude = lon
            updated += 1

    db.session.commit()
    print(f"Backfilled {updated} station(s).")
    print("\nAll done.")
