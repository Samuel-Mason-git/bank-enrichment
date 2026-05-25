from pathlib import Path
from dotenv import load_dotenv
from database_functions import init_db, clear_db

load_dotenv(Path(__file__).parent.parent.parent / "config" / ".env")

init_db()
clear_db()
