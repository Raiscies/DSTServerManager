#!/usr/bin/env python3


from enum import Enum, auto
import os
import signal
import re
import inspect
import time

from typing import Callable, Coroutine, Final, Any

import asyncio
from asyncio.subprocess import create_subprocess_exec, Process, PIPE, STDOUT
from asyncio.queues import Queue

import subprocess
import threading

from collections import deque
from collections.abc import Iterable
import argparse
from dataclasses import dataclass, field
from pathlib import Path

import shutil

import requests


# about configuarations
CONFIG_PATH = './'
CONFIG_NAME = 'manager_service.config'

class ConfigDict(dict):
    def __missing__(self, key):
        return '{' + key + '}'

def parse_config():
    
    with open(Path(CONFIG_PATH) / Path(CONFIG_NAME), 'r', encoding = 'utf-8') as config_file:
        config_string = config_file.read()
    
    global CONFIG
    CONFIG = ConfigDict()

    format_conf = lambda key, default = None: CONFIG[key].format_map(CONFIG) if key in CONFIG else default

    for line in config_string.splitlines():
        line = line.strip(' \t\v')
        if len(line) == 0 or line[0] == '#': continue # skip the comment line
        key, value = line.split(maxsplit = 1)
        CONFIG[key] = value

    try:
        global TARGET_INSTALL_PATH, TARGET_PATH, TARGET_NAME, TARGET_ARGS, STEAMCMD_PATH, SAVES_PATH, ARCHIVE_PATH, LOG_BUFFER_SIZE, CHECK_IS_CLUSTER, CHECK_IS_SHARD
        global PROCESS_SEARCH_CMD, CMD_PARSER_ARGS, RESTART_DELAY_SECONDS, PLANED_RESTART_MINUTES
        global LATEST_VERSION_QUERY_URL, LATEST_VERSION_QUERY_TIMEOUT, CURRENT_VERSION_QUERY_CMD, GAME_UPDATE_CMD, CHECK_FOR_GAME_UPDATE_INTERVAL
        global ADMIN_KUID, OPERATORS_KUID
        global command_parser
        global DEBUG

        # constants
        TARGET_NAME                 = format_conf('TARGET_NAME')
        TARGET_ARGS                 = format_conf('TARGET_ARGS')
        LATEST_VERSION_QUERY_URL    = format_conf('LATEST_VERSION_QUERY_URL')

        CONFIG['TARGET_INSTALL_PATH']   = TARGET_INSTALL_PATH   = Path(format_conf('TARGET_INSTALL_PATH')).expanduser()
        CONFIG['TARGET_PATH']           = TARGET_PATH           = Path(format_conf('TARGET_PATH')).expanduser()
        CONFIG['STEAMCMD_PATH']         = STEAMCMD_PATH         = Path(format_conf('STEAMCMD_PATH')).expanduser()
        CONFIG['SAVES_PATH']            = SAVES_PATH            = Path(format_conf('SAVES_PATH')).expanduser()
        
        CONFIG['ARCHIVE_PATH']          = ARCHIVE_PATH          = Path(format_conf('ARCHIVE_PATH')).expanduser()

        CONFIG['LOG_BUFFER_SIZE']                   = LOG_BUFFER_SIZE                   = int(format_conf('LOG_BUFFER_SIZE'))
        CONFIG['CHECK_IS_CLUSTER']                  = CHECK_IS_CLUSTER                  = format_conf('CHECK_IS_CLUSTER').split()
        CONFIG['CHECK_IS_SHARD']                    = CHECK_IS_SHARD                    = format_conf('CHECK_IS_SHARD').split()

        CONFIG['CMD_PARSER_ARGS']                   = CMD_PARSER_ARGS                   = format_conf('CMD_PARSER_ARGS').split()
       
        CONFIG['RESTART_DELAY_SECONDS']             = RESTART_DELAY_SECONDS             = int(format_conf('RESTART_DELAY_SECONDS', 0)) 
        CONFIG['PLANED_RESTART_MINUTES']            = PLANED_RESTART_MINUTES            = int(format_conf('PLANED_RESTART_MINUTES', 10))

        CONFIG['CURRENT_VERSION_QUERY_CMD']         = CURRENT_VERSION_QUERY_CMD         = format_conf('CURRENT_VERSION_QUERY_CMD').split()
        CONFIG['GAME_UPDATE_CMD']                   = GAME_UPDATE_CMD                   = format_conf('GAME_UPDATE_CMD').split()
        CONFIG['CHECK_FOR_GAME_UPDATE_INTERVAL']    = CHECK_FOR_GAME_UPDATE_INTERVAL    = int(format_conf('CHECK_FOR_GAME_UPDATE_INTERVAL'))
        
        CONFIG['LATEST_VERSION_QUERY_TIMEOUT']      = LATEST_VERSION_QUERY_TIMEOUT      = int(format_conf('LATEST_VERSION_QUERY_TIMEOUT'))

        CONFIG['OPERATORS_KUID']                    = OPERATORS_KUID                    = format_conf('OPERATORS_KUID').split()

        CONFIG['DEBUG']                             = DEBUG                             = format_conf('DEBUG', 'false').lower() == 'true' 
        # PROCESS_SEARCH_CMD  = format_conf('PROCESS_SEARCH_CMD') # .format(uid = os.getuid())

    except Exception as e:
        print('error at parsing config file: ')
        print(str(e))
        raise
       
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

# return result immediactly if func is a normal function, 
# or await for result if func() is a coroutine
async def anycall_await(func: Callable, *args, **kwargs):
    res = func(*args, **kwargs)
    if inspect.isawaitable(res):
        res = await res
    return res


class ManagerTask:
    refs: dict[str, asyncio.Task] = dict()

    def __init__(self, name: str, target: Callable):
        self.name = name
        self.target = target
    
    def __str__(self):
        return f'ManagerTask({self.name}->{self.target})'
    
    def __repr__(self):
        return str(self)

    def run(self, task_list: list):
        ManagerTask.refs[self.name] = (r := asyncio.create_task(
            self.__target(task_list)
        ))
        r.add_done_callback(lambda _: ManagerTask.refs.pop(self.name, None))

    def cancel(self):
        if self.name in ManagerTask.refs:
            ManagerTask.refs[self.name].cancel()
        

    async def __target(self, task_list: list):
        task_list.append(self)
        try:
            await anycall_await(self.target)
        except asyncio.CancelledError: pass
        finally:
            task_list.remove(self)

    @staticmethod
    def cancel_all():
        for _, t in ManagerTask.refs.items():
            t.cancel()
            

    @staticmethod
    async def wait_all():
        if len(ManagerTask.refs) != 0:
            await asyncio.wait(ManagerTask.refs.values())



