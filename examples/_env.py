"""Shared example bootstrap: load .env if python-dotenv is installed."""


def load_env() -> bool:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv()
