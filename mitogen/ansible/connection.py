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

"""
Basic Ansible connection plug-in mostly useful for testing functionality,
due to Ansible's use of the multiprocessing package a lot more work is required
to share the mitogen SSH connection across tasks.

Enable it by:

    $ cat ansible.cfg
    [defaults]
    connection_plugins = plugins/connection

    $ mkdir -p plugins/connection
    $ cat > plugins/connection/mitogen_conn.py <<-EOF
    from mitogen.ansible.connection import Connection
    EOF
"""

if 0:
    from typing import * # pylint: disable=import-error

import mitogen.master
import mitogen.ssh
import mitogen.utils
from mitogen.ansible import helpers

import ansible.errors
import ansible.plugins.connection


class Connection(ansible.plugins.connection.ConnectionBase):
    broker = None
    context = None

    become_methods = [] # type: List[str]
    transport = 'mitogen'

    @property
    def connected(self):
        # type: () -> bool
        return self.broker is not None

    def _connect(self):
        # type: () -> None
        if self.connected:
            return
        self.broker = mitogen.master.Broker()
        self.router = mitogen.master.Router(self.broker)
        if self._play_context.remote_addr == 'localhost':
            self.context = self.router.connect('local')
        else:
            self.context = self.router.connect('ssh',
                hostname=self._play_context.remote_addr,
            )

    def exec_command(self, cmd, in_data=None, sudoable=True):
        # type: (List[str], Optional[bytes], bool) -> Any
        super(Connection, self).exec_command(cmd, in_data=in_data, sudoable=sudoable)
        if in_data:
            raise ansible.errors.AnsibleError("does not support module pipelining")

        return self.context.call(helpers.exec_command, cmd, in_data)

    def fetch_file(self, in_path, out_path):
        # type: (str, str) -> None
        output = self.context.call(helpers.read_path, in_path)
        helpers.write_path(out_path, output)

    def put_file(self, in_path, out_path):
        # type: (str, str) -> None
        self.context.call(helpers.write_path, out_path,
                          helpers.read_path(in_path))

    def close(self):
        # type: () -> None
        self.broker.shutdown()
        self.broker.join()