class Timer(ManagerTask):

    # for run_times argument
    FOREVER: Final[int] = -1

    def __init__(self, interval_seconds: float, callback: Callable, run_times: int = 1, *, name: str | None = None, jump_first_timing: bool = False):
        super().__init__(name if name is not None else f'unnamedTimer_{id(self)}', self.timing) 

        self.interval = interval_seconds   # in seconds
        self.run_times = run_times         # a negative number means run forever
        self.callback = callback
        self.the_sleep_task = None
        self.__break_flag: bool | None = None
        self.jump_first_timing = jump_first_timing  # call the callback once immediactly when the Task starts (if run_times  == 0, Task will do nothing)
        self.latest_sleep_timestamp: float = 0

    async def timing(self):
    
        if self.run_times < 0:
            if self.jump_first_timing:
                try:
                    await anycall_await(self.callback)
                except asyncio.CancelledError: 
                    return
                if self.__break_flag: return

            while True:
                try:
                    await self.sleep(self.interval)
                except asyncio.CancelledError:
                    if self.__break_flag is None: return

                try:
                    await anycall_await(self.callback)
                except asyncio.CancelledError: 
                    return
                if self.__break_flag: return
        
        else:
            if self.jump_first_timing:
                if self.run_times == 0: return
                self.run_times -= 1
                try:
                    await anycall_await(self.callback)
                except asyncio.CancelledError: 
                    return
                if self.__break_flag: return

            for i in range(self.run_times):
                try:
                    await self.sleep(self.interval)
                except asyncio.CancelledError:
                    if self.__break_flag is None: return

                try:
                    await anycall_await(self.callback)
                except asyncio.CancelledError: 
                    return
                if self.__break_flag: return

    def call_continue(self):
        self.__break_flag = False
        if self.the_sleep_task is not None:
            self.the_sleep_task.cancel()
        

    def call_break(self):
        self.__break_flag = True
        if self.the_sleep_task is not None:
            self.the_sleep_task.cancel()
        
    def __str__(self):
        if self.run_times < 0:
            return f'Timer({self.name}, {self.interval}s * forever, {self.has_sleep_for_time()}s + {self.remains()}s)'
        else:
            return f'Timer({self.name}, {self.interval}s * {self.run_times} times, {self.has_sleep_for_time()}s + {self.remains()}s)'

    def __repr__(self):
        return str(self)
    
    def has_sleep_for_time(self):
        if self.latest_sleep_timestamp != 0:
            return time.time() - self.latest_sleep_timestamp
        else:
            return 0
    
    # returns the remained time of the sleep
    def remains(self):
        if self.latest_sleep_timestamp != 0:
            return self.interval - (time.time() - self.latest_sleep_timestamp)
        else:
            return 0
        
    async def sleep(self, delay: float, result = None):
        self.latest_sleep_timestamp = time.time()
        self.the_sleep_task = asyncio.create_task(asyncio.sleep(delay, result))
        
        try:
            await self.the_sleep_task
        except asyncio.CancelledError: raise
        finally:
            self.the_sleep_task = None
            self.latest_sleep_timestamp = 0

class ShutdownTimer(ManagerTask):
    

    def __init__(self, shutdown_in_minutes: int, process, do_restart: bool = False, do_announce: bool = True, reason: str | None = None, name: str | None = None):
        super().__init__(name if name is not None else f'unnamedShutdownTimer_{id(self)}',  self.count_backwards)

        self.restart_minutes: int = shutdown_in_minutes if shutdown_in_minutes >= 0 else 0
        self.target_process: DSTServerProcess = process
        self.do_restart: bool = do_restart
        self.do_announce: bool = do_announce
        self.reason: str | None = reason
        self.start_timestamp: float = 0

    def __str__(self):
        return f'ShutdownTimer({self.name}, {self.shutdown_or_restart(False)}, {self.restart_minutes}min(s) = {self.has_sleep_for_time()}s + {self.remains()}s)'
    def __repr__(self):
        return str(self)

    # in seconds
    def has_sleep_for_time(self):
        return time.time() - self.start_timestamp if self.start_timestamp != 0 else 0
    
    # in seconds
    def remains(self):
        return self.restart_minutes * 60 - (time.time() - self.start_timestamp) if self.start_timestamp != 0 else 0

    async def count_backwards(self):
        if self.restart_minutes <= 0:
            self.__announce_now()
            if self.do_restart:
                self.target_process.restart()
            else:
                self.target_process.stop()
            return

        self.start_timestamp = time.time()
        count_minute = self.restart_minutes
        if self.restart_minutes > 5:
            irregular_count_minute = self.restart_minutes % 5
            self.__announce_minutes(self.restart_minutes)
            await asyncio.sleep(irregular_count_minute * 60)
            count_minute -= irregular_count_minute
        
        # regular counting in minutes(count_minute > 1)
        while count_minute > 0: 
            self.__announce_minutes(count_minute)
            if count_minute <= 5:
                await asyncio.sleep((count_minute - 1) * 60) # reserve one minute
                break
            else:
                await asyncio.sleep(5 * 60)
                count_minute -= 5
        
        # count backward 1 minute
        self.__announce_minutes(1)
        await asyncio.sleep(30)
        
        self.__announce_seconds(30)
        await asyncio.sleep(20)

        self.__announce_seconds(10)
        await asyncio.sleep(10)

        self.__announce_now()
        if self.do_restart:
            self.target_process.restart()
        else:
            self.target_process.stop()
        
        self.start_timestamp = 0

    def shutdown_or_restart(self, chs: bool = True):
        if chs:
            return '重启' if self.do_restart else '关闭'
        else:
            return 'restart' if self.do_restart else 'shutdown'

    def __announce(self, msg: str):
        if self.do_announce:
            # self.target_process.send(f'c_announce("{msg}")' if self.reason is None else f'c_announce("{msg} reason: {self.reason}")')            
            self.target_process.send(f'c_announce("{msg}")' if self.reason is None else f'c_announce("{msg} 原因: {self.reason}")')

    def __announce_minutes(self, minute: int): 
        # self.__announce(f'server shard {self.target_process.shard_name} will {self.shutdown_or_restart()} in {minute} minute(s).')
        self.__announce(f'世界 {self.target_process.shard_name} 将会在{minute}分钟后{self.shutdown_or_restart()}.')

    def __announce_seconds(self, second: int):
        # self.__announce(f'server shard {self.target_process.shard_name} will {self.shutdown_or_restart()} in {second} second(s).')
        self.__announce(f'世界 {self.target_process.shard_name} 将会在{second}秒后{self.shutdown_or_restart()}.')

    def __announce_now(self):
        # self.__announce(f'server shard {self.target_process.shard_name} will {self.shutdown_or_restart()} now.')
        self.__announce(f'世界 {self.target_process.shard_name} 现在{self.shutdown_or_restart()}.')


