from __future__ import print_function

import errno
import logging
import os
import sys
import unittest
import socket
from io import BytesIO

from mock import Mock

from haka_mqtt.mqtt import MqttConnack, MqttTopic, MqttSuback, SubscribeResult, MqttConnect, MqttSubscribe, MqttPublish, \
    MqttPuback, MqttPingreq
from haka_mqtt.reactor import (
    Reactor,
    ReactorProperties,
    ReactorState, KeepaliveTimeoutReactorError)
from haka_mqtt.scheduler import Scheduler


def buffer_packet(packet):
    bio = BytesIO()
    packet.encode(bio)
    return bio.getvalue()


class TestReactor(unittest.TestCase):
    def setUp(self):
        logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
        self.socket = Mock()
        self.endpoint = ('test.mosquitto.org', 1883)
        self.client_id = 'client'
        self.keepalive_period = 10*60
        self.scheduler = Scheduler()

        p = ReactorProperties()
        p.socket = self.socket
        p.endpoint = self.endpoint
        p.client_id = self.client_id
        p.keepalive_period = self.keepalive_period
        p.scheduler = self.scheduler
        p.clean_session = True

        self.on_publish = Mock()

        self.properties = p
        self.reactor = Reactor(p)
        self.reactor.on_publish = self.on_publish
        self.log = logging.getLogger(self.__class__.__name__)
        self.log.info('%s setUp()', self._testMethodName)

    def tearDown(self):
        self.assertEqual(0, len(self.scheduler))
        self.log.info('%s tearDown()', self._testMethodName)

    def set_recv_result(self, rv):
        if isinstance(rv, Exception):
            self.socket.recv.side_effect = rv
        else:
            self.socket.recv.return_value = rv

    def set_recv_packet_result_then_read(self, p):
        self.socket.recv.return_value = buffer_packet(p)
        self.reactor.read()
        self.socket.recv.assert_called_once()
        self.socket.recv.reset_mock()

    def set_send_result(self, rv):
        if isinstance(rv, Exception):
            self.socket.send.side_effect = rv
        else:
            self.socket.send.return_value = rv

    def set_send_result_then_write(self, rv, buf):
        self.set_send_result(rv)

        self.reactor.write()
        self.socket.send.assert_called_once_with(buf)
        self.socket.send.reset_mock()

    def set_send_packet_result(self, p, exc=None):
        with BytesIO() as f:
            num_bytes_written = p.encode(f)

        if exc:
            num_bytes_written = exc

        self.set_send_result(num_bytes_written)

    def set_send_packet_result_then_write(self, p, exc=None):
        buf = buffer_packet(p)

        if exc:
            num_bytes_written = exc

        self.set_send_result(len(buf))

        self.reactor.write()
        self.socket.send.assert_called_once_with(buf)
        self.socket.send.reset_mock()

    def encode_packet_to_buf(self, p):
        with BytesIO() as f:
            p.encode(f)
            buf = f.getvalue()

        return bytearray(buf)

    def start_to_connect(self):
        self.assertEqual(ReactorState.init, self.reactor.state)

        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.endpoint)
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.set_send_packet_result_then_write(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.connack)


class TestReactorPaths(TestReactor, unittest.TestCase):
    def test_connack_keepalive_timeout(self):
        self.start_to_connect()
        p = MqttPingreq()
        self.set_send_packet_result(p)
        self.scheduler.poll(self.keepalive_period)
        self.socket.send.assert_called_once_with(buffer_packet(p))
        self.socket.send.reset_mock()

        self.scheduler.poll(self.keepalive_period * 0.5)
        self.assertEqual(ReactorState.error, self.reactor.state)
        self.assertEqual(self.reactor.error, KeepaliveTimeoutReactorError())

    def test_connack_unexpected_session_present(self):
        self.start_to_connect()

        connack = MqttConnack(True, 0)
        self.set_recv_packet_result_then_read(connack)
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_start(self):
        self.start_to_connect()

        connack = MqttConnack(False, 0)
        self.set_recv_packet_result_then_read(connack)
        self.assertEqual(self.reactor.state, ReactorState.connected)

        TOPIC = 'bear_topic'
        p = MqttSubscribe(0, [MqttTopic(TOPIC, 0)])
        self.set_send_packet_result(p)
        self.reactor.subscribe(p.topics)
        self.socket.send.assert_called_once_with(buffer_packet(p))
        self.socket.send.reset_mock()

        suback = MqttSuback(p.packet_id, [SubscribeResult.qos0])
        self.set_recv_packet_result_then_read(suback)
        self.assertEqual(self.reactor.state, ReactorState.connected)

        p = MqttPublish(1, TOPIC, 'outgoing', False, 0, False)
        self.set_send_packet_result(p)
        self.reactor.publish(p.topic, p.payload, p.qos)
        self.socket.send.assert_called_once_with(buffer_packet(p))
        self.socket.send.reset_mock()

        p = MqttPuback(p.packet_id)
        self.set_recv_packet_result_then_read(p)

        publish = MqttPublish(1, TOPIC, 'incoming', False, 1, False)
        puback = MqttPuback(p.packet_id)
        self.set_send_packet_result(puback)
        self.set_recv_packet_result_then_read(publish)
        self.on_publish.assert_called_once_with(self.reactor, publish)
        self.on_publish.reset_mock()
        self.socket.send.assert_called_once_with(buffer_packet(puback))

        self.reactor.terminate()


class TestReactorPeerDisconnect(TestReactor, unittest.TestCase):
    def test_connect(self):
        self.assertEqual(self.reactor.state, ReactorState.init)
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.endpoint)
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.set_send_result(socket.error(errno.EPIPE, os.strerror(errno.EPIPE)))
        self.reactor.write()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_connack(self):
        self.assertEqual(self.reactor.state, ReactorState.init)
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.endpoint)
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.set_send_packet_result_then_write(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.connack)

        self.set_recv_result(0)
        self.reactor.read()
        self.assertEqual(self.reactor.state, ReactorState.error)

    def test_connected(self):
        self.assertEqual(self.reactor.state, ReactorState.init)
        self.socket.connect.side_effect = socket.error(errno.EINPROGRESS, '')
        self.reactor.start()
        self.socket.connect.assert_called_once_with(self.endpoint)
        self.assertEqual(ReactorState.connecting, self.reactor.state)
        self.assertFalse(self.reactor.want_read())
        self.assertTrue(self.reactor.want_write())

        self.socket.getsockopt.return_value = 0
        self.set_send_packet_result_then_write(MqttConnect(self.client_id, True, self.keepalive_period))
        self.assertEqual(self.reactor.state, ReactorState.connack)

        self.set_recv_packet_result_then_read(MqttConnack(False, 0))
        self.assertEqual(self.reactor.state, ReactorState.connected)

        self.set_recv_result(0)
        self.reactor.read()
