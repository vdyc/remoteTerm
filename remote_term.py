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
    alias_dict = dict(config.items("ALIAS"))

    def tx(self, text):
        text = self.alias_dict[text] if text in self.alias_dict.keys() else text
        return text


TRANSFORMATIONS = {
    'direct': Transform,    # no transformation
    'default': NoTerminal,
    'nocontrol': NoControls,
    'printable': Printable,
    'colorize': Colorize,
    'debug': DebugIO,
    'alias': Alias,
}


class RemoteTerm(Miniterm):
    def __init__(self, serial_instance):
        super().__init__(serial_instance, echo=False, eol='crlf')
        self.filters = ["colorize"]
        self.tx_q = Queue()

    def socket_input(self):
        context = zmq.Context()
        socket = context.socket(zmq.REP)
        socket.bind(config.get("DEFAULT", "socket"))

        while True:
            #  Wait for next request from client
            command_in = socket.recv()
            self.tx_q.put(command_in.decode('ascii') + "\n")
            #  Send reply back to client
            socket.send(b"In queue")

    def keyboard_input(self):
        while self.alive:
            try:
                command_in = self.console.getkey()
                self.tx_q.put(command_in)
            except KeyboardInterrupt:
                self.tx_q.put('\x03')

    def serial_communication(self):
        """loop and copy serial->console"""
        try:
            menu_active = False
            while self.alive:
                # Serial TX
                if not self.tx_q.empty():
                    c = self.tx_q.get()
                    if menu_active:
                        self.handle_menu_key(c)
                        menu_active = False
                    elif c == self.menu_character:
                        menu_active = True  # next char will be for menu
                    elif c == self.exit_character:
                        self.stop()  # exit app
                        break
                    else:
                        # ~ if self.raw:
                        text = c
                        for transformation in self.tx_transformations:
                            text = transformation.tx(text)
                        self.serial.write(self.tx_encoder.encode(text))
                        if self.echo:
                            echo_text = c
                            for transformation in self.tx_transformations:
                                echo_text = transformation.echo(echo_text)
                            self.console.write(echo_text)

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
                                text = transformation.rx(text)
                            self.console.write(text)
        except serial.SerialException:
            print(f"{serial.SerialException}")
            self.alive = False
            self.console.cancel()
            raise       # XXX handle instead of re-raise?

    def _start_reader(self):
        """Start serial r/w thread"""
        self._reader_alive = True
        # start serial r/w thread
        self.receiver_thread = threading.Thread(target=self.serial_communication, name='serial_comm')
        self.receiver_thread.daemon = True
        self.receiver_thread.start()

    def start(self):
        """start worker threads"""
        self.alive = True
        # enter console->serial loop
        self.socket_thread = threading.Thread(target=self.socket_input, name='socket')
        self.socket_thread.daemon = True
        self.socket_thread.start()
        self.transmitter_thread = threading.Thread(target=self.keyboard_input, name='tx')
        self.transmitter_thread.daemon = True
        self.transmitter_thread.start()
        self._start_reader()
        self.console.setup()

    def join(self, transmit_only=False):
        """wait for worker threads to terminate"""
        self.transmitter_thread.join()
        self.socket_thread.join()
        if not transmit_only:
            if hasattr(self.serial, 'cancel_read'):
                self.serial.cancel_read()
            self.receiver_thread.join()

    def writer(self):
        """\
        Loop and copy console->serial until self.exit_character character is
        found. When self.menu_character is found, interpret the next key
        locally.
        """
        print("Error, feature moved to serial_communication")


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