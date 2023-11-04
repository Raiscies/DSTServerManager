import asyncio
from enum import Enum, auto
import os
import signal

import asyncio.subprocess as sp
from asyncio.subprocess import Process, PIPE, STDOUT
from asyncio.queues import Queue

import threading

# from queue import Queue

from collections import deque
# import asyncio.subprocess
import argparse
from dataclasses import dataclass, field
from pathlib import Path


# about configuarations
CONFIG_PATH = './'
CONFIG_NAME = 'manager_service.config'

def parse_config():
    
    with open(Path(CONFIG_PATH) / Path(CONFIG_NAME), 'r', encoding = 'utf-8') as config_file:
        config_string = config_file.read()
    
    global CONFIG
    CONFIG = dict()
    for line in config_string.splitlines():
        line = line.strip(' \t\v')
        if len(line) == 0 or line[0] == '#': continue # skip the comment line
        key, value = line.split(maxsplit = 1)
        CONFIG[key] = value

    try:
        global TARGET_PATH, TARGET_NAME, TARGET_ARGS, SAVES_PATH, LOG_BUFFER_SIZE, CHECK_IS_CLUSTER, CHECK_IS_SHARD
        global PROCESS_SEARCH_CMD, CMD_PARSER_ARGUMENTS
        global command_parser
        
        # constants
        TARGET_PATH         = CONFIG['TARGET_PATH']
        TARGET_NAME         = CONFIG['TARGET_NAME']
        TARGET_ARGS         = CONFIG['TARGET_ARGS'].split()
        SAVES_PATH          = CONFIG['SAVES_PATH']
        LOG_BUFFER_SIZE     = int(CONFIG['LOG_BUFFER_SIZE'])
        CHECK_IS_CLUSTER    = CONFIG['CHECK_IS_CLUSTER'].split()
        CHECK_IS_SHARD      = CONFIG['CHECK_IS_SHARD'].split()
        PROCESS_SEARCH_CMD  = CONFIG['PROCESS_SEARCH_CMD'].format(uid = os.getuid(), target_name = TARGET_NAME)
        CMD_PARSER_ARGS     = CONFIG['CMD_PARSER_ARGS'].split()
    except Exception as e:
        print('error at parsing config file: ')
        print(str(e))
        os.abort()
       
    command_parser = argparse.ArgumentParser()
    for i in CMD_PARSER_ARGS:
        command_parser.add_argument(i)

parse_config()

'''
    saves directory format:
    DoNotStarveTogether/
        - MyDediServer/ (cluster directory)
            - Master/ (shard directory)
                - backup/ (logs)
                - save/   (core data)
                - leveldataoverride.lua
                - server.ini
                - modoverrides.lua
                - server_chat_log.txt
                - server_log.txt
            - Caves/ (shard directory)
                ...
            - adminlist.txt (optional)
            - blocklist.txt (optional)
            - cluster_token.txt
            - cluster.ini
        - MyDediServer2/ (cluster directory)
        ...
'''

# assume cluster's and shard's names are both unique 
@dataclass
class DSTShardItem:
    pass
    # handler = None       # shard process handler
    # pid: int  = -1       # shard pid
    # parent_pid: int = -1 # monitor parent pid
    
@dataclass
class DSTClusterItem:
    path: str = 'unknown'                    # cluster path
    name: str = 'unknown_cluster'            # cluster(saves) name, or folder name
    shards: dict[str, DSTShardItem] = field(default_factory = dict)  # the shards of this cluster, name : DSTShardItem

class ProcessStatus(Enum):
    ready       = auto()    # while the process is new created
    running     = auto()    # while the process is running normally
    dying       = auto()    # while the process is being sended terminate signal
    dead        = auto()    # while the process is dead(maybe it is doing the on_stop callback)
    not_exist   = auto()    # the quaryed process does not exists


