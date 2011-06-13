#!/usr/bin/python
"""SocksiPy - Python SOCKS module.
Version 1.03

Copyright 2011 Bjarni R. Einarsson. All rights reserved.
Copyright 2006 Dan-Haim. All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of Dan Haim nor the names of his contributors may be used
   to endorse or promote products derived from this software without specific
   prior written permission.

THIS SOFTWARE IS PROVIDED BY DAN HAIM "AS IS" AND ANY EXPRESS OR IMPLIED
WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO
EVENT SHALL DAN HAIM OR HIS CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA
OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT
OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMANGE.


This module provides a standard socket-like interface for Python
for tunneling connections through SOCKS proxies.

"""

"""

Refactored to allow proxy chaining and use as a command-line netcat-like
tool by Bjarni R. Einarsson (http://bre.klaki.net/) for use with PageKite
(http://pagekite.net/).

Minor modifications made by Christopher Gilbert (http://motomastyle.com/)
for use in PyLoris (http://pyloris.sourceforge.net/)

Minor modifications made by Mario Vilas (http://breakingcode.wordpress.com/)
mainly to merge bug fixes found in Sourceforge

"""

import os, fcntl, socket, sys, select, struct, threading
import ssl

DEBUG = False

PROXY_TYPE_DEFAULT = -1
PROXY_TYPE_NONE = 0
PROXY_TYPE_SOCKS4 = 1
PROXY_TYPE_SOCKS5 = 2
PROXY_TYPE_HTTP = 3
PROXY_TYPE_SSL = 4
PROXY_TYPE_SSL_WEAK = 5
PROXY_TYPE_SSL_ANON = 6

PROXY_SSL_TYPES = (PROXY_TYPE_SSL, PROXY_TYPE_SSL_WEAK, PROXY_TYPE_SSL_ANON)
PROXY_DEFAULTS = {
    PROXY_TYPE_NONE: 0,
    PROXY_TYPE_DEFAULT: 0,
    PROXY_TYPE_SOCKS4: 1080,
    PROXY_TYPE_SOCKS5: 1080,
    PROXY_TYPE_HTTP: 8080,
    PROXY_TYPE_SSL: 443,
    PROXY_TYPE_SSL_WEAK: 443,
    PROXY_TYPE_SSL_ANON: 443,
}
PROXY_TYPES = {
  'defaults': PROXY_TYPE_DEFAULT,
  'default': PROXY_TYPE_DEFAULT,
  'none': PROXY_TYPE_NONE,
  'socks4': PROXY_TYPE_SOCKS4,
  'socks4a': PROXY_TYPE_SOCKS4,
  'socks5': PROXY_TYPE_SOCKS5,
  'socks': PROXY_TYPE_SOCKS5,
  'http': PROXY_TYPE_HTTP,
  'ssl': PROXY_TYPE_SSL,
  'ssl-weak': PROXY_TYPE_SSL_WEAK,
  'ssl-anon': PROXY_TYPE_SSL_ANON,
}

P_TYPE = 0
P_HOST = 1
P_PORT = 2
P_RDNS = 3
P_USER = 4
P_PASS = 5

DEFAULT_ROUTE = '*'
_proxyroutes = { }
_orgsocket = socket.socket
_orgcreateconn = socket.create_connection
_thread_locals = threading.local()

class ProxyError(Exception): pass
class GeneralProxyError(ProxyError): pass
class Socks5AuthError(ProxyError): pass
class Socks5Error(ProxyError): pass
class Socks4Error(ProxyError): pass
class HTTPError(ProxyError): pass

_generalerrors = ("success",
    "invalid data",
    "not connected",
    "not available",
    "bad proxy type",
    "bad input")

_socks5errors = ("succeeded",
    "general SOCKS server failure",
    "connection not allowed by ruleset",
    "Network unreachable",
    "Host unreachable",
    "Connection refused",
    "TTL expired",
    "Command not supported",
    "Address type not supported",
    "Unknown error")

_socks5autherrors = ("succeeded",
    "authentication is required",
    "all offered authentication methods were rejected",
    "unknown username or invalid password",
    "unknown error")

_socks4errors = ("request granted",
    "request rejected or failed",
    "request rejected because SOCKS server cannot connect to identd on the client",
    "request rejected because the client program and identd report different user-ids",
    "unknown error")


def parseproxy(arg):
    args = arg.split(':')
    args[0] = PROXY_TYPES.get(args[0], PROXY_TYPE_HTTP)
    if len(args) > 2: args[2] = int(args[2])
    return args

