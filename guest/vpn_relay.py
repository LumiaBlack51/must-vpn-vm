"""Run only inside the guest via pinned SSH. Bind every target socket to utun."""
import os
import re
import select
import socket
import subprocess
import sys


def connect_vpn(host, port, timeout):
    addresses = dict.fromkeys(x[4][0] for x in socket.getaddrinfo(
        host, None, socket.AF_INET, socket.SOCK_STREAM))
    for address in addresses:
        route = subprocess.run(['ip', '-4', 'route', 'get', address],
                               capture_output=True, text=True, timeout=3)
        device = re.search(r' dev (utun[0-9]+)(?: |$)', route.stdout)
        if route.returncode != 0 or device is None: continue
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Device binding remains on the socket even if the VPN route changes.
            # Failure to bind or connect never retries through the physical NIC.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                            device[1].encode('ascii') + b'\0')
            sock.settimeout(timeout)
            sock.connect((address, port)); sock.settimeout(None)
            return sock
        except BaseException:
            sock.close(); raise
    raise OSError('Target has no guest VPN route.')


def stream(sock):
    reading = [0, sock]
    while reading:
        ready, _, _ = select.select(reading, [], [])
        for source in ready:
            data = os.read(0, 65536) if source == 0 else sock.recv(65536)
            if not data:
                reading.remove(source)
                if source == 0: sock.shutdown(socket.SHUT_WR)
            elif source == 0: sock.sendall(data)
            else:
                while data:
                    written = os.write(1, data)
                    data = data[written:]
        # Remote EOF ends the SSH command; do not wait indefinitely on local stdin.
        if sock not in reading: return


if __name__ == '__main__':
    with connect_vpn(sys.argv[1], int(sys.argv[2]), float(sys.argv[3])) as connection:
        os.write(1, b'\0')  # Success marker consumed by the host before SOCKS success.
        stream(connection)
