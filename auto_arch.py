#!/usr/bin/python

"""Automatically installs Arch Linux"""

from argparse import Action, ArgumentParser, Namespace
import atexit
import curses
import json
import os
import shutil
from signal import signal, SIGINT, SIGTERM
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple


# General utilities
# ----------------------------------------------------------------------------


def blue(msg: str) -> str:
    return "\033[1;34m" + msg + "\033[0m"


def yellow(msg: str) -> str:
    return "\033[1;33m" + msg + "\033[0m"


def green(msg: str) -> str:
    return "\033[1;32m" + msg + "\033[0m"


def red(msg: str) -> str:
    return "\033[1;31m" + msg + "\033[0m"


def warning(msg: str) -> None:
    print(yellow("Warning: " + msg + "."))


def error(msg: str) -> None:
    print(red("Error: " + msg + "."))


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
    try:
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    except os.error:
        return False
    return True


def remove(path: str) -> bool:
    try:
        shutil.rmtree(path)
    except os.error:
        return False
    return True


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
        # Failed to get device information from lsblk
        return None

    return str(devices).splitlines()


def is_device_valid(
    dev_path: str, min_dev_size: int
) -> Tuple[bool, Optional[str]]:
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
        return (
            False,
            "Failed to get device information from lsblk for device: "
            + dev_path,
        )

    dev_info = str(dev_info).split()
    if len(dev_info) <= 1:
        return False, "Not enough fields given by lsblk for device: " + dev_path

    if dev_path != dev_info[0]:
        return (
            False,
            "Wrong device given by lsblk."
            + "\nExpected: "
            + dev_path
            + "\n   Given:"
            + dev_info[0],
        )

    dev_size = dev_info[1]

    if int(dev_size) < min_dev_size:
        return (
            False,
            "Not enough space on device: "
            + dev_path
            + "\n   Minimum required: "
            + str(min_dev_size)
            + " bytes"
            + "\nAvailable on device: "
            + dev_size
            + " bytes",
        )

    return True, None


def device_lacks_partitions(dev_path: str) -> Optional[bool]:
    parts = get("lsblk", "--noheadings", "--output", "path", dev_path)
    if not parts:
        # Failed to list partitions on device: {dev}
        return None

    parts = str(parts).splitlines()[1:]
    if len(parts) > 0:
        # Partitions found on device: {dev}
        return False

    return True


def get_device(min_size: int) -> Optional[str]:
    """Select the device to format for installation"""

    devices = list_all_devices()
    if not devices:
        # Failed to list devices
        return None

    for dev_path in devices:
        if not is_device_valid(dev_path, min_size):
            # The minimum requirements for installation were not met.
            continue

        if not device_lacks_partitions(dev_path):
            # Formatting a device that already contains partitions results in irreversible data loss!
            # Never format a device with existing partitions without explicit permission from the user.
            continue

        return dev_path

    return None


def is_uefi_bootable() -> bool:
    """Determine whether this system is UEFI bootable or not"""

    return os.path.exists("/sys/firmware/efi/fw_platform_size")


