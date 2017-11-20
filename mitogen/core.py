# Copyright 2017, David Wilson
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# 3. Neither the name of the copyright holder nor the names of its contributors
# may be used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import Queue
import cPickle
import cStringIO
import collections
import errno
import fcntl
import imp
import itertools
import logging
import os
import select
import signal
import socket
import struct
import sys
import threading
import time
import traceback
import zlib

if 0:
    from typing import * # pylint: disable=import-error
    from types import ModuleType
    _T = TypeVar('_T')
else:
    from typing import cast, overload
    from typing import Iterable


LOG = logging.getLogger('mitogen')
IOLOG = logging.getLogger('mitogen.io')
IOLOG.setLevel(logging.INFO)

GET_MODULE = 100
CALL_FUNCTION = 101
FORWARD_LOG = 102
ADD_ROUTE = 103
ALLOCATE_ID = 104
SHUTDOWN = 105

CHUNK_SIZE = 16384


if __name__ == 'mitogen.core':
    # When loaded using import mechanism, ExternalContext.main() will not have
    # a chance to set the synthetic mitogen global, so just import it here.
    import mitogen
else:
    # When loaded as __main__, ensure classes and functions gain a __module__
    # attribute consistent with the host process, so that pickling succeeds.
    __name__ = 'mitogen.core'


class Error(Exception):
    def __init__(self, fmt, *args):
        # type: (str, object) -> None
        if args:
            fmt %= args
        Exception.__init__(self, fmt)


class SecurityError(Error):
    pass


class CallError(Error):
    def __init__(self, e):
        # type: (object) -> None
        s = '%s.%s: %s' % (type(e).__module__, type(e).__name__, e)
        tb = sys.exc_info()[2]
        if tb:
            s += '\n'
            s += ''.join(traceback.format_tb(tb))
        Error.__init__(self, s)

    def __reduce__(self):
        # type: () -> Tuple[Callable, Tuple]
        return (_unpickle_call_error, (self[0],))


def _unpickle_call_error(s):
    # type: (str) -> CallError
    assert type(s) is str and len(s) < 10000
    inst = CallError.__new__(CallError) #  type: CallError
    Exception.__init__(inst, s)
    return inst


class ChannelError(Error):
    remote_msg = 'Channel closed by remote end.'
    local_msg = 'Channel closed by local end.'


class StreamError(Error):
    pass


class TimeoutError(StreamError):
    pass


class Dead(object):
    def __eq__(self, other):
        # type: (object) -> bool
        return type(other) is Dead

    def __reduce__(self):
        # type: () -> Tuple[Callable, Tuple]
        return (_unpickle_dead, ())

    def __repr__(self):
        # type: () -> str
        return '<Dead>'


def _unpickle_dead():
    # type: () -> Dead
    return _DEAD


_DEAD = Dead() # type: Dead


def listen(obj, name, func):
    # type: (object, str, Callable) -> None
    signals = vars(obj).setdefault('_signals', {})
    signals.setdefault(name, []).append(func)


def fire(obj, name, *args, **kwargs):
    #type: (Any, str, Any, Any) -> List
    signals = vars(obj).get('_signals', {})
    return [func(*args, **kwargs) for func in signals.get(name, ())]


def takes_econtext(func):
    # type: (Callable) -> Callable
    func.mitogen_takes_econtext = True
    return func


def takes_router(func):
    # type: (Callable) -> Callable
    func.mitogen_takes_router = True
    return func


def set_cloexec(fd):
    # type: (int) -> None
    flags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)


def set_nonblock(fd):
    # type: (int) -> None
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)


@overload
def io_op(func, *args):
    # type: (Callable[[int, str], int], Any) -> Tuple[Optional[int], bool]
    pass


@overload
def io_op(func, *args):
    # type: (Callable[[int, int], str], Any) -> Tuple[Optional[str], bool]
    pass

def io_op(func, *args): 
    # type: (Union[Callable[[int, str], int], Callable[[int, int], str]], Any) -> Tuple[Union[Optional[int], Optional[str]], bool]
    try:
        return func(*args), False
    except OSError, e:
        IOLOG.debug('io_op(%r) -> OSError: %s', func, e)
        if e.errno not in (errno.EIO, errno.ECONNRESET, errno.EPIPE):
            raise
        return None, True


def enable_debug_logging():
    # type: () -> None
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    IOLOG.setLevel(logging.DEBUG)
    fp = open('/tmp/mitogen.%s.log' % (os.getpid(),), 'w', 1)
    set_cloexec(fp.fileno())
    handler = logging.StreamHandler(fp)
    handler.formatter = logging.Formatter(
        '%(asctime)s %(levelname).1s %(name)s: %(message)s',
        '%H:%M:%S'
    )
    root.handlers.insert(0, handler)


def _profile_hook(name, func, *args):
    # type: (str, Callable[..., None], Any) -> None
    # Callable[[str, Callable, Any], Any]
    func(*args)

def enable_profiling():
    # type: () -> None
    global _profile_hook
    import cProfile, pstats
    def _profile_hook(name, func, *args):
        # type: (str, Callable, Any) -> Any
        profiler = cProfile.Profile()
        profiler.enable()
        try:
            return func(*args)
        finally:
            profiler.create_stats()
            fp = open('/tmp/mitogen.stats.%d.%s.log' % (os.getpid(), name), 'w')
            try:
                stats = pstats.Stats(profiler, stream=fp)
                stats.sort_stats('cumulative')
                stats.print_stats()
            finally:
                fp.close()


