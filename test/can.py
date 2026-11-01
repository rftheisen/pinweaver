"""CAN 2.0A frames and a bus model with a receiving node, for the tests.

Frame (base format): SOF, 11-bit ID, RTR, IDE, r0, 4-bit DLC, data, CRC-15,
all MSB first and bit-stuffed (after 5 equal bits, the opposite bit), then
CRC delimiter, ACK slot, ACK delimiter, 7 bits of EOF (no stuffing).
Dominant = 0, recessive = 1; the bus is a wired AND.
"""
from cocotb.triggers import RisingEdge


def crc15(bits):
    crc = 0
    for b in bits:
        fb = b ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if fb:
            crc ^= 0x4599
    return crc


def frame_bits(ident, data):
    """SOF .. data, unstuffed."""
    bits = [0] + [(ident >> (10 - i)) & 1 for i in range(11)] + [0, 0, 0]
    bits += [(len(data) >> (3 - i)) & 1 for i in range(4)]
    for b in data:
        bits += [(b >> (7 - i)) & 1 for i in range(8)]
    return bits


def host_bytes(ident, data):
    """What the host pushes to can_tx: bit count - 1, then the bits packed MSB first."""
    bits = frame_bits(ident, data)
    padded = bits + [0] * (-len(bits) % 8)
    packed = [sum(padded[i + k] << (7 - k) for k in range(8)) for i in range(0, len(padded), 8)]
    return [len(bits) - 1] + packed


class CanBus:
    """Wired-AND bus over the engines' TX pins, driving the shared RX pin, plus
    one receiving node that decodes frames and sends ACK."""

    def __init__(self, dut, pins, tx_pins, rx_pin, clocks_per_bit=100, ack=True):
        self.dut, self.pins = dut, pins
        self.tx_pins, self.rx_pin = tx_pins, rx_pin
        self.bit = clocks_per_bit
        self.ack = ack
        self.frames = []        # (ident, data, crc_ok, stuff_bits)
        self.errors = []
        self.ack_low = False

    def _start_frame(self, t):
        self.state, self.phase = "frame", "stuffed"
        self.next_sample = t + self.bit // 2
        self.bits, self.tail, self.last, self.run = [], [], None, 0
        self.stuffed, self.n_bits, self.crc_done = 0, None, False
        self.ack_start = self.ack_end = None

    def _bit(self, b, t):
        if self.phase == "tail":                     # no stuffing from here on
            self.tail.append(b)
            n = len(self.tail)
            if n == 1:                               # CRC delimiter
                if b != 1:
                    self.errors.append("CRC delimiter not recessive")
                if self.ack:                         # pull the next bit (ACK slot) low
                    self.ack_start = t + self.bit // 2
                    self.ack_end = self.ack_start + self.bit
            elif n == 3 and b != 1:
                self.errors.append("ACK delimiter not recessive")
            elif n > 3 and b != 1:
                self.errors.append("EOF not recessive")
            if n == 10:
                self._finish()
            return
        if self.run == 5:                            # a stuff bit: drop it
            if b == self.last:
                self.errors.append("missing stuff bit")
            self.last, self.run = b, 1
            self.stuffed += 1
            if self.crc_done:
                self.phase = "tail"
            return
        if self.crc_done:                            # no stuff bit after the CRC
            self.phase = "tail"
            self._bit(b, t)
            return
        self.run = self.run + 1 if b == self.last else 1
        self.last = b
        self.bits.append(b)
        if len(self.bits) == 19:
            dlc = sum(self.bits[15 + k] << (3 - k) for k in range(4))
            self.n_bits = 19 + 8 * min(dlc, 8) + 15
        if self.n_bits and len(self.bits) == self.n_bits:
            self.crc_done = True

    def _finish(self):
        bits = self.bits
        ident = sum(bits[1 + k] << (10 - k) for k in range(11))
        dlc = sum(bits[15 + k] << (3 - k) for k in range(4))
        data = [sum(bits[19 + 8 * j + k] << (7 - k) for k in range(8)) for j in range(min(dlc, 8))]
        crc = sum(bits[-15 + k] << (14 - k) for k in range(15))
        self.frames.append((ident, data, crc == crc15(bits[:-15]), self.stuffed))
        self.state, self.quiet = "idle", 0

    async def run(self):
        p = self.pins
        self.state, self.quiet, prev, t = "idle", 0, 1, 0
        while True:
            await RisingEdge(self.dut.clk)
            t += 1
            bus = 1
            for tx in self.tx_pins:
                if p.oe(tx) and p.out(tx) == 0:
                    bus = 0
            if self.ack_low:
                bus = 0
            p.drive(self.rx_pin, bus)
            if self.state == "idle":
                if bus == 0 and prev == 1:
                    self._start_frame(t)
            else:
                if prev == 1 and bus == 0 and not self.ack_low:
                    self.next_sample = t + self.bit // 2   # resynchronise on falling edges
                if t == self.next_sample:
                    self.next_sample += self.bit
                    self._bit(bus, t)
                if self.ack_start is not None:
                    if t == self.ack_start:
                        self.ack_low = True
                    elif t == self.ack_end:
                        self.ack_low = False
                        self.ack_start = None
            prev = bus
