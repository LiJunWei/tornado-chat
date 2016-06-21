#!/usr/bin/env python

from multiprocessing import Process, Pipe
import time
from time import sleep
import random
import uuid
import logging

from tornado.websocket import websocket_connect
from tornado.ioloop import PeriodicCallback
from tornado.ioloop import IOLoop
from tornado.httpclient import HTTPClient, HTTPRequest

# use a module-level logger
mlog = logging.getLogger('simulator.chatsimu')


class ChatClient(Process):

    def __init__(self, pipe, room_name, send_rate=10, server_address='127.0.0.1', port=8888):
        super(ChatClient, self).__init__()
        self.pipe = pipe
        self.loop = None
        self.room_name = room_name
        i, d = divmod(send_rate, 1)
        self.send_rate = send_rate if d != 0 else send_rate - \
            0.0001   # modify send rate so it's not an integer
        self.send_delay = float(1) / self.send_rate
        self.sending_enabled = True
        self.nick = uuid.uuid4().hex[0:8]
        self.server_address = server_address
        self.port = port
        self.client_id = None
        self.payload_head = None
        self.connected = False
        self.msgs_received = 0
        self.msgs_sent = 0
        self.wsconn = None
        self.periodic_check = None
        #self.die = False

    def run(self):
        # first, we just wait for the connect command, ignores everything else
        self.pipe.send('running')
        cmd = ""
        while cmd != "connect":
            cmd = self.pipe.recv()
        self.kick_off()

    def kick_off(self):
        self.loop = IOLoop.instance()
        self.loop.add_callback(self.connect)
        self.loop.start()

    def connect(self):
        http_client = HTTPClient()
        url = HTTPRequest("http://%s:%s/?room=%s&nick=%s" % (self.server_address, str(self.port),
                                                             self.room_name, self.nick),
                          connect_timeout=480, request_timeout=1000)
        # yes, we're doing a synchronous (blocking) call.
        response = http_client.fetch(url)
        if response.error:
            print("response = ", response)
        else:
            self.client_id = self.__get_ftc_id(response.headers)
            if not self.client_id:
                return
            self.payload_head = self.client_id
            self.__connect_ws()

    @staticmethod
    def __get_ftc_id(headers):
        res = None
        sch = headers['Set-Cookie']
        v = sch.split(';')[0]
        if 'ftc_cid' in v:
            res = v.split('=')[1]
        return res

    def __connect_ws(self):
        portstr = str(self.port)
        wsuri = "ws://%s:%s/ws" % (self.server_address, portstr)
        hreq = HTTPRequest(wsuri, connect_timeout=480, request_timeout=480,
                           headers={"Cookie": "ftc_cid=%s" % self.client_id})
        websocket_connect(hreq, callback=self.handle_wsconn)

    def handle_wsconn(self, conn):
        self.connected = True
        self.wsconn = conn.result(timeout=680)
        self.periodic_check = PeriodicCallback(self.check_command, 473)
        self.periodic_check.start()
        self.pipe.send('connected')

    def send(self):
        if self.sending_enabled:
            self.send_message()
            self.loop.add_timeout(time.time() + self.send_delay, self.send)

    def send_message(self):
        if self.wsconn:
            msg_str = '{"msgtype": "text", "payload": "%s-%d" , "sent_ts": %f}' % (self.payload_head,
                                                                                   self.msgs_sent, time.time())
            self.wsconn.write_message(msg_str)
            self.msgs_sent += 1

    def check_command(self):
        cmd = None
        if self.pipe.poll():
            cmd = self.pipe.recv()  # check the pipe with a non-blocking call!
        if cmd:
            ftc = getattr(self, cmd)
            if ftc is not None:
                ftc()

    def finish(self):
        self.loop.stop()

    def dummy_handler(self, message):
        pass


class LoaderClient(ChatClient):

    def __init__(self, pipe, room_name, send_rate, server_address='127.0.0.1', port=8888):
        super(LoaderClient, self).__init__(
            pipe, room_name, send_rate, server_address, port)

    def handle_wsconn(self, conn):
        super(LoaderClient, self).handle_wsconn(conn)
        self.wsconn.on_message = self.dummy_handler

class ChatLoader(object):

    def __init__(self, server_address, port, num_rooms, ncprobe, num_clients_per_room,
                 connect_rate=30, send_rate=10):
        self.__server_address = server_address
        self.__port = port
        self.num_rooms = int(num_rooms)
        self.send_rate = send_rate
        self.num_clients_per_room = num_clients_per_room
        self.ncprobe = ncprobe   # num additional clients in probe room
        self.__total_num_clients = 0
        self.__connect_rate = connect_rate
        self.__clients_connected = 0
        self.__clients_done_sending = 0
        self.rooms = []
        self.clients = []  # list of tuples (client, pipe)

    def generate_rooms(self):
        #mlog.info("GENERATE_ROOMS - NUM_ROOMS:%d"%(self.num_rooms,))
        for i in range(0, self.num_rooms):
            rn = uuid.uuid4().hex[0:4]
            mlog.debug("GENERATE_ROOM - ROOM:%s - CLIENTS:%d" %
                       (rn, self.num_clients_per_room))
            #rp = RoomInfo(rn, self.num_clients_per_room)
            self.rooms.append(rn)

    def instantiate_clients(self):
        mlog.debug("INSTANTIATE_CLIENTS")
        # add clients to the probe room
        for pc in range(0, self.ncprobe):
            pipe, b = Pipe()
            cc = LoaderClient(pipe=b, room_name="proberoom", send_rate=self.send_rate,
                              server_address=self.__server_address, port=self.__port)
            self.clients.append((cc, pipe))
        for rn in self.rooms:
            for i in range(0, self.num_clients_per_room):
                pipe, b = Pipe()
                cc = LoaderClient(pipe=b, room_name=rn, send_rate=self.send_rate,
                                  server_address=self.__server_address, port=self.__port)
                self.clients.append((cc, pipe))
        self.__total_num_clients = len(self.clients)

    def start_all_clients(self):
        mlog.debug("STARTING_ALL_CLIENTS")
        for client, pipe in self.clients:
            client.start()
        # wait until we get the "running" confirmation from all clients.
        for client, pipe in self.clients:
            res = ""
            while res != "running":
                res = pipe.recv()
        mlog.debug("ALL_CLIENTS_STARTED")

    def connect_all_clients(self):
        mlog.debug("CONNECTING_ALL_CLIENTS")
        for client, pipe in self.clients:
            pipe.send("connect")
            sleep(float(1) / self.__connect_rate)

        # seconds to wait for connection confirmation
        time_limit = time.time() + self.__total_num_clients / 2.0
        i = 0
        while self.__clients_connected < self.__total_num_clients:
            client, pipe = self.clients[i]
            if pipe.poll():
                res = pipe.recv()
                if res == "connected":
                    self.__clients_connected += 1
            else:
                i += 1
                if i >= self.__total_num_clients:
                    i = 0
            if time.time() > time_limit:
                break
        mlog.debug("CLIENTS_CONNECTED - NUM: %d - OF:  %d" %
                   (self.__clients_connected, self.__total_num_clients))
        if self.__clients_connected != self.__total_num_clients:
            mlog.debug("SOME_CLIENTS_DID_NOT_CONNECT")

    def start_sending(self):
        mlog.debug("CLIENTS_START_SENDING")
        random.shuffle(self.clients)
        for client, pipe in self.clients:
            pipe.send("send")

    def finish_clients(self):
        mlog.debug("FINISHING_CLIENTS")
        for client, pipe in self.clients:
            pipe.send('finish')
        mlog.debug("ALL_CLIENTS_FINISHED")
