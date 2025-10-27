"""Chatmail <-> Mastodon bridge"""

from .hooks import cli  # import from hooks so they are added


def main() -> None:
    """Start the CLI application."""
    try:
        cli.start()
    except KeyboardInterrupt:
        pass
