#!/usr/bin/env python3

# from rich import pretty, print
from manager_service import *
# pretty.install()

# admin_forwad = {kuid: False for }
def dispatch_command(player_kuid: str, command_seq: list, process: DSTServerProcess, player_name: str, player_x: float | None = None, player_y: float | None = None, *, master_only: bool = False) -> bool:
    
    if master_only and not process.is_master: return False

    announce = lambda msg: process.send(f'c_announce("{msg}")')
    announce_not_operator = lambda: announce('you are not a operator to execute this command.')
    announce_unknown_command = lambda: announce('unknown command.')
    announce_wrong_args = lambda: announce('wrong argument(s).')

    if player_kuid not in OPERATORS_KUID: 
        if command_seq[0][0] == '#':
           announce_not_operator()
        return False

    announce = lambda msg: process.send(f'c_announce("{msg}")')
    announce_unknown_command = lambda: announce('unknown command.')
    announce_wrong_args = lambda: announce('wrong arguments.')

    match command_seq[0]:
        case '#save' | '#存档' | '#保存' | '#存':

            if len(command_seq) != 1:
                announce_wrong_args()
                return False
            # announce(f'received saving request from {player_name}.')
            announce(f'从 {player_name} 接收到存档请求.')
            process.send('c_save()')
        
        case '#rollback' | '#回档' | '#回':
        
            if len(command_seq) == 1:
                command_seq.append(1) # roll_day = 1
            elif len(command_seq) > 2:
                announce_wrong_args()

            try:
                roll_day = int(command_seq[1])
            except:
                announce_wrong_args()
                return False

            if roll_day == 1:
                announce(f'从 {player_name} 接收到回档请求, 到最新的存档点.')
                # announce(f'received rollback request from {player_name}, to the newest saving point')
            else:
                announce(f'从 {player_name} 接收到回档请求, 到第{roll_day}个存档点.')
                # announce(f'received rollback request from {player_name}, saving point: {roll_day}')
            process.send(f'c_rollback({roll_day})')
            
        case '#ban' | '#封禁' | '#禁':
            if len(command_seq) != 2:
                announce_wrong_args()
            
            announce(f'received ban request from {player_name}, target KUID: {command_seq[1]}') 
            if command_seq[1] in OPERATORS_KUID:
                announce(f'ban this player({command_seq[1]}) is not allowed')
                return False
            process.send(f'TheNet:Ban({command_seq[1]})')
            
        # case '#kick' | '#踢':
        #     pass
        # case '#forward':
        #     if len(command_seq) != 2:
        #         announce_wrong_args()
            

        case _:
            if command_seq[0][0] == '#':
                announce_unknown_command()
            return False

    return True

'''
    command terminal output example:
    [23:06:33]: [(KU_9bMvNhA2) Raiscies] ReceiveRemoteExecute(test) @(153.33, 108.14)

    or say in the chat panel
    [21:12:55]: [Say] (KU_9bMvNhA2) Raiscies: 1
    
''' 


command_terminal_input_regex = re.compile(
    r'^\[\d\d:\d\d:\d\d\]: \[\((KU_[^)]+)\) ([^]]+)\] ReceiveRemoteExecute\(([^)]+)\) @\(([^)]*), ([^)]*)\)$'
) 
def handle_custom_command_terminal_input(line: str, m: re.Match, line_count: int, process: DSTServerProcess):
    player_kuid, player_name, command_string, player_x, player_y = m.groups()
    try:
        player_x = float(player_x)
        player_y = float(player_y)
    except:
        player_x = None
        player_y = None

    command_seq = command_string.strip().split()
    dispatch_command(player_kuid, command_seq, process, player_name, player_x, player_y, master_only = True)

say_input_regex = re.compile(
    r'^\[\d\d:\d\d:\d\d\]: \[Say\] \((KU_[^)]+)\) ([^:]+): (.*)$'
)
def handle_say_input(line: str, m: re.Match, line_count: int, process: DSTServerProcess):
    player_kuid, player_name, say_string = m.groups()
    # print(f'Catched say input, kuid = {player_kuid}, name = {player_name}, str = {say_string}')
    command_seq = say_string.strip().split()
    dispatch_command(player_kuid, command_seq, process, player_name, master_only = True)


'''
    mod outdated example:
    [05:21:22]: [Mod Warning] 建家党狂喜(more items) is out of date and needs to be updated for new users to be able to join the server.
'''
mod_outdated_output_regex = re.compile(
    r'^\[\d\d:\d\d:\d\d\]: \[Mod Warning\] (.*?) is out of date and needs to be updated for new users to be able to join the server\.$'
)
def handle_mod_outdated(line: str, m: re.Match, line_count: int, process: DSTServerProcess):
    # process.set_shutdown_timer(PLANED_RESTART_MINUTES, True, True, 'Update Mod')
    process.set_shutdown_timer(PLANED_RESTART_MINUTES, True, True, '更新模组')


''' 
    remote command input example:
    [15:21:35]: RemoteCommandInput: "1"
    [15:21:35]: attempt to call a nil value

'''
remote_command_input_regex = re.compile(r'^\[\d\d:\d\d:\d\d\]: RemoteCommandInput: " *(.*?) *"$')

class RemoteCommandHandler:
    process: DSTServerProcess
    result_table: dict[str, list]

    def __init__(self, process: DSTServerProcess):
        self.process = process

    def handle_remote_command_input(self, line: str, m: re.Match, line_count: int, process: DSTServerProcess):
        pass


# if __name__ == '__main__':
mgr = DSTServerManager()
output_handler = OutputHandler(
    # (command_terminal_input_regex, handle_custom_command_terminal_input), 
    # (say_input_regex, handle_say_input),
    (mod_outdated_output_regex, handle_mod_outdated)
)

print(f'CONFIG: {CONFIG}')

class DefaultProcessListener(ProcessListener):
    def __init__(self, name: str):
        super().__init__(name,
                         on_start = self.__on_start, 
                         on_stop = self.__on_stop,
                         on_received_line = self.__on_received_line
                         )
        
    def __on_start(self, proc):
        print(f'on_starting: {proc.cluster_name}.{proc.shard_name}')
        print(f'archieving shard({proc.cluster_name}.{proc.shard_name})...')
        proc.make_backup()
    
    def __on_stop(self, proc):
        print(f'on_stopping: {proc.cluster_name}.{proc.shard_name}')
        
    def __on_received_line(self, proc, line):
        if line.strip() == '': return
        print(f'[{proc.cluster_name}.{proc.shard_name}] {line}')
        output_handler.check(line, proc)


DSTServerProcess.register_global(DefaultProcessListener('default_process_listener'))

def signal_terminate(signum: int, _) -> None:
    mgr.terminate()
    if signum == signal.SIGINT:
        raise KeyboardInterrupt()

signal.signal(signal.SIGTERM, signal_terminate)
signal.signal(signal.SIGINT , signal_terminate)
signal.signal(signal.SIGABRT, signal_terminate)

