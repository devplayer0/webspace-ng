from functools import wraps
import sys
import os
import signal
import termios
import tty
import socket
import select
import shutil

from humanfriendly import format_size
from eventfd import EventFD

from .. import WebspaceError
from .client import Client

CONSOLE_ESCAPE = b'\x1d'
CONSOLE_ESCAPE_QUIT = b'q'

def ask(question, default="yes"):
    """Ask a yes/no question via input() and return their answer.

    "question" is a string that is presented to the user.
    "default" is the presumed answer if the user just hits <Enter>.
        It must be "yes" (the default), "no" or None (meaning
        an answer is required of the user).

    The "answer" return value is True for "yes" or False for "no".
    """
    valid = {"yes": True, "y": True, "ye": True,
             "no": False, "n": False}
    if default is None:
        prompt = " [y/n] "
    elif default == "yes":
        prompt = " [Y/n] "
    elif default == "no":
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '{}'".format(default))

    while True:
        print(question + prompt, end='')
        choice = input().lower()
        if default is not None and choice == '':
            return valid[default]
        elif choice in valid:
            return valid[choice]
        else:
            print("Please respond with 'yes' or 'no' "
                             "(or 'y' or 'n').")

class process:
    def __init__(self, message, done=' done.'):
        self.message = message
        self.done = done
    def __enter__(self):
        print(self.message, end='')
        sys.stdout.flush()
        return self
    def __exit__(self, ex_type, e_value, trace):
        if not e_value:
            print(self.done)
        else:
            print()

def find_image(client, id_):
    image_list = client.images()
    # First try to find it by an alias
    for i in image_list:
        for a in i['aliases']:
            if a['name'] == id_:
                return i

    # Otherwise by fingerprint
    for i in image_list:
        if i['fingerprint'] == id_:
            return i

    return None

def cmd(f):
    @wraps(f)
    def wrapper(args):
        user = args.user if 'user' in args else None
        with Client(args.socket_path, user=user) as client:
            try:
                return f(client, args)
            except Exception as ex:
                print('Error: {}'.format(ex), file=sys.stderr)
    return wrapper

@cmd
def images(client, _args):
    image_list = client.images()
    print('Available images: ')
    for image in image_list:
        print(' - Fingerprint: {}'.format(image['fingerprint']))
        if image['aliases']:
            aliases = map(lambda a: a['name'], image['aliases'])
            print('   Aliases: {}'.format(', '.join(aliases)))
        if 'description' in image['properties']:
            print('   Description: {}'.format(image['properties']['description']))
        print('   Size: {}'.format(format_size(image['size'], binary=True)))

@cmd
def init(client, args):
    with process('Creating your container...', done=' success!'):
        image = find_image(client, args.image)
        if image is None:
            raise WebspaceError('"{}" is not a valid image alias / fingerprint'.format(args.image))

        client.init(image['fingerprint'])

@cmd
def status(client, _args):
    info = client.status()
    print('Container status: {}'.format(info['status']))
    if info['disk']:
        print('Disks:')
        for name, data in info['disk'].items():
            print(' - {}: Used {}'.format(name, format_size(data['usage'], binary=True)))
    print('Memory use: {}'.format(format_size(info['memory']['usage'], binary=True)))
    print('Running processes: {}'.format(info['processes']))
    if info['network'] and not (len(info['network']) == 1 and 'lo' in info['network']):
        print('Network interfaces:')
        for name, data in info['network'].items():
            if name == 'lo':
                continue
            print(' - {} ({}):'.format(name, data['hwaddr']))
            print('   Sent/received: {}/{}'.format(
                                                   format_size(data['counters']['bytes_sent'], binary=True),
                                                   format_size(data['counters']['bytes_received'], binary=True)))
            for addr in data['addresses']:
                print('   IPv{} address: {}/{}'.format('6' if addr['family'] == 'inet6' else '4',
                                                    addr['address'], addr['netmask']))

@cmd
def log(client, _args):
    print(client.log())

@cmd
def console(client, _args):
    print('Attaching to console...')
    t_width, t_height = shutil.get_terminal_size()
    sock_path = client.console(t_width, t_height)
    def notify_resize(_signum, _frame):
        t_width, t_height = shutil.get_terminal_size()
        client.console_resize(t_width, t_height)
    # SIGWINCH is sent when the terminal is resized
    signal.signal(signal.SIGWINCH, notify_resize)

    # Establish the terminal pipe connection
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)

    stdin = sys.stdin.fileno()
    old = termios.tcgetattr(stdin)
    tty.setraw(stdin, when=termios.TCSANOW)

    should_quit = EventFD()
    def trigger_quit(_signum, _frame):
        should_quit.set()
    signal.signal(signal.SIGINT, trigger_quit)
    signal.signal(signal.SIGTERM, trigger_quit)
    print('Attached, hit ^] (Ctrl+]) and then q to disconnect', end='\r\n')

    try:
        escape_read = False
        while True:
            r, _, _ = select.select([should_quit, sys.stdin, sock], [], [])
            if should_quit in r:
                break
            if sys.stdin in r:
                data = os.read(stdin, 1)
                if escape_read:
                    if data == CONSOLE_ESCAPE_QUIT:
                        # The user wants to quit
                        break

                    # They don't want to quit, lets send the escape key along with their data
                    sock.sendall(CONSOLE_ESCAPE + data)
                    escape_read = False
                elif data == CONSOLE_ESCAPE:
                    escape_read = True
                else:
                    sock.sendall(data)
            if sock in r:
                data = sock.recv(4096)
                if not data:
                    break

                sys.stdout.buffer.write(data)
                sys.stdout.flush()
    finally:
        # Restore the terminal to its original state
        termios.tcsetattr(stdin, termios.TCSANOW, old)
        sock.close()

@cmd
def shutdown(client, _args):
    with process('Shutting your container down...'):
        client.shutdown()

@cmd
def reboot(client, _args):
    with process('Rebooting your container...'):
        client.reboot()

@cmd
def delete(client, _args):
    if not ask('Are you sure?', default='no'):
        return

    with process('Deleting your container...'):
        client.delete()

@cmd
def config_show(client, args):
    config = client.get_config()
    print('Container configuration:')
    for k, v in config.items():
        print('{}: {}'.format(k, v))
@cmd
def config_set(client, args):
    client.set_option(args.key, args.value)
@cmd
def config_unset(client, args):
    client.unset_option(args.key)
