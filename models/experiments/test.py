import serial, time

PORT = "/dev/ttyACM0"   # change to your actual device if different

ser = serial.Serial(
    PORT,
    baudrate=115200,     # ignored by USB-ACM, fine to leave
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=0            # non-blocking
)

ser.reset_input_buffer()

buf = b""
while True:
    chunk = ser.read(4096)
    if chunk:
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip(b"\r")
            if not line:
                continue
            # timestamp + raw line
            print(f"{time.time():.9f}")
            print(line)
            # If you only want GGA lines:
            # if line.startswith(b"$GNGGA") or line.startswith(b"$GPGGA"):
            #     print(f"{time.time():.9f}", line)