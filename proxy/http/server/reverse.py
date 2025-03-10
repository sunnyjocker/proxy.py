# -*- coding: utf-8 -*-
"""
    proxy.py
    ~~~~~~~~
    ⚡⚡⚡ Fast, Lightweight, Pluggable, TLS interception capable proxy server focused on
    Network monitoring, controls & Application development, testing, debugging.

    :copyright: (c) 2013-present by Abhinav Singh and contributors.
    :license: BSD, see LICENSE for more details.
"""
import re
import random
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Optional

from proxy.http import Url
from proxy.core.base import TcpUpstreamConnectionHandler
from proxy.http.parser import HttpParser, httpParserTypes
from proxy.http.server import HttpWebServerBasePlugin, httpProtocolTypes
from proxy.common.utils import text_
from proxy.http.exception import HttpProtocolException
from proxy.common.constants import (
    HTTPS_PROTO, DEFAULT_HTTP_PORT, DEFAULT_HTTPS_PORT,
    DEFAULT_REVERSE_PROXY_ACCESS_LOG_FORMAT,
)


if TYPE_CHECKING:   # pragma: no cover
    from .plugin import ReverseProxyBasePlugin


logger = logging.getLogger(__name__)


class ReverseProxy(TcpUpstreamConnectionHandler, HttpWebServerBasePlugin):
    """Extend in-built Web Server to add Reverse Proxy capabilities."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.choice: Optional[Url] = None
        self.plugins: List['ReverseProxyBasePlugin'] = []
        for klass in self.flags.plugins[b'ReverseProxyBasePlugin']:
            plugin: 'ReverseProxyBasePlugin' = klass(
                self.uid, self.flags, self.client, self.event_queue, self.upstream_conn_pool,
            )
            self.plugins.append(plugin)

    def handle_upstream_data(self, raw: memoryview) -> None:
        # Parse response and implement plugin hook per parsed response object
        # This will give plugins a chance to modify the responses before dispatching to client             
        response = HttpParser(httpParserTypes.RESPONSE_PARSER)
        logger.debug("Parsing the upstream data...")
        response.parse(raw)
        if response.is_complete:
                logger.debug(
                    "Response completed with status code %s",
                    response.code,
                )         

        for plugin in self.plugins:
            r = plugin.after_handling_upstream_data(response)
            if r is None:
                self.client.queue(raw)
                return 
            response = r   

        self.client.queue(memoryview(response.build_response()))

    def routes(self) -> List[Tuple[int, str]]:
        r = []
        for plugin in self.plugins:
            for route in plugin.regexes():
                r.append((httpProtocolTypes.HTTP, route))
                r.append((httpProtocolTypes.HTTPS, route))
        return r

    def handle_request(self, request: HttpParser) -> None:
        # before_routing
        for plugin in self.plugins:
            r = plugin.before_routing(request)
            if r is None:
                raise HttpProtocolException('before_routing closed connection')
            request = r

        # routes
        for plugin in self.plugins:
            for route in plugin.routes():
                if isinstance(route, tuple):
                    pattern = re.compile(route[0])
                    if pattern.match(text_(request.path)):
                        self.choice = Url.from_bytes(
                            random.choice(route[1]),
                        )
                        break
                elif isinstance(route, str):
                    pattern = re.compile(route)
                    if pattern.match(text_(request.path)):
                        self.choice = plugin.handle_route(request, pattern)
                        break
                else:
                    raise ValueError('Invalid route')

        assert self.choice and self.choice.hostname
        port = self.choice.port or \
            DEFAULT_HTTP_PORT \
            if self.choice.scheme == b'http' \
            else DEFAULT_HTTPS_PORT
        self.initialize_upstream(text_(self.choice.hostname), port)
        assert self.upstream
        try:
            self.upstream.connect()
            if self.choice.scheme == HTTPS_PROTO:
                self.upstream.wrap(
                    text_(
                        self.choice.hostname,
                    ),
                    as_non_blocking=True,
                    ca_file=self.flags.ca_file,
                )
            request.path = self.choice.remainder
            self.upstream.queue(memoryview(request.build()))
        except ConnectionRefusedError:
            raise HttpProtocolException(    # pragma: no cover
                'Connection refused by upstream server {0}:{1}'.format(
                    text_(self.choice.hostname), port,
                ),
            )

    def on_client_connection_close(self) -> None:
        if self.upstream and not self.upstream.closed:
            logger.debug('Closing upstream server connection')
            self.upstream.close()
            self.upstream = None

    def on_access_log(self, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        context.update({
            'upstream_proxy_pass': str(self.choice) if self.choice else None,
        })
        logger.info(DEFAULT_REVERSE_PROXY_ACCESS_LOG_FORMAT.format_map(context))
        return None
