import network
import time
import json
import struct
import socket
import uasyncio as asyncio
from machine import I2C, Pin, RTC, WDT, reset
import ubinascii
import uhashlib
import gc
import os

# Try to import MQTT client
try:
    from umqtt.simple import MQTTClient
except ImportError:
    print("ERROR: umqtt.simple not found. MQTT disabled.")
    MQTTClient = None

# ------------------------------------------------------------
#  CONFIGURATION
# ------------------------------------------------------------
AP_SSID      = "PowerMonitor"
AP_PASSWORD  = "ina238pro"
AP_IP        = "192.168.4.1"

I2C_SDA      = 19
I2C_SCL      = 20
INA238_ADDR  = 0x40

READ_INTERVAL_S = 1.0
MAX_HISTORY     = 7200    # 2 hours at 1 reading per second (7200 points)
ENERGY_COST_PER_KWH = 0.15
SHUNT_RESISTOR_OHM = 0.015
MAX_CURRENT_AMPS   = 10.0

# CSV Logging settings
CSV_LOG_ENABLED = True
CSV_LOG_INTERVAL = 10   # Log to CSV every 10 seconds
CSV_MAX_SIZE_BYTES = 200 * 1024  # 200KB max file size

# Watchdog timers
WATCHDOG_TIMEOUT_MS = 30000  # 30 seconds
HEARTBEAT_INTERVAL_S = 5     # Send heartbeat every 5 seconds
RECONNECT_ATTEMPTS = 3        # Number of reconnect attempts

# ------------------------------------------------------------
#  GLOBAL VARIABLES
# ------------------------------------------------------------
latest = {"v": 0.0, "i": 0.0, "p": 0.0, "temp": 0.0, "uptime": 0}
history = []          # list of (timestamp, voltage, current, power)
total_energy_wh = 0.0
last_update_time = 0
last_csv_write_time = 0
last_heartbeat = 0
websocket_error_count = 0

ws_clients = []
server_task = None
sensor_task = None

config = {
    "wifi_ssid": "",
    "wifi_password": "",
    "mqtt_broker": "",
    "mqtt_port": 1883,
    "shunt_resistor": SHUNT_RESISTOR_OHM,
    "max_current": MAX_CURRENT_AMPS,
    "energy_cost": ENERGY_COST_PER_KWH,
    "csv_log_interval": CSV_LOG_INTERVAL
}

mqtt_client = None

# ------------------------------------------------------------
#  INA238 DRIVER
# ------------------------------------------------------------
class INA238:
    REG_CONFIG    = 0x00
    REG_ADC_CFG   = 0x01
    REG_SHUNT_CAL = 0x02
    REG_VBUS      = 0x05
    REG_DIETEMP   = 0x06
    REG_CURRENT   = 0x07
    REG_POWER     = 0x08

    def __init__(self, i2c, addr=0x40, shunt_ohm=0.015, max_amps=10.0):
        self.i2c = i2c
        self.addr = addr
        self._Rs = shunt_ohm
        self._Imax = max_amps
        self._calibrate()
        self._configure()

    def _write16(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, struct.pack('>H', val & 0xFFFF))

    def _read16(self, reg, signed=False):
        data = self.i2c.readfrom_mem(self.addr, reg, 2)
        raw = struct.unpack('>H', data)[0]
        if signed and raw & 0x8000:
            return raw - 0x10000
        return raw

    def _calibrate(self):
        self._current_lsb = self._Imax / 32768.0
        cal_val = int(13107.2e-6 / (self._current_lsb * self._Rs))
        cal_val = max(1, min(cal_val, 0x7FFF))
        self._write16(self.REG_SHUNT_CAL, cal_val)
        self._power_lsb = 3.2 * self._current_lsb

    def _configure(self):
        cfg = (0x0F << 12) | (0x02 << 9) | (0x02 << 6) | (0x02 << 3) | 0x03
        self._write16(self.REG_ADC_CFG, cfg)

    @property
    def voltage(self):
        raw = self._read16(self.REG_VBUS)
        return raw * 3.125e-3

    @property
    def current(self):
        raw = self._read16(self.REG_CURRENT, signed=True)
        return raw * self._current_lsb

    @property
    def power(self):
        raw = self._read16(self.REG_POWER)
        return raw * self._power_lsb

    @property
    def temperature(self):
        raw = self._read16(self.REG_DIETEMP, signed=True)
        return (raw >> 4) * 0.125

