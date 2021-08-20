"""
sim.py

Copyright (C) 2020-2021 Tomas Hlavacek (tmshlvck@gmail.com)

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.
This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
You should have received a copy of the GNU General Public License along with
this program. If not, see <http://www.gnu.org/licenses/>.
"""

# TODO: Documentation!!!

import usys
import uasyncio
import machine
import uio
import ujson


# Disable PIN:
# AT+CPIN="1234"
# AT+CLCK="SC",0,"1234"

DEBUG = True

# Utils for debugging
def d(msg):
  if DEBUG:
    print(msg)

lastasc2 = ''
async def readline(prompt=""):
  global lastasc2

  cin = uasyncio.StreamReader(usys.stdin)
  ss = uio.StringIO()
  ss.seek(0)
  ss_cur = 0

  usys.stdout.write("%s" % prompt)
  while True:
    ch = await cin.read(1)
    asc2 = ord(ch)
    if 31 < asc2 < 127: # ASCII printable characters
      ss.seek(ss_cur)
      ss.write(ch)
      ss_cur += 1
      usys.stdout.write(ch)
    #elif asc2 == 127: # DEL
    elif asc2 == 8: # BS
      ss_cur -= 1 if ss_cur > 0 else 0
      usys.stdout.write(ch)
    elif asc2 == 13 or asc2 == 10: # CR|LF
      if lastasc2 == 13 or lastasc2 == 10: # avoid duplicate LF from mpfshell
        continue
      else:
        lastasc2 = asc2

      usys.stdout.write(b'\n')
      break

    lastasc2 = asc2

  ss.seek(0)
  return ss.read(ss_cur)




class Queue:
  def __init__(self):
    self.e = uasyncio.Event()
    self.q = []


  async def put(self, elem):
    self.q.append(elem)
    self.e.set()


  async def get(self):
    while not (self.e.is_set() and len(self.q) > 0):
      await self.e.wait()

    elem = self.q.pop(0)
    if len(self.q) == 0:
      self.e.clear()

    return elem


