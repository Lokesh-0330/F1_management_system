\set ON_ERROR_STOP on

DROP VIEW  IF EXISTS View_Race_Calendar;
DROP VIEW  IF EXISTS View_Constructor_Standings;
DROP VIEW  IF EXISTS View_Driver_Standings;
DROP TABLE IF EXISTS penalties, pitstops, sessionresults, sessions, races, circuits, drivers, teams CASCADE;
DROP FUNCTION IF EXISTS f1_points(TEXT, INT);

CREATE TABLE teams (
    team_id        SERIAL PRIMARY KEY,
    name           VARCHAR(60) NOT NULL,
    base_country   VARCHAR(60) NOT NULL,
    team_principal VARCHAR(80) NOT NULL,
    team_color     VARCHAR(7)  NOT NULL DEFAULT '#2A2E3D',
    CONSTRAINT uq_team_name  UNIQUE (name),
    CONSTRAINT ck_team_color CHECK (team_color ~ '^#[0-9A-Fa-f]{6}$')
);

CREATE TABLE drivers (
    driver_id     SERIAL PRIMARY KEY,
    team_id       INT         NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    driver_number INT         NOT NULL,
    code          VARCHAR(3)  NOT NULL,
    first_name    VARCHAR(50) NOT NULL,
    last_name     VARCHAR(50) NOT NULL,
    nationality   VARCHAR(50) NOT NULL,
    date_of_birth DATE        NOT NULL,
    CONSTRAINT uq_driver_number UNIQUE (driver_number),
    CONSTRAINT uq_driver_code   UNIQUE (code),
    CONSTRAINT ck_driver_number CHECK (driver_number BETWEEN 1 AND 99),
    CONSTRAINT ck_driver_code   CHECK (code ~ '^[A-Z]{3}$')
);

CREATE TABLE circuits (
    circuit_id    SERIAL PRIMARY KEY,
    name          VARCHAR(80)  NOT NULL,
    city          VARCHAR(60)  NOT NULL,
    country       VARCHAR(60)  NOT NULL,
    length_km     NUMERIC(5,3) NOT NULL,
    race_laps     INT          NOT NULL,
    lap_record_ms INT,
    record_holder VARCHAR(60),
    record_year   INT,
    CONSTRAINT uq_circuit_name   UNIQUE (name),
    CONSTRAINT ck_circuit_length CHECK (length_km > 0),
    CONSTRAINT ck_circuit_laps   CHECK (race_laps > 0),
    CONSTRAINT ck_record_lap     CHECK (lap_record_ms IS NULL OR lap_record_ms > 0),
    CONSTRAINT ck_record_year    CHECK (record_year IS NULL OR record_year BETWEEN 1950 AND 2100)
);

CREATE TABLE races (
    race_id    SERIAL PRIMARY KEY,
    round      INT         NOT NULL,
    name       VARCHAR(80) NOT NULL,
    circuit_id INT         NOT NULL REFERENCES circuits(circuit_id) ON DELETE CASCADE,
    race_date  DATE        NOT NULL,
    CONSTRAINT uq_race_round UNIQUE (round),
    CONSTRAINT ck_race_round CHECK (round > 0)
);

CREATE TABLE sessions (
    session_id    SERIAL PRIMARY KEY,
    race_id       INT         NOT NULL REFERENCES races(race_id) ON DELETE CASCADE,
    session_type  VARCHAR(20) NOT NULL,
    session_start TIMESTAMP   NOT NULL,
    CONSTRAINT uq_session_per_race UNIQUE (race_id, session_type),
    CONSTRAINT ck_session_type CHECK (session_type IN
        ('FP1','FP2','FP3','Sprint Qualifying','Sprint','Qualifying','Race'))
);

CREATE TABLE sessionresults (
    result_id       SERIAL PRIMARY KEY,
    session_id      INT         NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    driver_id       INT         NOT NULL REFERENCES drivers(driver_id)   ON DELETE CASCADE,
    grid_position   INT         NOT NULL,
    finish_position INT         NOT NULL,
    points          INT         NOT NULL DEFAULT 0,
    best_lap_ms     INT,
    status          VARCHAR(10) NOT NULL DEFAULT 'Finished',
    CONSTRAINT uq_result_driver   UNIQUE (session_id, driver_id),
    CONSTRAINT uq_result_position UNIQUE (session_id, finish_position),
    CONSTRAINT ck_grid_position   CHECK (grid_position BETWEEN 0 AND 22),
    CONSTRAINT ck_finish_position CHECK (finish_position BETWEEN 1 AND 22),
    CONSTRAINT ck_points          CHECK (points >= 0),
    CONSTRAINT ck_best_lap        CHECK (best_lap_ms IS NULL OR best_lap_ms > 0),
    CONSTRAINT ck_result_status   CHECK (status IN ('Finished','DNF','DSQ'))
);

