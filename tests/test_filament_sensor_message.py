"""Production filament switch sensor: native ordering, genuine reference and failure containment."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('filament_switch_sensor', ROOT / 'klipper_extras/filament_switch_sensor.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def harness(is_printing=False, runout_gcode=None, insert_gcode=None, sensor_name="filament0"):
    events, calls = {}, []
    current_time = [10.0]
    
    objects = {
        'gcode': NS(
            register_mux_command=lambda *a, **kw: calls.append(('register', a[0])),
            respond_info=lambda s: calls.append(('message', s)),
            respond_error=lambda s: calls.append(('error', s)),
            _respond_error=lambda s: calls.append(('error', s)),
            error=lambda s: calls.append(('error', s)),
            run_script=lambda s: calls.append(('script', str(s))),
            run_command=lambda s: calls.append(('script', str(s)))
        ),
        'print_stats': NS(get_status=lambda now: {'state': 'printing' if is_printing else 'standby'}),
        'pause_resume': NS(is_paused=lambda: getattr(sensor.runout_helper, 'paused', False)),
        'gcode_macro': NS(load_template=lambda config, name, default='': type('Template', (), {
            'render': lambda *a, **kw: insert_gcode if name == 'insert_gcode' else runout_gcode,
            'get_command': lambda *a, **kw: insert_gcode if name == 'insert_gcode' else (runout_gcode or ''),
            '__bool__': lambda self: True,
            '__nonzero__': lambda self: True,
            '__str__': lambda self: str(insert_gcode if name == 'insert_gcode' else runout_gcode)
        })() if (insert_gcode if name == 'insert_gcode' else runout_gcode) else None),
        'buttons': NS(register_buttons=lambda pins, cb: calls.append(('register_buttons', pins, cb)))
    }

    printer = NS(
        lookup_object=lambda name, default=None: objects.get(name, default),
        register_event_handler=lambda name, cb: events.setdefault(name, []).append(cb),
        load_object=lambda config, name: objects.get(name, NS()),
        get_reactor=lambda: NS(
            monotonic=lambda: current_time[0],
            never=float('inf'),
            NEVER=float('inf'),
            pause=lambda t: calls.append(('pause', t)),
            register_callback=lambda cb, *a, **kw: calls.append(('callback', cb)),
            register_async_callback=lambda cb, *a, **kw: calls.append(('callback', cb)),
            register_timer=lambda cb, when, *a, **kw: calls.append(('callback', cb)),
            update_timer=lambda cb, when, *a, **kw: calls.append(('callback', cb))
        )
    )
    
    config = NS(
        get_name=lambda: f'filament_switch_sensor {sensor_name}',
        get_printer=lambda: printer,
        getboolean=lambda name, default=True: True if name in ('pause_on_runout', 'switch_pin') else default,
        getfloat=lambda name, default=0.5, above=0.0: default,
        get=lambda name, default=None: (
            'PC0' if name == 'switch_pin' else
            insert_gcode if name == 'insert_gcode' else
            runout_gcode if name == 'runout_gcode' else
            default
        )
    )
    
    sensor = module.SwitchSensor(config)
    sensor._current_time = current_time
    return sensor, calls, objects, events

@pytest.mark.parametrize("sensor_name", ["filament0", "filament1", "filament2", "filament3"])
def test_multi_toolhead_runout_triggers_error_when_printing(sensor_name):
    sensor, calls, objects, events = harness(is_printing=True, runout_gcode='PAUSE', sensor_name=sensor_name)
    events['klippy:ready'][0]()

    sensor.runout_helper.note_filament_present(True)
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.note_filament_present(False)

    error_calls = [c for c in calls if c[0] == 'error']
    assert len(error_calls) == 1
    assert "runout/broken" in error_calls[0][1]


@pytest.mark.parametrize("sensor_name", ["filament0", "filament1", "filament2", "filament3"])
def test_multi_toolhead_runout_ignores_error_when_standby(sensor_name):
    sensor, calls, objects, events = harness(is_printing=False, runout_gcode='PAUSE')
    events['klippy:ready'][0]()

    sensor.runout_helper.name = sensor_name
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.note_filament_present(False)

    script_calls = [c for c in calls if c[0] == 'script']
    assert len(script_calls) == 0


def test_filament_insert_triggers_callback():
    sensor, calls, objects, events = harness(is_printing=False, runout_gcode=None)
    events['klippy:ready'][0]()
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.insert_gcode = NS(render=lambda: 'RESUME')
    
    sensor.runout_helper.note_filament_present(True)
    
    assert any(c[0] == 'callback' for c in calls)

def test_filament_reload_triggers_resume_callback():
    sensor, calls, objects, events = harness(
        is_printing=True,
        runout_gcode='PAUSE',
        insert_gcode='RESUME_PRINT',
        sensor_name='filament0'
    )
    events['klippy:ready'][0]()

    calls.clear()

    # Prime state directly: filament absent and printer paused
    sensor._current_time[0] = 11.0
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.filament_present = False
    sensor.runout_helper.paused = True

    calls.clear()

    # Simulate reloading filament (False -> True) at t = 15.0
    sensor._current_time[0] = 15.0
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.note_filament_present(True)

    # Extract and execute the scheduled reactor callback
    callbacks = [c for c in calls if c[0] == 'callback']
    assert len(callbacks) == 1
    callbacks[0][1](sensor._current_time[0])

    # Verify script execution
    script_calls = [c for c in calls if c[0] == 'script']
    assert len(script_calls) == 1
    assert script_calls[0][1] == 'RESUME_PRINT'