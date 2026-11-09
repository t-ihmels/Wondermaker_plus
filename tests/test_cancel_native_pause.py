"""Rendered vendor cancel against the native pause/reset contract.

Klipper pause_resume.cmd_CLEAR_PAUSE clears is_paused and pause_command_sent;
virtual_sdcard.cmd_SDCARD_RESET_FILE resets only its file. This deliberately
models those separate native commands, not a fictitious BASE_CANCEL_PRINT call.
"""
import pytest
from test_config_macros import HeaterModel, ROOT


class NativePauseContract:
    def __init__(self, paused):
        self.is_paused = paused
        self.pause_command_sent = paused
        self.file_open = True
        self.events = []

    def dispatch(self, command, reset_fails=False):
        self.events.append(command)
        if command == 'SDCARD_RESET_FILE':
            if reset_fails:
                raise RuntimeError('SD reset refused')
            self.file_open = False
        elif command == 'CLEAR_PAUSE':
            self.is_paused = False
            self.pause_command_sent = False
        elif command == 'BASE_CANCEL_PRINT':
            raise AssertionError('cancel must not expand into the native cancel path')


def cancel_commands():
    m = HeaterModel(ROOT / 'config/live')
    m.p['configfile']['settings'].update({
        'printer': {'max_velocity': 600, 'max_accel': 10000},
        'stepper_z': {'position_endstop': 300},
    })
    m.p['save_variables']['variables']['current_extruder'] = 3
    m.p['toolhead']['homed_axes'] = 'xyz'
    script = m.templates['CANCEL_PRINT'].render(printer=m.p, params={})
    return [line.strip().split()[0] for line in script.splitlines() if line.strip()]


@pytest.mark.parametrize('paused', [True, False])
def test_cancel_resets_file_then_clears_native_pause_idempotently(paused):
    native = NativePauseContract(paused)
    commands = cancel_commands()
    for repeat in range(2):
        for command in commands:
            native.dispatch(command)
        assert not native.file_open
        assert not native.is_paused
        assert not native.pause_command_sent
    assert commands.index('SDCARD_RESET_FILE') < commands.index('CLEAR_PAUSE')
    assert 'TURN_OFF_HEATERS' in commands


def test_failed_file_reset_does_not_clear_native_pause():
    native = NativePauseContract(True)
    with pytest.raises(RuntimeError, match='SD reset refused'):
        for command in cancel_commands():
            native.dispatch(command, reset_fails=True)
    assert native.file_open and native.is_paused and native.pause_command_sent
    assert 'CLEAR_PAUSE' not in native.events