class Message(object):
    dst_id = None       # type: int
    src_id = None       # type: int
    handle = None       # type: int
    reply_to = None     # type: Optional[int]
    data = ''           # type: bytes

    router = None       # type: Router

    def __init__(self, **kwargs):
        # type: (Any) -> None
        self.src_id = mitogen.context_id
        vars(self).update(kwargs)

    def _unpickle_context(self, context_id, name):
        # type: (int, str) -> Context
        if self.router is not None:
            return _unpickle_context(self.router, context_id, name)
        else:
            raise Exception

    def _find_global(self, module, func):
        # type: (str, Callable) -> Union[Callable, Context]
        """Return the class implementing `module_name.class_name` or raise
        `StreamError` if the module is not whitelisted."""
        if module == __name__:
            if func == '_unpickle_call_error':
                return _unpickle_call_error
            elif func == '_unpickle_dead':
                return _unpickle_dead
            elif func == '_unpickle_context':
                return self._unpickle_context

        raise StreamError('cannot unpickle %r/%r', module, func)

    @classmethod
    def pickled(cls, obj, **kwargs):
        # type: (Any, Any) -> Message
        self = cls(**kwargs)
        try:
            self.data = cPickle.dumps(obj, protocol=2)
        except cPickle.PicklingError, e:
            self.data = cPickle.dumps(CallError(e), protocol=2)
        return self

    def unpickle(self):
        # type: () -> Any
        """Deserialize `data` into an object."""
        IOLOG.debug('%r.unpickle()', self)
        fp = cStringIO.StringIO(self.data)
        unpickler = cPickle.Unpickler(fp)
        unpickler.find_global = self._find_global
        try:
            return unpickler.load()
        except (TypeError, ValueError), ex:
            raise StreamError('invalid message: %s', ex)

    def __repr__(self):
        # type: () -> str
        return 'Message(%r, %r, %r, %r, %r..%d)' % (
            self.dst_id, self.src_id, self.handle, self.reply_to,
            (self.data or '')[:50], len(self.data)
        )


class Sender(object):
    def __init__(self, context, dst_handle):
        #type: (Context, Any) -> None
        self.context = context
        self.dst_handle = dst_handle

    def __repr__(self):
        # type: () -> str
        return 'Sender(%r, %r)' % (self.context, self.dst_handle)

    def close(self):
        # type: () -> None
        """Indicate this channel is closed to the remote side."""
        IOLOG.debug('%r.close()', self)
        self.context.send(
            Message.pickled(
                _DEAD,
                handle=self.dst_handle
            )
        )

    def put(self, data):
        # type: (Any) -> None
        """Send `data` to the remote."""
        IOLOG.debug('%r.put(%r..)', self, data[:100])
        self.context.send(
            Message.pickled(
                data,
                handle=self.dst_handle
            )
        )


def _queue_interruptible_get(queue, timeout=None, block=True):
    # type: (Queue.Queue, Optional[float], bool) -> Any
    # bool is subclass of int, cannot use isinstance!
    assert timeout is None or type(timeout) in (int, long, float)
    assert isinstance(block, bool)

    if timeout is not None:
        timeout += time.time()

    msg = None
    while msg is None and (timeout is None or timeout > time.time()):
        try:
            msg = queue.get(block, 0.5)
        except Queue.Empty:
            if not block:
                break

    if msg is None:
        raise TimeoutError('deadline exceeded.')

    return msg


class Receiver(Iterable):
    notify = None # type: Optional[Callable[[Receiver], None]]
    raise_channelerror = True

    def __init__(self, router, handle=None, persist=True, respondent=None):
        # type: (Router, Optional[int], bool, Optional[Context]) -> None
        self.router = router
        self.handle = handle  # Avoid __repr__ crash in add_handler()
        self.handle = router.add_handler(self._on_receive, handle,
                                         persist, respondent)
        self._queue = Queue.Queue() # type: Queue.Queue[Union[Message, Dead]]

    def __repr__(self):
        # type: () -> str
        return 'Receiver(%r, %r)' % (self.router, self.handle)

    def _on_receive(self, msg):
        # type: (Message) -> None
        """Callback from the Stream; appends data to the internal queue."""
        IOLOG.debug('%r._on_receive(%r)', self, msg)
        self._queue.put(msg)
        if self.notify:
            self.notify(self) # pylint: disable=not-callable

    def close(self):
        # type: () -> None
        self._queue.put(_DEAD)

    def empty(self):
        # type: () -> bool
        return self._queue.empty()

    def get(self, timeout=None, block=True):
        # type: (Optional[Union[int, float]], bool) -> Tuple[Message, Any]
        IOLOG.debug('%r.get(timeout=%r, block=%r)', self, timeout, block)

        msg = _queue_interruptible_get(self._queue, timeout, block=block)
        IOLOG.debug('%r.get() got %r', self, msg)

        if msg == _DEAD:
            raise ChannelError(ChannelError.local_msg)

        # Must occur off the broker thread.
        data = msg.unpickle()
        if data == _DEAD and self.raise_channelerror:
            raise ChannelError(ChannelError.remote_msg)

        if isinstance(data, CallError):
            raise data

        return msg, data

    def get_data(self, timeout=None):
        # type: (Optional[Union[int, float]]) -> Any
        return self.get(timeout)[1]

    def __iter__(self):
        # type: () -> Iterator[Tuple[Message, Any]]
        while True:
            try:
                yield self.get()
            except ChannelError:
                return


