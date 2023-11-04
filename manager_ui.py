
from textual import work, on
from textual.app import App, ComposeResult
from textual.widgets import Static, Label, Footer, Input, Log, Tab, TextArea, TabbedContent, TabPane, Button, Switch
from textual.validation import Function



class ServerDisplayItem(Static):

    def __init__(self, process):
        Static.__init__(self)
        self.server_name_pid = f'{process[1]}({process[0]})'

    def compose(self) -> ComposeResult:
        yield Label(self.server_name_pid, id = 'server_name_pid')
        # yield Label('Pid', id = 'server_pid')
        yield Switch(False, id = 'server_switch')


class ServerManagerUI(App):
    BINDINGS = [
        ('c', 'clear_log', 'Clear Logs'),
        ('t', 'toggle_dark', 'Toggle dark mode'), 
        ('b', 'quit', 'Switch to background')
    ]

    CSS_PATH = 'manager_ui.tcss'

    def __init__(self, manager_service):
        App.__init__(self)

        self.manager_service = manager_service

    def compose(self) -> ComposeResult:
        with TabbedContent():
            with TabPane('DashBoard', id = 'dashboard'):
                yield Tab('Running Servers( Name(PID) )')
                for process in self.manager_service.process_list:
                    yield ServerDisplayItem(process)
                
            with TabPane('Terminal', id = 'terminal'):
                # yield Tab('Console', id = 'the_tab')
                yield Log(id = 'the_log', highlight = True)
                yield Input(
                    id = 'the_input', 
                    validators = [
                        Function(lambda s: len(s.strip(' \r\n\t\v')) != 0)
                    ],
                    validate_on = ["submitted"]
                )
                yield Footer()

    def write_log(self, log: str):
        self.query_one('#the_log', Log).write(log, scroll_end = self.query_one('#the_log', Log).is_vertical_scroll_end)

    @work(exclusive = True, thread = True)
    async def update_log(self) -> None:
        self.write_log('Beginning to receive the log.\n')
        while True:
            line = target.stdout.readline()
            if not line:
                self.write_log('no more log.\n')
                break           
            self.write_log(line.decode('utf8'))

    @on(Input.Submitted)
    def send_command(self, event: Input.Submitted) -> None:
        if event.validation_result.is_valid:
            event.input.value = ''
            # self.query_one('#the_input', Input).clear()
            self.write_log('[command] {}\n'.format(event.value))
            

    def action_toggle_dark(self) -> None:
        self.dark = not self.dark
    
    def action_clear_log(self) -> None:
        self.query_one('#the_log', Log).clear()
    
    async def action_switch_background(self) -> None:
        self.write_log('[command] server manager is suspended')
        # self.action_quit()
        await self.run_action('quit()')
        # os.kill(os.getpid(), signal.SIGTSTP)


    async def on_ready(self):
        self.query_one('#the_input', Input).focus()
        self.update_log()
        