class CheckForGameUpdateTimer(Timer):

    __current_version_search_regex = re.compile(r'BuildID (\d+)')

    def __init__(self, the_manager):
        super().__init__(CHECK_FOR_GAME_UPDATE_INTERVAL * 60, self.check_and_prepare_for_game_update, Timer.FOREVER, name = 'CheckForGameUpdateTimer', jump_first_timing = True)
        self.the_manager: DSTServerManager = the_manager
        self.update_available: bool | None = None
        self.processes_should_restart: list[DSTServerProcess] = []

    async def check_and_prepare_for_game_update(self):

        await asyncio.gather(
            self.__query_game_current_version(), 
            asyncio.to_thread(self.__query_game_latest_version)
        )        

        if self.current_version is None or self.latest_version is None: 
            self.update_available = None
        elif self.current_version < self.latest_version:
            self.update_available = True            
            print(f'[CheckForGameUpdateTimer] new game version is available, current_version = {self.current_version}, latest_version = {self.latest_version}.') if DEBUG else None
        else:
            self.update_available = False
            print(f'[CheckForGameUpdateTimer] game is the newest, version: {self.current_version}')

        if self.update_available:

            if len(self.the_manager.processes) != 0:
                print('[CheckForGameUpdateTimer] plan for updating the game.') if DEBUG else None
                DSTServerProcess.register_global(ProcessListener('__try_push_update_task' , on_stop = self.try_push_update_task))
                for shard in self.the_manager.processes:
                    if shard.status != ProcessStatus.running: continue

                    self.processes_should_restart.append(shard)
                    shard.set_shutdown_timer(
                        PLANED_RESTART_MINUTES,
                        False,  # don't do_restart
                        True,   # do_announce
                        # 'Game Core Update'
                        '更新游戏本体'
                    )
            else:
                print('[CheckForGameUpdateTimer] no existing process, update now')
                # update now
                self.the_manager.run_task(
                    ManagerTask('GameUpdateTask', self.update)
                )


    def try_push_update_task(self, process):
        # if len(self.the_manager.processes) > 1: return
        if len(self.the_manager.get_processes(None, (ProcessStatus.running, ProcessStatus.dying))) > 1:
            return

        # this is the last process that in on_stop

        # push update task
        self.the_manager.run_task(
            ManagerTask('GameUpdateTask', self.update)
        )

        # remove temporary callback
        DSTServerProcess.unregister_global('__try_push_update_task')

    async def update(self):
        # global GAME_UPDATE_CMD
        # GAME_UPDATE_CMD: list
        print('[CheckForGameUpdateTimer] update now') if DEBUG else None
        await (await create_subprocess_exec(
            str(STEAMCMD_PATH / Path(GAME_UPDATE_CMD[0])), 
            *GAME_UPDATE_CMD[1:]
        )).communicate()
        
        print('[CheckForGameUpdateTimer] reviving all existing processes now')
        # restart processes
        for proc in self.processes_should_restart:
            proc.revive()
        self.processes_should_restart.clear()

    async def __query_game_current_version(self):
        print('[CheckForGameUpdateTimer] querying current version...') if DEBUG else None
        out, _ = await (await create_subprocess_exec(
            str(STEAMCMD_PATH / CURRENT_VERSION_QUERY_CMD[0]), 
            *CURRENT_VERSION_QUERY_CMD[1:],
            stdout = PIPE,
        )).communicate()

        try:
            ver = self.__current_version_search_regex.search(out.decode()).groups()[0] # type: ignore
            self.current_version = int(ver)
            print(f'[CheckForGameUpdateTimer] current version: {self.current_version}') if DEBUG else None
        except:
            self.current_version = None
            print('[CheckForGameUpdateTimer] error: failed to query current version.') if DEBUG else None


    def __query_game_latest_version(self):
        print('[CheckForGameUpdateTimer] querying latest version...') if DEBUG else None
        
        try:
            req = requests.get(LATEST_VERSION_QUERY_URL, timeout = LATEST_VERSION_QUERY_TIMEOUT)
            self.latest_version = int(req.json()['data']['343050']['depots']['branches']['public']['buildid'])
            print(f'[CheckForGameUpdateTimer] latest version: {self.latest_version}') if DEBUG else None
        except:
            self.latest_version = None
            print('[CheckForGameUpdateTimer] error: failed to query latest version.') if DEBUG else None



# assume cluster's and shard's names are both unique 
@dataclass
class DSTShardItem:
    server_ini: dict = field(default_factory = dict)
    
@dataclass
class DSTClusterItem:
    path: str = 'unknown'                    # cluster path
    name: str = 'unknown_cluster'            # cluster(saves) name, or folder name
    shards: dict[str, DSTShardItem] = field(default_factory = dict)  # the shards of this cluster, name : DSTShardItem
    master: str | None = None
  

def default_callback(*args): pass
    
class Listener:

    
    def __init__(self, name: str, *, 
                 on_registered: Callable = default_callback, 
                 on_unregistered: Callable = default_callback):
        self.name = name # must be unique
        self.on_registered = on_registered
        self.on_unregistered = on_unregistered

class ProcessListener(Listener):


    def __init__(self, name, *, 
                 on_start: Callable = default_callback,
                 on_stop: Callable = default_callback, 
                 on_received_line: Callable = default_callback):
        super().__init__(name)
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_received_line = on_received_line

        

