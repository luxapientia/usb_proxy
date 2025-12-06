#!/usr/bin/env python3
"""
Lightweight USB Proxy for forwarding data between POS (host) and Printer (device)
Simpler and faster alternative to the C++ implementation
"""

import os
import sys
import argparse
import struct
import threading
import time
from datetime import datetime
import usb.core
import usb.util
from queue import Queue, Empty

# Helper function for timestamped logging
def log(msg, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{timestamp}] [{level}] {msg}")
    sys.stdout.flush()

# Helper to calculate ioctl numbers (like Linux _IOW, _IOR, _IO macros)
def _IOC(direction, ioctl_type, nr, size):
    return (direction << 30) | (ord(ioctl_type) << 8) | (nr << 0) | (size << 16)

def _IO(ioctl_type, nr):
    return _IOC(0, ioctl_type, nr, 0)

def _IOW(ioctl_type, nr, size):
    return _IOC(1, ioctl_type, nr, size)

def _IOR(ioctl_type, nr, size):
    return _IOC(2, ioctl_type, nr, size)

def _IOWR(ioctl_type, nr, size):
    return _IOC(3, ioctl_type, nr, size)

# raw-gadget constants - properly calculated
UDC_NAME_LENGTH_MAX = 128
USB_RAW_INIT_SIZE = UDC_NAME_LENGTH_MAX + UDC_NAME_LENGTH_MAX + 1  # 257
USB_RAW_IOCTL_INIT = _IOW('U', 0, USB_RAW_INIT_SIZE)
USB_RAW_IOCTL_RUN = _IO('U', 1)
USB_RAW_IOCTL_EVENT_FETCH = _IOR('U', 2, 8)  # struct base: type(4) + length(4)
USB_RAW_IOCTL_EP0_WRITE = _IOW('U', 3, 8)  # struct base: ep(2) + flags(2) + length(4)
USB_RAW_IOCTL_EP0_READ = _IOWR('U', 4, 8)
USB_RAW_IOCTL_EP_ENABLE = _IOW('U', 5, 9)
USB_RAW_IOCTL_EP_DISABLE = _IOW('U', 6, 4)
USB_RAW_IOCTL_EP_WRITE = _IOW('U', 7, 8)
USB_RAW_IOCTL_EP_READ = _IOWR('U', 8, 8)
USB_RAW_IOCTL_CONFIGURE = _IO('U', 9)
USB_RAW_IOCTL_VBUS_DRAW = _IOW('U', 10, 4)
USB_RAW_IOCTL_EPS_INFO = _IOR('U', 11, 1920)
USB_RAW_IOCTL_EP0_STALL = _IO('U', 12)
USB_RAW_IOCTL_EP_SET_HALT = _IOW('U', 13, 4)

# USB Event types
USB_RAW_EVENT_CONNECT = 1
USB_RAW_EVENT_CONTROL = 2
USB_RAW_EVENT_SUSPEND = 3
USB_RAW_EVENT_RESUME = 4
USB_RAW_EVENT_RESET = 5
USB_RAW_EVENT_DISCONNECT = 6

# USB Speed
USB_SPEED_HIGH = 3

# USB Request types
USB_DIR_OUT = 0x00
USB_DIR_IN = 0x80
USB_TYPE_STANDARD = 0x00
USB_TYPE_MASK = 0x60
USB_REQ_GET_STATUS = 0x00
USB_REQ_CLEAR_FEATURE = 0x01
USB_REQ_SET_FEATURE = 0x03
USB_REQ_SET_ADDRESS = 0x05
USB_REQ_GET_DESCRIPTOR = 0x06
USB_REQ_SET_DESCRIPTOR = 0x07
USB_REQ_GET_CONFIGURATION = 0x08
USB_REQ_SET_CONFIGURATION = 0x09
USB_REQ_GET_INTERFACE = 0x0a
USB_REQ_SET_INTERFACE = 0x0b

# USB Descriptor types
USB_DT_DEVICE = 0x01
USB_DT_CONFIG = 0x02
USB_DT_STRING = 0x03

# Endpoint types
USB_ENDPOINT_XFER_CONTROL = 0
USB_ENDPOINT_XFER_ISOC = 1
USB_ENDPOINT_XFER_BULK = 2
USB_ENDPOINT_XFER_INT = 3


