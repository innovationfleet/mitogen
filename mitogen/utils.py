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

import logging
import sys

import mitogen
import mitogen.core
import mitogen.master

if 0:
    from typing import * # pylint: disable=import-error
    from types import FunctionType

LOG = logging.getLogger('mitogen')


def disable_site_packages():
    # type: () -> None
    for entry in sys.path[:]:
        if 'site-packages' in entry or 'Extras' in entry:
            sys.path.remove(entry)


def log_to_file(path=None, io=True, level='INFO'):
    # type: (Optional[str], bool, str) -> None
    log = logging.getLogger('')
    if path:
        fp = open(path, 'w', 1) # type: Union[BinaryIO, IO]
        mitogen.core.set_cloexec(fp.fileno())
    else:
        fp = sys.stderr

    level = getattr(logging, level, logging.INFO)
    log.setLevel(level)
    if io:
        logging.getLogger('mitogen.io').setLevel(level)

    fmt = '%(asctime)s %(levelname).1s %(name)s: %(message)s'
    datefmt = '%H:%M:%S'
    handler = logging.StreamHandler(fp)
    handler.formatter = logging.Formatter(fmt, datefmt)
    log.handlers.insert(0, handler)


def run_with_router(func, *args, **kwargs):
    # type: (Callable, object, object) -> Any
    broker = mitogen.master.Broker()
    router = mitogen.master.Router(broker)
    try:
        return func(router, *args, **kwargs)
    finally:
        broker.shutdown()
        broker.join()


def with_router(func):
    # type: (FunctionType) -> Callable
    def wrapper(*args, **kwargs):
        # type: (object, object) -> Any
        return run_with_router(func, *args, **kwargs)
    wrapper.func_name = func.func_name
    return wrapper
