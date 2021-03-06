from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging
import datetime
import sys
import os

from flask import Flask, json, request
from flask_misaka import Misaka
from flask_cors import CORS
from rate_limit_quotas import FlaskLimiter

import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.logging import ignore_logger

from decimal import Decimal
from measurements.database import init_db
from measurements.config import metrics

APP_DIR = os.path.dirname(__file__)


class FlaskJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, datetime.datetime):
            if o.tzinfo:
                # eg: '2015-09-25T23:14:42.588601+00:00'
                return o.isoformat("T")
            else:
                # No timezone present - assume UTC.
                # eg: '2015-09-25T23:14:42.588601Z'
                return o.isoformat("T") + "Z"

        if isinstance(o, datetime.date):
            return o.isoformat()

        if isinstance(o, Decimal):
            return float(o)

        if isinstance(o, set):
            return list(o)

        return json.JSONEncoder.default(self, o)


def init_app(app, testmode=False):
    # We load configurations first from the config file (where some options
    # are overridable via environment variables) or from the config file
    # pointed to by the MEASUREMENTS_CONFIG environment variable.
    # The later overrides the former.
    app.config.from_object("measurements.config")
    app.config.from_envvar("MEASUREMENTS_CONFIG", silent=True)

    # Prevent messy duplicate logs during testing
    if not testmode:
        app.logger.addHandler(logging.StreamHandler())

    if app.config["APP_ENV"] == "production":
        app.logger.setLevel(logging.WARNING)
    elif app.config["APP_ENV"] == "development":
        app.logger.setLevel(logging.DEBUG)
        # Set the jinja templates to reload when in development
        app.jinja_env.auto_reload = True
        app.config["TEMPLATES_AUTO_RELOAD"] = True
        app.config["DEBUG"] = True
    elif app.config["APP_ENV"] not in (
        "testing",
        "staging",
    ):  # known envs according to Readme.md
        raise RuntimeError("Unexpected APP_ENV", app.config["APP_ENV"])

    for key in app.config.keys():
        SECRET_SUBSTRINGS = ["_SECRET_", "DATABASE_URL"]
        # Do not log, even in debug, anything containing the word "SECRET" or "DATABASE_URL"
        if any([s in key for s in SECRET_SUBSTRINGS]):
            continue
        app.logger.debug("{}: {}".format(key, app.config[key]))

    if app.config["APP_ENV"] == "production":
        sentry_sdk.init(
            dsn="https://dcb077b34ac140d58a7c37609cea0cf9@sentry.io/1367288",
            integrations=[FlaskIntegration()],
        )
        # TODO Temporary workaround to ignore flask-limiter logs due to:
        # https://github.com/ooni/api/issues/145 &
        # https://github.com/alisaifee/flask-limiter/issues/186
        ignore_logger("flask-limiter")

    md = Misaka(fenced_code=True)
    md.init_app(app)

    CORS(app, resources={r"/api/*": {"origins": "*"}})


def check_config(config):
    pass


def create_app(*args, testmode=False, **kw):
    from measurements import views

    if sys.version_info[0] < 3:
        raise RuntimeError("Python >= 3 is required")

    app = Flask(__name__)
    app.json_encoder = FlaskJSONEncoder

    # Order matters
    init_app(app, testmode=testmode)
    check_config(app.config)

    # Setup Database connector
    init_db(app)

    # Setup rate limiting
    # NOTE: the limits apply per-process. The number of processes is set in:
    # https://github.com/ooni/sysadmin/blob/master/ansible/roles/ooni-measurements/tasks/main.yml
    limits = dict(
        ipaddr_per_month=6000,
        token_per_month=6000,
        ipaddr_per_week=2000,
        token_per_week=2000,
        ipaddr_per_day=400,
        token_per_day=500,
    )
    # Whitelist Prometheus and AMS Explorer
    # TODO: move addrs to an external config file /etc/ooniapi.conf ?
    whitelist = ["37.218.245.43", "37.218.242.149"]
    app.limiter = FlaskLimiter(limits=limits, app=app, whitelisted_ipaddrs=whitelist)

    # Lazy setup of the prometheus metrics collection
    if testmode == False:
        # TODO look into how we can improve the testing pattern or how we setup
        # prometheus metrics collection to avoid having to disable them
        metrics.init_app(app)

    views.register(app)

    # why is it `teardown_appcontext` and not `teardown_request` ?...
    @app.teardown_appcontext
    def shutdown_session(exception=None):
        app.db_session.remove()

    return app
