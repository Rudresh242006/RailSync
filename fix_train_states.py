from app import app
from extensions import db
from models import TrainStatus
from datetime import datetime

with app.app_context():
    statuses = TrainStatus.query.filter(TrainStatus.journey_direction.in_(['forward', 'reverse'])).all()
    now = datetime.now()
    
    for status in statuses:
        details = status.get_route_details()
        if not details: continue
        
        # Find exactly where the train SHOULD be
        correct_station_id = details[0]['station_id']
        correct_state = 'stopped'
        
        for i, d in enumerate(details):
            if now < d['actual_arrival']:
                # It's currently traveling to this station!
                # That means it departed the previous station.
                correct_station_id = details[i-1]['station_id']
                correct_state = 'en_route'
                break
            elif now < d['actual_departure']:
                # It has arrived at this station but hasn't departed yet.
                correct_station_id = d['station_id']
                correct_state = 'stopped'
                break
            else:
                # It has departed this station. If it's the last station, it should be arrived.
                if i == len(details) - 1:
                    correct_station_id = d['station_id']
                    correct_state = 'stopped'
        
        # Update the status if it's wrong
        if status.current_station_id != correct_station_id or status.state != correct_state:
            print(f"Fixing Train {status.train_id}: {status.current_station_id} ({status.state}) -> {correct_station_id} ({correct_state})")
            status.current_station_id = correct_station_id
            status.state = correct_state
            
            # If it's en_route, set the departure time
            if correct_state == 'en_route':
                # find the departure time of the correct_station_id
                for d in details:
                    if d['station_id'] == correct_station_id:
                        status.current_departure_time = d['actual_departure']
                        break
            else:
                status.current_departure_time = None
                
    db.session.commit()
    print("Done fixing states.")
