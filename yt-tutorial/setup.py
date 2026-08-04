import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

print(os.getenv("CUSTOM_API_BASE", "alter"))