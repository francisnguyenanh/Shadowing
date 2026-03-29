import sqlite3
import click
from flask import current_app, g


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            current_app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_url TEXT NOT NULL,
            video_id TEXT NOT NULL,
            title TEXT,
            language TEXT,
            duration INTEGER,
            audio_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS segments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id INTEGER REFERENCES videos(id),
            segment_order INTEGER,
            start_time REAL,
            end_time REAL,
            text TEXT,
            translation TEXT,
            bookmarked INTEGER DEFAULT 0
        )
    ''')
    # Migrations for existing databases
    for _sql in [
        'ALTER TABLE segments ADD COLUMN bookmarked INTEGER DEFAULT 0',
        'ALTER TABLE videos ADD COLUMN audio_path TEXT',
        'ALTER TABLE videos ADD COLUMN transcript_raw TEXT',
    ]:
        try:
            db.execute(_sql)
            db.commit()
        except Exception:
            pass  # column already exists
    db.execute('''
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    db.execute('''
        CREATE TABLE IF NOT EXISTS playlist_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(playlist_id, video_id)
        )
    ''')
    db.commit()
