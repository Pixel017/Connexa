"""Connexa - run the whole platform with: python run.py"""
import os

from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "connexa.main:app",
        host=os.getenv("CONNEXA_HOST", "127.0.0.1"),
        port=int(os.getenv("CONNEXA_PORT", "8000")),
        log_level="info",
    )
