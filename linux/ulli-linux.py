#!/usr/bin/env python3
"""
Linux-to-Linux Installer
A GUI tool to install a second Linux distribution alongside an existing one.

Supported targets: Linux Mint 22.3, Ubuntu 24.04.4, Kubuntu 24.04.4,
                   Debian Live 13.3.0 KDE, Fedora 43 KDE

Filesystem strategy:
  - btrfs:  Shrink the existing partition and install into new unallocated space

Boot strategy:
  - Optional rEFInd boot manager on a dedicated FAT32 partition with ext4 driver
  - Optional ext4 boot partition (12 GB) or FAT32 boot partition (7 GB)

Requirements:
  pip3 install requests
  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 parted btrfs-progs \
                   grub-common grub2-common unzip
"""

import os, sys

# ─── root helper mode ────────────────────────────────────────────────────────
# When invoked with --root-helper, run as a JSON-RPC subprocess that executes
# privileged commands on behalf of the unprivileged GUI process.
if "--root-helper" in sys.argv:
    import json as _json, subprocess as _sp, os as _os

    ALLOWED_COMMANDS = {
        "parted", "sfdisk", "partprobe", "btrfs", "mkfs.fat", "mkfs.ext4",
        "rsync", "mount", "umount", "findmnt", "df", "blkid",
        "lsblk", "efibootmgr", "grub-mkconfig", "update-grub", "grub2-mkconfig",
        "dd", "wipefs", "sgdisk", "udevadm", "sync", "ntfsresize",
        "resize2fs", "e2fsck", "dumpe2fs", "systemctl", "7z",
        "dmsetup", "swapon", "swapoff", "fuser", "env", "chown",
        "ls", "find", "cp", "rm", "unzip", "reboot",
    }

    ALLOWED_PREFIXES = (
        "/mnt/", "/run/udev/", "/boot/", "/sys/", "/dev/", "/proc/"
    )

    def _validate_request(req):
        """Validate every JSON-RPC request before execution."""
        _t = req.get("type")

        if _t not in ("run", "mkdir", "read", "write", "unlink", "exists"):
            return False, f"Unknown request type: {_t}"

        if _t == "run":
            cmd = req.get("cmd", [])
            if not cmd or cmd[0] not in ALLOWED_COMMANDS:
                return False, f"Command '{cmd[0] if cmd else 'empty'}' not allowed"

            # Block basic dangerous shell patterns
            cmd_str = " ".join(cmd)
            if any(p in cmd_str for p in (">>", "<<", "|", "chmod 777", "/etc/shadow", "/etc/sudoers")):
                return False, "Dangerous pattern detected in command"

        elif _t in ("mkdir", "read", "write", "unlink", "exists"):
            path = req.get("path", "")
            real_path = _os.path.realpath(path)  # Resolve ../ to prevent traversal

            if not real_path.startswith(ALLOWED_PREFIXES):
                return False, f"Path '{path}' not in allowed prefixes"

        return True, "OK"

    for _line in sys.stdin:
        try:
            _req = _json.loads(_line)
        except _json.JSONDecodeError:
            continue

        _valid, _msg = _validate_request(_req)
        if not _valid:
            _resp = {"rc": 1, "err": f"Security validation failed: {_msg}"}
            sys.stdout.write(_json.dumps(_resp) + "\n")
            sys.stdout.flush()
            continue

        _t = _req.get("type")
        try:
            if _t == "run":
                _r = _sp.run(_req["cmd"], capture_output=True, text=True,
                             input=_req.get("input"))
                _resp = {"rc": _r.returncode,
                         "out": _r.stdout or "", "err": _r.stderr or ""}
            elif _t == "mkdir":
                _os.makedirs(_req["path"], exist_ok=True)
                _resp = {"rc": 0}
            elif _t == "read":
                with open(_req["path"], "r", errors="replace") as _f:
                    _resp = {"rc": 0, "content": _f.read()}
            elif _t == "write":
                with open(_req["path"], "w") as _f:
                    _f.write(_req["content"])
                _resp = {"rc": 0}
            elif _t == "unlink":
                _os.unlink(_req["path"])
                _resp = {"rc": 0}
            elif _t == "exists":
                _resp = {"rc": 0, "exists": _os.path.exists(_req["path"])}
        except Exception as _e:
            _resp = {"rc": 1, "err": str(_e)}

        sys.stdout.write(_json.dumps(_resp) + "\n")
        sys.stdout.flush()
    sys.exit(0)

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Gdk, GLib, Pango, Vte

import subprocess, threading, hashlib, shutil, json, time, signal, re
import urllib.request, urllib.error
from pathlib import Path
from datetime import datetime

_is_root = os.geteuid() == 0

# ─── constants ───────────────────────────────────────────────────────────────

MIN_BOOT_GB_EXT4  = 12
MIN_BOOT_GB_FAT32 = 7
MIN_LINUX_GB      = 20
MiB               = 1_048_576          # 1 MiB in bytes  (2^20)
GiB               = 1_073_741_824      # 1 GiB in bytes  (2^30)

REFIND_URL      = "https://sourceforge.net/projects/refind/files/0.14.2/refind-bin-0.14.2.zip/download"
REFIND_FILENAME = "refind-bin-0.14.2.zip"
REFIND_MIB      = 100          # 100 MiB FAT32 partition for rEFInd

DISTROS = {
    "mint": {
        "label":    "Linux Mint 22.3 \"Zena\" – Cinnamon  (~2.9 GB)",
        "filename": "linuxmint-22.3-cinnamon-64bit.iso",
        "sha256":   "a081ab202cfda17f6924128dbd2de8b63518ac0531bcfe3f1a1b88097c459bd4",
        "size_gb":  2.9,
        "mirrors": [
            "https://mirrors.kernel.org/linuxmint/stable/22.3/linuxmint-22.3-cinnamon-64bit.iso",
            "https://mirror.csclub.uwaterloo.ca/linuxmint/stable/22.3/linuxmint-22.3-cinnamon-64bit.iso",
            "https://mirrors.seas.harvard.edu/linuxmint/stable/22.3/linuxmint-22.3-cinnamon-64bit.iso",
        ],
        "live_path": "casper/vmlinuz",
    },
    "cachyos": {
        "label":    "CachyOS Desktop  (~2.6 GB)",
        "filename": "cachyos-desktop-linux-260308.iso",
        "sha256":   "69f1ffbded158d4d95e6567e994b1813d0d040d323742aef9f489a0b71ad1d29",
        "size_gb":  2.6,
        "mirrors": [
            "https://cdn77.cachyos.org/ISO/desktop/260308/cachyos-desktop-linux-260308.iso",
        ],
        "live_path": "boot/vmlinuz-linux-cachyos",
        "hybrid": True,
    },
    "ubuntu": {
        "label":    "Ubuntu 24.04.4 LTS – GNOME  (~5.9 GB)",
        "filename": "ubuntu-24.04.4-desktop-amd64.iso",
        "sha256":   "3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e",
        "size_gb":  5.9,
        "mirrors": [
            "https://releases.ubuntu.com/24.04.4/ubuntu-24.04.4-desktop-amd64.iso",
            "https://mirror.cs.uchicago.edu/ubuntu-releases/24.04.4/ubuntu-24.04.4-desktop-amd64.iso",
            "https://mirrors.mit.edu/ubuntu-releases/24.04.4/ubuntu-24.04.4-desktop-amd64.iso",
        ],
        "live_path": "casper/vmlinuz",
    },
    "kubuntu": {
        "label":    "Kubuntu 24.04.4 LTS – KDE Plasma  (~4.2 GB)",
        "filename": "kubuntu-24.04.4-desktop-amd64.iso",
        "sha256":   "02cda2568cb96c090b0438a31a7d2e7b07357fde16217c215e7c3f45263bcc49",
        "size_gb":  4.2,
        "mirrors": [
            "https://cdimage.ubuntu.com/kubuntu/releases/24.04.4/release/kubuntu-24.04.4-desktop-amd64.iso",
            "https://ftpmirror.your.org/pub/ubuntu/cdimage/kubuntu/releases/24.04/release/kubuntu-24.04.4-desktop-amd64.iso",
        ],
        "live_path": "casper/vmlinuz",
    },
    "debian": {
        "label":    "Debian Live 13.3.0 – KDE  (~3.2 GB)",
        "filename": "debian-live-13.3.0-amd64-kde.iso",
        "sha256":   "6a162340bca02edf67e159c847cd605618a77d50bf82088ee514f83369e43b89",
        "size_gb":  3.2,
        "mirrors": [
            "https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/debian-live-13.3.0-amd64-kde.iso",
            "https://mirrors.kernel.org/debian-cd/current-live/amd64/iso-hybrid/debian-live-13.3.0-amd64-kde.iso",
        ],
        "live_path": "live/vmlinuz",
    },
    "fedora": {
        "label":    "Fedora 43 – KDE Plasma Desktop  (~3.0 GB)",
        "filename": "Fedora-KDE-Desktop-Live-43-1.6.x86_64.iso",
        "sha256":   "181fe3e265fb5850c929f5afb7bdca91bb433b570ef39ece4a7076187435fdab",
        "size_gb":  3.0,
        "mirrors": [
            "https://d2lzkl7pfhq30w.cloudfront.net/pub/fedora/linux/releases/43/KDE/x86_64/iso/Fedora-KDE-Desktop-Live-43-1.6.x86_64.iso",
            "https://mirror.web-ster.com/fedora/releases/43/KDE/x86_64/iso/Fedora-KDE-Desktop-Live-43-1.6.x86_64.iso",
        ],
        "live_path": "LiveOS/squashfs.img",
        "hybrid": True,
    },
}

# ─── unit conversion helpers ─────────────────────────────────────────────────
#
# User-facing sizes are in decimal GB (base-10) for familiarity.
# All internal arithmetic uses MiB (base-2) since parted, sfdisk,
# and the kernel partition table all work in MiB-aligned units.
# btrfs resize targets are derived from MiB values (mib * MiB) so that
# the filesystem and partition table always agree on the exact byte count.

def gb_to_mib(gb):
    """Convert user-facing decimal GB to MiB (rounded DOWN to avoid over-allocation)."""
    return int(gb * 1_000_000_000 / MiB)

def mib_to_bytes(mib):
    """Convert MiB to exact byte count. Used for btrfs/ext resize targets."""
    return mib * MiB

def mib_to_display_gb(mib):
    """Convert MiB to a human-friendly decimal GB string value."""
    return round(mib * MiB / 1_000_000_000, 2)

def bytes_to_display_gb(b):
    """Convert bytes to human-friendly decimal GB for display."""
    return round(b / 1_000_000_000, 2)

def bytes_to_mib(b):
    """Convert bytes to MiB (rounded DOWN)."""
    return int(b / MiB)

# ─── privileged helper ────────────────────────────────────────────────────────

