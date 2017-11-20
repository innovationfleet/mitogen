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

if 0:
    from typing import * # pylint: disable=import-error

"""
On the Mitogen master, this is imported from ``mitogen/__init__.py`` as would
be expected. On the slave, it is built dynamically during startup.
"""

#: This is ``False`` in slave contexts. It is used in single-file Python
#: programs to avoid reexecuting the program's :py:func:`main` function in the
#: slave. For example:
#:
#:      .. code-block:: python
#:
#:          def do_work():
#:              os.system('hostname')
#:
#:          def main(broker):
#:              context = mitogen.master.connect(broker)
#:              context.call(do_work)  # Causes slave to import __main__.
#:
#:          if __name__ == '__main__' and mitogen.is_master:
#:              import mitogen.utils
#:              mitogen.utils.run_with_broker(main)
#:
is_master = True # type: bool


#: This is ``0`` in a master, otherwise it is a master-generated ID unique to
#: the slave context used for message routing.
context_id = 0 # type: int


#: This is ``None`` in a master, otherwise it is the master-generated ID unique
#: to the slave's parent context.
parent_id = None # type: Optional[int]


#: This is an empty list in a master, otherwise it is a list of parent context
#: IDs ordered from most direct to least direct.
parent_ids = []  # type: List[int]