class CallbackCategory(Enum):
    START = auto()
    STOP  = auto()
    RECEIVED_LINE = auto()

class ProcessStatus(Enum):
    ready       = auto()    # while the process is new created
    running     = auto()    # while the process is running normally
    dying       = auto()    # while the process is being sended terminate signal
    dead        = auto()    # while the process is dead(maybe it is doing the on_stop callback)
    not_exist   = auto()    # currently not being used (the quaryed process does not exists)


class DSTServerProcess:
    
    # class members

    # global callback function lists
    # global_on_start: list = []
    # global_on_stop: list = []
    # global_on_received_line: list = []


    # listeners for every single process
    listeners: dict[str, ProcessListener]
    # global listeners that will apply to all of the processes
    global_listeners: dict[str, ProcessListener] = dict()
    
    # instance members

    cluster_name: str
    shard_name: str
    handler: Process
    parent_pid: int

    is_master: bool

    # event callbacks

    # on_start: list # list[callback]
    # on_stop: list
    # on_received_line: list

    @dataclass
    class restart_state_t:
        auto_restart: bool = True
        requested_restart: bool | None = None
        shutdown_timer: ShutdownTimer | None = None

    __restart_state: restart_state_t

    # @dataclass
    # class enable_global_callbacks_t:
    #     on_start = True 
    #     on_stop  = True
    #     on_received_line = True

    # enable_global_callbacks: enable_global_callbacks_t

    status: ProcessStatus

    log_buffer: deque[str]

    def __eq__(self, other) -> bool:
        return self.handler.pid == other.handler.pid if self.handler is not None else self.cluster_name == other.cluster_name and self.shard_name == other.shard_name

    def __str__(self):
        return f'DSTServerProcess({self.cluster_name}.{self.shard_name}, {self.status.name})'
    def __repr__(self):
        return str(self)

    def __init__(self, manager, shard, log_buffer_size: int = LOG_BUFFER_SIZE, on_start: list = [], on_stop: list = [], on_received_line: list = [], auto_restart = True):
        print(f'[DSTServerProcess] initializing: {shard.cluster_name}:{shard.name}') if DEBUG else None
        
        # self.process_list = process_list
        self.the_manager: DSTServerManager = manager
        self.cluster_name = shard.cluster_name
        self.shard_name = shard.name
        self.is_master = manager.clusters[self.cluster_name].master == shard.name
        # self.on_start = on_start
        # self.on_stop = on_stop
        # self.on_received_line = on_received_line
        self.restart_delay = RESTART_DELAY_SECONDS
        
        self.status = ProcessStatus.ready
        self.log_buffer = deque(maxlen = log_buffer_size)
        self.__restart_state = self.restart_state_t()

        self.listeners = dict()

        # self.enable_global_callbacks = self.enable_global_callbacks_t()


        self.the_manager.processes.append(self)

    @property
    def auto_restart(self):
        return self.__restart_state.auto_restart
        
    @auto_restart.setter
    def auto_restart(self, value: bool):
        self.__restart_state.auto_restart = value

    def __hash__(self):
        return hash(self.cluster_name + ' ' + self.shard_name) 

    async def run(self):
        print(f'[DSTServerProcess] ready to run: {self.cluster_name}:{self.shard_name}') if DEBUG else None
        self.__invoke_on_start()
        # self.invoke_callbacks(self.on_start, self)
        # if self.enable_global_callbacks.on_start: 
        #     self.invoke_callbacks(DSTServerProcess.global_on_start, self)

        print(f'[DSTServerProcess] finished on_start callback: {self.cluster_name}:{self.shard_name}') if DEBUG else None
        print(f'[DSTServerProcess] command: {str(TARGET_PATH / TARGET_NAME)} args: {TARGET_ARGS.format(cluster_name = self.cluster_name, shard_name = self.shard_name, manager_pid = os.getpid()).split()}') if DEBUG else None
        self.handler = await create_subprocess_exec(
            str(TARGET_PATH / TARGET_NAME), 
            *TARGET_ARGS.format(cluster_name = self.cluster_name, shard_name = self.shard_name, manager_pid = os.getpid()).split(),
            cwd = TARGET_PATH, 
            stdin = PIPE, 
            stdout = PIPE, 
            stderr = STDOUT,
            close_fds = True
        )
        print(f'[DSTServerProcess] created a subprocess: {self.cluster_name}:{self.shard_name}') if DEBUG else None
       
        self.parent_pid = os.getpid()
        self.status = ProcessStatus.running
        self.do_log_loop()
        print(f'[DSTServerProcess] wait for process stop: {self.cluster_name}:{self.shard_name}') if DEBUG else None
       
        await self.handler.wait() # await for the ending of this process
        self.status = ProcessStatus.dead

        self.__invoke_on_stop()

        # self.invoke_callbacks(self.on_stop, self)
        # if self.enable_global_callbacks.on_stop:
        #     self.invoke_callbacks(DSTServerProcess.global_on_stop, self)

        if self.should_restart():
            print('[DSTServerProcess] do restart now')
            self.__do_restart()
        else:
            print('[DSTServerProcess] removing process reference from manager processes list')
            # print('[DSTServerProcess] before: ' + str(self.the_manager.processes))
            self.the_manager.processes.remove(self)
            # print('[DSTServerProcess] after: ' + str(self.the_manager.processes))
    def do_log_loop(self):
        print(f'[DSTServerProcess] create log loop: {self.cluster_name}:{self.shard_name}') if DEBUG else None
       
        async def loop():
       
            while not self.handler.stdout.at_eof(): # type: ignore 
                try:
                    line = (await self.handler.stdout.readline()).decode().rstrip() # type: ignore
                    
                    self.__invoke_on_received_line(line)
                    # self.invoke_callbacks(self.on_received_line, self, line)
                    # if self.enable_global_callbacks.on_received_line:
                    #     self.invoke_callbacks(DSTServerProcess.global_on_received_line, self, line)

                    self.log_buffer.append(line)

                except Exception as e:
                    print(f'[DSTServerProcess] exception at log loop of {self.cluster_name}:{self.shard_name}:\n {str(e)}')

              
            print(f'[DSTServerProcess] ended log loop of {self.cluster_name}:{self.shard_name}') if DEBUG else None
       
        asyncio.create_task(loop())
        
    # def invoke_callbacks(self, callback_list, *args):
    #     for callback in callback_list:
    #         # if the callback returns False, then just stop to call the following callbacks 
    #         if (ret := callback(*args)) is not None and not ret: return 

    # def __copy_listeners(self, the_listeners):
    #     # this is prevent the listeners is deleted by somewhere while it's  iterating
    #     return (l for l in the_listeners.values())

    def __invoke_on_start(self):
        # create a shallow copy for iteration, in case some listeners remove themselves from the listeners dict in the callback 
        for listener in DSTServerProcess.global_listeners.copy().values():
            listener.on_start(self)
        for listener in self.listeners.copy().values():
            listener.on_start(self)

    def __invoke_on_stop(self):
        for listener in DSTServerProcess.global_listeners.copy().values():
            listener.on_stop(self)
        for listener in self.listeners.copy().values():
            listener.on_stop(self)

    def __invoke_on_received_line(self, line):
        # we don't make a copy in this listener function, cuz it will be call frequently
        # due to the performance 
        # for listener in DSTServerProcess.global_listeners.copy().values():
        for listener in DSTServerProcess.global_listeners.values():
            listener.on_received_line(self, line)
        # for listener in self.listeners.copy().values():
        for listener in self.listeners.values():
            listener.on_received_line(self, line)


    
    def should_restart(self):
        if self.__restart_state.requested_restart is not None:
            return self.__restart_state.requested_restart
        else:
            return self.__restart_state.auto_restart
        
    def __do_restart(self):
        self.__restart_state.requested_restart = None
        self.status = ProcessStatus.ready
        self.the_manager.run_task(
            Timer(
                self.restart_delay, 
                lambda: self.the_manager.run_process(self)
            )
        )

    def revive(self):
        if self.status != ProcessStatus.dead or self in self.the_manager.processes: return
        self.__restart_state = self.restart_state_t()
        self.the_manager.processes.append(self)
        self.status = ProcessStatus.ready
        self.the_manager.run_process(self)
       
    def stop(self):
        self.__restart_state.requested_restart = False
        self.reset_shutdown_timer()
        if self.status == ProcessStatus.running:
            self.handler.terminate()
            self.status = ProcessStatus.dying

    def restart(self):
        self.__restart_state.requested_restart = True
        self.reset_shutdown_timer()
        if self.status == ProcessStatus.running:
            self.handler.terminate() 
            self.status = ProcessStatus.dying

    def set_shutdown_timer(self, minutes: int, do_restart: bool = False, do_announce: bool = True, reason: str | None = None, cover_existing_timer: bool = False):
        if minutes < 0: minutes = 0
        if self.__restart_state.shutdown_timer is not None:
            if not cover_existing_timer: return
            self.the_manager.stop_task(self.__restart_state.shutdown_timer)
            self.__restart_state.shutdown_timer = None

        self.__restart_state.shutdown_timer = ShutdownTimer(
            minutes, self, do_restart, do_announce, reason, f'ShutdownTimer_{self.cluster_name}_{self.shard_name}'
        )
        self.the_manager.run_task(
            self.__restart_state.shutdown_timer
        )

    def reset_shutdown_timer(self):
        if self.__restart_state.shutdown_timer is not None:
            self.the_manager.stop_task(self.__restart_state.shutdown_timer)
            self.__restart_state.shutdown_timer = None
            

    def send(self, message: str):
        if self.status in (ProcessStatus.running, ProcessStatus.dying):
            self.the_manager.send_to_process(self, message)

    def make_backup(self):
        time_str = time.strftime('%m.%d.%H:%M', time.localtime())
        backup_name = f'{time_str}-{self.cluster_name}.{self.shard_name}'

        async def do_make_backup():
            await self.the_manager.zip_shard(
                Shard(self.cluster_name, self.shard_name), 
                backup_name
            )

        self.the_manager.run_task(
            ManagerTask(
                f'Task(Backup {self.cluster_name}.{self.shard_name})', 
                do_make_backup
            )
        )
    
    def register(self, listener: ProcessListener):
        self.listeners[listener.name] = listener

    def unregister(self, name: str) -> ProcessListener | None:
        # if index >= len(self.listeners): return None
        if name not in self.listeners: return None

        listener = self.listeners[name]
        del self.listeners[name]
        return listener
    
    @staticmethod
    def register_global(listener: ProcessListener):
        DSTServerProcess.global_listeners[listener.name] = listener
    
    @staticmethod
    def unregister_global(name: str) -> ProcessListener | None:
        if name not in DSTServerProcess.global_listeners: return None
        
        listener = DSTServerProcess.global_listeners[name]
        del DSTServerProcess.global_listeners[name]
        return listener
    
