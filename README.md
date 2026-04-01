# 🚆 RailSync — AI-Powered Smart Railway Platform

A full-stack Flask web app with AI-driven platform allocation, real-time notifications, seat booking, and live train tracking.

---

## 📁 Project Structure

```
railsync/
├── app.py                  # Flask app factory + SocketIO
├── models.py               # SQLAlchemy models (matches your DB schema)
├── requirements.txt
├── .env.example
├── routes/
│   ├── auth.py             # Login, register, logout
│   ├── user.py             # Passenger: search, book, track, notifications
│   ├── admin.py            # Station Master: trains, platforms, AI delay
│   └── api.py              # REST API for live status, AJAX
└── templates/
    ├── base.html            # Shared layout (sidebar, topbar, dark theme)
    ├── index.html           # Landing page
    ├── login.html           # Login with role switcher
    ├── register.html        # User registration
    ├── user/
    │   ├── dashboard.html
    │   ├── search.html      # Train search + booking form
    │   ├── bookings.html    # All user bookings with cancel
    │   ├── track.html       # Live train tracker
    │   ├── seat_map.html    # Visual seat grid (window/aisle)
    │   └── notifications.html
    └── admin/
        ├── dashboard.html   # Platform status board + delay alerts
        ├── trains.html      # Train list
        ├── add_train.html   # Dynamic route builder
        ├── delay.html       # Report delay + AI result panel
        └── platforms.html   # Add platforms + allocate trains
```

---

## 🚀 Setup Instructions

### 1. Create & activate virtual environment

```bash
python -m venv venv
source venv/bin/activate        # Linux/Mac
venv\Scripts\activate           # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Set up MySQL database

Run your existing SQL schema in MySQL:

```bash
mysql -u root -p < your_schema.sql
```

Make sure the database is named `Train`.

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:
```
SECRET_KEY=any-random-string-here
DATABASE_URL=mysql+pymysql://root:yourpassword@localhost/Train
ANTHROPIC_API_KEY=sk-ant-...your-key...
```

Get your Anthropic API key from: https://console.anthropic.com

### 5. Run the app

```bash
python app.py
```

Visit: http://localhost:5000

---

## 👤 How to Create First Admin (Station Master)

Since Station Masters are created by system admins (not via public register), insert one directly into MySQL:

```sql
USE Train;

-- First add a station
INSERT INTO Station (station_name, city, state) VALUES ('Central Station', 'Mumbai', 'Maharashtra');

-- Then add a station master (password is bcrypt hash of 'admin123')
INSERT INTO StationMaster (name, email, phone, password_hash, station_id)
VALUES (
  'Raj Kumar',
  'admin@railsync.com',
  '9876543210',
  '$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW',  -- 'secret'
  1
);
```

Or generate your own hash in Python:
```python
from flask_bcrypt import Bcrypt
bcrypt = Bcrypt()
print(bcrypt.generate_password_hash('yourpassword').decode('utf-8'))
```

---

## ✨ Features

### Passenger Panel
- 🔍 Search trains by source/destination/date
- 🎫 Book tickets with seat preference (window/any)
- 💳 Online payment (UPI, Card, Netbanking, Wallet)
- 🪑 Visual seat map (available/booked/window colour coded)
- 📍 Live train tracker with route timeline + platform info
- 🔔 Real-time delay & platform change notifications
- ❌ Cancel booking with auto-refund

### Station Master Panel
- ➕ Add trains with dynamic multi-stop route builder
- 🗑 Remove trains (cascades all data)
- 🚉 Platform status board (occupied/free in real time)
- ⚠ Report delays — triggers AI platform reallocation
- 🤖 Claude AI analyses conflicts and suggests best platform
- 📢 Auto-generates passenger announcement text
- 🔗 Sends notifications to all affected passengers instantly

---

## 🤖 AI Platform Allocation

When a Station Master reports a delay:

1. Flask sends train info, delay, all current platform allocations to **Claude (claude-sonnet-4-20250514)**
2. Claude returns JSON with:
   - `action`: "reassign" or "keep"
   - `suggested_platform_id`: which platform to move to
   - `affected_trains`: other trains needing to move
   - `reasoning`: human-readable explanation
   - `announcement`: passenger-facing message
3. System auto-applies the reallocation in the DB
4. All booked passengers get an instant notification

---

## 🔐 Security

- Passwords stored as **bcrypt hashes** (never plain text)
- Session management via Flask-Login
- Role-based access control (user vs admin decorators)
- CSRF protection via Flask secret key
- SQL injection protection via SQLAlchemy ORM

---

## 🔌 WebSocket Support

Flask-SocketIO is included for real-time push notifications. Passengers can join a room per train:

```javascript
const socket = io();
socket.emit('join', { train_id: 123 });
socket.on('new_notification', (data) => {
  // Show alert to user
});
```

---

## 📦 Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.10+, Flask 3.0 |
| ORM | SQLAlchemy + PyMySQL |
| Auth | Flask-Login + Flask-Bcrypt |
| AI | Anthropic Claude (claude-sonnet-4-20250514) |
| Real-time | Flask-SocketIO + Eventlet |
| Frontend | Jinja2, vanilla JS, CSS variables |
| Database | MySQL 8+ |

---

## 🗺 API Endpoints

| Method | URL | Description |
|--------|-----|-------------|
| GET | `/api/train-status/<id>` | Live train status JSON |
| GET | `/api/notifications/unread-count` | Unread count for nav badge |
| GET | `/api/notifications/latest` | Last 5 notifications |
| GET | `/api/seat-availability/<train_id>/<date>` | Full seat map JSON |
