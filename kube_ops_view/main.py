#!/usr/bin/env python3

import gevent.monkey

gevent.monkey.patch_all()

import click
import flask
import functools
import gevent
import gevent.wsgi
import json
import json_delta
import logging
import os
import signal
import time
import kube_ops_view
from pathlib import Path

from flask import Flask, redirect
from flask_oauthlib.client import OAuth
from .oauth import OAuthRemoteAppWithRefresh
from urllib.parse import urljoin

from .mock import get_mock_clusters
from .kubernetes import get_kubernetes_clusters
from .stores import MemoryStore, RedisStore
from .cluster_discovery import DEFAULT_CLUSTERS, StaticClusterDiscoverer, ClusterRegistryDiscoverer


logger = logging.getLogger(__name__)

SERVER_STATUS = {'shutdown': False}
AUTHORIZE_URL = os.getenv('AUTHORIZE_URL')
APP_URL = os.getenv('APP_URL')

app = Flask(__name__)

oauth = OAuth(app)

auth = OAuthRemoteAppWithRefresh(
    oauth,
    'auth',
    request_token_url=None,
    access_token_method='POST',
    access_token_url=os.getenv('ACCESS_TOKEN_URL'),
    authorize_url=AUTHORIZE_URL
)
oauth.remote_apps['auth'] = auth