class Channel(Sender, Receiver):
    def __init__(self, router, context, dst_handle, handle=None):
        # type: (Router, Context, Any, Optional[Any]) -> None
        Sender.__init__(self, context, dst_handle)
        Receiver.__init__(self, router, handle)

    def __repr__(self):
        # type: () -> str
        return 'Channel(%s, %s)' % (
            Sender.__repr__(self),
            Receiver.__repr__(self)
        )


class Importer(object):
    """
    Import protocol implementation that fetches modules from the parent
    process.

    :param context: Context to communicate via.
    """
    def __init__(self, context, core_src):
        # type: (Context, str) -> None
        self._context = context
        self._present = {'mitogen': [
            'mitogen.ansible',
            'mitogen.compat',
            'mitogen.compat.pkgutil',
            'mitogen.fakessh',
            'mitogen.master',
            'mitogen.ssh',
            'mitogen.sudo',
            'mitogen.utils',
        ]}
        self.tls = threading.local() # type: threading.local
        self._cache = {} # type: Dict[str, Tuple[None, str, str]]
        if core_src:
            self._cache['mitogen.core'] = (
                None,
                'mitogen/core.py',
                zlib.compress(core_src),
            )

    def __repr__(self):
        # type: () -> str
        return 'Importer()'

    def find_module(self, fullname, path=None):
        # type: (str, Optional[str]) -> Optional[Importer]
        if hasattr(self.tls, 'running'):
            return None

        self.tls.running = True
        fullname = fullname.rstrip('.')
        try:
            pkgname, _, _ = fullname.rpartition('.')
            LOG.debug('%r.find_module(%r)', self, fullname)
            if fullname not in self._present.get(pkgname, (fullname,)):
                LOG.debug('%r: master doesn\'t know %r', self, fullname)
                return None

            pkg = sys.modules.get(pkgname)
            if pkg and getattr(pkg, '__loader__', None) is not self:
                LOG.debug('%r: %r is submodule of a package we did not load',
                          self, fullname)
                return None

            try:
                __import__(fullname, {}, {}, [''])
                LOG.debug('%r: %r is available locally', self, fullname)
                return None
            except ImportError:
                LOG.debug('find_module(%r) returning self', fullname)
                return self
        finally:
            del self.tls.running

    def _load_module_hacks(self, fullname):
        # type: (str) -> None
        f = sys._getframe(2)
        requestee = f.f_globals['__name__']

        if fullname == '__main__' and requestee == 'pkg_resources':
            # Anything that imports pkg_resources will eventually cause
            # pkg_resources to try and scan __main__ for its __requires__
            # attribute (pkg_resources/__init__.py::_build_master()). This
            # breaks any app that is not expecting its __main__ to suddenly be
            # sucked over a network and injected into a remote process, like
            # py.test.
            raise ImportError('Refused')

        if fullname == 'pbr':
            # It claims to use pkg_resources to read version information, which
            # would result in PEP-302 being used, but it actually does direct
            # filesystem access. So instead smodge the environment to override
            # any version that was defined. This will probably break something
            # later.
            os.environ['PBR_VERSION'] = '0.0.0'

    def load_module(self, fullname):
        # type: (str) -> ModuleType
        LOG.debug('Importer.load_module(%r)', fullname)
        self._load_module_hacks(fullname)

        try:
            ret = self._cache[fullname]
        except KeyError:
            self._cache[fullname] = ret = (
                self._context.send_await(
                    Message(data=fullname, handle=GET_MODULE)
                )
            )

        if ret is None:
            raise ImportError('Master does not have %r' % (fullname,))

        pkg_present = ret[0]
        mod = sys.modules.setdefault(fullname, imp.new_module(fullname)) # type: ModuleType
        mod.__file__ = self.get_filename(fullname)
        assert mod.__file__
        mod.__loader__ = self
        if pkg_present is not None:  # it's a package.
            mod.__path__ = []
            mod.__package__ = fullname
            self._present[fullname] = pkg_present
        else:
            mod.__package__ = fullname.rpartition('.')[0] or None
        code = compile(self.get_source(fullname), mod.__file__, 'exec')
        exec code in vars(mod)
        return mod

    def get_filename(self, fullname):
        # type: (str) -> Optional[str]
        if fullname in self._cache:
            return 'master:' + self._cache[fullname][1]
        return None

    def get_source(self, fullname):
        # type: (str) -> Optional[str]
        if fullname in self._cache:
            return zlib.decompress(self._cache[fullname][2])
        return None


