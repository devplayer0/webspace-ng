from functools import wraps
import sys
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
            return f(client, args)
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
    print('Creating your container...')
    image = find_image(client, args.image)
    if image is None:
        raise WebspaceError('"{}" is not a valid image alias / fingerprint'.format(args.image))

    client.init(image['fingerprint'])
    print('Success!')

@cmd
def status(client, _):
    info = client.status()
    print('Container status: {}'.format(info))

@cmd
def console(client, _):
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
    tty.setraw(stdin)

    should_quit = EventFD()
    def trigger_quit(_signum, _frame):
        should_quit.set()
    signal.signal(signal.SIGINT, trigger_quit)
    signal.signal(signal.SIGTERM, trigger_quit)
    print('Attached, hit ^] (Ctrl+]) and then q to disconnect', end='\r\n')

    try:
        escape_read = False
        while True:
            r, _, _ = select.select([should_quit, stdin, sock], [], [])
            if should_quit in r:
                break
            if stdin in r:
                data = sys.stdin.buffer.read(1)
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
        termios.tcsetattr(stdin, termios.TCSADRAIN, old)
        sock.close()