def authorize(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if AUTHORIZE_URL and 'auth_token' not in flask.session:
            return redirect(urljoin(APP_URL, '/login'))
        return f(*args, **kwargs)

    return wrapper


@app.route('/health')
def health():
    if SERVER_STATUS['shutdown']:
        flask.abort(503)
    else:
        return 'OK'


@app.route('/')
@authorize
def index():
    static_build_path = Path(__file__).parent / 'static' / 'build'
    candidates = sorted(static_build_path.glob('app*.js'))
    if candidates:
        app_js = candidates[0].name
        if app.debug:
            # cache busting for local development
            app_js += '?_={}'.format(time.time())
    else:
        logger.error('Could not find JavaScript application bundle app*.js in {}'.format(static_build_path))
        flask.abort(503, 'JavaScript application bundle not found (missing build)')
    return flask.render_template('index.html', app_js=app_js, version=kube_ops_view.__version__)


def event(cluster_ids: set):
    # first sent full data once
    for cluster_id in (app.store.get('cluster-ids') or []):
        if not cluster_ids or cluster_id in cluster_ids:
            cluster = app.store.get(cluster_id)
            yield 'event: clusterupdate\ndata: ' + json.dumps(cluster, separators=(',', ':')) + '\n\n'
    while True:
        for event_type, event_data in app.store.listen():
            # hacky, event_data can be delta or full cluster object
            if not cluster_ids or event_data.get('cluster_id', event_data.get('id')) in cluster_ids:
                yield 'event: ' + event_type + '\ndata: ' + json.dumps(event_data, separators=(',', ':')) + '\n\n'


@app.route('/events')
@authorize
def get_events():
    '''SSE (Server Side Events), for an EventSource'''
    cluster_ids = set()
    for _id in flask.request.args.get('cluster_ids', '').split():
        if _id:
            cluster_ids.add(_id)
    return flask.Response(event(cluster_ids), mimetype='text/event-stream')


@app.route('/screen-tokens', methods=['GET', 'POST'])
@authorize
def screen_tokens():
    new_token = None
    if flask.request.method == 'POST':
        new_token = app.store.create_screen_token()
    return flask.render_template('screen-tokens.html', new_token=new_token)


@app.route('/screen/<token>')
def redeem_screen_token(token: str):
    remote_addr = flask.request.headers.get('X-Forwarded-For') or flask.request.remote_addr
    logger.info('Trying to redeem screen token "{}" for IP {}..'.format(token, remote_addr))
    try:
        app.store.redeem_screen_token(token, remote_addr)
    except:
        flask.abort(401)
    flask.session['auth_token'] = (token, '')
    return redirect(urljoin(APP_URL, '/'))


@app.route('/login')
def login():
    redirect_uri = urljoin(APP_URL, '/login/authorized')
    return auth.authorize(callback=redirect_uri)


@app.route('/logout')
def logout():
    flask.session.pop('auth_token', None)
    return redirect(urljoin(APP_URL, '/'))


@app.route('/login/authorized')
def authorized():
    resp = auth.authorized_response()
    if resp is None:
        return 'Access denied: reason=%s error=%s' % (
            flask.request.args['error'],
            flask.request.args['error_description']
        )
    if not isinstance(resp, dict):
        return 'Invalid auth response'
    flask.session['auth_token'] = (resp['access_token'], '')
    return redirect(urljoin(APP_URL, '/'))


def update(cluster_discoverer, store, mock: bool):
    while True:
        lock = store.acquire_lock()
        if lock:
            try:
                if mock:
                    _clusters = get_mock_clusters()
                else:
                    _clusters = get_kubernetes_clusters(cluster_discoverer)
                cluster_ids = []
                for cluster in _clusters:
                    old_data = store.get(cluster['id'])
                    if old_data:
                        # https://pikacode.com/phijaro/json_delta/ticket/11/
                        # diff is extremely slow without array_align=False
                        delta = json_delta.diff(old_data, cluster, verbose=app.debug, array_align=False)
                        store.publish('clusterdelta', {'cluster_id': cluster['id'], 'delta': delta})
                    else:
                        store.publish('clusterupdate', cluster)
                    store.set(cluster['id'], cluster)
                    cluster_ids.append(cluster['id'])
                store.set('cluster-ids', cluster_ids)
            except:
                logger.exception('Failed to update')
            finally:
                store.release_lock(lock)
        gevent.sleep(5)


def shutdown():
    # just wait some time to give Kubernetes time to update endpoints
    # this requires changing the readinessProbe's
    # PeriodSeconds and FailureThreshold appropriately
    # see https://godoc.org/k8s.io/kubernetes/pkg/api/v1#Probe
    gevent.sleep(10)
    exit(0)


def exit_gracefully(signum, frame):
    logger.info('Received TERM signal, shutting down..')
    SERVER_STATUS['shutdown'] = True
    gevent.spawn(shutdown)


def print_version(ctx, param, value):
    if not value or ctx.resilient_parsing:
        return
    click.echo('Kubernetes Operational View {}'.format(kube_ops_view.__version__))
    ctx.exit()


@click.command(context_settings={'help_option_names': ['-h', '--help']})
@click.option('-V', '--version', is_flag=True, callback=print_version, expose_value=False, is_eager=True,
              help='Print the current version number and exit.')
@click.option('-p', '--port', type=int, help='HTTP port to listen on (default: 8080)', envvar='SERVER_PORT', default=8080)
@click.option('-d', '--debug', is_flag=True, help='Run in debugging mode', envvar='DEBUG')
@click.option('-m', '--mock', is_flag=True, help='Mock Kubernetes clusters', envvar='MOCK')
@click.option('--secret-key', help='Secret key for session cookies', envvar='SECRET_KEY', default='development')
@click.option('--redis-url', help='Redis URL to use for pub/sub and job locking', envvar='REDIS_URL')
@click.option('--clusters', help='Comma separated list of Kubernetes API server URLs (default: {})'.format(DEFAULT_CLUSTERS),
              envvar='CLUSTERS')
@click.option('--cluster-registry-url', help='URL to cluster registry', envvar='CLUSTER_REGISTRY_URL')
def main(port, debug, mock, secret_key, redis_url, clusters, cluster_registry_url):
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO)

    store = RedisStore(redis_url) if redis_url else MemoryStore()

    app.debug = debug
    app.secret_key = secret_key
    app.store = store

    if cluster_registry_url:
        discoverer = ClusterRegistryDiscoverer(cluster_registry_url)
    else:
        api_server_urls = clusters.split(',') if clusters else []
        discoverer = StaticClusterDiscoverer(api_server_urls)

    gevent.spawn(update, cluster_discoverer=discoverer, store=store, mock=mock)

    signal.signal(signal.SIGTERM, exit_gracefully)
    http_server = gevent.wsgi.WSGIServer(('0.0.0.0', port), app)
    logger.info('Listening on :{}..'.format(port))
    http_server.serve_forever()