# ------------------------------------------------------------
#  CSV LOGGING FUNCTIONS
# ------------------------------------------------------------
def init_csv_file():
    try:
        if "power_data.csv" not in os.listdir():
            with open("power_data.csv", "w") as f:
                f.write("timestamp,datetime,voltage_V,current_A,power_W,temperature_C,energy_kWh,cost_USD\n")
            print("CSV file created")
        else:
            import stat
            size = os.stat("power_data.csv")[stat.ST_SIZE]
            if size > CSV_MAX_SIZE_BYTES:
                if "power_data_old.csv" in os.listdir():
                    os.remove("power_data_old.csv")
                os.rename("power_data.csv", "power_data_old.csv")
                with open("power_data.csv", "w") as f:
                    f.write("timestamp,datetime,voltage_V,current_A,power_W,temperature_C,energy_kWh,cost_USD\n")
                print("CSV rotated")
    except Exception as e:
        print("CSV init error:", e)

def write_csv_row(data):
    try:
        dt_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(data['timestamp']))
        with open("power_data.csv", "a") as f:
            f.write(f"{data['timestamp']},{dt_str},{data['v']:.3f},{data['i']:.3f},{data['p']:.3f},{data['temp']:.1f},{data['energy_kwh']:.6f},{data['cost']:.4f}\n")
        return True
    except Exception as e:
        print("CSV write error:", e)
        return False

def get_csv_data_last_hours(hours=2):
    """Get CSV data from last X hours"""
    try:
        current_time = time.time()
        cutoff_time = current_time - (hours * 3600)
        
        with open("power_data.csv", "r") as f:
            lines = f.readlines()
        
        if len(lines) <= 1:
            return "No data available for last {} hours".format(hours)
        
        # Keep header
        filtered = [lines[0]]
        for line in lines[1:]:
            parts = line.split(',')
            if len(parts) >= 2:
                try:
                    timestamp = float(parts[0])
                    if timestamp >= cutoff_time:
                        filtered.append(line)
                except:
                    continue
        
        return ''.join(filtered)
    except Exception as e:
        print("CSV read error:", e)
        return "Error reading CSV data"

def get_all_csv():
    try:
        with open("power_data.csv", "r") as f:
            return f.read()
    except:
        return "No data available"

def get_csv_size():
    try:
        import stat
        size = os.stat("power_data.csv")[stat.ST_SIZE]
        return size / 1024
    except:
        return 0

def clear_csv():
    try:
        with open("power_data.csv", "w") as f:
            f.write("timestamp,datetime,voltage_V,current_A,power_W,temperature_C,energy_kWh,cost_USD\n")
        return True
    except:
        return False

# ------------------------------------------------------------
#  CONFIGURATION MANAGEMENT
# ------------------------------------------------------------
def save_config():
    try:
        with open("config.json", "w") as f:
            json.dump(config, f)
        print("Config saved")
    except Exception as e:
        print("Save config error:", e)

def load_config():
    global config
    try:
        with open("config.json", "r") as f:
            saved = json.load(f)
            config.update(saved)
        print("Config loaded")
    except:
        print("No saved config, using defaults")
        save_config()

# ------------------------------------------------------------
#  WIFI MANAGEMENT WITH AUTO-RECONNECT
# ------------------------------------------------------------
def connect_wifi(ssid, password):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    
    if wlan.isconnected():
        print("Already connected to WiFi")
        return True
    
    print(f"Connecting to WiFi: {ssid}")
    wlan.connect(ssid, password)
    
    # Wait for connection with timeout
    for i in range(20):  # 10 second timeout
        if wlan.isconnected():
            print("WiFi connected:", wlan.ifconfig())
            return True
        time.sleep(0.5)
    
    print("WiFi connection failed")
    return False