CREATE TABLE pitstops (
    pit_id        SERIAL PRIMARY KEY,
    result_id     INT          NOT NULL REFERENCES sessionresults(result_id) ON DELETE CASCADE,
    lap_number    INT          NOT NULL,
    stop_time_sec NUMERIC(5,2) NOT NULL,
    tire_compound VARCHAR(12)  NOT NULL,
    CONSTRAINT uq_pit_lap       UNIQUE (result_id, lap_number),
    CONSTRAINT ck_pit_lap       CHECK (lap_number > 0),
    CONSTRAINT ck_pit_time      CHECK (stop_time_sec > 0),
    CONSTRAINT ck_tire_compound CHECK (tire_compound IN
        ('Soft','Medium','Hard','Intermediate','Wet'))
);

CREATE TABLE penalties (
    penalty_id      SERIAL PRIMARY KEY,
    result_id       INT          NOT NULL REFERENCES sessionresults(result_id) ON DELETE CASCADE,
    penalty_type    VARCHAR(20)  NOT NULL,
    penalty_seconds INT          NOT NULL DEFAULT 0,
    license_points  INT          NOT NULL DEFAULT 0,
    reason          VARCHAR(300) NOT NULL,
    issued_at       TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_penalty_type    CHECK (penalty_type IN
        ('Time Penalty','Drive-Through','Stop-Go','Grid Drop','Reprimand','Disqualification')),
    CONSTRAINT ck_penalty_seconds CHECK (penalty_seconds BETWEEN 0 AND 300),
    CONSTRAINT ck_license_points  CHECK (license_points BETWEEN 0 AND 12)
);

CREATE INDEX idx_drivers_team     ON drivers(team_id);
CREATE INDEX idx_races_circuit    ON races(circuit_id);
CREATE INDEX idx_sessions_race    ON sessions(race_id);
CREATE INDEX idx_results_driver   ON sessionresults(driver_id);
CREATE INDEX idx_pitstops_result  ON pitstops(result_id);
CREATE INDEX idx_penalties_result ON penalties(result_id);

-- Championship points for a finishing position
CREATE FUNCTION f1_points(sess_type TEXT, pos INT) RETURNS INT AS $$
    SELECT CASE
        WHEN sess_type = 'Race' THEN
            CASE pos WHEN 1 THEN 25 WHEN 2 THEN 18 WHEN 3 THEN 15 WHEN 4 THEN 12
                     WHEN 5 THEN 10 WHEN 6 THEN 8  WHEN 7 THEN 6  WHEN 8 THEN 4
                     WHEN 9 THEN 2  WHEN 10 THEN 1 ELSE 0 END
        WHEN sess_type = 'Sprint' THEN
            CASE WHEN pos BETWEEN 1 AND 8 THEN 9 - pos ELSE 0 END
        ELSE 0
    END
$$ LANGUAGE SQL IMMUTABLE;

CREATE VIEW View_Driver_Standings AS
SELECT
    d.driver_id,
    d.driver_number,
    d.code,
    d.first_name || ' ' || d.last_name AS driver_name,
    t.name AS team_name,
    COALESCE(SUM(sr.points), 0) AS total_points,
    COUNT(*) FILTER (WHERE s.session_type = 'Race' AND sr.finish_position = 1)  AS wins,
    COUNT(*) FILTER (WHERE s.session_type = 'Race' AND sr.finish_position <= 3) AS podiums,
    RANK() OVER (ORDER BY COALESCE(SUM(sr.points), 0) DESC) AS standing_pos
FROM drivers d
JOIN teams t                ON t.team_id = d.team_id
LEFT JOIN sessionresults sr ON sr.driver_id = d.driver_id
LEFT JOIN sessions s        ON s.session_id = sr.session_id
GROUP BY d.driver_id, d.driver_number, d.code, d.first_name, d.last_name, t.name;

CREATE VIEW View_Constructor_Standings AS
SELECT
    t.team_id,
    t.name AS team_name,
    t.base_country,
    COUNT(DISTINCT d.driver_id) AS driver_count,
    COALESCE(SUM(sr.points), 0) AS total_points,
    COUNT(*) FILTER (WHERE s.session_type = 'Race' AND sr.finish_position = 1) AS wins,
    RANK() OVER (ORDER BY COALESCE(SUM(sr.points), 0) DESC) AS standing_pos
