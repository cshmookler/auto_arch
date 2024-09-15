#!/usr/bin/python

"""Automatically installs MOOS"""

from argparse import Action, ArgumentParser, Namespace
import atexit
import curses
from dataclasses import dataclass, fields
from enum import Enum, IntEnum, auto
import json
import os
from queue import Queue, Empty
import shutil
from signal import signal, SIGINT, SIGTERM
import subprocess
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Tuple


# Global paths.
this_dir = os.path.dirname(__file__)
home_dir = os.path.expanduser("~")


# Subprocess and filesystem utilities
# ----------------------------------------------------------------------------


def run(
    *args,
    input: str | None = None,
    quiet: bool = True,
    env: Dict[str, str] | None = None
) -> bool:
    return (
        subprocess.run(
            args,
            capture_output=quiet,
            env=env,
            input=input,
            text=True if input else None,
        ).returncode
        == 0
    )


def get(*args) -> str | None:
    result = subprocess.run(args, capture_output=True)
    if result.returncode == 0:
        return result.stdout.decode().strip()
    return None


def write(path: str, mode: str, text: str) -> bool:
    try:
        with open(path, mode) as file:
            file.write(text)
    except:
        return False
    return True


def copy(src: str, dst: str) -> bool:
    return run("cp", "-r", src, dst)


def remove(path: str) -> bool:
    return run("rm", "-rf", path)


def make_absolute(path: str) -> str:
    if os.path.isabs(path):
        return path
    else:
        return this_dir + "/" + path


# Message reporting and processing utilities
# ----------------------------------------------------------------------------


class Level(IntEnum):
    normal = auto()
    success = auto()
    error = auto()
    warning = auto()
    info = auto()
    verbose = auto()


@dataclass
class Message:
    raw: str
    level: Level


class Logger:
    def __init__(self, level: Level) -> None:
        self._log = Queue()
        self._level = level
        self._log_file = None
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        self.show_all_as_ansi()
        if self._log_file is not None:
            self._log_file.close()

    def _put(self, msg: str, level: Level) -> None:
        self._log.put(Message(msg, level), block=False)

    def normal(self, msg: str) -> None:
        self._put(msg, Level.normal)

    def success(self, msg: str) -> None:
        self._put(msg, Level.success)

    def error(self, msg: str) -> None:
        self._put("  [Error] " + msg + ".", Level.error)

    def warning(self, msg: str) -> None:
        self._put("[Warning] " + msg + ".", Level.warning)

    def info(self, msg: str) -> None:
        self._put("   [Info] " + msg + ".", Level.info)

    def verbose(self, msg: str) -> None:
        self._put("[Verbose] " + msg + ".", Level.verbose)

    def set_log_file(self, path: str) -> bool:
        try:
            self._log_file = open(path, "w")
            return True
        except:
            return False

    def set_log_level(self, level: Level) -> None:
        self._level = level

    def _get_next(self) -> Optional[Message]:
        try:
            msg: Message = self._log.get_nowait()
        except:
            return None
        if self._log_file is not None:
            self._log_file.write(msg.raw + "\n")
        return msg

    @staticmethod
    def _green(msg: str) -> str:
        return "\033[1;32m" + msg + "\033[0m"

    @staticmethod
    def _red(msg: str) -> str:
        return "\033[1;31m" + msg + "\033[0m"

    @staticmethod
    def _yellow(msg: str) -> str:
        return "\033[1;33m" + msg + "\033[0m"

    @staticmethod
    def _blue(msg: str) -> str:
        return "\033[1;34m" + msg + "\033[0m"

    @staticmethod
    def _as_ansi(msg: str, level: Level) -> str:
        if level == Level.normal:
            return msg
        if level == Level.success:
            return Logger._green(msg)
        if level == Level.error:
            return Logger._red(msg)
        if level == Level.warning:
            return Logger._yellow(msg)
        if level == Level.info:
            return Logger._blue(msg)
        if level == Level.verbose:
            return msg

        return "[Unknown] " + msg

    def show_all_as_ansi(self) -> None:
        while not self._log.empty():
            msg: Optional[Message] = self._get_next()
            if msg is None:
                break
            if msg.level > self._level:
                break
            print(Logger._as_ansi(msg.raw, msg.level))

    def show_all_as_curses(
        self,
        color_setter: Callable[[Level], None],
        writer: Callable[[str], None],
    ) -> None:
        while not self._log.empty():
            msg: Optional[Message] = self._get_next()
            if msg is None:
                break
            if msg.level > self._level:
                break

            color_setter(msg.level)
            writer(msg.raw + "\n")
            color_setter(Level.normal)


