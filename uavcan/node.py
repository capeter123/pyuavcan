#encoding=utf-8

import time
import math
import ctypes
import struct
import logging
import binascii
import functools
import collections


import uavcan
import uavcan.dsdl as dsdl
import uavcan.driver as driver
import uavcan.transport as transport


try:
    import tornado
    import tornado.gen
    import tornado.ioloop
    import tornado.concurrent
except ImportError:
    pass


class Node(object):
    def __init__(self, handlers, node_id=127):
        self.can = None
        self.transfer_manager = transport.TransferManager()
        self.handlers = handlers
        self.node_id = node_id
        self.outstanding_requests = {}
        self.outstanding_request_callbacks = {}
        self.outstanding_request_timestamps = {}
        self.next_transfer_ids = collections.defaultdict(int)
        self.node_info = {}

    def _recv_frame(self, dev, message):
        frame_id, frame_data, ext_id = message
        if not ext_id:
            return

        frame = transport.Frame(frame_id, frame_data)
        # logging.debug("Node._recv_frame(): got {0!s}".format(frame))

        transfer_frames = self.transfer_manager.receive_frame(frame)
        if not transfer_frames:
            return

        dtid = transfer_frames[0].data_type_id
        if transfer_frames[0].transfer_priority == \
                transport.TransferPriority.SERVICE:
            kind = dsdl.parser.CompoundType.KIND_SERVICE
        else:
            kind = dsdl.parser.CompoundType.KIND_MESSAGE
        datatype = uavcan.DATATYPES.get((dtid, kind))
        if not datatype:
            logging.debug(("Node._recv_frame(): unrecognised data type " +
                           "ID {0:d} for kind {1:d}").format(dtid, kind))
            return

        transfer = transport.Transfer()
        transfer.from_frames(transfer_frames, datatype_crc=datatype.base_crc)

        if transfer.is_message():
            payload = datatype()  # Broadcast or unicast
        elif transfer.is_request():
            payload = datatype(mode="request")
        else:  # transfer.is_response()
            payload = datatype(mode="response")

        payload.unpack(transport.bits_from_bytes(transfer.payload))

        logging.info("Node._recv_frame(): received {0!r}".format(payload))

        # If it's a node info request, keep track of the status of each node
        if payload.type == uavcan.protocol.NodeStatus:
            self.node_info[transfer.source_node_id] = {
                "uptime": payload.uptime_sec,
                "status": payload.status_code,
                "timestamp": time.time()
            }

        if transfer.is_response() and transfer.dest_node_id == self.node_id:
            # This is a reply to a request we sent. Look up the original
            # request and call the appropriate callback
            requests = self.outstanding_requests.keys()
            for key in requests:
                if transfer.is_response_to(self.outstanding_requests[key]):
                    # Call the request's callback and remove it from the
                    # active list
                    if key in self.outstanding_request_callbacks:
                        self.outstanding_request_callbacks[key]((payload, transfer))
                        del self.outstanding_request_callbacks[key]
                    del self.outstanding_requests[key]
                    del self.outstanding_request_timestamps[key]
                    break
        elif transfer.is_broadcast() or transfer.dest_node_id == self.node_id:
            # This is a request, a unicast or a broadcast; look up the
            # appropriate handler by data type ID
            for handler in self.handlers:
                if handler[0] == datatype:
                    kwargs = handler[2] if len(handler) == 3 else {}
                    h = handler[1](payload, transfer, self, **kwargs)
                    h._execute()

    def _next_transfer_id(self, key):
        transfer_id = self.next_transfer_ids[key]
        self.next_transfer_ids[key] = (transfer_id + 1) & 0x1F
        return transfer_id

    def listen(self, device, baudrate=1000000, io_loop=None):
        if device.startswith("/dev"):
            self.can = driver.SLCAN(device, baudrate=baudrate)
        else:
            self.can = driver.SocketCAN(device)

        self.can.open()
        self.can.add_to_ioloop(tornado.ioloop.IOLoop.current(),
                               callback=self._recv_frame)

        # Send node status every 0.5 sec
        self.start_time = time.time()
        # TODO: make it easier to get constant values from UAVCAN types
        self.status = uavcan.protocol.NodeStatus().STATUS_OK
        self.nodestatus_timer = tornado.ioloop.PeriodicCallback(
            self.send_node_status,
            500, io_loop=io_loop)
        self.nodestatus_timer.start()

    def send_node_status(self):
        status = uavcan.protocol.NodeStatus()
        status.uptime_sec = int(time.time() - self.start_time)
        status.status_code = self.status
        status.vendor_specific_status_code = 0
        self.send_message(status)

    @tornado.concurrent.return_future
    def send_request(self, payload, dest_node_id=None, callback=None):
        transfer_id = self._next_transfer_id((payload.type.default_dtid,
                                              dest_node_id))
        transfer = transport.Transfer(
            payload=payload,
            source_node_id=self.node_id,
            dest_node_id=dest_node_id,
            transfer_id=transfer_id,
            service_not_message=True,
            request_not_response=True)

        for frame in transfer.to_frames(datatype_crc=payload.type.base_crc):
            self.can.send(frame.message_id, frame.to_bytes(), extended=True)

        self.outstanding_requests[transfer.key] = transfer
        self.outstanding_request_callbacks[transfer.key] = callback
        self.outstanding_request_timestamps[transfer.key] = time.time()

        logging.info(
            "Node.send_request(dest_node_id={0:d}): sent {1!r}".format(
            dest_node_id, payload))

    def send_message(self, payload):
        transfer_id = self._next_transfer_id(payload.type.default_dtid)
        transfer = transport.Transfer(
            payload=payload,
            source_node_id=self.node_id,
            transfer_id=transfer_id,
            service_not_message=False)

        for frame in transfer.to_frames(datatype_crc=payload.type.base_crc):
            self.can.send(frame.message_id, frame.to_bytes(), extended=True)

        logging.info("Node.send_message(): sent {0!r}".format(payload))


class MessageHandler(object):
    def __init__(self, payload, transfer, node, *args, **kwargs):
        self.message = payload
        self.transfer = transfer
        self.node = node

    def _execute(self):
        self.on_message(self.message)

    def on_message(self, message):
        pass


class ServiceHandler(MessageHandler):
    def __init__(self, *args, **kwargs):
        super(ServiceHandler, self).__init__(*args, **kwargs)
        self.request = self.message
        self.response = transport.CompoundValue(self.request.type, tao=True,
                                                mode="response")

    def _execute(self):
        result = self.on_request()

        # on_request generally wouldn't return anything, but if it's a
        # coroutine and it yields then we'll get a future back (the value
        # of which is irrelevant). Wait for the future to ensure the handler
        # has populated all the response fields.

        # FIXME
        #if tornado.concurrent.is_future(result):
        #    result = yield result

        # Send the response transfer
        transfer = transport.Transfer(
            payload=self.response,
            source_node_id=self.node.node_id,
            dest_node_id=self.transfer.source_node_id,
            transfer_id=self.transfer.transfer_id,
            transfer_priority=self.transfer.transfer_priority,
            service_not_message=True,
            request_not_response=False
        )
        for frame in transfer.to_frames(datatype_crc=self.request.type.base_crc):
            self.node.can.send(frame.message_id, frame.to_bytes(),
                               extended=True)

        logging.info(
            "ServiceHandler._execute(dest_node_id={0:d}): sent {1!r}".format(
            self.transfer.source_node_id, self.response))

    def on_request(self):
        pass
