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