FROM teams t
LEFT JOIN drivers d         ON d.team_id = t.team_id
LEFT JOIN sessionresults sr ON sr.driver_id = d.driver_id
LEFT JOIN sessions s        ON s.session_id = sr.session_id
GROUP BY t.team_id, t.name, t.base_country;

-- Race joined with its circuit; race distance is derived, never stored
CREATE VIEW View_Race_Calendar AS
SELECT
    r.race_id,
    r.round,
    r.name,
    r.race_date,
    c.circuit_id,
    c.name AS circuit,
    c.city,
    c.country,
    c.length_km,
    c.race_laps,
    ROUND(c.length_km * c.race_laps, 1) AS race_distance_km,
    c.lap_record_ms,
    c.record_holder,
    c.record_year
FROM races r
JOIN circuits c ON c.circuit_id = r.circuit_id;

-- Sample data

INSERT INTO teams (name, base_country, team_principal, team_color) VALUES
('Ferrari',         'Italy',          'Frédéric Vasseur', '#E8002D'),
('Red Bull Racing', 'United Kingdom', 'Christian Horner', '#3671C6'),
('McLaren',         'United Kingdom', 'Andrea Stella',     '#FF8000'),
('Mercedes',        'United Kingdom', 'Toto Wolff',        '#27F4D2'),
('Aston Martin',    'United Kingdom', 'Mike Krack',        '#229971'),
('Williams',        'United Kingdom', 'James Vowles',      '#64C4FF');

INSERT INTO drivers (team_id, driver_number, code, first_name, last_name, nationality, date_of_birth)
SELECT t.team_id, v.num, v.code, v.fn, v.ln, v.nat, v.dob::date
FROM (VALUES
    ('Ferrari',         16, 'LEC', 'Charles',   'Leclerc',    'Monegasque', '1997-10-16'),
    ('Ferrari',         55, 'SAI', 'Carlos',    'Sainz',      'Spanish',    '1994-09-01'),
    ('Red Bull Racing',  1, 'VER', 'Max',       'Verstappen', 'Dutch',      '1997-09-30'),
    ('Red Bull Racing',  3, 'RIC', 'Daniel',    'Ricciardo',  'Australian', '1989-07-01'),
    ('McLaren',         44, 'HAM', 'Lewis',     'Hamilton',   'British',    '1985-01-07'),
    ('McLaren',         14, 'ALO', 'Fernando',  'Alonso',     'Spanish',    '1981-07-29'),
    ('Mercedes',        63, 'RUS', 'George',    'Russell',    'British',    '1998-02-15'),
    ('Mercedes',        12, 'ANT', 'Kimi',      'Antonelli',  'Italian',    '2006-08-25'),
    ('Aston Martin',    18, 'STR', 'Lance',     'Stroll',     'Canadian',   '1998-10-29'),
    ('Aston Martin',    27, 'HUL', 'Nico',      'Hülkenberg', 'German',     '1987-08-19'),
    ('Williams',        23, 'ALB', 'Alexander', 'Albon',      'Thai',       '1996-03-23'),
    ('Williams',        43, 'COL', 'Franco',    'Colapinto',  'Argentine',  '2003-05-27')
) AS v(team, num, code, fn, ln, nat, dob)
JOIN teams t ON t.name = v.team;

-- Track facts entered from memory: verify before presenting
INSERT INTO circuits (name, city, country, length_km, race_laps, lap_record_ms, record_holder, record_year) VALUES
('Albert Park Circuit',       'Melbourne',   'Australia',            5.278, 58, 79813, 'Charles Leclerc',     2024),
('Circuit de Monaco',         'Monte Carlo', 'Monaco',               3.337, 78, 72909, 'Lewis Hamilton',      2021),
('Silverstone Circuit',       'Silverstone', 'United Kingdom',       5.891, 52, 87097, 'Max Verstappen',      2020),
('Autodromo Nazionale Monza', 'Monza',       'Italy',                5.793, 53, 81046, 'Rubens Barrichello',  2004),
('Marina Bay Street Circuit', 'Singapore',   'Singapore',            4.940, 62, 95867, 'Lewis Hamilton',      2023),
('Yas Marina Circuit',        'Abu Dhabi',   'United Arab Emirates', 5.281, 58, 86103, 'Max Verstappen',      2021);

