# -*- coding: utf-8 -*-
#
# Copyright (c) 2014-2015 Université Catholique de Louvain.
#
# This file is part of INGInious.
#
# INGInious is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# INGInious is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with INGInious.  If not, see <http://www.gnu.org/licenses/>.
""" A JobManager that can interact with distant agents, via RPyC """

import threading

import copy
import rpyc
import tempfile
import tarfile

from backend.job_managers.abstract import AbstractJobManager
from common.base import directory_compare_from_hash, get_tasks_directory, directory_content_with_hash
import os

class RemoteManualAgentJobManager(AbstractJobManager):
    """ A Job Manager that handles connections with distant Agents using RPyC """
    def __init__(self, agents, image_aliases, hook_manager=None, is_testing=False):
        """
            Starts the job manager.

            Arguments:

            :param agents:
                A list of dictionaries containing information about distant backend agents:
                ::

                    {
                        'host': "the host of the agent",
                        'port': "the port on which the agent listens"
                    }
            :param image_aliases: a dict of image aliases, like {"default": "ingi/inginious-c-default"}.
            :param hook_manager: An instance of HookManager. If no instance is given(None), a new one will be created.
        """

        AbstractJobManager.__init__(self, image_aliases, hook_manager, is_testing)
        self._agents = [None for _ in range(0, len(agents))]
        self._agents_thread = [None for _ in range(0, len(agents))]
        self._agents_info = agents

        # init the synchronization of task directories
        self._last_content_in_task_directory = directory_content_with_hash(get_tasks_directory())
        threading.Timer((60 if not is_testing else 2), self._try_synchronize_task_dir).start()

        # connect to agents
        self._try_agent_connection()

        self._next_agent = 0
        self._running_on_agent = [[] for _ in range(0, len(agents))]

    def _try_agent_connection(self):
        """ Tries to connect to the agents that are not connected yet """
        if self._closed:
            return

        for entry, info in enumerate(self._agents_info):
            if self._agents[entry] is None:
                try:
                    conn = rpyc.connect(info['host'], info['port'], service=self._get_rpyc_server(entry),
                                        config={"allow_public_attrs": True, 'allow_pickle': True})
                except:
                    self._agents[entry] = None
                    self._agents_thread[entry] = None
                    print "Cannot connect to agent {}-{}".format(info['host'], info['port'])
                else:
                    self._agents[entry] = conn
                    self._agents_thread[entry] = rpyc.BgServingThread(conn)
                    self._synchronize_image_aliases(self._agents[entry])
                    self._synchronize_task_dir(self._agents[entry])

        if not self._is_testing:
            threading.Timer(10, self._try_agent_connection).start()

    def _synchronize_image_aliases(self, agent):
        """ Update the list of image aliases on the remote agent """
        update_image_aliases = rpyc.async(agent.root.update_image_aliases)
        update_image_aliases(self._image_aliases).wait()

    def _try_synchronize_task_dir(self):
        """ Check if the remote tasks dirs (on the remote agents) should be updated """
        if self._closed:
            return

        current_content_in_task_directory = directory_content_with_hash(get_tasks_directory())
        changed, deleted = directory_compare_from_hash(current_content_in_task_directory,self._last_content_in_task_directory)
        if len(changed) != 0 or len(deleted) != 0:
            self._last_content_in_task_directory = current_content_in_task_directory
            for agent in self._agents:
                if agent is not None:
                    self._synchronize_task_dir(agent)

        if not self._is_testing:
            threading.Timer(60, self._try_synchronize_task_dir).start()

    def _synchronize_task_dir(self, agent):
        """ Synchronizes the task directory with the remote agent. Steps are:
            - Get list of (path, file hash) for the main task directory (p1)
            - Ask agent for a list of all files in their task directory (p1)
            - Find differences for each agents (p2)
            - Create an archive with differences (p2)
            - Send it to each agent (p2)
            - Agents updates their directory
        """
        local_td = self._last_content_in_task_directory
        async_get_file_list = rpyc.async(agent.root.get_task_directory_hashes)
        async_get_file_list().add_callback(lambda r: self._synchronize_task_dir_p2(agent, local_td, r))

    def _synchronize_task_dir_p2(self, agent, local_td, async_value_remote_td):
        """ Synchronizes the task directory with the remote agent, part 2 """
        try:
            remote_td = copy.deepcopy(async_value_remote_td.value)
        except:
            print "An error occured while retrieving list of files in the task dir from remote agent"
            return

        if remote_td is None:  # sync disabled for this Agent
            return

        to_update, to_delete = directory_compare_from_hash(local_td, remote_td)
        tmpfile = tempfile.TemporaryFile()
        tar = tarfile.open(fileobj=tmpfile, mode='w:gz')
        for path in to_update:
            # be a little safe about what the agent returns...
            if os.path.relpath(os.path.join(get_tasks_directory(), path), get_tasks_directory()) == path and ".." not in path:
                tar.add(arcname=path, name=os.path.join(get_tasks_directory(), path))
            else:
                print "Agent returned non-safe file path: "+path
        tar.close()
        tmpfile.flush()
        tmpfile.seek(0)

        # sync the agent
        async_update = rpyc.async(agent.root.update_task_directory)
        # do not forget to close the file
        async_update(tmpfile, to_delete).add_callback(lambda r: tmpfile.close())

    def _select_agent(self):
        """ Select which agent should handle the next job.
            For now we use a round-robin, but will probably be improved over time.
        """
        available_agents = [i for i, j in enumerate(self._agents) if j is not None]
        if len(available_agents) == 0:
            return None
        chosen_agent = available_agents[self._next_agent % len(available_agents)]
        self._next_agent += 1
        return chosen_agent

    def _execute_job(self, jobid, task, inputdata, debug):
        """ Chooses an agent and executes a job on it """
        agent_id = self._select_agent()
        if agent_id is None:
            self._job_ended(jobid,
                            {'result': 'crash',
                             'text': 'There are not any agent available for grading. Please retry later. '
                                     'If this error persists, please contact the course administrator.'},
                            None)
            return
        try:
            agent = self._agents[agent_id]
            async_run = rpyc.async(agent.root.new_job)
            result = async_run(str(jobid), str(task.get_course_id()), str(task.get_id()), dict(inputdata), debug, None)
            self._running_on_agent[agent_id].append(jobid)
            result.add_callback(lambda r: self._execute_job_callback(jobid, r, agent_id))
        except:
            self._agent_shutdown(agent_id)
            self._execute_job(jobid, task, inputdata, debug)

    def _job_ended(self, jobid, result, agent_id=None):
        if agent_id is not None:
            self._running_on_agent[agent_id].remove(jobid)
        AbstractJobManager._job_ended(self, jobid, result)

    def _execute_job_callback(self, jobid, callback_return_val, agent_id):
        """ Called when an agent is done with a job or raised an exception """
        if callback_return_val.error:
            print "Agent {} made an exception while running jobid {}".format(agent_id, jobid)
            self._job_ended(jobid, {"result": "crash"}, agent_id)
        else:
            self._job_ended(jobid, copy.deepcopy(callback_return_val.value), agent_id)

    def _get_rpyc_server(self, agent_id):
        """ Return a service associated with this JobManager instance """
        on_agent_connection = self._on_agent_connection
        on_agent_disconnection = self._on_agent_disconnection

        class MasterBackendServer(rpyc.Service):
            def on_connect(self):
                on_agent_connection()

            def on_disconnect(self):
                on_agent_disconnection(agent_id)

        return MasterBackendServer

    def _on_agent_connection(self):
        """ Called when a RPyC service start: handles the connection of a distant Agent """
        print "Agent connected"

    def _on_agent_disconnection(self, agent_id):
        """ Called when a RPyC service ends: handles the disconnection of a distant Agent """
        print "Agent disconnected"
        self._agent_shutdown(agent_id)

    def _agent_shutdown(self, agent_id):
        """ Close a connection to an agent (failure/...) """

        # delete jobs that were running on this agent
        map(lambda jid: self._job_ended(jid, {'result': 'crash', 'text': 'Remote agent shutdown'}, agent_id), self._running_on_agent[agent_id])

        try:
            self._agents[agent_id] = None
            self._running_on_agent[agent_id] = []
            self._agents_thread[agent_id].close()
        except:
            pass

    def number_agents_available(self):
        """ Returns the number of connected agents """
        return len([entry for entry in self._agents if entry is not None])

    def close(self):
        """ Close the Job Manager """
        self._closed = True
        for i, entry in enumerate(self._agents):
            if entry is not None:
                # Hack a bit BgServingThread to ensure it closes properly
                thread = self._agents_thread[i]
                thread._active = False
                self._agents[i] = None
                self._agents_thread[i] = None
                entry.close()
                thread._thread.join()
                thread._conn = None