class LogHandler(logging.Handler):
    def __init__(self, context):
        # type: (Context) -> None
        logging.Handler.__init__(self)
        self.context = context
        self.local = threading.local()

    def emit(self, rec):
        # type: (logging.LogRecord) -> None
        if rec.name == 'mitogen.io' or \
           getattr(self.local, 'in_emit', False):
            return None

        self.local.in_emit = True
        try:
            msg = self.format(rec)
            encoded = '%s\x00%s\x00%s' % (rec.name, rec.levelno, msg)
            self.context.send(Message(data=encoded, handle=FORWARD_LOG))
        finally:
            self.local.in_emit = False
        return None


class Side(object):
    def __init__(self, stream, fd, keep_alive=True):
        # type: (BasicStream, int, bool) -> None
        self.stream = stream
        self.fd = fd # type: Optional[int]
        self.keep_alive = keep_alive
        set_nonblock(fd)

    def __repr__(self):
        # type: () -> str
        return '<Side of %r fd %s>' % (self.stream, self.fd)

    def fileno(self):
        # type: () -> int
        if self.fd is None:
            raise StreamError('%r.fileno() called but no FD set', self)
        return self.fd

    def close(self):
        # type: () -> None
        if self.fd is not None:
            IOLOG.debug('%r.close()', self)
            os.close(self.fd)
            self.fd = None

    def read(self, n=CHUNK_SIZE):
        # type: (int) -> str
        s, disconnected = io_op(os.read, self.fd, n)
        if disconnected:
            return ''
        return cast(str, s)

    def write(self, s):
        # type: (str) -> Optional[int]
        if self.fd is None:
            return None

        written, disconnected = io_op(os.write, self.fd, s[:CHUNK_SIZE])
        if disconnected:
            return None
        return cast(int, written)


class BasicStream(object):
    receive_side = None     # type: Side
    transmit_side = None    # type: Side

    def on_disconnect(self, broker):
        # type: (Broker) -> None
        LOG.debug('%r.on_disconnect()', self)
        broker.stop_receive(self)
        broker.stop_transmit(self)
        self.receive_side.close()
        self.transmit_side.close()
        fire(self, 'disconnect')

    def on_shutdown(self, broker):
        # type: (Broker) -> None
        LOG.debug('%r.on_shutdown()', self)
        fire(self, 'shutdown')
        self.on_disconnect(broker)


class Stream(BasicStream):
    """
    :py:class:`BasicStream` subclass implementing mitogen's :ref:`stream
    protocol <stream-protocol>`.
    """
    _input_buf = ''

    def __init__(self, router, remote_id, **kwargs):
        # type: (Router, int, Any) -> None
        self._router = router
        self.remote_id = remote_id
        self.name = 'default'
        self.construct(**kwargs)
        self._output_buf = collections.deque() # type: collections.deque

    def construct(self, **kwargs):
        # type: (Any) -> None
        pass

    def on_receive(self, broker):
        # type: (Broker) -> None
        """Handle the next complete message on the stream. Raise
        :py:class:`StreamError` on failure."""
        IOLOG.debug('%r.on_receive()', self)

        buf = self.receive_side.read()
        if buf is None:
            buf = ''

        self._input_buf += buf
        while self._receive_one(broker):
            pass

        if not buf:
            return self.on_disconnect(broker)

    HEADER_FMT = '>hhLLL'
    HEADER_LEN = struct.calcsize(HEADER_FMT)

    def _receive_one(self, broker):
        # type: (Broker) -> bool
        if len(self._input_buf) < self.HEADER_LEN:
            return False

        msg = Message()
        # To support unpickling Contexts.
        msg.router = self._router

        (msg.dst_id, msg.src_id,
         msg.handle, msg.reply_to, msg_len) = struct.unpack(
            self.HEADER_FMT,
            self._input_buf[:self.HEADER_LEN]
        )

        if (len(self._input_buf) - self.HEADER_LEN) < msg_len:
            IOLOG.debug('%r: Input too short (want %d, got %d)',
                        self, msg_len, len(self._input_buf) - self.HEADER_LEN)
            return False

        msg.data = self._input_buf[self.HEADER_LEN:self.HEADER_LEN+msg_len]
        self._input_buf = self._input_buf[self.HEADER_LEN+msg_len:]
        self._router._async_route(msg, self)
        return True

    def on_transmit(self, broker):
        # type: (Broker) -> None
        """Transmit buffered messages."""
        IOLOG.debug('%r.on_transmit()', self)

        if self._output_buf:
            buf = self._output_buf.popleft()
            written = self.transmit_side.write(buf)
            if not written:
                LOG.debug('%r.on_transmit(): disconnection detected', self)
                self.on_disconnect(broker)
                return
            elif written != len(buf):
                self._output_buf.appendleft(buf[written:])

            IOLOG.debug('%r.on_transmit() -> len %d', self, written)

        if not self._output_buf:
            broker.stop_transmit(self)

    def _send(self, msg):
        # type: (Message) -> None
        IOLOG.debug('%r._send(%r)', self, msg)
        pkt = struct.pack('>hhLLL', msg.dst_id, msg.src_id,
                          msg.handle, msg.reply_to or 0, len(msg.data)
        ) + msg.data
        self._output_buf.append(pkt)
        self._router.broker.start_transmit(self)

    def send(self, msg):
        # type: (Message) -> None
        """Send `data` to `handle`, and tell the broker we have output. May
        be called from any thread."""
        self._router.broker.defer(self._send, msg)

    def on_disconnect(self, broker):
        # type: (Broker) -> None
        super(Stream, self).on_disconnect(broker)
        self._router.on_disconnect(self, broker)

    def on_shutdown(self, broker):
        # type: (Broker) -> None
        """Override BasicStream behaviour of immediately disconnecting."""
        LOG.debug('%r.on_shutdown(%r)', self, broker)

    def accept(self, rfd, wfd):
        # type: (int, int) -> None
        # TODO: what is this os.dup for?
        self.receive_side = Side(self, os.dup(rfd))
        self.transmit_side = Side(self, os.dup(wfd))
        assert self.receive_side.fd
        set_cloexec(self.receive_side.fd)
        assert self.transmit_side.fd
        set_cloexec(self.transmit_side.fd)

    def __repr__(self):
        # type: () -> str
        cls = type(self)
        return '%s.%s(%r)' % (cls.__module__, cls.__name__, self.name)