INSERT INTO races (round, name, circuit_id, race_date)
SELECT v.rnd, v.gp, c.circuit_id, v.rdate::date
FROM (VALUES
    (1, 'Australian Grand Prix', 'Albert Park Circuit',       '2026-03-08'),
    (2, 'Monaco Grand Prix',     'Circuit de Monaco',         '2026-06-07'),
    (3, 'British Grand Prix',    'Silverstone Circuit',       '2026-07-05'),
    (4, 'Italian Grand Prix',    'Autodromo Nazionale Monza', '2026-09-06'),
    (5, 'Singapore Grand Prix',  'Marina Bay Street Circuit', '2026-10-11'),
    (6, 'Abu Dhabi Grand Prix',  'Yas Marina Circuit',        '2026-12-06')
) AS v(rnd, gp, circuit, rdate)
JOIN circuits c ON c.name = v.circuit;

-- Every race weekend gets the same five sessions (times in UTC)
INSERT INTO sessions (race_id, session_type, session_start)
SELECT r.race_id, v.stype, (r.race_date + v.day_offset) + v.tod::time
FROM races r
CROSS JOIN (VALUES
    ('FP1',        -2, '11:30'),
    ('FP2',        -2, '15:00'),
    ('FP3',        -1, '11:30'),
    ('Qualifying', -1, '15:00'),
    ('Race',        0, '14:00')
) AS v(stype, day_offset, tod);

-- Qualifying and race orders for rounds 1-5. base_ms is the pole time (Qualifying)
-- or the reference lap used to generate race fastest laps. dnf lists retirements.
CREATE TEMP TABLE seed (
    rnd          INT,
    session_type VARCHAR(20),
    order_codes  TEXT[],
    dnf          TEXT[],
    base_ms      INT
);

INSERT INTO seed (rnd, session_type, order_codes, dnf, base_ms) VALUES
(1, 'Qualifying', '{VER,RIC,HAM,ALO,LEC,RUS,SAI,ANT,ALB,STR,HUL,COL}', '{}',    75900),
(1, 'Race',       '{VER,RIC,ALO,HAM,LEC,RUS,SAI,ANT,ALB,STR,HUL,COL}', '{COL}', 80300),
(2, 'Qualifying', '{LEC,VER,ALO,RIC,HAM,SAI,RUS,ANT,ALB,STR,HUL,COL}', '{}',    70200),
(2, 'Race',       '{VER,ALO,RIC,LEC,HAM,SAI,RUS,ALB,ANT,STR,COL,HUL}', '{}',    73400),
(3, 'Qualifying', '{RIC,VER,HAM,ALO,RUS,LEC,ANT,SAI,ALB,HUL,STR,COL}', '{}',    85000),
(3, 'Race',       '{RIC,VER,ALO,HAM,RUS,LEC,ANT,SAI,HUL,ALB,STR,COL}', '{COL}', 87600),
(4, 'Qualifying', '{VER,SAI,ALO,LEC,RIC,HAM,RUS,ANT,ALB,STR,COL,HUL}', '{}',    78800),
(4, 'Race',       '{VER,ALO,RIC,SAI,LEC,HAM,RUS,ANT,STR,ALB,COL,HUL}', '{HUL}', 81500),
(5, 'Qualifying', '{VER,ALO,RIC,HAM,LEC,SAI,RUS,ANT,ALB,STR,HUL,COL}', '{}',    89000),
(5, 'Race',       '{VER,RIC,ALO,HAM,LEC,SAI,RUS,ANT,ALB,HUL,COL,STR}', '{STR}', 96400);

INSERT INTO sessionresults (session_id, driver_id, grid_position, finish_position, points, best_lap_ms, status)
SELECT s.session_id,
       d.driver_id,
       CASE WHEN x.session_type = 'Race' THEN array_position(q.order_codes, u.code) ELSE 0 END,
       u.pos::int,
       CASE WHEN u.code = ANY (x.dnf) THEN 0 ELSE f1_points(x.session_type, u.pos::int) END,
       CASE WHEN u.code = ANY (x.dnf) THEN NULL
            WHEN x.session_type = 'Qualifying' THEN x.base_ms + (u.pos - 1) * 190
            ELSE x.base_ms + ((u.pos * 5 + x.rnd * 7) % 12) * 110 END,
       CASE WHEN u.code = ANY (x.dnf) THEN 'DNF' ELSE 'Finished' END
FROM seed x
JOIN seed q     ON q.rnd = x.rnd AND q.session_type = 'Qualifying'
JOIN races r    ON r.round = x.rnd
JOIN sessions s ON s.race_id = r.race_id AND s.session_type = x.session_type
CROSS JOIN LATERAL unnest(x.order_codes) WITH ORDINALITY AS u(code, pos)
JOIN drivers d  ON d.code = u.code;

