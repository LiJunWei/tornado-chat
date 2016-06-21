#!/usr/bin/env python

import os
import uuid
import json
import re
import logging
import time
import argparse
import struct

import tornado.ioloop
import tornado.web
from tornado import websocket
from tornado.util import bytes_type
from tornado.iostream import StreamClosedError

MAX_ROOMS = 100
MAX_USERS_PER_ROOM = 100
MAX_LEN_ROOMNAME = 20
MAX_LEN_NICKNAME = 20


class RoomHandler(object):

    def __init__(self):
        # client_id :  {'wsconn': wsconn, 'room':room, # 'nick':nick}
        self.client_info = {}
        # dict  to store a list of  {'cid':cid, 'nick':nick , 'wsconn': wsconn}
        # for each room
        self.room_info = {}
        # room:[conn...]
        self.roomates = {}
        self.pending_cwsconn = {}

    def add_room(self, room, nick):
        """Add nick to room. Return generated client_id"""
        if len(self.room_info) > MAX_ROOMS:
            log.error("MAX_ROOMS_REACHED")
            return -1
        if room in self.room_info and len(self.room_info[room]) >= MAX_USERS_PER_ROOM:
            log.error("MAX_USERS_PER_ROOM_REACHED")
            return -2
        roomvalid = re.match(r'[\w-]+$', room)
        nickvalid = re.match(r'[\w-]+$', nick)
        if roomvalid == None:
            log.error("INVALID_ROOM_NAME - ROOM:%s" % (room,))
            return -3
        if nickvalid == None:
            return -4
            log.error("INVALID_NICK_NAME - NICK:%s" % (nick,))

        client_id = uuid.uuid4().int  # generate a client id.
        if not room in self.room_info:  # it's a new room
            self.room_info[room] = []
            log.debug("ADD_ROOM - ROOM_NAME:%s" % room)

        # process duplicate nicks
        c = 1
        name = nick
        nicks = self.nicks_in_room(room)
        while True:
            if name in nicks:
                name = nick + str(c)
            else:
                break
            c += 1
        self.add_pending(client_id, room, name)
        return client_id

    def add_pending(self, client_id, room, nick):
        log.debug("ADD_PENDING - CLIENT_ID:%s" % client_id)
        # we still don't know the WS connection for this client
        self.pending_cwsconn[client_id] = {'room': room, 'nick': nick}

    def remove_pending(self, client_id):
        if client_id in self.pending_cwsconn:
            log.debug("REMOVE_PENDING - CLIENT_ID:%s" % client_id)
            del(self.pending_cwsconn[client_id])  # no longer pending

    def add_client_wsconn(self, client_id, conn):
        """Store the websocket connection corresponding to an existing client."""
        self.client_info[client_id] = self.pending_cwsconn[client_id]
        self.client_info[client_id]['wsconn'] = conn
        room = self.pending_cwsconn[client_id]['room']
        nick = self.pending_cwsconn[client_id]['nick']
        self.room_info[room].append(
            {'client_id': client_id, 'nick': nick, 'wsconn': conn})
        self.remove_pending(client_id)
        # add this conn to the corresponding roomates set
        if room in self.roomates:
            self.roomates[room].add(conn)
        else:
            self.roomates[room] = {conn}
        # send "join" and and "nick_list" messages
        self.send_join_msg(client_id)
        nick_list = self.nicks_in_room(room)
        cwsconns = self.roomate_cwsconns(client_id)
        self.send_nicks_msg(cwsconns, nick_list)

    def remove_client(self, client_id):
        """Remove all client information from the room handler."""
        client_room = self.client_info[client_id]['room']
        nick = self.client_info[client_id]['nick']
        # first, remove the client connection from the corresponding room in
        # self.roomates
        client_conn = self.client_info[client_id]['wsconn']
        if client_conn in self.roomates[client_room]:
            self.roomates[client_room].remove(client_conn)
            if len(self.roomates[client_room]) == 0:
                del(self.roomates[client_room])
        r_cwsconns = self.roomate_cwsconns(client_id)

        self.client_info[client_id] = None
        for user in self.room_info[client_room]:
            if user['client_id'] == client_id:
                self.room_info[client_room].remove(user)
                break
        self.send_leave_msg(nick, r_cwsconns)
        nick_list = self.nicks_in_room(client_room)
        self.send_nicks_msg(r_cwsconns, nick_list)
        if len(self.room_info[client_room]) == 0:  # if room is empty, remove.
            del(self.room_info[client_room])
            log.debug("REMOVE_ROOM - ROOM_ID:%s" % client_room)

    def nicks_in_room(self, room):
        """Return a list with the nicknames of the users currently connected to the specified room."""
        return [user['nick'] for user in self.room_info[room]]

    def roomate_cwsconns(self, cid):
        """Return a list with the connections of the users currently connected to the room where
        the specified client (cid) is connected."""
        cid_room = self.client_info[cid]['room']
        conns = {}
        if cid_room in self.roomates:
            conns = self.roomates[cid_room]
        return conns

    def send_join_msg(self, client_id):
        nick = self.client_info[client_id]['nick']
        r_cwsconns = self.roomate_cwsconns(client_id)
        msg = {"msgtype": "join", "username": nick,
               "payload": " joined the chat room.", "sent_ts": "%10.6f" % (time.time(),)}
        pmessage = json.dumps(msg)
        for conn in r_cwsconns:
            conn.write_message(pmessage)

    @staticmethod
    def send_nicks_msg(conns, nick_list):
        """Send a message of type 'nick_list' (contains a list of nicknames) to all the specified connections."""
        msg = {"msgtype": "nick_list", "payload": nick_list,
               "sent_ts": "%10.6f" % (time.time(),)}
        pmessage = json.dumps(msg)
        # print "SENDING NICKS MESSAGE: %s"%(pmessage,)
        for c in conns:
            c.write_message(pmessage)

    @staticmethod
    def send_leave_msg(nick, rconns):
        """Send a message of type 'leave', specifying the nickname that is leaving, to all the specified connections."""
        msg = {"msgtype": "leave", "username": nick,
               "payload": " left the chat room.", "sent_ts": "%10.6f" % (time.time(),)}
        pmessage = json.dumps(msg)
        for conn in rconns:
            conn.write_message(pmessage)