class Context(object):
    remote_name = None

    def __init__(self, router, context_id, name=None):
        # type: (Router, int, Optional[str]) -> None
        self.router = router
        self.context_id = context_id
        self.name = name

    def __reduce__(self):
        # type: () -> Tuple[Callable[[Router, int, str], Context], Tuple[int, Optional[str]]]
        return _unpickle_context, (self.context_id, self.name)

    def on_disconnect(self, broker):
        # type: (Broker) -> None
        LOG.debug('Parent stream is gone, dying.')
        fire(self, 'disconnect')
        broker.shutdown()

    def on_shutdown(self, broker):
        # type: (Broker) -> None
        pass

    def send(self, msg):
        # type: (Message) -> None
        """send `obj` to `handle`, and tell the broker we have output. May
        be called from any thread."""
        msg.dst_id = self.context_id
        if msg.src_id is None:
            msg.src_id = mitogen.context_id
        self.router.route(msg)

    def send_async(self, msg, persist=False):
        # type: (Message, bool) -> Receiver
        if self.router.broker._thread == threading.currentThread():  # TODO
            raise SystemError('Cannot making blocking call on broker thread')

        receiver = Receiver(self.router, persist=persist, respondent=self) # type: Receiver
        msg.reply_to = receiver.handle

        LOG.debug('%r.send_async(%r)', self, msg)
        self.send(msg)
        return receiver

    def send_await(self, msg, deadline=None):
        # type: (Message, Optional[Union[int, float]]) -> Any
        """Send `msg` and wait for a response with an optional timeout."""
        receiver = self.send_async(msg)
        response = receiver.get_data(deadline)
        IOLOG.debug('%r._send_await() -> %r', self, response)
        return response

    def __repr__(self):
        # type: () -> str
        return 'Context(%s, %r)' % (self.context_id, self.name)


def _unpickle_context(router, context_id, name):
    # type: (Router, int, str) -> Context
    assert isinstance(router, Router)
    assert isinstance(context_id, (int, long)) and context_id > 0
    assert isinstance(name, basestring) and len(name) < 100
    return Context(router, context_id, name)


class Waker(BasicStream):
    """
    :py:class:`BasicStream` subclass implementing the
    `UNIX self-pipe trick`_. Used internally to wake the IO multiplexer when
    some of its state has been changed by another thread.

    .. _UNIX self-pipe trick: https://cr.yp.to/docs/selfpipe.html
    """
    def __init__(self, broker):
        # type: (Broker) -> None
        self._broker = broker
        rfd, wfd = os.pipe()
        set_cloexec(rfd)
        set_cloexec(wfd)
        self.receive_side = Side(self, rfd)
        self.transmit_side = Side(self, wfd)

    def __repr__(self):
        # type: () -> str
        return 'Waker(%r)' % (self._broker,)

    def wake(self):
        # type: () -> None
        """
        Write a byte to the self-pipe, causing the IO multiplexer to wake up.
        Nothing is written if the current thread is the IO multiplexer thread.
        """
        IOLOG.debug('%r.wake() [fd=%r]', self, self.transmit_side.fd)
        if threading.currentThread() != self._broker._thread:
            try:
                self.transmit_side.write(' ')
            except OSError, e:
                if e[0] != errno.EBADF:
                    raise

    def on_receive(self, broker):
        # type: (Broker) -> None
        """
        Read a byte from the self-pipe.
        """
        self.receive_side.read(256)


class IoLogger(BasicStream):
    """
    :py:class:`BasicStream` subclass that sets up redirection of a standard
    UNIX file descriptor back into the Python :py:mod:`logging` package.
    """
    _buf = ''

    def __init__(self, broker, name, dest_fd):
        # type: (Broker, str, int) -> None
        self._broker = broker
        self._name = name
        self._log = logging.getLogger(name)

        self._rsock, self._wsock = socket.socketpair()
        os.dup2(self._wsock.fileno(), dest_fd)
        set_cloexec(self._rsock.fileno())
        set_cloexec(self._wsock.fileno())

        self.receive_side = Side(self, self._rsock.fileno())
        self.transmit_side = Side(self, dest_fd)
        self._broker.start_receive(self)

    def __repr__(self):
        # type: () -> str
        return '<IoLogger %s>' % (self._name,)

    def _log_lines(self):
        # type: () -> None
        while self._buf.find('\n') != -1:
            line, _, self._buf = self._buf.partition('\n')
            self._log.info('%s', line.rstrip('\n'))

    def on_shutdown(self, broker):
        # type: (Broker) -> None
        """Shut down the write end of the logging socket."""
        LOG.debug('%r.on_shutdown()', self)
        self._wsock.shutdown(socket.SHUT_WR)
        self._wsock.close()
        self.transmit_side.close()

    def on_receive(self, broker):
        # type: (Broker) -> None
        IOLOG.debug('%r.on_receive()', self)
        assert self.receive_side.fd
        buf = os.read(self.receive_side.fd, CHUNK_SIZE)
        if not buf:
            return self.on_disconnect(broker)

        self._buf += buf
        self._log_lines()