def check_and_reconnect_wifi():
    """Check WiFi status and reconnect if needed"""
    wlan = network.WLAN(network.STA_IF)
    if config["wifi_ssid"] and config["wifi_password"]:
        if not wlan.isconnected():
            print("WiFi disconnected, attempting reconnect...")
            return connect_wifi(config["wifi_ssid"], config["wifi_password"])
    return wlan.isconnected()

# ------------------------------------------------------------
#  MQTT FUNCTIONS
# ------------------------------------------------------------
async def mqtt_publish_task():
    global mqtt_client
    while True:
        try:
            if config["mqtt_broker"]:
                if mqtt_client is None:
                    client_id = ubinascii.hexlify(machine.unique_id()).decode()
                    mqtt_client = MQTTClient(client_id, config["mqtt_broker"], port=config["mqtt_port"])
                    mqtt_client.connect()
                    print("MQTT connected")
                else:
                    # Test connection with a ping
                    mqtt_client.ping()
            await asyncio.sleep(30)
        except Exception as e:
            print("MQTT error:", e)
            mqtt_client = None
            await asyncio.sleep(10)

async def mqtt_send(data):
    global mqtt_client
    if mqtt_client and config["mqtt_broker"]:
        try:
            mqtt_client.publish(b"power_monitor/data", json.dumps(data).encode())
        except Exception as e:
            print("MQTT publish error:", e)
            mqtt_client = None

# ------------------------------------------------------------
#  WEBSOCKET FUNCTIONS
# ------------------------------------------------------------
async def ws_send(writer, msg):
    try:
        data = msg.encode()
        length = len(data)
        header = bytearray()
        header.append(0x81)
        if length <= 125:
            header.append(length)
        elif length <= 65535:
            header.append(126)
            header.extend(struct.pack('>H', length))
        else:
            header.append(127)
            header.extend(struct.pack('>Q', length))
        writer.write(header + data)
        await writer.drain()
        return True
    except Exception as e:
        print("WS send error:", e)
        return False