def setdefaultproxy(proxytype=None, addr=None, port=None, rdns=True,
                    username=None, password=None,
                    append=False, dest=DEFAULT_ROUTE):
    """setdefaultproxy(proxytype, addr[, port[, rdns[, username[, password]]]])
    Sets a default proxy which all further socksocket objects will use,
    unless explicitly changed.
    """
    global _proxyroutes
    route = _proxyroutes.get(dest.lower(), None)
    proxy = (proxytype, addr, port, rdns, username, password)
    if append:
        if not route:
          route = _proxyroutes.get(DEFAULT_ROUTE, [])[:]
        route.append(proxy)
        _proxyroutes[dest.lower()] = route
    else:
        _proxyroutes[dest.lower()] = [proxy]
    if DEBUG: print 'Routes are: %s' % (_proxyroutes, )

def usesystemdefaults():
    import os

    no_proxy = ['localhost', 'localhost.localdomain', '127.0.0.1']
    no_proxy.extend(os.environ.get('no_PROXY',
                                   os.environ.get('NO_PROXY',
                                                  '')).split(','))
    for host in no_proxy:
        setdefaultproxy(PROXY_TYPE_NONE, dest=host)

    for var in ('ALL_PROXY', 'HTTPS_PROXY', 'http_proxy'):
        val = os.environ.get(var.lower(), os.environ.get(var, None))
        if val:
            setdefaultproxy(*parseproxy(val.replace('/', '')))
            return

def sockcreateconn(*args, **kwargs):
    _thread_locals.create_conn = args[0]
    rv = _orgcreateconn(*args, **kwargs)
    _thread_locals.create_conn = None
    return rv

