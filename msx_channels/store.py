import sqlite3
import uuid


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, video TEXT NOT NULL,
            rotation INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()

    def all(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM channels ORDER BY created_at, id')]

    def save(self, title, video, rotation, identifier=None):
        if identifier:
            cursor = self.db.execute('UPDATE channels SET title=?, video=?, rotation=? WHERE id=?',
                                     (title, video, rotation, identifier))
            if not cursor.rowcount:
                raise KeyError(identifier)
        else:
            identifier = uuid.uuid4().hex[:16]
            self.db.execute('INSERT INTO channels (id,title,video,rotation) VALUES (?,?,?,?)',
                            (identifier, title, video, rotation))
        self.db.commit()
        return identifier

    def delete(self, identifier):
        cursor = self.db.execute('DELETE FROM channels WHERE id=?', (identifier,))
        self.db.commit()
        if not cursor.rowcount:
            raise KeyError(identifier)

    def close(self):
        self.db.close()
