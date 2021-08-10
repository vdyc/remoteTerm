#!/usr/bin/env python

# This tool reuse miniterm code and support command over network
# (C)2021 Yuanchao Dai

# Very simple serial terminal
#
# This file is part of pySerial. https://github.com/pyserial/pyserial
# (C)2002-2020 Chris Liechti <cliechti@gmx.net>
#
# SPDX-License-Identifier:    BSD-3-Clause

import serial
# import os
# import subprocess
import time
# import shutil
import re
import sys
import configparser
import signal
import atexit
import argparse
from datetime import datetime
from queue import Queue
from threading import Thread
from serial.tools.list_ports import comports
from serial.tools.miniterm import *
import zmq

config_file = r".\serial.cfg"
config = configparser.ConfigParser()
config.read(config_file)


class Alias(Transform):
    """Use alias for serial console commands"""
    def __init__(self):
        self.alias_dict = dict(config.items("ALIAS"))
        atexit.register(self.cleanup)
        f_out = os.path.join(os.path.dirname(sys.argv[0]), "log")
        if not os.path.exists(f_out):
            os.makedirs(f_out)
        self.log_file = open(os.path.join(f_out, datetime.now().strftime("%Y%m%d_%H%M%S.log")), "w+")

    def cleanup(self):
        self.log_file.close()

    def rx(self, text):
        self.log_file.write(text)
        if "\n" in text:
            self.log_file.flush()
        return text

    def tx(self, text):
        text = text.replace("\r\n", "\n")
        if text.endswith("\n"):
            if text.startswith("kieny_"):
                # For key input, letters come in separately, when "enter" pressed, need to reassemble the command
                text = text[len("kieny_"):]
                # print(f"text: {text.encode('ascii')}")
                text_strip = text[:-1]
                backspace_position = len(text_strip)
                if backspace_position and text_strip in self.alias_dict.keys():
                    text = '\x08' * backspace_position + self.alias_dict[text_strip] + "\n"
                else:
                    text = "\n"
            else:
                # For socket input, command always come in whole
                text_strip = text[:-1]
                text = self.alias_dict[text_strip] + "\n" if text_strip in self.alias_dict.keys() else text
        self.log_file.write(text)
        return text


class TeraTermCommandLine(Transform):
    """Support a few frequently used ttl command"""
    regex_awaiting = ""
    cur_line = ""
    regex_match = False

    def rx(self, text):
        self.cur_line += text
        new_line_started = True
        if "\n" in self.cur_line:
            if self.cur_line.endswith("\n"):
                new_line_started = False
            lines = self.cur_line.split("\n")
            # in case rx is multiline
            self.cur_line = lines[-1] if new_line_started else ""
            if self.regex_awaiting != "":
                for line in lines:
                    _find = re.findall(self.regex_awaiting, line)
                    if len(_find):
                        self.regex_match = True
                        self.regex_awaiting = ""
        return text

    def tx(self, text):
        def ttl_send_command(arg):
            _command = arg
            try:
                _command = arg.strip()[1:-1]
            except:
                print(f"Unable to parse command: {arg}")
            return _command

        def ttl_pause(arg):
            # Sleep here doesn't guarantee the gap between commands, better idea?
            time.sleep(int(arg))
            return ""

        def ttl_wait_regex(arg):
            try:
                _regex = arg.strip()[1:-1]
                self.regex_awaiting = _regex
                _start = datetime.utcnow()
                while not self.regex_match:
                    time.sleep(0.1)
                    if (datetime.utcnow() - _start).total_seconds() > int(config.get("TERATERM_TTL", "regex_timeout_sec")):
                        break
                self.regex_match = False
                self.regex_awaiting = ""
            except:
                print(f"Unable to parse command: {arg}")
            finally:
                return ""

        TTL = {"sendln ": ttl_send_command,
               "pause ": ttl_pause,
               "waitregex": ttl_wait_regex
               }

        _transformed = text
        for key in TTL.keys():
            if text.startswith(key):
                print(f"{key}: {text}")
                _transformed = TTL[key](text[len(key):])
        return _transformed


TRANSFORMATIONS = {
    'direct': Transform,    # no transformation
    'default': NoTerminal,
    'nocontrol': NoControls,
    'printable': Printable,
    'colorize': Colorize,
    'debug': DebugIO,
    'alias': Alias,
    'ttl': TeraTermCommandLine,
}


