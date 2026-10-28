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
from flixcli.daemon import main
sys.exit(main())
