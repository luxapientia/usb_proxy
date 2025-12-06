# Python USB Proxy - Quick Start

## Why Python Version?

This lightweight Python implementation solves the "operation took too long" timeout errors experienced with the C++ version by:

- **Simpler architecture**: No complex injection or MITM features
- **Faster startup**: Less initialization overhead
- **Better timeout handling**: Python's exception handling makes timeouts easier to manage
- **Easier debugging**: Print statements for real-time monitoring

## Installation

```bash
# Install dependencies
sudo apt install python3-pip
pip3 install -r requirements.txt

# Make script executable
chmod +x usb-proxy.py
```

## Usage

### For Raspberry Pi 4 with POS and Printer

```bash
# Find your printer's VID and PID
lsusb
# Example output: Bus 001 Device 003: ID 04b8:0e28 Seiko Epson Corp. TM-T82

# Run the proxy
sudo python3 usb-proxy.py \
    --vendor_id=04b8 \
    --product_id=0e28 \
    --device=fe980000.usb \
    --driver=fe980000.usb
```

### Connection Setup

```
Printer (Epson) --USB--> RPi Host Port
RPi OTG Port --USB--> POS Terminal
```

## How It Works

1. **Device Side**: Uses PyUSB to read from AND write to the printer (bidirectional)
2. **Host Side**: Uses raw-gadget to emulate the printer to the POS
3. **Bidirectional Forwarding**:
   - **IN endpoints** (printer → POS): Read from device, write to host
   - **OUT endpoints** (POS → printer): Read from host, write to device
4. **Configuration Detection**: Automatically finds correct config by matching SET_CONFIGURATION value
5. **Class Requests**: Forwards ALL control requests (standard/class/vendor)

## Differences from C++ Version

| Feature | C++ Version | Python Version |
|---------|-------------|----------------|
| Injection/MITM | ✅ Full support | ❌ Not included |
| Isochronous transfers | ⚠️ Partial | ❌ Not included |
| **Bulk transfers (IN+OUT)** | ✅ Yes | ✅ **Yes - Full bidirectional** |
| **Interrupt transfers (IN+OUT)** | ✅ Yes | ✅ **Yes - Full bidirectional** |
| Control transfers | ✅ Full | ✅ **All types (STD/CLASS/VENDOR)** |
| **Configuration detection** | ✅ Yes | ✅ **Auto-detect by value** |
| Speed | Fast | **Faster startup** |
| Memory | Lower | Very low |
| Debugging | Verbose logs | **Direction-labeled prints** |
| Code size | ~1500 lines | ~400 lines |

## Troubleshooting

**Permission denied on /dev/raw-gadget:**
```bash
sudo chmod 666 /dev/raw-gadget
# Or run with sudo
```

**Device not found:**
```bash
# Check device is connected
lsusb

# Try without VID/PID to use first available device
sudo python3 usb-proxy.py --device=fe980000.usb --driver=fe980000.usb
```

**Timeout errors:**
- The Python version has 100ms read timeouts (adjustable in code)
- Check printer is powered and responding
- Verify USB cable quality

## Performance Tips

1. **Reduce logging**: Comment out print statements in `endpoint_reader` and `endpoint_writer` for production use
2. **Adjust timeouts**: Modify timeout values in `endpoint_reader()` if needed
3. **Use Python 3.9+**: Newer Python versions have better performance

## Code Customization

**Change read timeout** (line ~200):
```python
data = self.usb_device.read(ep_addr, 4096, timeout=100)  # 100ms
```

**Change queue size** (line ~225):
```python
queue = Queue(maxsize=10)  # Limit queue size
```

**Add logging to file**:
```python
import logging
logging.basicConfig(filename='usb-proxy.log', level=logging.INFO)
logging.info(f"Read {len(data)} bytes from EP{hex(ep_addr)}")
```

## License

Same as parent project (Apache 2.0)
