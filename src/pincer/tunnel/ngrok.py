"""Built-in ngrok tunnel for local development.

Activated only when PINCER_NGROK_AUTHTOKEN is set; completely inert otherwise
so cloud and production deployments are unaffected.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class NgrokTunnel:
    """Open a single ngrok tunnel to target_port and expose the public URL.

    No-op when authtoken is empty — safe to construct and call unconditionally.
    """

    def __init__(self, authtoken: str, domain: str, target_port: int) -> None:
        self._authtoken = authtoken
        self._domain = domain
        self._target_port = target_port
        self._tunnel = None

    async def start(self) -> str:
        """Open the tunnel and return the public HTTPS URL, or '' if disabled."""
        if not self._authtoken:
            logger.debug("NgrokTunnel: no authtoken — tunnel disabled")
            return ""

        try:
            from pyngrok import ngrok as _ngrok
        except ImportError:
            raise RuntimeError(
                "pyngrok is not installed. Run: pip install 'pincer-agent[local]'"
            ) from None
        _ngrok.set_auth_token(self._authtoken)

        kwargs: dict = {
            "proto": "http",
        }
        if self._domain:
            kwargs["domain"] = self._domain

        self._tunnel = _ngrok.connect(self._target_port, **kwargs)
        public_url: str = self._tunnel.public_url
        if public_url.startswith("http://"):
            public_url = "https://" + public_url[7:]
        logger.info("NgrokTunnel: public URL → %s", public_url)
        return public_url

    async def stop(self) -> None:
        """Disconnect the tunnel; idempotent."""
        if self._tunnel is None:
            return
        try:
            from pyngrok import ngrok as _ngrok

            _ngrok.disconnect(self._tunnel.public_url)
        except Exception as e:
            logger.debug("NgrokTunnel stop error: %s", e)
        finally:
            self._tunnel = None