-- Practice sessions: a repeatable shuffled order, slower than the pole time
INSERT INTO sessionresults (session_id, driver_id, grid_position, finish_position, points, best_lap_ms, status)
SELECT s.session_id,
       ranked.driver_id,
       0,
       ranked.rk::int,
       0,
       q.base_ms + CASE s.session_type WHEN 'FP1' THEN 1800 WHEN 'FP2' THEN 900 ELSE 400 END
                 + (ranked.rk - 1) * 150,
       'Finished'
FROM sessions s
JOIN races r ON r.race_id = s.race_id
JOIN seed q  ON q.rnd = r.round AND q.session_type = 'Qualifying'
CROSS JOIN LATERAL (
    SELECT d.driver_id,
           ROW_NUMBER() OVER (ORDER BY md5(d.code || s.session_id::text)) AS rk
    FROM drivers d
) ranked
WHERE s.session_type IN ('FP1', 'FP2', 'FP3');

DROP TABLE seed;

INSERT INTO pitstops (result_id, lap_number, stop_time_sec, tire_compound)
SELECT sr.result_id, v.lap, v.secs, v.tire
FROM (VALUES
    (1, 'VER', 18, 2.41, 'Hard'),   (1, 'RIC', 17, 2.52, 'Hard'),
    (1, 'RIC', 39, 2.47, 'Medium'), (1, 'ALO', 19, 2.63, 'Hard'),
    (1, 'LEC', 21, 2.58, 'Medium'),
    (2, 'VER', 31, 2.58, 'Medium'), (2, 'ALO', 30, 2.44, 'Hard'),
    (2, 'RIC', 32, 2.71, 'Hard'),   (2, 'HAM', 33, 2.66, 'Hard'),
    (2, 'LEC', 29, 2.49, 'Medium'),
    (3, 'RIC', 20, 2.29, 'Hard'),   (3, 'RIC', 41, 2.33, 'Medium'),
    (3, 'VER', 21, 2.36, 'Hard'),   (3, 'ALO', 22, 2.55, 'Hard'),
    (3, 'HAM', 23, 2.61, 'Medium'),
    (4, 'VER', 24, 2.39, 'Hard'),   (4, 'ALO', 25, 2.42, 'Hard'),
    (4, 'RIC', 23, 2.68, 'Medium'), (4, 'SAI', 24, 2.31, 'Hard'),
    (4, 'LEC', 26, 2.74, 'Hard'),
    (5, 'VER', 22, 2.35, 'Soft'),   (5, 'VER', 44, 2.38, 'Medium'),
    (5, 'RIC', 23, 2.50, 'Hard'),   (5, 'ALO', 24, 2.47, 'Hard'),
    (5, 'HAM', 25, 2.57, 'Medium'), (5, 'RUS', 26, 2.62, 'Hard')
) AS v(rnd, code, lap, secs, tire)
JOIN races r           ON r.round = v.rnd
JOIN sessions s        ON s.race_id = r.race_id AND s.session_type = 'Race'
JOIN drivers d         ON d.code = v.code
JOIN sessionresults sr ON sr.session_id = s.session_id AND sr.driver_id = d.driver_id;

INSERT INTO penalties (result_id, penalty_type, penalty_seconds, license_points, reason, issued_at)
SELECT sr.result_id, v.ptype, v.secs, v.pts, v.reason, v.issued::timestamp
FROM (VALUES
    (1, 'ALB', 'Time Penalty',  5, 1, 'Exceeding the pit lane speed limit',      '2026-03-08 05:30'),
    (2, 'HAM', 'Time Penalty',  5, 1, 'Causing a collision at the first corner', '2026-06-07 14:25'),
    (3, 'RUS', 'Time Penalty',  5, 2, 'Repeated track limits violations',        '2026-07-05 15:40'),
    (3, 'COL', 'Drive-Through', 0, 2, 'Causing a collision with another car',    '2026-07-05 14:55'),
    (4, 'SAI', 'Reprimand',     0, 0, 'Unsafe release in the pit lane',          '2026-09-06 14:30'),
    (5, 'RIC', 'Time Penalty',  5, 1, 'Causing a collision at turn 1',           '2026-10-11 15:05'),
    (5, 'LEC', 'Time Penalty', 10, 2, 'Forcing another driver off the track',    '2026-10-11 15:20')
) AS v(rnd, code, ptype, secs, pts, reason, issued)
JOIN races r           ON r.round = v.rnd
JOIN sessions s        ON s.race_id = r.race_id AND s.session_type = 'Race'
JOIN drivers d         ON d.code = v.code
JOIN sessionresults sr ON sr.session_id = s.session_id AND sr.driver_id = d.driver_id;