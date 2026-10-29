import ssl
if hasattr(ssl.SSLContext, "_load_windows_store_certs"):
    _orig_ws_certs = ssl.SSLContext._load_windows_store_certs
    def _tolerant_ws_certs(self, *args):
        # Some machines carry a corrupt cert in the Windows store (vendor
        # security agents); frozen apps fail hard on it. Skip bad stores --
        # LAN HTTP traffic does not rely on them.
        try:
            return _orig_ws_certs(self, *args)
        except ssl.SSLError:
            pass
    ssl.SSLContext._load_windows_store_certs = _tolerant_ws_certs

import sys


def main() -> int:
    """One binary, three roles.

    No arguments opens the desktop window. Any argument goes to the CLI, which
    is where `serve` (the background daemon) and `mcp` (the stdio server AI
    clients register) live. Shipping a single file is what stops users from
    ending up with a stale daemon next to a fresh client.
    """
    args = sys.argv[1:]
    if not args or args[0] in ("--gui", "gui"):
        from flixcli.gui import main as gui_main
        return gui_main()
    from flixcli.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