class USBProxy:
    def __init__(self, vendor_id=None, product_id=None, device="dummy_udc.0", driver="dummy_udc"):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.device_name = device
        self.driver_name = driver
        self.usb_device = None
        self.gadget_fd = None
        self.running = True
        self.endpoint_threads = []
        self.endpoint_queues = {}
        # Cached descriptors (like C++ version)
        self.device_descriptor = None
        self.config_descriptors = {}  # key: config_index, value: bytes
        self.string_descriptors = {}  # key: string_index, value: bytes
        # Hotplug state tracking
        self.host_connected = False
        self.device_configured = False
        self.endpoints_running = True  # Flag for endpoint threads
        
    def find_and_open_device(self):
        """Find and open the target USB device"""
        log("="*60, "INFO")
        log("DEVICE DISCOVERY", "INFO")
        log("="*60, "INFO")
        
        if self.vendor_id and self.product_id:
            log(f"Searching for device {hex(self.vendor_id)}:{hex(self.product_id)}...", "INFO")
            dev = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id)
        else:
            log("Searching for any USB device...", "INFO")
            dev = usb.core.find()
        
        if dev is None:
            log("ERROR: Device not found!", "ERROR")
            raise ValueError("Device not found")
        
        log(f"✓ Found device: {hex(dev.idVendor)}:{hex(dev.idProduct)}", "SUCCESS")
        log(f"  Manufacturer: {usb.util.get_string(dev, dev.iManufacturer) if dev.iManufacturer else 'N/A'}", "INFO")
        log(f"  Product: {usb.util.get_string(dev, dev.iProduct) if dev.iProduct else 'N/A'}", "INFO")
        log(f"  Configurations: {dev.bNumConfigurations}", "INFO")
        self.usb_device = dev
        
        # Detach kernel driver if necessary
        if dev.is_kernel_driver_active(0):
            log("  Detaching kernel driver...", "INFO")
            dev.detach_kernel_driver(0)
            log("  ✓ Kernel driver detached", "SUCCESS")
        
        # Reset device before proxy (like C++ version - device-libusb.cpp line 146)
        # This ensures device is in a clean state
        try:
            log("  Resetting device...", "INFO")
            dev.reset()
            log("  ✓ Device reset", "SUCCESS")
        except Exception as e:
            log(f"  Warning: Device reset failed: {e} (continuing anyway)", "WARN")
        
        # Check that device is responsive (like C++ version - device-libusb.cpp line 155-162)
        try:
            log("  Checking device responsiveness...", "INFO")
            # Try to read string descriptor 0 (language IDs)
            unused = dev.ctrl_transfer(0x80, 0x06, 0x0300, 0x0409, 4, timeout=1000)
            log("  ✓ Device is responsive", "SUCCESS")
        except Exception as e:
            log(f"  ERROR: Device unresponsive: {e}", "ERROR")
            raise ValueError(f"Device unresponsive: {e}")
        
        # Cache all descriptors BEFORE host connects (like C++ version)
        log("="*60, "INFO")
        log("CACHING DEVICE DESCRIPTORS", "INFO")
        log("="*60, "INFO")
        self.cache_descriptors(dev)
        
        return dev
    
    def cache_descriptors(self, dev):
        """Cache all device descriptors before host connects (like C++ setup_host_usb_desc)"""
        try:
            # Cache device descriptor
            log("Reading device descriptor...", "INFO")
            self.device_descriptor = dev.ctrl_transfer(
                0x80,  # bmRequestType: IN, Standard, Device
                0x06,  # bRequest: GET_DESCRIPTOR
                0x0100,  # wValue: Device descriptor, index 0
                0,  # wIndex: 0
                18,  # wLength: Device descriptor is 18 bytes
                timeout=1000
            )
            log(f"✓ Cached device descriptor ({len(self.device_descriptor)} bytes)", "SUCCESS")
            
            # Cache all configuration descriptors
            log(f"Reading {dev.bNumConfigurations} configuration descriptor(s)...", "INFO")
            for cfg_idx in range(dev.bNumConfigurations):
                # First, get the config descriptor length (first 9 bytes)
                cfg_header = dev.ctrl_transfer(
                    0x80,  # bmRequestType: IN, Standard, Device
                    0x06,  # bRequest: GET_DESCRIPTOR
                    0x0200 | cfg_idx,  # wValue: Config descriptor, index cfg_idx
                    0,  # wIndex: 0
                    9,  # wLength: Config header is 9 bytes (includes wTotalLength)
                    timeout=1000
                )
                
                if len(cfg_header) >= 9:
                    # Extract total length from bytes 2-3 (little endian)
                    total_length = struct.unpack("<H", bytes(cfg_header[2:4]))[0]
                    
                    # Now read the full config descriptor
                    full_cfg = dev.ctrl_transfer(
                        0x80,  # bmRequestType: IN, Standard, Device
                        0x06,  # bRequest: GET_DESCRIPTOR
                        0x0200 | cfg_idx,  # wValue: Config descriptor, index cfg_idx
                        0,  # wIndex: 0
                        total_length,  # wLength: Full config descriptor
                        timeout=1000
                    )
                    self.config_descriptors[cfg_idx] = bytes(full_cfg)
                    log(f"✓ Cached config descriptor {cfg_idx} ({len(full_cfg)} bytes)", "SUCCESS")
            
            # Cache string descriptors (if available)
            log("Caching string descriptors...", "INFO")
            if hasattr(dev, 'iManufacturer') and dev.iManufacturer:
                try:
                    str_desc = dev.ctrl_transfer(
                        0x80,  # bmRequestType: IN, Standard, Device
                        0x06,  # bRequest: GET_DESCRIPTOR
                        0x0300 | dev.iManufacturer,  # wValue: String descriptor
                        0x0409,  # wIndex: English (US)
                        255,  # wLength: Max string length
                        timeout=1000
                    )
                    self.string_descriptors[dev.iManufacturer] = bytes(str_desc)
                    log(f"✓ Cached string descriptor {dev.iManufacturer}", "SUCCESS")
                except:
                    pass
            
            if hasattr(dev, 'iProduct') and dev.iProduct:
                try:
                    str_desc = dev.ctrl_transfer(
                        0x80,  # bmRequestType: IN, Standard, Device
                        0x06,  # bRequest: GET_DESCRIPTOR
                        0x0300 | dev.iProduct,  # wValue: String descriptor
                        0x0409,  # wIndex: English (US)
                        255,  # wLength: Max string length
                        timeout=1000
                    )
                    self.string_descriptors[dev.iProduct] = bytes(str_desc)
                    log(f"✓ Cached string descriptor {dev.iProduct}", "SUCCESS")
                except:
                    pass
            
            if hasattr(dev, 'iSerialNumber') and dev.iSerialNumber:
                try:
                    str_desc = dev.ctrl_transfer(
                        0x80,  # bmRequestType: IN, Standard, Device
                        0x06,  # bRequest: GET_DESCRIPTOR
                        0x0300 | dev.iSerialNumber,  # wValue: String descriptor
                        0x0409,  # wIndex: English (US)
                        255,  # wLength: Max string length
                        timeout=1000
                    )
                    self.string_descriptors[dev.iSerialNumber] = bytes(str_desc)
                    log(f"✓ Cached string descriptor {dev.iSerialNumber}", "SUCCESS")
                except:
                    pass
            
            log("✓ All descriptors cached successfully", "SUCCESS")
            log("="*60, "INFO")
            
        except Exception as e:
            log(f"ERROR: Failed to cache descriptors: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            raise
    
    def open_raw_gadget(self):
        """Open /dev/raw-gadget"""
        log("="*60, "INFO")
        log("GADGET INITIALIZATION", "INFO")
        log("="*60, "INFO")
        log("Opening /dev/raw-gadget...", "INFO")
        self.gadget_fd = os.open("/dev/raw-gadget", os.O_RDWR)
        log(f"✓ Gadget FD: {self.gadget_fd}", "SUCCESS")
        return self.gadget_fd
    
    def init_gadget(self):
        """Initialize the gadget with device/driver names"""
        log(f"Initializing gadget with driver='{self.driver_name}', device='{self.device_name}'...", "INFO")
        
        import fcntl
        
        # Create struct: driver_name[128] + device_name[128] + speed[1]
        # Use struct.pack with proper null-padding
        driver_padded = self.driver_name.encode('utf-8')[:127].ljust(128, b'\x00')
        device_padded = self.device_name.encode('utf-8')[:127].ljust(128, b'\x00')
        
        init_struct = driver_padded + device_padded + bytes([USB_SPEED_HIGH])
        
        log(f"  Struct size: {len(init_struct)} bytes", "INFO")
        
        try:
            result = fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_INIT, init_struct)
            log(f"✓ Gadget initialized (USB_SPEED_HIGH), result={result}", "SUCCESS")
        except OSError as e:
            log(f"Failed to initialize gadget: {e}", "ERROR")
            log(f"  Driver: '{self.driver_name}' ({len(self.driver_name)} chars)", "ERROR")
            log(f"  Device: '{self.device_name}' ({len(self.device_name)} chars)", "ERROR")
            log(f"  IOCTL number: {hex(USB_RAW_IOCTL_INIT)}", "ERROR")
            raise
    
    def run_gadget(self):
        """Start the gadget"""
        log("Starting gadget...", "INFO")
        import fcntl
        fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_RUN)
        log("✓ Gadget is now running and waiting for host", "SUCCESS")
        log("="*60, "INFO")
    
    def ensure_clean_connection(self):
        """Ensure we start with a clean disconnect/reconnect cycle"""
        log("="*60, "INFO")
        log("ENSURING CLEAN CONNECTION CYCLE", "INFO")
        log("="*60, "INFO")
        log("Checking for existing connection...", "INFO")
        
        import time
        
        try:
            # Fetch the first event (blocking call)
            # If we get CONNECT immediately, we were already connected
            event_type, event_data = self.fetch_event()
            
            if event_type == USB_RAW_EVENT_CONNECT:
                log("⚠ Already connected to POS - waiting for disconnect...", "WARN")
                # Wait for DISCONNECT or RESET event
                while self.running:
                    event_type, event_data = self.fetch_event()
                    if event_type == USB_RAW_EVENT_DISCONNECT:
                        log("✓ Disconnected from POS", "SUCCESS")
                        # Wait a moment for disconnect to fully propagate
                        time.sleep(0.2)
                        break
                    elif event_type == USB_RAW_EVENT_RESET:
                        # Reset also means disconnected
                        log("✓ Reset received (treated as disconnect)", "SUCCESS")
                        time.sleep(0.2)
                        break
                    elif event_type == 0:
                        # Invalid event - continue waiting
                        continue
                    else:
                        # Other event - log and continue waiting for disconnect
                        event_names = {1: "CONNECT", 2: "CONTROL", 3: "SUSPEND", 
                                     4: "RESUME", 5: "RESET", 6: "DISCONNECT"}
                        event_name = event_names.get(event_type, f"UNKNOWN({event_type})")
                        log(f"  Received {event_name} while waiting for disconnect, continuing...", "INFO")
            elif event_type == USB_RAW_EVENT_DISCONNECT:
                log("✓ Already disconnected - ready for fresh connection", "SUCCESS")
            elif event_type == USB_RAW_EVENT_RESET:
                log("✓ Reset event received - ready for fresh connection", "SUCCESS")
            elif event_type == USB_RAW_EVENT_CONTROL:
                # CONTROL event means we're connected but didn't get CONNECT
                # This can happen - wait for disconnect first
                log("⚠ CONTROL event received (connection active) - waiting for disconnect...", "WARN")
                while self.running:
                    event_type, event_data = self.fetch_event()
                    if event_type == USB_RAW_EVENT_DISCONNECT or event_type == USB_RAW_EVENT_RESET:
                        log("✓ Disconnected from POS", "SUCCESS")
                        time.sleep(0.2)
                        break
                    elif event_type == 0:
                        continue
            else:
                # Some other event - log it
                event_names = {1: "CONNECT", 2: "CONTROL", 3: "SUSPEND", 
                             4: "RESUME", 5: "RESET", 6: "DISCONNECT"}
                event_name = event_names.get(event_type, f"UNKNOWN({event_type})")
                log(f"  Received {event_name} event, will handle in main loop", "INFO")
        except Exception as e:
            log(f"Error checking connection state: {e} (continuing anyway)", "WARN")
        
        # Now wait for a fresh CONNECT event
        log("Waiting for POS to connect...", "INFO")
        while self.running:
            event_type, event_data = self.fetch_event()
            if event_type == USB_RAW_EVENT_CONNECT:
                log("="*60, "SUCCESS")
                log("✓ FRESH CONNECTION FROM POS DETECTED", "SUCCESS")
                log("="*60, "SUCCESS")
                self.host_connected = True
                break
            elif event_type == USB_RAW_EVENT_DISCONNECT:
                log("  Disconnect received, still waiting for connect...", "INFO")
                continue
            elif event_type == USB_RAW_EVENT_RESET:
                log("  Reset received, still waiting for connect...", "INFO")
                continue
            elif event_type == 0:
                # Invalid event - continue waiting
                continue
            elif event_type == USB_RAW_EVENT_CONTROL:
                # CONTROL event means we're connected but didn't get CONNECT
                # This can happen - proceed
                log("  CONTROL event received (connection active), proceeding...", "INFO")
                self.host_connected = True
                break
            else:
                # Other event - log and continue waiting
                event_names = {1: "CONNECT", 2: "CONTROL", 3: "SUSPEND", 
                             4: "RESUME", 5: "RESET", 6: "DISCONNECT"}
                event_name = event_names.get(event_type, f"UNKNOWN({event_type})")
                log(f"  Received {event_name} while waiting for connect, continuing...", "INFO")
        
        log("="*60, "INFO")
        log("Ready to process USB events", "INFO")
        log("="*60, "INFO")
    
    def fetch_event(self):
        """Fetch USB event from raw-gadget"""
        import fcntl
        
        # struct usb_raw_event: u32 type, u32 length, u8 data[0]
        # We need to allocate space for type + length + data
        # Allocate 8 bytes for type+length, plus 4096 for event data
        event_buffer = bytearray(8 + 4096)
        
        try:
            fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_EVENT_FETCH, event_buffer, True)
        except OSError as e:
            log(f"fetch_event ioctl failed: {e}", "ERROR")
            # Return invalid event to trigger cleanup
            return (0, b'')
        
        # Parse the event structure
        event_type = struct.unpack("<I", bytes(event_buffer[0:4]))[0]
        event_length = struct.unpack("<I", bytes(event_buffer[4:8]))[0]
        
        # Guard against invalid length
        if event_length > 4096:
            log(f"Invalid event_length: {event_length}", "WARN")
            event_length = 0
        
        event_data = bytes(event_buffer[8:8+event_length])
        return event_type, event_data
    
    def ep0_read(self, length):
        """Read from EP0"""
        import fcntl
        # struct usb_raw_ep_io: u16 ep, u16 flags, u32 length, u8 data[]
        buffer = bytearray(8 + length)
        struct.pack_into("<HHI", buffer, 0, 0, 0, length)
        try:
            fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_EP0_READ, buffer, True)
            return bytes(buffer[8:8+length])
        except OSError as e:
            log(f"ep0_read failed: {e}", "ERROR")
            return b''
    
    def ep0_write(self, data):
        """Write to EP0"""
        import fcntl
        # struct usb_raw_ep_io: u16 ep, u16 flags, u32 length, u8 data[]
        buffer = bytearray(8 + len(data))
        struct.pack_into("<HHI", buffer, 0, 0, 0, len(data))
        buffer[8:] = data
        try:
            return fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_EP0_WRITE, buffer)
        except OSError as e:
            log(f"ep0_write failed: {e}", "ERROR")
            return -1
    
    def ep0_stall(self):
        """Stall EP0"""
        import fcntl
        fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_EP0_STALL)
    
    def ep_enable(self, descriptor):
        """Enable an endpoint - returns endpoint number"""
        import fcntl
        # descriptor should be 7 bytes for endpoint descriptor
        try:
            ep_num = fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_EP_ENABLE, descriptor)
            return ep_num
        except OSError as e:
            log(f"ep_enable failed: {e}", "ERROR")
            return -1
    
    def ep_read(self, ep_num, length):
        """Read from endpoint"""
        import fcntl
        # struct usb_raw_ep_io: u16 ep, u16 flags, u32 length, u8 data[]
        buffer = bytearray(8 + length)
        struct.pack_into("<HHI", buffer, 0, ep_num, 0, length)
        try:
            result = fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_EP_READ, buffer, True)
            # ioctl returns number of bytes read
            if result > 0:
                return bytes(buffer[8:8+result])
            else:
                # Parse length field from returned buffer
                actual_len = struct.unpack("<I", bytes(buffer[4:8]))[0]
                return bytes(buffer[8:8+actual_len])
        except OSError as e:
            # This can happen on timeout or device disconnect
            return b''
    
    def ep_write(self, ep_num, data):
        """Write to endpoint"""
        import fcntl
        # struct usb_raw_ep_io: u16 ep, u16 flags, u32 length, u8 data[]
        buffer = bytearray(8 + len(data))
        struct.pack_into("<HHI", buffer, 0, ep_num, 0, len(data))
        buffer[8:] = data
        try:
            return fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_EP_WRITE, buffer)
        except OSError as e:
            log(f"ep_write failed on EP#{ep_num}: {e}", "ERROR")
            return -1
    
    def configure_gadget(self):
        """Configure the gadget"""
        import fcntl
        fcntl.ioctl(self.gadget_fd, USB_RAW_IOCTL_CONFIGURE)
    
    def handle_control_request(self, ctrl_data):
        """Handle control transfer - forwards all types including class-specific"""
        # Parse control request
        bmRequestType, bRequest, wValue, wIndex, wLength = struct.unpack("<BBHHH", ctrl_data[:8])
        
        req_type = (bmRequestType & USB_TYPE_MASK) >> 5
        type_str = {0: "STD", 1: "CLASS", 2: "VENDOR"}.get(req_type, "UNK")
        direction = "IN" if (bmRequestType & USB_DIR_IN) else "OUT"
        log(f"EP0 {direction}: {type_str} Type={hex(bmRequestType)} Req={hex(bRequest)} Val={hex(wValue)} Idx={hex(wIndex)} Len={wLength}", "CTRL")
        
        try:
            # Handle GET_STATUS locally (especially after reset when device might not be ready)
            if (bmRequestType & USB_TYPE_MASK) == USB_TYPE_STANDARD and \
               bRequest == USB_REQ_GET_STATUS and \
               (bmRequestType & USB_DIR_IN):
                # Return 2 bytes: 0x0000 (bus-powered, no remote wakeup)
                status_response = struct.pack("<H", 0x0000)
                self.ep0_write(status_response)
                log(f"  ✓ GET_STATUS handled locally (0x0000)", "CTRL")
                return
            
            # Forward ALL control requests to real device (standard, class, vendor)
            if bmRequestType & USB_DIR_IN:
                # Check if this is a GET_DESCRIPTOR request - use cached descriptors
                if (bmRequestType & USB_TYPE_MASK) == USB_TYPE_STANDARD and \
                   bRequest == USB_REQ_GET_DESCRIPTOR:
                    
                    desc_type = (wValue >> 8) & 0xFF
                    desc_index = wValue & 0xFF
                    
                    # Use cached device descriptor
                    if desc_type == USB_DT_DEVICE and self.device_descriptor:
                        data = self.device_descriptor[:wLength]  # Return requested length
                        
                        # Special handling - ensure bMaxPacketSize0 >= 64
                        # Some UDCs require this (see C++ code line 467-473)
                        if len(data) >= 8:
                            data_list = list(data)
                            if data_list[7] < 64:  # bMaxPacketSize0 is at offset 7
                                log(f"  Adjusting bMaxPacketSize0 from {data_list[7]} to 64", "INFO")
                                data_list[7] = 64
                                data = bytes(data_list)
                        
                        self.ep0_write(data)
                        log(f"  ✓ Sent cached device descriptor ({len(data)} bytes)", "CTRL")
                        return
                    
                    # Use cached configuration descriptor
                    elif desc_type == USB_DT_CONFIG and desc_index in self.config_descriptors:
                        cached_cfg = self.config_descriptors[desc_index]
                        data = cached_cfg[:wLength]  # Return requested length
                        self.ep0_write(data)
                        log(f"  ✓ Sent cached config descriptor {desc_index} ({len(data)} bytes)", "CTRL")
                        return
                    
                    # Use cached string descriptor
                    elif desc_type == USB_DT_STRING and desc_index in self.string_descriptors:
                        cached_str = self.string_descriptors[desc_index]
                        data = cached_str[:wLength]  # Return requested length
                        self.ep0_write(data)
                        log(f"  ✓ Sent cached string descriptor {desc_index} ({len(data)} bytes)", "CTRL")
                        return
                
                # For all other requests, query device directly
                data = self.usb_device.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex, wLength, timeout=1000)
                self.ep0_write(bytes(data))
                log(f"  ✓ Forwarded {len(data)} bytes to host", "CTRL")
            else:
                # OUT transfer - forward to device, then ACK
                # CRITICAL: Order matters! Forward first, ACK only if successful (like C++ version)
                if wLength > 0:
                    # For non-zero length: ep0_read reads data AND ACKs the request
                    data = self.ep0_read(wLength)
                    # Now forward to device
                    result = self.usb_device.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex, data, timeout=1000)
                    if result < 0:
                        raise Exception(f"Device control transfer failed: {result}")
                else:
                    # For wLength=0: Forward to device FIRST, then ACK only if successful
                    # This matches C++ behavior (proxy.cpp lines 619-639)
                    try:
                        result = self.usb_device.ctrl_transfer(bmRequestType, bRequest, wValue, wIndex, None, timeout=1000)
                        if result == 0:
                            # Device accepted - ACK the request
                            self.ep0_read(0)  # ACK
                            log(f"  ✓ Forwarded to device and ACKed", "CTRL")
                        else:
                            # Device rejected - stall
                            log(f"  Device rejected (result={result}), stalling", "WARN")
                            self.ep0_stall()
                            return
                    except Exception as e:
                        # Device error - stall
                        log(f"  Device error: {e}, stalling", "ERROR")
                        self.ep0_stall()
                        return
                    return  # Already logged, don't log again below
                log(f"  ✓ Forwarded to device", "CTRL")
        except Exception as e:
            log(f"  ✗ Error: {e}, stalling EP0", "ERROR")
            self.ep0_stall()
    
    def endpoint_in_reader(self, ep_addr, ep_type, queue):
        """Thread to read from device endpoint (IN) and queue data for host"""
        log(f"Thread started: IN reader for device EP{hex(ep_addr)}", "THREAD")
        
        while self.running and self.endpoints_running:
            try:
                if ep_type == USB_ENDPOINT_XFER_BULK:
                    data = self.usb_device.read(ep_addr, 4096, timeout=100)
                elif ep_type == USB_ENDPOINT_XFER_INT:
                    data = self.usb_device.read(ep_addr, 64, timeout=100)
                else:
                    time.sleep(0.01)
                    continue
                
                if data:
                    queue.put(bytes(data))
                    log(f"  IN ← device: {len(data)} bytes from EP{hex(ep_addr)} | Data: {data[:16].hex()}{'...' if len(data) > 16 else ''}", "DATA")
            except usb.core.USBTimeoutError:
                continue
            except Exception as e:
                if self.running and self.endpoints_running:
                    log(f"IN reader error on EP{hex(ep_addr)}: {e}", "ERROR")
                break
    
    def endpoint_in_writer(self, ep_num, queue):
        """Thread to write queued data to gadget endpoint (send to host)"""
        log(f"Thread started: IN writer for gadget EP#{ep_num}", "THREAD")
        
        while self.running and self.endpoints_running:
            try:
                data = queue.get(timeout=0.1)
                self.ep_write(ep_num, data)
                log(f"  IN → host: {len(data)} bytes to EP#{ep_num}", "DATA")
            except Empty:
                continue
            except Exception as e:
                if self.running and self.endpoints_running:
                    log(f"IN writer error on EP#{ep_num}: {e}", "ERROR")
                break
    
    def endpoint_out_reader(self, ep_num, ep_type, queue):
        """Thread to read from gadget endpoint (OUT from host) and queue for device"""
        log(f"Thread started: OUT reader for gadget EP#{ep_num}", "THREAD")
        
        while self.running and self.endpoints_running:
            try:
                if ep_type == USB_ENDPOINT_XFER_BULK:
                    data = self.ep_read(ep_num, 4096)
                elif ep_type == USB_ENDPOINT_XFER_INT:
                    data = self.ep_read(ep_num, 64)
                else:
                    time.sleep(0.01)
                    continue
                
                if data:
                    queue.put(data)
                    log(f"  OUT ← host: {len(data)} bytes from EP#{ep_num} | Data: {data[:16].hex()}{'...' if len(data) > 16 else ''}", "DATA")
            except Exception as e:
                if self.running and self.endpoints_running:
                    log(f"OUT reader error on EP#{ep_num}: {e}", "ERROR")
                break
    
    def endpoint_out_writer(self, ep_addr, queue):
        """Thread to write queued data to device endpoint (send to printer)"""
        log(f"Thread started: OUT writer for device EP{hex(ep_addr)}", "THREAD")
        
        while self.running and self.endpoints_running:
            try:
                data = queue.get(timeout=0.1)
                if ep_addr & 0x80:
                    # This is an IN endpoint, shouldn't happen
                    log(f"ERROR: Trying to write to IN endpoint {hex(ep_addr)}", "ERROR")
                    continue
                
                self.usb_device.write(ep_addr, data, timeout=1000)
                log(f"  OUT → device: {len(data)} bytes to EP{hex(ep_addr)}", "DATA")
            except Empty:
                continue
            except Exception as e:
                if self.running and self.endpoints_running:
                    log(f"OUT writer error on EP{hex(ep_addr)}: {e}", "ERROR")
                break
    
    def setup_endpoints(self, config_value):
        """Setup bidirectional endpoint forwarding threads"""
        log("="*60, "INFO")
        log(f"ENDPOINT SETUP: Configuration {config_value}", "INFO")
        log("="*60, "INFO")
        
        # Find the configuration with this value
        config_index = None
        for idx, cfg in enumerate(self.usb_device):
            if cfg.bConfigurationValue == config_value:
                config_index = idx
                break
        
        if config_index is None:
            log(f"ERROR: Configuration {config_value} not found!", "ERROR")
            return
        
        log(f"✓ Using configuration index {config_index}", "SUCCESS")
        cfg = self.usb_device[config_index]
        
        for intf in cfg:
            log(f"Interface {intf.bInterfaceNumber}, Alternate {intf.bAlternateSetting}", "INFO")
            for ep in intf:
                ep_addr = ep.bEndpointAddress
                ep_type = usb.util.endpoint_type(ep.bmAttributes)
                is_in = bool(ep_addr & 0x80)
                
                direction = "IN" if is_in else "OUT"
                type_name = {0: "CTRL", 1: "ISOC", 2: "BULK", 3: "INT"}.get(ep_type, "UNK")
                
                log(f"  Setting up {direction} EP{hex(ep_addr)} ({type_name}, MaxPkt={ep.wMaxPacketSize})", "INFO")
                
                # Build endpoint descriptor
                ep_desc = struct.pack("<BBBBHB",
                    7,  # bLength
                    0x05,  # bDescriptorType (ENDPOINT)
                    ep_addr,
                    ep.bmAttributes,
                    ep.wMaxPacketSize,
                    ep.bInterval
                )
                
                # Enable in gadget
                ep_num = self.ep_enable(ep_desc)
                log(f"    ✓ Enabled as gadget EP#{ep_num}", "SUCCESS")
                
                # Create queue for this endpoint
                queue = Queue()
                self.endpoint_queues[ep_addr] = queue
                
                if is_in:
                    # IN endpoint: device → host
                    # Read from device, write to gadget
                    reader = threading.Thread(target=self.endpoint_in_reader, 
                                            args=(ep_addr, ep_type, queue),
                                            name=f"IN-read-{hex(ep_addr)}")
                    writer = threading.Thread(target=self.endpoint_in_writer,
                                            args=(ep_num, queue),
                                            name=f"IN-write-#{ep_num}")
                    
                    reader.daemon = True
                    writer.daemon = True
                    reader.start()
                    writer.start()
                    
                    self.endpoint_threads.extend([reader, writer])
                else:
                    # OUT endpoint: host → device
                    # Read from gadget, write to device
                    reader = threading.Thread(target=self.endpoint_out_reader,
                                            args=(ep_num, ep_type, queue),
                                            name=f"OUT-read-#{ep_num}")
                    writer = threading.Thread(target=self.endpoint_out_writer,
                                            args=(ep_addr, queue),
                                            name=f"OUT-write-{hex(ep_addr)}")
                    
                    reader.daemon = True
                    writer.daemon = True
                    reader.start()
                    writer.start()
                    
                    self.endpoint_threads.extend([reader, writer])
    
    def cleanup_endpoints(self):
        """Clean up endpoint threads and queues on host disconnect/reset"""
        if not self.endpoint_threads:
            return  # Nothing to clean up
        
        log("="*60, "INFO")
        log("CLEANING UP ENDPOINTS", "INFO")
        log("="*60, "INFO")
        
        # Signal endpoint threads to stop
        self.endpoints_running = False
        log(f"Stopping {len(self.endpoint_threads)} endpoint thread(s)...", "INFO")
        
        # Wait for threads to finish
        for thread in self.endpoint_threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
                if thread.is_alive():
                    log(f"Warning: Thread {thread.name} did not stop in time", "WARN")
                else:
                    log(f"✓ Thread {thread.name} stopped", "SUCCESS")
        
        # Clear thread list and queues
        self.endpoint_threads.clear()
        self.endpoint_queues.clear()
        
        # Re-enable flag for next connection
        self.endpoints_running = True
        
        log("✓ Endpoint cleanup complete", "SUCCESS")
        log("="*60, "INFO")
    
    def ep0_loop(self):
        """Main EP0 event loop with hotplug support"""
        log("="*60, "INFO")
        log("ENTERING EP0 EVENT LOOP", "INFO")
        log("="*60, "INFO")
        configured = False
        last_heartbeat = time.time()
        
        while self.running:
            try:
                # Log heartbeat every 5 seconds to show we're still alive
                current_time = time.time()
                if current_time - last_heartbeat > 5.0:
                    if configured:
                        log("Proxy active, waiting for events...", "INFO")
                    else:
                        log("Still waiting for control requests from host...", "INFO")
                    last_heartbeat = current_time
                
                event_type, event_data = self.fetch_event()
                
                # Log what event we received
                event_names = {0: "INVALID", 1: "CONNECT", 2: "CONTROL", 
                              3: "SUSPEND", 4: "RESUME", 5: "RESET", 6: "DISCONNECT"}
                event_name = event_names.get(event_type, f"UNKNOWN({event_type})")
                
                if event_type == 0:
                    continue  # Invalid event
                    
                log(f"Event: {event_name} (type={event_type}, data_len={len(event_data)})", "EVENT")
                
                # Handle RESET/DISCONNECT first (like C++ version - proxy.cpp line 396)
                # Note: C++ treats DISCONNECT like RESET due to dwc2 bug
                if event_type == USB_RAW_EVENT_RESET or event_type == USB_RAW_EVENT_DISCONNECT:
                    if event_type == USB_RAW_EVENT_RESET:
                        log("="*60, "WARN")
                        log("USB HOST HOTPLUG: RESET", "WARN")
                        log("="*60, "WARN")
                    else:
                        log("="*60, "WARN")
                        log("USB HOST HOTPLUG: DISCONNECTED (treated as RESET)", "WARN")
                        log("="*60, "WARN")
                        self.host_connected = False
                    
                    # Clean up endpoints if configured (like C++ version - proxy.cpp line 403-421)
                    if configured or self.device_configured:
                        log("Cleaning up endpoints due to reset...", "INFO")
                        self.cleanup_endpoints()
                        configured = False
                        self.device_configured = False
                    
                    # Reset the device (like C++ version - proxy.cpp line 405)
                    try:
                        if self.usb_device:
                            self.usb_device.reset()
                            log("✓ Device reset", "SUCCESS")
                    except Exception as e:
                        log(f"Error resetting device: {e}", "ERROR")
                    
                    log("Host reset. Waiting for re-enumeration...", "INFO")
                    continue
                
                # Skip non-CONTROL events (like C++ version - proxy.cpp line 425)
                # This includes CONNECT, SUSPEND, RESUME - they're just informational
                if event_type != USB_RAW_EVENT_CONTROL:
                    if event_type == USB_RAW_EVENT_CONNECT:
                        # Silently note connection (C++ version doesn't log this)
                        self.host_connected = True
                        # If we were previously configured, clean up and reset state
                        if configured or self.device_configured:
                            log("Host reconnected, cleaning up previous session...", "INFO")
                            self.cleanup_endpoints()
                            configured = False
                            self.device_configured = False
                    elif event_type == USB_RAW_EVENT_SUSPEND:
                        log("USB SUSPEND event received from host", "INFO")
                    elif event_type == USB_RAW_EVENT_RESUME:
                        log("USB RESUME event received from host", "INFO")
                    continue
                
                # Process CONTROL events (like C++ version - proxy.cpp line 425+)
                if event_type == USB_RAW_EVENT_CONTROL:
                    # Parse control request first
                    if len(event_data) < 8:
                        log("ERROR: Control event data too short!", "ERROR")
                        self.ep0_stall()
                        continue
                    
                    bmRequestType, bRequest, wValue, wIndex, wLength = struct.unpack("<BBHHH", event_data[:8])
                    
                    # Handle SET_ADDRESS - this is handled by gadget, not forwarded to device
                    if (bmRequestType & USB_TYPE_MASK) == USB_TYPE_STANDARD and \
                       bRequest == USB_REQ_SET_ADDRESS:
                        log(f"SET_ADDRESS: address={wValue} (handled by gadget)", "INFO")
                        # Just ACK the request - gadget handles address assignment
                        self.ep0_read(0)  # ACK
                        log("✓ SET_ADDRESS ACKed", "SUCCESS")
                        continue
                    
                    # Handle GET_STATUS - return standard response (especially after reset)
                    # GET_STATUS returns device status: bit 0 = self-powered, bit 1 = remote wakeup
                    # For most devices, return 0x0000 (bus-powered, no remote wakeup)
                    if (bmRequestType & USB_TYPE_MASK) == USB_TYPE_STANDARD and \
                       bRequest == USB_REQ_GET_STATUS and \
                       (bmRequestType & USB_DIR_IN):
                        log(f"GET_STATUS: recipient={wIndex & 0xFF} (handled locally)", "INFO")
                        # Return 2 bytes: 0x0000 (bus-powered, no remote wakeup)
                        status_response = struct.pack("<H", 0x0000)
                        self.ep0_write(status_response)
                        log("✓ GET_STATUS responded (0x0000)", "SUCCESS")
                        continue
                    
                    # Handle GET_CONFIGURATION - return current configuration
                    if (bmRequestType & USB_TYPE_MASK) == USB_TYPE_STANDARD and \
                       bRequest == USB_REQ_GET_CONFIGURATION and \
                       (bmRequestType & USB_DIR_IN):
                        log(f"GET_CONFIGURATION (handled locally)", "INFO")
                        # Return current configuration value (1 byte)
                        config_value = 1 if configured else 0  # Return 0 if not configured
                        self.ep0_write(bytes([config_value]))
                        log(f"✓ GET_CONFIGURATION responded ({config_value})", "SUCCESS")
                        continue
                    
                    # Handle SET_CONFIGURATION specially BEFORE forwarding (like C++ version)
                    if (bmRequestType & USB_TYPE_MASK) == USB_TYPE_STANDARD and \
                       bRequest == USB_REQ_SET_CONFIGURATION and not configured:
                        # This is an OUT transfer with wLength=0, so we need to ACK it
                        # But we must configure everything BEFORE ACKing
                        log("="*60, "INFO")
                        log(f"SET_CONFIGURATION: value={wValue}", "CONFIG")
                        
                        # Set configuration on real device
                        self.usb_device.set_configuration(wValue)
                        log(f"✓ Device configured", "SUCCESS")
                        
                        # Configure gadget
                        log("Configuring gadget...", "INFO")
                        self.configure_gadget()
                        log("✓ Gadget configured", "SUCCESS")
                        
                        # Setup endpoint forwarding with actual config VALUE, not index
                        self.setup_endpoints(wValue)
                        configured = True
                        self.device_configured = True
                        
                        # NOW ACK the request (for OUT with wLength=0, ep0_read ACKs)
                        self.ep0_read(0)
                        log("✓ SET_CONFIGURATION request ACKed", "SUCCESS")
                        log("="*60, "INFO")
                        log("DEVICE FULLY CONFIGURED AND READY", "SUCCESS")
                        log("="*60, "INFO")
                        continue
                    
                    # Handle all other control requests normally
                    self.handle_control_request(event_data)
                    
            except KeyboardInterrupt:
                log("Keyboard interrupt received, stopping...", "WARN")
                self.running = False
                break
            except Exception as e:
                log(f"EP0 loop error: {e}", "ERROR")
                import traceback
                traceback.print_exc()
    
    def run(self):
        """Main entry point"""
        try:
            # Find and open real USB device
            self.find_and_open_device()
            
            # Open and initialize gadget
            self.open_raw_gadget()
            self.init_gadget()
            self.run_gadget()
            
            # Ensure clean disconnect/reconnect cycle before processing events
            self.ensure_clean_connection()
            
            # Start EP0 loop
            self.ep0_loop()
            
        except KeyboardInterrupt:
            log("\nShutting down proxy...", "INFO")
        finally:
            self.running = False
            if self.gadget_fd:
                os.close(self.gadget_fd)
            log("Cleanup complete. Goodbye!", "INFO")


def main():
    parser = argparse.ArgumentParser(description="Lightweight USB Proxy")
    parser.add_argument("--vendor_id", type=lambda x: int(x, 16), help="Vendor ID (hex)")
    parser.add_argument("--product_id", type=lambda x: int(x, 16), help="Product ID (hex)")
    parser.add_argument("--device", default="dummy_udc.0", help="UDC device name")
    parser.add_argument("--driver", default="dummy_udc", help="UDC driver name")
    
    args = parser.parse_args()
    
    log("="*60, "INFO")
    log("LIGHTWEIGHT USB PROXY v1.0", "INFO")
    log("POS <-> Printer Bidirectional Forwarder", "INFO")
    log("="*60, "INFO")
    
    proxy = USBProxy(
        vendor_id=args.vendor_id,
        product_id=args.product_id,
        device=args.device,
        driver=args.driver
    )
    
    proxy.run()


if __name__ == "__main__":
    main()
