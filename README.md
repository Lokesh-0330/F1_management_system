# F1 Management System

Database Systems course project: Flask + PostgreSQL (psycopg2) with a pit-wall style UI.

## Features
- 8 tables in 3NF: teams, drivers, circuits, races, sessions, sessionresults, pitstops, penalties
- Views: View_Driver_Standings, View_Constructor_Standings, View_Race_Calendar
- Fan portal: standings, schedule, circuits, race weekends (FP1-3, Qualifying, Race, pit stops), 11 SQL reports
- Admin desk: teams, drivers, circuits, race calendar, session results, pit stops, FIA penalties
- Parameterised queries only, rollback on every error, constraints enforced in the database

## ER diagram
```mermaid
erDiagram
    TEAMS ||--o{ DRIVERS : employs
    CIRCUITS ||--o{ RACES : hosts
    RACES ||--|{ SESSIONS : has
    SESSIONS ||--o{ SESSIONRESULTS : classifies
    DRIVERS ||--o{ SESSIONRESULTS : scores
    SESSIONRESULTS ||--o{ PITSTOPS : has
    SESSIONRESULTS ||--o{ PENALTIES : receives

    TEAMS {
        int team_id PK
        string name UK
        string base_country
        string team_principal
        string team_color
    }
    DRIVERS {
        int driver_id PK
        int team_id FK
        int driver_number UK
        string code UK
        string first_name
        string last_name
        string nationality
        date date_of_birth
    }
    CIRCUITS {
        int circuit_id PK
        string name UK
        string city
        string country
        decimal length_km
        int race_laps
        int lap_record_ms
        string record_holder
        int record_year
    }
    RACES {
        int race_id PK
        int round UK
        string name
        int circuit_id FK
        date race_date
    }
    SESSIONS {
        int session_id PK
        int race_id FK
        string session_type
        timestamp session_start
    }
    SESSIONRESULTS {
        int result_id PK
        int session_id FK
        int driver_id FK
        int grid_position
        int finish_position
        int points
        int best_lap_ms
        string status
    }
    PITSTOPS {
        int pit_id PK
        int result_id FK
        int lap_number
        decimal stop_time_sec
        string tire_compound
    }
    PENALTIES {
        int penalty_id PK
        int result_id FK
        string penalty_type
        int penalty_seconds
        int license_points
        string reason
        timestamp issued_at
    }
```

## Setup (Windows + Ubuntu WSL2)
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip postgresql postgresql-contrib
sudo service postgresql start
sudo -u postgres psql -c "ALTER USER postgres PASSWORD 'postgres';"
sudo -u postgres createdb -E UTF8 -T template0 f1_db
PGPASSWORD=postgres psql -h localhost -U postgres -d f1_db -f schema.sql

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000. Admin login: `admin` / `password123` (override with the
`ADMIN_USERNAME` and `ADMIN_PASSWORD` environment variables).

Running `schema.sql` again drops and recreates everything, so it also resets the data.

## Environment variables
`DB_NAME DB_USER DB_PASSWORD DB_HOST DB_PORT SECRET_KEY ADMIN_USERNAME ADMIN_PASSWORD`

## Project structure
```
f1-management-system/
├── schema.sql
├── app.py
├── requirements.txt
├── .gitignore
├── README.md
└── templates/
```