class socksocket(socket.socket):
    """socksocket([family[, type[, proto]]]) -> socket object
    Open a SOCKS enabled socket. The parameters are the same as
    those of the standard socket init. In order for SOCKS to work,
    you must specify family=AF_INET, type=SOCK_STREAM and proto=0.
    """

    def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, _sock=None):
        self.__sock = _orgsocket(family, type, proto, _sock)
        self.__proxy = None
        self.__proxysockname = None
        self.__proxypeername = None

    def __getattribute__(self, name):
        if name.startswith('_socksocket__'):
          return object.__getattribute__(self, name)
        elif name in ('setproxy', 'connect', 'getproxysockname',
                      'getproxypeername', 'getpeername'):
          return object.__getattribute__(self, name)
        else:
          return getattr(object.__getattribute__(self, "_socksocket__sock"),
                         name)

    def __setattr__(self, name, value):
        if name.startswith('_socksocket__'):
          return object.__setattr__(self, name, value)
        else:
          return setattr(object.__getattribute__(self, "_socksocket__sock"),
                         name, value)

    def __recvall(self, count):
        """__recvall(count) -> data
        Receive EXACTLY the number of bytes requested from the socket.
        Blocks until the required number of bytes have been received.
        """
        data = self.recv(count)
        while len(data) < count:
            d = self.recv(count-len(data))
            if not d: raise GeneralProxyError((0, "connection closed unexpectedly"))
            data = data + d
        return data

    def setproxy(self, proxytype=None, addr=None, port=None, rdns=True, username=None, password=None, append=False):
        """setproxy(proxytype, addr[, port[, rdns[, username[, password]]]])
        Sets the proxy to be used.
        proxytype -    The type of the proxy to be used. Three types
                are supported: PROXY_TYPE_SOCKS4 (including socks4a),
                PROXY_TYPE_SOCKS5 and PROXY_TYPE_HTTP
        addr -        The address of the server (IP or DNS).
        port -        The port of the server. Defaults to 1080 for SOCKS
                servers and 8080 for HTTP proxy servers.
        rdns -        Should DNS queries be preformed on the remote side
                (rather than the local side). The default is True.
                Note: This has no effect with SOCKS4 servers.
        username -    Username to authenticate with to the server.
                The default is no authentication.
        password -    Password to authenticate with to the server.
                Only relevant when username is also provided.
        append -      Append this proxy to the chain.
        """
        proxy = (proxytype, addr, port, rdns, username, password)
        if append and self.__proxy:
            self.__proxy.append(proxy)
        else:
            self.__proxy = [proxy]

    def __negotiatesocks5(self, destaddr, destport, proxy):
        """__negotiatesocks5(self, destaddr, destport, proxy)
        Negotiates a connection through a SOCKS5 server.
        """
        # First we'll send the authentication packages we support.
        if (proxy[P_USER]!=None) and (proxy[P_PASS]!=None):
            # The username/password details were supplied to the
            # setproxy method so we support the USERNAME/PASSWORD
            # authentication (in addition to the standard none).
            self.sendall(struct.pack('BBBB', 0x05, 0x02, 0x00, 0x02))
        else:
            # No username/password were entered, therefore we
            # only support connections with no authentication.
            self.sendall(struct.pack('BBB', 0x05, 0x01, 0x00))
        # We'll receive the server's response to determine which
        # method was selected
        chosenauth = self.__recvall(2)
        if chosenauth[0:1] != chr(0x05).encode():
            self.close()
            raise GeneralProxyError((1, _generalerrors[1]))
        # Check the chosen authentication method
        if chosenauth[1:2] == chr(0x00).encode():
            # No authentication is required
            pass
        elif chosenauth[1:2] == chr(0x02).encode():
            # Okay, we need to perform a basic username/password
            # authentication.
            self.sendall(chr(0x01).encode() +
                         chr(len(proxy[P_USER])) + proxy[P_USER] +
                         chr(len(proxy[P_PASS])) + proxy[P_PASS])
            authstat = self.__recvall(2)
            if authstat[0:1] != chr(0x01).encode():
                # Bad response
                self.close()
                raise GeneralProxyError((1, _generalerrors[1]))
            if authstat[1:2] != chr(0x00).encode():
                # Authentication failed
                self.close()
                raise Socks5AuthError((3, _socks5autherrors[3]))
            # Authentication succeeded
        else:
            # Reaching here is always bad
            self.close()
            if chosenauth[1] == chr(0xFF).encode():
                raise Socks5AuthError((2, _socks5autherrors[2]))
            else:
                raise GeneralProxyError((1, _generalerrors[1]))
        # Now we can request the actual connection
        req = struct.pack('BBB', 0x05, 0x01, 0x00)
        # If the given destination address is an IP address, we'll
        # use the IPv4 address request even if remote resolving was specified.
        try:
            ipaddr = socket.inet_aton(destaddr)
            req = req + chr(0x01).encode() + ipaddr
        except socket.error:
            # Well it's not an IP number,  so it's probably a DNS name.
            if proxy[P_RDNS]:
                # Resolve remotely
                ipaddr = None
                req = req + (chr(0x03).encode() +
                             chr(len(destaddr)).encode() + destaddr)
            else:
                # Resolve locally
                ipaddr = socket.inet_aton(socket.gethostbyname(destaddr))
                req = req + chr(0x01).encode() + ipaddr
        req = req + struct.pack(">H", destport)
        self.sendall(req)
        # Get the response
        resp = self.__recvall(4)
        if resp[0:1] != chr(0x05).encode():
            self.close()
            raise GeneralProxyError((1, _generalerrors[1]))
        elif resp[1:2] != chr(0x00).encode():
            # Connection failed
            self.close()
            if ord(resp[1:2])<=8:
                raise Socks5Error((ord(resp[1:2]),
                                   _socks5errors[ord(resp[1:2])]))
            else:
                raise Socks5Error((9, _socks5errors[9]))
        # Get the bound address/port
        elif resp[3:4] == chr(0x01).encode():
            boundaddr = self.__recvall(4)
        elif resp[3:4] == chr(0x03).encode():
            resp = resp + self.recv(1)
            boundaddr = self.__recvall(ord(resp[4:5]))
        else:
            self.close()
            raise GeneralProxyError((1,_generalerrors[1]))
        boundport = struct.unpack(">H", self.__recvall(2))[0]
        self.__proxysockname = (boundaddr, boundport)
        if ipaddr != None:
            self.__proxypeername = (socket.inet_ntoa(ipaddr), destport)
        else:
            self.__proxypeername = (destaddr, destport)

    def getproxysockname(self):
        """getsockname() -> address info
        Returns the bound IP address and port number at the proxy.
        """
        return self.__proxysockname

    def getproxypeername(self):
        """getproxypeername() -> address info
        Returns the IP and port number of the proxy.
        """
        return _orgsocket.getpeername(self)

    def getpeername(self):
        """getpeername() -> address info
        Returns the IP address and port number of the destination
        machine (note: getproxypeername returns the proxy)
        """
        return self.__proxypeername

    def __negotiatesocks4(self, destaddr, destport, proxy):
        """__negotiatesocks4(self, destaddr, destport, proxy)
        Negotiates a connection through a SOCKS4 server.
        """
        # Check if the destination address provided is an IP address
        rmtrslv = False
        try:
            ipaddr = socket.inet_aton(destaddr)
        except socket.error:
            # It's a DNS name. Check where it should be resolved.
            if proxy[P_RDNS]:
                ipaddr = struct.pack("BBBB", 0x00, 0x00, 0x00, 0x01)
                rmtrslv = True
            else:
                ipaddr = socket.inet_aton(socket.gethostbyname(destaddr))
        # Construct the request packet
        req = struct.pack(">BBH", 0x04, 0x01, destport) + ipaddr
        # The username parameter is considered userid for SOCKS4
        if proxy[P_USER] != None:
            req = req + proxy[P_USER]
        req = req + chr(0x00).encode()
        # DNS name if remote resolving is required
        # NOTE: This is actually an extension to the SOCKS4 protocol
        # called SOCKS4A and may not be supported in all cases.
        if rmtrslv:
            req = req + destaddr + chr(0x00).encode()
        self.sendall(req)
        # Get the response from the server
        resp = self.__recvall(8)
        if resp[0:1] != chr(0x00).encode():
            # Bad data
            self.close()
            raise GeneralProxyError((1,_generalerrors[1]))
        if resp[1:2] != chr(0x5A).encode():
            # Server returned an error
            self.close()
            if ord(resp[1:2]) in (91, 92, 93):
                self.close()
                raise Socks4Error((ord(resp[1:2]), _socks4errors[ord(resp[1:2]) - 90]))
            else:
                raise Socks4Error((94, _socks4errors[4]))
        # Get the bound address/port
        self.__proxysockname = (socket.inet_ntoa(resp[4:]),
                                struct.unpack(">H", resp[2:4])[0])
        if rmtrslv != None:
            self.__proxypeername = (socket.inet_ntoa(ipaddr), destport)
        else:
            self.__proxypeername = (destaddr, destport)

    def __negotiatehttp(self, destaddr, destport, proxy):
        """__negotiatehttp(self, destaddr, destport, proxy)
        Negotiates a connection through an HTTP server.
        """
        # If we need to resolve locally, we do this now
        if not proxy[P_RDNS]:
            addr = socket.gethostbyname(destaddr)
        else:
            addr = destaddr
        self.sendall(("CONNECT " + addr + ":" + str(destport) +
                      " HTTP/1.1\r\n" + "Host: " + destaddr + "\r\n\r\n"
                      ).encode())
        # We read the response until we get the string "\r\n\r\n"
        resp = self.recv(1)
        while resp.find("\r\n\r\n".encode()) == -1:
            resp = resp + self.recv(1)
        # We just need the first line to check if the connection
        # was successful
        statusline = resp.splitlines()[0].split(" ".encode(), 2)
        if statusline[0] not in ("HTTP/1.0".encode(), "HTTP/1.1".encode()):
            self.close()
            raise GeneralProxyError((1, _generalerrors[1]))
        try:
            statuscode = int(statusline[1])
        except ValueError:
            self.close()
            raise GeneralProxyError((1, _generalerrors[1]))
        if statuscode != 200:
            self.close()
            raise HTTPError((statuscode, statusline[2]))
        self.__proxysockname = ("0.0.0.0", 0)
        self.__proxypeername = (addr, destport)

    def __negotiatessl(self, destaddr, destport, proxy,
                       insecure=False, anonymous=False):
        """__negotiatehttp(self, destaddr, destport, proxy)
        Negotiates an SSL session.
        """
        self.__sock = ssl.wrap_socket(self.__sock)
        self.__sock.do_handshake()
        if DEBUG: print '*** Wrapped %s:%s in %s' % (destaddr, destport, self.__sock)

    def __default_route(self, dest):
        return _proxyroutes.get(str(dest).lower(),
                                _proxyroutes.get(DEFAULT_ROUTE,
                                                 None)) or []

    def connect(self, destpair):
        """connect(self, despair)
        Connects to the specified destination through a chain of proxies.
        destpar - A tuple of the IP/DNS address and the port number.
        (identical to socket's connect).
        To select the proxy servers use setproxy() and chainproxy().
        """
        destpair = getattr(_thread_locals, 'create_conn', destpair)

        # Do a minimal input check first
        if ((not type(destpair) in (list, tuple)) or
            (len(destpair) < 2) or (type(destpair[0]) != type('')) or
            (type(destpair[1]) != int)):
            raise GeneralProxyError((5, _generalerrors[5]))

        if self.__proxy:
            proxy_chain = self.__proxy
            default_dest = destpair[0]
        else:
            proxy_chain = self.__default_route(destpair[0])
            default_dest = DEFAULT_ROUTE

        for proxy in proxy_chain:
            if (proxy[P_TYPE] or PROXY_TYPE_NONE) not in PROXY_DEFAULTS:
                raise GeneralProxyError((4, _generalerrors[4]))

        chain = proxy_chain[:]
        chain.append([PROXY_TYPE_NONE, destpair[0], destpair[1]])
        if DEBUG: print '*** Chain: %s' % chain

        first = True
        result = None
        while chain:
            proxy = chain.pop(0)

            if proxy[P_TYPE] == PROXY_TYPE_DEFAULT:
                chain[0:0] = self.__default_route(default_dest)
                if DEBUG: print '*** Chain: %s' % chain
                continue

            if proxy[P_PORT] != None:
                portnum = proxy[P_PORT]
            else:
                portnum = PROXY_DEFAULTS[proxy[P_TYPE] or PROXY_TYPE_NONE]

            if first and proxy[P_HOST]:
                if DEBUG: print '*** Connect: %s:%s' % (proxy[P_HOST], portnum)
                result = self.__sock.connect((proxy[P_HOST], portnum))
                first = False

            if chain:
                nexthop = (chain[0][1], chain[0][2])
                if DEBUG: print '*** Negotiating: %s' % (nexthop, )
                if proxy[P_TYPE] == PROXY_TYPE_SOCKS5:
                    self.__negotiatesocks5(nexthop[0], nexthop[1], proxy)
                elif proxy[P_TYPE] == PROXY_TYPE_SOCKS4:
                    self.__negotiatesocks4(nexthop[0], nexthop[1], proxy)
                elif proxy[P_TYPE] == PROXY_TYPE_HTTP:
                    self.__negotiatehttp(nexthop[0], nexthop[1], proxy)
                elif proxy[P_TYPE] in PROXY_SSL_TYPES:
                    self.__negotiatessl(nexthop[0], nexthop[1], proxy,
                      insecure=(proxy[P_TYPE] == PROXY_TYPE_SSL_WEAK),
                      anonymous=(proxy[P_TYPE] == PROXY_TYPE_SSL_ANON))
                elif proxy[P_TYPE] != PROXY_TYPE_NONE or not first:
                    raise GeneralProxyError((4, _generalerrors[4]))

        if DEBUG: print '*** Connected!'
        return result

