"""Notify systemd from the working loop so a stuck loop cannot feed its watchdog."""
import os
import socket
import time


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET", "")
    if os.name == "nt" or not address or not hasattr(socket, "AF_UNIX"):
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.settimeout(1)
            client.sendto(message.encode(), address)
    except OSError:
        pass


def supervised_wait(stop, delay, *, wake=None):
    deadline = time.monotonic() + delay
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        notify("WATCHDOG=1")
        stop.wait(min(30, remaining))
        if not stop.is_set() and wake and wake():
            return