class CursesApp:
    def __init__(self) -> None:
        # Beginning application initialization
        self.good = False

        # Ensure that the terminal is restored to its original state
        self.clean = False
        atexit.register(self.cleanup)

        # Identify the terminal type and send required setup codes (if any)
        self.screen = curses.initscr()

        # Setup colors
        curses.start_color()
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)
        self.screen.bkgdset(" ", curses.color_pair(1) | curses.A_BOLD)

        # Edit terminal settings
        curses.curs_set(0)  # Hide the cursor
        curses.noecho()  # Do not echo key presses
        curses.cbreak()  # React to keys instantly without waiting for the Enter key

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
            error(
                "Error: Min dim: "
                + str(self.min_lines)
                + "x"
                + str(self.min_cols)
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
            curses.nocbreak()
            curses.echo()
            curses.curs_set(1)
            curses.endwin()
            self.clean = True

    # def _highlight(self) -> None:
    #     self.win.bkgdset(curses.color_pair(2) | curses.A_BOLD)

    # def _unhighlight(self) -> None:
    #     self.win.bkgdset(curses.color_pair(1) | curses.A_BOLD)

    def show_help(self) -> None:
        self.win.clear()
        self.win.addstr(
            "  down:  j / DOWN_ARROW\n"
            "    up:  k / UP_ARROW\n"
            "cancel:  q\n"
            "select:      ENTER"
        )
        self.win.refresh()
        self.win.getkey()

    def select(
        self,
        prompt: str,
        items: List[str],
        headings: Optional[str] = None,
        validator: Callable[[str], Tuple[bool, Optional[str]]] = lambda _: (
            True,
            None,
        ),
    ) -> Tuple[Optional[str], Optional[str]]:
        if len(items) <= 0:
            return None, "Not enough items given to select from"

        cursor_i: int = 0
        error: Optional[str] = None

        while True:
            try:
                self.win.clear()

                self.win.addstr(prompt + "\n\n")

                if headings:
                    self.win.addstr("     " + headings + "\n")

                for i in range(len(items)):
                    item = items[i]
                    if type(item) is not str:
                        return None, "Invalid type (" + str(
                            type(item)
                        ) + ") for item: " + str(item)

                    if cursor_i == i:
                        self.win.addstr("===> ")
                    else:
                        self.win.addstr("     ")

                    self.win.addstr(item + "\n")

                if error:
                    self.win.addstr("\nError: " + error)

                self.win.refresh()

                key = self.screen.getkey()

                if key == "j" or key == "KEY_DOWN":
                    cursor_i += 1
                elif key == "k" or key == "KEY_UP":
                    cursor_i -= 1
                elif key == "q":
                    return None, None
                elif key == "\n":
                    pass
                else:
                    self.show_help()
                    continue

                cursor_i = max(cursor_i, 0)
                cursor_i = min(cursor_i, len(items) - 1)

                if key == "\n":
                    valid, error = validator(items[cursor_i])
                    if valid:
                        return items[cursor_i], None

            except curses.error:
                pass

    def get_device(self, min_bytes: int) -> Optional[str]:
        devices = get(
            "lsblk", "--nodeps", "--output", "path,size,rm,ro,pttype,ptuuid"
        )
        if not devices:
            # Failed to list devices
            return None

        devices = str(devices).splitlines()
        if len(devices) <= 1:
            # Not enough devices listed
            return None

        device_headings = devices[0]
        devices = devices[1:]

        def interactive_device_validator(
            dev_info: str,
        ) -> Tuple[bool, Optional[str]]:
            dev_info_list = dev_info.split()
            if len(dev_info_list) <= 0:
                return False, "Missing path field."

            dev_path = dev_info_list[0]

            dev_valid, error = is_device_valid(dev_path, min_bytes)
            if not dev_valid:
                return False, error

            if not device_lacks_partitions(dev_path):
                selection, error = self.select(
                    "The selected device already contains partitions!\n\n"
                    "Are you sure you want to format this device?",
                    [
                        "No. Select a different device.",
                        "Yes. Permanently delete all data on " + dev_path + ".",
                    ],
                )
                if not selection:
                    if error:
                        return False, error
                    return False, None

                return selection.startswith("Yes"), None

            return True, None

        device_info, error = self.select(
            "Select the device to format for installation:",
            devices,
            headings=device_headings,
            validator=interactive_device_validator,
        )
        if not device_info:
            # Failed to select a device
            return None

        device_info = str(device_info).split()
        if len(device_info) <= 0:
            # Missing path field
            return None

        return device_info[0]


def interactive_conf(profile: dict) -> bool:
    # Setup the interactive GUI
    app = CursesApp()
    if not app.good:
        return False

    while True:
        selection, error_msg = app.select(
            "Select a field to change before installation:",
            [
                " network install  ->  " + str(profile["network_install"]),
                "min device bytes  ->  " + str(profile["min_device_bytes"]),
                "          device  ->  "
                + (profile["device"] if profile["device"] else ""),
                "      boot label  ->  " + profile["boot_label"],
                "       time zone  ->  " + profile["time_zone"],
                "        hostname  ->  " + profile["hostname"],
                "   root password  ->  " + profile["root_password"],
                "            user  ->  " + profile["user"],
                "   user password  ->  " + profile["user_password"],
                "      sudo group  ->  " + profile["sudo_group"] + "\n",
                "Begin Installation",
            ],
        )
        if not selection:
            return False

        selection = str(selection).strip()
        if selection.startswith("device"):
            profile["device"] = app.get_device(profile["min_device_bytes"])
        elif selection.startswith("Begin"):
            if profile["device"]:
                break
            profile["device"] = app.get_device(profile["min_device_bytes"])
            if device:
                break

    # All necessary information has been collected. Installation may now begin.
    app.cleanup()

    # Attempt to clear the screen after field selection is complete.
    run("clear", quiet=False)  # Do nothing if this fails

    return True


if __name__ == "__main__":
    # Setup signal handlers.
    signal(SIGINT, lambda c, _: quit(1))
    signal(SIGTERM, lambda c, _: quit(1))

    # Define the help message and arguments.
    arg_parser = ArgumentParser(
        prog="auto_arch",
        description="This script uses an existing Arch Linux installation to install Arch Linux on a device.",
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
        action="store_true",
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

    # Determine whether this program is running in interactive mode or script mode
    interactive: bool = not args.non_interactive

    # Example package list and profile
    packages: List[str] = [
        "base",
        "base-devel",
        "linux",
        "linux-firmware",
        "bash",
        "bash-completion",
        "man-db",
        "man-pages",
        "cgs-limine-auto",
        "less",
        "curl",
        "git",
        "python",
        "ufw",
        "nano",
        "vim",
        "networkmanager",
        "alsa-utils",
        "bluez",
        "bluez-utils",
        "pulseaudio",
        "pulseaudio-alsa",
        "pulseaudio-bluetooth",
    ]
    profile: dict = {
        "network_install": True,
        "min_device_bytes": int(10e9),
        "device": None,
        "boot_label": "Arch Linux",
        "time_zone": "America/Denver",
        "hostname": "arch",
        "root_password": "root",
        "user": "main",
        "user_password": "main",
        "sudo_group": "wheel",
    }

    # Read the configuration files (package list and profile)
    home_dir = os.path.expanduser("~")
    package_list_name = "packages"
    profile_conf_name = "profile.json"
    package_list_default_path = home_dir + "/.auto_arch/" + package_list_name
    profile_conf_default_path = home_dir + "/.auto_arch/" + profile_conf_name

    def get_packages(packages: List[str], path: str) -> bool:
        try:
            with open(path, "r") as packages_file:
                packages = [line.strip() for line in packages_file]
            return True
        except:
            return False

    def get_profile(profile: dict, path: str) -> bool:
        try:
            with open(path, "r") as profile_file:
                profile = json.load(profile_file)
            return True
        except:
            return False

    if args.conf_dir:
        package_list_path = args.conf_dir + "/" + package_list_name
        if not get_packages(packages, package_list_path):
            error("Failed to read the package list at " + package_list_path)
            quit(1)
        profile_conf_path = args.conf_dir + "/" + profile_conf_name
        if not get_profile(profile, profile_conf_name):
            error("Failed to read the profile at " + package_list_path)
            quit(1)
    else:
        if not get_packages(packages, package_list_default_path):
            warning("Package list not found at " + package_list_default_path)
            warning("Using the default package list instead")
        if not get_profile(profile, profile_conf_default_path):
            warning("Package profile not found at " + profile_conf_default_path)
            warning("Using the default profile instead")

    if not profile["device"]:
        device = get_device(profile["min_device_bytes"])

    if not interactive:
        if not profile["device"]:
            error(
                "Failed to find a suitable device for installation. Manual intervention is required"
            )
            quit(1)
    else:
        if not interactive_conf(profile):
            error(
                "An operation failed during interactive profile configuration"
            )
            quit(1)

    # Setup debug utilities
    cols, lines = os.get_terminal_size()

    def sep() -> None:
        print("-" * cols)

    def section(msg: str) -> None:
        sep()
        print(msg + "...")

    section("Identifying supported boot modes")
    uefi = is_uefi_bootable()
    if uefi:
        print("This system is UEFI bootable")
    else:
        print("This system is BIOS bootable")

    if get(
        "lsblk", "--noheadings", "--output", "mountpoints", profile["device"]
    ):
        section("Unmounting all partitions on " + profile["device"])
        if not run("bash", "-ec", "umount " + profile["device"] + "?*"):
            error("Failed to unmount all partitions on " + profile["device"])
            quit(1)

    section("Formatting and partitioning " + profile["device"])
    boot_part_size_megs: int = 500
    boot_part_num: int = 1
    root_part_num: int = 2
    if uefi:
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
            ") | fdisk " + profile["device"],
        ):
            error("Failed to format and partition " + profile["device"])
            quit(1)
    else:
        if not run(
            "bash",
            "-ec",
            "("
            "    echo o  ;"  # new MBR partition table
            "    echo n  ;"  # new boot partition (required by limine)
            "    echo p  ;"  # primary partition
            "    echo " + str(boot_part_num) + ";"  # boot partition number
            "    echo    ;"  # start at the first sector
            "    echo +"
            + str(boot_part_size_megs)
            + "M;"  # reserve space for the boot partition
            "    echo a  ;"  # set the bootable flag
            "    echo n  ;"  # new root partition
            "    echo p  ;"  # primary partition
            "    echo " + str(root_part_num) + ";"  # root partiion number
            "    echo    ;"  # start at the end of the boot partition
            "    echo    ;"  # reserve the rest of the device
            "    echo w  ;"  # write changes
            ") | fdisk " + profile["device"],
        ):
            error("Failed to format and partition " + profile["device"])
            quit(1)

    section("Creating filesystems on " + profile["device"])
    boot_part = profile["device"] + str(boot_part_num)
    root_part = profile["device"] + str(root_part_num)
    if not run("mkfs.fat", "-F", "32", boot_part):
        error("Failed to create a FAT32 filesystem on " + boot_part)
        quit(1)
    if not run("mkfs.ext4", root_part):
        error("Failed to create an EXT4 filesystem on " + root_part)
        quit(1)

    section("Mounting filesystems")
    root_mount = "/mnt"
    boot_mount = "/mnt/boot"
    if not run("mount", "--mkdir", root_part, root_mount):
        error("Failed to mount " + root_part + " to " + root_mount)
        quit(1)
    if not run("mount", "--mkdir", boot_part, boot_mount):
        error("Failed to mount " + boot_part + " to " + boot_mount)
        quit(1)

    section("Syncing package databases")
    if profile["network_install"]:
        if not run(
            "pacman", "-Sy", "--noconfirm", "archlinux-keyring", quiet=False
        ):
            error("Failed to sync package databases")
            quit(1)
    else:
        if not run("pacman", "-Sy", quiet=False):
            error("Failed to sync package databases")
            quit(1)

    section("Installing packages with pacstrap")
    if not run("pacstrap", "-K", root_mount, *packages, quiet=False):
        error("Failed to install essential packages")
        quit(1)

    section("Generating fstab")
    fstab_data = get("genfstab", "-U", root_mount)
    if not fstab_data:
        error("Failed to generate fstab")
        quit(1)
    if not write(root_mount + "/etc/fstab", "w", fstab_data):
        error("Failed to write to " + root_mount + "/etc/fstab")
        quit(1)

    section("Copying this script to the root partition")
    if not copy(__file__, root_mount + "/root/auto_arch.py"):
        error("Failed to copy this script to " + root_mount + "/root")
        quit(1)

    section("Changing root to " + root_mount)
    if not run(
        "arch-chroot",
        root_mount,
        "python",
        "-Bc",
        "from auto_arch import post_pacstrap_setup\n"
        "\n"
        "quit(\n"
        "    not post_pacstrap_setup(\n"
        "        boot_part=" + boot_part + ",\n"
        "        profile=" + str(profile) + ",\n"
        "    )\n"
        ")",
    ):
        error("Failed operation while root was changed to " + root_mount)
        quit(1)

    section("Removing this script from the root partition")
    remove(root_mount + "/root/auto_arch.py")  # Do nothing if this fails

    section("Unmounting all partitions on " + profile["device"])
    if not run("bash", "-ec", "umount " + profile["device"] + "?*"):
        error("Failed to unmount all partitions on " + profile["device"])
        quit(1)

    sep()
    print(green("Installation complete!"))

    quit(0)


def post_pacstrap_setup(
    profile: dict,
    boot_part: str,
) -> bool:
    section("Installing the boot loader")
    if not run("auto_limine", boot_part, "--label", profile["boot_label"]):
        error("Failed to install the boot loader (Limine)")
        return False

    section("Setting the root password")
    if not run("chpasswd", input="root:" + profile["root_password"]):
        error("Failed to set the root password")
        # Continue installation even if this fails

    section("Creating the sudo group")
    if run("groupadd", "--force", profile["sudo_group"]):
        section("Creating the user")
        if run(
            "useradd",
            "--create-home",
            "--user-group",
            "--groups",
            profile["sudo_group"],
            profile["user"],
        ):
            section("Setting the user password")
            if not run(
                "chpasswd",
                input=profile["user"] + ":" + profile["user_password"],
            ):
                error("Failed to set the user password")
                # Continue installation even if this fails
        else:
            error("Failed to create the user")
            # Continue installation even if this fails

        section("Providing root privileges to all members of the sudo group")
        if not write(
            "/etc/sudoers",
            "a",
            "\n"
            "## Allow members of group "
            + profile["sudo_group"]
            + " to execute any command\n"
            + profile["sudo_group"]
            + " ALL=(ALL:ALL) ALL\n",
        ):
            error(
                "Failed to provide root privileges to all members of the sudo group"
            )
            # Continue installation even if this fails
    else:
        error("Failed to create the sudo group")
        # Continue installation even if this fails

    section("Setting time zone: " + profile["time_zone"])
    if not run(
        "ln",
        "-sf",
        "/usr/share/zoneinfo/" + profile["time_zone"],
        "/etc/localtime",
    ):
        error("Failed to set time zone: " + profile["time_zone"])
        # Continue installation even if this fails

    section("Syncronizing the hardware clock with the system clock")
    if not run("hwclock", "--systohc"):
        error("Failed to set the hardware clock")
        # Continue installation even if this fails

    section("Syncronizing the hardware clock with the system clock")
    if not run("hwclock", "--systohc"):
        error("Failed to set the hardware clock")
        # Continue installation even if this fails

    section("Enabling NTP time synchronization")
    if not run("systemctl", "enable", "systemd-timesyncd.service"):
        error("Failed to enable the systemd-timesyncd service")
        # Continue installation even if this fails

    section("Adding locales to /etc/locale.gen")
    if write("/etc/locale.gen", "a", "en_US.UTF-8 UTF-8"):
        section("Generating locales")
        if run("locale-gen"):
            if not write("/etc/locale.conf", "w", "LANG=en_US.UTF-8"):
                error("Failed to write locale to /etc/locale.conf")
                # Continue installation even if this fails
        else:
            error("Failed to generate locales")
            # Continue installation even if this fails
    else:
        error("Failed to edit /etc/locale.gen, cannot generate locales")
        # Continue installation even if this fails

    section("Setting hostname")
    if not write("/etc/hostname", "w", profile["hostname"]):
        error("Failed to write hostname to /etc/hostname")
        # Continue installation even if this fails

    section("Enabling automatic network configuration")
    if not run("systemctl", "enable", "NetworkManager"):
        error("Failed to enable the NetworkManager service")
        # Continue installation even if this fails

    section("Enabling bluetooth")
    if not run("systemctl", "enable", "bluetooth.service"):
        error("Failed to enable bluetooth service")
        # Continue installation even if this fails

    section("Enabling the firewall")
    if not run("systemctl", "enable", "ufw.service"):
        error("Failed to enable the ufw service")
        # Continue installation even if this fails

    section("Enabling ssh")
    if not run("systemctl", "enable", "sshd.service"):
        error("Failed to enable the sshd service")
        # Continue installation even if this fails

    # section("Enabling libvirtd")
    # if not run("systemctl", "enable", "libvirtd.service"):
    #     error("Failed to enable the libvirtd service")
    #     # Continue installation even if this fails

    return True
