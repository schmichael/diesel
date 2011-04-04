# vim:ts=4:sw=4:expandtab
'''Core implementation/handling of generators, including
the various yield tokens.
'''
import socket
import traceback
import errno
import sys
import itertools
from OpenSSL import SSL
from greenlet import greenlet

from diesel import pipeline
from diesel import buffer
from diesel.security import ssl_async_handshake
from diesel import runtime
from diesel import log

class ConnectionClosed(socket.error):
    '''Raised if the client closes the connection.
    '''
    def __init__(self, msg, buffer=None):
        socket.error.__init__(self, msg)
        self.buffer = buffer

class ClientConnectionError(socket.error):
    '''Raised if a client cannot connect.
    '''

class ClientConnectionTimeout(socket.error):
    '''Raised if the client connection timed out before succeeding.
    '''

class LoopKeepAlive(Exception):
    '''Raised when an exception occurs that causes a loop to terminate;
    allows the app to re-schedule keep_alive loops.
    '''

class ParentDiedException(Exception):
    '''Raised when the parent (assigned via fork_child) has died.
    '''

class TerminateLoop(Exception):
    '''Raised to terminate the current loop, closing the socket if there
    is one associated with the loop.
    '''

CRLF = '\r\n'
BUFSIZ = 2 ** 14

def until(*args, **kw):
    return current_loop.input_op(*args, **kw)

def until_eol():
    return until(CRLF)

def receive(*args, **kw):
    return current_loop.input_op(*args, **kw)

def send(*args, **kw):
    return current_loop.send(*args, **kw)

def wait(*args, **kw):
    return current_loop.wait(*args, **kw)

def fire(*args, **kw):
    return current_loop.fire(*args, **kw)

def sleep(*args, **kw):
    return current_loop.sleep(*args, **kw)

def thread(*args, **kw):
    return current_loop.thread(*args, **kw)

def _private_connect(*args, **kw):
    return current_loop.connect(*args, **kw)

def first(*args, **kw):
    return current_loop.first(*args, **kw)

def label(*args, **kw):
    return current_loop.label(*args, **kw)

def fork(*args, **kw):
    return current_loop.fork(False, *args, **kw)

def fork_child(*args, **kw):
    return current_loop.fork(True, *args, **kw)

class call(object):
    def __init__(self, f, inst=None):
        self.f = f
        self.client = inst

    def __get__(self, inst, cls):
        return call(self.f, inst)

    def __call__(self, *args, **kw):
        if not self.client.connected:
            raise ConnectionClosed(
                    "ClientNotConnected: client is not connected")
        if self.client.is_closed:
            raise ConnectionClosed(
                    "Client call failed: client connection was closed")
        current_loop.connection_stack.append(self.client.conn)
        try:
            r = self.f(self.client, *args, **kw)
        finally:
            current_loop.connection_stack.pop()
        return r

current_loop = None

ContinueNothing = object()

def identity(cb): return cb

ids = itertools.count(1)