class SIM:
  CMD_TIMEOUT = 5

  def __init__(self, pwr_pin, reset_pin, rx_pin, tx_pin, uart_num=1):
    self.pwr_pin = machine.Pin(pwr_pin, machine.Pin.OUT)
    self.reset_pin = machine.Pin(reset_pin, machine.Pin.OUT)
    self.rx_pin = rx_pin
    self.tx_pin = tx_pin
    self.uart_num = uart_num
    self.uart = None
    self.rdr = None
    self.wtr = None
    self.multiplextask = None
    self.mqttqueue = Queue()
    self.interactqueue = Queue()


  async def signal_reset(self):
    self.reset_pin.on()
    await uasyncio.sleep_ms(500)
    self.reset_pin.off()


  async def signal_pwr(self):
    self.pwr_pin.on()
    await uasyncio.sleep_ms(1500)
    self.pwr_pin.off()


  async def _input_drain(self):
    r = b''
    try:
      while True:
        r += await uasyncio.wait_for(self.rdr.readline(), 1)
    except uasyncio.TimeoutError:
      pass


  async def _rdr_multiplex(self):
    while True:
      try:
        l = await self.rdr.readline()
        if not l:
          continue
        #d("DBG: %s" % str(l))
        if l.startswith(b'+SMSUB:') and self.mqttqueue:
          await self.mqttqueue.put(l)
        if l.startswith(b'+CREG:'): # network registration event
          d("Unsolicited CREG: %s" % str(l))
        if l.startswith(b'*PSUTTZ:'): # network time event
          d("Unsolicited PSUTTZ: %s" % str(l))
        else:
          await self.interactqueue.put(l)
      except uasyncio.CancelledError:
        break
      except Exception as e:
        print('sim._rdr_multiplex exception:')
        usys.print_exception(e)


  async def init(self):
    await self.signal_reset()

    self.uart = machine.UART(self.uart_num)
    self.uart.init(rx=self.rx_pin, tx=self.tx_pin, baudrate=9600)
    self.rdr = uasyncio.StreamReader(self.uart)
    self.wtr = uasyncio.StreamWriter(self.uart)

    await self.signal_pwr() 
    await uasyncio.sleep(6)
    d("gsm: modem enabled")

    await self._input_drain()
    self.multiplextask = uasyncio.create_task(self._rdr_multiplex())
    for _ in [0,1]:
      for _ in range(0,10):
        try:
          await self.at('AT', 'OK', 1)
          break
        except uasyncio.TimeoutError:
          pass
        await uasyncio.sleep_ms(100)
      try:
        await self.at('AT', 'OK', self.CMD_TIMEOUT)
        break
      except uasyncio.TimeoutError:
        await self.signal_pwr()
    await self.at('AT+CMGF=1', 'OK', self.CMD_TIMEOUT) # set SMS mode to text



  async def deinit(self):
    await self.wtr.drain()
    if self.multiplextask:
      self.multiplextask.cancel()
      await self.multiplextask
      self.multiplextask = None
    try:
      self.rdr.close()
    except:
      pass
    self.rdr = None
    try:
      self.wtr.close()
    except:
      pass
    self.wtr = None
    self.uart.deinit()
    await uasyncio.sleep_ms(100)
    await self.signal_reset()
    await uasyncio.sleep_ms(100)
    await self.signal_pwr()
    d("gsm: modem disabled")


  async def debug_shell(self):
    async def readloop():
      try:
        while True:
          l = await self.interactqueue.get()
          print('gsm: %s' % l.strip())
      except:
        print("EXCEPTION in gsm readloop")

    rt = uasyncio.create_task(readloop())

    while True:
      l = await readline("gsm#")
      l = l.strip()
      if l == 'stop':
        break
      elif l == 'reset':
        await self.reset()
      elif l:
        await self.wtr.awrite(l.encode() + b"\r\n")

    rt.cancel()


  async def _at(self, cmd, expectend, partialend=False):
    """
    returns array of byte arrays (lines)
    """

    d("gsm< %s" % cmd)
    if type(cmd) is bytes:
      await self.wtr.awrite(cmd)
    else:
      await self.wtr.awrite(cmd.encode() + b"\r\n")
    r = []
    while True:
      rl = await self.interactqueue.get()
      try:
        l = rl.decode()
      except UnicodeError:
        l = str(rl)
      r.append(rl)

      if isinstance(expectend, list) or isinstance(expectend, set):
        if l.strip() in expectend:
          d("gsm> %s" % str(r))
          return r
      else:
        if l.strip() == expectend or (partialend and expectend in l.strip()):
          d("gsm> %s" % str(r))
          return r


  async def at(self, cmd, expectend, timeout=5, partialend=False):
    return await uasyncio.wait_for(self._at(cmd, expectend, partialend), timeout)


  DELIM = ','
  QUOTES = ["'", '"']
  @classmethod
  def _splitcsv(cls, l):
    acc = ''
    quote = None
    for c in l:
      if quote:
        acc += c
        if c == quote:
          quote = None
      else:
        if c == cls.DELIM:
          yield acc
          acc = ''
        elif c in cls.QUOTES:
          acc += c
          quote = c
        else:
          acc += c
    if acc:
      yield acc


  async def atcsv_multi(self, cmd, expectend, keystr, timeout=5, partialend=False):
    """
    cmd: 'string or byte array'
    expectend: 'OK or something to distingusih the last line'
    keystr: 'string to match in the output line, i.e. +CSI on line like +CSI: 1,2,3'
    """

    lns = await self.at(cmd, expectend, timeout, partialend)

    res = []
    for rl in lns:
      if rl:
        l = rl.decode().strip()
        if l.startswith(keystr):
          g = list(self._splitcsv(l))
          g[0] = g[0].split(':', 1)[1].lstrip()
          res.append(g)

    return res


  async def atcsv(self, cmd, expectend, keystr, timeout=5, partialend=False):
    res = await self.atcsv_multi(cmd, expectend, keystr, timeout, partialend)
    if res:
      return res[0]
    else:
      raise ValueError('Can not parse modem output: %s' % str(res))


  async def get_sms(self):
    return await self.atcsv_multi('AT+CMGL="ALL"', 'OK', '+CMGL:', self.CMD_TIMEOUT)


  async def del_sms(self, smsid):
    return await self.at('AT+CMGD=%d' % smsid, 'OK', self.CMD_TIMEOUT)


  async def send_sms(self, tel, msg):
    """
    AT+CMGS=<da>[,<toda>]<CR>textisentered<ctrl-Z/ESC>
    """
    bmsg = msg.encode()
    await self.wtr.awrite(('AT+CMGS="%s"\r\n' % tel).encode())
    await uasyncio.sleep(1)
    await self.wtr.awrite(bmsg+b'\x21')
    while True:
      ret = (await uasyncio.wait_for(self.interactqueue.get(), self.CMD_TIMEOUT)).decode().strip()
      if ret == 'OK':
        return


  async def connect_apn(self, apn, user=None, password=None):
    self.apn = apn
    await self.at("AT+CNMP=2", "OK", self.CMD_TIMEOUT) # autoselect GSM/LTE
    await self.at("AT+CMNB=3", "OK", self.CMD_TIMEOUT) # CAT-M or NB-IoT mode
    await self.at('AT+SAPBR=3,1,"APN","%s"' % self.apn, "OK", self.CMD_TIMEOUT)
    if user:
      await self.at('AT+SAPBR=3,1,"USER","%s"' % user, "OK", self.CMD_TIMEOUT)
    if password:
      await self.at('AT+SAPBR=3,1,"PWD","%s"' % password, "OK", self.CMD_TIMEOUT)
    return await self.at('AT+SAPBR=1,1', "OK", self.CMD_TIMEOUT)


  async def get_ntp(self, ntpserver, tzoffset=0):
    """
    tzoffset = timezone offset with recpect to GMT in hrs (-12..12)
    """
    await self.at('AT+CNTPCID=1', 'OK', self.CMD_TIMEOUT)
    await self.at('AT+CNTP="%s",%d,1' % (ntpserver, tzoffset*4), 'OK', self.CMD_TIMEOUT)
    return await self.atcsv('AT+CNTP', '+CNTP:', '+CNTP', self.CMD_TIMEOUT, True)


  async def get_netinfo(self):
    """
+CPSI: <System Mode>,<Operation Mode>,<MCC>-<MNC>,<TAC>,<SCellID>,<PCellID>,<Frequency Band>,<earfcn>,<dlbw>,<ulbw>,<RSRQ>,<RSRP>,<RSSI>,<RSSNR>

<System Mode> System mode. "NO SERVICE"
"GSM"
"LTE CAT-M1"
"LTE NB-IOT"
<Operation Mode> UE operation mode. "Online", "Offline", "Factory Test Mode", "Reset", "Low Power Mode".
<MCC> Mobile Country Code (first part of the PLMN code)
<MNC> Mobile Network Code (second part of the PLMN code)
<LAC> Location Area Code (hexadecimal digits)
<Cell ID> Service-cell Identify
<Absolute RF Ch Num> AFRCN for service-cell.
<Track LO Adjust> Track LO Adjust
<C1> Coefficient for base station selection
<C2> Coefficient for Cell re-selection
<TAC> Tracing Area Code
<SCellID> Serving Cell ID
<PCellID> Physical Cell ID
<Frequency Band> Frequency Band of active set
<earfcn> E-UTRA absolute radio frequency channel number for se
arching CAT-M or NB-IOT cells
<dlbw> Transmission bandwidth configuration of the serving cell o
n the downlink
<ulbw> Transmission bandwidth configuration of the serving cell
on the uplink
<RSRP> Current reference signal received power.Available for CA
T-M or NB-IOT. <RSRQ> Current reference signal receive quality as measured by L1. <RSSI> Current Received signal strength indicator
<RSSNR> Average reference signal signal-to-noise ratio of the servi
ng cell The value of SINR can be calculated according to <RSSNR>,the formula is as below:
SINR = 2 * <RSSNR> - 20
The range of SINR is from -20 to 30
    """
    return await self.atcsv('AT+CPSI?', 'OK', '+CPSI', self.CMD_TIMEOUT)


  async def get_signalinfo(self):
    """
+CSQ: <rssi>,<ber>

<rssi>
0 - 115 dBm or less
1 - 111 dBm
2...30 - 110... - 54 dBm
31 - 52 dBm or greater
99 not known or not detectable
<ber> (in percent):
0...7 As RXQUAL values in the table in GSM 05.08 [20] subclause 7.2.4
99 Not known or not detectable
    """
    return await self.atcsv('AT+CSQ', 'OK', '+CSQ', 5)


  async def get_netreg(self):
    """
    CREG: <n>,<stat>[,<lac>,<ci>,<netact>]
    n: 
    0 Disable network registration unsolicited result code
    1 Enable network registration unsolicited result code
    2 Enable network registration unsolicited result code with
location information(2 is only for 7000 series module which
support GPRS.)
    
    <stat>
    0 Not registered, MT is not currently searching a new
      operator to register to
    1 Registered, home network
    2 Not registered, but MT is currently searching a new
      operator to register to
    3 Registration denied
    4 Unknown
    5 Registered, roaming
    """
    return await self.atcsv('AT+CREG?', 'OK', '+CREG', 5)


  NETREG_TIMEOUT = 120
  async def wait_for_netreg(self):
    """
      wait until registered to network or timeout (120 sec) is reached
      when registered return
      when timeout raise Exception()
    """
    i = 0
    while i < self.NETREG_TIMEOUT:
      rs = await self.get_netreg()
      if int(rs[1]) == 1:
        return
      await uasyncio.sleep(1)
    else:
      raise Exception("Can not register to the network in timeout %d" % self.NETREG_TIMEOUT)


  async def get_time(self):
    """
    AT+CCLK?
    +CCLK: "21/05/10,22:10:39+08"
    """
    return (await self.atcsv('AT+CCLK?', 'OK', '+CCLK', self.CMD_TIMEOUT))[0].strip()


  async def enable_gnss(self):
    await self.at('AT+CGNSPWR=1', 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SGPIO=0,4,1,1', 'OK', self.CMD_TIMEOUT)


  async def disable_gnss(self):
    await self.at('AT+CGNSPWR=0', 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SGPIO=0,4,1,0', 'OK', self.CMD_TIMEOUT)


  async def get_gnss(self):
    """
+CGNSINF: <GNSS run status>,<Fix status>,<UTC date & Time>,<Latitude>,<Longitude>,<MSL Altitude>,<Speed Over
Ground>,<Course Over Ground>,<Fix Mode>,<Reserved1>,<HDOP>,<PDOP>,<VDOP>,<Reserved2>,<GNSS Satellites in View>,<GNSS Satellites Used>,<GLONASS Satellites Used>,<Reserved3>,<C/N0 max>,<HPA>,<VPA>

    """
    return await self.atcsv('AT+CGNSINF', 'OK', '+CGNSINF', self.CMD_TIMEOUT)


  async def mqtt_connect(self, host, user, passwd, clientid, port=1883):
    await self.at('AT+CNACT=1,"%s"' % self.apn, 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SMCONF="CLIENTID",%s' % clientid, 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SMCONF="URL","%s","%d"' % (host, port), 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SMCONF="USERNAME","%s"' % user, 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SMCONF="PASSWORD","%s"' % passwd, 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SMCONF="KEEPTIME",60', 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SMCONF="RETAIN",1', 'OK', self.CMD_TIMEOUT)
    await self.at('AT+SMCONN', 'OK', self.CMD_TIMEOUT)


  async def mqtt_pub(self, topic, msg, qos=1, retain=1):
    """
    +SMPUB: <topic>,<content length>,<qos>,<retain>
    <topic>Subscribe packet
    <qos>Send packet QOS level, range:  0~2
      0 = at most once
      1 = at least once
      2 = exactly once
    <content length>Message length,  range: 0~512
    <retain>Server hold message range: 0~1
    """
    bmsg = msg.encode()
    await self.wtr.awrite(('AT+SMPUB="%s",%d,%d,%d\r\n' % (topic, len(bmsg), qos, retain)).encode())
    await uasyncio.sleep(1)
    await self.wtr.awrite(bmsg+b'\r\n')
    while True:
      ret = (await uasyncio.wait_for(self.interactqueue.get(), self.CMD_TIMEOUT)).decode().strip()
      if ret == 'OK':
        return


  async def mqtt_getconnstatus(self):
    """
    Response
    +SMSTATE: <status>

    OK

    <status>
    0      Expression MQTT disconnect state
    1      Expression MQTT on-line state
    """
    return await self.atcsv('AT+SMSTATE?', 'OK', '+SMSTATE', self.CMD_TIMEOUT)


  async def mqtt_getappstatus(self):
    """
    +CNACT: <status>,<ip_addr>
    <status> 0 Deactived
             1 Actived
             2 Inoperation
    """
    return await self.atcsv('AT+CNACT?', 'OK', '+CNACT', self.CMD_TIMEOUT)


  async def mqtt_disconnect(self):
    await self.at('AT+SMDISC', 'OK', self.CMD_TIMEOUT)
    await self.at('AT+CNACT=0', 'OK', self.CMD_TIMEOUT)


  async def mqtt_sub(self, topic, qos=1):
    await self.at('AT+SMUNSUB="%s"' % topic, ['OK', 'ERROR'], self.CMD_TIMEOUT)
    await self.at('AT+SMSUB="%s",%d' % (topic, qos), 'OK', self.CMD_TIMEOUT)


  async def mqtt_unsub(self, topic):
    await self.at('AT+SMUNSUB="%s"' % topic, 'OK', self.CMD_TIMEOUT)


  async def mqtt_getmsg(self, topic):
    # ignoring topic, sorry
    #return list(self._splitcsv((await self.mqttqueue.get()))
    return str(await self.mqttqueue.get())




class MQTTUplink:
  ENABLE_GNSS = False
  PUB_INTERVAL = 600 # seconds
  RESTART_INTERVAL = 120 # seconds
  MAX_REPEATS = 5
  NAME = 'beetle'

  def __init__(self, config):
    self.config = config
    self.sim = None
    self.running = False
    self.outt = None
    self.cmdrt = None
    self.restarts = 0


  async def _cmd(self):
    while self.running:
      try:
        cmd = await self.sim.mqtt_getmsg('%s/cmd' % self.NAME)
        scmd = cmd.decode()

        print("RECEIVED COMMAND: %s" % scmd)
        if scmd == 'reset':
          machine.reset()
        elif scmd == 'uplinkstop':
          self.stop()

      except uasyncio.CancelledError:
        break 
      except Exception as e:
        print("Exception in uplink cmd loop:")
        usys.print_exception(e)


  async def _run(self, data):
    c = self.config
    self.running = True
    while self.running:
      try:
        self.sim = SIM(c['MODEM_POWER_PIN'], c['MODEM_RESET_PIN'], c['MODEM_RX_PIN'], c['MODEM_TX_PIN'])

        await self.sim.signal_reset()
        d('sim.init:')
        d(await self.sim.init())
        d(await self.sim.wait_for_netreg())
        d(await self.sim.connect_apn(c['MQTT_APN']))
        uasyncio.sleep(10)
        d('sim.get_netinfo:')
        d(await self.sim.get_netinfo())
        d('sim.get_signalinfo:')
        d(await self.sim.get_signalinfo())
        d('sim.get_inetreg:')
        d(await self.sim.get_netreg())
        d('sim.get_ntp:')
        d(await self.sim.get_ntp(c['NTP_SERVER']))
        if self.ENABLE_GNSS:
          d(await self.sim.enable_gnss())

        d('sim.mqtt_connect:')
        await self.sim.mqtt_connect(c['MQTT_BROKER'], c['MQTT_USER'], c['MQTT_PASS'], c['MQTT_CLIENTID'])

        connc = 0
        while True:
          await uasyncio.sleep(5)
          d('sim.mqtt_getconnstatus:')
          cstatus = int((await self.sim.mqtt_getconnstatus())[0])
          if cstatus == 1:
            break
          else:
            connc += 1
            if connc > self.MAX_REPEATS:
              raise Exception("Can not connect to MQTT.")
 
        d('subscribing to %s/cmd' % self.NAME)
        await self.sim.mqtt_sub('%s/cmd' % self.NAME)
        self.cmdrt = uasyncio.create_task(self._cmd())

        print("Uplink established")

        inexs = 0
        while self.running:
          try:
            ts = await self.sim.get_time()

            if self.ENABLE_GNSS:
              loc = await self.sim.get_gnss()
              await self.sim.mqtt_pub('%s/loc' % self.NAME, ujson.dumps(loc))

            for k in data:
              await self.sim.mqtt_pub('%s/%s' % (self.NAME, k), ujson.dumps(data[k]))

            await self.sim.mqtt_pub('%s/status' % (self.NAME,), ujson.dumps([ts, await self.get_status()]))
            # TODO: singal that that has been uplinked
            # if self.dataup_signal:
            #   self.dataup_signal()

            await uasyncio.sleep(self.PUB_INTERVAL)
            inexs = 0
          except Exception as e:
            inexs += 1
            print("Internal exception inside uplink run publish loop:")
            usys.print_exception(e)
            if inexs > 5:
              raise

        d('unsubscribing to %s/cmd' % self.NAME)
        await self.sim.mqtt_unsub('%s/cmd' % self.NAME)

        await self.sim.mqtt_disconnect()
      except uasyncio.CancelledError:
        break 
      except Exception as e:
        print("Exception in uplink run loop:")
        usys.print_exception(e)
      finally:
        self.restarts += 1
        d("Uplink cleanup...")
        if self.cmdrt:
          self.cmdrt.cancel()
          await self.cmdrt
          self.cmdrt = None
        if self.sim:
          await self.sim.deinit()
          self.sim = None

      if self.running:
        print("Uplink will be restarted after %d s" % self.RESTART_INTERVAL)
        await uasyncio.sleep(self.RESTART_INTERVAL)


  def start(self, data):
    self.outt = uasyncio.create_task(self._run(data))


  async def stop(self):
    self.running = False
    if self.cmdrt:
      self.cmdrt.cancel()
      await self.cmdrt
      self.cmdrt = None
    d('waiting for uplink tasks to stop')
    await self.outt


  async def get_status(self):
    res = {'running': self.running,
           'restarts': self.restarts }
    if self.running and self.sim:
      res['connected'] = await self.sim.get_netinfo()
      res['signal'] = await self.sim.get_signalinfo()
      res['app'] = await self.sim.mqtt_getappstatus()
      res['mqtt'] = await self.sim.mqtt_getconnstatus()

    return res

