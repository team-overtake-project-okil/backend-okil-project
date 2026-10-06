import os
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()

url = os.getenv("DB_URL")
engine = create_engine(url)

def check_db():
    try:
        conn = engine.connect()
        conn.close()
        return True
    except Exception:
        return False