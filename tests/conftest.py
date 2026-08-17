import sys
from pathlib import Path

LAMBDA_DIR = Path(__file__).resolve().parent.parent / "lambda"
sys.path.insert(0, str(LAMBDA_DIR))