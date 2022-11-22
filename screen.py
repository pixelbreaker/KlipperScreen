#!/usr/bin/python

import argparse
import json
import logging
import os
import subprocess
import pathlib
import traceback  # noqa

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango
from importlib import import_module
from jinja2 import Environment
from signal import SIGTERM

from ks_includes import functions
from ks_includes.KlippyWebsocket import KlippyWebsocket
from ks_includes.KlippyRest import KlippyRest
from ks_includes.files import KlippyFiles
from ks_includes.KlippyGtk import KlippyGtk
from ks_includes.printer import Printer
from ks_includes.widgets.keyboard import Keyboard
from ks_includes.config import KlipperScreenConfig
from panels.base_panel import BasePanel

logging.getLogger("urllib3").setLevel(logging.WARNING)

PRINTER_BASE_STATUS_OBJECTS = [
    'bed_mesh',
    'configfile',
    'display_status',
    'extruder',
    'fan',
    'gcode_move',
    'heater_bed',
    'idle_timeout',
    'pause_resume',
    'print_stats',
    'toolhead',
    'virtual_sdcard',
    'webhooks',
    'motion_report',
    'firmware_retraction',
    'exclude_object',
]

klipperscreendir = pathlib.Path(__file__).parent.resolve()


def set_text_direction(lang=None):
    rtl_languages = ['he_IL']
    if lang is None:
        for lng in rtl_languages:
            if os.getenv('LANG').startswith(lng):
                lang = lng
                break
    if lang in rtl_languages:
        Gtk.Widget.set_default_direction(Gtk.TextDirection.RTL)
        logging.debug("Enabling RTL mode")
        return False
    Gtk.Widget.set_default_direction(Gtk.TextDirection.LTR)
    return True