@dataclass
class Cluster:
    name: str

    @staticmethod
    def from_process(proc: DSTServerProcess):
        return Cluster(proc.cluster_name)

    def __str__(self):
        return f'Cluster({self.name})'
    
    def __repr__(self):
        return str(self)

# use this to search and operate clusters and shards
@dataclass
class Shard:
    cluster_name: str
    name: str | None = None # None means Master

    __cluster_shard_regex = re.compile(r'([\w -])+\.([\w -])+')

    @staticmethod
    def from_process(proc: DSTServerProcess):
        return Shard(proc.cluster_name, proc.shard_name)
    
    
    def __init__(self, cluster_or_cluster_dot_shard_name, shard_name: str | None = None):
        if shard_name is not None or '.' not in cluster_or_cluster_dot_shard_name:
            self.cluster_name = cluster_or_cluster_dot_shard_name
            self.name = shard_name
            return
        
        # eg: Cluster.Master
        #     Cluster        -> same as Cluster.Master if exists, else search for the unique shard of the cluster, else return None
        result = self.__cluster_shard_regex.match(cluster_or_cluster_dot_shard_name)
        if result is None: 
            self.cluster_name = cluster_or_cluster_dot_shard_name
        else:       
            self.cluster_name, self.name = result.groups()
            

    def __str__(self):
        return f'Shard({self.name if self.name is not None else "MASTER"} of cluster {self.cluster_name})'
    
    def __repr__(self):
        return str(self)

