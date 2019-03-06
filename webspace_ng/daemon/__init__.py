import logging
import signal
import threading
import argparse

from munch import Munch
import yaml

from ..unixrpc import ThreadedUnixRPCServer
from . import webspace

is_shutdown = False
def shutdown():
    global is_shutdown
    is_shutdown = True

    logging.info('shutting down...')
    server.shutdown()

def sig_handler(_num, _frame):
    if not is_shutdown:
        threading.Thread(target=shutdown).start()

def merge(source, destination):
    """
    run me with nosetests --with-doctest file.py

    >>> a = { 'first' : { 'all_rows' : { 'pass' : 'dog', 'number' : '1' } } }
    >>> b = { 'first' : { 'all_rows' : { 'fail' : 'cat', 'number' : '5' } } }
    >>> merge(b, a) == { 'first' : { 'all_rows' : { 'pass' : 'dog', 'fail' : 'cat', 'number' : '5' } } }
    True
    """
    for key, value in source.items():
        if isinstance(value, dict):
            # get node or create one
            node = destination.setdefault(key, {})
            merge(value, node)
        else:
            destination[key] = value

    return destination
def load_config():
    config = {
        'bind_socket': '/var/lib/webspace-ng/unix.socket',
        'lxd': {
            'socket': '/var/lib/lxd/unix.socket',
            'profile': 'webspace',
            'suffix': '-ws'
        }
    }

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-c', '--config', help='Path to config file', default='/etc/webspaced.yaml')
    parser.add_argument('-v', '--verbose', action='count', help='Print more detailed log messages')
    parser.add_argument('-b', '--bind', dest='bind_socket',
                          help='Path to the Unix socket to bind on (default {})'.format(config['bind_socket']))
    parser.add_argument('-s', '--lxd-socket', dest='lxd_socket',
                          help='Path to the LXD Unix socket (default {})'.format(config['lxd']['socket']))
    args = parser.parse_args()

    with open(args.config) as conf:
        yaml_dict = yaml.safe_load(conf)
        if yaml_dict is not None:
            merge(yaml_dict, config)

    config = Munch.fromDict(config)
    if args.bind_socket is not None:
        config.bind_socket = args.bind_socket
    if args.lxd_socket is not None:
        config.lxd.socket = args.lxd_socket

    level = logging.INFO
    if args.verbose and args.verbose >= 1:
        level = logging.DEBUG
    logging.basicConfig(level=level, format='[{asctime:s}] {levelname:s}: {message:s}', style='{')

    return config

def main():
    config = load_config()

    global server
    server = ThreadedUnixRPCServer(config.bind_socket, allow_none=True)
    manager = webspace.Manager(config, server)

    # Shutdown handler
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    server.register_instance(manager)

    # RPC main loop
    server.serve_forever()
    server.server_close()

    manager.stop()