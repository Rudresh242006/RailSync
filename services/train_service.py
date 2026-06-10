def allocate_platform(train_data):
    if not train_data:
        return {"error": "No data provided"}

    train_number = train_data.get("train_number")

    if not train_number:
        return {"error": "Train number missing"}

    # Logic to handle alphanumeric train numbers
    try:
        import re
        digits = re.sub(r'\D', '', str(train_number))
        val = int(digits) if digits else hash(str(train_number))
        platform = 1 if val % 2 == 0 else 2
    except Exception:
        platform = 1

    return {
        "train_number": train_number,
        "platform": platform,
        "status": "allocated"
    }

def recalculate_platform_allocations():
    """
    Dynamically recalculate all platform allocations for the entire network
    based on the current schedules and live delays.
    """
    from extensions import db
    from models import Train, TrainRoute, PlatformAllocation, Platform, TrainStatus
    from datetime import datetime, timedelta

    trains = Train.query.all()
    schedules = []

    for train in trains:
        if not train.service_start_date:
            continue
            
        status = TrainStatus.query.filter_by(train_id=train.train_id).first()
        delay_minutes = status.delay_minutes if status else 0

        base_time = train.service_start_date
        routes = TrainRoute.query.filter_by(train_id=train.train_id).order_by(TrainRoute.stop_number).all()

        current_dt = base_time
        for i, route in enumerate(routes):
            if i == 0:
                arrival = current_dt
                departure = current_dt
            else:
                arr_time = route.arrival_time
                dep_time = route.departure_time
                prev_dep = routes[i-1].departure_time
                prev_dt = schedules[-1]['base_departure'] # Base departure without delay
                
                arr_m = arr_time.hour * 60 + arr_time.minute
                prev_dep_m = prev_dep.hour * 60 + prev_dep.minute
                
                travel_m = arr_m - prev_dep_m
                if travel_m < 0:
                    travel_m += 24 * 60
                    
                arrival = prev_dt + timedelta(minutes=travel_m)
                
                if dep_time:
                    dep_m = dep_time.hour * 60 + dep_time.minute
                    wait_m = dep_m - arr_m
                    if wait_m < 0:
                        wait_m += 24 * 60
                    departure = arrival + timedelta(minutes=wait_m)
                else:
                    departure = arrival

            # Add delay minutes to get the actual projected times
            actual_arrival = arrival + timedelta(minutes=delay_minutes)
            actual_departure = departure + timedelta(minutes=delay_minutes)

            schedules.append({
                'train_id': train.train_id,
                'station_id': route.station_id,
                'arrival': actual_arrival,
                'departure': actual_departure,
                'base_departure': departure,  # Needed for next loop calculation
                'route_id': route.route_id
            })

    # Group by station
    st_events = {}
    for s in schedules:
        if s['station_id'] not in st_events:
            st_events[s['station_id']] = []
        st_events[s['station_id']].append(s)

    allocations_to_add = []

    for sid, events in st_events.items():
        events.sort(key=lambda x: x['arrival'])
        
        platform_objs = Platform.query.filter_by(station_id=sid).all()
        # map '1', '2', '3' -> platform_id
        p_map = {p.platform_number: p.platform_id for p in platform_objs}
        
        platforms_end_times = {'1': None, '2': None, '3': None}
        
        for e in events:
            assigned = False
            for p_num in ['1', '2', '3']:
                # Provide a small buffer for platform clearance, e.g., 5 mins
                clearance_time = platforms_end_times[p_num] + timedelta(minutes=5) if platforms_end_times[p_num] else None
                if clearance_time is None or clearance_time <= e['arrival']:
                    platforms_end_times[p_num] = e['departure']
                    e['platform_num'] = p_num
                    assigned = True
                    break
                    
            if not assigned:
                e['platform_num'] = '1' # Fallback
                
            alloc = PlatformAllocation(
                train_id=e['train_id'],
                station_id=e['station_id'],
                platform_id=p_map.get(e['platform_num']) or (next(iter(p_map.values())) if p_map else None),
                arrival_time=e['arrival'],
                departure_time=e['departure']
            )
            if alloc.platform_id is None:
                continue  # skip if station has no platforms configured
            allocations_to_add.append(alloc)

    # 1. Cache existing allocations to check for changes
    old_allocs = PlatformAllocation.query.all()
    old_map = {(a.train_id, a.station_id): a.platform_id for a in old_allocs}
    
    # Apply changes to DB in one transaction
    try:
        from models import Booking, Notification
        
        # Determine changed platforms and generate notifications
        notifications_to_add = []
        for alloc in allocations_to_add:
            key = (alloc.train_id, alloc.station_id)
            old_platform_id = old_map.get(key)
            
            if old_platform_id and old_platform_id != alloc.platform_id:
                # Platform changed!
                train = Train.query.get(alloc.train_id)
                platform = Platform.query.get(alloc.platform_id)
                station = platform.station
                
                # Notify users who are boarding this train at this station
                bookings = Booking.query.filter_by(
                    train_id=alloc.train_id,
                    source_station_id=alloc.station_id,
                    status='CONFIRMED'
                ).all()
                
                for b in bookings:
                    msg = f"Platform Change Alert: Due to dynamic scheduling, your train {train.train_name} at {station.station_name} will now arrive at Platform {platform.platform_number}."
                    notif = Notification(
                        user_id=b.user_id,
                        train_id=b.train_id,
                        message=msg,
                        notif_type='delay'  # reuse 'delay' category for general alerts
                    )
                    notifications_to_add.append(notif)
                    
        PlatformAllocation.query.delete()
        db.session.bulk_save_objects(allocations_to_add)
        db.session.bulk_save_objects(notifications_to_add)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Error recalculating platform allocations: {e}")