# The global logger object.
logger = Logger(Level.verbose)


# ----------------------------------------------------------------------------


def list_all_devices() -> Optional[List[str]]:
    devices = get(
        "lsblk",
        "--noheadings",
        "--nodeps",
        "--output",
        "path",
    )
    if not devices:
        logger.error("Failed to get device information from lsblk")
        return None

    return str(devices).splitlines()


def is_device_valid(dev_path: str, min_dev_bytes: int) -> bool:
    dev_info = get(
        "lsblk",
        "--noheadings",
        "--nodeps",
        "--bytes",
        "--output",
        "path,size",
        dev_path,
    )
    if not dev_info:
        logger.error(
            "Failed to get device information from lsblk for device: "
            + dev_path
        )
        return False

    dev_info = str(dev_info).split()
    if len(dev_info) <= 1:
        logger.error("Not enough fields given by lsblk for device: " + dev_path)
        return False

    if dev_path != dev_info[0]:
        logger.error(
            "Wrong device given by lsblk."
            + "\nExpected: "
            + dev_path
            + "\n   Given:"
            + dev_info[0]
        )
        return False

    dev_size = dev_info[1]

    if int(dev_size) < min_dev_bytes:
        logger.error(
            "Not enough space on device: "
            + dev_path
            + "\n   Minimum required: "
            + str(min_dev_bytes)
            + " bytes"
            + "\nAvailable on device: "
            + dev_size
            + " bytes"
        )
        return False

    return True


def device_lacks_partitions(dev_path: str) -> Optional[bool]:
    parts = get("lsblk", "--noheadings", "--output", "path", dev_path)
    if not parts:
        logger.error(
            "Failed to use lsblk to list partitions on device: " + dev_path
        )
        return None

    parts = str(parts).splitlines()[1:]
    if len(parts) > 0:
        logger.warning("Partitions found on device: " + dev_path)
        return False

    return True


def get_device(min_size: int) -> Optional[str]:
    """Select the device to format for installation"""

    devices = list_all_devices()
    if not devices:
        logger.error("Failed to list devices")
        return None

    for dev_path in devices:
        if not is_device_valid(dev_path, min_size):
            logger.info(
                "The minimum requirements for installation were not met by device: "
                + dev_path
            )
            continue

        if not device_lacks_partitions(dev_path):
            logger.info(
                "Formatting a device that already contains partitions will result in irreversible data loss!"
                "\n\t\tExplicit permission (via interactive mode) is required to format a device with existing partitions"
            )
            continue

        return dev_path

    return None


class Field:
    @staticmethod
    def default_validator(_: str) -> bool:
        return True

    @staticmethod
    def numeric_validator(value: str) -> bool:
        if not value.isnumeric():
            logger.error("The given value is not numeric: " + value)
            return False
        return True

    @staticmethod
    def boot_label_validator(value: str) -> bool:
        if not value:
            logger.error("Boot labels must contain at least one character")
            return False
        if not (value.isprintable() and value.isascii()):
            logger.error(
                "Boot labels cannot contain non-printable or non-ascii characters"
            )
            return False
        return True

    @staticmethod
    def hostname_validator(value: str) -> bool:
        if not value:
            logger.error("Hostnames must contain at least one character")
            return False
        if len(value) > 64:
            logger.error("Hostnames cannot be longer than 64 characters")
            return False
        if not (
            value.replace("-", "").islower()
            and value.replace("-", "").isalnum()
        ):
            logger.error(
                "Hostnames may only contain lowercase letters, numbers, and hyphens"
            )
            return False
        return True

    @staticmethod
    def name_validator(value: str) -> bool:
        if not value:
            logger.error("Names must contain at least one character")
            return False
        if value.isnumeric():
            logger.error("Names cannot be entirely numeric")
            return False
        if value.startswith("-"):
            logger.error("Names cannot start with a hyphen")
            return False
        if len(value) > 32:
            logger.error("Names cannot be longer than 32 characters")
            return False
        if not value.replace("-", "").replace("_", "").isalnum():
            logger.error(
                "Names may only contain letters, numbers, underscores, and hyphens"
            )
            return False
        return True

    @staticmethod
    def password_validator(value: str) -> bool:
        if not value:
            logger.error("Passwords must contain at least one character")
            return False
        if not (value.isprintable() and value.isascii()):
            logger.error(
                "Passwords cannot contain non-printable or non-ascii characters"
            )
            return False
        return True

    def __init__(
        self,
        default_value: Any,
        types: Any,
        validator: Callable[[str], bool] = default_validator,
    ) -> None:
        self._value = default_value
        self._types = types
        self._validator = validator

    def get(self) -> Any:
        return self._value

    def get_str(self) -> str:
        if self._value is None:
            return ""
        return str(self._value)

    def set(self, value: Any) -> bool:
        if self._validator(str(value)):
            self._value = value
            return True
        return False