class PrivilegedHelper:
    """Persistent root subprocess for running privileged operations.

    Spawned once via ``pkexec`` when the first privileged call is made.
    The GUI stays unprivileged; only disk operations cross the privilege
    boundary through this JSON-over-pipes channel.
    """
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def running(cls):
        return cls._instance is not None

    def __init__(self):
        script = os.path.abspath(sys.argv[0])
        try:
            self.proc = subprocess.Popen(
                ["pkexec", sys.executable, script, "--root-helper"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        except FileNotFoundError:
            raise RuntimeError(
                "pkexec not found. Install polkit:\n"
                "  Debian/Ubuntu:  sudo apt install policykit-1\n"
                "  Fedora/RHEL:    sudo dnf install polkit\n"
                "  Arch/CachyOS:   sudo pacman -S polkit")
        if self.proc.poll() is not None:
            raise RuntimeError(
                "Authentication cancelled or failed. "
                "Root privileges are required for disk operations.")

    def _send(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(
                "Privileged helper died unexpectedly. "
                "Authentication may have been cancelled.")
        return json.loads(line)

    def run(self, cmd, input_data=None):
        req = {"type": "run", "cmd": cmd}
        if input_data is not None:
            req["input"] = input_data
        resp = self._send(req)
        return resp["rc"], (resp.get("out") or "").strip(), (resp.get("err") or "").strip()

    def makedirs(self, path):
        self._send({"type": "mkdir", "path": str(path)})

    def read_file(self, path):
        resp = self._send({"type": "read", "path": str(path)})
        return resp.get("content", "")

    def write_file(self, path, content):
        self._send({"type": "write", "path": str(path), "content": content})

    def unlink(self, path):
        self._send({"type": "unlink", "path": str(path)})

    def exists(self, path):
        resp = self._send({"type": "exists", "path": str(path)})
        return resp.get("exists", False)

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait()

# ─── helpers ─────────────────────────────────────────────────────────────────

def run(cmd, **kw):
    if not _is_root:
        helper = PrivilegedHelper.get()
        return helper.run(cmd, input_data=kw.get("input"))
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    r = subprocess.run(cmd, **kw)
    out = r.stdout.strip() if r.stdout else ""
    err = r.stderr.strip() if r.stderr else ""
    return r.returncode, out, err

def get_root_fs_info():
    code, out, _ = run(["findmnt", "-n", "-o", "SOURCE,FSTYPE,TARGET", "/"])
    if code != 0:
        return None
    parts = out.split()
    if len(parts) < 3:
        return None
    device = parts[0]
    if "[" in device:
        device = device.split("[")[0]
    return {"device": device, "fstype": parts[1], "mountpoint": parts[2]}

def get_partition_info(device):
    clean_dev = device.split("[")[0] if "[" in device else device
    code, out, _ = run(["df", "--block-size=1", "--output=size,avail", clean_dev])
    if code != 0:
        code, out, _ = run(["df", "--block-size=1", "--output=size,avail", "/"])
        if code != 0:
            return None, None
    lines = []
    for l in out.strip().splitlines():
        parts = l.split()
        if parts and parts[0].isdigit():
            lines.append(l)
    if not lines:
        return None, None
    vals = lines[-1].split()
    return int(vals[0]), int(vals[1])

def priv_makedirs(path):
    if _is_root:
        os.makedirs(path, exist_ok=True)
    else:
        PrivilegedHelper.get().makedirs(path)

def priv_read_file(path):
    if _is_root:
        with open(path, "r", errors="replace") as f:
            return f.read()
    return PrivilegedHelper.get().read_file(path)

def priv_write_file(path, content):
    if _is_root:
        with open(path, "w") as f:
            f.write(content)
    else:
        PrivilegedHelper.get().write_file(path, content)

def priv_unlink(path):
    if _is_root:
        os.unlink(path)
    else:
        PrivilegedHelper.get().unlink(path)

def sha256_file(path, progress_cb=None):
    h = hashlib.sha256()
    size = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb(done / size)
    return h.hexdigest()

def iso_cache_dir():
    d = Path.home() / ".cache" / "linux-installer"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _fix_cache_permissions():
    """Fix root-owned cache dir/files left by a previous privileged run."""
    d = Path.home() / ".cache" / "linux-installer"
    if not d.exists():
        return
    uid, gid = os.getuid(), os.getgid()
    try:
        st = d.stat()
        if st.st_uid != uid or st.st_gid != gid:
            run(["chown", "-R", f"{uid}:{gid}", str(d)])
    except OSError:
        pass

# ─── disk enumeration helpers ────────────────────────────────────────────────

def get_all_disks():
    code, out, _ = run(["lsblk", "-b", "-n", "-d", "-o", "NAME,SIZE,MODEL,TYPE", "--json"])
    if code != 0:
        code, out, _ = run(["lsblk", "-b", "-n", "-d", "-o", "NAME,SIZE,MODEL,TYPE"])
        if code != 0:
            return []
        disks = []
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 4 and parts[3].strip() == "disk":
                disks.append({
                    "name": parts[0], "path": f"/dev/{parts[0]}",
                    "size_bytes": int(parts[1]),
                    "model": parts[2] if len(parts) > 2 else "",
                })
        return disks
    data = json.loads(out)
    disks = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk":
            continue
        disks.append({
            "name": dev["name"], "path": f"/dev/{dev['name']}",
            "size_bytes": int(dev.get("size", 0)),
            "model": (dev.get("model") or "").strip(),
        })
    return disks

def get_disk_partitions(disk_path):
    code, out, _ = run(["parted", "-m", disk_path, "unit", "MiB", "print", "free"])
    if code != 0:
        return [], "gpt", 0
    partitions = []
    disk_label = "gpt"
    disk_size_mib = 0
    for line in out.splitlines():
        line = line.rstrip(";").strip()
        if not line or line == "BYT":
            continue
        cols = line.split(":")
        if len(cols) >= 6 and cols[0] == disk_path:
            disk_label = cols[5]
            try:
                disk_size_mib = int(float(cols[1].replace("MiB", "")))
            except ValueError:
                pass
            continue
        if len(cols) < 4:
            continue
        is_free = any(c.strip().lower() == "free" for c in cols)
        try:
            start_mib = int(float(cols[1].replace("MiB", "")))
            end_mib = int(float(cols[2].replace("MiB", "")))
            size_mib = int(float(cols[3].replace("MiB", "")))
        except (ValueError, IndexError):
            continue
        part_num = 0
        if not is_free and cols[0].isdigit():
            part_num = int(cols[0])
        partitions.append({
            "num": part_num, "start_mib": start_mib, "end_mib": end_mib,
            "size_mib": size_mib,
            "fstype": cols[4].strip() if len(cols) > 4 else "",
            "name": cols[5].strip() if len(cols) > 5 else "",
            "flags": cols[6].strip() if len(cols) > 6 else "",
            "is_free": is_free,
        })
    return partitions, disk_label, disk_size_mib

def get_partition_fstype(dev_path):
    code, out, _ = run(["blkid", "-o", "value", "-s", "TYPE", dev_path])
    if code == 0 and out.strip():
        return out.strip()
    return ""

def get_partition_usage(dev_path):
    code, out, _ = run(["findmnt", "-n", "-o", "TARGET", dev_path])
    if code == 0 and out.strip():
        mountpoint = out.strip()
        code2, df_out, _ = run(["df", "--block-size=1", "--output=size,avail", mountpoint])
        if code2 == 0:
            for line in df_out.strip().splitlines():
                parts = line.split()
                if parts and parts[0].isdigit():
                    return int(parts[0]), int(parts[1])
    return None, None

def get_disk_unallocated_mib(disk_path):
    parts, _, disk_size_mib = get_disk_partitions(disk_path)
    total = 0
    for p in parts:
        if p["is_free"] and p["size_mib"] > 1:
            total += p["size_mib"]
    if total == 0 and disk_size_mib > 0:
        used = sum(p["size_mib"] for p in parts if not p["is_free"])
        gap = disk_size_mib - used
        if gap > 10:
            total = gap
    return total

def get_disk_layout_text(disk_path):
    parts, label, total_mib = get_disk_partitions(disk_path)
    lines = []
    if not parts:
        lines.append(f"  [Empty disk]  {mib_to_display_gb(total_mib)} GB")
        return lines
    for p in parts:
        size_gb = mib_to_display_gb(p["size_mib"])
        if p["is_free"]:
            if size_gb > 0.01:
                lines.append(f"  [Unallocated]             {size_gb} GB")
            continue
        dev_path = _part_dev_path(disk_path, p["num"])
        fstype = get_partition_fstype(dev_path)
        name = p["name"] or ""
        flags = p["flags"] or ""
        if "boot" in flags or "esp" in flags:
            label_str = "EFI System (ESP)     "
        elif name:
            label_str = f"{name:<22}"
        elif fstype:
            label_str = f"Partition ({fstype:<8}) "
        else:
            label_str = "Partition            "
        code, mnt_out, _ = run(["findmnt", "-n", "-o", "TARGET", dev_path])
        mount_info = ""
        if code == 0 and mnt_out.strip():
            mountpoint = mnt_out.strip()
            total, free = get_partition_usage(dev_path)
            if total and free:
                mount_info = f"  [mounted: {mountpoint}, Free: {bytes_to_display_gb(free)} GB]"
            else:
                mount_info = f"  [mounted: {mountpoint}]"
        lines.append(f"  {label_str} {size_gb} GB{mount_info}")
    return lines

def _part_dev_path(disk_path, part_num):
    if "nvme" in disk_path or "mmcblk" in disk_path:
        return f"{disk_path}p{part_num}"
    return f"{disk_path}{part_num}"

def _parse_bytes_value(line):
    parts = line.split()
    for i, p in enumerate(parts):
        if p == "bytes" and i > 0:
            try:
                return int(parts[i - 1])
            except ValueError:
                pass
    return 0

def _ntfs_info(dev_path):
    if not shutil.which("ntfsresize"):
        return None, None
    code, out, _ = run(["ntfsresize", "--info", "--force", dev_path])
    if code != 0:
        return None, None
    current_size = 0
    min_size = 0
    for line in out.splitlines():
        if "Current volume size" in line and "bytes" in line:
            current_size = _parse_bytes_value(line)
        elif "You might resize at" in line and "bytes" in line:
            min_size = _parse_bytes_value(line)
    if current_size > 0:
        free = current_size - min_size if min_size > 0 else current_size // 2
        return current_size, free
    return None, None

# ─── application ─────────────────────────────────────────────────────────────

class InstallerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.linux.installer")
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        win = InstallerWindow(application=app)
        win.present()


class InstallerWindow(Gtk.ApplicationWindow):
    def __init__(self, **kw):
        super().__init__(title="ULLI USB-less Linux Installer", **kw)
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geom = monitor.get_geometry()
        scale = monitor.get_scale_factor()
        scr_w, scr_h = geom.width * scale, geom.height * scale
        self._scr_h = scr_h
        if scr_h <= 900:
            win_w, win_h = 700, min(scr_h - 80, 620)
        else:
            win_w, win_h = 760, 820
        self.set_default_size(win_w, win_h)
        self.set_resizable(True)

        self.selected_distro = "mint"
        self.custom_iso_path = ""
        self.fs_info = None
        self.running = False
        self.cancel_restart = False

        settings = Gtk.Settings.get_default()
        settings.props.gtk_application_prefer_dark_theme = True

        self._apply_css()
        self._build_ui()
        self.show_all()
        threading.Thread(target=self._refresh_disk_info, daemon=True).start()

    # ── option helpers ────────────────────────────────────────────────────────
    def _boot_gb(self):
        """Return boot partition size in GB based on ext4/FAT32 toggle."""
        if self.ext4_boot_check.get_active():
            return MIN_BOOT_GB_EXT4
        return MIN_BOOT_GB_FAT32

    def _use_refind(self):
        """Whether rEFInd should be installed."""
        return self.refind_check.get_active()

    # ── CSS ───────────────────────────────────────────────────────────────────
    def _apply_css(self):
        css = b"""
        window { background-color: #1a1d21; }
        * { color: #c8cdd8; }
        label { color: #c8cdd8; }
        checkbutton label, radiobutton label { color: #c8cdd8; }
        entry { background-color: #2e333d; color: #c8cdd8; border-color: #3d4350; }
        spinbutton { background-color: #2e333d; color: #c8cdd8; border-color: #3d4350; }
        spinbutton.size-spin, spinbutton.size-spin entry, spinbutton.size-spin text { background-color: #ffffff; color: #000000; }
        separator { background-color: #2e333d; }
        .header-title {
            font-family: 'IBM Plex Mono', 'Fira Mono', monospace;
            font-size: 22px; font-weight: 700;
            color: #87b94a; letter-spacing: -0.5px;
        }
        .sub-header { font-size: 11px; color: #5a6070; font-family: monospace; }
        .group-box {
            background-color: #22262d;
            border-radius: 8px;
            border: 1px solid #2e333d;
        }
        .group-label {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 11px; font-weight: 700;
            color: #87b94a; letter-spacing: 1px;
        }
        .distro-radio {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 12px; color: #000000;
        }
        .distro-radio * { color: #87b94a; }
        .distro-radio cellview { color: #87b94a; }
        .distro-radio:checked { color: #87b94a; }
        .custom-iso-check {
            font-family: 'IBM Plex Mono', monospace;
            font-size: 12px; color: #ffffff;
        }
        .custom-iso-check label { color: #ffffff; }
        .disk-info { font-family: monospace; font-size: 11px; color: #8892a4; }
        .fs-btrfs { color: #5bc8f5; font-weight: bold; }
        .fs-other { color: #aaaaaa; }
        .log-box {
            font-family: 'Fira Code', 'Cascadia Code', monospace;
            font-size: 11px; background-color: #ffffff; color: #000000;
        }
        .log-box text { color: #000000; background-color: #ffffff; }
        .btn-start {
            background-color: #5a8a2a; color: #ffffff;
            font-family: 'IBM Plex Mono', monospace;
            font-weight: 700; font-size: 13px;
            border-radius: 6px; border: none; padding: 8px 20px;
        }
        .btn-start:hover  { background-color: #6fa038; }
        .btn-start:active { background-color: #4a7222; }
        .btn-exit {
            background-color: #2e333d; color: #8892a4;
            font-family: 'IBM Plex Mono', monospace;
            font-size: 12px; border-radius: 6px;
            border: 1px solid #3d4350; padding: 8px 20px;
        }
        .btn-exit:hover { background-color: #3d4350; color: #c8cdd8; }
        .progress-bar { min-height: 6px; }
        .progress-bar trough { background-color: #1a1d21; border-radius: 3px; }
        .progress-bar progress { background-color: #87b94a; border-radius: 3px; }
        .strategy-label {
            font-family: monospace; font-size: 11px;
            padding: 4px 8px; border-radius: 4px;
        }
        .strategy-btrfs { background-color: #1a3040; color: #5bc8f5; }
        .strategy-none  { background-color: #2a2a2a; color: #888888; }
        .btn-browse { color: #4a7222; }
        .btn-browse label { color: #ffffff; }
        filechooser, filechooser * { color: #000000; background-color: #ffffff; }
        filechooser entry { color: #000000; background-color: #ffffff; }
        filechooser treeview { color: #000000; background-color: #ffffff; }
        filechooser treeview:selected,
        filechooser treeview row:selected,
        filechooser treeview:selected *,
        filechooser row:selected,
        filechooser row:selected * { background-color: #4a90d9; color: #ffffff; }
        filechooser treeview header button { color: #000000; }
        filechooser placesview { color: #000000; background-color: #f0f0f0; }
        filechooser placessidebar { color: #000000; background-color: #f0f0f0; }
        filechooser placessidebar label { color: #000000; }
        filechooser placessidebar row:selected,
        filechooser placessidebar row:selected * { background-color: #4a90d9; color: #ffffff; }
        filechooser button { color: #000000; }
        filechooser button label { color: #000000; }
        filechooser label { color: #000000; }
        filechooser .path-bar button label { color: #000000; }
        .dialog-action-area button { color: #000000; }
        .dialog-action-area button label { color: #000000; }
        .disk-plan { background-color: #f5f5f5; }
        .disk-plan * { color: #1a1a1a; }
        .disk-plan label { color: #1a1a1a; }
        .disk-plan frame label { color: #333333; }
        .disk-plan radiobutton label { color: #1a1a1a; }
        .disk-plan textview, .disk-plan textview text {
            color: #000000; background-color: #ffffff;
        }
        .disk-plan combobox * { color: #1a1a1a; }
        .disk-plan button { color: #1a1a1a; }
        .disk-plan button label { color: #1a1a1a; }
        .power-warn-area * { color: #000000; }
        .power-warn-area label { color: #000000; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        main_scroll = Gtk.ScrolledWindow()
        main_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.pack_start(main_scroll, True, True, 0)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_margin_start(16); root.set_margin_end(16)
        root.set_margin_top(16); root.set_margin_bottom(16)
        main_scroll.add(root)

        # Header
        hdr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label="⚙ ULLI USB-less Linux Installer")
        title.get_style_context().add_class("header-title")
        sub = Gtk.Label(label="Dual-boot installer  ·  no USB required")
        sub.get_style_context().add_class("sub-header")
        hdr.pack_start(title, False, False, 0)
        hdr.pack_start(sub, False, False, 0)
        root.pack_start(hdr, False, False, 8)

        self.status_label = Gtk.Label(label="Ready")
        self.status_label.get_style_context().add_class("sub-header")
        root.pack_start(self.status_label, False, False, 4)
        self.progress = Gtk.ProgressBar()
        self.progress.get_style_context().add_class("progress-bar")
        root.pack_start(self.progress, False, False, 4)

        root.pack_start(self._build_distro_group(), False, False, 8)
        root.pack_start(self._build_disk_group(), False, False, 0)
        root.pack_start(self._build_log_group(), True, True, 10)

        bottom = self._build_bottom_bar()
        bottom.set_margin_start(16); bottom.set_margin_end(16)
        bottom.set_margin_bottom(16)
        outer.pack_start(bottom, False, False, 0)

    def _group_frame(self, title):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.get_style_context().add_class("group-box")
        outer.set_margin_bottom(2)
        lbl = Gtk.Label(label=title, xalign=0)
        lbl.get_style_context().add_class("group-label")
        lbl.set_margin_start(12); lbl.set_margin_top(8)
        outer.pack_start(lbl, False, False, 0)
        sep = Gtk.Separator()
        sep.set_margin_start(8); sep.set_margin_end(8)
        outer.pack_start(sep, False, False, 0)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_start(12); inner.set_margin_end(12)
        inner.set_margin_bottom(12); inner.set_margin_top(4)
        outer.pack_start(inner, True, True, 0)
        return outer, inner

    def _build_distro_group(self):
        outer, inner = self._group_frame("DISTRIBUTION")
        self._distro_keys = list(DISTROS.keys())
        self.distro_combo = Gtk.ComboBoxText()
        self.distro_combo.get_style_context().add_class("distro-radio")
        for key in self._distro_keys:
            self.distro_combo.append_text(DISTROS[key]["label"])
        self.distro_combo.set_active(0)
        self.distro_combo.connect("changed", self._on_distro_combo_changed)
        inner.pack_start(self.distro_combo, False, False, 2)

        sep = Gtk.Separator()
        inner.pack_start(sep, False, False, 4)

        custom_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.custom_radio = Gtk.CheckButton(label="Use existing ISO:")
        self.custom_radio.get_style_context().add_class("custom-iso-check")
        self.custom_radio.connect("toggled", self._on_custom_toggled)
        custom_row.pack_start(self.custom_radio, False, False, 0)
        self.custom_entry = Gtk.Entry()
        self.custom_entry.set_sensitive(False)
        self.custom_entry.set_hexpand(True)
        custom_row.pack_start(self.custom_entry, True, True, 0)
        self.browse_btn = Gtk.Button(label="Browse…")
        self.browse_btn.set_sensitive(False)
        self.browse_btn.get_style_context().add_class("btn-browse")
        self.browse_btn.connect("clicked", self._on_browse)
        custom_row.pack_start(self.browse_btn, False, False, 0)
        inner.pack_start(custom_row, False, False, 2)
        return outer

    def _build_disk_group(self):
        outer, inner = self._group_frame("DISK INFORMATION")
        self.disk_info_label = Gtk.Label(xalign=0)
        self.disk_info_label.get_style_context().add_class("disk-info")
        self.disk_info_label.set_line_wrap(True)
        inner.pack_start(self.disk_info_label, False, False, 0)
        self.strategy_label = Gtk.Label(label="Detecting filesystem…", xalign=0)
        self.strategy_label.get_style_context().add_class("strategy-label")
        self.strategy_label.get_style_context().add_class("strategy-none")
        inner.pack_start(self.strategy_label, False, False, 4)
        return outer

    def _build_log_group(self):
        outer, inner = self._group_frame("INSTALLATION LOG")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(200)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.get_style_context().add_class("log-box")
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buf = self.log_view.get_buffer()
        scroll.add(self.log_view)
        inner.pack_start(scroll, True, True, 0)
        return outer

    def _build_bottom_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Top row: existing checkboxes + buttons
        top_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.delete_check = Gtk.CheckButton(label="Delete ISO after installation")
        left.pack_start(self.delete_check, False, False, 0)
        self.restart_check = Gtk.CheckButton(label="Update GRUB and restart")
        self.restart_check.set_active(True)
        left.pack_start(self.restart_check, False, False, 0)
        top_row.pack_start(left, True, True, 0)

        self.start_btn = Gtk.Button(label="▶  Start Installation")
        self.start_btn.get_style_context().add_class("btn-start")
        self.start_btn.connect("clicked", self._on_start)
        top_row.pack_start(self.start_btn, False, False, 0)

        exit_btn = Gtk.Button(label="Exit")
        exit_btn.get_style_context().add_class("btn-exit")
        exit_btn.connect("clicked", lambda _: self.get_application().quit())
        top_row.pack_start(exit_btn, False, False, 0)
        bar.pack_start(top_row, False, False, 0)

        sep = Gtk.Separator()
        bar.pack_start(sep, False, False, 2)

        # Boot format and rEFInd options
        self.ext4_boot_check = Gtk.CheckButton(
            label="Use ext4 boot partition (12 GB) instead of FAT32 (7 GB)  –  "
                  "required for large distros (Bazzite, etc.) · requires rEFInd")
        self.ext4_boot_check.set_active(False)
        self.ext4_boot_check.connect("toggled", self._on_ext4_boot_toggled)
        bar.pack_start(self.ext4_boot_check, False, False, 0)

        self.refind_check = Gtk.CheckButton(
            label="Install rEFInd boot manager  –  "
                  "required for large distros (Bazzite, etc.) · requires disabling Secure Boot")
        self.refind_check.set_active(False)
        self.refind_check.connect("toggled", self._on_refind_toggled)
        bar.pack_start(self.refind_check, False, False, 0)

        return bar

    def _on_ext4_boot_toggled(self, btn):
        if btn.get_active():
            self.refind_check.set_active(True)

    def _on_refind_toggled(self, btn):
        if not btn.get_active():
            self.ext4_boot_check.set_active(False)

    # ── signal handlers ───────────────────────────────────────────────────────
    def _on_distro_combo_changed(self, combo):
        idx = combo.get_active()
        if 0 <= idx < len(self._distro_keys):
            self.selected_distro = self._distro_keys[idx]

    def _on_custom_toggled(self, btn):
        on = btn.get_active()
        self.custom_entry.set_sensitive(on)
        self.browse_btn.set_sensitive(on)
        self.distro_combo.set_sensitive(not on)

    def _on_browse(self, _btn):
        dlg = Gtk.FileChooserDialog(
            title="Select ISO file", parent=self,
            action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        user_home = os.environ.get("HOME") or Path.home()
        dlg.set_current_folder(str(user_home))
        f = Gtk.FileFilter(); f.set_name("ISO files"); f.add_pattern("*.iso")
        dlg.add_filter(f)
        if dlg.run() == Gtk.ResponseType.OK:
            self.custom_iso_path = dlg.get_filename()
            self.custom_entry.set_text(self.custom_iso_path)
            self.custom_entry.set_tooltip_text(self.custom_iso_path)
            iso_name = os.path.basename(self.custom_iso_path)
            self.custom_radio.set_label(f"Use existing ISO:  {iso_name}")
        dlg.destroy()

    def _on_start(self, _btn):
        if self.running:
            return
        threading.Thread(target=self._run_install, daemon=True).start()

    # ── disk info ─────────────────────────────────────────────────────────────
    def _refresh_disk_info(self):
        try:
            return self._refresh_disk_info_inner()
        except RuntimeError as e:
            self._ui_set_disk_info(
                f"Root privileges required:\n{e}",
                "Cannot proceed without authentication.", "strategy-none")

    def _refresh_disk_info_inner(self):
        self.fs_info = get_root_fs_info()
        if not self.fs_info:
            self._ui_set_disk_info("Could not detect root filesystem", None, None)
            return
        dev = self.fs_info["device"]
        fstype = self.fs_info["fstype"]
        total, free = get_partition_info(dev)
        total_gb = bytes_to_display_gb(total) if total else "?"
        free_gb = bytes_to_display_gb(free) if free else "?"
        text = (
            f"Device:     {dev}\n"
            f"Filesystem: {fstype}\n"
            f"Total:      {total_gb} GB\n"
            f"Free:       {free_gb} GB\n"
            f"Mountpoint: {self.fs_info['mountpoint']}")
        all_disks = get_all_disks()
        root_disk_path = ""
        if dev:
            disk_d, _ = self._resolve_disk_and_part(dev)
            if disk_d:
                root_disk_path = disk_d
        other_disks = [d for d in all_disks if d["path"] != root_disk_path]
        if fstype == "btrfs":
            strat = "STRATEGY: shrink btrfs → install to new partition"
            sc = "strategy-btrfs"
        elif other_disks:
            strat = f"Root is {fstype} (not shrinkable) – use a second drive to install"
            sc = "strategy-none"
        else:
            strat = f"WARNING: unsupported filesystem ({fstype}) – only btrfs is supported"
            sc = "strategy-none"
        self._ui_set_disk_info(text, strat, sc)

    def _ui_set_disk_info(self, text, strat, style_class):
        def _update():
            self.disk_info_label.set_text(text)
            ctx = self.strategy_label.get_style_context()
            for c in ["strategy-btrfs", "strategy-none"]:
                ctx.remove_class(c)
            if strat:
                self.strategy_label.set_text(strat)
                if style_class:
                    ctx.add_class(style_class)
        GLib.idle_add(_update)

    # ── logging ───────────────────────────────────────────────────────────────
    def log(self, msg, error=False):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        def _append():
            end = self.log_buf.get_end_iter()
            self.log_buf.insert(end, line)
            self.log_view.scroll_to_iter(self.log_buf.get_end_iter(), 0, False, 0, 0)
        GLib.idle_add(_append)
        if error:
            print(f"\033[31m{line}\033[0m", end="", file=sys.stderr)
        else:
            print(line, end="")

    def set_status(self, msg):
        GLib.idle_add(self.status_label.set_text, msg)

    def set_progress(self, frac):
        GLib.idle_add(self.progress.set_fraction, min(1.0, max(0.0, frac)))

    def pulse(self):
        GLib.idle_add(self.progress.pulse)

    # ── disk plan dialog ────────────────────────────────────────────────────
    def _show_disk_plan(self, distro_label):
        boot_mib = gb_to_mib(self._boot_gb())
        refind_mib = REFIND_MIB if self._use_refind() else 0

        all_disks = get_all_disks()
        root_info = self.fs_info
        root_dev = root_info["device"] if root_info else ""
        root_disk_path = ""
        if root_dev:
            disk_dev, _ = self._resolve_disk_and_part(root_dev)
            if disk_dev:
                root_disk_path = disk_dev

        disk_entries = []
        for d in all_disks:
            size_gb = bytes_to_display_gb(d["size_bytes"])
            free_mib = get_disk_unallocated_mib(d["path"])
            free_gb = mib_to_display_gb(free_mib)
            is_root = (d["path"] == root_disk_path)
            prefix = f"{d['name']} (current OS)" if is_root else d["name"]
            model = d["model"] or "Disk"
            label = f"{prefix} – {model} – {size_gb} GB – Free: {free_gb} GB"
            disk_entries.append({
                "name": d["name"], "path": d["path"], "label": label,
                "is_root": is_root, "size_gb": size_gb,
                "size_mib": bytes_to_mib(d["size_bytes"]),
                "free_gb": free_gb, "free_mib": free_mib,
            })

        dialog = Gtk.Dialog(
            title="Disk Plan – Review Before Proceeding",
            transient_for=self, modal=True, destroy_with_parent=True)
        dialog.set_default_size(700, min(680, self._scr_h - 140) if self._scr_h <= 900 else 680)
        dialog.set_resizable(True)

        raw_content = dialog.get_content_area()
        dialog_scroll = Gtk.ScrolledWindow()
        dialog_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        raw_content.pack_start(dialog_scroll, True, True, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.get_style_context().add_class("disk-plan")
        content.set_margin_start(16); content.set_margin_end(16)
        content.set_margin_top(12); content.set_margin_bottom(8)
        dialog_scroll.add(content)

        title = Gtk.Label(label=f"Review Disk Changes for {distro_label}", xalign=0)
        title.set_markup(
            f'<span size="large" weight="bold" foreground="#1a1a1a">'
            f'Review Disk Changes for {distro_label}</span>')
        content.pack_start(title, False, False, 0)

        warn_label = Gtk.Label(xalign=0, wrap=True)
        warn_label.set_markup(
            '<span foreground="#996600">⚠  These changes modify your disk\'s partition '
            'table. Some options (like wipe &amp; reformat) will DESTROY ALL DATA on the '
            'target disk. Make sure you have a backup of important files before proceeding.'
            '</span>')
        content.pack_start(warn_label, False, False, 4)

        disk_frame = Gtk.Frame(label="Target Disk")
        disk_combo = Gtk.ComboBoxText()
        root_index = 0
        for i, de in enumerate(disk_entries):
            disk_combo.append_text(de["label"])
            if de["is_root"]:
                root_index = i
        disk_combo.set_active(root_index)
        disk_frame.add(disk_combo)
        disk_frame.set_margin_top(4)
        content.pack_start(disk_frame, False, False, 0)

        size_frame = Gtk.Frame(label="Linux Partition Size")
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        size_box.set_margin_start(8); size_box.set_margin_end(8)
        size_box.set_margin_top(4); size_box.set_margin_bottom(4)
        size_lbl = Gtk.Label(label="Linux size (GB):", xalign=0)
        size_box.pack_start(size_lbl, False, False, 0)
        size_adj = Gtk.Adjustment(value=30, lower=MIN_LINUX_GB, upper=500,
                                  step_increment=5, page_increment=20)
        size_spin = Gtk.SpinButton(adjustment=size_adj, climb_rate=1, digits=0)
        size_spin.get_style_context().add_class("size-spin")
        size_box.pack_start(size_spin, False, False, 0)
        size_help = Gtk.Label(label="  Minimum 20 GB · Recommended 60+ GB", xalign=0)
        size_box.pack_start(size_help, True, True, 0)
        size_frame.add(size_box)
        content.pack_start(size_frame, False, False, 0)

        layout_frame = Gtk.Frame(label="Current Disk Layout")
        layout_text = Gtk.TextView()
        layout_text.set_editable(False); layout_text.set_cursor_visible(False)
        layout_text.set_monospace(True); layout_text.set_wrap_mode(Gtk.WrapMode.NONE)
        layout_scroll = Gtk.ScrolledWindow()
        layout_scroll.set_min_content_height(80)
        layout_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        layout_scroll.add(layout_text)
        layout_frame.add(layout_scroll)
        content.pack_start(layout_frame, False, False, 0)

        strat_frame = Gtk.Frame(label="Installation Strategy")
        strat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        strat_box.set_margin_start(8); strat_box.set_margin_end(8)
        strat_box.set_margin_top(4); strat_box.set_margin_bottom(4)
        radio_primary = Gtk.RadioButton.new_with_label(None, "")
        radio_secondary = Gtk.RadioButton.new_with_label_from_widget(radio_primary, "")
        radio_wipe = Gtk.RadioButton.new_with_label_from_widget(radio_primary, "")
        strat_box.pack_start(radio_primary, False, False, 0)
        strat_box.pack_start(radio_secondary, False, False, 0)
        strat_box.pack_start(radio_wipe, False, False, 0)
        strat_frame.add(strat_box)
        content.pack_start(strat_frame, False, False, 0)

        changes_frame = Gtk.Frame(label="Planned Changes")
        changes_text = Gtk.TextView()
        changes_text.set_editable(False); changes_text.set_cursor_visible(False)
        changes_text.set_monospace(True)
        changes_scroll = Gtk.ScrolledWindow()
        changes_scroll.set_min_content_height(60)
        changes_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        changes_scroll.add(changes_text)
        changes_frame.add(changes_scroll)
        content.pack_start(changes_frame, False, False, 0)

        after_frame = Gtk.Frame(label="Disk Layout After Changes")
        after_text = Gtk.TextView()
        after_text.set_editable(False); after_text.set_cursor_visible(False)
        after_text.set_monospace(True)
        after_scroll = Gtk.ScrolledWindow()
        after_scroll.set_min_content_height(60)
        after_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        after_scroll.add(after_text)
        after_frame.add(after_scroll)
        content.pack_start(after_frame, False, False, 0)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        confirm_btn = dialog.add_button("Confirm & Proceed", Gtk.ResponseType.OK)
        confirm_btn.get_style_context().add_class("suggested-action")

        plan_state = {
            "strategy": "shrink_root", "target_disk": root_disk_path,
            "shrink_dev": None, "shrink_mib": 0,
        }

        def update_all(*_args):
            idx = disk_combo.get_active()
            if idx < 0 or idx >= len(disk_entries):
                return
            linux_gb = int(size_spin.get_value())
            linux_mib = gb_to_mib(linux_gb)
            total_needed_mib = linux_mib + boot_mib + refind_mib
            total_needed_gb = mib_to_display_gb(total_needed_mib)

            sel = disk_entries[idx]
            sel_path = sel["path"]
            is_root_disk = sel["is_root"]
            plan_state["target_disk"] = sel_path

            layout_lines = get_disk_layout_text(sel_path)
            free_mib = get_disk_unallocated_mib(sel_path)
            free_gb = mib_to_display_gb(free_mib)
            if free_gb > 0.01:
                layout_lines.append("")
                layout_lines.append(f"  Total unallocated space: {free_gb} GB")
            layout_text.get_buffer().set_text("\n".join(layout_lines))

            has_free = (free_mib >= (boot_mib + refind_mib + gb_to_mib(1)))
            boot_gb_d = mib_to_display_gb(boot_mib)
            linux_gb_d = mib_to_display_gb(linux_mib)
            boot_fs = "ext4" if self.ext4_boot_check.get_active() else "FAT32"
            refind_str = f" + {REFIND_MIB} MB rEFInd" if refind_mib else ""

            change_lines = []
            after_lines = []

            if is_root_disk:
                radio_wipe.set_visible(False)
                fstype = self.fs_info["fstype"] if self.fs_info else ""
                can_shrink = (fstype == "btrfs")

                if has_free and can_shrink:
                    radio_primary.set_label(
                        f"Shrink root btrfs partition by {total_needed_gb} GB "
                        f"for Linux ({linux_gb_d} GB) + boot ({boot_gb_d} GB){refind_str}")
                    radio_primary.set_visible(True); radio_primary.set_sensitive(True)
                    radio_secondary.set_label(
                        f"Use existing unallocated space ({free_gb} GB) – no shrink needed")
                    radio_secondary.set_visible(True); radio_secondary.set_sensitive(True)
                    strat_frame.set_visible(True)
                elif has_free:
                    radio_primary.set_label(
                        f"Use existing unallocated space ({free_gb} GB)")
                    radio_primary.set_visible(True); radio_primary.set_sensitive(True)
                    radio_secondary.set_visible(False)
                    strat_frame.set_visible(True)
                elif can_shrink:
                    radio_primary.set_label(
                        f"Shrink root btrfs partition by {total_needed_gb} GB")
                    radio_primary.set_visible(True); radio_primary.set_sensitive(True)
                    radio_secondary.set_visible(False)
                    strat_frame.set_visible(True)
                else:
                    radio_primary.set_label(
                        f"Cannot proceed: {fstype} is not shrinkable and no free space")
                    radio_primary.set_visible(True); radio_primary.set_sensitive(False)
                    radio_secondary.set_visible(False)
                    strat_frame.set_visible(True)

                use_free = has_free and radio_secondary.get_visible() and radio_secondary.get_active()
                if not can_shrink and has_free:
                    use_free = True

                if use_free:
                    plan_state["strategy"] = "use_free_root"
                    plan_state["shrink_dev"] = None
                    plan_state["shrink_mib"] = 0
                    change_lines.append("  1. Root partition is NOT modified")
                    change_lines.append(
                        f"  2. Create {boot_gb_d} GB {boot_fs} boot partition (LINUX_LIVE)")
                    change_lines.append(
                        f"  3. Remaining space for Linux installer")
                    step = 4
                    if refind_mib:
                        change_lines.append(
                            f"  {step}. Create {REFIND_MIB} MB FAT32 rEFInd partition (at end of free space)")
                        step += 1
                    change_lines.append(
                        f"  {step}. Configure boot for {distro_label}")

                    for p_line in get_disk_layout_text(sel_path):
                        if "[Unallocated]" not in p_line:
                            after_lines.append(p_line.rstrip() + "  (unchanged)")
                    remain_gb = mib_to_display_gb(free_mib - boot_mib - refind_mib)
                    if remain_gb > 0:
                        after_lines.append(
                            f"  [Unallocated – Linux]  {remain_gb} GB  ← for installer")
                    after_lines.append(
                        f"  LINUX_LIVE ({boot_fs})    {boot_gb_d} GB  ← {distro_label}")
                    if refind_mib:
                        after_lines.append(
                            f"  REFIND (FAT32)        {mib_to_display_gb(REFIND_MIB)} GB  ← rEFInd")
                else:
                    plan_state["strategy"] = "shrink_root"
                    plan_state["shrink_dev"] = root_dev
                    plan_state["shrink_mib"] = total_needed_mib

                    root_total, _ = get_partition_info(root_dev)
                    root_size_gb = bytes_to_display_gb(root_total or 0)
                    new_size_gb = round(root_size_gb - total_needed_gb, 2)

                    change_lines.append(
                        f"  1. Shrink root ({root_dev}) from "
                        f"{root_size_gb} GB to {new_size_gb} GB  (−{total_needed_gb} GB)")
                    change_lines.append(
                        f"  2. Create {boot_gb_d} GB {boot_fs} boot partition (LINUX_LIVE)")
                    change_lines.append(
                        f"  3. Leave {linux_gb_d} GB for Linux installer")
                    step = 4
                    if refind_mib:
                        change_lines.append(
                            f"  {step}. Create {REFIND_MIB} MB FAT32 rEFInd partition (at end of free space)")
                        step += 1
                    change_lines.append(
                        f"  {step}. Configure boot for {distro_label}")

                    parts, _, _ = get_disk_partitions(sel_path)
                    _, root_part_num = self._resolve_disk_and_part(root_dev)
                    for p in parts:
                        if p["is_free"]:
                            continue
                        s_gb = mib_to_display_gb(p["size_mib"])
                        if p["num"] == root_part_num:
                            after_lines.append(
                                f"  Root ({root_dev})       {new_size_gb} GB  (shrunk)")
                            after_lines.append(
                                f"  LINUX_LIVE ({boot_fs})    {boot_gb_d} GB  ← {distro_label}")
                            after_lines.append(
                                f"  [Unallocated – Linux]  {linux_gb_d} GB  ← for installer")
                            if refind_mib:
                                after_lines.append(
                                    f"  REFIND (FAT32)        {mib_to_display_gb(REFIND_MIB)} GB  ← rEFInd")
                        else:
                            dev_p = _part_dev_path(sel_path, p["num"])
                            lbl = p["name"] or get_partition_fstype(dev_p) or "Partition"
                            after_lines.append(f"  {lbl:<22} {s_gb} GB")

            else:
                # Other disk
                SHRINKABLE_FS = ("btrfs", "ext4", "ext3", "ext2", "ntfs")
                shrinkable = []
                parts, _, _ = get_disk_partitions(sel_path)
                for p in parts:
                    if p["is_free"] or p["num"] == 0:
                        continue
                    dev_p = _part_dev_path(sel_path, p["num"])
                    fs = get_partition_fstype(dev_p)
                    if fs in SHRINKABLE_FS:
                        total_b, free_b = get_partition_usage(dev_p)
                        if not total_b or not free_b:
                            if fs == "ntfs":
                                total_b, free_b = _ntfs_info(dev_p)
                            if not total_b or not free_b:
                                part_size_b = p["size_mib"] * MiB
                                total_b = part_size_b
                                free_b = part_size_b // 2
                        if free_b > mib_to_bytes(total_needed_mib):
                            shrinkable.append({
                                "dev": dev_p, "num": p["num"], "fstype": fs,
                                "size_gb": mib_to_display_gb(p["size_mib"]),
                                "free_gb": bytes_to_display_gb(free_b),
                            })

                has_shrinkable = len(shrinkable) > 0

                non_shrinkable_fs = []
                for p in parts:
                    if p["is_free"] or p["num"] == 0:
                        continue
                    dev_p = _part_dev_path(sel_path, p["num"])
                    fs = get_partition_fstype(dev_p)
                    if fs and fs not in SHRINKABLE_FS and fs not in ("vfat", "swap", ""):
                        non_shrinkable_fs.append({"dev": dev_p, "fstype": fs,
                            "size_gb": mib_to_display_gb(p["size_mib"])})

                if has_free:
                    radio_primary.set_label(
                        f"Use existing unallocated space ({free_gb} GB) on {sel['name']}")
                    radio_primary.set_visible(True); radio_primary.set_sensitive(True)
                    if not radio_primary.get_active() and not radio_secondary.get_active() \
                            and not radio_wipe.get_active():
                        radio_primary.set_active(True)
                else:
                    radio_primary.set_visible(False)
                    radio_primary.set_active(False)

                if has_shrinkable:
                    best = max(shrinkable, key=lambda s: s["free_gb"])
                    radio_secondary.set_label(
                        f"Shrink {best['dev']} ({best['fstype']}, {best['size_gb']} GB, "
                        f"{best['free_gb']} GB free) to make space")
                    radio_secondary.set_visible(True); radio_secondary.set_sensitive(True)
                    if not has_free and not radio_wipe.get_active():
                        radio_secondary.set_active(True)
                else:
                    radio_secondary.set_visible(False)
                    radio_secondary.set_active(False)

                wipe_min = 512 + boot_mib + refind_mib + gb_to_mib(1)
                disk_size_ok = (sel["size_mib"] >= wipe_min)
                radio_wipe.set_label(
                    f"⚠ Wipe & reformat entire disk ({sel['size_gb']} GB) – "
                    f"ALL DATA ON {sel['name']} WILL BE DESTROYED")
                radio_wipe.set_visible(True)
                radio_wipe.set_sensitive(disk_size_ok)

                if not has_free and not has_shrinkable:
                    radio_primary.set_label(
                        f"No unallocated space or shrinkable partitions on {sel['name']}")
                    radio_primary.set_visible(True); radio_primary.set_sensitive(False)
                    if non_shrinkable_fs:
                        fs_list = ", ".join(
                            f"{x['dev']} ({x['fstype']})" for x in non_shrinkable_fs)
                        radio_secondary.set_label(
                            f"Cannot shrink {fs_list} – only btrfs/ext4/NTFS can be resized")
                        radio_secondary.set_visible(True); radio_secondary.set_sensitive(False)
                    if disk_size_ok and not radio_wipe.get_active():
                        radio_wipe.set_active(True)

                strat_frame.set_visible(True)

                using_wipe = radio_wipe.get_visible() and radio_wipe.get_active()
                using_shrink = (has_shrinkable and
                    radio_secondary.get_visible() and radio_secondary.get_active())
                if has_shrinkable and not has_free and not using_wipe:
                    using_shrink = True

                if using_wipe:
                    plan_state["strategy"] = "wipe_disk"
                    plan_state["shrink_dev"] = None
                    plan_state["shrink_mib"] = 0
                    esp_mib = 512
                    usable_gb = mib_to_display_gb(sel["size_mib"] - esp_mib - boot_mib - refind_mib)
                    change_lines.append("  ⚠ WARNING: This will ERASE ALL DATA on this disk!")
                    change_lines.append("")
                    change_lines.append("  1. Root partition is NOT modified (different disk)")
                    change_lines.append(
                        f"  2. Wipe {sel['name']} and create a new GPT partition table")
                    change_lines.append(f"  3. Create 512 MB EFI System Partition (ESP)")
                    change_lines.append(
                        f"  4. Create {boot_gb_d} GB {boot_fs} boot partition (LINUX_LIVE)")
                    change_lines.append(
                        f"  5. Leave ~{usable_gb} GB unallocated for Linux installer")
                    step = 6
                    if refind_mib:
                        change_lines.append(
                            f"  {step}. Create {REFIND_MIB} MB FAT32 rEFInd partition (at end of disk)")
                        step += 1
                    change_lines.append(
                        f"  {step}. Configure boot for {distro_label}")

                    after_lines.append(f"  EFI System (ESP)       0.5 GB  ← UEFI boot files")
                    after_lines.append(
                        f"  LINUX_LIVE ({boot_fs})    {boot_gb_d} GB  ← {distro_label}")
                    after_lines.append(
                        f"  [Unallocated – Linux]  ~{usable_gb} GB  ← for installer")
                    if refind_mib:
                        after_lines.append(
                            f"  REFIND (FAT32)        {mib_to_display_gb(REFIND_MIB)} GB  ← rEFInd")

                elif using_shrink:
                    best = max(shrinkable, key=lambda s: s["free_gb"])
                    plan_state["strategy"] = "other_disk_shrink"
                    plan_state["shrink_dev"] = best["dev"]
                    plan_state["shrink_mib"] = total_needed_mib
                    new_size_gb = round(best["size_gb"] - total_needed_gb, 2)
                    change_lines.append("  1. Root partition is NOT modified (different disk)")
                    change_lines.append(
                        f"  2. Shrink {best['dev']} from {best['size_gb']} GB to "
                        f"{new_size_gb} GB  (−{total_needed_gb} GB)")
                    change_lines.append(
                        f"  3. Create {boot_gb_d} GB {boot_fs} boot partition (LINUX_LIVE)")
                    change_lines.append(
                        f"  4. Leave {linux_gb_d} GB for Linux installer")
                    step = 5
                    if refind_mib:
                        change_lines.append(
                            f"  {step}. Create {REFIND_MIB} MB FAT32 rEFInd partition (at end of free space)")
                        step += 1
                    change_lines.append(
                        f"  {step}. Configure boot for {distro_label}")

                elif has_free:
                    plan_state["strategy"] = "other_disk_free"
                    plan_state["shrink_dev"] = None
                    plan_state["shrink_mib"] = 0
                    change_lines.append("  1. Root partition is NOT modified (different disk)")
                    change_lines.append(
                        f"  2. Create {boot_gb_d} GB {boot_fs} (LINUX_LIVE) on {sel['name']}")
                    change_lines.append(
                        f"  3. Remaining space for installer")
                    step = 4
                    if refind_mib:
                        change_lines.append(
                            f"  {step}. Create {REFIND_MIB} MB FAT32 rEFInd partition (at end of free space)")
                        step += 1
                    change_lines.append(
                        f"  {step}. Configure boot for {distro_label}")
                else:
                    plan_state["strategy"] = "blocked"
                    change_lines.append("  Cannot proceed with this disk.")
                    if non_shrinkable_fs:
                        change_lines.append("")
                        for nf in non_shrinkable_fs:
                            change_lines.append(
                                f"  {nf['dev']} is {nf['fstype']} ({nf['size_gb']} GB) – cannot be shrunk.")
                        change_lines.append("")
                        change_lines.append("  To use this disk:")
                        change_lines.append("    – Back up data, shrink/delete partitions with GParted")
                        change_lines.append("    – Re-run ULLI (it will detect the free space)")

            changes_text.get_buffer().set_text("\n".join(change_lines))
            after_text.get_buffer().set_text(
                "\n".join(after_lines) if after_lines else "(see planned changes above)")
            confirm_btn.set_sensitive(plan_state["strategy"] != "blocked")

        disk_combo.connect("changed", update_all)
        radio_primary.connect("toggled", update_all)
        radio_secondary.connect("toggled", update_all)
        radio_wipe.connect("toggled", update_all)
        size_spin.connect("value-changed", update_all)
        update_all()

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.OK:
            return {
                "approved": True,
                "strategy": plan_state["strategy"],
                "target_disk": plan_state["target_disk"],
                "shrink_dev": plan_state["shrink_dev"],
                "shrink_mib": plan_state["shrink_mib"],
                "linux_mib": gb_to_mib(int(size_spin.get_value())),
            }
        return None

    def _show_power_warning_dialog(self):
        dlg = Gtk.MessageDialog(
            transient_for=self, modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="Keep your computer plugged in")
        dlg.format_secondary_text(
            "A partition resize is about to begin. Power loss during this process "
            "could corrupt your partition table.\n\n"
            "Make sure your computer is connected to AC power before continuing.")
        dlg.get_message_area().get_style_context().add_class("power-warn-area")
        dlg.run()
        dlg.destroy()

    # ── installation entry point ───────────────────────────────────────────────
    def _run_install(self):
        self.running = True
        GLib.idle_add(self.start_btn.set_sensitive, False)
        try:
            self._do_install()
        except Exception as e:
            self.log(f"FATAL ERROR: {e}", error=True)
            self.set_status("Installation failed!")
        finally:
            self.running = False
            GLib.idle_add(self.start_btn.set_sensitive, True)
            GLib.idle_add(self.set_progress, 0)

    def _do_install(self):
        self.log("=" * 52)
        self.log("Linux Live Installer starting")
        self.log("=" * 52)
        _fix_cache_permissions()

        self.fs_info = get_root_fs_info()
        if not self.fs_info:
            self.log("Cannot detect root filesystem.", error=True)
            return

        fstype = self.fs_info["fstype"]
        device = self.fs_info["device"]
        distro_key = self.selected_distro
        custom_mode = self.custom_radio.get_active()
        distro = DISTROS[distro_key]

        self.log(f"Root device : {device}")
        self.log(f"Filesystem  : {fstype}")
        if custom_mode:
            iso_basename = os.path.basename(self.custom_iso_path) if self.custom_iso_path else "unknown"
            self.log(f"Distro      : Custom ISO – {iso_basename}")
            distro_label = os.path.splitext(iso_basename)[0]
        else:
            self.log(f"Distro      : {distro['label']}")
            distro_label = distro["label"].split("(")[0].strip()
        self._distro_label = distro_label

        plan_result = [None]
        plan_event = threading.Event()
        def show_plan_on_main():
            plan_result[0] = self._show_disk_plan(distro_label)
            plan_event.set()
            return False
        GLib.idle_add(show_plan_on_main)
        plan_event.wait()

        plan = plan_result[0]
        if not plan or not plan.get("approved"):
            self.log("Installation cancelled by user.")
            self.set_status("Ready")
            return

        strategy = plan["strategy"]
        target_disk = plan["target_disk"]
        shrink_dev = plan.get("shrink_dev")
        shrink_mib = plan.get("shrink_mib", 0)
        linux_mib = plan.get("linux_mib", gb_to_mib(30))
        self.log(f"Disk plan approved. Strategy: {strategy}, Target: {target_disk}")
        self.log(f"Target size : {mib_to_display_gb(linux_mib)} GB ({linux_mib} MiB)")

        # Resolve ISO
        if custom_mode:
            iso_path = self.custom_iso_path
            if not iso_path or not os.path.exists(iso_path):
                self.log("No valid ISO selected.", error=True)
                return
            self.log(f"Custom ISO: {iso_path}")
        else:
            iso_path = str(iso_cache_dir() / distro["filename"])
            if os.path.exists(iso_path):
                sz = os.path.getsize(iso_path)
                self.log(f"Found cached ISO ({bytes_to_display_gb(sz)} GB): {iso_path}")
                ok = self._verify_checksum(iso_path, distro["sha256"])
                if not ok:
                    self.log("Checksum mismatch – re-downloading.", error=True)
                    os.unlink(iso_path)
                    if not self._download_iso(distro, iso_path):
                        return
            else:
                if not self._download_iso(distro, iso_path):
                    return

        self._boot_part_dev = None

        if strategy in ("shrink_root", "other_disk_shrink"):
            warn_event = threading.Event()
            def _show_warn():
                self._show_power_warning_dialog()
                warn_event.set()
                return False
            GLib.idle_add(_show_warn)
            warn_event.wait()

        ok = False
        if strategy == "shrink_root":
            if fstype != "btrfs":
                self.log(f"Cannot shrink root: {fstype} not btrfs.", error=True)
                return
            ok = self._strategy_btrfs(device, linux_mib, iso_path, distro, distro_key, custom_mode)
        elif strategy in ("use_free_root", "other_disk_free"):
            ok = self._strategy_use_free(target_disk, linux_mib, iso_path, distro, distro_key, custom_mode)
        elif strategy == "other_disk_shrink":
            if not shrink_dev:
                self.log("No partition selected to shrink.", error=True)
                return
            ok = self._strategy_other_disk_shrink(
                target_disk, shrink_dev, shrink_mib, linux_mib,
                iso_path, distro, distro_key, custom_mode)
        elif strategy == "wipe_disk":
            ok = self._strategy_wipe_disk(
                target_disk, linux_mib, iso_path, distro, distro_key, custom_mode)
        else:
            self.log(f"Unknown strategy: {strategy}", error=True)
            return

        if not ok:
            self.log("Installation aborted.", error=True)
            return

        if self.delete_check.get_active() and not custom_mode:
            try:
                os.unlink(iso_path)
                self.log("ISO file deleted.")
            except Exception as e:
                self.log(f"Could not delete ISO: {e}")

        # rEFInd boot entry is configured by _install_refind() if enabled
        self._update_grub()

        if self.restart_check.get_active():
            self._do_restart()

        self.set_status("Installation complete!")
        self.log("=" * 52)
        self.log("All done! Review the log above for any warnings.")
        self.log("=" * 52)

    # ── ISO download / verify ─────────────────────────────────────────────────
    def _download_iso(self, distro, dest):
        for i, url in enumerate(distro["mirrors"]):
            host = url.split("/")[2]
            self.log(f"Trying mirror {i+1}/{len(distro['mirrors'])}: {host}")
            self.set_status(f"Connecting to {host}…")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "linux-installer/1.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    total_mb = round(total / 1e6, 1)
                    done = 0
                    with open(dest, "wb") as f:
                        while True:
                            chunk = resp.read(1 << 17)
                            if not chunk:
                                break
                            f.write(chunk)
                            done += len(chunk)
                            if total:
                                frac = done / total
                                mb = round(done / 1e6, 1)
                                GLib.idle_add(self.progress.set_fraction, frac)
                                self.set_status(
                                    f"Downloading {frac*100:.0f}%  {mb} / {total_mb} MB")
                self.log(f"Download complete: {bytes_to_display_gb(os.path.getsize(dest))} GB")
                GLib.idle_add(self.progress.set_fraction, 0)

                if not self._verify_checksum(dest, distro["sha256"]):
                    self.log("Checksum failed – trying next mirror.", error=True)
                    os.unlink(dest)
                    continue
                return True
            except Exception as e:
                self.log(f"Download error: {e}", error=True)
                if os.path.exists(dest):
                    os.unlink(dest)

        self.log("All download mirrors failed.", error=True)
        self.log(f"Please download manually and place at:\n  {dest}", error=True)
        return False

    def _verify_checksum(self, path, expected):
        self.log("Verifying SHA-256 checksum…")
        self.set_status("Verifying ISO integrity…")
        def progress_cb(frac):
            GLib.idle_add(self.progress.set_fraction, frac)
            self.set_status(f"Checksumming… {frac*100:.0f}%")
        actual = sha256_file(path, progress_cb)
        GLib.idle_add(self.progress.set_fraction, 0)
        if actual == expected:
            self.log("✓ Checksum OK")
            return True
        self.log(f"✗ Expected: {expected}", error=True)
        self.log(f"✗ Actual:   {actual}", error=True)
        return False

    # ── shared partition helpers ──────────────────────────────────────────────

    def _resize_partition_entry(self, disk_dev, part_num, part_start_mib, new_size_mib):
        """Shrink a partition table entry via sfdisk (with parted fallback)."""
        new_part_end_mib = part_start_mib + new_size_mib
        new_size_sectors = new_size_mib * 2048
        self.log(f"Shrinking partition {part_num}: end → {new_part_end_mib} MiB "
                 f"({new_size_sectors} sectors)")
        self.set_status("Shrinking partition…")

        sfdisk_script = f"{part_num}: size={new_size_sectors}\n"
        rc, _, sfdisk_err = run(
            ["sfdisk", "--no-reread", "-N", str(part_num), disk_dev],
            input=sfdisk_script)
        if rc != 0:
            self.log(f"sfdisk resize failed: {sfdisk_err}", error=True)
            self.log("Trying parted fallback…")
            rc2, _, parted_err = run(
                ["env", "LANG=C", "LC_ALL=C",
                 "parted", "---pretend-input-tty", "-s", "--", disk_dev,
                 "resizepart", str(part_num), f"{new_part_end_mib}MiB"],
                input="Yes\n")
            if rc2 != 0:
                self.log(f"Partition resize failed: {parted_err}", error=True)
                return None

        run(["partprobe", disk_dev])
        time.sleep(2)

        parts, _, _ = get_disk_partitions(disk_dev)
        for p in parts:
            if not p["is_free"] and p["num"] == part_num:
                self.log(f"Partition {part_num} now ends at {p['end_mib']} MiB.")
                return p["end_mib"]

        self.log("Cannot read partition table after resize.", error=True)
        return None

    def _create_partitions(self, disk_path, boot_start, boot_end,
                            linux_start_hint, linux_end_str, is_gpt):
        """Create LINUX_LIVE, optionally REFIND, and linux partitions.
        Returns (boot_dev, linux_dev, refind_dev_or_None) or None."""
        use_refind = self._use_refind()
        linux_start = boot_end + 1
        refind_start = 0
        refind_end = 0

        if use_refind:
            if linux_end_str == "100%":
                _, _, disk_size_mib = get_disk_partitions(disk_path)
                refind_end = disk_size_mib - 1
                refind_start = refind_end - REFIND_MIB
                linux_end_str = f"{refind_start - 1}MiB"
            else:
                orig_end = int(float(linux_end_str.replace("MiB", "")))
                refind_end = orig_end
                refind_start = refind_end - REFIND_MIB
                linux_end_str = f"{refind_start - 1}MiB"

        self.log(f"Creating partitions: boot {boot_start}-{boot_end} MiB" +
                 (f", refind {refind_start}–{refind_end} MiB" if use_refind else "") +
                 f", linux {linux_start}–{linux_end_str}")
        self.set_status("Creating partitions…")

        boot_fs_hint = "ext4" if self.ext4_boot_check.get_active() else "fat32"
        if is_gpt:
            parts_cmd = ["mkpart", "LINUX_LIVE", boot_fs_hint,
                         f"{boot_start}MiB", f"{boot_end}MiB"]
            if use_refind:
                parts_cmd += ["mkpart", "REFIND", "fat32",
                              f"{refind_start}MiB", f"{refind_end}MiB"]
            parts_cmd += ["mkpart", "linux", "ext4",
                          f"{linux_start}MiB", linux_end_str]
        else:
            parts_cmd = ["mkpart", "primary", boot_fs_hint,
                         f"{boot_start}MiB", f"{boot_end}MiB"]
            if use_refind:
                parts_cmd += ["mkpart", "primary", "fat32",
                              f"{refind_start}MiB", f"{refind_end}MiB"]
            parts_cmd += ["mkpart", "primary", "ext4",
                          f"{linux_start}MiB", linux_end_str]

        code, _, err = run(["parted", "-s", "--", disk_path] + parts_cmd)
        if code != 0:
            self.log(f"parted mkpart: {err} – retrying without FS hints", error=True)
            if is_gpt:
                parts_cmd2 = ["mkpart", "LINUX_LIVE",
                              f"{boot_start}MiB", f"{boot_end}MiB"]
                if use_refind:
                    parts_cmd2 += ["mkpart", "REFIND",
                                   f"{refind_start}MiB", f"{refind_end}MiB"]
                parts_cmd2 += ["mkpart", "linux",
                               f"{linux_start}MiB", linux_end_str]
            else:
                parts_cmd2 = ["mkpart", "primary",
                              f"{boot_start}MiB", f"{boot_end}MiB"]
                if use_refind:
                    parts_cmd2 += ["mkpart", "primary",
                                   f"{refind_start}MiB", f"{refind_end}MiB"]
                parts_cmd2 += ["mkpart", "primary",
                               f"{linux_start}MiB", linux_end_str]
            code, _, err2 = run(["parted", "-s", "--", disk_path] + parts_cmd2)
            if code != 0:
                self.log(f"Cannot create partitions: {err2}", error=True)
                return None

        time.sleep(2)
        run(["partprobe", disk_path])
        time.sleep(2)

        parts, _, _ = get_disk_partitions(disk_path)
        boot_num = linux_num = None
        refind_num = None
        for p in parts:
            if p["is_free"] or p["num"] == 0:
                continue
            if abs(p["start_mib"] - boot_start) <= 2:
                boot_num = p["num"]
            elif use_refind and abs(p["start_mib"] - refind_start) <= 2:
                refind_num = p["num"]
            elif abs(p["start_mib"] - linux_start) <= 2:
                linux_num = p["num"]

        if boot_num is None or linux_num is None or (use_refind and refind_num is None):
            self.log(f"Cannot identify new partitions (boot={boot_num}, "
                     f"refind={refind_num}, linux={linux_num}). "
                     f"Expected starts: {boot_start}, "
                     + (f"{refind_start}, " if use_refind else "")
                     + f"{linux_start} MiB", error=True)
            return None

        boot_dev = _part_dev_path(disk_path, boot_num)
        linux_dev = _part_dev_path(disk_path, linux_num)
        refind_dev = _part_dev_path(disk_path, refind_num) if use_refind else None
        self.log(f"Boot partition  : {boot_dev}")
        if refind_dev:
            self.log(f"rEFInd partition: {refind_dev}")
        self.log(f"Linux partition : {linux_dev}")
        return boot_dev, linux_dev, refind_dev

    def _format_and_populate_boot(self, boot_dev, iso_path, distro, distro_key):
        """Format boot_dev, mount it, copy ISO contents. Returns True on success."""
        use_ext4 = self.ext4_boot_check.get_active()
        if use_ext4:
            self.set_status("Formatting boot partition ext4…")
            code, _, err = run(["mkfs.ext4", "-F", "-L", "LINUX_LIVE", boot_dev])
            if code != 0:
                self.log(f"mkfs.ext4 failed: {err}", error=True)
                return False
        else:
            self.set_status("Formatting boot partition FAT32…")
            code, _, err = run(["mkfs.fat", "-F32", "-n", "LINUX_LIVE", boot_dev])
            if code != 0:
                self.log(f"mkfs.fat failed: {err}", error=True)
                return False

        mnt = "/mnt/linux_installer_boot"
        priv_makedirs(mnt)
        run(["mount", boot_dev, mnt])
        try:
            ok = self._copy_iso_to_mount(iso_path, mnt, distro, distro_key)
        finally:
            run(["umount", mnt])
        return ok

    def _finalize_strategy(self, boot_dev, linux_dev, refind_dev, distro_label):
        """Install rEFInd if enabled, or set direct UEFI boot entry. Log results."""
        self.log(f"Boot partition ready at {boot_dev}.")
        self.log(f"Linux partition at {linux_dev} – "
                 "the installer will format this during installation.")
        self._boot_part_dev = boot_dev
        if refind_dev and self._use_refind():
            self._install_refind(refind_dev, boot_dev, distro_label)
        else:
            # No rEFInd — set UEFI boot entry pointing directly at the
            # FAT32 LINUX_LIVE partition (firmware can read FAT32 natively)
            self._set_uefi_boot_entry_direct(boot_dev, distro_label)
        self._write_boot_instructions(
            boot_dev=boot_dev, linux_dev=linux_dev, distro_label=distro_label)

    # ── rEFInd download and install ───────────────────────────────────────────
    def _download_refind(self):
        """Download rEFInd binary zip. Returns path or None."""
        dest = str(iso_cache_dir() / REFIND_FILENAME)
        if os.path.exists(dest):
            self.log(f"Found cached rEFInd: {dest}")
            return dest
        self.log("Downloading rEFInd boot manager…")
        self.set_status("Downloading rEFInd…")
        try:
            req = urllib.request.Request(
                REFIND_URL, headers={"User-Agent": "linux-installer/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                done = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(1 << 17)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            GLib.idle_add(self.progress.set_fraction, done / total)
                            self.set_status(f"Downloading rEFInd… {done*100//total}%")
            GLib.idle_add(self.progress.set_fraction, 0)
            self.log(f"rEFInd downloaded: {round(os.path.getsize(dest)/1e6,1)} MB")
            return dest
        except Exception as e:
            self.log(f"rEFInd download failed: {e}", error=True)
            if os.path.exists(dest):
                os.unlink(dest)
            return None

    def _install_refind(self, refind_dev, boot_dev, distro_label):
        """Format rEFInd partition, extract rEFInd, write config, set UEFI entry."""
        self.log("")
        self.log("━━ Installing rEFInd boot manager ━━")

        refind_zip = self._download_refind()
        if not refind_zip:
            self.log("Cannot install rEFInd without the download.", error=True)
            return

        self.log(f"Formatting rEFInd partition {refind_dev} as FAT32…")
        self.set_status("Formatting rEFInd partition…")
        code, _, err = run(["mkfs.fat", "-F32", "-n", "REFIND", refind_dev])
        if code != 0:
            self.log(f"mkfs.fat failed: {err}", error=True)
            return

        refind_mnt = "/mnt/linux_installer_refind"
        priv_makedirs(refind_mnt)
        code, _, err = run(["mount", refind_dev, refind_mnt])
        if code != 0:
            self.log(f"Cannot mount rEFInd partition: {err}", error=True)
            return

        try:
            extract_dir = "/mnt/linux_installer_refind_extract"
            priv_makedirs(extract_dir)
            self.log("Extracting rEFInd…")
            self.set_status("Extracting rEFInd…")
            code, _, err = run(["unzip", "-o", refind_zip, "-d", extract_dir])
            if code != 0:
                self.log(f"unzip failed: {err}", error=True)
                return

            refind_src = os.path.join(extract_dir, "refind-bin-0.14.2", "refind")
            efi_boot = os.path.join(refind_mnt, "EFI", "BOOT")
            priv_makedirs(efi_boot)

            self.log("Copying rEFInd files…")
            self.set_status("Installing rEFInd…")

            # Copy refind_x64.efi as default UEFI loader
            run(["cp",
                 os.path.join(refind_src, "refind_x64.efi"),
                 os.path.join(efi_boot, "BOOTx64.EFI")])

            # Copy filesystem drivers (ext4 needed to read LINUX_LIVE)
            drivers_dst = os.path.join(efi_boot, "drivers_x64")
            priv_makedirs(drivers_dst)
            drivers_src = os.path.join(refind_src, "drivers_x64")
            for drv in ["ext4_x64.efi", "ext2_x64.efi"]:
                src = os.path.join(drivers_src, drv)
                code_chk, _, _ = run(["ls", src])
                if code_chk == 0:
                    run(["cp", src, drivers_dst])
                    self.log(f"  Copied driver: {drv}")

            # Copy icons
            icons_src = os.path.join(refind_src, "icons")
            icons_dst = os.path.join(efi_boot, "icons")
            code_chk, _, _ = run(["ls", icons_src])
            if code_chk == 0:
                run(["rsync", "-a", f"{icons_src}/", f"{icons_dst}/"])
                self.log("  Copied rEFInd icons.")

            # Detect boot layout on LINUX_LIVE
            boot_mnt_chk = "/mnt/linux_installer_refind_bootchk"
            priv_makedirs(boot_mnt_chk)
            code, _, _ = run(["mount", "-o", "ro", boot_dev, boot_mnt_chk])
            boot_mounted = (code == 0)

            has_pxeboot = has_casper = has_live = False
            extra_args = ""

            if boot_mounted:
                has_pxeboot = os.path.exists(
                    os.path.join(boot_mnt_chk, "images", "pxeboot", "vmlinuz"))
                has_casper = os.path.exists(
                    os.path.join(boot_mnt_chk, "casper", "vmlinuz"))
                has_live = os.path.exists(
                    os.path.join(boot_mnt_chk, "live", "vmlinuz"))

                # Extract kernel args from distro's grub.cfg
                for cfg_path in ["EFI/BOOT/grub.cfg", "boot/grub2/grub.cfg",
                                 "boot/grub/grub.cfg"]:
                    full = os.path.join(boot_mnt_chk, cfg_path)
                    if os.path.exists(full):
                        try:
                            content = priv_read_file(full)
                            for line in content.splitlines():
                                s = line.strip()
                                if s.startswith("linux") or s.startswith("linuxefi"):
                                    parts = s.split()
                                    args = []
                                    for p in parts[2:]:
                                        if p.startswith("root="):
                                            continue
                                        if "CDLABEL=" in p or "LABEL=" in p:
                                            p = re.sub(
                                                r'(CDLABEL=|LABEL=)\S+',
                                                r'\g<1>LINUX_LIVE', p)
                                        args.append(p)
                                    extra_args = " ".join(args)
                                    break
                        except Exception:
                            pass
                        if extra_args:
                            break
                run(["umount", boot_mnt_chk])

            # Write refind.conf
            self.log("Writing rEFInd configuration…")
            conf = (
                "# rEFInd configuration – generated by ULLI\n"
                "timeout 10\n"
                "use_graphics_for linux\n"
                "scanfor internal,external,manual\n"
                "scan_all_linux_kernels false\n"
                "\n")

            if has_pxeboot:
                if not extra_args:
                    extra_args = "rd.live.image rhgb quiet"
                conf += (
                    f'menuentry "{distro_label}" {{\n'
                    f'  volume LINUX_LIVE\n'
                    f'  loader /images/pxeboot/vmlinuz\n'
                    f'  initrd /images/pxeboot/initrd.img\n'
                    f'  options "root=live:LABEL=LINUX_LIVE {extra_args}"\n'
                    f'}}\n\n'
                    f'menuentry "{distro_label} (verbose)" {{\n'
                    f'  volume LINUX_LIVE\n'
                    f'  loader /images/pxeboot/vmlinuz\n'
                    f'  initrd /images/pxeboot/initrd.img\n'
                    f'  options "root=live:LABEL=LINUX_LIVE rd.live.image"\n'
                    f'}}\n')
            elif has_casper:
                if not extra_args:
                    extra_args = "quiet splash"
                conf += (
                    f'menuentry "{distro_label}" {{\n'
                    f'  volume LINUX_LIVE\n'
                    f'  loader /casper/vmlinuz\n'
                    f'  initrd /casper/initrd\n'
                    f'  options "boot=casper {extra_args}"\n'
                    f'}}\n')
            elif has_live:
                if not extra_args:
                    extra_args = "boot=live components quiet splash"
                conf += (
                    f'menuentry "{distro_label}" {{\n'
                    f'  volume LINUX_LIVE\n'
                    f'  loader /live/vmlinuz\n'
                    f'  initrd /live/initrd.img\n'
                    f'  options "{extra_args}"\n'
                    f'}}\n')
            else:
                conf += "# Unknown layout – rEFInd will auto-scan.\n"

            priv_write_file(os.path.join(efi_boot, "refind.conf"), conf)
            self.log("rEFInd configuration written.")

            self._set_uefi_boot_entry_refind(refind_dev, distro_label)
            self.log("rEFInd installed successfully.")
            self.log(f"  rEFInd partition: {refind_dev}")

        finally:
            run(["umount", refind_mnt])

    def _set_uefi_boot_entry_refind(self, refind_dev, distro_label):
        """Create UEFI NVRAM entry pointing at the rEFInd FAT32 partition."""
        if not os.path.isdir("/sys/firmware/efi"):
            self.log("System is not UEFI – skipping boot entry.")
            return
        if not shutil.which("efibootmgr"):
            self.log("efibootmgr not found.", error=True)
            return

        disk_dev, part_num = self._resolve_disk_and_part(refind_dev)
        if not disk_dev or not part_num:
            self.log(f"Cannot resolve disk/partition for {refind_dev}", error=True)
            return

        entry_name = "rEFInd – ULLI"
        loader = "\\EFI\\BOOT\\BOOTx64.EFI"

        # Remove existing ULLI rEFInd entries
        code, efi_out, _ = run(["efibootmgr", "-v"])
        if code == 0:
            for line in efi_out.splitlines():
                if "refind" in line.lower() and "ulli" in line.lower():
                    m = re.match(r"Boot(\w{4})", line)
                    if m:
                        self.log(f"Removing existing entry Boot{m.group(1)}")
                        run(["efibootmgr", "-b", m.group(1), "-B"])

        self.log(f"Creating UEFI boot entry: \"{entry_name}\"")
        self.log(f"  Disk: {disk_dev}  Partition: {part_num}  Loader: {loader}")

        code, out, err = run([
            "efibootmgr", "--create",
            "--disk", disk_dev,
            "--part", str(part_num),
            "--label", entry_name,
            "--loader", loader])
        if code != 0:
            self.log(f"efibootmgr --create failed: {err}", error=True)
            return

        new_boot_num = None
        m = re.search(r"Boot(\w{4})\*?\s+" + re.escape(entry_name), out)
        if m:
            new_boot_num = m.group(1)
        if not new_boot_num:
            code, efi_out, _ = run(["efibootmgr"])
            for line in efi_out.splitlines():
                if entry_name in line:
                    m = re.match(r"Boot(\w{4})", line)
                    if m:
                        new_boot_num = m.group(1)
                        break

        if new_boot_num:
            code, efi_out, _ = run(["efibootmgr"])
            boot_order = ""
            for line in efi_out.splitlines():
                if line.startswith("BootOrder:"):
                    boot_order = line.split(":")[1].strip()
                    break
            order_entries = [e.strip() for e in boot_order.split(",") if e.strip()]
            order_entries = [e for e in order_entries if e != new_boot_num]
            new_order = ",".join([new_boot_num] + order_entries)
            code, _, err = run(["efibootmgr", "-o", new_order])
            if code == 0:
                self.log(f"UEFI boot order set: {new_order}")
                self.log(f"Boot{new_boot_num} (\"{entry_name}\") is now the default.")
            else:
                self.log(f"Could not set boot order: {err}", error=True)
        else:
            self.log("UEFI entry created but could not determine boot number.")

        code, efi_out, _ = run(["efibootmgr"])
        if code == 0:
            self.log("Current UEFI boot entries:")
            for line in efi_out.splitlines():
                self.log(f"  {line}")

    def _set_uefi_boot_entry_direct(self, boot_part_dev, distro_label):
        """Create a UEFI boot entry pointing directly at the FAT32 LINUX_LIVE
        partition. Only works when the boot partition is FAT32."""
        self.log("Configuring UEFI boot entry (direct)…")
        self.set_status("Setting UEFI boot order…")

        if not os.path.isdir("/sys/firmware/efi"):
            self.log("System is not UEFI – skipping UEFI boot entry.")
            self.log("You may need to select the boot device manually from "
                     "your BIOS/legacy boot menu.")
            return

        if not shutil.which("efibootmgr"):
            self.log("efibootmgr not found. Install with: sudo apt install efibootmgr",
                     error=True)
            return

        disk_dev, part_num = self._resolve_disk_and_part(boot_part_dev)
        if not disk_dev or not part_num:
            self.log(f"Cannot resolve disk/partition for {boot_part_dev}", error=True)
            return

        # Mount the boot partition to find the EFI loader
        mnt = "/mnt/linux_installer_efi_check"
        priv_makedirs(mnt)
        code, _, _ = run(["mount", "-o", "ro", boot_part_dev, mnt])
        if code != 0:
            code, mnt_out, _ = run(["findmnt", "-n", "-o", "TARGET", boot_part_dev])
            if code == 0 and mnt_out:
                mnt = mnt_out.strip()
            else:
                self.log("Cannot mount boot partition to find EFI loader.", error=True)
                return

        efi_loader = None
        efi_search_paths = [
            "EFI/BOOT/BOOTx64.EFI", "EFI/BOOT/bootx64.efi",
            "EFI/BOOT/grubx64.efi", "EFI/boot/BOOTx64.EFI",
            "EFI/boot/bootx64.efi",
        ]
        for rel_path in efi_search_paths:
            full = os.path.join(mnt, rel_path)
            if os.path.exists(full):
                efi_loader = "\\" + rel_path.replace("/", "\\")
                break

        if not efi_loader:
            efi_dir = os.path.join(mnt, "EFI")
            if os.path.isdir(efi_dir):
                for root, dirs, files in os.walk(efi_dir):
                    for f in files:
                        if f.lower().endswith(".efi"):
                            rel = os.path.relpath(os.path.join(root, f), mnt)
                            efi_loader = "\\" + rel.replace("/", "\\")
                            break
                    if efi_loader:
                        break

        run(["umount", mnt])

        if not efi_loader:
            self.log("No EFI bootloader found on the boot partition.", error=True)
            self.log("You may need to boot from the UEFI firmware menu manually.")
            return

        self.log(f"Found EFI loader: {efi_loader}")

        entry_name = distro_label.split("–")[0].strip().rstrip('"').strip()

        # Remove existing entries with the same name
        code, efi_out, _ = run(["efibootmgr", "-v"])
        if code == 0:
            for line in efi_out.splitlines():
                if entry_name.lower() in line.lower():
                    m = re.match(r"Boot(\w{4})", line)
                    if m:
                        boot_num = m.group(1)
                        self.log(f"Removing existing UEFI entry Boot{boot_num}")
                        run(["efibootmgr", "-b", boot_num, "-B"])

        self.log(f"Creating UEFI boot entry: \"{entry_name}\"")
        self.log(f"  Disk: {disk_dev}  Partition: {part_num}  Loader: {efi_loader}")

        code, out, err = run([
            "efibootmgr", "--create",
            "--disk", disk_dev,
            "--part", str(part_num),
            "--label", entry_name,
            "--loader", efi_loader])
        if code != 0:
            self.log(f"efibootmgr --create failed: {err}", error=True)
            self.log("You may need to select the boot device from the UEFI firmware menu.")
            return

        new_boot_num = None
        m = re.search(r"Boot(\w{4})\*?\s+" + re.escape(entry_name), out)
        if m:
            new_boot_num = m.group(1)

        if not new_boot_num:
            code, efi_out, _ = run(["efibootmgr"])
            for line in efi_out.splitlines():
                if entry_name in line:
                    m = re.match(r"Boot(\w{4})", line)
                    if m:
                        new_boot_num = m.group(1)
                        break

        if new_boot_num:
            code, efi_out, _ = run(["efibootmgr"])
            boot_order = ""
            for line in efi_out.splitlines():
                if line.startswith("BootOrder:"):
                    boot_order = line.split(":")[1].strip()
                    break
            order_entries = [e.strip() for e in boot_order.split(",") if e.strip()]
            order_entries = [e for e in order_entries if e != new_boot_num]
            new_order = ",".join([new_boot_num] + order_entries)
            code, _, err = run(["efibootmgr", "-o", new_order])
            if code == 0:
                self.log(f"UEFI boot order set: {new_order}")
                self.log(f"Boot{new_boot_num} (\"{entry_name}\") is now the default.")
            else:
                self.log(f"Could not set boot order: {err}", error=True)
        else:
            self.log("UEFI entry created but could not determine its boot number.")
            self.log("Check with: sudo efibootmgr -v")

        code, efi_out, _ = run(["efibootmgr"])
        if code == 0:
            self.log("Current UEFI boot entries:")
            for line in efi_out.splitlines():
                self.log(f"  {line}")

    # ── btrfs strategy ────────────────────────────────────────────────────────
    def _strategy_btrfs(self, device, linux_mib, iso_path, distro, distro_key, custom_mode):
        """Shrink root btrfs, resize partition entry, create partitions."""
        self.log("")
        self.log("━━ Strategy: btrfs shrink + new partition ━━")

        boot_mib = gb_to_mib(self._boot_gb())
        refind_mib = REFIND_MIB if self._use_refind() else 0
        total_shrink_mib = linux_mib + boot_mib + refind_mib

        # Get btrfs usage
        self.set_status("Querying btrfs filesystem usage…")
        code, out, err = run(["btrfs", "filesystem", "usage", "-b", "/"])
        if code != 0:
            self.log(f"btrfs usage failed: {err}", error=True)
            return False

        dev_size = used = 0
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith("Device size:"):
                dev_size = int(stripped.split(":")[1].strip().split()[0])
            elif stripped.startswith("Used:"):
                used = int(stripped.split(":")[1].strip().split()[0])

        free_bytes = dev_size - used
        needed_bytes = mib_to_bytes(total_shrink_mib)
        safe_free = free_bytes - (10 * GiB)

        self.log(f"btrfs device size : {bytes_to_display_gb(dev_size)} GB")
        self.log(f"btrfs used        : {bytes_to_display_gb(used)} GB")
        self.log(f"Available to shrink: {bytes_to_display_gb(safe_free)} GB")

        if safe_free < needed_bytes:
            self.log(
                f"Not enough space! Need {mib_to_display_gb(total_shrink_mib)} GB, "
                f"have only {bytes_to_display_gb(safe_free)} GB safe to use.", error=True)
            return False

        new_fs_size_mib = bytes_to_mib(dev_size) - total_shrink_mib
        new_fs_size_bytes = mib_to_bytes(new_fs_size_mib)

        self.log(f"Shrinking btrfs to {mib_to_display_gb(new_fs_size_mib)} GB "
                 f"({new_fs_size_mib} MiB, {new_fs_size_bytes} bytes)…")
        self.set_status("Shrinking btrfs filesystem (this may take a while)…")

        code, out, err = run(["btrfs", "filesystem", "resize",
                               str(new_fs_size_bytes), "/"])
        if code != 0:
            self.log(f"btrfs resize failed: {err}", error=True)
            self.log("TIP: try running 'btrfs balance start /' first.", error=True)
            return False
        self.log("btrfs filesystem shrunk successfully.")

        # Verify shrink
        self.set_status("Verifying btrfs shrink…")
        code2, out2, _ = run(["btrfs", "filesystem", "usage", "-b", "/"])
        if code2 == 0:
            actual_size = 0
            for line in out2.splitlines():
                stripped = line.strip()
                if stripped.startswith("Device size:"):
                    actual_size = int(stripped.split(":")[1].strip().split()[0])
            if actual_size > 0:
                tolerance = 100 * MiB
                if actual_size > new_fs_size_bytes + tolerance:
                    self.log(
                        f"Post-shrink check failed: btrfs still reports "
                        f"{bytes_to_display_gb(actual_size)} GB, expected "
                        f"{bytes_to_display_gb(new_fs_size_bytes)} GB. "
                        f"Aborting to avoid partition table corruption.", error=True)
                    return False
                self.log(f"Post-shrink verified: btrfs is now "
                         f"{bytes_to_display_gb(actual_size)} GB.")

        # Find parent disk and partition
        disk_dev, part_num = self._resolve_disk_and_part(device)
        if not disk_dev:
            self.log("Cannot resolve parent disk for partition.", error=True)
            return False
        self.log(f"Disk: {disk_dev}  Partition: {part_num}")

        parts, disk_label, _ = get_disk_partitions(disk_dev)
        is_gpt = "gpt" in disk_label.lower()

        part_start_mib = part_end_mib = None
        next_part_start_mib = None
        for p in parts:
            if p["is_free"]:
                continue
            if p["num"] == part_num:
                part_start_mib = p["start_mib"]
                part_end_mib = p["end_mib"]
            elif p["num"] > part_num and next_part_start_mib is None:
                next_part_start_mib = p["start_mib"]

        if part_start_mib is None or part_end_mib is None:
            self.log("Cannot determine partition boundaries.", error=True)
            return False

        new_part_size_mib = new_fs_size_mib
        current_part_size_mib = part_end_mib - part_start_mib
        if new_part_size_mib > current_part_size_mib:
            self.log(f"New partition size ({new_part_size_mib} MiB) would exceed "
                     f"current ({current_part_size_mib} MiB). Aborting.", error=True)
            return False

        actual_new_end = self._resize_partition_entry(
            disk_dev, part_num, part_start_mib, new_part_size_mib)
        if actual_new_end is None:
            self.log("You may need to grow btrfs back: btrfs filesystem resize max /", error=True)
            return False

        boot_start = actual_new_end + 1
        boot_end = boot_start + boot_mib
        if next_part_start_mib is not None:
            linux_end_str = f"{next_part_start_mib - 1}MiB"
        else:
            linux_end_str = "100%"

        result = self._create_partitions(
            disk_dev, boot_start, boot_end, 0, linux_end_str, is_gpt)
        if result is None:
            return False
        boot_part_dev, linux_part_dev, refind_part_dev = result

        if not self._format_and_populate_boot(boot_part_dev, iso_path, distro, distro_key):
            return False

        self._finalize_strategy(boot_part_dev, linux_part_dev, refind_part_dev,
                                self._distro_label)
        return True

    # ── use-free-space strategy ───────────────────────────────────────────────
    def _strategy_use_free(self, disk_path, linux_mib, iso_path, distro,
                           distro_key, custom_mode):
        self.log("")
        self.log("━━ Strategy: use existing unallocated space ━━")

        boot_mib = gb_to_mib(self._boot_gb())
        refind_mib = REFIND_MIB if self._use_refind() else 0
        total_needed_mib = linux_mib + boot_mib + refind_mib

        parts, disk_label, _ = get_disk_partitions(disk_path)
        is_gpt = "gpt" in disk_label.lower()

        best_free = None
        for p in parts:
            if p["is_free"] and p["size_mib"] >= total_needed_mib:
                if best_free is None or p["size_mib"] > best_free["size_mib"]:
                    best_free = p

        if not best_free:
            self.log("No suitable unallocated region found on disk.", error=True)
            return False

        self.log(f"Using free region: {best_free['start_mib']}–{best_free['end_mib']} MiB "
                 f"({mib_to_display_gb(best_free['size_mib'])} GB)")

        boot_start = best_free["start_mib"] + 1
        boot_end = boot_start + boot_mib
        linux_end_str = f"{best_free['end_mib'] - 1}MiB"

        result = self._create_partitions(
            disk_path, boot_start, boot_end, 0, linux_end_str, is_gpt)
        if result is None:
            return False
        boot_dev, linux_dev, refind_dev = result

        if not self._format_and_populate_boot(boot_dev, iso_path, distro, distro_key):
            return False

        self._finalize_strategy(boot_dev, linux_dev, refind_dev, self._distro_label)
        return True

    # ── filesystem shrink helpers ─────────────────────────────────────────────
    def _shrink_btrfs(self, dev, shrink_mib):
        """Shrink a btrfs filesystem by shrink_mib MiB."""
        code, mnt_out, _ = run(["findmnt", "-n", "-o", "TARGET", dev])
        if code != 0 or not mnt_out.strip():
            tmp_mnt = "/mnt/linux_installer_shrink_target"
            priv_makedirs(tmp_mnt)
            code, _, err = run(["mount", dev, tmp_mnt])
            if code != 0:
                self.log(f"Cannot mount {dev}: {err}", error=True)
                return False
            mountpoint = tmp_mnt
            was_mounted = False
        else:
            mountpoint = mnt_out.strip()
            was_mounted = True

        try:
            self.set_status(f"Querying btrfs usage on {dev}…")
            code, out, err = run(["btrfs", "filesystem", "usage", "-b", mountpoint])
            if code != 0:
                self.log(f"btrfs usage failed: {err}", error=True)
                return False

            dev_size = 0
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("Device size:"):
                    dev_size = int(stripped.split(":")[1].strip().split()[0])

            new_fs_mib = bytes_to_mib(dev_size) - shrink_mib
            new_fs_bytes = mib_to_bytes(new_fs_mib)

            self.log(f"Shrinking btrfs from {bytes_to_display_gb(dev_size)} GB "
                     f"to {mib_to_display_gb(new_fs_mib)} GB ({new_fs_mib} MiB)")
            self.set_status(f"Shrinking btrfs on {dev}…")
            code, _, err = run(["btrfs", "filesystem", "resize",
                                 str(new_fs_bytes), mountpoint])
            if code != 0:
                self.log(f"btrfs resize failed: {err}", error=True)
                return False
            self.log("btrfs filesystem shrunk successfully.")

            # Verify
            code2, out2, _ = run(["btrfs", "filesystem", "usage", "-b", mountpoint])
            if code2 == 0:
                actual_size = 0
                for line in out2.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("Device size:"):
                        actual_size = int(stripped.split(":")[1].strip().split()[0])
                if actual_size > 0:
                    tolerance = 100 * MiB
                    if actual_size > new_fs_bytes + tolerance:
                        self.log(
                            f"Post-shrink check failed: btrfs still reports "
                            f"{bytes_to_display_gb(actual_size)} GB.", error=True)
                        return False
                    self.log(f"Post-shrink verified: btrfs is now "
                             f"{bytes_to_display_gb(actual_size)} GB.")
            return True
        finally:
            if not was_mounted:
                run(["umount", mountpoint])

    def _shrink_ext(self, dev, shrink_mib):
        """Shrink an ext2/3/4 filesystem. Must be unmounted."""
        code, mnt_out, _ = run(["findmnt", "-n", "-o", "TARGET", dev])
        if code == 0 and mnt_out.strip():
            self.log(f"{dev} is mounted at {mnt_out.strip()} — unmounting…")
            code, _, err = run(["umount", dev])
            if code != 0:
                self.log(f"Cannot unmount {dev}: {err}", error=True)
                return False

        self.set_status(f"Checking filesystem on {dev}…")
        self.log(f"Running e2fsck on {dev}…")
        code, out, err = run(["e2fsck", "-f", "-y", dev])
        if code not in (0, 1):  # 1 = errors fixed
            self.log(f"e2fsck failed ({code}): {err}", error=True)
            return False

        code, out, _ = run(["dumpe2fs", "-h", dev])
        block_size = block_count = 0
        for line in out.splitlines():
            if line.startswith("Block size:"):
                block_size = int(line.split(":")[1].strip())
            elif line.startswith("Block count:"):
                block_count = int(line.split(":")[1].strip())

        if block_size == 0 or block_count == 0:
            self.log("Cannot determine ext filesystem size.", error=True)
            return False

        current_size = block_size * block_count
        shrink_bytes = mib_to_bytes(shrink_mib)
        new_size = current_size - shrink_bytes
        new_blocks = new_size // block_size

        self.log(f"ext filesystem: {bytes_to_display_gb(current_size)} GB → "
                 f"{bytes_to_display_gb(new_size)} GB ({new_blocks} blocks)")

        self.set_status(f"Shrinking ext filesystem on {dev}…")
        code, _, err = run(["resize2fs", dev, f"{new_blocks}"])
        if code != 0:
            self.log(f"resize2fs failed: {err}", error=True)
            self.log(f"You may need to grow back: resize2fs {dev}", error=True)
            return False
        self.log("ext filesystem shrunk successfully.")

        # Verify
        code2, out2, _ = run(["dumpe2fs", "-h", dev])
        if code2 == 0:
            actual_bs = actual_bc = 0
            for line in out2.splitlines():
                if line.startswith("Block size:"):
                    actual_bs = int(line.split(":")[1].strip())
                elif line.startswith("Block count:"):
                    actual_bc = int(line.split(":")[1].strip())
            if actual_bs > 0 and actual_bc > 0:
                actual_size = actual_bs * actual_bc
                tolerance = 100 * MiB
                if actual_size > new_size + tolerance:
                    self.log(f"Post-shrink check failed.", error=True)
                    return False
                self.log(f"Post-shrink verified: ext is now "
                         f"{bytes_to_display_gb(actual_size)} GB.")
        return True

    def _shrink_ntfs(self, dev, shrink_mib):
        """Shrink NTFS using ntfsresize. Must be unmounted."""
        code, mnt_out, _ = run(["findmnt", "-n", "-o", "TARGET", dev])
        if code == 0 and mnt_out.strip():
            self.log(f"{dev} is mounted — unmounting…")
            code, _, err = run(["umount", dev])
            if code != 0:
                self.log(f"Cannot unmount {dev}: {err}", error=True)
                return False

        if not shutil.which("ntfsresize"):
            self.log("ntfsresize not found. Install ntfs-3g.", error=True)
            return False

        self.set_status(f"Querying NTFS info on {dev}…")
        code, out, err = run(["ntfsresize", "--info", "--force", dev])
        if code != 0:
            self.log(f"ntfsresize --info failed: {err}", error=True)
            return False

        current_size = 0
        for line in out.splitlines():
            if "Current volume size" in line and "bytes" in line:
                current_size = _parse_bytes_value(line)
                break
        if current_size == 0:
            self.log("Cannot determine NTFS volume size.", error=True)
            return False

        shrink_bytes = mib_to_bytes(shrink_mib)
        new_size = current_size - shrink_bytes
        if new_size < GiB:
            self.log(f"New NTFS size too small: {bytes_to_display_gb(new_size)} GB", error=True)
            return False

        self.log(f"NTFS: {bytes_to_display_gb(current_size)} GB → "
                 f"{bytes_to_display_gb(new_size)} GB")

        # Dry run
        self.set_status(f"Testing NTFS resize on {dev}…")
        code, out, err = run(["ntfsresize", "--no-action", "--size",
                               str(new_size), "--force", dev])
        if code != 0:
            self.log(f"ntfsresize dry run failed: {err}", error=True)
            return False
        self.log("Dry run OK, proceeding with actual resize…")

        self.set_status(f"Shrinking NTFS on {dev}…")
        code, out, err = run(["ntfsresize", "--size", str(new_size), "--force", dev])
        if code != 0:
            self.log(f"ntfsresize failed: {err}", error=True)
            self.log("You may need to run chkdsk from Windows.", error=True)
            return False
        self.log("NTFS filesystem shrunk successfully.")

        # Verify
        code2, out2, _ = run(["ntfsresize", "--info", "--force", dev])
        if code2 == 0:
            actual_size = 0
            for line in out2.splitlines():
                if "Current volume size" in line and "bytes" in line:
                    actual_size = _parse_bytes_value(line)
                    break
            if actual_size > 0:
                tolerance = 100 * MiB
                if actual_size > new_size + tolerance:
                    self.log(f"Post-shrink check failed.", error=True)
                    return False
                self.log(f"Post-shrink verified: NTFS is now "
                         f"{bytes_to_display_gb(actual_size)} GB.")
        return True

    # ── other-disk-shrink strategy ────────────────────────────────────────────
    def _strategy_other_disk_shrink(self, disk_path, shrink_dev, shrink_mib,
                                     linux_mib, iso_path, distro, distro_key,
                                     custom_mode):
        self.log("")
        self.log("━━ Strategy: shrink partition on another disk ━━")
        self.log(f"Target disk: {disk_path}")
        self.log(f"Shrinking: {shrink_dev} by {mib_to_display_gb(shrink_mib)} GB "
                 f"({shrink_mib} MiB)")

        boot_mib = gb_to_mib(self._boot_gb())
        refind_mib = REFIND_MIB if self._use_refind() else 0
        total_needed_mib = linux_mib + boot_mib + refind_mib

        fstype = get_partition_fstype(shrink_dev)
        if fstype not in ("btrfs", "ext4", "ext3", "ext2", "ntfs"):
            self.log(f"Cannot shrink {shrink_dev}: filesystem is {fstype}.", error=True)
            return False

        shrink_fn = {"btrfs": self._shrink_btrfs, "ntfs": self._shrink_ntfs}.get(
            fstype, self._shrink_ext)
        if not shrink_fn(shrink_dev, shrink_mib):
            return False

        _, part_num = self._resolve_disk_and_part(shrink_dev)
        if not part_num:
            self.log(f"Cannot resolve partition number for {shrink_dev}", error=True)
            return False

        parts, disk_label, _ = get_disk_partitions(disk_path)
        is_gpt = "gpt" in disk_label.lower()

        part_start_mib = part_end_mib = None
        next_part_start_mib = None
        for p in parts:
            if p["is_free"]:
                continue
            if p["num"] == part_num:
                part_start_mib = p["start_mib"]
                part_end_mib = p["end_mib"]
            elif p["num"] > part_num and next_part_start_mib is None:
                next_part_start_mib = p["start_mib"]

        if part_start_mib is None or part_end_mib is None:
            self.log("Cannot determine partition boundaries.", error=True)
            return False

        new_part_size_mib = part_end_mib - part_start_mib - total_needed_mib
        actual_new_end = self._resize_partition_entry(
            disk_path, part_num, part_start_mib, new_part_size_mib)
        if actual_new_end is None:
            return False

        boot_start = actual_new_end + 1
        boot_end = boot_start + boot_mib
        linux_end_str = f"{next_part_start_mib - 1}MiB" if next_part_start_mib else "100%"

        result = self._create_partitions(
            disk_path, boot_start, boot_end, 0, linux_end_str, is_gpt)
        if result is None:
            return False
        boot_dev, linux_dev, refind_dev = result

        if not self._format_and_populate_boot(boot_dev, iso_path, distro, distro_key):
            return False

        self._finalize_strategy(boot_dev, linux_dev, refind_dev, self._distro_label)
        return True

    # ── wipe-disk strategy ────────────────────────────────────────────────────
    def _strategy_wipe_disk(self, disk_path, linux_mib, iso_path, distro,
                            distro_key, custom_mode):
        self.log("")
        self.log("━━ Strategy: wipe & reformat entire disk ━━")
        self.log(f"Target disk: {disk_path}")

        boot_mib = gb_to_mib(self._boot_gb())
        esp_mib = 512
        use_refind = self._use_refind()
        refind_mib = REFIND_MIB if use_refind else 0

        # Safety check
        root_info = get_root_fs_info()
        if root_info:
            root_disk, _ = self._resolve_disk_and_part(root_info["device"])
            if root_disk and root_disk == disk_path:
                self.log("REFUSING to wipe the disk containing the running OS!", error=True)
                return False

        # Unmount everything on the target disk
        self.log("Unmounting any mounted partitions on target disk…")
        self.set_status("Unmounting target disk…")
        parts, _, _ = get_disk_partitions(disk_path)
        for p in parts:
            if p["is_free"] or p["num"] == 0:
                continue
            dev_p = _part_dev_path(disk_path, p["num"])

            code, swap_out, _ = run(["swapon", "--show=NAME", "--noheadings"])
            if code == 0 and dev_p in swap_out:
                self.log(f"  Deactivating swap on {dev_p}")
                run(["swapoff", dev_p])

            code, mnt_out, _ = run(["findmnt", "-n", "-o", "TARGET", dev_p])
            if code == 0 and mnt_out.strip():
                self.log(f"  Unmounting {dev_p} from {mnt_out.strip()}")
                code, _, err = run(["umount", "-f", dev_p])
                if code != 0:
                    self.log(f"  Force unmount failed, trying lazy…")
                    code, _, err = run(["umount", "-l", dev_p])
                    if code != 0:
                        self.log(f"  Failed to unmount {dev_p}: {err}", error=True)
                        return False

        # Remove device-mapper references
        for p in parts:
            if p["is_free"] or p["num"] == 0:
                continue
            dev_p = _part_dev_path(disk_path, p["num"])
            dev_name = os.path.basename(dev_p)
            holders_dir = f"/sys/class/block/{dev_name}/holders"
            if os.path.isdir(holders_dir):
                for holder in os.listdir(holders_dir):
                    self.log(f"  Removing device-mapper mapping: {holder}")
                    run(["dmsetup", "remove", "--force", holder])

        run(["partprobe", disk_path])
        time.sleep(1)

        # Inhibit automounting
        udev_rule_path = "/run/udev/rules.d/99-ulli-inhibit.rules"
        disk_basename = os.path.basename(disk_path)
        udev_rule_installed = False
        udisks_was_running = False

        try:
            priv_makedirs("/run/udev/rules.d")
            udev_content = (
                f'SUBSYSTEM=="block", KERNEL=="{disk_basename}*", '
                f'ENV{{UDISKS_IGNORE}}="1", ENV{{UDISKS_AUTO}}="0"\n')
            priv_write_file(udev_rule_path, udev_content)
            run(["udevadm", "control", "--reload-rules"])
            udev_rule_installed = True
            self.log("Automount inhibit rule installed.")
        except Exception as e:
            self.log(f"Note: could not set udev inhibit rule: {e}")

        if shutil.which("systemctl"):
            code, _, _ = run(["systemctl", "is-active", "--quiet", "udisks2"])
            if code == 0:
                self.log("Stopping udisks2…")
                run(["systemctl", "stop", "udisks2"])
                udisks_was_running = True

        try:
            # Wipe signatures
            if shutil.which("wipefs"):
                self.log(f"Wiping filesystem signatures on {disk_path}…")
                run(["wipefs", "--all", "--force", disk_path])
                for p in parts:
                    if p["is_free"] or p["num"] == 0:
                        continue
                    dev_p = _part_dev_path(disk_path, p["num"])
                    if os.path.exists(dev_p):
                        run(["wipefs", "--all", "--force", dev_p])
            time.sleep(1)

            # Create GPT
            self.log(f"Creating new GPT partition table on {disk_path}…")
            self.set_status("Creating new partition table…")
            code, _, err = run(["parted", "-s", disk_path, "mklabel", "gpt"])
            if code != 0:
                self.log(f"parted mklabel failed: {err}", error=True)
                if shutil.which("sgdisk"):
                    self.log("Trying sgdisk fallback…")
                    run(["sgdisk", "--zap-all", disk_path])
                    code3, _, err3 = run(["sgdisk", "-o", disk_path])
                    if code3 != 0:
                        self.log(f"sgdisk failed: {err3}", error=True)
                        return False
                else:
                    return False

            run(["partprobe", disk_path])
            time.sleep(1)

            # Partition layout
            esp_start = 1
            esp_end = esp_start + esp_mib
            boot_start = esp_end
            boot_end = boot_start + boot_mib
            # rEFInd goes at the END of the disk so free space is contiguous
            if use_refind:
                _, _, disk_size_mib = get_disk_partitions(disk_path)
                refind_end = disk_size_mib - 1  # leave 1 MiB for GPT backup
                refind_start = refind_end - REFIND_MIB
            else:
                refind_start = refind_end = 0

            self.log(f"Creating ESP: {esp_start}–{esp_end} MiB")
            self.log(f"Creating boot: {boot_start}–{boot_end} MiB")
            if use_refind:
                self.log(f"Creating rEFInd: {refind_start}–{refind_end} MiB")
            self.set_status("Creating partitions…")

            # Create ESP
            code, _, err = run(["parted", "-s", "--", disk_path,
                                "mkpart", "EFI", "fat32",
                                f"{esp_start}MiB", f"{esp_end}MiB",
                                "set", "1", "esp", "on"])
            if code != 0:
                self.log(f"Failed to create ESP: {err}", error=True)
                return False

            # Create boot partition
            boot_fs_hint = "ext2" if self.ext4_boot_check.get_active() else "fat32"
            code, _, err = run(["parted", "-s", "--", disk_path,
                                "mkpart", "LINUX_LIVE", boot_fs_hint,
                                f"{boot_start}MiB", f"{boot_end}MiB"])
            if code != 0:
                self.log(f"Failed to create boot partition: {err}", error=True)
                return False

            # Create rEFInd partition at end of disk if enabled
            if use_refind:
                code, _, err = run(["parted", "-s", "--", disk_path,
                                    "mkpart", "REFIND", "fat32",
                                    f"{refind_start}MiB", f"{refind_end}MiB"])
                if code != 0:
                    self.log(f"Failed to create rEFInd partition: {err}", error=True)
                    return False

            time.sleep(1)
            run(["partprobe", disk_path])
            if shutil.which("udevadm"):
                run(["udevadm", "settle", "--timeout=10"])
            time.sleep(1)

            esp_dev = _part_dev_path(disk_path, 1)
            boot_dev = _part_dev_path(disk_path, 2)
            refind_dev = _part_dev_path(disk_path, 3) if use_refind else None

            devs_to_prep = [esp_dev, boot_dev]
            if refind_dev:
                devs_to_prep.append(refind_dev)

            for dev in devs_to_prep:
                if not os.path.exists(dev):
                    time.sleep(3)
                    run(["partprobe", disk_path])
                    if shutil.which("udevadm"):
                        run(["udevadm", "settle", "--timeout=10"])
                    time.sleep(2)
                    if not os.path.exists(dev):
                        self.log(f"Partition device {dev} not found.", error=True)
                        return False

            # Force-release and wipe
            for dev in devs_to_prep:
                run(["umount", "-f", dev])
                if shutil.which("fuser"):
                    run(["fuser", "-k", dev])
                dev_name = os.path.basename(dev)
                holders_dir = f"/sys/class/block/{dev_name}/holders"
                if os.path.isdir(holders_dir):
                    for holder in os.listdir(holders_dir):
                        run(["dmsetup", "remove", "--force", holder])

            time.sleep(1)
            for dev in devs_to_prep:
                run(["dd", "if=/dev/zero", f"of={dev}",
                     "bs=1M", "count=2", "conv=notrunc", "status=none"])
            run(["partprobe", disk_path])
            if shutil.which("udevadm"):
                run(["udevadm", "settle", "--timeout=5"])
            time.sleep(1)

            # Format ESP
            self.log(f"Formatting ESP ({esp_dev}) as FAT32…")
            fmt_ok = False
            for attempt in range(4):
                code, _, err = run(["mkfs.fat", "-F32", "-n", "EFI", esp_dev])
                if code == 0:
                    fmt_ok = True; break
                self.log(f"  Format attempt {attempt+1}/4 failed: {err}")
                run(["umount", "-f", esp_dev])
                if shutil.which("fuser"):
                    run(["fuser", "-k", esp_dev])
                run(["dd", "if=/dev/zero", f"of={esp_dev}",
                     "bs=1M", "count=1", "conv=notrunc", "status=none"])
                time.sleep(2)
            if not fmt_ok:
                self.log(f"ESP format failed: {err}", error=True)
                return False

            # Format LINUX_LIVE
            self.log(f"Formatting boot partition ({boot_dev})…")
            fmt_ok = False
            for attempt in range(4):
                if self.ext4_boot_check.get_active():
                    code, _, err = run(["mkfs.ext4", "-F", "-L", "LINUX_LIVE", boot_dev])
                else:
                    code, _, err = run(["mkfs.fat", "-F32", "-n", "LINUX_LIVE", boot_dev])
                if code == 0:
                    fmt_ok = True; break
                self.log(f"  Format attempt {attempt+1}/4 failed: {err}")
                run(["umount", "-f", boot_dev])
                if shutil.which("fuser"):
                    run(["fuser", "-k", boot_dev])
                run(["dd", "if=/dev/zero", f"of={boot_dev}",
                     "bs=1M", "count=1", "conv=notrunc", "status=none"])
                time.sleep(2)
            if not fmt_ok:
                self.log(f"Boot format failed: {err}", error=True)
                return False

        finally:
            if udev_rule_installed:
                try:
                    priv_unlink(udev_rule_path)
                    run(["udevadm", "control", "--reload-rules"])
                    self.log("Automount inhibit rule removed.")
                except Exception:
                    pass
            if udisks_was_running:
                self.log("Restarting udisks2…")
                run(["systemctl", "start", "udisks2"])

        # Mount and copy ISO
        mnt = "/mnt/linux_installer_boot"
        priv_makedirs(mnt)
        run(["mount", boot_dev, mnt])
        try:
            ok = self._copy_iso_to_mount(iso_path, mnt, distro, distro_key)
        finally:
            run(["umount", mnt])

        if not ok:
            return False

        # Log final layout
        self.log("")
        boot_fs = "ext4" if self.ext4_boot_check.get_active() else "FAT32"
        self.log(f"Disk {disk_path} wiped and reformatted:")
        self.log(f"  Partition 1: {esp_dev}  – ESP (512 MB, FAT32)")
        self.log(f"  Partition 2: {boot_dev} – LINUX_LIVE "
                 f"({mib_to_display_gb(boot_mib)} GB, {boot_fs})")
        if refind_dev:
            self.log(f"  Partition 3: {refind_dev} – rEFInd ({REFIND_MIB} MB, FAT32)")
        disk_total_mib = get_disk_partitions(disk_path)[2]
        remaining_mib = disk_total_mib - esp_mib - boot_mib - refind_mib
        if remaining_mib > 0:
            self.log(f"  Remaining:   ~{mib_to_display_gb(remaining_mib)} GB unallocated")

        self._boot_part_dev = boot_dev
        if use_refind and refind_dev:
            self._install_refind(refind_dev, boot_dev, self._distro_label)
        else:
            self._set_uefi_boot_entry_direct(boot_dev, self._distro_label)
        self._write_boot_instructions(
            boot_dev=boot_dev,
            linux_dev=f"{disk_path} (remaining unallocated space)",
            distro_label=self._distro_label)
        return True

    # ── ISO copy / GRUB integration ───────────────────────────────────────────
    def _copy_iso_to_mount(self, iso_path, mnt, distro, distro_key):
        """Mount ISO read-only and rsync its contents to mnt."""
        iso_mnt = "/mnt/linux_installer_iso_copy"
        priv_makedirs(iso_mnt)

        hybrid = distro.get("hybrid", False)

        if not hybrid:
            code, _, err = run(["mount", "-o", "loop,ro", iso_path, iso_mnt])
            if code != 0:
                self.log(f"Cannot mount ISO: {err}", error=True)
                return False

        try:
            if hybrid:
                self.log("Hybrid ISO detected – extracting with 7z…")
                code, _, err = run(["7z", "x", f"-o{mnt}", iso_path, "-y"],
                                   capture_output=False)
                if code not in (0, 1):
                    self.log(f"7z extraction failed ({code}): {err}", error=True)
                    return False
            else:
                self.log("Copying ISO contents to boot partition (10–20 min)…")
                self.set_status("Copying files…")

                exclude_args = []
                try:
                    for entry in os.listdir(iso_mnt):
                        full = os.path.join(iso_mnt, entry)
                        if os.path.islink(full):
                            target = os.path.realpath(full)
                            if os.path.realpath(iso_mnt) == target:
                                exclude_args += ["--exclude", f"/{entry}"]
                                self.log(f"Skipping self-referential symlink: {entry} -> .")
                except OSError:
                    pass

                code, _, err = run(
                    ["rsync", "-a", "--copy-links", "--info=progress2"]
                    + exclude_args +
                    [f"{iso_mnt}/", f"{mnt}/"],
                    capture_output=False)
                if code != 0:
                    self.log(f"rsync failed: {err}", error=True)
                    return False

            if distro_key == "fedora":
                self._patch_fedora_labels(mnt, "LINUX_LIVE")

        finally:
            if not hybrid:
                run(["umount", iso_mnt])

        self.log("ISO contents copied.")
        return True

    def _patch_fedora_labels(self, mnt, label):
        self.log(f"Patching Fedora boot config labels → {label}")
        cfg_files = [
            f"{mnt}/EFI/BOOT/grub.cfg",
            f"{mnt}/boot/grub2/grub.cfg",
            f"{mnt}/isolinux/isolinux.cfg",
        ]
        for p in cfg_files:
            if not os.path.exists(p):
                continue
            content = priv_read_file(p)
            patched = re.sub(r"(root=live:(?:CD)?LABEL=)(\S+)", rf"\g<1>{label}", content)
            patched = re.sub(r"(set isolabel=)(\S+)", rf"\g<1>{label}", patched)
            if patched != content:
                priv_write_file(p, patched)
                self.log(f"  Patched: {Path(p).name}")

    def _update_grub(self):
        self.log("Updating GRUB…")
        self.set_status("Running GRUB update…")

        candidates = []
        if os.path.isdir("/sys/firmware/efi"):
            candidates.append((
                ["grub2-mkconfig", "-o", "/boot/efi/EFI/fedora/grub.cfg"],
                "Fedora/RHEL UEFI grub2-mkconfig"))
            candidates.append((
                ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"],
                "Arch/CachyOS grub-mkconfig"))

        candidates.append((
            ["grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"],
            "grub2-mkconfig (BIOS / generic)"))
        candidates.append((
            ["update-grub"],
            "update-grub (Debian/Ubuntu)"))
        candidates.append((
            ["grub2-mkconfig", "-o", "/boot/grub/grub.cfg"],
            "grub2-mkconfig (openSUSE / generic)"))

        for cmd, desc in candidates:
            if not shutil.which(cmd[0]):
                continue
            self.log(f"Trying: {desc}  ({' '.join(cmd)})")
            code, _, err = run(cmd)
            if code == 0:
                self.log(f"GRUB updated successfully via: {desc}")
                return True
            self.log(f"  → returned {code}: {err.splitlines()[0] if err else '(no output)'}")

        self.log("Could not update GRUB automatically.", error=True)
        self.log("Run one of the following manually after reboot:")
        self.log("  Fedora/RHEL (UEFI):  sudo grub2-mkconfig -o /boot/efi/EFI/fedora/grub.cfg")
        self.log("  Arch/CachyOS:        sudo grub-mkconfig -o /boot/grub/grub.cfg")
        self.log("  Debian/Ubuntu:       sudo update-grub")
        return False

    # ── helpers ───────────────────────────────────────────────────────────────
    def _resolve_disk_and_part(self, device):
        m = re.match(r"^(/dev/(?:nvme\d+n\d+|[a-z]+))p?(\d+)$", device)
        if m:
            return m.group(1), int(m.group(2))
        return None, None

    def _write_boot_instructions(self, boot_dev, linux_dev, distro_label):
        dest = Path.home() / "Desktop" / "Linux_Installer_Instructions.txt"
        use_ext4 = self.ext4_boot_check.get_active()
        fs_type = "ext4" if use_ext4 else "FAT32"

        if self._use_refind():
            boot_method = (
                "rEFInd boot manager has been installed and set as the default UEFI boot entry.\n"
                "\n"
                "To boot the live environment:\n"
                "  1. Restart your computer.\n"
                f'  2. rEFInd should appear automatically and show "{distro_label}".\n'
                "  3. If rEFInd doesn't appear, enter UEFI/BIOS (F2/F10/F12/DEL)\n"
                '     and select "rEFInd – ULLI" from the boot menu.\n'
                "  4. Disable Secure Boot if needed.\n")
        else:
            boot_method = (
                "To boot the live environment:\n"
                "  1. Restart your computer.\n"
                "  2. Enter UEFI/BIOS (F2 / F10 / F12 / DEL / ESC at POST).\n"
                f"  3. Set Boot Order to prioritise: {boot_dev}\n"
                "  4. Disable Secure Boot if enabled.\n"
                "  5. Save & exit – the system boots into the live environment.\n")

        body = f"""
Linux Installer – Boot Instructions
====================================
Distro:          {distro_label}
Boot partition:  {boot_dev}  ({fs_type} – contains live ISO files)
Linux partition: {linux_dev}  (for the installer)

{boot_method}
During installation the installer will auto-detect {linux_dev}
as free space and offer "Install alongside existing Linux".
"""
        try:
            dest.write_text(body)
            self.log(f"Instructions written to {dest}")
        except Exception:
            pass

    def _do_restart(self):
        self.log("Restarting in 15 seconds… (close this window to cancel)")
        self.set_status("Restarting in 15 seconds…")
        for i in range(15, 0, -1):
            if self.cancel_restart:
                self.log("Restart cancelled.")
                return
            self.set_status(f"Restarting in {i} seconds…")
            time.sleep(1)
        run(["reboot"])


# ─── entry point ──────────────────────────────────────────────────────────────

def check_deps():
    missing = []
    for tool in ["parted", "rsync", "mkfs.ext4", "mkfs.fat",
                 "btrfs", "blkid",
                 "sfdisk", "resize2fs", "e2fsck", "lsblk",
                 "ntfsresize", "unzip"]:
        if shutil.which(tool) is None:
            missing.append(tool)
    grub_tools = ["update-grub", "grub2-mkconfig", "grub-mkconfig"]
    if not any(shutil.which(t) for t in grub_tools):
        missing.append("grub tool (one of: update-grub / grub2-mkconfig / grub-mkconfig)")
    return missing


if __name__ == "__main__":
    if "--check-deps" in sys.argv:
        m = check_deps()
        if m:
            print("Missing tools:", ", ".join(m))
            print("Install with:")
            print("  Debian/Ubuntu:  sudo apt install " +
                  "parted rsync dosfstools e2fsprogs btrfs-progs grub-common " +
                  "fdisk util-linux ntfs-3g unzip")
            print("  Fedora/RHEL:    sudo dnf install " +
                  "parted rsync dosfstools e2fsprogs btrfs-progs grub2-tools " +
                  "util-linux ntfsprogs efibootmgr unzip")
            print("  Arch/CachyOS:   sudo pacman -S " +
                  "parted rsync dosfstools e2fsprogs btrfs-progs grub " +
                  "util-linux ntfs-3g efibootmgr unzip")
        else:
            print("All dependencies satisfied.")
        sys.exit(0)

    app = InstallerApp()
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    sys.exit(app.run(sys.argv))
