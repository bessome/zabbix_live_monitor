"""Application paths and environment configuration."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DB_PATH = Path(os.environ.get("APP_DB_PATH", ROOT / "instance" / "monitor.db"))
CATEGORIES = ("TV_Amplifires", "Switches", "Modems", "VOIP", "Routers")
SECRET = os.environ.get("APP_SECRET_KEY")
if not SECRET or len(SECRET) < 32:
    raise RuntimeError("Set APP_SECRET_KEY to a random value of at least 32 characters")