class KlipperScreen(Gtk.Window):
    """ Class for creating a screen for Klipper via HDMI """
    _cur_panels = []
    connecting = False
    connecting_to_printer = None
    connected_printer = None
    files = None
    keyboard = None
    load_panel = {}
    panels = {}
    popup_message = None
    screensaver = None
    printer = None
    subscriptions = []
    updating = False
    update_queue = []
    _ws = None
    screensaver_timeout = None
    reinit_count = 0
    max_retries = 4

    def __init__(self, args, version):
        self.blanking_time = 600
        self.use_dpms = True
        self.apiclient = None
        self.version = version
        self.dialogs = []
        self.confirm = None

        configfile = os.path.normpath(os.path.expanduser(args.configfile))

        self._config = KlipperScreenConfig(configfile, self)
        self.lang_ltr = set_text_direction(self._config.get_main_config().get("language", None))

        Gtk.Window.__init__(self)
        self.connect("key-press-event", self._key_press_event)
        self.set_title("KlipperScreen")
        monitor = Gdk.Display.get_default().get_primary_monitor()
        self.width = self._config.get_main_config().getint("width", monitor.get_geometry().width)
        self.height = self._config.get_main_config().getint("height", monitor.get_geometry().height)
        self.set_default_size(self.width, self.height)
        self.set_resizable(False)
        if not (self._config.get_main_config().get("width") or self._config.get_main_config().get("height")):
            self.fullscreen()
        self.vertical_mode = self.width < self.height
        logging.info(f"Screen resolution: {self.width}x{self.height}")
        self.theme = self._config.get_main_config().get('theme')
        show_cursor = self._config.get_main_config().getboolean("show_cursor", fallback=False)
        self.gtk = KlippyGtk(self, self.width, self.height, self.theme, show_cursor,
                             self._config.get_main_config().get("font_size", "medium"))
        self.init_style()
        self.set_icon_from_file(os.path.join(klipperscreendir, "styles", "icon.svg"))

        self.base_panel = BasePanel(self, title="Base Panel")
        self.add(self.base_panel.get())
        self.show_all()
        if show_cursor:
            self.get_window().set_cursor(
                Gdk.Cursor.new_for_display(Gdk.Display.get_default(), Gdk.CursorType.ARROW))
            os.system("xsetroot  -cursor_name  arrow")
        else:
            self.get_window().set_cursor(
                Gdk.Cursor.new_for_display(Gdk.Display.get_default(), Gdk.CursorType.BLANK_CURSOR))
            os.system("xsetroot  -cursor ks_includes/emptyCursor.xbm ks_includes/emptyCursor.xbm")
        self.base_panel.activate()
        if self._config.errors:
            self.show_error_modal("Invalid config file", self._config.get_errors())
            # Prevent this dialog from being destroyed
            self.dialogs = []
        self.set_screenblanking_timeout(self._config.get_main_config().get('screen_blanking'))

        self.initial_connection()

    def initial_connection(self):
        printers = self._config.get_printers()
        default_printer = self._config.get_main_config().get('default_printer')
        logging.debug(f"Default printer: {default_printer}")
        if [True for p in printers if default_printer in p]:
            self.connect_printer(default_printer)
        elif len(printers) == 1:
            pname = list(printers[0])[0]
            self.connect_printer(pname)
        else:
            self.base_panel.show_printer_select(True)
            self.show_printer_select()

    def connect_printer(self, name):
        self.connecting_to_printer = name
        if self._ws is not None and self._ws.connected:
            self._ws.close()
            return

        data = {
            "moonraker_host": "127.0.0.1",
            "moonraker_port": "7125",
            "moonraker_api_key": False
        }

        logging.info(f"Connecting to printer: {name}")
        for printer in self._config.get_printers():
            pname = list(printer)[0]
            if pname != name:
                continue
            data = printer[pname]
            break

        self.apiclient = KlippyRest(data["moonraker_host"], data["moonraker_port"], data["moonraker_api_key"])
        self.printer = Printer(self.state_execute)
        self.printer.state_callbacks = {
            "disconnected": self.state_disconnected,
            "error": self.state_error,
            "paused": self.state_paused,
            "printing": self.state_printing,
            "ready": self.state_ready,
            "startup": self.state_startup,
            "shutdown": self.state_shutdown
        }
        self._remove_all_panels()
        self.printer_initializing(_("Connecting to %s") % name)

        self._ws = KlippyWebsocket(self,
                                   {
                                       "on_connect": self.init_printer,
                                       "on_message": self._websocket_callback,
                                       "on_close": self.websocket_disconnected
                                   },
                                   data["moonraker_host"],
                                   data["moonraker_port"]
                                   )

        self.files = KlippyFiles(self)
        self._ws.initial_connect()

    def ws_subscribe(self):
        requested_updates = {
            "objects": {
                "bed_mesh": ["profile_name", "mesh_max", "mesh_min", "probed_matrix", "profiles"],
                "configfile": ["config"],
                "display_status": ["progress", "message"],
                "fan": ["speed"],
                "gcode_move": ["extrude_factor", "gcode_position", "homing_origin", "speed_factor", "speed"],
                "idle_timeout": ["state"],
                "pause_resume": ["is_paused"],
                "print_stats": ["print_duration", "total_duration", "filament_used", "filename", "state", "message",
                                "info"],
                "toolhead": ["homed_axes", "estimated_print_time", "print_time", "position", "extruder",
                             "max_accel", "max_accel_to_decel", "max_velocity", "square_corner_velocity"],
                "virtual_sdcard": ["file_position", "is_active", "progress"],
                "webhooks": ["state", "state_message"],
                "firmware_retraction": ["retract_length", "retract_speed", "unretract_extra_length", "unretract_speed"],
                "motion_report": ["live_position", "live_velocity", "live_extruder_velocity"],
                "exclude_object": ["current_object", "objects", "excluded_objects"]
            }
        }
        for extruder in self.printer.get_tools():
            requested_updates['objects'][extruder] = [
                "target", "temperature", "pressure_advance", "smooth_time", "power"]
        for h in self.printer.get_heaters():
            requested_updates['objects'][h] = ["target", "temperature", "power"]
        for f in self.printer.get_fans():
            requested_updates['objects'][f] = ["speed"]
        for f in self.printer.get_filament_sensors():
            requested_updates['objects'][f] = ["enabled", "filament_detected"]
        for p in self.printer.get_output_pins():
            requested_updates['objects'][p] = ["value"]

        self._ws.klippy.object_subscription(requested_updates)

    def _load_panel(self, panel, *args):
        if panel not in self.load_panel:
            logging.debug(f"Loading panel: {panel}")
            panel_path = os.path.join(os.path.dirname(__file__), 'panels', f"{panel}.py")
            logging.info(f"Panel path: {panel_path}")
            if not os.path.exists(panel_path):
                logging.error(f"Panel {panel} does not exist")
                raise FileNotFoundError(os.strerror(2), "\n" + panel_path)

            module = import_module(f"panels.{panel}")
            if not hasattr(module, "create_panel"):
                raise ImportError(f"Cannot locate create_panel function for {panel}")
            self.load_panel[panel] = getattr(module, "create_panel")

        try:
            return self.load_panel[panel](*args)
        except Exception as e:
            raise RuntimeError(f"Unable to create panel: {panel}\n{e}") from e

    def show_panel(self, panel_name, panel_type, title, remove=None, pop=True, **kwargs):
        try:
            if remove == 2:
                self._remove_all_panels()
            elif remove == 1:
                self._remove_current_panel(pop)

            if panel_name not in self.panels:
                try:
                    self.panels[panel_name] = self._load_panel(panel_type, self, title)
                    if hasattr(self.panels[panel_name], "initialize"):
                        self.panels[panel_name].initialize(**kwargs)
                except Exception as e:
                    if panel_name in self.panels:
                        del self.panels[panel_name]
                    self.show_error_modal(f"Unable to load panel {panel_type}", f"{e}")
                    return

            logging.debug(f"Attaching panel {panel_name}")
            self.base_panel.add_content(self.panels[panel_name])
            self.base_panel.show_back(len(self._cur_panels) > 0)

            if hasattr(self.panels[panel_name], "process_update"):
                self.add_subscription(panel_name)
            if hasattr(self.panels[panel_name], "activate"):
                self.panels[panel_name].activate()
            self.show_all()
        except Exception as e:
            logging.exception(f"Error attaching panel:\n{e}")

        self._cur_panels.append(panel_name)
        logging.debug(f"Current panel hierarchy: {' > '.join(self._cur_panels)}")

    def show_popup_message(self, message, level=3):
        self.close_screensaver()
        if self.popup_message is not None:
            self.close_popup_message()

        msg = Gtk.Button(label=f"{message}")
        msg.set_hexpand(True)
        msg.set_vexpand(True)
        msg.get_child().set_line_wrap(True)
        msg.get_child().set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        msg.connect("clicked", self.close_popup_message)

        close = Gtk.Button(label="<b><big>X</big></b>")
        close.set_hexpand(False)
        close.set_vexpand(True)
        close.get_child().set_use_markup(True)
        close.set_can_focus(False)
        close.connect("clicked", self.close_popup_message)

        box = Gtk.Box()
        box.set_size_request(self.width, -1)
        box.set_halign(Gtk.Align.CENTER)
        box.get_style_context().add_class("message_popup")
        if level == 1:
            box.get_style_context().add_class("message_popup_echo")
        elif level == 2:
            box.get_style_context().add_class("message_popup_warning")
        else:
            box.get_style_context().add_class("message_popup_error")

        box.add(msg)
        box.add(close)

        self.base_panel.get().put(box, 0, 0)

        self.show_all()
        self.popup_message = box

        if self._config.get_main_config().getboolean('autoclose_popups', True):
            GLib.timeout_add_seconds(10, self.close_popup_message)

        return False

    def close_popup_message(self, widget=None):
        if self.popup_message is None:
            return

        self.base_panel.get().remove(self.popup_message)
        self.popup_message = None

    def show_error_modal(self, err, e=""):
        logging.error(f"Showing error modal: {err} {e}")

        title = Gtk.Label()
        title.set_markup(f"<b>{err}</b>\n")
        title.set_line_wrap(True)
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        version = Gtk.Label(label=f"{self.version}")
        version.set_halign(Gtk.Align.END)

        message = Gtk.Label(label=f"{e}")
        message.set_line_wrap(True)
        scroll = self.gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(message)

        help_notice = Gtk.Label(label="Provide /tmp/KlipperScreen.log when asking for help.\n")
        help_notice.set_line_wrap(True)

        grid = Gtk.Grid()
        grid.attach(title, 0, 0, 1, 1)
        grid.attach(version, 1, 0, 1, 1)
        grid.attach(Gtk.Separator(), 0, 1, 2, 1)
        grid.attach(scroll, 0, 2, 2, 1)
        grid.attach(help_notice, 0, 3, 2, 1)

        buttons = [
            {"name": _("Go Back"), "response": Gtk.ResponseType.CANCEL}
        ]
        self.gtk.Dialog(self, buttons, grid, self.error_modal_response)

    def error_modal_response(self, widget, response_id):
        widget.destroy()
        self.reload_panels()

    def restart_warning(self, value):
        logging.debug(f"Showing restart warning because: {value}")

        buttons = [
            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL},
            {"name": _("Restart"), "response": Gtk.ResponseType.OK}
        ]

        label = Gtk.Label()
        label.set_markup(_("To apply %s KlipperScreen needs to be restarted") % value)
        label.set_hexpand(True)
        label.set_halign(Gtk.Align.CENTER)
        label.set_vexpand(True)
        label.set_valign(Gtk.Align.CENTER)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)

        self.gtk.Dialog(self, buttons, label, self.restart_ks)

    def restart_ks(self, widget, response_id):
        if response_id == Gtk.ResponseType.OK:
            logging.debug("Restarting")
            self._ws.send_method("machine.services.restart", {"service": "KlipperScreen"})
        widget.destroy()

    def init_style(self):
        css_data = pathlib.Path(os.path.join(klipperscreendir, "styles", "base.css")).read_text()

        with open(os.path.join(klipperscreendir, "styles", "base.conf")) as f:
            style_options = json.load(f)
        # Load custom theme
        theme = os.path.join(klipperscreendir, "styles", self.theme)
        theme_style = os.path.join(theme, "style.css")
        theme_style_conf = os.path.join(theme, "style.conf")

        if os.path.exists(theme_style):
            with open(theme_style) as css:
                css_data += css.read()
        if os.path.exists(theme_style_conf):
            try:
                with open(theme_style_conf) as f:
                    style_options.update(json.load(f))
            except Exception as e:
                logging.error(f"Unable to parse custom template conf file:\n{e}")

        self.gtk.color_list = style_options['graph_colors']

        for i in range(len(style_options['graph_colors']['extruder']['colors'])):
            num = "" if i == 0 else i
            css_data += "\n.graph_label_extruder%s {border-left-color: #%s}" % (
                num,
                style_options['graph_colors']['extruder']['colors'][i]
            )
        for i in range(len(style_options['graph_colors']['bed']['colors'])):
            css_data += "\n.graph_label_heater_bed%s {border-left-color: #%s}" % (
                "" if i == 0 else i + 1,
                style_options['graph_colors']['bed']['colors'][i]
            )
        for i in range(len(style_options['graph_colors']['fan']['colors'])):
            css_data += "\n.graph_label_fan_%s {border-left-color: #%s}" % (
                i + 1,
                style_options['graph_colors']['fan']['colors'][i]
            )
        for i in range(len(style_options['graph_colors']['sensor']['colors'])):
            css_data += "\n.graph_label_sensor_%s {border-left-color: #%s}" % (
                i + 1,
                style_options['graph_colors']['sensor']['colors'][i]
            )

        css_data = css_data.replace("KS_FONT_SIZE", f"{self.gtk.get_font_size()}")

        style_provider = Gtk.CssProvider()
        style_provider.load_from_data(css_data.encode())

        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def is_updating(self):
        return self.updating

    def _go_to_submenu(self, widget, name):
        logging.info(f"#### Go to submenu {name}")
        # Find current menu item
        if "main_panel" in self._cur_panels:
            menu = "__main"
        elif "splash_screen" in self._cur_panels:
            menu = "__splashscreen"
        else:
            menu = "__print"

        logging.info(f"#### Menu {menu}")
        disname = self._config.get_menu_name(menu, name)
        menuitems = self._config.get_menu_items(menu, name)
        if len(menuitems) != 0:
            self.show_panel(name, "menu", disname, 1, False, items=menuitems)
        else:
            logging.info("No items in menu")

    def _remove_all_panels(self):
        self.subscriptions = []
        self._cur_panels = []
        for _ in self.base_panel.content.get_children():
            self.base_panel.content.remove(_)
        for panel in list(self.panels):
            if panel not in ["printer_select", "splash_screen"]:
                del self.panels[panel]
        for dialog in self.dialogs:
            dialog.destroy()
        self.close_screensaver()

    def _remove_current_panel(self, pop=True):
        if len(self._cur_panels) <= 0:
            self.reload_panels()
            return
        self.base_panel.remove(self.panels[self._cur_panels[-1]].get_content())
        if hasattr(self.panels[self._cur_panels[-1]], "deactivate"):
            self.panels[self._cur_panels[-1]].deactivate()
        if self._cur_panels[-1] in self.subscriptions:
            self.subscriptions.remove(self._cur_panels[-1])
        if pop is True:
            self._cur_panels.pop()
            if len(self._cur_panels) > 0:
                self.base_panel.add_content(self.panels[self._cur_panels[-1]])
                self.base_panel.show_back(len(self._cur_panels) != 1)
                if hasattr(self.panels[self._cur_panels[-1]], "activate"):
                    self.panels[self._cur_panels[-1]].activate()
                if hasattr(self.panels[self._cur_panels[-1]], "process_update"):
                    self.add_subscription(self._cur_panels[-1])
                self.show_all()

    def _menu_go_back(self, widget=None):
        logging.info("#### Menu go back")
        self.remove_keyboard()
        if self._config.get_main_config().getboolean('autoclose_popups', True):
            self.close_popup_message()
        self._remove_current_panel()

    def _menu_go_home(self, widget=None):
        logging.info("#### Menu go home")
        self.remove_keyboard()
        self.close_popup_message()
        while len(self._cur_panels) > 1:
            self._remove_current_panel()

    def add_subscription(self, panel_name):
        if panel_name not in self.subscriptions:
            self.subscriptions.append(panel_name)

    def reset_screensaver_timeout(self, *args):
        if self.screensaver_timeout is not None:
            GLib.source_remove(self.screensaver_timeout)
            self.screensaver_timeout = GLib.timeout_add_seconds(self.blanking_time, self.show_screensaver)

    def show_screensaver(self):
        logging.debug("Showing Screensaver")
        if self.screensaver is not None:
            self.close_screensaver()
        self.remove_keyboard()
        for dialog in self.dialogs:
            dialog.hide()

        close = Gtk.Button()
        close.connect("clicked", self.close_screensaver)

        box = Gtk.Box()
        box.set_size_request(self.width, self.height)
        box.pack_start(close, True, True, 0)
        box.set_halign(Gtk.Align.CENTER)
        box.get_style_context().add_class("screensaver")
        self.base_panel.get().put(box, 0, 0)

        # Avoid leaving a cursor-handle
        close.grab_focus()
        self.screensaver = box
        self.screensaver.show_all()
        return False

    def close_screensaver(self, widget=None):
        if self.screensaver is None:
            return False
        logging.debug("Closing Screensaver")
        self.base_panel.get().remove(self.screensaver)
        self.screensaver = None
        if self.use_dpms:
            self.wake_screen()
        else:
            self.screensaver_timeout = GLib.timeout_add_seconds(self.blanking_time, self.show_screensaver)
        for dialog in self.dialogs:
            dialog.show()
        self.show_all()
        return False

    def check_dpms_state(self):
        if not self.use_dpms:
            return False
        state = functions.get_DPMS_state()
        if state == functions.DPMS_State.Fail:
            logging.info("DPMS State FAIL: Stopping DPMS Check")
            self.set_dpms(False)
            return False
        elif state != functions.DPMS_State.On:
            if self.screensaver is None:
                self.show_screensaver()
        return True

    def wake_screen(self):
        # Wake the screen (it will go to standby as configured)
        if self._config.get_main_config().get('screen_blanking') != "off":
            logging.debug("Screen wake up")
            os.system("xset -display :0 dpms force on")

    def set_dpms(self, use_dpms):
        self.use_dpms = use_dpms
        logging.info(f"DPMS set to: {self.use_dpms}")
        self.set_screenblanking_timeout(self._config.get_main_config().get('screen_blanking'))

    def set_screenblanking_timeout(self, time):
        os.system("xset -display :0 s blank")
        os.system("xset -display :0 s off")
        self.use_dpms = self._config.get_main_config().getboolean("use_dpms", fallback=True)

        if time == "off":
            logging.debug(f"Screen blanking: {time}")
            if self.screensaver_timeout is not None:
                GLib.source_remove(self.screensaver_timeout)
            os.system("xset -display :0 dpms 0 0 0")
            return

        self.blanking_time = abs(int(time))
        logging.debug(f"Changing screen blanking to: {self.blanking_time}")
        if self.use_dpms and functions.dpms_loaded is True:
            os.system("xset -display :0 +dpms")
            if functions.get_DPMS_state() == functions.DPMS_State.Fail:
                logging.info("DPMS State FAIL")
            else:
                logging.debug("Using DPMS")
                os.system("xset -display :0 s off")
                os.system(f"xset -display :0 dpms 0 {self.blanking_time} 0")
                GLib.timeout_add_seconds(1, self.check_dpms_state)
                return
        # Without dpms just blank the screen
        logging.debug("Not using DPMS")
        os.system("xset -display :0 dpms 0 0 0")
        if self.screensaver_timeout is None:
            self.screensaver_timeout = GLib.timeout_add_seconds(self.blanking_time, self.show_screensaver)
        return

    def set_updating(self, updating=False):
        if self.updating is True and updating is False and len(self.update_queue) > 0:
            i = self.update_queue.pop()
            self.update_queue = []
            i[0]()
        self.updating = updating

    def show_printer_select(self, widget=None):
        self.base_panel.show_heaters(False)
        self.show_panel("printer_select", "printer_select", _("Printer Select"), 2)

    def state_execute(self, callback):
        if self.is_updating():
            self.update_queue.append([callback])
        else:
            self.init_printer()
            callback()

    def websocket_disconnected(self, msg):
        self.printer_initializing(msg, remove=True)
        self.connecting = True
        self.connected_printer = None
        self.files.reset()
        self.files = None
        self.printer.reset()
        self.printer = None
        self.connect_printer(self.connecting_to_printer)

    def state_disconnected(self):
        logging.debug("### Going to disconnected")
        self.close_screensaver()
        self.printer_initializing(_("Klipper has disconnected"), remove=True)

    def state_error(self):
        self.close_screensaver()
        msg = _("Klipper has encountered an error.") + "\n"
        state = self.printer.get_stat("webhooks", "state_message")
        if "FIRMWARE_RESTART" in state:
            msg += _("A FIRMWARE_RESTART may fix the issue.") + "\n"
        elif "micro-controller" in state:
            msg += _("Please recompile and flash the micro-controller.") + "\n"
        self.printer_initializing(msg + "\n" + state, remove=True)

    def state_paused(self):
        if "job_status" not in self._cur_panels:
            self.printer_printing()

    def state_printing(self):
        if "job_status" not in self._cur_panels:
            self.printer_printing()
        else:
            self.panels["job_status"].new_print()

    def state_ready(self):
        # Do not return to main menu if completing a job, timeouts/user input will return
        if "job_status" in self._cur_panels:
            return
        self.printer_ready()

    def state_startup(self):
        self.printer_initializing(_("Klipper is attempting to start"))

    def state_shutdown(self):
        self.close_screensaver()
        msg = self.printer.get_stat("webhooks", "state_message")
        msg = msg if "ready" not in msg else ""
        self.printer_initializing(_("Klipper has shutdown") + "\n\n" + msg, remove=True)

    def toggle_macro_shortcut(self, value):
        self.base_panel.show_macro_shortcut(value)

    def change_language(self, lang):
        self._config.install_language(lang)
        self.lang_ltr = set_text_direction(lang)
        self._config._create_configurable_options(self)
        self.reload_panels()

    def reload_panels(self, *args):
        self._remove_all_panels()
        self.printer.change_state(self.printer.state)

    def _websocket_callback(self, action, data):
        if self.connecting:
            return
        if action == "notify_klippy_disconnected":
            self.printer.change_state("disconnected")
            return
        elif action == "notify_klippy_shutdown":
            self.printer.change_state("shutdown")
        elif action == "notify_klippy_ready":
            self.printer.change_state("ready")
        elif action == "notify_status_update" and self.printer.state != "shutdown":
            self.printer.process_update(data)
        elif action == "notify_filelist_changed":
            if self.files is not None:
                self.files.process_update(data)
        elif action == "notify_metadata_update":
            self.files.request_metadata(data['filename'])
        elif action == "notify_update_response":
            if 'message' in data and 'Error' in data['message']:
                logging.error(f"{action}:{data['message']}")
                self.show_popup_message(data['message'], 3)
        elif action == "notify_power_changed":
            logging.debug("Power status changed: %s", data)
            self.printer.process_power_update(data)
            self.panels['splash_screen'].check_power_status()
        elif action == "notify_gcode_response" and self.printer.state not in ["error", "shutdown"]:
            if not (data.startswith("B:") or data.startswith("T:")):
                if data.startswith("echo: "):
                    self.show_popup_message(data[6:], 1)
                elif data.startswith("!! "):
                    self.show_popup_message(data[3:], 3)
                if "SAVE_CONFIG" in data and self.printer.state == "ready":
                    script = {"script": "SAVE_CONFIG"}
                    self._confirm_send_action(
                        None,
                        _("Save configuration?") + "\n\n" + _("Klipper will reboot"),
                        "printer.gcode.script",
                        script
                    )
        self.base_panel.process_update(action, data)
        if self._cur_panels and self._cur_panels[-1] in self.subscriptions:
            self.panels[self._cur_panels[-1]].process_update(action, data)

    def _confirm_send_action(self, widget, text, method, params=None):
        buttons = [
            {"name": _("Continue"), "response": Gtk.ResponseType.OK},
            {"name": _("Cancel"), "response": Gtk.ResponseType.CANCEL}
        ]

        try:
            env = Environment(extensions=["jinja2.ext.i18n"], autoescape=True)
            env.install_gettext_translations(self._config.get_lang())
            j2_temp = env.from_string(text)
            text = j2_temp.render()
        except Exception as e:
            logging.debug(f"Error parsing jinja for confirm_send_action\n{e}")

        label = Gtk.Label()
        label.set_markup(text)
        label.set_hexpand(True)
        label.set_halign(Gtk.Align.CENTER)
        label.set_vexpand(True)
        label.set_valign(Gtk.Align.CENTER)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)

        if self.confirm is not None:
            self.confirm.destroy()
        self.confirm = self.gtk.Dialog(self, buttons, label, self._confirm_send_action_response, method, params)

    def _confirm_send_action_response(self, widget, response_id, method, params):
        if response_id == Gtk.ResponseType.OK:
            self._send_action(widget, method, params)

        widget.destroy()

    def _send_action(self, widget, method, params):
        logging.info(f"{method}: {params}")
        self._ws.send_method(method, params)

    def printer_initializing(self, msg, remove=False):
        self.close_popup_message()
        if 'splash_screen' not in self.panels or remove:
            self.show_panel('splash_screen', "splash_screen", None, 2)
        self.panels['splash_screen'].update_text(msg)

    def search_power_devices(self, power_devices):
        if self.connected_printer is None or not power_devices:
            return
        found_devices = []
        devices = self.printer.get_power_devices()
        logging.debug("Power devices: %s", devices)
        if devices is not None:
            for device in devices:
                for power_device in power_devices:
                    if device == power_device and power_device not in found_devices:
                        found_devices.append(power_device)
        if found_devices:
            logging.info("Found %s", found_devices)
            return found_devices
        else:
            logging.info("Associated power devices not found")
            return None

    def power_on(self, widget, devices):
        for device in devices:
            if self.printer.get_power_device_status(device) == "off":
                self.show_popup_message(_("Sending Power ON signal to: %s") % devices, level=1)
                logging.info("%s is OFF, Sending Power ON signal", device)
                self._ws.klippy.power_device_on(device)
            elif self.printer.get_power_device_status(device) == "on":
                logging.info("%s is ON", device)

    def init_printer(self):
        if self.reinit_count > self.max_retries or 'printer_select' in self._cur_panels:
            return
        state = self.apiclient.get_server_info()
        if state is False:
            logging.info("Moonraker not connected")
            return
        self.connecting = not self._ws.connected
        self.connected_printer = self.connecting_to_printer
        self.base_panel.set_ks_printer_cfg(self.connected_printer)

        # Moonraker is ready, set a loop to init the printer
        self.reinit_count += 1

        powerdevs = self.apiclient.send_request("machine/device_power/devices")
        if powerdevs is not False:
            self.printer.configure_power_devices(powerdevs['result'])

        if state['result']['klippy_connected'] is False:
            logging.info("Klipper not connected")
            msg = _("Moonraker: connected") + "\n\n"
            msg += f"Klipper: {state['result']['klippy_state']}" + "\n\n"
            if self.reinit_count <= self.max_retries:
                msg += _("Retrying") + f' #{self.reinit_count}'
            self.printer_initializing(msg)
            GLib.timeout_add_seconds(3, self.init_printer)
            return

        printer_info = self.apiclient.get_printer_info()
        if printer_info is False:
            self.printer_initializing("Unable to get printer info from moonraker")
            GLib.timeout_add_seconds(3, self.init_printer)
            return

        config = self.apiclient.send_request("printer/objects/query?configfile")
        if config is False:
            self.printer_initializing("Error getting printer configuration")
            GLib.timeout_add_seconds(3, self.init_printer)
            return

        # Reinitialize printer, in case the printer was shut down and anything has changed.
        self.printer.reinit(printer_info['result'], config['result']['status'])

        self.ws_subscribe()
        extra_items = (self.printer.get_tools()
                       + self.printer.get_heaters()
                       + self.printer.get_fans()
                       + self.printer.get_filament_sensors()
                       + self.printer.get_output_pins()
                       )

        data = self.apiclient.send_request("printer/objects/query?" + "&".join(PRINTER_BASE_STATUS_OBJECTS +
                                                                               extra_items))
        if data is False:
            self.printer_initializing("Error getting printer object data with extra items")
            GLib.timeout_add_seconds(3, self.init_printer)
            return

        tempstore = self.apiclient.send_request("server/temperature_store")
        if tempstore is not False:
            self.printer.init_temp_store(tempstore['result'])
        self.printer.process_update(data['result']['status'])

        self.files.initialize()
        self.files.refresh_files()

        logging.info("Printer initialized")
        self.reinit_count = 0

    def base_panel_show_all(self):
        self.base_panel.show_macro_shortcut(self._config.get_main_config().getboolean('side_macro_shortcut', True))
        self.base_panel.show_heaters(True)
        self.base_panel.show_estop(True)

    def printer_ready(self):
        self.close_popup_message()
        self.show_panel('main_panel', "main_menu", None, 2, items=self._config.get_menu_items("__main"))
        self.base_panel_show_all()

    def printer_printing(self):
        self.close_screensaver()
        self.close_popup_message()
        self.show_panel('job_status', "job_status", _("Printing"), 2)
        self.base_panel_show_all()
        for dialog in self.dialogs:
            dialog.destroy()

    def show_keyboard(self, widget=None, event=None, entry=None):
        if self.keyboard is not None:
            return

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(self.gtk.get_content_width(), self.gtk.get_keyboard_height())

        if self._config.get_main_config().getboolean("use-matchbox-keyboard", False):
            env = os.environ.copy()
            usrkbd = os.path.expanduser("~/.matchbox/keyboard.xml")
            if os.path.isfile(usrkbd):
                env["MB_KBD_CONFIG"] = usrkbd
            else:
                env["MB_KBD_CONFIG"] = "ks_includes/locales/keyboard.xml"
            p = subprocess.Popen(["matchbox-keyboard", "--xid"], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env)
            xid = int(p.stdout.readline())
            logging.debug(f"XID {xid}")
            logging.debug(f"PID {p.pid}")

            keyboard = Gtk.Socket()
            box.get_style_context().add_class("keyboard_matchbox")
            box.pack_start(keyboard, True, True, 0)
            self.base_panel.get_content().pack_end(box, False, False, 0)

            self.show_all()
            keyboard.add_id(xid)

            self.keyboard = {
                "box": box,
                "process": p,
                "socket": keyboard
            }
            return
        if entry is None:
            logging.debug("Error: no entry provided for keyboard")
            return
        box.get_style_context().add_class("keyboard_box")
        box.add(Keyboard(self, self.remove_keyboard, entry=entry))
        self.keyboard = {
            "entry": entry,
            "box": box
        }
        self.base_panel.get_content().pack_end(box, False, False, 0)
        self.base_panel.get_content().show_all()

    def remove_keyboard(self, widget=None, event=None):
        if self.keyboard is None:
            return

        if 'process' in self.keyboard:
            os.kill(self.keyboard['process'].pid, SIGTERM)
        self.base_panel.get_content().remove(self.keyboard['box'])
        self.keyboard = None

    def _key_press_event(self, widget, event):
        keyval_name = Gdk.keyval_name(event.keyval)
        if keyval_name == "Escape":
            self._menu_go_home()
        elif keyval_name == "BackSpace" and len(self._cur_panels) > 1:
            self.base_panel.back()


def main():
    version = functions.get_software_version()
    parser = argparse.ArgumentParser(description="KlipperScreen - A GUI for Klipper")
    homedir = os.path.expanduser("~")

    parser.add_argument(
        "-c", "--configfile", default=os.path.join(homedir, "KlipperScreen.conf"), metavar='<configfile>',
        help="Location of KlipperScreen configuration file"
    )
    logdir = os.path.join(homedir, "printer_data", "logs")
    if not os.path.exists(logdir):
        logdir = "/tmp"
    parser.add_argument(
        "-l", "--logfile", default=os.path.join(logdir, "KlipperScreen.log"), metavar='<logfile>',
        help="Location of KlipperScreen logfile output"
    )
    args = parser.parse_args()

    functions.setup_logging(
        os.path.normpath(os.path.expanduser(args.logfile)),
        version
    )

    functions.patch_threading_excepthook()

    logging.info(f"KlipperScreen version: {version}")

    win = KlipperScreen(args, version)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        logging.exception(f"Fatal error in main loop:\n{ex}")
