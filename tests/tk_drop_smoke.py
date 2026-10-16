"""Exercise real Xdnd messages addressed to the full app and nested preview.
Run on an X11 display: python3 tests/tk_drop_smoke.py
Uses only a temporary installation and temporary JPEG.
"""
import ctypes as C,time,sys,tempfile
from pathlib import Path
from unittest.mock import Mock
import tkinter as tk
from tkinter import ttk
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import App
class Data(C.Union):_fields_=[('b',C.c_char*20),('s',C.c_short*10),('l',C.c_long*5)]
class Client(C.Structure):_fields_=[('type',C.c_int),('serial',C.c_ulong),('send_event',C.c_int),('display',C.c_void_p),('window',C.c_ulong),('message_type',C.c_ulong),('format',C.c_int),('data',Data)]
class Event(C.Union):_fields_=[('client',Client),('pad',C.c_long*24)]
x=C.CDLL('libX11.so.6');x.XOpenDisplay.argtypes=[C.c_char_p];x.XOpenDisplay.restype=C.c_void_p
d=x.XOpenDisplay(None);assert d
x.XInternAtom.argtypes=[C.c_void_p,C.c_char_p,C.c_int];x.XInternAtom.restype=C.c_ulong
x.XSendEvent.argtypes=[C.c_void_p,C.c_ulong,C.c_int,C.c_long,C.POINTER(Event)]
x.XFlush.argtypes=[C.c_void_p]
def atom(name):return x.XInternAtom(d,name.encode(),False)
def send(window,name,values):
 e=Event();e.client.type=33;e.client.display=d;e.client.window=window;e.client.message_type=atom(name);e.client.format=32
 for i,v in enumerate(values):e.client.data.l[i]=v
 x.XSendEvent(d,window,False,0,C.byref(e));x.XFlush(d)
import core
from PIL import Image
fixture=tempfile.TemporaryDirectory();folder=Path(fixture.name)
settings=core.Settings(folder/'installation');settings.ensure()
root=tk.Tk();root.withdraw()
errors=[]
root.report_callback_exception=lambda *args: errors.append(str(args))
app=App(root,settings)
root.deiconify()
for _ in range(80):root.update();time.sleep(.025)
assert not app.busy
source=tk.Toplevel(root);source.geometry('100x50+800+100');root.update()
def pump():
 for _ in range(10):root.update();time.sleep(.02)
try:
 with tempfile.TemporaryDirectory() as tmp:
  path=Path(tmp)/'image.jpg';Image.new('RGB',(50,40),'red').save(path)
  callback=source.register(lambda offset,length:path.as_uri()+'\r\n')
  root.tk.call('selection','handle','-selection','XdndSelection','-type','text/uri-list',source._w,callback)
  root.tk.call('selection','own','-selection','XdndSelection',source._w)
  sid=source.winfo_id()
  import subprocess,re
  tree=subprocess.check_output(['xwininfo','-tree','-id',str(root.winfo_id())],text=True)
  parent=re.search(r'Parent window id: (0x[0-9a-f]+)',tree)[1]
  root.lift();root.update()
  for target in [app.preview.winfo_id(),root.winfo_id(),int(parent,0)]:
   app.clear()
   send(target,'XdndEnter',[sid,5<<24,atom('text/uri-list'),0,0]);pump()
   px=app.preview.winfo_rootx()+200;py=app.preview.winfo_rooty()+100
   send(target,'XdndPosition',[sid,0,(px<<16)|py,0,atom('XdndActionCopy')]);pump()
   send(target,'XdndDrop',[sid,0,0,0,0]);pump()
   assert app.media==[path], (target, app.media, errors)
   assert app.preview.original is not None, app.status.get()
 print('PASS: native drops to the preview canvas, main client and wrapper open and render the image.')
finally:app.executor.shutdown(wait=True);root.destroy();fixture.cleanup()
