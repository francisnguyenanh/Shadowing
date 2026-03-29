SECRET_KEY = 'shadowing-local-2024'
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'database.db')

MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