# ------------------------------------------------------------
#  DASHBOARD HTML
# ------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Power Monitor with 2-Hour Graph</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  *{box-sizing:border-box}
  body{font-family:'Segoe UI',sans-serif;margin:0;background:#f0f2f5;padding:20px}
  .card{background:white;border-radius:16px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,0.1);margin-bottom:20px}
  .metric{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap}
  .metric-value{font-size:2.5rem;font-weight:bold;font-family:monospace}
  .metric-unit{color:#666;margin-left:8px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:15px;margin-bottom:20px}
  .status{display:inline-block;padding:4px 12px;border-radius:20px;font-size:0.8rem;color:white}
  .online{background:#16a34a}
  .offline{background:#dc2626}
  canvas{max-height:400px;width:100%}
  .config-form input, .config-form select, .config-form button{padding:10px;margin:5px 0;width:100%;border:1px solid #ccc;border-radius:8px}
  button{background:#2563eb;color:white;border:none;cursor:pointer;font-weight:bold}
  button:hover{background:#1e40af}
  .btn-danger{background:#dc2626}
  .btn-danger:hover{background:#b91c1c}
  .btn-success{background:#16a34a}
  .btn-success:hover{background:#15803d}
  .csv-actions{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap}
  .csv-actions button{flex:1;min-width:120px}
  .file-info{font-size:0.8rem;color:#666;margin-top:10px}
  .live-badge{font-size:0.7rem;margin-left:10px}
  @media (max-width: 768px){.metric-value{font-size:1.5rem}}
</style>
</head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">
  <h1>⚡ Power Monitor <span class="live-badge" id="liveBadge">● LIVE</span></h1>
  <div><span id="wsStatus" class="status offline">Connecting...</span></div>
</div>

<div class="grid">
  <div class="card"><div>🔌 Voltage</div><div class="metric"><span class="metric-value" id="voltage">0.000</span><span class="metric-unit">V</span></div></div>
  <div class="card"><div>⚡ Current</div><div class="metric"><span class="metric-value" id="current">0.000</span><span class="metric-unit">A</span></div></div>
  <div class="card"><div>🔥 Power</div><div class="metric"><span class="metric-value" id="power">0.000</span><span class="metric-unit">W</span></div></div>
  <div class="card"><div>🌡️ Temp</div><div class="metric"><span class="metric-value" id="temp">0.0</span><span class="metric-unit">°C</span></div></div>
  <div class="card"><div>🔋 Energy</div><div class="metric"><span class="metric-value" id="energy">0.000</span><span class="metric-unit">kWh</span></div></div>
  <div class="card"><div>💰 Cost</div><div class="metric"><span class="metric-value" id="cost">0.00</span><span class="metric-unit">$</span></div></div>
</div>

<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap">
    <h3>📈 Power & Voltage Trend (Last 2 Hours)</h3>
    <div>
      <button onclick="downloadLast2HoursCSV()" style="margin-right:10px">📥 Download Last 2h CSV</button>
      <button onclick="toggleChartType()">🔄 Toggle Chart Type</button>
    </div>
  </div>
  <canvas id="powerChart" width="800" height="400"></canvas>
</div>

<div class="card">
  <h3>💾 Data Logging</h3>
  <div class="csv-actions">
    <button onclick="downloadAllCSV()" class="btn-success">📥 Download All CSV</button>
    <button onclick="downloadLast2HoursCSV()" class="btn-success">📥 Last 2 Hours CSV</button>
    <button onclick="clearCSV()" class="btn-danger" id="clearBtn">🗑️ Clear All Data</button>
  </div>
  <div class="file-info" id="csvInfo">Loading file info...</div>
</div>

<div class="card">
  <h3>⚙️ Configuration</h3>
  <form id="configForm" class="config-form">
    <input type="text" id="wifi_ssid" placeholder="WiFi SSID">
    <input type="password" id="wifi_password" placeholder="WiFi Password">
    <input type="text" id="mqtt_broker" placeholder="MQTT Broker IP">
    <input type="number" id="mqtt_port" placeholder="MQTT Port" value="1883">
    <input type="number" step="any" id="shunt" placeholder="Shunt Resistor (ohm)" value="0.015">
    <input type="number" step="any" id="max_current" placeholder="Max Current (A)" value="10.0">
    <input type="number" step="any" id="cost_kwh" placeholder="Cost per kWh ($)" value="0.15">
    <input type="number" id="csv_interval" placeholder="CSV Log Interval (seconds)" value="10">
    <button type="submit">💾 Save & Restart</button>
  </form>
</div>

<script>
  let ws, chart;
  let powerData = [];
  let voltageData = [];
  let timeLabels = [];
  let chartType = 'line';
  
  // Chart.js setup
  const ctx = document.getElementById('powerChart').getContext('2d');
  
  function initChart() {
    chart = new Chart(ctx, {
      type: chartType,
      data: {
        datasets: [
          {
            label: 'Power (W)',
            data: [],
            borderColor: '#2563eb',
            backgroundColor: 'rgba(37, 99, 235, 0.1)',
            borderWidth: 2,
            fill: true,
            yAxisID: 'y'
          },
          {
            label: 'Voltage (V)',
            data: [],
            borderColor: '#16a34a',
            backgroundColor: 'rgba(22, 163, 74, 0.1)',
            borderWidth: 2,
            fill: true,
            yAxisID: 'y1'
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          tooltip: { mode: 'index', intersect: false },
          legend: { position: 'top' }
        },
        scales: {
          x: {
            type: 'time',
            time: { unit: 'minute', tooltipFormat: 'HH:mm:ss' },
            title: { display: true, text: 'Time' }
          },
          y: {
            title: { display: true, text: 'Power (W)' },
            position: 'left',
            grid: { drawOnChartArea: true }
          },
          y1: {
            title: { display: true, text: 'Voltage (V)' },
            position: 'right',
            grid: { drawOnChartArea: false }
          }
        }
      }
    });
  }
  
  function updateChart(powerHistory, voltageHistory) {
    if (!chart) initChart();
    
    const now = Date.now();
    const twoHoursAgo = now - (2 * 60 * 60 * 1000);
    
    // Filter data for last 2 hours
    const filteredPower = powerHistory.filter(p => p.t * 1000 >= twoHoursAgo);
    const filteredVoltage = voltageHistory.filter(v => v.t * 1000 >= twoHoursAgo);
    
    chart.data.datasets[0].data = filteredPower.map(p => ({x: p.t * 1000, y: p.p}));
    chart.data.datasets[1].data = filteredVoltage.map(v => ({x: v.t * 1000, y: v.v}));
    chart.update();
  }
  
  function toggleChartType() {
    chartType = chartType === 'line' ? 'bar' : 'line';
    chart.config.type = chartType;
    chart.update();
  }

  function connectWebSocket() {
    if (ws && ws.readyState === WebSocket.OPEN) return;
    
    ws = new WebSocket('ws://' + location.host + '/ws');
    ws.onopen = () => {
      document.getElementById('wsStatus').className = 'status online';
      document.getElementById('wsStatus').innerText = 'Live';
      document.getElementById('liveBadge').style.color = '#16a34a';
      fetchInitialHistory();
    };
    ws.onclose = () => {
      document.getElementById('wsStatus').className = 'status offline';
      document.getElementById('wsStatus').innerText = 'Offline';
      document.getElementById('liveBadge').style.color = '#dc2626';
      setTimeout(connectWebSocket, 2000);
    };
    ws.onerror = () => {
      console.log('WebSocket error');
      ws.close();
    };
    ws.onmessage = (e) => {
      const d = JSON.parse(e.data);
      document.getElementById('voltage').innerText = d.v.toFixed(3);
      document.getElementById('current').innerText = d.i.toFixed(3);
      document.getElementById('power').innerText = d.p.toFixed(3);
      document.getElementById('temp').innerText = d.temp.toFixed(1);
      document.getElementById('energy').innerText = (d.energy_kwh || 0).toFixed(3);
      document.getElementById('cost').innerText = (d.cost || 0).toFixed(2);
      
      if (d.power_history && d.voltage_history) {
        updateChart(d.power_history, d.voltage_history);
      }
    };
  }
  
  async function fetchInitialHistory() {
    const response = await fetch('/history');
    const data = await response.json();
    if (data.power_history && data.voltage_history) {
      updateChart(data.power_history, data.voltage_history);
    }
  }
  
  async function downloadAllCSV() {
    const response = await fetch('/csv/all');
    const csvData = await response.text();
    const blob = new Blob([csvData], {type: 'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `power_data_all_${new Date().toISOString().slice(0,19)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
  
  async function downloadLast2HoursCSV() {
    const response = await fetch('/csv/last2h');
    const csvData = await response.text();
    const blob = new Blob([csvData], {type: 'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `power_data_last2h_${new Date().toISOString().slice(0,19)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
  
  async function clearCSV() {
    if (confirm('⚠️ Are you sure? This will delete ALL logged data!')) {
      const response = await fetch('/csv/clear', {method: 'POST'});
      if (response.ok) {
        alert('CSV data cleared');
        updateCSVInfo();
      } else {
        alert('Failed to clear CSV');
      }
    }
  }
  
  async function updateCSVInfo() {
    const response = await fetch('/csv/info');
    const info = await response.json();
    document.getElementById('csvInfo').innerHTML = `📊 File size: ${info.size_kb.toFixed(1)} KB | Total records: ${info.records} | Last 2h records: ${info.last2h_records}`;
  }
  
  document.getElementById('configForm').onsubmit = async (e) => {
    e.preventDefault();
    const payload = {
      wifi_ssid: document.getElementById('wifi_ssid').value,
      wifi_password: document.getElementById('wifi_password').value,
      mqtt_broker: document.getElementById('mqtt_broker').value,
      mqtt_port: parseInt(document.getElementById('mqtt_port').value),
      shunt_resistor: parseFloat(document.getElementById('shunt').value),
      max_current: parseFloat(document.getElementById('max_current').value),
      energy_cost: parseFloat(document.getElementById('cost_kwh').value),
      csv_log_interval: parseInt(document.getElementById('csv_interval').value)
    };
    const res = await fetch('/config', { method: 'POST', body: JSON.stringify(payload), headers: {'Content-Type': 'application/json'} });
    if (res.ok) {
      alert('Config saved, device will restart...');
      setTimeout(() => location.reload(), 3000);
    } else alert('Failed to save');
  };
  
  fetch('/config').then(r => r.json()).then(cfg => {
    document.getElementById('wifi_ssid').value = cfg.wifi_ssid || '';
    document.getElementById('mqtt_broker').value = cfg.mqtt_broker || '';
    document.getElementById('mqtt_port').value = cfg.mqtt_port || 1883;
    document.getElementById('shunt').value = cfg.shunt_resistor || 0.015;
    document.getElementById('max_current').value = cfg.max_current || 10.0;
    document.getElementById('cost_kwh').value = cfg.energy_cost || 0.15;
    document.getElementById('csv_interval').value = cfg.csv_log_interval || 10;
  });
  
  connectWebSocket();
  setInterval(updateCSVInfo, 10000);
  initChart();
</script>
</body>
</html>
"""

# ------------------------------------------------------------
#  HTTP SERVER HANDLER
# ------------------------------------------------------------
async def handle_client(reader, writer):
    global ws_clients
    try:
        first_line = await reader.readline()
        if not first_line:
            writer.close()
            return
        
        parts = first_line.decode().split()
        if len(parts) < 2:
            writer.close()
            return
        
        method, path = parts[0], parts[1]
        
        # WebSocket upgrade
        if method == "GET" and path == "/ws":
            # Read headers
            key_line = None
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode().strip()
                if not line:
                    break
                if "Sec-WebSocket-Key" in line:
                    key_line = line
            if key_line:
                key = key_line.split(":")[1].strip()
                accept = ubinascii.b2a_base64(uhashlib.sha1(key.encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()).strip()
                response = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept.decode()}\r\n\r\n"
                )
                writer.write(response.encode())
                await writer.drain()
                ws_clients.append((reader, writer))
                # Keep connection alive
                while True:
                    await asyncio.sleep(1)
                    if writer.is_closing():
                        break
                if (reader, writer) in ws_clients:
                    ws_clients.remove((reader, writer))
            return
        
        # Get history data (last 2 hours)
        elif method == "GET" and path == "/history":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
            # Get last 2 hours of data
            cutoff = time.time() - (2 * 3600)
            power_history = [{"t": h[0], "p": h[3]} for h in history if h[0] >= cutoff]
            voltage_history = [{"t": h[0], "v": h[1]} for h in history if h[0] >= cutoff]
            response = json.dumps({"power_history": power_history, "voltage_history": voltage_history})
            writer.write(response.encode())
            await writer.drain()
            writer.close()
            return
        
        # Download all CSV
        elif method == "GET" and path == "/csv/all":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Disposition: attachment; filename=power_data_all.csv\r\n\r\n")
            writer.write(get_all_csv().encode())
            await writer.drain()
            writer.close()
            return
        
        # Download last 2 hours CSV
        elif method == "GET" and path == "/csv/last2h":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/csv\r\nContent-Disposition: attachment; filename=power_data_last2h.csv\r\n\r\n")
            writer.write(get_csv_data_last_hours(2).encode())
            await writer.drain()
            writer.close()
            return
        
        # Get CSV info
        elif method == "GET" and path == "/csv/info":
            all_data = get_all_csv()
            total_records = len(all_data.split('\n')) - 2 if all_data else 0
            last2h_data = get_csv_data_last_hours(2)
            last2h_records = len(last2h_data.split('\n')) - 2 if last2h_data != "No data available" else 0
            last2h_records = max(0, last2h_records)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
            writer.write(json.dumps({"size_kb": get_csv_size(), "records": max(0, total_records), "last2h_records": last2h_records}).encode())
            await writer.drain()
            writer.close()
            return
        
        # Clear CSV
        elif method == "POST" and path == "/csv/clear":
            success = clear_csv()
            writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
            writer.write(b'{"status":"ok"}' if success else b'{"status":"error"}')
            await writer.drain()
            writer.close()
            return
        
        # Get config
        elif method == "GET" and path == "/config":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
            writer.write(json.dumps(config).encode())
            await writer.drain()
            writer.close()
            return
        
        # Save config
        elif method == "POST" and path == "/config":
            content_length = 0
            while True:
                line = await reader.readline()
                if not line:
                    break
                if line.startswith(b"Content-Length:"):
                    content_length = int(line.split(b":")[1].strip())
                if line == b"\r\n":
                    break
            body = await reader.read(content_length)
            try:
                new_config = json.loads(body.decode())
                for k, v in new_config.items():
                    if k in config:
                        config[k] = v
                save_config()
                writer.write(b"HTTP/1.1 200 OK\r\n\r\n{\"status\":\"ok\"}")
                await writer.drain()
                writer.close()
                # Schedule restart
                asyncio.create_task(restart_device())
            except Exception as e:
                print("Config save error:", e)
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
            return
        
        # Serve dashboard
        else:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
            writer.write(DASHBOARD_HTML.encode())
            await writer.drain()
            writer.close()
    except Exception as e:
        print("Handler error:", e)
        try:
            writer.close()
        except:
            pass

async def restart_device():
    await asyncio.sleep(1)
    print("Restarting device...")
    reset()

# ------------------------------------------------------------
#  WATCHDOG HEARTBEAT
# ------------------------------------------------------------
async def watchdog_heartbeat(wdt):
    """Feed the watchdog regularly"""
    while True:
        try:
            wdt.feed()
            await asyncio.sleep(1)
        except Exception as e:
            print("Watchdog feed error:", e)
            await asyncio.sleep(0.5)

async def health_check():
    """Check system health and reconnect if needed"""
    while True:
        await asyncio.sleep(10)
        
        # Check WiFi and reconnect if needed
        if not check_and_reconnect_wifi():
            print("⚠️ WiFi connection lost, retrying...")
        
        # Check if web server is still running
        global server_task
        if server_task is None or server_task.done():
            print("⚠️ Web server stopped, restarting...")
            server_task = asyncio.create_server(handle_client, "0.0.0.0", 80)
        
        # Force garbage collection to prevent memory issues
        gc.collect()
        print(f"Free memory: {gc.mem_free()} bytes")

# ------------------------------------------------------------
#  SENSOR LOOP
# ------------------------------------------------------------
async def sensor_loop(sensor):
    global last_update_time, total_energy_wh, last_csv_write_time, history
    start_time = time.time()
    last_update_time = start_time
    last_csv_write_time = start_time
    
    # Initialize CSV
    if CSV_LOG_ENABLED:
        init_csv_file()
    
    while True:
        try:
            now = time.time()
            dt = min(now - last_update_time, READ_INTERVAL_S * 2)
            
            # Read sensor with error handling
            try:
                v = sensor.voltage
                i = sensor.current
                p = sensor.power
                t = sensor.temperature
            except Exception as e:
                print("Sensor read error:", e)
                v = i = p = t = 0.0
                await asyncio.sleep(1)
                continue
            
            # Update energy
            energy_wh_inc = p * (dt / 3600.0)
            total_energy_wh += energy_wh_inc
            cost_dollars = total_energy_wh / 1000.0 * config["energy_cost"]
            
            latest_data = {
                "timestamp": now,
                "v": v,
                "i": i,
                "p": p,
                "temp": t,
                "energy_kwh": total_energy_wh / 1000.0,
                "cost": cost_dollars
            }
            
            latest.update(latest_data)
            latest["uptime"] = int(now - start_time)
            
            # Store history (keep last 2 hours = 7200 points)
            history.append((now, v, i, p))
            if len(history) > MAX_HISTORY:
                history.pop(0)
            
            # CSV logging
            if CSV_LOG_ENABLED and (now - last_csv_write_time) >= config.get("csv_log_interval", CSV_LOG_INTERVAL):
                if write_csv_row(latest_data):
                    last_csv_write_time = now
                    print(f"✓ CSV logged: {len(history)} points")
            
            # Prepare data for WebSocket
            cutoff = now - (2 * 3600)
            power_history = [{"t": h[0], "p": h[3]} for h in history if h[0] >= cutoff]
            voltage_history = [{"t": h[0], "v": h[1]} for h in history if h[0] >= cutoff]
            latest["power_history"] = power_history[-300:] if len(power_history) > 300 else power_history  # Limit for performance
            latest["voltage_history"] = voltage_history[-300:] if len(voltage_history) > 300 else voltage_history
            
            # Send to WebSocket clients
            msg = json.dumps(latest)
            for r, w in ws_clients[:]:
                success = await ws_send(w, msg)
                if not success:
                    try:
                        ws_clients.remove((r, w))
                        await w.close()
                    except:
                        pass
            
            # MQTT publish
            await mqtt_send(latest)
            
            # Console output
            print(f"V={v:.2f}V I={i:.3f}A P={p:.2f}W E={total_energy_wh/1000:.3f}kWh")
            
            last_update_time = now
            await asyncio.sleep(READ_INTERVAL_S)
            
        except Exception as e:
            print("Sensor loop error:", e)
            await asyncio.sleep(5)

# ------------------------------------------------------------
#  MAIN
# ------------------------------------------------------------
async def main():
    print("\n" + "="*50)
    print("POWER MONITOR WITH WATCHDOG & 2H GRAPH")
    print("="*50 + "\n")
    
    # Initialize watchdog
    try:
        wdt = WDT(timeout=WATCHDOG_TIMEOUT_MS)
        print(f"✓ Watchdog initialized ({WATCHDOG_TIMEOUT_MS}ms timeout)")
        asyncio.create_task(watchdog_heartbeat(wdt))
    except Exception as e:
        print("⚠️ Watchdog not available:", e)
    
    # Load config
    load_config()
    
    # Setup AP
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=AP_SSID, password=AP_PASSWORD, authmode=network.AUTH_WPA_WPA2_PSK)
    ap.ifconfig((AP_IP, "255.255.255.0", AP_IP, AP_IP))
    print(f"✓ AP started: {AP_SSID} @ {AP_IP}")
    
    # Connect to WiFi if configured
    if config["wifi_ssid"] and config["wifi_password"]:
        print(f"✓ Connecting to WiFi: {config['wifi_ssid']}")
        connect_wifi(config["wifi_ssid"], config["wifi_password"])
    
    # Initialize I2C and sensor
    try:
        i2c = I2C(0, sda=Pin(I2C_SDA), scl=Pin(I2C_SCL), freq=400000)
        devices = i2c.scan()
        print(f"✓ I2C devices found: {[hex(d) for d in devices]}")
        
        if INA238_ADDR not in devices:
            print(f"⚠️ INA238 not found at 0x{INA238_ADDR:X}")
        else:
            print(f"✓ INA238 detected at 0x{INA238_ADDR:X}")
        
        sensor = INA238(i2c, addr=INA238_ADDR,
                       shunt_ohm=config["shunt_resistor"],
                       max_amps=config["max_current"])
        print("✓ INA238 initialized")
    except Exception as e:
        print("❌ I2C/INA238 error:", e)
        print("Will retry in 10 seconds...")
        await asyncio.sleep(10)
        reset()
    
    # Start web server
    global server_task
    server_task = await asyncio.start_server(handle_client, "0.0.0.0", 80)
    print("✓ HTTP server running on port 80")
    
    # Start tasks
    asyncio.create_task(mqtt_publish_task())
    asyncio.create_task(health_check())
    
    # Run sensor loop
    await sensor_loop(sensor)

# Run everything with error handling
try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\n✓ Stopped by user")
except Exception as e:
    print(f"\n❌ Fatal error: {e}")
    print("Restarting in 10 seconds...")
    time.sleep(10)
    reset()
finally:
    asyncio.new_event_loop()