"""A small, independent model of the low-speed USB wire format, for the tests.

Packet bits: SYNC (0x80), PID, payload, CRC16 (low byte first), all LSB first,
bit-stuffed (a 0 after six 1s), NRZI (0 = change level), then EOP
(two bit times of SE0 and one of J). Low speed: J = D- high, D+ low.
"""

J, K, SE0 = "J", "K", "0"


def crc16_usb(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def packet_bytes(pid, payload):
    crc = crc16_usb(payload)
    return [0x80, pid] + list(payload) + [crc & 0xFF, crc >> 8]


def to_bits(data):
    return [(b >> i) & 1 for b in data for i in range(8)]


def stuff(bits):
    out, run = [], 0
    for b in bits:
        out.append(b)
        run = run + 1 if b else 0
        if run == 6:
            out.append(0)
            run = 0
    return out


def encode(pid, payload):
    """Line states, one per bit time, from SYNC to the end of EOP."""
    level, states = J, []
    for b in stuff(to_bits(packet_bytes(pid, payload))):
        if b == 0:
            level = K if level == J else J
        states.append(level)
    return states + [SE0, SE0, J]


def state(d_minus, d_plus):
    return {(1, 0): J, (0, 1): K, (0, 0): SE0}.get((d_minus, d_plus), "1")


class DecodeError(AssertionError):
    pass


def decode(samples, clocks_per_bit, slack=0.3):
    """Decode one packet from per-clock line states. Checks bit timing, NRZI,
    stuffing, SYNC, EOP and the CRC. Returns (pid, payload, info)."""
    # runs of equal states
    runs = []
    for s in samples:
        if runs and runs[-1][0] == s:
            runs[-1][1] += 1
        else:
            runs.append([s, 1])
    while runs and runs[0][0] == J:        # idle before the packet
        runs.pop(0)
    if not runs or runs[0][0] != K:
        raise DecodeError(f"no SYNC: {runs[:4]}")
    bits, eop = [], None
    for i, (s, n) in enumerate(runs):
        nb = n / clocks_per_bit
        k = round(nb)
        if s == SE0:
            eop = (nb, runs[i + 1] if i + 1 < len(runs) else None)
            break
        if s not in (J, K):
            raise DecodeError(f"bad line state {s!r}")
        if k < 1 or abs(nb - k) > slack:
            raise DecodeError(f"run of {n} clocks is not a whole number of bits ({nb:.2f})")
        bits += [0] + [1] * (k - 1)        # a level change is a 0, staying is a 1
    if eop is None:
        raise DecodeError("no EOP")
    se0_bits, after = eop
    if abs(se0_bits - 2) > slack:
        raise DecodeError(f"SE0 lasted {se0_bits:.2f} bits")
    if after is None or after[0] != J:
        raise DecodeError("no J after SE0")
    # undo stuffing
    data, run, i, stuffed = [], 0, 0, 0
    while i < len(bits):
        b = bits[i]
        data.append(b)
        run = run + 1 if b else 0
        i += 1
        if run == 6:
            if i < len(bits) and bits[i] != 0:
                raise DecodeError("stuff bit missing")
            i += 1
            stuffed += 1
            run = 0
    if len(data) % 8:
        raise DecodeError(f"{len(data)} bits is not whole bytes")
    by = [sum(data[j + k] << k for k in range(8)) for j in range(0, len(data), 8)]
    if by[0] != 0x80:
        raise DecodeError(f"SYNC was {by[0]:#04x}")
    pid, payload, crc = by[1], by[2:-2], by[-2] | (by[-1] << 8)
    if crc16_usb(payload) != crc:
        raise DecodeError(f"CRC {crc:#06x}, expected {crc16_usb(payload):#06x}")
    if (pid & 0xF) != (~pid >> 4) & 0xF:
        raise DecodeError(f"bad PID {pid:#04x}")
    return pid, payload, {"stuffed": stuffed, "bits": len(bits)}