class DSTServerProcess:

    cluster_name: str
    shard_name: str
    handler: Process
    parent_pid: int

    # event callbacks
    on_start: list # list[tuple[callback, run_in_thread]]
    on_stop: list
    on_received_line: list

    status: ProcessStatus

    log_buffer: deque[str]

    def __eq__(self, other) -> bool:
        return self.handler.pid == other.handler.pid if self.handler is not None else self.cluster_name == other.cluster_name and self.shard_name == other.shard_name
    
    def __init__(self, process_list: set, cluster: str, shard: str, log_buffer_size: int = LOG_BUFFER_SIZE, on_start: list = [], on_stop: list = [], on_received_line: list = []):
        self.process_list = process_list
        self.cluster_name = cluster
        self.shard_name = shard
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_received_line = on_received_line
        self.status = ProcessStatus.ready
        self.log_buffer = deque(maxlen = log_buffer_size)

        self.process_list.add(self)

    async def run(self):
        self.invoke_callbacks(self.on_start, self)

        self.handler = await sp.create_subprocess_shell(
            str(Path(TARGET_PATH) / Path(TARGET_NAME)) + ' ' +
                ' '.join(TARGET_ARGS).format(cluster_name = self.cluster_name, shard_name = self.shard_name, manager_pid = os.getpid()),
            stdin = PIPE, 
            stdout = PIPE, 
            stderr = STDOUT,
            close_fds = True
        )
        self.parent_pid = os.getpid()
        self.status = ProcessStatus.running
        self.do_log_loop()
        await self.handler.wait() # await for the ending of this process
        self.status = ProcessStatus.dead
        
        self.invoke_callbacks(self.on_stop, self)

        self.process_list.discard(self)

    def do_log_loop(self):
        async def loop():
            while self.status == ProcessStatus.running:
                line = (await self.handler.stdout.readline()).decode() # type: ignore
                self.invoke_callbacks(self.on_received_line, self, line)
                self.log_buffer.append(line)
                
        asyncio.create_task(loop())
        
    def invoke_callbacks(self, callback_list, *args):
        for callback, by_thread in callback_list:
            if by_thread:
                threading.Thread(target = callback, args = args).start()
            else:
                callback(*args)

# @dataclass
# class Listener:
#     listen: function
#     on_listened: function

#     def __init__(self, listen, on_listened):
#         pass
        