class RemoteTerm(Miniterm):
    def __init__(self, serial_instance):
        super().__init__(serial_instance, echo=False, eol='crlf', filters=config.get("MISC", "filter").split(";"))
        self.tx_q = Queue()
        self.socket = None

    def update_transformations(self):
        """take list of transformation classes and instantiate them for rx and tx"""
        transformations = [EOL_TRANSFORMATIONS[self.eol]] + [TRANSFORMATIONS[f] for f in self.filters]
        self.tx_transformations = [t() for t in transformations]
        self.rx_transformations = list(reversed(self.tx_transformations))

    def socket_input(self):
        context = zmq.Context()
        self.socket = context.socket(zmq.REP)
        self.socket.bind(config.get("SOCKET", "socket_port"))

        while True:
            #  Wait for next request from client
            command_in = self.socket.recv()
            self.tx_q.put(command_in.decode('ascii') + "\n")
            #  Send reply back to client
            self.socket.send(b"In queue")

    def keyboard_input(self):
        command_in = "kieny_"
        menu_active = False

        while self.alive:
            try:
                key_in = self.console.getkey()
                # print(f"key_in: {key_in} / {key_in.encode('ascii')}")
                command_in += key_in
                if menu_active:
                    self.handle_menu_key(key_in)
                    menu_active = False
                elif key_in == self.menu_character:
                    menu_active = True  # next char will be for menu
                elif key_in == self.exit_character:
                    # exit app
                    print("RemoteTerminal exit")
                    os.kill(os.getpid(), signal.SIGTERM)
                    break
                elif key_in in "\r\n":
                    self.tx_q.put(command_in)
                    command_in = "kieny_"
                else:
                    self.tx_q.put(key_in)
            except KeyboardInterrupt:
                self.tx_q.put('\x03')

    def reader(self):
        """loop and copy serial->console"""
        try:
            while self.alive:
                # Serial RX
                if self._reader_alive:
                    # read all that is there or wait for one byte
                    data = self.serial.read(self.serial.in_waiting or 1)
                    if data:
                        if self.raw:
                            self.console.write_bytes(data)
                        else:
                            text = self.rx_decoder.decode(data)
                            for transformation in self.rx_transformations:
                                try:
                                    text = transformation.rx(text)
                                except Exception as e:
                                    print(f"Unable to handle string: {text} \n{e}")
                                    test = ""
                                    pass
                            self.console.write(text)
        except serial.SerialException:
            print(f"{serial.SerialException}")
            self.alive = False
            self.console.cancel()
            raise       # XXX handle instead of re-raise?

    def writer(self):
        try:
            while self.alive:
                # Serial TX
                time.sleep(0.05)
                if not self.tx_q.empty():
                    c = self.tx_q.get()
                    text = c
                    for transformation in self.tx_transformations:
                        text = transformation.tx(text)
                    self.serial.write(self.tx_encoder.encode(text))
                    if self.echo:
                        echo_text = c
                        for transformation in self.tx_transformations:
                            echo_text = transformation.echo(echo_text)
                        self.console.write(echo_text)
        except:
            self.alive = False
            raise

    def _start_reader(self):
        """Start serial r/w thread"""
        self._reader_alive = True
        # start serial r/w thread
        self.receiver_thread = threading.Thread(target=self.reader, name='serial_comm')
        self.receiver_thread.daemon = True
        self.receiver_thread.start()

    def start(self):
        """start worker threads"""
        self.alive = True
        # enter console->serial loop
        self.socket_thread = threading.Thread(target=self.socket_input, name='socket')
        self.socket_thread.daemon = True
        self.socket_thread.start()
        self.keyboard_thread = threading.Thread(target=self.keyboard_input, name='keyboard')
        self.keyboard_thread.daemon = True
        self.keyboard_thread.start()
        self._start_reader()
        self.transmitter_thread = threading.Thread(target=self.writer, name='tx')
        self.transmitter_thread.daemon = True
        self.transmitter_thread.start()
        self.console.setup()

    def join(self, transmit_only=False):
        """wait for worker threads to terminate"""
        self.keyboard_thread.join()
        self.socket_thread.join()
        self.transmitter_thread.join()
        if not transmit_only:
            if hasattr(self.serial, 'cancel_read'):
                self.serial.cancel_read()
            self.receiver_thread.join()


# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
# default args can be used to override when calling main() from an other script
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
def main(default_port=None, default_baudrate=115200, default_rts=None, default_dtr=None):
    """Command line tool, entry point"""

    import argparse

    parser = argparse.ArgumentParser(
        description='RemoteTerm - A simple terminal program for the serial port.')

    parser.add_argument(
        'port',
        nargs='?',
        help='serial port name ("-" to show port list)',
        default=default_port)

    parser.add_argument(
        'baudrate',
        nargs='?',
        type=int,
        help='set baud rate, default: %(default)s',
        default=default_baudrate)

    group = parser.add_argument_group('port settings')

    group.add_argument(
        '--parity',
        choices=['N', 'E', 'O', 'S', 'M'],
        type=lambda c: c.upper(),
        help='set parity, one of {N E O S M}, default: N',
        default='N')

    group.add_argument(
        '--rtscts',
        action='store_true',
        help='enable RTS/CTS flow control (default off)',
        default=False)

    group.add_argument(
        '--xonxoff',
        action='store_true',
        help='enable software flow control (default off)',
        default=False)

    group.add_argument(
        '--rts',
        type=int,
        help='set initial RTS line state (possible values: 0, 1)',
        default=default_rts)

    group.add_argument(
        '--dtr',
        type=int,
        help='set initial DTR line state (possible values: 0, 1)',
        default=default_dtr)

    group.add_argument(
        '--non-exclusive',
        dest='exclusive',
        action='store_false',
        help='disable locking for native ports',
        default=True)

    group.add_argument(
        '--ask',
        action='store_true',
        help='ask again for port when open fails',
        default=False)

    group = parser.add_argument_group('data handling')

    group.add_argument(
        '-e', '--echo',
        action='store_true',
        help='enable local echo (default off)',
        default=False)

    group.add_argument(
        '--encoding',
        dest='serial_port_encoding',
        metavar='CODEC',
        help='set the encoding for the serial port (e.g. hexlify, Latin1, UTF-8), default: %(default)s',
        default='UTF-8')

    group.add_argument(
        '-f', '--filter',
        action='append',
        metavar='NAME',
        help='add text transformation',
        default=[])

    group.add_argument(
        '--eol',
        choices=['CR', 'LF', 'CRLF'],
        type=lambda c: c.upper(),
        help='end of line mode',
        default='CRLF')

    group.add_argument(
        '--raw',
        action='store_true',
        help='Do no apply any encodings/transformations',
        default=False)

    group = parser.add_argument_group('hotkeys')

    group.add_argument(
        '--exit-char',
        type=int,
        metavar='NUM',
        help='Unicode of special character that is used to exit the application, default: %(default)s',
        default=0x1d)  # GS/CTRL+]

    group.add_argument(
        '--menu-char',
        type=int,
        metavar='NUM',
        help='Unicode code of special character that is used to control RemoteTerm (menu), default: %(default)s',
        default=0x14)  # Menu: CTRL+T

    group = parser.add_argument_group('diagnostics')

    group.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='suppress non-error messages',
        default=False)

    group.add_argument(
        '--develop',
        action='store_true',
        help='show Python traceback on error',
        default=False)

    args = parser.parse_args()

    if args.menu_char == args.exit_char:
        parser.error('--exit-char can not be the same as --menu-char')

    if args.filter:
        if 'help' in args.filter:
            sys.stderr.write('Available filters:\n')
            sys.stderr.write('\n'.join(
                '{:<10} = {.__doc__}'.format(k, v)
                for k, v in sorted(TRANSFORMATIONS.items())))
            sys.stderr.write('\n')
            sys.exit(1)
        filters = args.filter
    else:
        filters = ['default']

    while True:
        # no port given on command line -> ask user now
        if args.port is None or args.port == '-':
            try:
                args.port = ask_for_port()
            except KeyboardInterrupt:
                sys.stderr.write('\n')
                parser.error('user aborted and port is not given')
            else:
                if not args.port:
                    parser.error('port is not given')
        try:
            serial_instance = serial.serial_for_url(
                args.port,
                args.baudrate,
                parity=args.parity,
                rtscts=args.rtscts,
                xonxoff=args.xonxoff,
                do_not_open=True)

            serial_instance.timeout = 0.1

            if args.dtr is not None:
                if not args.quiet:
                    sys.stderr.write('--- forcing DTR {}\n'.format('active' if args.dtr else 'inactive'))
                serial_instance.dtr = args.dtr
            if args.rts is not None:
                if not args.quiet:
                    sys.stderr.write('--- forcing RTS {}\n'.format('active' if args.rts else 'inactive'))
                serial_instance.rts = args.rts

            if isinstance(serial_instance, serial.Serial):
                serial_instance.exclusive = args.exclusive

            serial_instance.open()
        except serial.SerialException as e:
            sys.stderr.write('could not open port {!r}: {}\n'.format(args.port, e))
            if args.develop:
                raise
            if not args.ask:
                sys.exit(1)
            else:
                args.port = '-'
        else:
            break

    remoteTerm = RemoteTerm(serial_instance)
    remoteTerm.exit_character = unichr(args.exit_char)
    remoteTerm.menu_character = unichr(args.menu_char)
    remoteTerm.raw = args.raw
    remoteTerm.set_rx_encoding(args.serial_port_encoding)
    remoteTerm.set_tx_encoding(args.serial_port_encoding)

    if not args.quiet:
        sys.stderr.write('--- RemoteTerm on {p.name}  {p.baudrate},{p.bytesize},{p.parity},{p.stopbits} ---\n'.format(
            p=remoteTerm.serial))
        sys.stderr.write('--- Quit: {} | Menu: {} | Help: {} followed by {} ---\n'.format(
            key_description(remoteTerm.exit_character),
            key_description(remoteTerm.menu_character),
            key_description(remoteTerm.menu_character),
            key_description('\x08')))

    remoteTerm.start()
    try:
        remoteTerm.join(False)
    except KeyboardInterrupt:
        pass
    if not args.quiet:
        sys.stderr.write('\n--- exit ---\n')
    remoteTerm.join()
    remoteTerm.close()


# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
if __name__ == '__main__':
    main()
