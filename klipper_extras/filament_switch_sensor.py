# Generic Filament Sensor Module
#
# Copyright (C) 2019  Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging

class RunoutHelper:
    def __init__(self, config):
        self.name = config.get_name().split()[-1]
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        # Read config
        self.runout_pause = config.getboolean('pause_on_runout', True)
        if self.runout_pause:
            self.printer.load_object(config, 'pause_resume')
        self.runout_gcode = self.insert_gcode = None
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        if self.runout_pause or config.get('runout_gcode', None) is not None:
            self.runout_gcode = gcode_macro.load_template(
                config, 'runout_gcode', '')
        if config.get('insert_gcode', None) is not None:
            self.insert_gcode = gcode_macro.load_template(
                config, 'insert_gcode')
        self.pause_delay = config.getfloat('pause_delay', .5, above=.0)
        self.event_delay = config.getfloat('event_delay', 3., above=0.)
        # Internal state
        self.min_event_systime = self.reactor.NEVER
        self.filament_present = False
        self.sensor_enabled = True
        # Register commands and event handlers
        self.printer.register_event_handler("klippy:ready", self._handle_ready)
        self.gcode.register_mux_command(
            "QUERY_FILAMENT_SENSOR", "SENSOR", self.name,
            self.cmd_QUERY_FILAMENT_SENSOR,
            desc=self.cmd_QUERY_FILAMENT_SENSOR_help)
        self.gcode.register_mux_command(
            "SET_FILAMENT_SENSOR", "SENSOR", self.name,
            self.cmd_SET_FILAMENT_SENSOR,
            desc=self.cmd_SET_FILAMENT_SENSOR_help)
    def _handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.
    def _respond_runout_error(self):
        if self.name == "filament0":
            self.gcode._respond_error("ZRU-M002: Toolhead 1 runout/broken. Click OK, reload filament until blue light is on, push until resistance is felt, then resume.")
        elif self.name == "filament1":
            self.gcode._respond_error("ZRU-M005: Toolhead 2 runout/broken. Click OK, reload filament until blue light is on, push until resistance is felt, then resume.")
        elif self.name == "filament2":
            self.gcode._respond_error("ZRU-M008: Toolhead 3 runout/broken. Click OK, reload filament until blue light is on, push until resistance is felt, then resume.")
        elif self.name == "filament3":
            self.gcode._respond_error("ZRU-M011: Toolhead 4 runout/broken. Click OK, reload filament until blue light is on, push until resistance is felt, then resume.")
        elif self.name == "path_0":
            self.gcode._respond_error("ZRU-M003: Toolhead 1 filament tangled. Please untangle the spool and click resume print.")
        elif self.name == "path_1":
            self.gcode._respond_error("ZRU-M006: Toolhead 2 filament tangled. Please untangle the spool and click resume print.")
        elif self.name == "path_2":
            self.gcode._respond_error("ZRU-M009: Toolhead 3 filament tangled. Please untangle the spool and click resume print.")
        elif self.name == "path_3":
            self.gcode._respond_error("ZRU-M012: Toolhead 4 filament tangled. Please untangle the spool and click resume print.")
    def _runout_event_handler(self, eventtime):
        # Pausing from inside an event requires that the pause portion
        # of pause_resume execute immediately.
        pause_prefix = ""
        if self.runout_pause:
            pause_resume = self.printer.lookup_object('pause_resume')
            pause_resume.send_pause_command()
            pause_prefix = "PAUSE\n"
            self.printer.get_reactor().pause(eventtime + self.pause_delay)
        self._exec_gcode(pause_prefix, self.runout_gcode)
    def _insert_event_handler(self, eventtime):
        self._exec_gcode("", self.insert_gcode)
    def _exec_gcode(self, prefix, template):
        try:
            self.gcode.run_script(prefix + template.render() + "\nM400")
        except Exception:
            logging.exception("Script running error")
        self.min_event_systime = self.reactor.monotonic() + self.event_delay
    def note_filament_present(self, is_filament_present):
        if is_filament_present == self.filament_present:
            return
        self.filament_present = is_filament_present
        eventtime = self.reactor.monotonic()
        print_stats = self.printer.lookup_object("print_stats")
        is_printing = print_stats.get_status(eventtime)["state"] == "printing"

        # 进料检测
        if is_filament_present and eventtime > self.min_event_systime:
            if not is_printing and self.insert_gcode is not None:
                # insert detected
                self.min_event_systime = self.reactor.NEVER
                logging.info(
                    "Filament Sensor %s: insert event detected, Time %.2f" %
                    (self.name, eventtime))
                self.reactor.register_callback(self._insert_event_handler)

        if eventtime < self.min_event_systime or not self.sensor_enabled:
            # do not process during the initialization time, duplicates,
            # during the event delay time, while an event is running, or
            # when the sensor is disabled
            return
# >>> tihmels begin: Only print runout error if the toolhead is currently printing
        if not is_filament_present and is_printing:
            self._respond_runout_error()
            if self.runout_gcode is not None:
                self.min_event_systime = self.reactor.NEVER
                logging.info(
                    "Filament Sensor %s: runout event detected, Time %.2f" %
                    (self.name, eventtime))
                self.reactor.register_callback(self._runout_event_handler)
# <<< tihmels end: Only print runout error if the toolhead is currently printing
    def get_status(self, eventtime):
        return {
            "filament_detected": bool(self.filament_present),
            "enabled": bool(self.sensor_enabled)}
    cmd_QUERY_FILAMENT_SENSOR_help = "Query the status of the Filament Sensor"
    def cmd_QUERY_FILAMENT_SENSOR(self, gcmd):
        if self.filament_present:
            msg = "Filament Sensor %s: filament detected" % (self.name)
        else:
            msg = "Filament Sensor %s: filament not detected" % (self.name)
        gcmd.respond_info(msg)
    cmd_SET_FILAMENT_SENSOR_help = "Sets the filament sensor on/off"
    def cmd_SET_FILAMENT_SENSOR(self, gcmd):
        self.sensor_enabled = gcmd.get_int("ENABLE", 1)

class SwitchSensor:
    def __init__(self, config):
        printer = config.get_printer()
        buttons = printer.load_object(config, 'buttons')
        switch_pin = config.get('switch_pin')
        buttons.register_buttons([switch_pin], self._button_handler)
        self.runout_helper = RunoutHelper(config)
        self.get_status = self.runout_helper.get_status
    def _button_handler(self, eventtime, state):
        self.runout_helper.note_filament_present(state)

def load_config_prefix(config):
    return SwitchSensor(config)