class Router(object):
    def __init__(self, broker):
        # type: (Broker) -> None
        self.broker = broker
        listen(broker, 'shutdown', self.on_broker_shutdown)

        #: context ID -> Stream
        self._stream_by_id = {}     # type: Dict[int, Stream]
        #: List of contexts to notify of shutdown.
        self._context_by_id = {}    # type: Dict[int, Context]
        self._last_handle = itertools.count(1000)
        #: handle -> (persistent?, func(msg))
        self._handle_map = {
            ADD_ROUTE: (True, self._on_add_route)
        }

    def __repr__(self):
        # type: () -> str
        return 'Router(%r)' % (self.broker,)

    def on_disconnect(self, stream, broker):
        # type: (Stream, Broker) -> None
        """Invoked by Stream.on_disconnect()."""
        for context in self._context_by_id.itervalues():
            stream_ = self._stream_by_id.get(context.context_id)
            if stream_ is stream:
                del self._stream_by_id[context.context_id]
                context.on_disconnect(broker)

    def on_broker_shutdown(self):
        # type: () -> None
        for context in self._context_by_id.itervalues():
            context.on_shutdown(self.broker)

    def add_route(self, target_id, via_id):
        # type: (int, int) -> None
        LOG.debug('%r.add_route(%r, %r)', self, target_id, via_id)
        try:
            self._stream_by_id[target_id] = self._stream_by_id[via_id]
        except KeyError:
            LOG.error('%r: cant add route to %r via %r: no such stream',
                      self, target_id, via_id)

    def _on_add_route(self, msg):
        # type: (Union[Message, Dead]) -> None
        if isinstance(msg, Message) and msg != _DEAD:
            target_id, via_id = map(int, msg.data.split('\x00'))
            self.add_route(target_id, via_id)

    def register(self, context, stream):
        # type: (Context, Stream) -> None
        LOG.debug('register(%r, %r)', context, stream)
        self._stream_by_id[context.context_id] = stream
        self._context_by_id[context.context_id] = context
        self.broker.start_receive(stream)

    def add_handler(self, fn, handle=None, persist=True, respondent=None):
        # type: (Callable, Optional[int], bool, Optional[Context]) -> int
        handle = handle or self._last_handle.next()
        IOLOG.debug('%r.add_handler(%r, %r, %r)', self, fn, handle, persist)
        self._handle_map[handle] = persist, fn

        if respondent:
            def on_disconnect():
                # type: () -> None
                if handle in self._handle_map:
                    fn(_DEAD)
                    del self._handle_map[handle]
            listen(respondent, 'disconnect', on_disconnect)

        return handle

    def on_shutdown(self, broker):
        # type: (Broker) -> None
        """Called during :py:meth:`Broker.shutdown`, informs callbacks
        registered with :py:meth:`add_handle_cb` the connection is dead."""
        LOG.debug('%r.on_shutdown(%r)', self, broker)
        fire(self, 'shutdown')
        for handle, (persist, fn) in self._handle_map.iteritems():
            LOG.debug('%r.on_shutdown(): killing %r: %r', self, handle, fn)
            fn(_DEAD)

    def _invoke(self, msg):
        # type: (Message) -> None
        #IOLOG.debug('%r._invoke(%r)', self, msg)
        try:
            persist, fn = self._handle_map[msg.handle]
        except KeyError:
            LOG.error('%r: invalid handle: %r', self, msg)
            return

        if not persist:
            del self._handle_map[msg.handle]

        try:
            fn(msg)
        except Exception:
            LOG.exception('%r._invoke(%r): %r crashed', self, msg, fn)

    def _async_route(self, msg, stream=None):
        # type: (Message, Optional[Stream]) -> None
        IOLOG.debug('%r._async_route(%r, %r)', self, msg, stream)
        # Perform source verification.
        if stream is not None:
            # MyPy claims (wrongly) Mapping[T, ...].get(k=None) with k==None
            # is invalid, see https://github.com/python/typing/issues/448
            parent_stream = self._stream_by_id.get(mitogen.parent_id) # type: ignore
            expected_stream = self._stream_by_id.get(msg.src_id, parent_stream)
            if stream != expected_stream:
                LOG.error('%r: bad source: got %r from %r, should be from %r',
                          self, msg, stream, expected_stream)

        if msg.dst_id == mitogen.context_id:
            # FIXME Why is this returning, when self._invoke() has no return?
            return self._invoke(msg)

        stream = self._stream_by_id.get(msg.dst_id)
        if stream is None:
            # MyPy claims (wrongly) Mapping[T, ...].get(k=None) with k==None
            # is invalid, see https://github.com/python/typing/issues/448
            stream = self._stream_by_id.get(mitogen.parent_id) # type: ignore

        if stream is None:
            LOG.error('%r: no route for %r, my ID is %r',
                      self, msg, mitogen.context_id)
            return

        stream.send(msg)

    def route(self, msg):
        # type: (Message) -> None
        self.broker.defer(self._async_route, msg)