class Loop(object):
    def __init__(self, loop_callable, *args, **kw):
        self.loop_callable = loop_callable
        self.loop_label = str(self.loop_callable)
        self.args = args
        self.kw = kw
        self.keep_alive = False
        self.hub = runtime.current_app.hub
        self.app = runtime.current_app
        self.id = ids.next()
        self.children = set()
        self.parent = None
        self.reset()

    def reset(self):
        self.running = False
        self._wakeup_timer = None
        self.fire_handlers = {}
        self.connection_stack = []
        self.coroutine = None

    def run(self):
        from diesel.app import ApplicationEnd
        self.running = True
        self.app.running.add(self)
        try:
            self.loop_callable(*self.args, **self.kw)
        except TerminateLoop:
            pass
        except (SystemExit, KeyboardInterrupt, ApplicationEnd):
            raise
        except:
            log.error("-- Unhandled Exception in local loop <%s> --" % self.loop_label)
            log.error(traceback.format_exc())
        finally:
            if self.connection_stack:
                assert len(self.connection_stack) == 1
                self.connection_stack.pop().close()
        self.running = False
        self.app.running.remove(self)
        self.notify_children()
        if self.parent and self in self.parent.children:
            self.parent.children.remove(self)
            self.parent = None

        if self.keep_alive:
            log.warn("(Keep-Alive loop %s died; restarting)" % self)
            self.reset()
            self.hub.call_later(0.5, self.wake)

    def notify_children(self):
        for c in self.children:
            c.parent_died()

    def __hash__(self):
        return self.id

    def __str__(self):
        return '<Loop id=%s callable=%s>' % (self.id,
        str(self.loop_callable))
        
    def clear_pending_events(self):
        '''When a loop is rescheduled, cancel any other timers or waits.
        '''
        if self._wakeup_timer and self._wakeup_timer.pending:
            self._wakeup_timer.cancel()
        if self.connection_stack:
            self.connection_stack[-1].buffer.clear_term()
            self.connection_stack[-1].waiting_callback = None
        self.fire_handlers = {}
        self.app.waits.clear(self)

    def thread(self, f, *args, **kw):
        self.hub.run_in_thread(self.wake, f, *args, **kw)
        return self.dispatch()

    def fork(self, make_child, f, *args, **kw):
        def wrap():
            return f(*args, **kw)
        l = Loop(wrap)
        if make_child:
            self.children.add(l)
            l.parent = self
        self.app.add_loop(l)
        return l

    def parent_died(self):
        if self.running:
            self.hub.schedule(lambda: self.wake(ParentDiedException()))

    def label(self, label):
        self.loop_label = label

    def first(self, sleep=None, waits=None,
            receive=None, until=None, until_eol=None):
        def marked_cb(kw):
            def deco(f):
                def mark(d):
                    if isinstance(d, Exception):
                        return f(d)
                    return f((kw, d))
                return mark
            return deco

        f_sent = filter(None, (receive, until, until_eol))
        assert len(f_sent) <= 1,(
        "only 1 of (receive, until, until_eol) may be provided")
        sentinel = None
        if receive:
            sentinel = receive
            tok = 'receive'
        elif until:
            sentinel = until
            tok = 'until'
        elif until_eol:
            sentinel = "\r\n"
            tok = 'until_eol'
        if sentinel:
            early_val = self._input_op(sentinel, marked_cb(tok))
            if early_val:
                return tok, early_val
            # othewise.. process others and dispatch

        if sleep is not None:
            self._sleep(sleep, marked_cb('sleep'))

        if waits:
            for w in waits:
                self._wait(w, marked_cb('wait-' + w))
        return self.dispatch()

    def connect(self, client, ip, sock, timeout=None):
        def cancel_callback(sock):
            self.hub.unregister(sock)
            sock.close()
            self.hub.schedule(lambda: self.wake(
                ClientConnectionTimeout("connection timeout")
                ))

        def connect_callback():
            if cancel_timer is not None:
                cancel_timer.cancel()
            self.hub.unregister(sock)
            def finish():
                client.conn = Connection(fsock, ip)
                client.connected = True
                self.hub.schedule(
                lambda: self.wake()
                )

            if client.ssl_ctx:
                fsock = SSL.Connection(client.ssl_ctx, sock)
                fsock.setblocking(0)
                fsock.set_connect_state()
                ssl_async_handshake(fsock, self.hub, finish)
            else:
                fsock = sock
                finish()

        def error_callback():
            if cancel_timer is not None:
                cancel_timer.cancel()
            self.hub.unregister(sock)
            self.hub.schedule(
            lambda: self.wake(
                    ClientConnectionError("odd error on connect()!")
                ))

        def read_callback():
            self.hub.unregister(sock)
            try:
                s = sock.recv(100)
            except socket.error, e:
                self.hub.schedule(
                    lambda: self.wake(
                        ClientConnectionError(str(e))
                    ))


        cancel_timer = None
        if timeout is not None:
            cancel_timer = self.hub.call_later(timeout, cancel_callback, sock)

        self.hub.register(sock, read_callback, connect_callback, error_callback)
        self.hub.enable_write(sock)
        try:
            return self.dispatch()
        except ClientConnectionError:
            if cancel_timer is not None:
                cancel_timer.cancel()
            raise

    def sleep(self, v=0):
        self._sleep(v)
        return self.dispatch()

    def _sleep(self, v, cb_maker=identity):
        cb = lambda: cb_maker(self.wake)(True)
        assert v >= 0

        if v > 0:
            self._wakeup_timer = self.hub.call_later(v, cb)
        else:
            self.hub.schedule(cb)

    def fire_in(self, what, value):
        if what in self.fire_handlers:
            handler = self.fire_handlers.pop(what)
            self.fire_handlers = {}
            handler(value)

    def wait(self, event):
        self._wait(event)
        return self.dispatch()

    def _wait(self, event, cb_maker=identity):
        rcb = cb_maker(self.wake)
        def cb(d):
            def call_in():
                rcb(d)
            self.hub.schedule(call_in)
        self.fire_handlers[event] = cb
        self.app.waits.wait(self, event)

    def fire(self, event, value=None):
        self.app.waits.fire(event, value)

    def dispatch(self):
        r = self.app.runhub.switch()
        return r

    def wake(self, value=ContinueNothing):
        '''Wake up this loop.  Called by the main hub to resume a loop
        when it is rescheduled.
        '''
        global current_loop
        if self.coroutine is None:
            self.coroutine = greenlet(self.run)
            assert self.coroutine.parent == runtime.current_app.runhub
        self.clear_pending_events()
        current_loop = self
        if isinstance(value, Exception):
            self.coroutine.throw(value)
        elif value is not ContinueNothing:
            self.coroutine.switch(value)
        else:
            self.coroutine.switch()

    def input_op(self, sentinel_or_receive):
        v = self._input_op(sentinel_or_receive)
        if v:
            return v
        else:
            return self.dispatch()

    def _input_op(self, sentinel, cb_maker=identity):
        conn = self.check_connection()
        cb = cb_maker(self.wake)
        res = conn.buffer.set_term(sentinel)
        return self.check_buffer(conn, cb)

    def check_buffer(self, conn, cb):
        res = conn.buffer.check()
        if res:
            return res
        conn.waiting_callback = cb
        return None

    def check_connection(self):
        try:
            conn = self.connection_stack[-1]
        except IndexError:
            raise RuntimeError("Cannot complete socket operation: no associated connection")
        if conn.closed:
            raise ConnectionClosed("Cannot complete socket operation: associated connection is closed")
        return conn

    def send(self, o, priority=5):
        conn = self.check_connection()
        conn.pipeline.add(o, priority)
        conn.set_writable(True)