class Event:
    class Category(Enum):
        RUN_PROCESS     = auto() 
        STOP_PROCESS    = auto()
        RESTART_PROCESS = auto()
        RUN_TASK        = auto() 
        STOP_TASK       = auto()
        SEND_MESSAGE    = auto()
        STOP_EVENT_LOOP = auto()


    def __init__(self, category: Category, *args) -> None:
        self.category = category
        self.args = args

    def get_process(self) -> DSTServerProcess:
        Category = Event.Category
        if self.category in (Category.RUN_PROCESS, Category.STOP_PROCESS, Category.RESTART_PROCESS, Category.SEND_MESSAGE):
            return self.args[0] 
        else:
            raise RuntimeError()
        
    def get_message(self) -> str:
        Category = Event.Category
        if self.category in (Category.SEND_MESSAGE, ):
            return self.args[1]
        else:
            raise RuntimeError()
        
    def get_task(self) -> ManagerTask:
        Category = Event.Category
        if self.category in (Category.RUN_TASK, Category.STOP_TASK):
            return self.args[0]
        else:
            raise RuntimeError()
    
    def get(self):
        Category = Event.Category
        match Category:
            case Category.RUN_PROCESS | Category.STOP_PROCESS | Category.RESTART_PROCESS:
                return self.args[0]
            case Category.RUN_TASK | Category.STOP_TASK:
                return self.args[0]
            case Category.SEND_MESSAGE:
                return (self.args[0], self.args[1])
            case Category.STOP_EVENT_LOOP:
                return None
            case _:
                return None
            
    
  

@dataclass
class OutputItem:
    pattern: str | re.Pattern | Callable
    callback: Callable[[str, int | re.Match | Any, int, tuple], bool | None]
    multiline: bool
    multiline_callback: Callable[[str, int | re.Match | Any, int, tuple], bool | None] 

    def __init__(self, 
                 pattern: str | re.Pattern | Callable, 
                 callback:  Callable[[str, int | re.Match | Any, int, tuple], bool | None], 
                 multiline: bool = False, 
                 multiline_callback: Callable[[str, int | re.Match | Any, int, tuple], bool | None] | None = None):
        
        self.pattern = pattern
        self.callback = callback
        self.multiline = multiline
        self.multiline_callback = multiline_callback if multiline_callback is not None else callback

    @staticmethod
    def from_tuple(tup: tuple):
        return OutputItem(
            tup[0], 
            tup[1], 
            tup[2] if len(tup) >= 3 else False, 
            tup[3] if len(tup) >= 4 else tup[1]
        )
    
    def __str__(self):
        return f'OutputItem({self.pattern})'

    def __repr__(self):
        return str(self)

class OutputHandler:
    # pattern_map: dict[str | re.Pattern, function]
    # pattern_map: list[tuple[str | re.Pattern | Callable, Callable] | OutputItem]
    pattern_map: list[OutputItem]
    call_only_once: bool
    lastline_matched: OutputItem | None
    line_count: int

    def check(self, line: str, 
            #   call_only_once: bool | None = None, 
            *args):
        # if call_only_once is None: call_only_once = self.call_only_once
        is_matched = False

        if self.lastline_matched is not None and self.lastline_matched.multiline:
            self.line_count += 1
            if self.lastline_matched.multiline_callback(line, None, self.line_count, *args): return
            self.lastline_matched = None

        self.line_count = 1
        for item in self.pattern_map: 
            match item.pattern:
                case str():
                    if (index := line.find(item.pattern)) != -1:
                        item.callback(line, index, self.line_count, *args)
                        is_matched = True
                case re.Pattern():
                    if (m := item.pattern.match(line)) is not None:
                        item.callback(line, m, self.line_count, *args)
                        is_matched = True
                case Callable():
                    if (res := item.pattern(line)) is not None:
                        item.callback(line, res, self.line_count, *args)
                        is_matched = True
            if is_matched: 
                self.lastline_matched = item
                return True
            
        # doesn't match any pattern
        self.lastline_matched = None
        return False

    def __init__(self, *pattern_callback_seq
                #  , call_only_once = True
                 ):
        # self.call_only_once = call_only_once
        self.pattern_map = [OutputItem.from_tuple(p) for p in pattern_callback_seq]
        self.lastline_matched = None
        self.line_count = 1

    def add(self, pattern: str | re.Pattern | Callable, callback: Callable):
        self.pattern_map.append(OutputItem(pattern, callback))

    def remove(self, target_pattern: str | re.Pattern | Callable):
        for index, item in enumerate(self.pattern_map):
            if item.pattern == target_pattern:
                return self.pattern_map.pop(index)
        return None
    
class ConsoleHandler:
    @dataclass
    class LineMatcher:
        pattern_begin: Callable | re.Pattern
        pattern_end: Callable | re.Pattern
        callback: Callable
        max_line_count: int = 1

    process: DSTServerProcess
    patterns: list[LineMatcher]

    def __init__(self, process: DSTServerProcess):
        self.process = process