@dataclass
class Profile:
    network_install: Field = Field(False, bool)
    min_device_bytes: Field = Field(
        int(10e9), int, validator=Field.numeric_validator
    )
    device: Field = Field(None, Optional[str])
    boot_label: Field = Field("MOOS", str, validator=Field.boot_label_validator)
    time_zone: Field = Field("America/Denver", str)
    hostname: Field = Field("moos", str, validator=Field.hostname_validator)
    root_password: Field = Field(
        "root", str, validator=Field.password_validator
    )
    username: Field = Field("main", str, validator=Field.name_validator)
    user_password: Field = Field(
        "main", str, validator=Field.password_validator
    )
    sudo_group: Field = Field("wheel", str, validator=Field.name_validator)
    restart: Field = Field(True, bool)

    def to_dict(self) -> dict:
        return {
            field.name: getattr(self, field.name).get()
            for field in fields(self)
        }


def dict_to_profile(profile_dict: dict) -> Profile:
    profile = Profile()
    for key, value in profile_dict.items():
        if hasattr(profile, key):
            field: Field = getattr(profile, key)
            if not field.set(value):
                logger.warning(
                    "The given value is invalid for the cooresponding field:"
                    + "\n\tfield: "
                    + key
                    + "\n\tvalue: "
                    + value
                )
            setattr(profile, key, field)
        else:
            logger.warning("Unrecognized field in profile: " + key)
    return profile


def dump_packages(packages: List[str], path: str) -> bool:
    try:
        with open(path, "w") as packages_file:
            packages_file.write("\n".join(packages))
        return True
    except:
        logger.error("Failed to write the package list to " + path)
        return False


def load_packages(path: str) -> Optional[List[str]]:
    try:
        with open(path, "r") as packages_file:
            return [line.strip() for line in packages_file]
    except:
        logger.error("Failed to read the package list from " + path)
        return None


def dump_profile(profile: Profile, path: str) -> bool:
    try:
        with open(path, "w") as profile_file:
            json.dump(profile.to_dict(), profile_file, indent=4)
        return True
    except:
        logger.error("Failed to write the profile to " + path)
        return False


def load_profile(path: str) -> Optional[Profile]:
    profile = Profile()
    try:
        with open(path, "r") as profile_file:
            return dict_to_profile(json.load(profile_file))
    except:
        logger.error("Failed to read the profile from " + path)
        return None


