import asyncio
import inspect
import logging
import os
import random
import re
import socket
import string
import sys
import uuid

from contextlib import suppress
from logging import LogRecord
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import pytest

from sanic_routing.exceptions import RouteExists
from sanic_testing.testing import PORT

from sanic import Sanic
from sanic.constants import HTTP_METHODS
from sanic.logging.formatter import AutoFormatter
from sanic.router import Router
from sanic.touchup.service import TouchUp


slugify = re.compile(r"[^a-zA-Z0-9_\-]")
random.seed("Pack my box with five dozen liquor jugs.")
Sanic.test_mode = True

if sys.platform in ["win32", "cygwin"]:
    collect_ignore = ["test_worker.py"]


def get_port():
    sock = socket.socket()
    sock.bind(
        ("", 0)
    )  # Bind to 0 port, so os will pick available port for us.
    return sock.getsockname()[1]


@pytest.fixture(scope="function")
def port():
    yield get_port()


async def _handler(request):
    """
    Dummy placeholder method used for route resolver when creating a new
    route into the sanic router. This router is not actually called by the
    sanic app. So do not worry about the arguments to this method.

    If you change the return value of this method, make sure to propagate the
    change to any test case that leverages RouteStringGenerator.
    """
    return 1


TYPE_TO_GENERATOR_MAP = {
    "str": lambda: "".join(
        [random.choice(string.ascii_lowercase) for _ in range(4)]
    ),
    "int": lambda: random.choice(range(1000000)),
    "float": lambda: random.random(),
    "alpha": lambda: "".join(
        [random.choice(string.ascii_lowercase) for _ in range(4)]
    ),
    "uuid": lambda: str(uuid.uuid1()),
}

CACHE: dict[str, Any] = {}


class RouteStringGenerator:
    ROUTE_COUNT_PER_DEPTH = 100
    HTTP_METHODS = HTTP_METHODS
    ROUTE_PARAM_TYPES = ["str", "int", "float", "alpha", "uuid"]

    def generate_random_direct_route(self, max_route_depth=4):
        routes = []
        for depth in range(1, max_route_depth + 1):
            for _ in range(self.ROUTE_COUNT_PER_DEPTH):
                route = "/".join(
                    [TYPE_TO_GENERATOR_MAP.get("str")() for _ in range(depth)]
                )
                route = route.replace(".", "", -1)
                route_detail = (random.choice(self.HTTP_METHODS), route)

                if route_detail not in routes:
                    routes.append(route_detail)
        return routes

    def add_typed_parameters(self, current_routes, max_route_depth=8):
        routes = []
        for method, route in current_routes:
            current_length = len(route.split("/"))
            new_route_part = "/".join(
                [
                    "<{}:{}>".format(
                        TYPE_TO_GENERATOR_MAP.get("str")(),
                        random.choice(self.ROUTE_PARAM_TYPES),
                    )
                    for _ in range(max_route_depth - current_length)
                ]
            )
            route = "/".join([route, new_route_part])
            route = route.replace(".", "", -1)
            routes.append((method, route))
        return routes

    @staticmethod
    def generate_url_for_template(template):
        url = template
        for pattern, param_type in re.findall(
            re.compile(r"((?:<\w+:(str|int|float|alpha|uuid)>)+)"),
            template,
        ):
            value = TYPE_TO_GENERATOR_MAP.get(param_type)()
            url = url.replace(pattern, str(value), -1)
        return url


@pytest.fixture(scope="function")
def sanic_router(app):
    # noinspection PyProtectedMember
    def _setup(route_details: tuple) -> tuple[Router, tuple]:
        router = Router()
        router.ctx.app = app
        added_router = []
        for method, route in route_details:
            try:
                router.add(
                    uri=f"/{route}",
                    methods=frozenset({method}),
                    host="localhost",
                    handler=_handler,
                )
                added_router.append((method, route))
            except RouteExists:
                pass
        router.finalize()
        return router, tuple(added_router)

    return _setup


@pytest.fixture(scope="function")
def route_generator() -> RouteStringGenerator:
    return RouteStringGenerator()


@pytest.fixture(scope="function")
def url_param_generator():
    return TYPE_TO_GENERATOR_MAP


@pytest.fixture(scope="function")
def app(request):
    if not CACHE:
        for target, method_name in TouchUp._registry:
            CACHE[method_name] = getattr(target, method_name)
    app = Sanic(slugify.sub("-", request.node.name))

    yield app
    for target, method_name in TouchUp._registry:
        setattr(target, method_name, CACHE[method_name])
    Sanic._app_registry.clear()
    AutoFormatter.SETUP = False
    AutoFormatter.LOG_EXTRA = False
    os.environ.pop("SANIC_LOG_EXTRA", None)
    os.environ.pop("SANIC_NO_COLOR", None)


@pytest.fixture(scope="function")
def run_startup(caplog):
    def run(app):
        nonlocal caplog
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with caplog.at_level(logging.DEBUG):
            server = app.create_server(
                debug=True, return_asyncio_server=True, port=PORT
            )
            loop._stopping = False

            _server = loop.run_until_complete(server)

            _server.close()
            loop.run_until_complete(_server.wait_closed())
            app.stop()

        return caplog.record_tuples

    return run


@pytest.fixture
def run_multi(caplog):
    def run(app, level=logging.DEBUG):
        @app.after_server_start
        async def stop(app, _):
            app.stop()

        with caplog.at_level(level):
            Sanic.serve()

        return caplog.record_tuples

    return run


@pytest.fixture(scope="function")
def message_in_records():
    def msg_in_log(records: list[LogRecord], msg: str):
        error_captured = False
        for record in records:
            if msg in record.message:
                error_captured = True
                break
        return error_captured

    return msg_in_log


@pytest.fixture
def ext_instance():
    ext_instance = MagicMock()
    ext_instance.injection = MagicMock()
    return ext_instance


@pytest.fixture(autouse=True)  # type: ignore
def mock_sanic_ext(ext_instance):  # noqa
    mock_sanic_ext = MagicMock(__version__="1.2.3")
    mock_sanic_ext.Extend = MagicMock()
    mock_sanic_ext.Extend.return_value = ext_instance
    sys.modules["sanic_ext"] = mock_sanic_ext
    yield mock_sanic_ext
    with suppress(KeyError):
        del sys.modules["sanic_ext"]


@pytest.fixture
def urlopen():
    urlopen = Mock()
    urlopen.return_value = urlopen
    urlopen.__enter__ = Mock(return_value=urlopen)
    urlopen.__exit__ = Mock()
    urlopen.read = Mock()
    with patch("sanic.cli.inspector_client.urlopen", urlopen):
        yield urlopen


@pytest.fixture(scope="module")
def static_file_directory():
    """The static directory to serve"""
    current_file = inspect.getfile(inspect.currentframe())
    current_directory = os.path.dirname(os.path.abspath(current_file))
    static_directory = os.path.join(current_directory, "static")
    return static_directory
