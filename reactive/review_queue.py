import configparser
import os
import shutil
import subprocess
import tempfile

from charmhelpers.core.hookenv import charm_dir
from charmhelpers.core.hookenv import close_port
from charmhelpers.core.hookenv import config
from charmhelpers.core.unitdata import kv
from charmhelpers.core.hookenv import log
from charmhelpers.core.hookenv import open_port
from charmhelpers.core.hookenv import status_set

from charmhelpers.core.host import chownr
from charmhelpers.core.host import service_restart
from charmhelpers.core.host import service_running
from charmhelpers.core.host import service_stop
from charmhelpers.core.host import init_is_systemd

from charmhelpers.fetch import install_remote

from charms.reactive import when
from charms.reactive import when_not
from charms.reactive import relations
from charms.reactive import set_state
from charms.reactive import is_state


config = config()
kvdb = kv()

DB_NAME = 'reviewqueue'
DB_ROLE = 'reviewqueue'

APP_DIR = '/opt/reviewqueue'
APP_INI_SRC = os.path.join(APP_DIR, 'production.ini')
APP_INI_DEST = '/etc/reviewqueue.ini'
APP_USER = 'ubuntu'
APP_GROUP = 'ubuntu'

SERVICE = 'reviewqueue'
TASK_SERVICE = 'reviewqueue-tasks'

UPSTART_FILE = '{}.conf'.format(SERVICE)
UPSTART_SRC = os.path.join(charm_dir(), 'files', 'upstart', UPSTART_FILE)
UPSTART_DEST = os.path.join('/etc/init', UPSTART_FILE)

UPSTART_TASK_FILE = '{}.conf'.format(TASK_SERVICE)
UPSTART_TASK_SRC = os.path.join(charm_dir(), 'files', 'upstart',
                                UPSTART_TASK_FILE)
UPSTART_TASK_DEST = os.path.join('/etc/init', UPSTART_TASK_FILE)

SYSTEMD_FILE = '{}.service'.format(SERVICE)
SYSTEMD_SRC = os.path.join(charm_dir(), 'files', 'systemd', SYSTEMD_FILE)
SYSTEMD_DEST = os.path.join('/etc/systemd/system', SYSTEMD_FILE)

SYSTEMD_TASK_FILE = '{}.service'.format(TASK_SERVICE)
SYSTEMD_TASK_SRC = os.path.join(charm_dir(), 'files', 'systemd',
                                SYSTEMD_TASK_FILE)
SYSTEMD_TASK_DEST = os.path.join('/etc/systemd/system', SYSTEMD_TASK_FILE)

LP_CREDS_FILE = 'lp-creds'
LP_CREDS_SRC = os.path.join(charm_dir(), 'files', LP_CREDS_FILE)
LP_CREDS_DEST = os.path.join(APP_DIR, LP_CREDS_FILE)


# List of app .ini keys that map to charm config.yaml keys
CFG_INI_KEYS = [
    'port',
    'base_url',
    'charmstore.api.url',
    'launchpad.api.url',
    'testing.timeout',
    'testing.substrates',
    'testing.default_substrates',
    'testing.jenkins_url',
    'testing.jenkins_token',
    'sendgrid.api_key',
    'sendgrid.from_email',
]

# Map ini key to the ini section it goes in. For ini keys not
# listed here, default section is 'app:main'
INI_SECTIONS = {
    'port': 'server:main',
}