class Broker(object):
    _waker = None   # type: Waker
    _thread = None  # type: threading.Thread
    shutdown_timeout = 3.0

    def __init__(self):
        # type: () -> None
        self._alive = True
        self._queue = Queue.Queue() # type: Queue.Queue[Tuple[Callable, Any, Any]]
        self._readers = set()       # type: Set[Side]
        self._writers = set()       # type: Set[Side]
        self._waker = Waker(self)
        self.start_receive(self._waker)
        self._thread = threading.Thread(
            target=_profile_hook,
            args=('broker', self._broker_main),
            name='mitogen-broker'
        )
        self._thread.start()

    def defer(self, func, *args, **kwargs):
        # type: (Callable, Any, Any) -> None
        if threading.currentThread() == self._thread:
            func(*args, **kwargs)
        else:
            self._queue.put((func, args, kwargs))
            self._waker.wake()

    def start_receive(self, stream):
        # type: (BasicStream) -> None
        IOLOG.debug('%r.start_receive(%r)', self, stream)
        assert stream.receive_side and stream.receive_side.fd is not None
        self.defer(self._readers.add, stream.receive_side)

    def stop_receive(self, stream):
        # type: (BasicStream) -> None
        IOLOG.debug('%r.stop_receive(%r)', self, stream)
        self.defer(self._readers.discard, stream.receive_side)

    def start_transmit(self, stream):
        # type: (BasicStream) -> None
        IOLOG.debug('%r.start_transmit(%r)', self, stream)
        assert stream.transmit_side and stream.transmit_side.fd is not None
        self.defer(self._writers.add, stream.transmit_side)

    def stop_transmit(self, stream):
        # type: (BasicStream) -> None
        IOLOG.debug('%r.stop_transmit(%r)', self, stream)
        self.defer(self._writers.discard, stream.transmit_side)

    def _call(self, stream, func):
        # type: (BasicStream, Callable) -> None
        try:
            func(self)
        except Exception:
            LOG.exception('%r crashed', stream)
            stream.on_disconnect(self)

    def _run_defer(self):
        # type: () -> None
        while not self._queue.empty():
            func, args, kwargs = self._queue.get()
            try:
                func(*args, **kwargs)
            except Exception:
                LOG.exception('defer() crashed: %r(*%r, **%r)',
                              func, args, kwargs)
                self.shutdown()

    def _loop_once(self, timeout=None):
        # type: (Optional[Union[int, float]]) -> None
        IOLOG.debug('%r._loop_once(%r)', self, timeout)
        self._run_defer()

        #IOLOG.debug('readers = %r', self._readers)
        #IOLOG.debug('writers = %r', self._writers)
        # https://github.com/dw/mitogen/issues/68
        rsides, wsides, _ = select.select(
            sorted(self._readers),
            sorted(self._writers),
            (),
            timeout,
        )
        for side in rsides:
            IOLOG.debug('%r: POLLIN for %r', self, side)
            self._call(side.stream, side.stream.on_receive)

        for side in wsides:
            IOLOG.debug('%r: POLLOUT for %r', self, side)
            self._call(side.stream, side.stream.on_transmit)

    def keep_alive(self):
        # type: () -> int
        return (sum((side.keep_alive for side in self._readers), 0) +
                (not self._queue.empty()))

    def _broker_main(self):
        # type: () -> None
        try:
            while self._alive:
                self._loop_once()

            self._run_defer()
            fire(self, 'shutdown')

            for side in self._readers | self._writers:
                self._call(side.stream, side.stream.on_shutdown)

            deadline = time.time() + self.shutdown_timeout
            while self.keep_alive() and time.time() < deadline:
                self._loop_once(max(0, deadline - time.time()))

            if self.keep_alive():
                LOG.error('%r: some streams did not close gracefully. '
                          'The most likely cause for this is one or '
                          'more child processes still connected to '
                          'our stdout/stderr pipes.', self)

            for side in self._readers | self._writers:
                LOG.error('_broker_main() force disconnecting %r', side)
                side.stream.on_disconnect(self)
        except Exception:
            LOG.exception('_broker_main() crashed')

        fire(self, 'exit')

    def shutdown(self):
        # type: () -> None
        LOG.debug('%r.shutdown()', self)
        self._alive = False
        self._waker.wake()

    def join(self):
        # type: () -> None
        self._thread.join()

    def __repr__(self):
        # type: () -> str
        return 'Broker()'