class DSTServerManager:
# this is the main class for Don't Starve Together Dedicated Server Manager.

    class Event:
        class Category(Enum):
            RUN_PROCESS = auto() 
            STOP_PROCESS = auto()
            STOP_EVENT_LOOP = auto()

        def __init__(self, category: Category, **kwargs) -> None:
            self.category = category
            self.args = kwargs

        def get_process(self) -> DSTServerProcess:
            Category = DSTServerManager.Event.Category
            if self.category in {Category.RUN_PROCESS, Category.STOP_PROCESS}:
                return self.args['process'] 
            else:
                raise RuntimeError()
                
    

    # server_info_list: list
    clusters: dict[str, DSTClusterItem]
    processes: set[DSTServerProcess]

    events: Queue[Event]

    def search_running_server(self, process_search_cmd = PROCESS_SEARCH_CMD):
        # generates processes
        pass
        # search_result = subprocess.Popen(process_search_cmd, stdout = subprocess.PIPE, shell = True).stdout.read().decode().splitlines() # type: ignore
        
        # for shard in search_result:
        #     pid, command = shard.split(' ', 1) # type: ignore
        #     args = command_parser.parse_args(command.split(' ')) # type: ignore
        #     if (cluster := self.clusters[args.cluster]) is not None: # wrong 
        #         # the cluster is exists 
        #         if cluster.shards[args.shard] is None: # wrong 
        #             # the shard is unknown
        #             cluster.shards[args.shard] = DSTShardItem(pid = int(pid), parent_pid = int(args.monitor_parent_process))
        #     else:
        #         # the cluster is unknown
        #         self.clusters[args.cluster] = DSTClusterItem(
        #             name   = args.cluster, 
        #             shards = {args.shard: DSTShardItem(pid = int(pid), parent_pid = int(args.monitor_parent_process))}
        #         )


    # check a cluster or a shard is valid
    def check_is_valid(self, path: Path, must_exists_list):
        if not path.is_dir(): return False

        for item in must_exists_list:
            if not (path / Path(item)).is_file():
                return False
        return True
    

    def search_saves(self, cluster_path = SAVES_PATH):
        # generates self.clusters
        self.clusters.clear()

        path = Path(cluster_path)
        if path.is_dir():
            cluster_list = [cluster for cluster in path.iterdir() if self.check_is_valid(cluster, CHECK_IS_CLUSTER)] # all of valid clusters in cluster_path
            for cluster in cluster_list:
                shard_list = [shard for shard in cluster.iterdir() if self.check_is_valid(shard, CHECK_IS_SHARD)] # all of valid shards
                self.clusters[cluster.name] = DSTClusterItem(
                    path = cluster.parent.name,
                    name = cluster.name,
                    shards = {shard.name: DSTShardItem() for shard in shard_list}
                )

    # manager main loop 
    def do_event_loop(self):
        tasks = set()
        async def loop():
            self.__event_loop = asyncio.get_running_loop()  # type: ignore
            Category = DSTServerManager.Event.Category
            while True:
                e = await self.events.get()

                match e.category:
                    case Category.RUN_PROCESS:
                        if e.get_process().status == ProcessStatus.ready:  
                            tasks.add(t := asyncio.create_task(e.get_process().run()))
                            t.add_done_callback(tasks.discard) 
                    case Category.STOP_PROCESS:
                        if e.get_process().status == ProcessStatus.running:
                            e.get_process().handler.terminate()
                            e.get_process().status = ProcessStatus.dying
                    case Category.STOP_EVENT_LOOP:
                        # terminate and wait for all process complete
                        [p.handler.terminate() for p in self.processes if p.status == ProcessStatus.running]
                        await asyncio.wait(tasks)
                        return

        asyncio.run(loop())
        
    def push_event(self, e: Event):
        asyncio.run_coroutine_threadsafe(self.events.put(e), self.__event_loop) # type: ignore

    def __init__(self, 
                #  game_target_path = TARGET_PATH, 
                #  game_target_name = TARGET_NAME, 
                #  config_path = DEFAULT_CONFIG_PATH, 
                #  config_name = DEFAULT_CONFIG_NAME
                 ):
        
        self.clusters = dict()
        self.processes = set()
        self.events = Queue()

        # self.target_path = game_target_path
        # self.target_name = game_target_name
        # self.config_path = config_path
        # self.config_name = config_name
        # self.worlds: list = self.parse_world_list(world_list)
        # self.CONFIG = self.parse_config(config_path)

        self.search_saves()
        # self.search_running_server()

        threading.Thread(target = self.do_event_loop, daemon = True).start()
    
    def get_process(self, cluster_name, shard_name, status_list = None):
        for process in self.processes:
            if process.cluster_name == cluster_name and process.shard_name == shard_name:
                if status_list is None or process.status in status_list:
                    return process
                else: break
                
        return None
    
    def __run_process_by_name(self, cluster_name, shard_name):
        Event = DSTServerManager.Event
        if self.get_process(cluster_name, shard_name, [ProcessStatus.running]) is not None:
            return False
        
        the_event = Event(
            Event.Category.RUN_PROCESS, 
            proecss = DSTServerProcess(self.processes, cluster_name, shard_name)
        )
        self.push_event(the_event)
        return True
    
    def __run_process(self, process: DSTServerProcess):
        Event = DSTServerManager.Event

        if self.get_process(process.cluster_name, process.shard_name, [ProcessStatus.running]) is not None or process.status != ProcessStatus.ready:
            return
        
        self.push_event(Event(
            Event.Category.RUN_PROCESS,  
            process = process
        ))


    def __stop_process_by_name(self, cluster_name, shard_name):
        Event = DSTServerManager.Event

        if (the_process := self.get_process(cluster_name, shard_name, [ProcessStatus.running])) is None:
            return
        
        self.push_event(
            Event(
                Event.Category.STOP_PROCESS, 
                process = the_process
            )
        )
    def __stop_process(self, process: DSTServerProcess):
        Event = DSTServerManager.Event

        if self.get_process(process.cluster_name, process.shard_name, [ProcessStatus.running]) is None or process.status != ProcessStatus.running:
            return
        
        self.push_event(Event(
            Event.Category.STOP_PROCESS,  
            process = process
        ))

    # APIs
    
    # run a shard
    def run_shard(self, cluster_name, shard_name):
        if cluster_name in self.clusters and shard_name in self.clusters[cluster_name].shards:
            return self.__run_process_by_name(cluster_name, shard_name)


    
    # run all of the shards of the appointted cluster
    def run_cluster(self, cluster_name):
        if cluster_name in self.clusters:
            if len(self.clusters[cluster_name].shards) == 0: return False # no shard could be run

            for shard_name, shard in self.clusters[cluster_name].shards:
                self.__run_process_by_name(cluster_name, shard_name)
     
    
    # shutdown a shard
    def stop_shard(self, cluster_name, shard_name):
        if cluster_name in self.clusters and shard_name in self.clusters[cluster_name].shards:
            return self.__stop_process_by_name(cluster_name, shard_name)


    def stop_cluster(self, cluster_name):
        if cluster_name not in self.clusters: return
        for shard_name, _ in self.clusters[cluster_name].shards:
            self.__stop_process_by_name(cluster_name, shard_name)

                
    def stop_all(self):
        [self.__stop_process(process) for process in self.processes]

