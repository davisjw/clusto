#!/usr/bin/env python
from traceback import format_exc
from struct import unpack
from errno import EINTR
from time import time
import socket
import signal

from logging.handlers import SysLogHandler
import logging

log = logging.getLogger('clusto.dhcp')
fmt = logging.Formatter('%(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S')
syslog = SysLogHandler()
syslog.setFormatter(fmt)
log.addHandler(syslog)
log.setLevel(logging.INFO)

runtime = logging.getLogger('scapy.runtime')
runtime.setLevel(logging.ERROR)

from scapy.all import BOOTP, DHCP, DHCPTypes, DHCPOptions, DHCPRevOptions

from clusto.scripthelpers import init_script
from clusto.drivers import IPManager, PenguinServer
import clusto

DHCPOptions.update({
    66: 'tftp_server',
    67: 'tftp_filename',
})

for k,v in DHCPOptions.iteritems():
    if type(v) is str:
        n = v
        v = None
    else:
        n = v.name
    DHCPRevOptions[n] = (k,v)

class DHCPRequest(object):
    def __init__(self, packet):
        self.packet = packet
        self.parse()

    def parse(self):
        options = self.packet[DHCP].options
        hwaddr = ':'.join(['%02x' % ord(x) for x in self.packet.chaddr[:6]])

        mac = None
        vendor = None
        options = dict([x for x in options if isinstance(x, tuple)])
        if 'client_id' in options:
            mac = unpack('>6s', options['client_id'][1:])[0]
            options['client_id'] = ':'.join(['%02x' % ord(x) for x in mac]).lower()

        self.type = DHCPTypes[options['message-type']]
        self.hwaddr = hwaddr
        self.options = options

class DHCPResponse(object):
    def __init__(self, type, offerip=None, options={}, request=None):
        self.type = type
        self.offerip = offerip
        self.serverip = gethostbyname(gethostname())
        self.options = options
        self.request = request

    def set_type(self, type):
        self.type = type

    def build(self):
        options = [
            ('message-type', self.type)
        ]
        for k, v in self.options.items():
            if k == 'enabled': continue
            if not k in DHCPRevOptions:
                log.warning('Unknown DHCP option: %s' % k)
                continue
            if isinstance(v, unicode):
                v = v.encode('ascii', 'ignore')
            options.append((k, v))

        bootp_options = {
            'op': 2,
            'xid': self.request.packet.xid,
            'ciaddr': self.offerip,
            'yiaddr': self.offerip,
            'chaddr': self.request.packet.chaddr,
        }
        if 'tftp_server' in self.options:
            bootp_options['siaddr'] = self.options['tftp_server']
        if 'tftp_filename' in self.options:
            bootp_options['file'] = self.options['tftp_filename']
        for k, v in bootp_options.items():
            if isinstance(v, unicode):
                bootp_options[k] = v.encode('ascii', 'ignore')

        pkt = BOOTP(**bootp_options)/DHCP(options=options)
        #pkt.show()
        return pkt.build()

class DHCPServer(object):
    def __init__(self, bind_address=('0.0.0.0', 67)):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(bind_address)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_id = socket.gethostbyname(socket.gethostname())

    def run(self):
        while True:
            try:
                data, address = self.sock.recvfrom(4096)
            except KeyboardInterrupt:
                break
            except socket.error, e:
                if e.args[0] == EINTR:
                    continue
                log.error(format_exc())
                break
            packet = BOOTP(data)
            request = DHCPRequest(packet)

            log.debug('%s %s' % (request.type, request.hwaddr))

            methodname = 'handle_%s' % request.type
            if hasattr(self, methodname):
                method = getattr(self, methodname)
                method(request)

    def send(self, address, data):
        while data:
            bytes = self.sock.sendto(str(data), 0, (address, 68))
            data = data[bytes:]

class ClustoDHCPServer(DHCPServer):
    def __init__(self):
        DHCPServer.__init__(self)
        self.offers = {}
        self.cache = {}
        self.ipmi_cache = {}

    def handle_request(self, request):
        chaddr = request.packet.chaddr
        if not chaddr in self.offers:
            log.warning('Got a request before sending an offer from %s' % request.hwaddr)
            return
        response = self.offers[chaddr]
        response.type = 'ack'

        self.send('255.255.255.255', response.build())

    def query_clusto(self, key, attrs, cache=None, cache_timeout=60.0):
        if key in cache:
            expires, server = cache[key]
            if time() < expires:
                return server
        
        server = clusto.get_entities(attrs=attrs)
        expires = time() + cache_timeout
        cache[key] = (expires, server)
        return server

    def clear_cache(self, signum, frame):
        log.info('Clearing cache (%i entries invalidated)' % (len(self.cache) + len(self.ipmi_cache)))
        self.cache.clear()
        self.ipmi_cache.clear()

    def handle_discover(self, request):
        self.update_ipmi(request)

        attrs = [{
            'key': 'port-nic-eth',
            'subkey': 'mac',
            'number': 1,
            'value': request.hwaddr,
        }]
        server = self.query_clusto(request.hwaddr, attrs, cache=self.cache)

        if not server:
            return

        if len(server) > 1:
            log.warning('More than one server with address %s: %s' % (request.hwaddr, ', '.join([x.name for x in server])))
            return
        
        server = server[0]

        if not server.attrs(key='dhcp', subkey='enabled', value=1, merge_container_attrs=True):
            log.info('DHCP not enabled for %s' % server.name)
            return

        ip = server.get_ips()
        if not ip:
            log.info('No IP assigned for %s' % server.name)
            return
        else:
            ip = ip[0]

        ipman = IPManager.get_ip_manager(ip)

        options = {
            'server_id': self.server_id,
            'lease_time': 3600,
            'renewal_time': 1600,
            'subnet_mask': ipman.netmask,
            'broadcast_address': ipman.ipy.broadcast().strNormal(),
            'router': ipman.gateway,
            'hostname': server.name,
        }

        log.info('Sending offer to %s' % server.name)

        for attr in server.attrs(key='dhcp', merge_container_attrs=True):
            options[attr.subkey] = attr.value

        response = DHCPResponse(type='offer', offerip=ip, options=options, request=request)
        self.offers[request.packet.chaddr] = response
        self.send('255.255.255.255', response.build())

    def update_ipmi(self, request):
        attrs = [{
            'key': 'bootstrap',
            'subkey': 'mac',
            'value': request.hwaddr,
        }, {
            'key': 'port-nic-eth',
            'subkey': 'mac',
            'number': 1,
            'value': request.hwaddr,
        }]
        server = self.query_clusto(request.hwaddr, attrs, cache=self.ipmi_cache)

        if not server:
            return

        try:
            server = server[0]
            if request.options.get('vendor_class_id', None) == 'udhcp 0.9.9-pre':
                # This is an IPMI request
                #logging.debug('Associating IPMI %s %s' % (request.hwaddr, server.name))
                server.set_port_attr('nic-eth', 1, 'ipmi-mac', request.hwaddr)
            else:
                #logging.debug('Associating physical %s %s' % (requst.hwaddr, server.name))
                server.set_port_attr('nic-eth', 1, 'mac', request.hwaddr)
        except:
            log.error('Error updating server MAC: %s' % format_exc())

if __name__ == '__main__':
    init_script()

    server = ClustoDHCPServer()
    signal.signal(signal.SIGHUP, server.clear_cache)

    log.info('Clusto DHCP server starting')
    server.run()