class DSTServerManager:
# this is the main class for Don't Starve Together Dedicated Server Manager.


    clusters: dict[str, DSTClusterItem]
    
    processes: list[DSTServerProcess]
    tasks: list[ManagerTask] # coroutine tasks

    events: Queue[Event]

    __is_running: bool

    # game_update_available: bool | None

    # def search_running_server(self, process_search_cmd = PROCESS_SEARCH_CMD):
        # generates processes
        # pass
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

        path = cluster_path
        if path.is_dir():
            cluster_list = [cluster for cluster in path.iterdir() if self.check_is_valid(cluster, CHECK_IS_CLUSTER)] # all of valid clusters in cluster_path
            for cluster in cluster_list:
                # shard_list = [shard for shard in cluster.iterdir() if self.check_is_valid(shard, CHECK_IS_SHARD)] 
                shard_dict = { # all of valid shards
                    shard.name: DSTShardItem(server_ini = self.parse_server_ini(Shard(cluster.name, shard.name))) for shard in cluster.iterdir() if self.check_is_valid(shard, CHECK_IS_SHARD)
                }
                master = None
                # search for the master of a cluster
                for name, shard in shard_dict.items():
                    if 'is_master' in shard.server_ini and shard.server_ini['is_master'] == 'true':
                        master = name
                        break

                self.clusters[cluster.name] = DSTClusterItem(
                    path = cluster.parent.name,
                    name = cluster.name,
                    shards = shard_dict, 
                    master = master
                )


    def parse_server_ini(self, shard: Shard) -> dict:
        
        if shard.name is None: 
            shard.name = self.__get_master(shard.cluster_name)
            if shard.name is None: return dict()

        path = Path(SAVES_PATH) / Path(shard.cluster_name) / Path(shard.name)
        if not path.parent.is_dir(): 
            print(f'error: the cluster {shard.cluster_name} is not found.') if DEBUG else None
            return dict()
        if not path.is_dir():
            print(f'error: the shard {shard.name} in the cluster {shard.cluster_name} is not found.') if DEBUG else None
            return dict()


        if path.is_dir() and (path / Path('server.ini')).is_file():
            server_ini = dict()
            with open(path / Path('server.ini'), 'r', encoding = 'utf-8') as f:
                for l in f:
                    if (line := l.strip()) == '': continue
                    if line[0] == '[': continue
                    key, value = line.split('=', 1)
                    server_ini[key.strip()] = value.strip()
            
            return server_ini
        else:
            print(f'error: server.ini is not found in the {shard.cluster_name}.{shard.name}.') if DEBUG else None
            return dict()

    # manager main loop 
    def do_event_loop(self):
        print('[event_loop] starting') if DEBUG else None
        process_refs: set[asyncio.Task] = set()
        # task_refs: set[asyncio.Task] = set()
        
        async def loop():
            self.__event_loop = asyncio.get_running_loop()  # type: ignore
            Category = Event.Category
            while True:
                print('[event_loop] waiting for a event') if DEBUG else None
                e = await self.events.get()
                print(f'[event_loop] got a event with category: {e.category}') if DEBUG else None
                match e.category:
                    case Category.RUN_PROCESS:

                        print(f'[event_loop] creating process: {e.get_process().cluster_name}:{e.get_process().shard_name} with status: {e.get_process().status}') if DEBUG else None
                        if e.get_process().status == ProcessStatus.ready:
                            process_refs.add(r := asyncio.create_task(e.get_process().run()))
                            r.add_done_callback(process_refs.discard) 
                            print(f'[event_loop] create process successfully: {e.get_process().cluster_name}:{e.get_process().shard_name} with status: {e.get_process().status}') if DEBUG else None
                            
                    case Category.STOP_PROCESS:
                        
                        e.get_process().stop()

                    case Category.RESTART_PROCESS:

                        e.get_process().restart()

                    case Category.RUN_TASK:
                        
                        e.get_task().run(self.tasks)

                    case Category.STOP_TASK:
                        
                        e.get_task().cancel()
  

                    case Category.SEND_MESSAGE:
                        
                        process = e.get_process()
                        if process.status in (ProcessStatus.running, ProcessStatus.dying): 
                            message = e.get_message().strip() + '\n'
                            process.handler.stdin.write(message.encode()) # type: ignore

                    case Category.STOP_EVENT_LOOP:
                        
                        ManagerTask.cancel_all()
                        # terminate and wait for all process complete
                        for p in self.processes: 
                            if p.status == ProcessStatus.running:
                                p.stop() 
                        
                        await ManagerTask.wait_all()
                        if len(process_refs) != 0:
                            await asyncio.wait(process_refs)

                        self.__is_running = False
                        return

        asyncio.run(loop())

    async def zip_save(self, cluster: Cluster, name: str, dist: Path | None = None) -> bool:
        if not self.cluster_exists(cluster): return False

        if dist is None:
            dist = ARCHIVE_PATH / cluster.name

        if dist.exists():
            if not dist.is_dir():
                # dist is not a directory
                return False
        else:
            os.makedirs(dist)

        async def do_zip_save():
            await asyncio.to_thread(
                lambda: shutil.make_archive(str(dist / name), 'zip', SAVES_PATH, cluster.name)
            )

        self.run_task(
            ManagerTask(
                f'Task(Zip Cluster {cluster}, to {name}.zip)', 
                do_zip_save
            )
        )
        
        return True

    async def zip_shard(self, shard: Shard, name: str, dist: Path | None = None) -> bool:
        if not self.shard_exists(shard): return False

        if (s := self.expand_shard_master(shard)) is not None:
            shard_name: str = s.name # type: ignore
        else:
            # unknown master shard 
            return False


        if dist is None:
            dist = ARCHIVE_PATH / shard.cluster_name / shard_name

        if dist.exists():
            if not dist.is_dir():
                # dist is not a directory
                return False
        else:
            os.makedirs(dist)

        async def do_zip_shard():
            await asyncio.to_thread(
                lambda: shutil.make_archive(str(dist / name), 'zip', SAVES_PATH / shard.cluster_name, shard_name)
            )
        
        self.run_task(
            ManagerTask(
                f'Task(Zip Shard {shard}, to {name}.zip)', 
                do_zip_shard
            )
        )

    
        return True
        
    def push_event(self, e: Event):
        asyncio.run_coroutine_threadsafe(self.events.put(e), self.__event_loop) # type: ignore


    def get_task(self, task_name: str):
        return next((t for t in self.tasks if t.name == task_name), None)

    def run_task(self, target: ManagerTask | str) -> bool:
        task = self.get_task(target) if isinstance(target, str) else target

        if task is None: return False

        self.push_event(
            Event(Event.Category.RUN_TASK, task)
        )
        return True

    def stop_task(self, target: ManagerTask | str) -> bool:
        task = self.get_task(target) if isinstance(target, str) else target

        if task is None: return False

        self.push_event(
            Event(Event.Category.STOP_TASK, task)
        )
        return True
        
    # use Timer to plan a task
    def run_timer(self, target: Callable, interval_seconds: int, run_times: int = 1, name: str | None = None):
        return self.run_task(Timer(interval_seconds, target, run_times, name = name))


    def __init__(self):
        
        self.clusters = dict()
        self.processes = []
        self.events = Queue()
        self.tasks = []
        self.game_update_available = None

        self.search_saves()
        # self.search_running_server()

        self.events.put_nowait(
            Event(
                Event.Category.RUN_TASK,
                CheckForGameUpdateTimer(self)
            )
        )

        self.event_loop_thread = threading.Thread(target = self.do_event_loop, daemon = True)
        self.event_loop_thread.start()
        self.__is_running = True

    # no check
    def __get_master(self, cluster: Cluster | str) -> str | None:
        return self.clusters[cluster if type(cluster) == str else cluster.name].master # type: ignore

    def shard_exists(self, shard: Shard) -> bool:
        if shard.cluster_name in self.clusters:
            if shard.name is not None: 
                return shard.name in self.clusters[shard.cluster_name].shards
            else:
                return self.__get_master(shard.cluster_name) is not None
        else: return False

    def cluster_exists(self, target: Cluster | Shard) -> bool:
        return target.name in self.clusters if type(target) == Cluster else target.cluster_name in self.clusters # type: ignore

    # expand a shard's name to its real master name
    def expand_shard_master(self, shard: Shard) -> Shard | None:
        if shard.name is not None: return shard
        if not self.shard_exists(shard): return None
        if self.clusters[shard.cluster_name].master is None: return None
        return Shard(shard.cluster_name, self.clusters[shard.cluster_name].master)
    
    # # assume the cluster/shard names does not contains '.' and other strange characters 
    # def to_process(self, s: str) -> DSTServerProcess | None:
 
    #     # eg: Cluster.Master
    #     #     Cluster        -> same as Cluster.Master if exists, else search for the unique shard of the cluster, else return None
    #     result = self.__cluster_shard_regex.match(s)
    #     if result is None: return None

    #     if len(result.groups()) == 2:
    #         # cluster: str
    #         cluster, shard = result.groups() 
    #         if not self.shard_exists(Shard(cluster, shard)): return None
    #     else:
    #         cluster = result.group()
    #         if not self.cluster_exists(Cluster(cluster)): return None
    #         shard = None

    #     if shard is not None:
    #         return self.get_process(Shard(cluster, shard))

    #     # shard is None:
    #     if (master_proc := self.get_process(Shard(cluster))) is not None: return master_proc

    #     # master_proc is None:
    #     # return the unique process of the cluster
    #     proc_list = self.get_processes(Cluster(cluster))
    #     return proc_list[0] if len(proc_list) == 1 else None
    
    def get_process(self, shard: Shard, with_status: Iterable | None = None) -> DSTServerProcess | None:
        if shard.name is None:
            if self.cluster_exists(shard) and self.clusters[shard.cluster_name].master is not None:
                shard.name = self.clusters[shard.cluster_name].master
            else: return None

        for process in self.processes:
            if process.cluster_name == shard.cluster_name and process.shard_name == shard.name:
                if with_status is None or process.status in with_status:
                    return process
                else: break
                
        return None
    
    def get_processes(self, cluster: Cluster | None = None, with_status: Iterable[ProcessStatus] | None = None) -> list:
        if cluster is None: 
            if with_status is None:
                return self.processes
            else:
                return [process for process in self.processes if process.status in with_status]


        result = []
        for process in self.processes:
            if process.cluster_name == cluster.name:
                if with_status is None or process.status in with_status:
                    result.append(process)
        return result
    
    
    def run_process(self, process: DSTServerProcess):
        if self.get_process(Shard.from_process(process), [ProcessStatus.running]) is not None or process.status != ProcessStatus.ready:
            return False
        
        self.push_event(Event(
            Event.Category.RUN_PROCESS,  
            process
        ))
        return True

    def stop_process(self, process: DSTServerProcess, restart: bool = False) -> bool:
        if process.status != ProcessStatus.running: return False
    
        self.push_event(Event(
            Event.Category.STOP_PROCESS if not restart else Event.Category.RESTART_PROCESS,  
            process
        ))
        return True

    def send_to_process(self, process: DSTServerProcess, message: str) -> bool:
        if process.status not in (ProcessStatus.running, ProcessStatus.dying): return False
        self.push_event(
            Event(
                Event.Category.SEND_MESSAGE, 
                process, 
                message
            )
        )
        return True

    def run(self, target: Shard | Cluster, *args) -> bool | tuple:
        match target:
            case Shard():
                if self.shard_exists(target):
                    shard_name: str = self.__get_master(target.cluster_name) if target.name is None else target.name # type:ignore
                    return self.run_process(
                        DSTServerProcess(self, Shard(target.cluster_name, shard_name), *args) 
                    )
                return False
            case Cluster():
                if self.cluster_exists(target):
                    return tuple(
                        self.run_process(DSTServerProcess(self, Shard(target.name, shard_name), *args)) for 
                        shard_name, _ in self.clusters[target.name].shards.items()
                    )
        return False


    def stop(self, target: Shard | Cluster, do_restart: bool = False) -> bool | tuple:
        match target:
            case Shard():
                if self.shard_exists(target):
                    return self.stop_process(
                        self.get_process(target), # type: ignore
                        do_restart
                    )
                return False
            case Cluster():
                if self.cluster_exists(target):
                    return tuple(self.stop_process(proc, do_restart) for proc in self.get_processes(target))
        return False

    def restart(self, target: Shard | Cluster) -> bool | tuple:
        return self.stop(target, True)

    def send(self, target: Shard | Cluster, message: str) -> bool | tuple:
        match target:
            case Shard():
                if self.shard_exists(target):
                    return self.send_to_process(
                        self.get_process(target), # type: ignore
                        message
                    )
                return False
            case Cluster():
                if self.cluster_exists(target):
                    return tuple(self.send_to_process(proc, message) for proc in self.get_processes(target))
        return False

    def stop_all(self, do_restart = False) -> tuple:
        return tuple(self.stop_process(process, do_restart) for process in self.processes)

    def restart_all(self) -> tuple:
        return tuple(self.stop_process(process, True) for process in self.processes)


    def terminate(self):
        if self.__is_running:
            print('[DSTServerManager] terminating manager service...') if DEBUG else None
            self.push_event(
                Event(
                    Event.Category.STOP_EVENT_LOOP
                )
            )
            self.event_loop_thread.join()
        print('[DSTServerManager] completed terminate manager service.') if DEBUG else None


# class ConsoleUtils:
#     def __init__(self, proc: DSTServerProcess):
#         self.proc = proc

#     def announce(self, msg: str, interval: int | None = None):
#         if interval is not None:
#             self.proc.send(f'c_announce({msg}, {interval})')
#         else:
#             self.proc.send(f'c_announce({msg})')
        

#     def shutdown(self, do_save: bool = True):
#         self.proc.send(f'c_shutdown({"true" if do_save else "false"})')
    