class MainHandler(tornado.web.RequestHandler):

    def initialize(self, room_handler):
        self.room_handler = room_handler


    def get(self, action=None):
        """Render chat.html if required arguments are present, render main.html otherwise."""
        if not action:  # init startup sequence, won't be completed until the websocket connection is established.
            try:
                room = self.get_argument("room")
                nick = self.get_argument("nick")
                # this alreay calls add_pending
                client_id = self.room_handler.add_room(room, nick)
                emsgs = ["The nickname provided was invalid. It can only contain letters, numbers, - and _.\nPlease try again.",
                         "The room name provided was invalid. It can only contain letters, numbers, - and _.\nPlease try again.",
                         "The maximum number of users in this room (%d) has been reached.\n\nPlease try again later." % MAX_USERS_PER_ROOM,
                         "The maximum number of rooms (%d) has been reached.\n\nPlease try again later." % MAX_ROOMS]
                if client_id == -1 or client_id == -2:
                    self.render("templates/maxreached.html",
                                emsg=emsgs[client_id])
                else:
                    if client_id < -2:
                        self.render("templates/main.html",
                                    emsg=emsgs[client_id])
                    else:
                        self.set_cookie("ftc_cid", str(client_id))
                        self.render("templates/chat.html", room_name=room)
            except tornado.web.MissingArgumentError:
                self.render("templates/main.html", emsg="")
        else:
            # drop client from "pending" list. Client cannot establish WS
            # connection.
            if action == "drop":
                client_id = self.get_cookie("ftc_cid")
                if client_id:
                    self.room_handler.remove_pending(client_id)
                    self.render("templates/nows.html")


