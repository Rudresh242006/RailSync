from app import app
from extensions import db, bcrypt
from models import Train, TrainDriver
from sqlalchemy import text

def seed_drivers():
    with app.app_context():
        # Alter TrainStatus table to add new columns if they don't exist
        try:
            db.session.execute(text("ALTER TABLE TrainStatus ADD COLUMN state VARCHAR(20) DEFAULT 'stopped';"))
            db.session.execute(text("ALTER TABLE TrainStatus ADD COLUMN current_departure_time DATETIME NULL;"))
            db.session.commit()
            print("Successfully added columns to TrainStatus.")
        except Exception as e:
            db.session.rollback()
            print("Columns might already exist:", e)

        # Create TrainDriver table if it doesn't exist
        db.create_all()

        trains = Train.query.all()
        password = '12345678@'
        password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

        drivers_added = 0
        for i, train in enumerate(trains, 1):
            email = f"driver{i}@gmail.com"
            existing_driver = TrainDriver.query.filter_by(email=email).first()
            if not existing_driver:
                new_driver = TrainDriver(
                    name=f"Driver {i}",
                    email=email,
                    password_hash=password_hash,
                    train_id=train.train_id
                )
                db.session.add(new_driver)
                drivers_added += 1
        
        db.session.commit()
        print(f"Successfully added {drivers_added} drivers.")

if __name__ == '__main__':
    seed_drivers()