class CursesApp:
    def _hide_cursor(self) -> None:
        curses.curs_set(0)  # Hide the cursor

    def _show_cursor(self) -> None:
        curses.curs_set(1)  # Show the cursor

    def _init_colors(self) -> None:
        curses.start_color()
        curses.init_pair(Level.normal, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(Level.success, curses.COLOR_GREEN, curses.COLOR_BLACK)
        curses.init_pair(Level.error, curses.COLOR_RED, curses.COLOR_BLACK)
        curses.init_pair(
            Level.warning,
            curses.COLOR_YELLOW,
            curses.COLOR_BLACK,
        )
        curses.init_pair(Level.info, curses.COLOR_CYAN, curses.COLOR_BLACK)
        curses.init_pair(Level.verbose, curses.COLOR_WHITE, curses.COLOR_BLACK)

    def _set_color(self, color: Level, window=None) -> None:
        if not window:
            window = self.win
        window.bkgdset(curses.color_pair(color))

    def __init__(self) -> None:
        # Beginning application initialization
        self.good = False

        # Ensure that the terminal is restored to its original state
        self.clean = False
        atexit.register(self.cleanup)

        # Identify the terminal type and send required setup codes (if any)
        self.screen = curses.initscr()

        # Setup colors
        self._init_colors()
        self._set_color(Level.normal, window=self.screen)

        # Edit terminal settings
        curses.noecho()  # Do not echo key presses
        curses.cbreak()  # React to keys instantly without waiting for the Enter key
        self._hide_cursor()

        # Modify curses behavior
        self.screen.keypad(True)  # Automatically interpret special key presses

        # Clear and refresh the screen and window
        self.screen.clear()
        self.screen.refresh()

        # The minimum number of lines and columns necessary for this program to function
        self.min_lines = 12
        self.min_cols = 44
        self.max_border_lines = 3
        self.max_border_cols = 10
        if curses.LINES < self.min_lines or curses.COLS < self.min_cols:
            self.cleanup()
            logger.error(
                "Min dim: " + str(self.min_lines) + "x" + str(self.min_cols)
            )
            return

        # Get the size and position of the window
        if curses.LINES > ((self.max_border_lines * 2) + self.min_lines):
            self.lines = curses.LINES - (self.max_border_lines * 2)
        else:
            self.lines = self.min_lines

        if curses.COLS > ((self.max_border_cols * 2) + self.min_cols):
            self.cols = curses.COLS - (self.max_border_cols * 2)
        else:
            self.cols = self.min_cols

        self.line_origin = int((curses.LINES - self.lines) / 2)
        self.col_origin = int((curses.COLS - self.cols) / 2)

        # Create the border and window
        self.border = curses.newwin(
            self.lines, self.cols, self.line_origin, self.col_origin
        )
        self.win = curses.newwin(
            self.lines - 2,
            self.cols - 4,
            self.line_origin + 1,
            self.col_origin + 2,
        )

        # Clear and refresh the border and window
        self.border.clear()
        self.border.border()
        self.border.refresh()
        self.win.clear()
        self.win.refresh()

        # Initialization is complete
        self.good = True

    def cleanup(self) -> None:
        self.good = False
        if self.clean == False:
            # Reset terminal settings
            self.screen.keypad(False)
            self._show_cursor()
            curses.echo()  # Echo key presses
            curses.nocbreak()  # Wait for the Enter key before receiving input
            curses.endwin()
            self.clean = True

    def show_help(self) -> None:
        self.win.clear()
        self.win.addstr(
            "  down:  j / DOWN_ARROW\n"
            "    up:  k / UP_ARROW\n"
            "cancel:  q\n"
            "select:  ; / ENTER"
        )
        self.win.refresh()
        self.win.getkey()

    def select(
        self,
        prompt: str,
        items: List[str],
        headings: Optional[str] = None,
        cursor_index: int = 0,
        validator: Callable[[str], bool] = Field.default_validator,
    ) -> Optional[int]:
        if len(items) <= 0:
            logger.error("Not enough items given to select from")
            return None

        while True:
            try:
                self.win.clear()

                self.win.addstr(prompt + "\n\n")
                if headings:
                    self.win.addstr("     " + headings + "\n")

                for this_index in range(len(items)):
                    item = items[this_index]
                    if type(item) is not str:
                        logger.error(
                            "The given item is not a string:"
                            "\n\ttype: "
                            + str(type(item))
                            + "\n\titem: "
                            + str(item)
                        )
                        return None

                    if cursor_index == this_index:
                        self.win.addstr("===> ")
                    else:
                        self.win.addstr("     ")

                    self.win.addstr(item + "\n")

                self.win.addstr("\n")
                logger.show_all_as_curses(self._set_color, self.win.addstr)

                self.win.refresh()

                key = self.screen.getkey()

                if key == "j" or key == "KEY_DOWN":
                    cursor_index += 1
                elif key == "k" or key == "KEY_UP":
                    cursor_index -= 1
                elif key == "q":
                    return None
                elif key == ";" or key == "\n":
                    if validator(items[cursor_index]):
                        return cursor_index
                    else:
                        return None
                else:
                    self.show_help()
                    continue

                cursor_index = max(cursor_index, 0)
                cursor_index = min(cursor_index, len(items) - 1)
            except curses.error:
                pass

    def input(
        self,
        field: Field,
        prompt: str,
    ) -> Field:
        response = field.get_str()

        while True:
            try:
                self.win.clear()
                self.win.addstr(prompt + "\n\n: " + response)
                self.win.refresh()

                self._show_cursor()
                key = self.screen.getkey()
                self._hide_cursor()

                if key == "\n":
                    field.set(response)
                    return field
                else:
                    if len(key) == 1:
                        response += key

                if key == "KEY_BACKSPACE":
                    response = response[:-1]

            except curses.error:
                pass

    def get_device(self, min_bytes: int) -> Optional[str]:
        devices = get(
            "lsblk", "--nodeps", "--output", "path,size,rm,ro,pttype,ptuuid"
        )
        if not devices:
            logger.error("Failed to get device information from lsblk")
            return None

        devices = str(devices).splitlines()
        if len(devices) <= 1:
            logger.error("Not enough devices listed")
            return None

        device_headings = devices[0]
        devices = devices[1:]

        def interactive_device_validator(dev_info: str) -> bool:
            dev_info_list = dev_info.split()
            if len(dev_info_list) <= 0:
                logger.error("Missing path field")
                return False

            dev_path = dev_info_list[0]

            if not is_device_valid(dev_path, min_bytes):
                logger.error(
                    "The selected device does not meet the minimum requirements for installation"
                )
                return False

            if not device_lacks_partitions(dev_path):
                selection_index = self.select(
                    "The selected device already contains partitions!\n\n"
                    "Are you sure you want to format this device?",
                    [
                        "No. Select a different device.",
                        "Yes. Permanently delete all data on " + dev_path + ".",
                    ],
                )
                return selection_index == 1

            return True

        selection_index = self.select(
            "Select the device to format for installation:",
            devices,
            headings=device_headings,
            validator=interactive_device_validator,
        )
        if selection_index is None:
            logger.error("Failed to select a device")
            return None

        device_info = devices[selection_index].split()
        if len(device_info) <= 0:
            logger.error("Missing path for device: " + devices[selection_index])
            return None

        return device_info[0]

    def get_time_zone(self) -> Optional[str]:
        timezones_str = get("timedatectl", "list-timezones", "--no-pager")
        if not timezones_str:
            logger.error("Failed to get the list of timezones from timedatectl")
            return None

        timezones_list = timezones_str.splitlines()

        selection_index = self.select(
            "Select the new timezone:", timezones_list
        )
        if selection_index is None:
            logger.error("Failed to select a timezone")
            return None

        return timezones_list[selection_index]


def interactive_conf(profile: Profile) -> Optional[Profile]:
    # Setup the interactive GUI
    app = CursesApp()
    if not app.good:
        return None

    cursor_index: Optional[int] = 0
    error: Optional[str] = None

    while True:
        cursor_index = app.select(
            "Select a field to change before installation:",
            [
                " network install  ->  " + profile.network_install.get_str(),
                "min device bytes  ->  " + profile.min_device_bytes.get_str(),
                "          device  ->  " + profile.device.get_str(),
                "      boot label  ->  " + profile.boot_label.get_str(),
                "       time zone  ->  " + profile.time_zone.get_str(),
                "        hostname  ->  " + profile.hostname.get_str(),
                "   root password  ->  " + profile.root_password.get_str(),
                "        username  ->  " + profile.username.get_str(),
                "   user password  ->  " + profile.user_password.get_str(),
                "      sudo group  ->  " + profile.sudo_group.get_str(),
                "         restart  ->  " + profile.restart.get_str() + "\n",
                "Begin Installation",
            ],
            cursor_index=cursor_index,
        )
        if cursor_index is None:
            return None

        if cursor_index == 0:  # network install
            selection_index = app.select(
                "Enable network installation mode?\n\n"
                "Note: Check the configuration at /etc/pacman.conf before changing this setting.",
                [
                    "No. Install packages from an offline repository.",
                    "Yes. Download and install packages from remote repositories.",
                ],
            )
            if selection_index is not None:
                profile.network_install.set(bool(selection_index))
        elif cursor_index == 1:  # min device bytes
            profile.min_device_bytes = app.input(
                profile.min_device_bytes,
                "Enter the minimum number of bytes for a device:",
            )
        elif cursor_index == 2:  # device
            profile.device.set(app.get_device(profile.min_device_bytes.get()))
        elif cursor_index == 3:  # boot label
            profile.boot_label = app.input(
                profile.boot_label, "Enter the new boot label:"
            )
        elif cursor_index == 4:  # time zone
            new_time_zone = app.get_time_zone()
            if new_time_zone:
                profile.time_zone.set(new_time_zone)
        elif cursor_index == 5:  # hostname
            profile.hostname = app.input(
                profile.hostname, "Enter the new hostname:"
            )
        elif cursor_index == 6:  # root password
            profile.root_password = app.input(
                profile.root_password,
                "Enter the new password for root:",
            )
        elif cursor_index == 7:  # username
            profile.username = app.input(
                profile.username,
                "Enter the new name for the user:",
            )
        elif cursor_index == 8:  # user password
            profile.user_password = app.input(
                profile.user_password,
                "Enter the new password for the user:",
            )
        elif cursor_index == 9:  # sudo group
            profile.sudo_group = app.input(
                profile.sudo_group,
                "Enter the new name for the sudo group:",
            )
        elif cursor_index == 10:  # restart
            selection_index = app.select(
                "Enable restart after installation?",
                [
                    "No. Do not restart once installation is complete.",
                    "Yes. Restart once installation is complete.",
                ],
            )
            if selection_index is not None:
                profile.restart.set(bool(selection_index))
        elif cursor_index == 11:  # Begin Installation
            if profile.device.get() is not None:
                break
            profile.device.set(app.get_device(profile.min_device_bytes.get()))
            if profile.device.get() is not None:
                break

    # All necessary information has been collected. Installation may now begin.
    app.cleanup()

    # Attempt to clear the screen after field selection is complete.
    run("clear", quiet=False)  # Do nothing if this fails

    return profile


def main() -> bool:
    # Setup signal handlers.
    signal(SIGINT, lambda c, _: show_errors_and_quit(status=False))
    signal(SIGTERM, lambda c, _: show_errors_and_quit(status=False))

    # Define the help message and arguments.
    arg_parser = ArgumentParser(
        prog="auto_moos",
        description="This script uses an existing MOOS installation to install MOOS on a device.",
    )
    arg_parser.add_argument(
        "-g",
        "--generate-conf",
        dest="generate_conf",
        help="generate an example package list and profile and exit",
        action="store_true",
    )
    arg_parser.add_argument(
        "-c",
        "--conf-dir",
        dest="conf_dir",
        help="set the path to the directory containing the package list and profile",
        action="store",
    )
    arg_parser.add_argument(
        "-l",
        "--log-file",
        dest="log_file",
        help="set the path to the log file",
        action="store",
    )
    arg_parser.add_argument(
        "-n",
        "--non-interactive",
        dest="non_interactive",
        help="run this script without a GUI",
        action="store_true",
    )

    # Parse command line arguments.
    args: Namespace = arg_parser.parse_args()

    # Declare the default package list.
    packages: List[str] = ["moos"]

    # Declare the default profile.
    profile = Profile()

    # Determine whether this program is running in interactive mode or script mode.
    interactive: bool = not args.non_interactive

    # Set the path to the configuration directory.
    if args.conf_dir:
        conf_dir = make_absolute(args.conf_dir)
    else:
        conf_dir = home_dir + "/.auto_moos"

    # Enable writing to the log file.
    if args.log_file:
        log_file_path = make_absolute(args.log_file)
    else:
        log_file_path = home_dir + "/.auto_moos_log"
    if not logger.set_log_file(log_file_path):
        logger.error("Failed to open the log file at " + log_file_path)
        return False

    package_list_path = conf_dir + "/packages"
    profile_path = conf_dir + "/profile.json"

    if args.generate_conf:
        # Ensure that this operation does not overwrite existing files
        if os.path.exists(package_list_path):
            logger.error(
                "A package list already exists at " + package_list_path
            )
            return False

        if os.path.exists(profile_path):
            logger.error("A profile already exists at " + profile_path)
            return False

        # Make the configuration directory if it does not already exist
        if not os.path.exists(conf_dir):
            os.makedirs(conf_dir)

        # Generate example packages
        if not dump_packages(packages, package_list_path):
            logger.error(
                "Failed to write an example package list to "
                + package_list_path
            )
            return False

        if not dump_profile(profile, profile_path):
            logger.error(
                "Failed to write an example profile to " + profile_path
            )
            return False

        quit(0)

    # Read the package list
    custom_packages = load_packages(package_list_path)
    if custom_packages:
        packages = custom_packages

    # Read the profile
    custom_profile = load_profile(profile_path)
    if custom_profile:
        profile = custom_profile

    # Attempt to automitically select a device.
    if profile.device.get() is None:
        profile.device.set(get_device(profile.min_device_bytes.get()))

    # If running in interactive mode, prompt the user to verify the profile.
    if interactive:
        profile = interactive_conf(profile)
        if not profile:
            logger.error(
                "An operation failed during interactive profile configuration"
            )
            return False

    # If a device still hasn't been selected, cancel installation.
    if profile.device.get() is None:
        logger.error(
            "Failed to find a suitable device for installation. Manual intervention is required"
        )
        return False

    # Setup debug utilities
    cols, lines = os.get_terminal_size()

    def sep() -> None:
        print("-" * cols)

    def section(msg: str) -> None:
        sep()
        print(msg + "...")

    if get(
        "lsblk",
        "--noheadings",
        "--output",
        "mountpoints",
        profile.device.get_str(),
    ):
        section("Unmounting all partitions on " + profile.device.get_str())
        if not run("bash", "-ec", "umount " + profile.device.get_str() + "?*"):
            logger.error(
                "Failed to unmount all partitions on "
                + profile.device.get_str()
            )
            return False

    section("Formatting and partitioning " + profile.device.get_str())
    boot_part_size_megs: int = 500
    boot_part_num: int = 1
    root_part_num: int = 2
    if not run(
        "bash",
        "-ec",
        "("
        "    echo g  ;"  # new GPT partition table
        "    echo n  ;"  # new EFI partition
        "    echo " + str(boot_part_num) + ";"  # EFI partition number
        "    echo    ;"  # start at the first sector
        "    echo +"
        + str(boot_part_size_megs)
        + "M;"  # reserve space for the EFI partition
        "    echo t  ;"  # change EFI partition type
        "    echo 1  ;"  # change partition type to EFI System
        "    echo n  ;"  # new root partition
        "    echo " + str(root_part_num) + ";"  # root partition number
        "    echo    ;"  # start at the end of the EFI partition
        "    echo    ;"  # reserve the rest of the device
        "    echo w  ;"  # write changes
        ") | fdisk " + profile.device.get_str(),
    ):
        logger.error(
            "Failed to format and partition " + profile.device.get_str()
        )
        return False

    section("Creating filesystems on " + profile.device.get_str())
    boot_part = profile.device.get_str() + str(boot_part_num)
    root_part = profile.device.get_str() + str(root_part_num)
    if not run("mkfs.fat", "-F", "32", boot_part):
        logger.error("Failed to create a FAT32 filesystem on " + boot_part)
        return False
    if not run("mkfs.ext4", root_part):
        logger.error("Failed to create an EXT4 filesystem on " + root_part)
        return False

    section("Mounting filesystems")
    root_mount = "/mnt"
    boot_mount = "/mnt/boot"
    if not run("mount", "--mkdir", root_part, root_mount):
        logger.error("Failed to mount " + root_part + " to " + root_mount)
        return False
    if not run("mount", "--mkdir", boot_part, boot_mount):
        logger.error("Failed to mount " + boot_part + " to " + boot_mount)
        return False

    section("Syncing package databases")
    if profile.network_install.get():
        if not run(
            "pacman", "-Sy", "--noconfirm", "archlinux-keyring", quiet=False
        ):
            logger.error("Failed to sync package databases")
            return False
    else:
        if not run("pacman", "-Sy", quiet=False):
            logger.error("Failed to sync package databases")
            return False

    section("Installing packages with pacstrap")
    if not run("pacstrap", "-K", root_mount, *packages, quiet=False):
        logger.error("Failed to install essential packages")
        return False

    section("Generating fstab")
    fstab_data = get("genfstab", "-U", root_mount)
    if not fstab_data:
        logger.error("Failed to generate fstab")
        return False
    if not write(root_mount + "/etc/fstab", "w", fstab_data):
        logger.error("Failed to write to " + root_mount + "/etc/fstab")
        return False

    section("Copying this script to the root partition")
    if not copy(__file__, root_mount + "/auto_moos.py"):
        logger.error("Failed to copy this script to " + root_mount + "/root")
        return False

    section("Changing root to " + root_mount)
    if not run(
        "arch-chroot",
        root_mount,
        "python",
        "-Bc",
        "from auto_moos import show_errors_and_quit, post_pacstrap_setup\n"
        "\n"
        "show_errors_and_quit(\n"
        "    post_pacstrap_setup(\n"
        "        profile_dict=" + str(profile.to_dict()) + ",\n"
        "        boot_part='" + boot_part + "',\n"
        "    )\n"
        ")",
        quiet=False,
    ):
        logger.error("Failed operation while root was changed to " + root_mount)
        return False

    section("Removing this script from the root partition")
    remove(root_mount + "/auto_moos.py")  # Do nothing if this fails

    section("Unmounting all partitions on " + profile.device.get_str())
    if not run("bash", "-ec", "umount " + profile.device.get_str() + "?*"):
        logger.error(
            "Failed to unmount all partitions on " + profile.device.get_str()
        )
        return False

    logger.success("Installation complete!")

    sep()
    print("Messages accumulated during installation: ")
    logger.show_all_as_ansi()

    restart_timeout: int = 10
    if profile.restart:
        sep()
        print("All logs will be stored to " + log_file_path + ".")
        print("Type CTRL-C to cancel the restart.")
        for i in range(restart_timeout):
            sleep(1)
            print("Restarting in " + str(restart_timeout - i) + "...")

        run("shutdown", "-r", "now")

    return True


def show_errors_and_quit(status: bool) -> None:
    logger.show_all_as_ansi()
    quit(not status)


def post_pacstrap_setup(
    profile_dict: dict,
    boot_part: str,
) -> bool:
    profile = dict_to_profile(profile_dict)

    # Setup debug utilities
    cols, lines = os.get_terminal_size()

    def sep() -> None:
        print("-" * cols)

    def section(msg: str) -> None:
        sep()
        print(msg + "...")

    section("Installing the boot loader")
    if not run(
        "auto_limine", boot_part, "--label", profile.boot_label.get_str()
    ):
        logger.error("Failed to install the boot loader (Limine)")
        return False

    section("Setting the root password")
    if not run("chpasswd", input="root:" + profile.root_password.get_str()):
        logger.error("Failed to set the root password")
        # Continue installation even if this fails

    section("Creating the sudo group")
    if run("groupadd", "--force", profile.sudo_group.get_str()):
        section("Creating the user")
        if run(
            "useradd",
            "--create-home",
            "--user-group",
            "--groups",
            profile.sudo_group.get_str(),
            profile.username.get_str(),
        ):
            section("Setting the user password")
            if not run(
                "chpasswd",
                input=profile.username.get_str()
                + ":"
                + profile.user_password.get_str(),
            ):
                logger.error("Failed to set the user password")
                # Continue installation even if this fails
        else:
            logger.error("Failed to create the user")
            # Continue installation even if this fails

        section("Providing root privileges to all members of the sudo group")
        if not write(
            "/etc/sudoers",
            "a",
            "\n"
            "## Allow members of group "
            + profile.sudo_group.get_str()
            + " to execute any command\n%"
            + profile.sudo_group.get_str()
            + " ALL=(ALL:ALL) ALL\n",
        ):
            logger.error(
                "Failed to provide root privileges to all members of the sudo group"
            )
            # Continue installation even if this fails
    else:
        logger.error("Failed to create the sudo group")
        # Continue installation even if this fails

    section("Setting time zone: " + profile.time_zone.get_str())
    if not run(
        "ln",
        "-sf",
        "/usr/share/zoneinfo/" + profile.time_zone.get_str(),
        "/etc/localtime",
    ):
        logger.error("Failed to set time zone: " + profile.time_zone.get_str())
        # Continue installation even if this fails

    section("Syncronizing the hardware clock with the system clock")
    if not run("hwclock", "--systohc"):
        logger.error("Failed to set the hardware clock")
        # Continue installation even if this fails

    section("Enabling NTP time synchronization")
    if not run("systemctl", "enable", "systemd-timesyncd.service"):
        logger.error("Failed to enable the systemd-timesyncd service")
        # Continue installation even if this fails

    section("Adding locales to /etc/locale.gen")
    if write("/etc/locale.gen", "a", "en_US.UTF-8 UTF-8"):
        section("Generating locales")
        if run("locale-gen"):
            if not write("/etc/locale.conf", "w", "LANG=en_US.UTF-8"):
                logger.error("Failed to write locale to /etc/locale.conf")
                # Continue installation even if this fails
        else:
            logger.error("Failed to generate locales")
            # Continue installation even if this fails
    else:
        logger.error("Failed to edit /etc/locale.gen, cannot generate locales")
        # Continue installation even if this fails

    section("Setting hostname")
    if not write("/etc/hostname", "w", profile.hostname.get_str()):
        logger.error("Failed to write hostname to /etc/hostname")
        # Continue installation even if this fails

    section("Enabling automatic network configuration")
    if not run("systemctl", "enable", "NetworkManager"):
        logger.error("Failed to enable the NetworkManager service")
        # Continue installation even if this fails

    section("Enabling bluetooth")
    if not run("systemctl", "enable", "bluetooth.service"):
        logger.error("Failed to enable bluetooth service")
        # Continue installation even if this fails

    section("Enabling the firewall")
    if not run("systemctl", "enable", "ufw.service"):
        logger.error("Failed to enable the ufw service")
        # Continue installation even if this fails

    section("Enabling ssh")
    if not run("systemctl", "enable", "sshd.service"):
        logger.error("Failed to enable the sshd service")
        # Continue installation even if this fails

    # section("Enabling libvirtd")
    # if not run("systemctl", "enable", "libvirtd.service"):
    #     logger.error("Failed to enable the libvirtd service")
    #     # Continue installation even if this fails

    return True


if __name__ == "__main__":
    show_errors_and_quit(main())