def wrapmodule(module):
    """wrapmodule(module)
    Attempts to replace a module's socket library with a SOCKS socket.
    This will only work on modules that import socket directly into the
    namespace; most of the Python Standard Library falls into this category.
    """
    module.socket.socket = socksocket
    module.socket.create_connection = sockcreateconn


## Netcat-like proxy-chaining tools follow ##

def __unblock(f):
    fd = f.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

def netcat(s, i, o):
    __unblock(s)
    __unblock(i)
    while True:
        in_r, out_r, err_r = select.select([s, i], [s, o], [s, i, o], 10)
        if s in in_r:
            data = s.recv(4096)
            if data == "": break
            o.write(data)
        if i in in_r:
            data = os.read(i.fileno(), 4096)
            if data == "":
                s.shutdown(socket.SHUT_WR)
            else:
                s.sendall(data)
    s.close()

def __proxy_connect_netcat(hostname, port, chain):
    try:
        s = socksocket(socket.AF_INET, socket.SOCK_STREAM)
        for proxy in chain:
            s.setproxy(*proxy, append=True)
        s.connect((hostname, port))
    except Exception, e:
        sys.stderr.write('Error: %s\n' % e)
        return False
    netcat(s, sys.stdin, sys.stdout)
    return True

def __make_proxy_chain(args):
    chain = []
    for arg in args:
        chain.append(parseproxy(arg))
    return chain

if __name__ == "__main__":
    usesystemdefaults()
    try:
        args = sys.argv[1:]
        if '--debug' in args:
            DEBUG = True
            args.remove('--debug')

        dest_host, dest_port = args.pop().split(':', 1)
        dest_port = int(dest_port)
        chain = __make_proxy_chain(args)
    except:
        sys.stderr.write(('Usage: %s '
                          '[<proto:proxy:port> [<proto:proxy:port> ...]] '
                          '<host:port>\n') % sys.argv[0])
        sys.exit(1)

    try:
        if not __proxy_connect_netcat(dest_host, dest_port, chain):
            sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(0)

