"""Production filament switch sensor: native ordering, genuine reference and failure containment."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('filament_switch_sensor', ROOT / 'klippy/extras/filament_switch_sensor.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def harness(is_printing=False, runout_gcode=None):
    events, calls = {}, []
    def status(d): return NS(get_status=lambda now: d)
    objects = {
        'gcode': NS(register_mux_command=lambda *a, **kw: calls.append(('register', a[0])),
                    respond_info=lambda s: calls.append(('message', s)),
                    _respond_error=lambda s: calls.append(('error', s)),
                    run_script=lambda s: calls.append(('script', s))),
        'print_stats': status({'state': 'printing' if is_printing else 'standby'}),
        'gcode_macro': NS(load_template=lambda config, name, default='': NS(render=lambda: runout_gcode) if runout_gcode else None)
    }
    printer = NS(lookup_object=lambda name: objects[name],
                 register_event_handler=lambda name, cb: events.setdefault(name, []).append(cb),
                 load_object=lambda config, name: objects.get(name, NS()),
                 get_reactor=lambda: NS(monotonic=lambda: 10.,
                                        never=float('inf'),
                                        pause=lambda t: calls.append(('pause', t)),
                                        register_callback=lambda cb: calls.append(('callback', cb))))
    config = NS(get_name=lambda: 'filament_switch_sensor filament0',
                get_printer=lambda: printer,
                getboolean=lambda name, default=True: default,
                getfloat=lambda name, default=0.5, above=0.0: default,
                get=lambda name, default=None: runout_gcode if name == 'runout_gcode' else default)
    
    sensor = module.SwitchSensor(config)
    return sensor, calls, objects, events


def test_filament_runout_triggers_error_and_pause_when_printing():
    sensor, calls, objects, events = harness(is_printing=True, runout_gcode='PAUSE')
    events['klippy:ready'][0]()
    
    # Trigger filament runout
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.note_filament_present(False)
    
    assert any(c[0] == 'error' for c in calls)
    assert any(c[0] == 'pause' for c in calls)


def test_filament_runout_ignores_error_when_standby():
    sensor, calls, objects, events = harness(is_printing=False, runout_gcode=None)
    events['klippy:ready'][0]()
    
    # Trigger filament runout while idle/unloading (standby state)
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.note_filament_present(False)
    
    assert not any(c[0] == 'error' for c in calls)
    assert not any(c[0] == 'pause' for c in calls)


def test_filament_insert_triggers_callback():
    sensor, calls, objects, events = harness(is_printing=False, runout_gcode=None)
    events['klippy:ready'][0]()
    sensor.runout_helper.min_event_systime = 0.
    sensor.runout_helper.insert_gcode = NS(render=lambda: 'RESUME')
    
    # Insert filament while not printing
    sensor.runout_helper.note_filament_present(True)
    
    assert any(c[0] == 'callback' for c in calls)