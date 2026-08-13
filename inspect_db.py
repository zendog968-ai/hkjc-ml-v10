import sqlite3
conn = sqlite3.connect('hkjc_last_season.sqlite')
queries = {
    'meetings': 'SELECT COUNT(*) FROM meetings',
    'races': 'SELECT COUNT(*) FROM races',
    'completed_races': "SELECT COUNT(*) FROM races WHERE race_status='completed'",
    'cancelled_races': "SELECT COUNT(*) FROM races WHERE race_status='cancelled'",
    'starters': 'SELECT COUNT(*) FROM starters',
    'coverage': "SELECT MIN(race_date), MAX(race_date), COUNT(DISTINCT race_date) FROM races",
}
for name, query in queries.items():
    print(name, conn.execute(query).fetchone())