@when('config.changed.repo')
def install_review_queue():
    status_set('maintenance', 'Installing Review Queue')

    with tempfile.TemporaryDirectory() as tmp_dir:
        install_dir = install_remote(config['repo'], dest=tmp_dir)
        contents = os.listdir(install_dir)
        if install_dir == tmp_dir and len(contents) == 1:
            # unlike the git handler, the archive handler just returns tmp_dir
            # even if the archive contents are nested in a folder as they
            # should be, so we have to normalize for that here
            install_dir = os.path.join(install_dir, contents[0])
        shutil.rmtree(APP_DIR, ignore_errors=True)
        log('Moving app source from {} to {}'.format(
            install_dir, APP_DIR))
        shutil.move(install_dir, APP_DIR)
    subprocess.check_call('make .venv'.split(), cwd=APP_DIR)
    if init_is_systemd():
        shutil.copyfile(SYSTEMD_SRC, SYSTEMD_DEST)
        shutil.copyfile(SYSTEMD_TASK_SRC, SYSTEMD_TASK_DEST)
    else:
        shutil.copyfile(UPSTART_SRC, UPSTART_DEST)
        shutil.copyfile(UPSTART_TASK_SRC, UPSTART_TASK_DEST)
    shutil.copyfile(LP_CREDS_SRC, LP_CREDS_DEST)
    shutil.copyfile(APP_INI_SRC, APP_INI_DEST)
    chownr(APP_DIR, APP_USER, APP_GROUP)

    set_state('reviewqueue.installed')


@when('config.changed', 'reviewqueue.installed')
def change_config():
    changes = []

    for ini_key in CFG_INI_KEYS:
        cfg_key = ini_key.replace('.', '_')
        if config.changed(cfg_key):
            changes.append((ini_key, config[cfg_key]))

    if changes:
        update_ini(changes)
        for change in changes:
            after_config_change(change[0])

        if is_state('db.database.available'):
            restart_web_service()

            if is_state('amqp.available'):
                service_restart(TASK_SERVICE)


@when('website.available')
def configure_website(http):
    http.configure(config['port'])


@when('amqp.connected')
def setup_amqp(amqp):
    amqp.request_access(
        username='reviewqueue',
        vhost='reviewqueue')


@when('amqp.available')
def configure_amqp(amqp):
    amqp_uri = 'amqp://{}:{}@{}:{}/{}'.format(
        amqp.username(),
        amqp.password(),
        amqp.private_address(),
        '5672',  # amqp.port() not available?
        amqp.vhost(),
    )

    if (kvdb.get('amqp_uri') != amqp_uri or
            not service_running(TASK_SERVICE)):
        kvdb.set('amqp_uri', amqp_uri)

        update_ini([
            ('broker', amqp_uri),
            ('backend', 'rpc://'),
        ], section='celery')

        if service_running(SERVICE):
            service_restart(TASK_SERVICE)


@when_not('amqp.available')
def stop_task_service():
    kvdb.set('amqp_uri', None)
    if service_running(TASK_SERVICE):
        service_stop(TASK_SERVICE)


@when('db.database.available')
def configure_db(db):
    db_uri = 'postgresql://{}:{}@{}:{}/{}'.format(
        db.user(),
        db.password(),
        db.host(),
        db.port(),
        db.database(),
    )

    if (kvdb.get('db_uri') != db_uri or
            not service_running(SERVICE)):
        kvdb.set('db_uri', db_uri)

        update_ini([
            ('sqlalchemy.url', db_uri),
        ])

        # initialize the DB
        subprocess.check_call(['/opt/reviewqueue/.venv/bin/initialize_db',
                               '/etc/reviewqueue.ini'])
        restart_web_service()


@when_not('db.database.available')
def stop_web_service():
    kvdb.set('db_uri', None)
    if service_running(SERVICE):
        service_stop(SERVICE)
    status_set('waiting', 'Waiting for database')


def restart_web_service():
    started = service_restart(SERVICE)
    if started:
        status_set('active', 'Serving on port {port}'.format(**config))
    else:
        status_set('blocked', 'Service failed to start')
    return started


def update_ini(kv_pairs, section=None):
    ini_changed = False

    ini = configparser.RawConfigParser()
    ini.read(APP_INI_DEST)

    for k, v in kv_pairs:
        this_section = INI_SECTIONS.get(k, section) or 'app:main'
        curr_val = ini.get(this_section, k)
        if curr_val != v:
            ini_changed = True
            log('[{}] {} = {}'.format(this_section, k, v))
            ini.set(this_section, k, v)

    if ini_changed:
        with open(APP_INI_DEST, 'w') as f:
            ini.write(f)


def after_config_change(config_key):
    if config_key == 'port':
        open_port(config['port'])
        if config.previous('port'):
            close_port(config.previous('port'))
        http = relations.RelationBase.from_state('website.available')
        if http:
            http.configure(config['port'])
