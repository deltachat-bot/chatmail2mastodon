"""Bot CLI"""

from deltabot_cli import BotCli

from ._version import __version__

cli = BotCli("chatmail2mastodon")
cli.add_generic_option("-v", "--version", action="version", version=__version__)
cli.add_generic_option(
    "--interval",
    type=int,
    default=30,
    help="how many seconds to sleep before checking feeds again (default: %(default)s)",
)
cli.add_generic_option(
    "--no-time",
    help="do not display date timestamp in log messages",
    action="store_false",
)
