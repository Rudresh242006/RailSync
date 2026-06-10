from app import app
from extensions import db
from models import Station, Platform

def add_platforms():
    with app.app_context():
        stations = Station.query.all()
        platforms_added = 0
        
        for station in stations:
            # Check existing platforms to avoid duplicates
            existing_platforms = [p.platform_number for p in station.platforms]
            
            for i in range(1, 4):
                platform_num = str(i)
                if platform_num not in existing_platforms:
                    new_platform = Platform(
                        station_id=station.station_id,
                        platform_number=platform_num,
                        is_available=True
                    )
                    db.session.add(new_platform)
                    platforms_added += 1
        
        db.session.commit()
        print(f"Successfully added {platforms_added} platforms.")

if __name__ == '__main__':
    add_platforms()
