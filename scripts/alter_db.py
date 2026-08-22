from app import app
from extensions import db
from sqlalchemy import text

with app.app_context():
    try:
        db.session.execute(text("ALTER TABLE Booking MODIFY seat_number VARCHAR(100);"))
        db.session.commit()
        print("Successfully updated Booking.seat_number to VARCHAR(100)")
    except Exception as e:
        print(f"Error altering table: {e}")