class Connection(object):
    def __init__(self, sock, addr):
        self.hub = runtime.current_app.hub
        self.pipeline = pipeline.Pipeline()
        self.buffer = buffer.Buffer()
        self.sock = sock
        self.addr = addr
        self.hub.register(sock, self.handle_read, self.handle_write, self.handle_error)
        self._writable = False
        self.closed = False
        self.waiting_callback = None

    def set_writable(self, val):
        '''Set the associated socket writable.  Called when there is
        data on the outgoing pipeline ready to be delivered to the 
        remote host.
        '''
        if self.closed:
            return
        if val and not self._writable:
            self.hub.enable_write(self.sock)
            self._writable = True
            return
        if not val and self._writable:
            self.hub.disable_write(self.sock)
            self._writable = False

    def close(self):
        self.set_writable(True)
        self.pipeline.close_request()
        
    def shutdown(self, remote_closed=False):
        '''Clean up after a client disconnects or after
        the connection_handler ends (and we disconnect).
        '''
        self.hub.unregister(self.sock)
        self.closed = True
        self.sock.close()

        if remote_closed and self.waiting_callback:
            self.waiting_callback(
            ConnectionClosed('Connection closed by remote host',
            self.buffer.pop()))

    def handle_write(self):
        '''The low-level handler called by the event hub
        when the socket is ready for writing.
        '''
        if not self.pipeline.empty and not self.closed:
            try:
                data = self.pipeline.read(BUFSIZ)
            except pipeline.PipelineCloseRequest:
                self.shutdown()
            else:
                try:
                    bsent = self.sock.send(data)
                except socket.error, e:
                    code, s = e
                    if code in (errno.EAGAIN, errno.EINTR):
                        self.pipeline.backup(data)
                        return 
                    self.shutdown(True)
                except (SSL.WantReadError, SSL.WantWriteError, SSL.WantX509LookupError):
                    self.pipeline.backup(data)
                    return
                except SSL.ZeroReturnError:
                    self.shutdown(True)
                except SSL.SysCallError:
                    self.shutdown(True)
                except:
                    sys.stderr.write("Unknown Error on send():\n%s"
                    % traceback.format_exc())
                    self.shutdown(True)

                else:
                    if bsent != len(data):
                        self.pipeline.backup(data[bsent:])

                    if not self.pipeline.empty:
                        return 
                    else:
                        self.set_writable(False)

    def handle_read(self):
        '''The low-level handler called by the event hub
        when the socket is ready for reading.
        '''
        if self.closed:
            return
        try:
            data = self.sock.recv(BUFSIZ)
        except socket.error, e:
            code, s = e
            if code in (errno.EAGAIN, errno.EINTR):
                return
            data = ''
        except (SSL.WantReadError, SSL.WantWriteError, SSL.WantX509LookupError):
            return
        except SSL.ZeroReturnError:
            data = ''
        except SSL.SysCallError:
            data = ''
        except:
            sys.stderr.write("Unknown Error on recv():\n%s"
            % traceback.format_exc())
            data = ''

        if not data:
            self.shutdown(True)
        else:
            res = self.buffer.feed(data)
            if res:
                self.waiting_callback(res)

    def handle_error(self):
        self.shutdown(True)