class ClientWSConnection(websocket.WebSocketHandler):

    def initialize(self, room_handler):
        """Store a reference to the "external" RoomHandler instance"""
        self.room_handler = room_handler

    def open(self):
        self.client_id = int(self.get_cookie("ftc_cid", 0))
        log.debug("OPEN_WS - CLIENT_ID:%s" % self.client_id)
        self.room_handler.add_client_wsconn(self.client_id, self)

    def on_message(self, message):
        msg = json.loads(message)
        msg['username'] = self.room_handler.client_info[self.client_id]['nick']
        pmessage = json.dumps(msg)
        rconns = self.room_handler.roomate_cwsconns(self.client_id)
        frame = self.make_frame(pmessage)
        for conn in rconns:
            # conn.write_message(pmessage)
            conn.write_frame(frame)

    def make_frame(self, message):
        opcode = 0x1
        message = tornado.escape.utf8(message)
        assert isinstance(message, bytes_type)
        finbit = 0x80
        mask_bit = 0
        frame = struct.pack("B", finbit | opcode)
        l = len(message)
        if l < 126:
            frame += struct.pack("B", l | mask_bit)
        elif l <= 0xFFFF:
            frame += struct.pack("!BH", 126 | mask_bit, l)
        else:
            frame += struct.pack("!BQ", 127 | mask_bit, l)
        frame += message
        return frame

    def write_frame(self, frame):
        try:
            #self._write_frame(True, opcode, message)
            self.stream.write(frame)
        except StreamClosedError:
            pass
            # self._abort()

    def on_close(self):
        log.debug("CLOSE_WS - CLIENT_ID:%s" % (self.client_id,))
        self.room_handler.remove_client(self.client_id)

    def allow_draft76(self):
        return True


def setup_cmd_parser():
    p = argparse.ArgumentParser(
        description='Simple WebSockets-based text chat server.')
    p.add_argument('-i', '--ip', action='store',
                   default='127.0.0.1', help='Server IP address.')
    p.add_argument('-p', '--port', action='store', type=int,
                   default=8888, help='Server Port.')
    p.add_argument('-g', '--log_file', action='store',
                   default='logsimplechat.log', help='Name of log file.')
    p.add_argument('-f', '--file_log_level', const=1, default=0, type=int, nargs="?",
                   help="0 = only warnings, 1 = info, 2 = debug. Default is 0.")
    p.add_argument('-c', '--console_log_level', const=1, default=3, type=int, nargs="?",
                   help="0 = No logging to console, 1 = only warnings, 2 = info, 3 = debug. Default is 0.")
    return p


def setup_logging(args):
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)   # set maximum level for the logger,
    formatter = logging.Formatter('%(asctime)s | %(thread)d | %(message)s')
    loglevels = [0, logging.WARN, logging.INFO, logging.DEBUG]
    fll = args.file_log_level
    cll = args.console_log_level
    fh = logging.FileHandler(args.log_file, mode='a')
    fh.setLevel(loglevels[fll])
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    if cll > 0:
        sh = logging.StreamHandler()
        sh.setLevel(loglevels[cll])
        sh.setFormatter(formatter)
        logger.addHandler(sh)
    return logger

if __name__ == "__main__":

    parse = setup_cmd_parser()
    args = parse.parse_args()
    log = setup_logging(args)
    room_handler = RoomHandler()
    app = tornado.web.Application([
        (r"/(|drop)", MainHandler, {'room_handler': room_handler}),
        (r"/ws", ClientWSConnection, {'room_handler': room_handler})
    ],
        static_path=os.path.join(os.path.dirname(__file__), "static")
    )
    app.listen(args.port, args.ip)
    log.info('START_SERVER - PID:%s' % (os.getpid()))
    log.info('LISTEN - IP:%s - PORT:%s' % (args.ip, args.port))
    tornado.ioloop.IOLoop.instance().start()