class ExternalContext(object):
    def _on_broker_shutdown(self):
        # type: () -> None
        self.channel.close()

    def _on_broker_exit(self):
        # type: () -> None
        os.kill(os.getpid(), signal.SIGTERM)

    def _on_shutdown_msg(self, msg):
        # type: (Message) -> None
        LOG.debug('_on_shutdown_msg(%r)', msg)
        if msg.src_id != mitogen.parent_id:
            LOG.warning('Ignoring SHUTDOWN from non-parent: %r', msg)
            return
        self.broker.shutdown()

    def _setup_master(self, profiling, parent_id, context_id, in_fd, out_fd):
        # type: (bool, int, int, int, int) -> None
        if profiling:
            enable_profiling()
        self.broker = Broker()
        self.router = Router(self.broker)
        self.router.add_handler(self._on_shutdown_msg, SHUTDOWN)
        self.master = Context(self.router, 0, 'master')
        if parent_id == 0:
            self.parent = self.master
        else:
            self.parent = Context(self.router, parent_id, 'parent')

        self.channel = Receiver(self.router, CALL_FUNCTION)
        self.stream = Stream(self.router, parent_id)
        self.stream.name = 'parent'
        self.stream.accept(in_fd, out_fd)
        self.stream.receive_side.keep_alive = False

        listen(self.broker, 'shutdown', self._on_broker_shutdown)
        listen(self.broker, 'exit', self._on_broker_exit)

        os.close(in_fd)
        try:
            os.wait()  # Reap first stage.
        except OSError:
            pass  # No first stage exists (e.g. fakessh)

    def _setup_logging(self, debug, log_level):
        # type: (bool, int) -> None
        root = logging.getLogger()
        root.setLevel(log_level)
        root.handlers = [LogHandler(self.master)]
        if debug:
            enable_debug_logging()

    def _setup_importer(self, core_src_fd):
        # type: (int) -> None
        if core_src_fd:
            with os.fdopen(101, 'r', 1) as fp:
                core_size = int(fp.readline())
                core_src = fp.read(core_size)
                # Strip "ExternalContext.main()" call from last line.
                core_src = '\n'.join(core_src.splitlines()[:-1])
                fp.close()
        else:
            core_src = None

        self.importer = Importer(self.parent, core_src)
        sys.meta_path.append(self.importer)

    def _setup_package(self, context_id, parent_ids):
        # type: (int, List[int]) -> None
        global mitogen
        mitogen = imp.new_module('mitogen')
        mitogen.__package__ = 'mitogen'
        mitogen.__path__ = []
        mitogen.__loader__ = self.importer
        mitogen.is_master = False
        mitogen.context_id = context_id
        mitogen.parent_ids = parent_ids
        mitogen.parent_id = parent_ids[0]
        mitogen.core = sys.modules['__main__']
        mitogen.core.__file__ = 'x/mitogen/core.py'  # For inspect.getsource()
        mitogen.core.__loader__ = self.importer
        sys.modules['mitogen'] = mitogen
        sys.modules['mitogen.core'] = mitogen.core
        del sys.modules['__main__']

    def _setup_stdio(self):
        # type: () -> None
        self.stdout_log = IoLogger(self.broker, 'stdout', 1)
        self.stderr_log = IoLogger(self.broker, 'stderr', 2)
        # Reopen with line buffering.
        sys.stdout = os.fdopen(1, 'w', 1)

        fp = open('/dev/null')
        try:
            os.dup2(fp.fileno(), 0)
        finally:
            fp.close()

    def _dispatch_calls(self):
        # type: () -> None
        for msg, data in self.channel:
            LOG.debug('_dispatch_calls(%r)', data)
            if msg.src_id not in mitogen.parent_ids:
                LOG.warning('CALL_FUNCTION from non-parent %r', msg.src_id)

            modname, klass, func, args, kwargs = data
            try:
                obj = __import__(modname, {}, {}, [''])
                if klass:
                    obj = getattr(obj, klass)
                fn = getattr(obj, func)
                if getattr(fn, 'mitogen_takes_econtext', None):
                    kwargs.setdefault('econtext', self)
                if getattr(fn, 'mitogen_takes_router', None):
                    kwargs.setdefault('router', self.router)
                ret = fn(*args, **kwargs)
                self.router.route(
                    Message.pickled(ret, dst_id=msg.src_id, handle=msg.reply_to)
                )
            except Exception, e:
                LOG.debug('_dispatch_calls: %s', e)
                e = CallError(e)
                self.router.route(
                    Message.pickled(e, dst_id=msg.src_id, handle=msg.reply_to)
                )
        self.dispatch_stopped = True

    def main(self, parent_ids, context_id, debug, profiling, log_level,
             in_fd=100, out_fd=1, core_src_fd=101, setup_stdio=True):
        # type: (List[int], int, bool, bool, int, int, int, int, bool) -> None
        self._setup_master(profiling, parent_ids[0], context_id, in_fd, out_fd)
        try:
            try:
                self._setup_logging(debug, log_level)
                self._setup_importer(core_src_fd)
                self._setup_package(context_id, parent_ids)
                if setup_stdio:
                    self._setup_stdio()

                self.router.register(self.parent, self.stream)

                sys.executable = os.environ.pop('ARGV0', sys.executable)
                LOG.debug('Connected to %s; my ID is %r, PID is %r',
                          self.parent, context_id, os.getpid())
                LOG.debug('Recovered sys.executable: %r', sys.executable)

                _profile_hook('main', self._dispatch_calls)
                LOG.debug('ExternalContext.main() normal exit')
            except BaseException:
                LOG.exception('ExternalContext.main() crashed')
                raise
        finally:
            self.broker.shutdown()
            self.broker.join()
