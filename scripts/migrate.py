from app import app
from extensions import db
from sqlalchemy import text
from models import Station

def run_migration():
    with app.app_context():
        # Add column if not exists
        try:
            db.session.execute(text("ALTER TABLE StationMaster ADD COLUMN is_on_leave BOOLEAN DEFAULT 0"))
            print("Added is_on_leave column.")
        except Exception as e:
            if 'Duplicate column name' in str(e):
                print("is_on_leave column already exists.")
            else:
                print("Error adding column:", str(e))
                db.session.rollback()
        
        # Add Temporary Pool station
        pool = Station.query.filter_by(station_name='Unassigned / Temporary Pool').first()
        if not pool:
            pool = Station(station_name='Unassigned / Temporary Pool', city='System', state='HQ')
            db.session.add(pool)
            print("Added Unassigned / Temporary Pool station.")
        else:
            print("Unassigned / Temporary Pool station already exists.")
            
        db.session.commit()
        print("Migration complete!")

if __name__ == '__main__':
    run_migration()
